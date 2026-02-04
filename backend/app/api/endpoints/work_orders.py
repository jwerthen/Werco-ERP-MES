from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Response
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role, get_audit_service
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.part import Part, PartType
from app.models.routing import Routing, RoutingOperation
from app.models.bom import BOM, BOMItem
from app.models.work_center import WorkCenter
from app.services.scheduling_service import SchedulingService
from app.core.realtime import safe_broadcast
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)
from app.schemas.work_order import (
    WorkOrderCreate, WorkOrderUpdate, WorkOrderResponse, WorkOrderSummary,
    WorkOrderOperationCreate, WorkOrderOperationUpdate, WorkOrderOperationResponse
)

router = APIRouter()


def _has_incomplete_predecessors(
    operations: List[WorkOrderOperation],
    sequence: int,
    current_operation_id: Optional[int] = None,
) -> bool:
    return any(
        op.sequence < sequence
        and op.status != OperationStatus.COMPLETE
        and (current_operation_id is None or op.id != current_operation_id)
        for op in operations
    )


def _release_first_group(work_order: WorkOrder) -> None:
    if not work_order.operations:
        return

    first_pending = min(
        (op for op in work_order.operations if op.status == OperationStatus.PENDING),
        key=lambda op: op.sequence,
        default=None,
    )
    if first_pending:
        first_pending.status = OperationStatus.READY


def _release_next_group(work_order: WorkOrder, completed_op: WorkOrderOperation) -> None:
    next_op = min(
        (
            op
            for op in work_order.operations
            if op.sequence > completed_op.sequence and op.status == OperationStatus.PENDING
        ),
        key=lambda x: x.sequence,
        default=None,
    )
    if next_op:
        next_op.status = OperationStatus.READY

def generate_work_order_number(db: Session) -> str:
    """Generate next work order number (WO-YYYYMMDD-XXX)"""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"WO-{today}-"
    
    last_wo = db.query(WorkOrder).filter(
        WorkOrder.work_order_number.like(f"{prefix}%")
    ).order_by(WorkOrder.work_order_number.desc()).first()
    
    if last_wo:
        last_num = int(last_wo.work_order_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:03d}"


@router.get("/", response_model=List[WorkOrderSummary])
def list_work_orders(
    skip: int = 0,
    limit: int = 100,
    status: Optional[WorkOrderStatus] = None,
    search: Optional[str] = None,
    include_deleted: bool = Query(False, description="Include soft-deleted work orders (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List work orders with summary info"""
    query = db.query(WorkOrder).options(joinedload(WorkOrder.part))
    
    # Filter out soft-deleted unless explicitly requested by admin
    if not include_deleted or current_user.role != UserRole.ADMIN:
        query = query.filter(WorkOrder.is_deleted == False)
    
    if status:
        query = query.filter(WorkOrder.status == status)
    else:
        # Default: exclude complete/closed/cancelled (only show active work orders)
        query = query.filter(WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]))
    
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                WorkOrder.work_order_number.ilike(search_filter),
                WorkOrder.customer_name.ilike(search_filter),
                WorkOrder.customer_po.ilike(search_filter),
                WorkOrder.lot_number.ilike(search_filter)
            )
        )
    
    work_orders = query.order_by(WorkOrder.priority, WorkOrder.due_date).offset(skip).limit(limit).all()
    
    result = []
    for wo in work_orders:
        summary = WorkOrderSummary(
            id=wo.id,
            work_order_number=wo.work_order_number,
            part_id=wo.part_id,
            part_number=wo.part.part_number if wo.part else None,
            part_name=wo.part.name if wo.part else None,
            part_type=wo.part.part_type.value if wo.part and wo.part.part_type else None,
            status=wo.status,
            priority=wo.priority,
            quantity_ordered=wo.quantity_ordered,
            quantity_complete=wo.quantity_complete,
            due_date=wo.due_date,
            customer_name=wo.customer_name,
        )
        result.append(summary)
    
    return result


