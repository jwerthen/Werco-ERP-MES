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
from app.services.scheduling_service import SchedulingService
from app.schemas.scheduling import (
    SchedulingRunRequest,
    SchedulingConflict,
    LoadChartRequest,
    LoadChartDataPoint
)
from app.core.queue import enqueue_job
from pydantic import BaseModel

router = APIRouter()


class ScheduleUpdate(BaseModel):
    scheduled_start: Optional[date] = None
    scheduled_end: Optional[date] = None


class WorkCenterUpdate(BaseModel):
    work_center_id: int


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


@router.put("/operations/{operation_id}/work-center")
def update_operation_work_center(
    operation_id: int,
    update: WorkCenterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Move an operation to a different work center"""
    from app.models.work_center import WorkCenter
    
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    # Verify target work center exists and is active
    work_center = db.query(WorkCenter).filter(
        WorkCenter.id == update.work_center_id,
        WorkCenter.is_active == True
    ).first()
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found or inactive")
    
    old_wc_id = operation.work_center_id
    operation.work_center_id = update.work_center_id
    
    db.commit()
    
    return {
        "message": "Operation moved to new work center",
        "operation_id": operation_id,
        "old_work_center_id": old_wc_id,
        "new_work_center_id": update.work_center_id,
        "new_work_center_code": work_center.code
    }


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
    """DEPRECATED: Use /run endpoint instead"""
    # Legacy endpoint - redirect to new constraint-based scheduling
    work_center_ids = [work_center_id] if work_center_id else None

    scheduling_service = SchedulingService(db)
    results = scheduling_service.run_scheduling(
        work_center_ids=work_center_ids,
        horizon_days=90,
        optimize_setup=False
    )

    return {
        "message": f"Scheduled {results['scheduled_count']} operations",
        "scheduled_count": results['scheduled_count'],
        "conflicts": results['conflict_count']
    }


@router.post("/run")
def run_scheduling(
    request: SchedulingRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Run constraint-based scheduling algorithm"""

    scheduling_service = SchedulingService(db)
    results = scheduling_service.run_scheduling(
        work_center_ids=request.work_center_ids,
        horizon_days=request.horizon_days,
        optimize_setup=request.optimize_setup
    )

    return results


@router.get("/conflicts", response_model=List[SchedulingConflict])
def get_scheduling_conflicts(
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get current scheduling conflicts (over-capacity situations)"""

    scheduling_service = SchedulingService(db)

    # Initialize capacity for all work centers
    from app.models.work_center import WorkCenter
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    scheduling_service._initialize_capacity(work_centers, 90)

    conflicts = scheduling_service.detect_conflicts(work_center_id)

    return conflicts


@router.post("/load-chart", response_model=List[LoadChartDataPoint])
def get_load_chart(
    request: LoadChartRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get work center load chart data"""

    scheduling_service = SchedulingService(db)

    # Initialize capacity
    from app.models.work_center import WorkCenter
    wc = db.query(WorkCenter).filter(WorkCenter.id == request.work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    days = (request.end_date - request.start_date).days
    scheduling_service._initialize_capacity([wc], max(days, 90))

    load_data = scheduling_service.get_load_chart(
        request.work_center_id,
        request.start_date,
        request.end_date
    )

    return load_data


@router.post("/run-background")
async def run_scheduling_background(
    request: SchedulingRunRequest,
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Queue scheduling run as background job"""

    await enqueue_job(
        "run_scheduling_job",
        work_center_ids=request.work_center_ids,
        horizon_days=request.horizon_days,
        optimize_setup=request.optimize_setup
    )

    return {"message": "Scheduling job queued"}
