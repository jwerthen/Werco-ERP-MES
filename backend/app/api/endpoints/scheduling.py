from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.work_center import WorkCenter
from app.services.scheduling_service import SchedulingService
from app.schemas.scheduling import (
    SchedulingRunRequest,
    SchedulingConflict,
    LoadChartRequest,
    LoadChartDataPoint
)
from app.core.queue import enqueue_job
from app.core.realtime import safe_broadcast
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)
from pydantic import BaseModel

router = APIRouter()


class ScheduleUpdate(BaseModel):
    scheduled_start: Optional[date] = None
    scheduled_end: Optional[date] = None


class WorkCenterUpdate(BaseModel):
    work_center_id: int


def _load_work_order_for_scheduling(db: Session, work_order_id: int) -> WorkOrder:
    work_order = db.query(WorkOrder).options(
        joinedload(WorkOrder.operations)
    ).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    if not work_order.operations:
        raise HTTPException(status_code=400, detail="Work order has no operations")
    return work_order


def _get_current_operation(work_order: WorkOrder) -> Tuple[List[WorkOrderOperation], WorkOrderOperation]:
    operations = sorted(work_order.operations, key=lambda op: op.sequence)
    current_op = next(
        (op for op in operations if op.status != OperationStatus.COMPLETE),
        None
    )
    if not current_op:
        raise HTTPException(status_code=400, detail="All operations are complete")
    return operations, current_op


def _resolve_work_center(db: Session, work_center_id: int) -> WorkCenter:
    work_center = db.query(WorkCenter).filter(
        WorkCenter.id == work_center_id,
        WorkCenter.is_active == True
    ).first()
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found or inactive")
    return work_center


def _operation_total_hours(operation: WorkOrderOperation) -> float:
    return max(0.0, float(operation.setup_time_hours or 0) + float(operation.run_time_hours or 0))


def _days_needed_for_operation(operation: WorkOrderOperation) -> int:
    total_hours = _operation_total_hours(operation)
    return max(1, int(total_hours / 8) + (1 if total_hours % 8 > 0 else 0))


def _apply_work_order_schedule(
    work_order: WorkOrder,
    operations: List[WorkOrderOperation],
    current_op: WorkOrderOperation,
    scheduled_start: date,
) -> Dict[str, Any]:
    current_op.scheduled_start = scheduled_start
    days_needed = _days_needed_for_operation(current_op)
    current_op.scheduled_end = scheduled_start + timedelta(days=days_needed - 1)

    if work_order.status in [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]:
        if current_op.status == OperationStatus.PENDING:
            current_op.status = OperationStatus.READY

    for op in operations:
        if op.sequence <= current_op.sequence:
            continue
        op.scheduled_start = None
        op.scheduled_end = None

    work_center_ids = {op.work_center_id for op in operations if op.work_center_id}
    return {
        "days_needed": days_needed,
        "work_center_ids": list(work_center_ids),
    }


def _build_daily_load_for_work_center(
    operations: List[WorkOrderOperation],
) -> Dict[date, float]:
    load_map: Dict[date, float] = {}
    for op in operations:
        if not op.scheduled_start:
            continue
        start_date = op.scheduled_start.date() if isinstance(op.scheduled_start, datetime) else op.scheduled_start
        end_date = start_date
        if op.scheduled_end:
            end_date = op.scheduled_end.date() if isinstance(op.scheduled_end, datetime) else op.scheduled_end
        if end_date < start_date:
            end_date = start_date

        span_days = (end_date - start_date).days + 1
        total_hours = _operation_total_hours(op)
        per_day_hours = total_hours / span_days if span_days > 0 else total_hours

        current = start_date
        while current <= end_date:
            load_map[current] = load_map.get(current, 0.0) + per_day_hours
            current += timedelta(days=1)
    return load_map