@router.get("/preview-operations/{part_id}")
def preview_work_order_operations(
    part_id: int,
    quantity: float = 1,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Preview what operations would be generated for a part (for debugging)"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    result = {
        "part_id": part_id,
        "part_number": part.part_number,
        "part_type": part.part_type.value,
        "is_assembly": part.part_type == PartType.ASSEMBLY,
        "quantity": quantity,
        "bom_found": False,
        "bom_status": None,
        "bom_items_count": 0,
        "component_routings": [],
        "operations_preview": []
    }
    
    if part.part_type == PartType.ASSEMBLY:
        # Check for BOM
        bom = db.query(BOM).filter(
            BOM.part_id == part_id,
            BOM.is_active == True
        ).first()
        
        if bom:
            result["bom_found"] = True
            result["bom_status"] = bom.status
            
            # Get BOM items with component parts preloaded
            items = db.query(BOMItem).options(
                joinedload(BOMItem.component_part)
            ).filter(
                BOMItem.bom_id == bom.id
            ).order_by(
                BOMItem.item_number.asc(),
                BOMItem.id.asc()
            ).all()
            result["bom_items_count"] = len(items)

            component_ids = [item.component_part_id for item in items if item.component_part_id]
            routings_by_part_id = {}
            if component_ids:
                routings = db.query(Routing).options(
                    selectinload(Routing.operations).selectinload(RoutingOperation.work_center)
                ).filter(
                    Routing.part_id.in_(component_ids),
                    Routing.is_active == True,
                    Routing.status == "released"
                ).all()
                routings_by_part_id = {r.part_id: r for r in routings}

            for item in items:
                component = item.component_part
                if not component:
                    continue
                    
                # Check for routing
                routing = routings_by_part_id.get(component.id)
                
                comp_info = {
                    "part_id": component.id,
                    "part_number": component.part_number,
                    "quantity_per": float(item.quantity),
                    "total_qty": float(item.quantity) * quantity,
                    "has_routing": routing is not None,
                    "routing_status": routing.status if routing else None,
                    "routing_operations": []
                }
                
                if routing:
                    for op in sorted(routing.operations, key=lambda operation: operation.sequence):
                        if op.is_active:
                            work_center = op.work_center
                            comp_info["routing_operations"].append({
                                "sequence": op.sequence,
                                "name": op.name,
                                "work_center_id": op.work_center_id
                            })
                            # Add to operations_preview with full details
                            result["operations_preview"].append({
                                "name": f"{component.part_number} - {op.name}",
                                "work_center_id": op.work_center_id,
                                "work_center_name": work_center.name if work_center else "Unknown",
                                "setup_hours": op.setup_hours,
                                "run_hours_per_unit": op.run_hours_per_unit,
                                "component_part_id": component.id,
                                "component_part_number": component.part_number,
                                "component_quantity": float(item.quantity) * quantity,
                                "operation_group": component.part_number[:50] if component.part_number else None
                            })
                
                result["component_routings"].append(comp_info)

            assembly_routing = db.query(Routing).options(
                selectinload(Routing.operations).selectinload(RoutingOperation.work_center)
            ).filter(
                Routing.part_id == part_id,
                Routing.is_active == True,
                Routing.status == "released"
            ).first()

            if assembly_routing:
                active_assembly_ops = [
                    op for op in sorted(assembly_routing.operations, key=lambda op: op.sequence) if op.is_active
                ]
                non_inspection_ops = [op for op in active_assembly_ops if not _is_inspection_operation(op)]
                inspection_ops = [op for op in active_assembly_ops if _is_inspection_operation(op)]

                for op in non_inspection_ops + inspection_ops:
                    work_center = op.work_center
                    is_inspection = _is_inspection_operation(op)
                    result["operations_preview"].append({
                        "name": f"{'FINAL INSPECTION' if is_inspection else 'FINAL ASSEMBLY'}: {op.name}",
                        "work_center_id": op.work_center_id,
                        "work_center_name": work_center.name if work_center else "Unknown",
                        "setup_hours": op.setup_hours,
                        "run_hours_per_unit": op.run_hours_per_unit,
                        "component_part_id": None,
                        "component_part_number": part.part_number,
                        "component_quantity": quantity,
                        "operation_group": "INSPECT" if is_inspection else "ASSEMBLY"
                    })
    
    return result


