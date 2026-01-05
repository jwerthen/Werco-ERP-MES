from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.work_center import WorkCenter
from app.models.time_entry import TimeEntry, TimeEntryType
from app.schemas.time_entry import ClockIn, ClockOut, TimeEntryResponse

router = APIRouter()


@router.get("/my-active-job")
def get_my_active_job(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the current user's active time entry (clocked in job)"""
    active_entry = db.query(TimeEntry).filter(
        and_(
            TimeEntry.user_id == current_user.id,
            TimeEntry.clock_out.is_(None)
        )
    ).first()
    
    if not active_entry:
        return {"active_job": None}
    
    # Get related data
    operation = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.id == active_entry.operation_id
    ).first()
    
    work_order = db.query(WorkOrder).options(
        joinedload(WorkOrder.part)
    ).filter(WorkOrder.id == active_entry.work_order_id).first()
    
    return {
        "active_job": {
            "time_entry_id": active_entry.id,
            "clock_in": active_entry.clock_in,
            "entry_type": active_entry.entry_type,
            "work_order_number": work_order.work_order_number if work_order else None,
            "part_number": work_order.part.part_number if work_order and work_order.part else None,
            "part_name": work_order.part.name if work_order and work_order.part else None,
            "operation_name": operation.name if operation else None,
            "operation_number": operation.operation_number if operation else None,
        }
    }


@router.post("/clock-in", response_model=TimeEntryResponse)
def clock_in(
    clock_in_data: ClockIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clock in to a work order operation"""
    # Check if user already clocked in
    existing = db.query(TimeEntry).filter(
        and_(
            TimeEntry.user_id == current_user.id,
            TimeEntry.clock_out.is_(None)
        )
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=400,
            detail="You are already clocked in. Please clock out first."
        )
    
    # Verify work order and operation
    operation = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.id == clock_in_data.operation_id
    ).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    if operation.work_order_id != clock_in_data.work_order_id:
        raise HTTPException(status_code=400, detail="Operation does not belong to this work order")
    
    # Update operation status
    if operation.status == OperationStatus.READY:
        operation.status = OperationStatus.IN_PROGRESS
        operation.actual_start = datetime.utcnow()
        operation.started_by = current_user.id
    
    # Update work order status
    work_order = operation.work_order
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()
    
    # Create time entry
    time_entry = TimeEntry(
        user_id=current_user.id,
        work_order_id=clock_in_data.work_order_id,
        operation_id=clock_in_data.operation_id,
        work_center_id=clock_in_data.work_center_id,
        entry_type=clock_in_data.entry_type,
        clock_in=datetime.utcnow(),
        notes=clock_in_data.notes
    )
    
    db.add(time_entry)
    db.commit()
    db.refresh(time_entry)
    
    return time_entry


@router.post("/clock-out/{time_entry_id}", response_model=TimeEntryResponse)
def clock_out(
    time_entry_id: int,
    clock_out_data: ClockOut,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clock out from a work order operation"""
    time_entry = db.query(TimeEntry).filter(
        and_(
            TimeEntry.id == time_entry_id,
            TimeEntry.user_id == current_user.id
        )
    ).first()
    
    if not time_entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    
    if time_entry.clock_out:
        raise HTTPException(status_code=400, detail="Already clocked out")
    
    # Update time entry
    time_entry.clock_out = datetime.utcnow()
    time_entry.duration_hours = (time_entry.clock_out - time_entry.clock_in).total_seconds() / 3600
    time_entry.quantity_produced = clock_out_data.quantity_produced
    time_entry.quantity_scrapped = clock_out_data.quantity_scrapped
    time_entry.scrap_reason = clock_out_data.scrap_reason
    time_entry.notes = clock_out_data.notes or time_entry.notes
    
    # Update operation actual hours
    operation = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.id == time_entry.operation_id
    ).first()
    
    if operation:
        if time_entry.entry_type == TimeEntryType.SETUP:
            operation.actual_setup_hours += time_entry.duration_hours
        else:
            operation.actual_run_hours += time_entry.duration_hours
        
        operation.quantity_complete += clock_out_data.quantity_produced
        operation.quantity_scrapped += clock_out_data.quantity_scrapped
    
    # Update work order totals
    work_order = db.query(WorkOrder).filter(
        WorkOrder.id == time_entry.work_order_id
    ).first()
    
    if work_order:
        work_order.actual_hours += time_entry.duration_hours
    
    db.commit()
    db.refresh(time_entry)
    
    return time_entry


