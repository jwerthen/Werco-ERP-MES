from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List work orders with summary info"""
    query = db.query(WorkOrder).options(joinedload(WorkOrder.part))
    
    if status:
        query = query.filter(WorkOrder.status == status)
    else:
        # Default: exclude closed/cancelled
        query = query.filter(WorkOrder.status.not_in([WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]))
    
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


@router.post("/", response_model=WorkOrderResponse)
def create_work_order(
    work_order_in: WorkOrderCreate,
    auto_routing: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new work order. If auto_routing=True, operations are auto-generated from part routing."""
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
        routing = db.query(Routing).options(
            joinedload(Routing.operations)
        ).filter(
            Routing.part_id == work_order_in.part_id,
            Routing.is_active == True,
            Routing.status == "released"
        ).first()
        
        if routing:
            for rop in sorted(routing.operations, key=lambda x: x.sequence):
                if not rop.is_active:
                    continue
                wo_op = WorkOrderOperation(
                    work_order_id=work_order.id,
                    sequence=rop.sequence,
                    operation_number=rop.operation_number or f"Op {rop.sequence}",
                    name=rop.name,
                    description=rop.description,
                    work_center_id=rop.work_center_id,
                    setup_time_hours=rop.setup_hours,
                    run_time_hours=float(rop.run_hours_per_unit or 0) * float(work_order_in.quantity_ordered),
                    status=OperationStatus.PENDING
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
    return work_order


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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    update_data = work_order_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(work_order, field, value)
    
    db.commit()
    db.refresh(work_order)
    return work_order


@router.post("/{work_order_id}/release")
def release_work_order(
    work_order_id: int,
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
    
    work_order.status = WorkOrderStatus.RELEASED
    work_order.released_by = current_user.id
    work_order.released_at = datetime.utcnow()
    
    # Set first operation to ready
    if work_order.operations:
        work_order.operations[0].status = OperationStatus.READY
    
    db.commit()
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
    if next_ops:
        next_op = min(next_ops, key=lambda x: x.sequence)
        next_op.status = OperationStatus.READY
    
    db.commit()
    return {"message": "Operation completed"}