def get_work_center_group(work_center: WorkCenter) -> str:
    """Get operation group name from work center type"""
    if not work_center:
        return "OTHER"
    wc_type = work_center.work_center_type.upper() if work_center.work_center_type else ""
    wc_name = work_center.name.upper() if work_center.name else ""
    
    # Map work center types to groups
    if "LASER" in wc_type or "LASER" in wc_name:
        return "LASER"
    elif "PRESS" in wc_type or "BRAKE" in wc_type or "BEND" in wc_name:
        return "BEND"
    elif "WELD" in wc_type or "WELD" in wc_name:
        return "WELD"
    elif "PAINT" in wc_type or "POWDER" in wc_type or "COAT" in wc_name:
        return "FINISH"
    elif "MACHINE" in wc_type or "CNC" in wc_type or "MILL" in wc_name or "LATHE" in wc_name:
        return "MACHINE"
    elif "ASSEMBLY" in wc_type or "ASSEM" in wc_name:
        return "ASSEMBLY"
    elif "INSPECT" in wc_type or "QC" in wc_name or "QUALITY" in wc_name:
        return "INSPECT"
    else:
        return wc_type or "OTHER"


def _is_inspection_operation(operation: RoutingOperation) -> bool:
    if operation.is_inspection_point:
        return True

    inspection_tokens = ("INSPECT", "INSPECTION", "QUALITY", "QC")
    text_fields = (
        (operation.name or "").upper(),
        (operation.description or "").upper(),
    )
    if any(token in field for field in text_fields for token in inspection_tokens):
        return True

    work_center = operation.work_center
    if not work_center:
        return False

    wc_fields = (
        (work_center.name or "").upper(),
        (work_center.work_center_type or "").upper(),
    )
    return any(token in field for field in wc_fields for token in inspection_tokens)