def _find_earliest_capacity_date(
    db: Session,
    operation: WorkOrderOperation,
    work_center_id: int,
    start_date: Optional[date],
    horizon_days: int,
) -> date:
    start = max(start_date or date.today(), date.today())
    wc = _resolve_work_center(db, work_center_id)
    daily_capacity = max(0.1, float(wc.capacity_hours_per_day or 8.0))

    scheduled_ops = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.work_center_id == work_center_id,
        WorkOrderOperation.status != OperationStatus.COMPLETE,
        WorkOrderOperation.scheduled_start.isnot(None),
        WorkOrderOperation.id != operation.id,
    ).all()
    daily_load = _build_daily_load_for_work_center(scheduled_ops)

    total_hours = _operation_total_hours(operation)
    days_needed = max(1, int(total_hours / daily_capacity) + (1 if total_hours % daily_capacity > 0 else 0))
    per_day_hours = total_hours / days_needed if days_needed > 0 else total_hours

    for offset in range(max(1, horizon_days)):
        candidate_start = start + timedelta(days=offset)
        can_fit = True
        for day_offset in range(days_needed):
            candidate_day = candidate_start + timedelta(days=day_offset)
            day_load = daily_load.get(candidate_day, 0.0)
            if day_load + per_day_hours > daily_capacity:
                can_fit = False
                break
        if can_fit:
            return candidate_start

    raise HTTPException(
        status_code=409,
        detail=(
            f"No available capacity for {wc.code} within {horizon_days} days. "
            "Adjust capacity, move work center, or schedule manually."
        ),
    )


def _broadcast_schedule_updates(
    work_order_id: int,
    operation_id: int,
    operation_work_center_id: Optional[int],
    work_center_ids: List[int],
) -> None:
    safe_broadcast(
        broadcast_work_order_update,
        work_order_id,
        {
            "event": "work_order_scheduled",
            "operation_id": operation_id,
            "work_center_id": operation_work_center_id,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_scheduled",
            "work_order_id": work_order_id,
            "operation_id": operation_id,
            "work_center_id": operation_work_center_id,
        }
    )
    for wc_id in work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {
                "event": "work_order_scheduled",
                "work_order_id": work_order_id,
                "operation_id": operation_id,
            }
        )


