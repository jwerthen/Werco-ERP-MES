from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.part import Part
from pydantic import BaseModel

router = APIRouter()


class ScheduleUpdate(BaseModel):
    scheduled_start: Optional[date] = None
    scheduled_end: Optional[date] = None


@router.get("/jobs")
def get_scheduled_jobs(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all operations for scheduling view"""
    query = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).join(WorkOrder).filter(
        WorkOrder.status.in_([
            WorkOrderStatus.RELEASED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD
        ]),
        WorkOrderOperation.status != OperationStatus.COMPLETE
    )
    
    if work_center_id:
        query = query.filter(WorkOrderOperation.work_center_id == work_center_id)
    
    operations = query.order_by(
        WorkOrder.priority,
        WorkOrder.due_date,
        WorkOrderOperation.sequence
    ).all()
    
    result = []
    for op in operations:
        wo = op.work_order
        result.append({
            "id": op.id,
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "operation_id": op.id,
            "operation_name": op.name,
            "operation_number": op.operation_number,
            "sequence": op.sequence,
            "part_number": wo.part.part_number if wo.part else "",
            "part_name": wo.part.name if wo.part else "",
            "work_center_id": op.work_center_id,
            "status": op.status.value if hasattr(op.status, 'value') else op.status,
            "scheduled_start": op.scheduled_start.isoformat() if op.scheduled_start else None,
            "scheduled_end": op.scheduled_end.isoformat() if op.scheduled_end else None,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "quantity": wo.quantity_ordered,
            "priority": wo.priority,
            "setup_hours": op.setup_time_hours or 0,
            "run_hours": op.run_time_hours or 0
        })
    
    return result


@router.put("/operations/{operation_id}/schedule")
def schedule_operation(
    operation_id: int,
    schedule: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Schedule or reschedule an operation"""
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    operation.scheduled_start = schedule.scheduled_start
    operation.scheduled_end = schedule.scheduled_end
    
    db.commit()
    
    return {"message": "Operation scheduled", "operation_id": operation_id}


@router.get("/capacity")
def get_capacity_summary(
    start_date: str,
    end_date: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get capacity utilization by work center"""
    from app.models.work_center import WorkCenter
    from sqlalchemy import func
    
    # Get all work centers
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    
    # Calculate scheduled hours per work center
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    
    result = []
    for wc in work_centers:
        # Get operations scheduled in date range
        ops = db.query(WorkOrderOperation).filter(
            WorkOrderOperation.work_center_id == wc.id,
            WorkOrderOperation.scheduled_start != None,
            WorkOrderOperation.scheduled_start >= start,
            WorkOrderOperation.scheduled_start <= end,
            WorkOrderOperation.status != OperationStatus.COMPLETE
        ).all()
        
        total_hours = sum((op.setup_time_hours or 0) + (op.run_time_hours or 0) for op in ops)
        
        # Assume 8 hours/day capacity
        days = (end - start).days + 1
        available_hours = days * 8
        
        result.append({
            "work_center_id": wc.id,
            "work_center_code": wc.code,
            "work_center_name": wc.name,
            "scheduled_hours": total_hours,
            "available_hours": available_hours,
            "utilization_pct": (total_hours / available_hours * 100) if available_hours > 0 else 0,
            "operation_count": len(ops)
        })
    
    return result


@router.post("/auto-schedule")
def auto_schedule_operations(
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Auto-schedule unscheduled operations by priority and due date"""
    query = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order)
    ).join(WorkOrder).filter(
        WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]),
        WorkOrderOperation.status.in_([OperationStatus.PENDING, OperationStatus.READY]),
        WorkOrderOperation.scheduled_start == None
    )
    
    if work_center_id:
        query = query.filter(WorkOrderOperation.work_center_id == work_center_id)
    
    operations = query.order_by(
        WorkOrder.priority,
        WorkOrder.due_date,
        WorkOrderOperation.sequence
    ).all()
    
    # Simple scheduling: assign dates starting today
    current_date = date.today()
    scheduled_count = 0
    
    for op in operations:
        op.scheduled_start = current_date
        # Estimate end date based on hours (8 hours/day)
        total_hours = (op.setup_time_hours or 0) + (op.run_time_hours or 0)
        days_needed = max(1, int(total_hours / 8) + (1 if total_hours % 8 > 0 else 0))
        op.scheduled_end = current_date
        scheduled_count += 1
    
    db.commit()
    
    return {"message": f"Scheduled {scheduled_count} operations"}