@router.post("/", response_model=WorkOrderResponse, status_code=status.HTTP_201_CREATED)
def create_work_order(
    work_order_in: WorkOrderCreate,
    request: Request,
    auto_routing: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new work order. If auto_routing=True, operations are auto-generated from part routing.
    For assembly parts with BOMs, component part routings follow BOM/routing sequence order."""
    
    # Initialize audit service
    audit = AuditService(db, current_user, request)
    
    # Verify part exists
    part = db.query(Part).filter(Part.id == work_order_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Generate work order number
    wo_number = generate_work_order_number(db)
    
    # Create work order
    wo_data = work_order_in.model_dump(exclude={"operations"})
    work_order = WorkOrder(
        **wo_data,
        work_order_number=wo_number,
        created_by=current_user.id
    )
    db.add(work_order)
    db.flush()  # Get the work order ID
    
    # Auto-generate operations from routing if enabled and no operations provided
    
    if auto_routing and not work_order_in.operations:
        is_assembly = part.part_type == PartType.ASSEMBLY
        
        # Check for BOM (for assemblies)
        bom = None
        if is_assembly:
            # Get BOM - prefer released, then draft
            bom = db.query(BOM).filter(
                BOM.part_id == work_order_in.part_id,
                BOM.is_active == True,
                BOM.status == "released"
            ).first()
            
            if not bom:
                bom = db.query(BOM).filter(
                    BOM.part_id == work_order_in.part_id,
                    BOM.is_active == True,
                    BOM.status == "draft"
                ).first()
            
            # Also try without status filter as fallback
            if not bom:
                bom = db.query(BOM).filter(
                    BOM.part_id == work_order_in.part_id,
                    BOM.is_active == True
                ).first()
            
            if bom:
                pass  # BOM found, will be used below
        
        if is_assembly and bom:
            # Assembly with BOM: follow BOM item order and component routing sequence
            _create_grouped_assembly_operations(
                db, work_order, bom, float(work_order_in.quantity_ordered)
            )
        else:
            # Simple part: use released routing only
            routing = db.query(Routing).options(
                selectinload(Routing.operations).selectinload(RoutingOperation.work_center)
            ).filter(
                Routing.part_id == work_order_in.part_id,
                Routing.is_active == True,
                Routing.status == "released"
            ).first()
            
            if routing:
                for rop in sorted(routing.operations, key=lambda x: x.sequence):
                    if not rop.is_active:
                        continue
                    work_center = rop.work_center
                    wo_op = WorkOrderOperation(
                        work_order_id=work_order.id,
                        sequence=rop.sequence,
                        operation_number=rop.operation_number or f"Op {rop.sequence}",
                        name=rop.name,
                        description=rop.description,
                        work_center_id=rop.work_center_id,
                        setup_time_hours=rop.setup_hours,
                        run_time_hours=float(rop.run_hours_per_unit or 0) * float(work_order_in.quantity_ordered),
                        status=OperationStatus.PENDING,
                        operation_group=get_work_center_group(work_center) if work_center else None
                    )
                    db.add(wo_op)
    else:
        # Create operations from input
        for op_data in work_order_in.operations:
            operation = WorkOrderOperation(
                work_order_id=work_order.id,
                **op_data.model_dump()
            )
            db.add(operation)
    
    db.commit()
    db.refresh(work_order)
    
    # Audit log for work order creation
    audit.log_create(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        new_values=work_order,
        extra_data={
            "part_number": part.part_number,
            "quantity": float(work_order.quantity_ordered),
            "auto_routing": auto_routing,
            "operation_count": len(work_order.operations)
        }
    )

    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_created",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    
    return work_order


def _create_grouped_assembly_operations(
    db: Session, 
    work_order: WorkOrder, 
    bom: BOM, 
    wo_quantity: float
):
    """Create assembly operations with component routing completion before final assembly."""

    bom_items = db.query(BOMItem).options(
        joinedload(BOMItem.component_part)
    ).filter(
        BOMItem.bom_id == bom.id
    ).order_by(
        BOMItem.item_number.asc(),
        BOMItem.id.asc()
    ).all()
    component_ids = [item.component_part_id for item in bom_items if item.component_part_id]

    routing_by_part_id = {}
    if component_ids:
        routings = db.query(Routing).options(
            selectinload(Routing.operations).selectinload(RoutingOperation.work_center)
        ).filter(
            Routing.part_id.in_(component_ids),
            Routing.is_active == True,
            Routing.status == "released"
        ).all()
        for routing in routings:
            existing = routing_by_part_id.get(routing.part_id)
            if not existing:
                routing_by_part_id[routing.part_id] = routing
    
    # Create work order operations with new sequences
    sequence = 10

    for item in bom_items:
        component = item.component_part
        if not component:
            continue

        # Calculate quantity needed for this component
        component_qty = float(item.quantity) * wo_quantity

        # Get routing for this component (prefer released, fall back to draft)
        routing = routing_by_part_id.get(component.id)

        if not routing:
            continue

        for rop in sorted(routing.operations, key=lambda operation: operation.sequence):
            if not rop.is_active:
                continue

            # Create descriptive name showing part info
            op_name = f"{component.part_number} - {rop.name}"

            # Create description with more context
            description_parts = []
            if rop.description:
                description_parts.append(rop.description)
            description_parts.append(f"Part: {component.name}")
            description_parts.append(f"Qty: {component_qty:.0f}")
            description = " | ".join(description_parts)

            wo_op = WorkOrderOperation(
                work_order_id=work_order.id,
                sequence=sequence,
                operation_number=f"Op {sequence}",
                name=op_name,
                description=description,
                work_center_id=rop.work_center_id,
                setup_time_hours=rop.setup_hours,
                run_time_hours=float(rop.run_hours_per_unit or 0) * component_qty,
                status=OperationStatus.PENDING,
                component_part_id=component.id,
                component_quantity=component_qty,
                operation_group=component.part_number[:50] if component.part_number else None
            )
            db.add(wo_op)

            sequence += 10
    
    # Add final assembly operation if the assembly part itself has a released routing
    assembly_routing = db.query(Routing).options(
        selectinload(Routing.operations).selectinload(RoutingOperation.work_center)
    ).filter(
        Routing.part_id == work_order.part_id,
        Routing.is_active == True,
        Routing.status == "released"
    ).first()
    
    if assembly_routing:
        active_assembly_ops = [
            op for op in sorted(assembly_routing.operations, key=lambda x: x.sequence) if op.is_active
        ]
        non_inspection_ops = [op for op in active_assembly_ops if not _is_inspection_operation(op)]
        inspection_ops = [op for op in active_assembly_ops if _is_inspection_operation(op)]

        for rop in non_inspection_ops + inspection_ops:
            is_inspection = _is_inspection_operation(rop)
            wo_op = WorkOrderOperation(
                work_order_id=work_order.id,
                sequence=sequence,
                operation_number=f"Op {sequence}",
                name=f"{'FINAL INSPECTION' if is_inspection else 'FINAL ASSEMBLY'}: {rop.name}",
                description=rop.description,
                work_center_id=rop.work_center_id,
                setup_time_hours=rop.setup_hours,
                run_time_hours=float(rop.run_hours_per_unit or 0) * wo_quantity,
                status=OperationStatus.PENDING,
                operation_group="INSPECT" if is_inspection else "ASSEMBLY"
            )
            db.add(wo_op)
            sequence += 10


@router.get("/{work_order_id}", response_model=WorkOrderResponse)
def get_work_order(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific work order with all operations"""
    work_order = db.query(WorkOrder).options(
        joinedload(WorkOrder.part),
        selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.component_part),
        selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.work_center)
    ).filter(WorkOrder.id == work_order_id).first()
    
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    # Normalize nullable numeric fields for serialization safety
    work_order.quantity_complete = work_order.quantity_complete or 0
    work_order.quantity_scrapped = work_order.quantity_scrapped or 0
    work_order.estimated_hours = work_order.estimated_hours or 0
    work_order.actual_hours = work_order.actual_hours or 0
    work_order.estimated_cost = work_order.estimated_cost or 0
    work_order.actual_cost = work_order.actual_cost or 0

    # Enrich operations with component part info and normalize nullables
    for op in work_order.operations:
        op.setup_time_hours = op.setup_time_hours or 0
        op.run_time_hours = op.run_time_hours or 0
        op.run_time_per_piece = op.run_time_per_piece or 0
        op.actual_setup_hours = op.actual_setup_hours or 0
        op.actual_run_hours = op.actual_run_hours or 0
        op.quantity_complete = op.quantity_complete or 0
        op.quantity_scrapped = op.quantity_scrapped or 0
        op.estimated_hours = float(op.setup_time_hours) + float(op.run_time_hours)
        op.actual_hours = float(op.actual_setup_hours) + float(op.actual_run_hours)
        op.work_center_name = op.work_center.name if op.work_center else None

        if op.component_part_id:
            component = op.component_part
            if component:
                op.component_part_number = component.part_number
                op.component_part_name = component.name
    
    return work_order


