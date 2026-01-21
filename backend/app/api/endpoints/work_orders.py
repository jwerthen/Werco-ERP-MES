from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session, joinedload
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
from app.schemas.work_order import (
    WorkOrderCreate, WorkOrderUpdate, WorkOrderResponse, WorkOrderSummary,
    WorkOrderOperationCreate, WorkOrderOperationUpdate, WorkOrderOperationResponse
)

router = APIRouter()


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
            
            # Get BOM items
            items = db.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
            result["bom_items_count"] = len(items)
            
            for item in items:
                component = db.query(Part).filter(Part.id == item.component_part_id).first()
                if not component:
                    continue
                    
                # Check for routing
                routing = db.query(Routing).filter(
                    Routing.part_id == component.id,
                    Routing.is_active == True
                ).first()
                
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
                    for op in routing.operations:
                        if op.is_active:
                            work_center = db.query(WorkCenter).filter(WorkCenter.id == op.work_center_id).first()
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
                                "operation_group": get_work_center_group(work_center)
                            })
                
                result["component_routings"].append(comp_info)
            
            # Sort operations_preview by group order then work center
            group_order = {'LASER': 1, 'MACHINE': 2, 'BEND': 3, 'WELD': 4, 'FINISH': 5, 'ASSEMBLY': 6, 'INSPECT': 7, 'OTHER': 8}
            result["operations_preview"].sort(key=lambda x: (
                group_order.get(x.get('operation_group', 'OTHER'), 99),
                x.get('work_center_name', ''),
                x.get('component_part_number', '')
            ))
    
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