@router.get("/work-orders")
def get_schedulable_work_orders(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get work orders for scheduling view (shows WO with its current/first operation)"""
    query = db.query(WorkOrder).options(
        joinedload(WorkOrder.part),
        joinedload(WorkOrder.operations)
    ).filter(
        WorkOrder.status.in_([
            WorkOrderStatus.RELEASED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD
        ])
    )
    
    work_orders = query.order_by(
        WorkOrder.priority,
        WorkOrder.due_date
    ).all()
    
    result = []
    for wo in work_orders:
        if not wo.operations:
            continue
            
        # Get operations sorted by sequence
        operations = sorted(wo.operations, key=lambda op: op.sequence)
        
        # Find current operation (first non-complete operation)
        current_op = None
        for op in operations:
            if op.status != OperationStatus.COMPLETE:
                current_op = op
                break
        
        # If all complete, skip this work order
        if not current_op:
            continue
        
        # Filter by work center if specified
        if work_center_id and current_op.work_center_id != work_center_id:
            continue
        
        # Calculate total remaining hours
        remaining_hours = sum(
            float(op.setup_time_hours or 0) + float(op.run_time_hours or 0)
            for op in operations if op.status != OperationStatus.COMPLETE
        )
        
        result.append({
            "id": wo.id,
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "part_number": wo.part.part_number if wo.part else "",
            "part_name": wo.part.name if wo.part else "",
            "current_operation_id": current_op.id,
            "current_operation_name": current_op.name,
            "current_operation_number": current_op.operation_number,
            "current_operation_sequence": current_op.sequence,
            "work_center_id": current_op.work_center_id,
            "status": wo.status.value if hasattr(wo.status, 'value') else wo.status,
            "operation_status": current_op.status.value if hasattr(current_op.status, 'value') else current_op.status,
            "scheduled_start": current_op.scheduled_start.isoformat() if current_op.scheduled_start else None,
            "scheduled_end": current_op.scheduled_end.isoformat() if current_op.scheduled_end else None,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "quantity": float(wo.quantity_ordered),
            "quantity_complete": float(wo.quantity_complete or 0),
            "priority": wo.priority,
            "total_operations": len(operations),
            "operations_complete": sum(1 for op in operations if op.status == OperationStatus.COMPLETE),
            "remaining_hours": remaining_hours,
            "setup_hours": float(current_op.setup_time_hours or 0),
            "run_hours": float(current_op.run_time_hours or 0)
        })
    
    return result


@router.get("/jobs")
def get_scheduled_jobs(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all operations for scheduling view (legacy endpoint)"""
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


class WorkOrderScheduleUpdate(BaseModel):
    scheduled_start: date
    work_center_id: Optional[int] = None  # Override first operation's work center


class EarliestScheduleRequest(BaseModel):
    work_center_id: Optional[int] = None
    start_date: Optional[date] = None
    horizon_days: int = 90


@router.put("/work-orders/{work_order_id}/schedule")
def schedule_work_order(
    work_order_id: int,
    schedule: WorkOrderScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Schedule an entire work order by scheduling its first operation.
    The work order will automatically flow through subsequent operations as each completes.
    """
    work_order = _load_work_order_for_scheduling(db, work_order_id)
    operations, current_op = _get_current_operation(work_order)

    if schedule.work_center_id:
        _resolve_work_center(db, schedule.work_center_id)
        current_op.work_center_id = schedule.work_center_id

    schedule_result = _apply_work_order_schedule(
        work_order=work_order,
        operations=operations,
        current_op=current_op,
        scheduled_start=schedule.scheduled_start,
    )
    work_center_ids = schedule_result["work_center_ids"]
    db.commit()

    if work_center_ids:
        SchedulingService(db).update_availability_rates(
            work_center_ids=work_center_ids,
            horizon_days=90
        )
    _broadcast_schedule_updates(
        work_order_id=work_order.id,
        operation_id=current_op.id,
        operation_work_center_id=current_op.work_center_id,
        work_center_ids=work_center_ids,
    )

    return {
        "message": f"Work order {work_order.work_order_number} scheduled",
        "work_order_id": work_order_id,
        "first_operation_id": current_op.id,
        "scheduled_start": schedule.scheduled_start.isoformat(),
        "scheduled_end": current_op.scheduled_end.isoformat() if current_op.scheduled_end else None,
        "work_center_id": current_op.work_center_id,
        "total_operations": len(operations),
        "days_needed": schedule_result["days_needed"],
    }


@router.post("/work-orders/{work_order_id}/schedule-earliest")
def schedule_work_order_earliest(
    work_order_id: int,
    request: EarliestScheduleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Schedule a work order at the earliest available date with capacity."""
    work_order = _load_work_order_for_scheduling(db, work_order_id)
    operations, current_op = _get_current_operation(work_order)

    target_work_center_id = request.work_center_id or current_op.work_center_id
    if not target_work_center_id:
        raise HTTPException(status_code=400, detail="Current operation has no work center")

    _resolve_work_center(db, target_work_center_id)
    current_op.work_center_id = target_work_center_id

    earliest_start = _find_earliest_capacity_date(
        db=db,
        operation=current_op,
        work_center_id=target_work_center_id,
        start_date=request.start_date,
        horizon_days=request.horizon_days,
    )

    schedule_result = _apply_work_order_schedule(
        work_order=work_order,
        operations=operations,
        current_op=current_op,
        scheduled_start=earliest_start,
    )
    work_center_ids = schedule_result["work_center_ids"]
    db.commit()

    if work_center_ids:
        SchedulingService(db).update_availability_rates(
            work_center_ids=work_center_ids,
            horizon_days=90
        )
    _broadcast_schedule_updates(
        work_order_id=work_order.id,
        operation_id=current_op.id,
        operation_work_center_id=current_op.work_center_id,
        work_center_ids=work_center_ids,
    )

    return {
        "message": f"Work order {work_order.work_order_number} scheduled at earliest capacity",
        "work_order_id": work_order_id,
        "first_operation_id": current_op.id,
        "scheduled_start": earliest_start.isoformat(),
        "scheduled_end": current_op.scheduled_end.isoformat() if current_op.scheduled_end else None,
        "work_center_id": current_op.work_center_id,
        "total_operations": len(operations),
        "days_needed": schedule_result["days_needed"],
    }


@router.put("/operations/{operation_id}/schedule")
def schedule_operation(
    operation_id: int,
    schedule: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Schedule or reschedule an individual operation"""
    operation = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == operation_id).first()
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    operation.scheduled_start = schedule.scheduled_start
    operation.scheduled_end = schedule.scheduled_end
    db.commit()

    SchedulingService(db).update_availability_rates(
        work_center_ids=[operation.work_center_id],
        horizon_days=90
    )

    safe_broadcast(
        broadcast_work_order_update,
        operation.work_order_id,
        {
            "event": "operation_scheduled",
            "operation_id": operation.id,
            "work_center_id": operation.work_center_id,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_scheduled",
            "work_order_id": operation.work_order_id,
            "operation_id": operation.id,
            "work_center_id": operation.work_center_id,
        }
    )
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_scheduled",
                "work_order_id": operation.work_order_id,
                "operation_id": operation.id,
            }
        )

    return {"message": "Operation scheduled", "operation_id": operation_id}


@router.put("/operations/{operation_id}/work-center")
def update_operation_work_center(
    operation_id: int,
    update: WorkCenterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Move an operation to a different work center"""
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

    SchedulingService(db).update_availability_rates(
        work_center_ids=list({old_wc_id, update.work_center_id}),
        horizon_days=90
    )

    safe_broadcast(
        broadcast_work_order_update,
        operation.work_order_id,
        {
            "event": "operation_moved",
            "operation_id": operation.id,
            "old_work_center_id": old_wc_id,
            "new_work_center_id": update.work_center_id,
        }
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_moved",
            "work_order_id": operation.work_order_id,
            "operation_id": operation.id,
            "old_work_center_id": old_wc_id,
            "new_work_center_id": update.work_center_id,
        }
    )
    if old_wc_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            old_wc_id,
            {
                "event": "operation_moved",
                "work_order_id": operation.work_order_id,
                "operation_id": operation.id,
            }
        )
    safe_broadcast(
        broadcast_shop_floor_update,
        update.work_center_id,
        {
            "event": "operation_moved",
            "work_order_id": operation.work_order_id,
            "operation_id": operation.id,
        }
    )

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


@router.get("/capacity-heatmap")
def get_capacity_heatmap(
    start_date: str,
    end_date: str,
    work_center_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get per-day capacity utilization by work center with overload flags."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    query = db.query(WorkCenter).filter(WorkCenter.is_active == True)
    if work_center_id:
        query = query.filter(WorkCenter.id == work_center_id)
    work_centers = query.order_by(WorkCenter.code).all()
    if work_center_id and not work_centers:
        raise HTTPException(status_code=404, detail="Work center not found")

    if not work_centers:
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "overload_cells": 0,
            "overloaded_work_centers": [],
            "work_centers": [],
        }

    wc_ids = [wc.id for wc in work_centers]
    operations = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.work_center_id.in_(wc_ids),
        WorkOrderOperation.status != OperationStatus.COMPLETE,
        WorkOrderOperation.scheduled_start.isnot(None),
    ).all()

    daily_load_by_wc: Dict[int, Dict[date, Dict[str, float]]] = {}
    for wc in work_centers:
        daily_load_by_wc[wc.id] = {}
        cursor = start
        while cursor <= end:
            daily_load_by_wc[wc.id][cursor] = {"hours": 0.0, "jobs": 0.0}
            cursor += timedelta(days=1)

    for op in operations:
        if not op.work_center_id:
            continue
        op_start = op.scheduled_start.date() if isinstance(op.scheduled_start, datetime) else op.scheduled_start
        op_end = op_start
        if op.scheduled_end:
            op_end = op.scheduled_end.date() if isinstance(op.scheduled_end, datetime) else op.scheduled_end
        if op_end < op_start:
            op_end = op_start
        if op_end < start or op_start > end:
            continue

        total_hours = _operation_total_hours(op)
        span_days = (op_end - op_start).days + 1
        per_day_hours = total_hours / span_days if span_days > 0 else total_hours

        overlap_start = max(start, op_start)
        overlap_end = min(end, op_end)
        cursor = overlap_start
        while cursor <= overlap_end:
            bucket = daily_load_by_wc[op.work_center_id][cursor]
            bucket["hours"] += per_day_hours
            bucket["jobs"] += 1.0
            cursor += timedelta(days=1)

    overload_cells = 0
    overloaded_work_centers: Set[int] = set()
    result_rows: List[Dict[str, Any]] = []
    for wc in work_centers:
        daily_capacity = max(0.1, float(wc.capacity_hours_per_day or 8.0))
        day_rows: List[Dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            bucket = daily_load_by_wc[wc.id][cursor]
            scheduled_hours = float(bucket["hours"])
            utilization_pct = (scheduled_hours / daily_capacity * 100.0) if daily_capacity > 0 else 0.0
            overloaded = utilization_pct > 100.0
            if overloaded:
                overload_cells += 1
                overloaded_work_centers.add(wc.id)
            day_rows.append({
                "date": cursor.isoformat(),
                "scheduled_hours": round(scheduled_hours, 2),
                "capacity_hours": round(daily_capacity, 2),
                "utilization_pct": round(utilization_pct, 1),
                "job_count": int(bucket["jobs"]),
                "overloaded": overloaded,
            })
            cursor += timedelta(days=1)
        result_rows.append({
            "work_center_id": wc.id,
            "work_center_code": wc.code,
            "work_center_name": wc.name,
            "capacity_hours_per_day": round(daily_capacity, 2),
            "days": day_rows,
        })

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "overload_cells": overload_cells,
        "overloaded_work_centers": sorted(overloaded_work_centers),
        "work_centers": result_rows,
    }


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

    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "scheduling_run",
            "work_center_ids": request.work_center_ids,
            "horizon_days": request.horizon_days,
        }
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