@router.get("/by-number/{wo_number}", response_model=WorkOrderResponse)
def get_work_order_by_number(
    wo_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a work order by work order number"""
    work_order = db.query(WorkOrder).options(
        joinedload(WorkOrder.operations)
    ).filter(WorkOrder.work_order_number == wo_number).first()
    
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    return work_order


@router.put("/{work_order_id}", response_model=WorkOrderResponse)
def update_work_order(
    work_order_id: int,
    work_order_in: WorkOrderUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(work_order, c.key) for c in work_order.__table__.columns}
    
    update_data = work_order_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(work_order, field, value)
    
    db.commit()
    db.refresh(work_order)
    
    # Audit log for update
    audit.log_update(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_values=old_values,
        new_values=work_order
    )

    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_updated",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_updated",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    
    return work_order


@router.delete("/{work_order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work_order(
    work_order_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete (only for draft/cancelled WOs)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Soft delete or permanently delete a work order.
    
    **Soft delete (default)**: Marks the work order as deleted but preserves data.
    
    **Hard delete**: Only allowed for draft or cancelled work orders.
    Permanently removes the record and associated operations.
    """
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    audit = AuditService(db, current_user, request)
    wo_number = work_order.work_order_number
    wo_id = work_order.id
    
    if hard_delete:
        # Only draft or cancelled can be hard deleted
        if work_order.status not in [WorkOrderStatus.DRAFT, WorkOrderStatus.CANCELLED]:
            raise HTTPException(
                status_code=400, 
                detail="Only draft or cancelled work orders can be hard deleted. Use soft delete instead."
            )
        
        # Delete operations first
        for op in work_order.operations:
            db.delete(op)
        
        db.delete(work_order)
        db.commit()
        
        audit.log_delete("work_order", wo_id, wo_number)
        safe_broadcast(
            broadcast_dashboard_update,
            {
                "event": "work_order_deleted",
                "work_order_id": wo_id,
                "status": "deleted",
            }
        )
        safe_broadcast(
            broadcast_work_order_update,
            wo_id,
            {
                "event": "work_order_deleted",
                "status": "deleted",
            }
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    
    # Soft delete - allowed for any status
    work_order.soft_delete(current_user.id)
    db.commit()
    
    audit.log_delete("work_order", wo_id, wo_number, soft_delete=True)
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_deleted",
            "work_order_id": wo_id,
            "status": "deleted",
        }
    )
    safe_broadcast(
        broadcast_work_order_update,
        wo_id,
        {
            "event": "work_order_deleted",
            "status": "deleted",
        }
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{work_order_id}/restore", summary="Restore a soft-deleted work order")
def restore_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Restore a soft-deleted work order."""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    if not work_order.is_deleted:
        raise HTTPException(status_code=400, detail="Work order is not deleted")
    
    audit = AuditService(db, current_user, request)
    
    work_order.restore()
    db.commit()
    
    audit.log_update("work_order", work_order.id, work_order.work_order_number,
                    old_values={"is_deleted": True},
                    new_values={"is_deleted": False},
                    action="restore")
    
    return {"message": f"Work order {work_order.work_order_number} restored"}


@router.post("/{work_order_id}/release", response_model=WorkOrderResponse)
def release_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Release a work order to production"""
    work_order = db.query(WorkOrder).options(joinedload(WorkOrder.part)).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    if work_order.status != WorkOrderStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft work orders can be released")
    
    # Verify has at least one operation
    if not work_order.operations:
        raise HTTPException(status_code=400, detail="Work order must have at least one operation")
    
    old_status = work_order.status.value
    work_order.status = WorkOrderStatus.RELEASED
    work_order.released_by = current_user.id
    work_order.released_at = datetime.utcnow()
    
    # Set first group to ready for assembly work orders
    _release_first_group(work_order)
    
    db.commit()

    work_center_ids = list({op.work_center_id for op in work_order.operations if op.work_center_id})
    SchedulingService(db).run_scheduling(
        work_center_ids=work_center_ids or None,
        horizon_days=90,
        optimize_setup=False,
        work_order_ids=[work_order.id]
    )
    
    # Audit log for status change
    audit = AuditService(db, current_user, request)
    audit.log_status_change(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_status=old_status,
        new_status="released"
    )

    db.refresh(work_order)
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_released",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_released",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    for wc_id in work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {
                "event": "work_order_released",
                "work_order_id": work_order.id,
            }
        )
    return work_order