@router.get("/work-center-queue/{work_center_id}")
def get_work_center_queue(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get operations queued at a work center"""
    operations = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(
        and_(
            WorkOrderOperation.work_center_id == work_center_id,
            WorkOrderOperation.status.in_([OperationStatus.READY, OperationStatus.IN_PROGRESS])
        )
    ).order_by(WorkOrderOperation.scheduled_start).all()
    
    queue = []
    for op in operations:
        wo = op.work_order
        queue.append({
            "operation_id": op.id,
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "part_number": wo.part.part_number if wo.part else None,
            "part_name": wo.part.name if wo.part else None,
            "operation_number": op.operation_number,
            "operation_name": op.name,
            "status": op.status,
            "quantity_ordered": wo.quantity_ordered,
            "quantity_complete": op.quantity_complete,
            "priority": wo.priority,
            "due_date": wo.due_date,
            "setup_time_hours": op.setup_time_hours,
            "run_time_hours": op.run_time_hours,
        })
    
    return {"queue": queue}


@router.get("/dashboard")
def shop_floor_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get shop floor dashboard data"""
    # Active work orders
    active_wos = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.IN_PROGRESS
    ).count()
    
    # Work orders due today
    from datetime import date
    due_today = db.query(WorkOrder).filter(
        and_(
            WorkOrder.due_date == date.today(),
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED])
        )
    ).count()
    
    # Overdue work orders
    overdue = db.query(WorkOrder).filter(
        and_(
            WorkOrder.due_date < date.today(),
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
        )
    ).count()
    
    # Work center status
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    wc_status = []
    for wc in work_centers:
        active_ops = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_center_id == wc.id,
                WorkOrderOperation.status == OperationStatus.IN_PROGRESS
            )
        ).count()
        
        queued_ops = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_center_id == wc.id,
                WorkOrderOperation.status == OperationStatus.READY
            )
        ).count()
        
        wc_status.append({
            "id": wc.id,
            "code": wc.code,
            "name": wc.name,
            "type": wc.work_center_type,
            "status": wc.current_status,
            "active_operations": active_ops,
            "queued_operations": queued_ops
        })
    
    # Recent completions
    recent = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.COMPLETE
    ).order_by(WorkOrder.actual_end.desc()).limit(5).all()
    
    return {
        "summary": {
            "active_work_orders": active_wos,
            "due_today": due_today,
            "overdue": overdue
        },
        "work_centers": wc_status,
        "recent_completions": [
            {
                "work_order_number": wo.work_order_number,
                "completed_at": wo.actual_end,
                "quantity_complete": wo.quantity_complete
            } for wo in recent
        ]
    }


@router.get("/active-users")
def get_active_shop_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of users currently clocked in"""
    active_entries = db.query(TimeEntry).options(
        joinedload(TimeEntry.user),
        joinedload(TimeEntry.work_order),
        joinedload(TimeEntry.operation),
        joinedload(TimeEntry.work_center)
    ).filter(TimeEntry.clock_out.is_(None)).all()
    
    users = []
    for entry in active_entries:
        users.append({
            "user_id": entry.user_id,
            "user_name": entry.user.full_name if entry.user else None,
            "work_order_number": entry.work_order.work_order_number if entry.work_order else None,
            "operation": entry.operation.name if entry.operation else None,
            "work_center": entry.work_center.name if entry.work_center else None,
            "clock_in": entry.clock_in,
            "entry_type": entry.entry_type
        })
    
    return {"active_users": users}