@router.post("/", response_model=WorkOrderResponse)
def create_work_order(
    work_order_in: WorkOrderCreate,
    request: Request,
    auto_routing: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new work order. If auto_routing=True, operations are auto-generated from part routing.
    For assembly parts with BOMs, component part routings are collected and grouped by work center type."""
    
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
            # Assembly with BOM: collect all component operations and group by work center
            _create_grouped_assembly_operations(
                db, work_order, bom, float(work_order_in.quantity_ordered)
            )
        else:
            # Simple part: use standard routing (prefer released, fall back to draft)
            routing = db.query(Routing).options(
                joinedload(Routing.operations)
            ).filter(
                Routing.part_id == work_order_in.part_id,
                Routing.is_active == True,
                Routing.status == "released"
            ).first()
            
            # Fall back to draft routing if no released routing exists
            if not routing:
                routing = db.query(Routing).options(
                    joinedload(Routing.operations)
                ).filter(
                    Routing.part_id == work_order_in.part_id,
                    Routing.is_active == True,
                    Routing.status == "draft"
                ).first()
            
            if routing:
                for rop in sorted(routing.operations, key=lambda x: x.sequence):
                    if not rop.is_active:
                        continue
                    work_center = db.query(WorkCenter).filter(WorkCenter.id == rop.work_center_id).first()
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
    
    return work_order


def _create_grouped_assembly_operations(
    db: Session, 
    work_order: WorkOrder, 
    bom: BOM, 
    wo_quantity: float
):
    """Create work order operations from component BOM items, grouped by work center type.
    
    This creates an intuitive workflow where:
    1. All similar operations (e.g., all laser cuts) are grouped together
    2. Operators can run similar parts back-to-back
    3. Each operation shows which part it's for and how many
    """
    
    # Collect all operations from all component parts
    all_operations = []  # List of (group, work_center_id, part, bom_item, routing_op)
    
    bom_items = db.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
    
    for item in bom_items:
        component = db.query(Part).filter(Part.id == item.component_part_id).first()
        if not component:
            continue
        
            
        # Calculate quantity needed for this component
        component_qty = float(item.quantity) * wo_quantity
        
        # Get routing for this component (prefer released, fall back to draft)
        routing = db.query(Routing).options(
            joinedload(Routing.operations)
        ).filter(
            Routing.part_id == component.id,
            Routing.is_active == True,
            Routing.status == "released"
        ).first()
        
        # Fall back to draft routing if no released routing exists
        if not routing:
            routing = db.query(Routing).options(
                joinedload(Routing.operations)
            ).filter(
                Routing.part_id == component.id,
                Routing.is_active == True,
                Routing.status == "draft"
            ).first()
        
        if not routing:
            continue
        
            
        for rop in routing.operations:
            if not rop.is_active:
                continue
            work_center = db.query(WorkCenter).filter(WorkCenter.id == rop.work_center_id).first()
            group = get_work_center_group(work_center)
            
            all_operations.append({
                'group': group,
                'work_center_id': rop.work_center_id,
                'work_center_name': work_center.name if work_center else "Unknown",
                'part': component,
                'bom_item': item,
                'routing_op': rop,
                'component_qty': component_qty,
                'original_sequence': rop.sequence
            })
    
    # Define group order (typical manufacturing flow)
    group_order = {
        'LASER': 1,
        'MACHINE': 2,
        'BEND': 3,
        'WELD': 4,
        'FINISH': 5,
        'ASSEMBLY': 6,
        'INSPECT': 7,
        'OTHER': 8
    }
    
    # Sort operations: first by group order, then by work center, then by part number
    all_operations.sort(key=lambda x: (
        group_order.get(x['group'], 99),
        x['work_center_name'],
        x['part'].part_number,
        x['original_sequence']
    ))
    
    
    # Create work order operations with new sequences
    sequence = 10
    current_group = None
    
    for op_data in all_operations:
        rop = op_data['routing_op']
        part = op_data['part']
        group = op_data['group']
        
        # Create descriptive name showing part info
        op_name = f"{part.part_number} - {rop.name}"
        
        # Create description with more context
        description_parts = []
        if rop.description:
            description_parts.append(rop.description)
        description_parts.append(f"Part: {part.name}")
        description_parts.append(f"Qty: {op_data['component_qty']:.0f}")
        description = " | ".join(description_parts)
        
        wo_op = WorkOrderOperation(
            work_order_id=work_order.id,
            sequence=sequence,
            operation_number=f"Op {sequence}",
            name=op_name,
            description=description,
            work_center_id=op_data['work_center_id'],
            setup_time_hours=rop.setup_hours,
            run_time_hours=float(rop.run_hours_per_unit or 0) * op_data['component_qty'],
            status=OperationStatus.PENDING,
            component_part_id=part.id,
            component_quantity=op_data['component_qty'],
            operation_group=group
        )
        db.add(wo_op)
        
        sequence += 10
    
    # Add final assembly operation if the assembly part itself has a routing
    assembly_routing = db.query(Routing).options(
        joinedload(Routing.operations)
    ).filter(
        Routing.part_id == work_order.part_id,
        Routing.is_active == True,
        Routing.status == "released"
    ).first()
    
    # Fall back to draft routing
    if not assembly_routing:
        assembly_routing = db.query(Routing).options(
            joinedload(Routing.operations)
        ).filter(
            Routing.part_id == work_order.part_id,
            Routing.is_active == True,
            Routing.status == "draft"
        ).first()
    
    if assembly_routing:
        for rop in sorted(assembly_routing.operations, key=lambda x: x.sequence):
            if not rop.is_active:
                continue
            work_center = db.query(WorkCenter).filter(WorkCenter.id == rop.work_center_id).first()
            wo_op = WorkOrderOperation(
                work_order_id=work_order.id,
                sequence=sequence,
                operation_number=f"Op {sequence}",
                name=f"FINAL: {rop.name}",
                description=rop.description,
                work_center_id=rop.work_center_id,
                setup_time_hours=rop.setup_hours,
                run_time_hours=float(rop.run_hours_per_unit or 0) * wo_quantity,
                status=OperationStatus.PENDING,
                operation_group=get_work_center_group(work_center) if work_center else "ASSEMBLY"
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
        joinedload(WorkOrder.operations),
        joinedload(WorkOrder.part)
    ).filter(WorkOrder.id == work_order_id).first()
    
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    # Enrich operations with component part info
    for op in work_order.operations:
        if op.component_part_id:
            component = db.query(Part).filter(Part.id == op.component_part_id).first()
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
    
    return work_order


@router.delete("/{work_order_id}")
def delete_work_order(
    work_order_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete (only for draft/cancelled WOs)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
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
        return {"message": f"Work order {wo_number} permanently deleted"}
    
    # Soft delete - allowed for any status
    work_order.soft_delete(current_user.id)
    db.commit()
    
    audit.log_delete("work_order", wo_id, wo_number, soft_delete=True)
    return {"message": f"Work order {wo_number} marked as deleted (soft delete)", "can_restore": True}


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


@router.post("/{work_order_id}/release")
def release_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Release a work order to production"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
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
    
    # Set first operation to ready
    if work_order.operations:
        work_order.operations[0].status = OperationStatus.READY
    
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
    
    return {"message": "Work order released", "work_order_number": work_order.work_order_number}


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
    items = db.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
    
    materials = []
    for item in items:
        component = db.query(Part).filter(Part.id == item.component_part_id).first()
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
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_start = datetime.utcnow()
    operation.started_by = current_user.id
    
    # Also update work order status if needed
    work_order = operation.work_order
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()
    
    db.commit()
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
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    operation.status = OperationStatus.COMPLETE
    operation.quantity_complete = quantity_complete
    operation.quantity_scrapped = quantity_scrapped
    operation.actual_end = datetime.utcnow()
    operation.completed_by = current_user.id
    
    # Check if next operation should be set to ready
    work_order = operation.work_order
    next_ops = [op for op in work_order.operations if op.sequence > operation.sequence]
    affected_work_centers = {operation.work_center_id}
    if next_ops:
        next_op = min(next_ops, key=lambda x: x.sequence)
        next_op.status = OperationStatus.READY
        affected_work_centers.add(next_op.work_center_id)

        scheduling_service = SchedulingService(db)
        if not next_op.scheduled_start:
            scheduling_service.run_scheduling(
                work_center_ids=list(affected_work_centers),
                horizon_days=90,
                optimize_setup=False,
                work_order_ids=[work_order.id]
            )
        else:
            scheduling_service.update_availability_rates(
                work_center_ids=list(affected_work_centers),
                horizon_days=90
            )
    else:
        SchedulingService(db).update_availability_rates(
            work_center_ids=list(affected_work_centers),
            horizon_days=90
        )
    
    db.commit()
    return {"message": "Operation completed"}