@router.post("/{work_order_id}/start")
def start_work_order(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start a work order (set to in-progress)"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    if work_order.status not in [WorkOrderStatus.RELEASED, WorkOrderStatus.ON_HOLD]:
        raise HTTPException(status_code=400, detail="Work order must be released or on-hold to start")
    
    work_order.status = WorkOrderStatus.IN_PROGRESS
    if not work_order.actual_start:
        work_order.actual_start = datetime.utcnow()
    
    db.commit()
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_started",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_started",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    return {"message": "Work order started"}


@router.get("/{work_order_id}/material-requirements")
def get_material_requirements(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get BOM material requirements for a work order with quantities calculated"""
    from app.models.bom import BOM, BOMItem
    
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    # Get BOM for the part
    bom = db.query(BOM).filter(
        BOM.part_id == work_order.part_id,
        BOM.is_active == True
    ).first()
    
    if not bom:
        return {
            "work_order_id": work_order_id,
            "work_order_number": work_order.work_order_number,
            "quantity_ordered": float(work_order.quantity_ordered),
            "has_bom": False,
            "materials": []
        }
    
    # Get BOM items with component parts
    items = db.query(BOMItem).options(
        joinedload(BOMItem.component_part)
    ).filter(BOMItem.bom_id == bom.id).all()
    
    materials = []
    for item in items:
        component = item.component_part
        if component:
            qty_per_assembly = float(item.quantity)
            qty_required = qty_per_assembly * float(work_order.quantity_ordered)
            scrap_allowance = qty_required * float(item.scrap_factor or 0)
            total_required = qty_required + scrap_allowance
            
            materials.append({
                "bom_item_id": item.id,
                "item_number": item.item_number,
                "part_id": component.id,
                "part_number": component.part_number,
                "part_name": component.name,
                "part_type": component.part_type.value if hasattr(component.part_type, 'value') else component.part_type,
                "quantity_per_assembly": qty_per_assembly,
                "quantity_required": round(qty_required, 3),
                "scrap_factor": float(item.scrap_factor or 0),
                "scrap_allowance": round(scrap_allowance, 3),
                "total_required": round(total_required, 3),
                "unit_of_measure": item.unit_of_measure or component.unit_of_measure.value,
                "item_type": item.item_type.value if hasattr(item.item_type, 'value') else item.item_type,
                "is_optional": item.is_optional,
                "notes": item.notes
            })
    
    return {
        "work_order_id": work_order_id,
        "work_order_number": work_order.work_order_number,
        "quantity_ordered": float(work_order.quantity_ordered),
        "has_bom": True,
        "bom_id": bom.id,
        "bom_revision": bom.revision,
        "materials": sorted(materials, key=lambda x: x["item_number"])
    }


@router.post("/{work_order_id}/complete")
def complete_work_order(
    work_order_id: int,
    quantity_complete: float,
    quantity_scrapped: float = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]))
):
    """Complete a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    work_order.status = WorkOrderStatus.COMPLETE
    work_order.quantity_complete = quantity_complete
    work_order.quantity_scrapped = quantity_scrapped
    work_order.actual_end = datetime.utcnow()
    
    db.commit()
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_completed",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_completed",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    return {"message": "Work order completed"}


# Operation endpoints
@router.post("/{work_order_id}/operations", response_model=WorkOrderOperationResponse)
def add_operation(
    work_order_id: int,
    operation_in: WorkOrderOperationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Add an operation to a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    operation = WorkOrderOperation(
        work_order_id=work_order_id,
        **operation_in.model_dump()
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


@router.put("/operations/{operation_id}", response_model=WorkOrderOperationResponse)
def update_operation(
    operation_id: int,
    operation_in: WorkOrderOperationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an operation"""
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    update_data = operation_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(operation, field, value)
    
    db.commit()
    db.refresh(operation)
    return operation


@router.post("/operations/{operation_id}/start")
def start_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start an operation"""
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if _has_incomplete_predecessors(work_order.operations, operation.sequence, operation.id):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")
    
    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_start = datetime.utcnow()
    operation.started_by = current_user.id
    
    # Also update work order status if needed
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()
    
    db.commit()
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_started",
            "operation_id": operation.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_started",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
        }
    )
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_started",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
            }
        )
    return {"message": "Operation started"}


@router.post("/operations/{operation_id}/complete")
def complete_operation(
    operation_id: int,
    quantity_complete: float,
    quantity_scrapped: float = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Complete an operation"""
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if work_order and _has_incomplete_predecessors(work_order.operations, operation.sequence, operation.id):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")
    
    operation.status = OperationStatus.COMPLETE
    operation.quantity_complete = quantity_complete
    operation.quantity_scrapped = quantity_scrapped
    operation.actual_end = datetime.utcnow()
    operation.completed_by = current_user.id
    
    # Check if next operation/group should be set to ready
    work_order = operation.work_order
    affected_work_centers = {operation.work_center_id}
    if work_order:
        _release_next_group(work_order, operation)
        newly_ready_wcs = {op.work_center_id for op in work_order.operations if op.status == OperationStatus.READY}
        affected_work_centers |= {wc_id for wc_id in newly_ready_wcs if wc_id}

        scheduling_service = SchedulingService(db)
        scheduling_service.update_availability_rates(
            work_center_ids=list(affected_work_centers),
            horizon_days=90
        )
    
    db.commit()
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_completed",
            "operation_id": operation.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_completed",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
        }
    )
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_completed",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
            }
        )
    return {"message": "Operation completed"}
