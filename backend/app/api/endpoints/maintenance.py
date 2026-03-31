from typing import List, Optional
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.maintenance import (
    MaintenanceSchedule, MaintenanceWorkOrder, MaintenanceLog,
    MaintenanceType, MaintenancePriority, MaintenanceStatus, MaintenanceFrequency,
    FREQUENCY_DAYS_MAP,
)
from pydantic import BaseModel

router = APIRouter()


# ── Pydantic Schemas ──────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    work_center_id: int
    name: str
    description: Optional[str] = None
    maintenance_type: str = "preventive"
    frequency: str = "monthly"
    frequency_days: Optional[int] = None
    estimated_duration_hours: float = 1.0
    priority: str = "medium"
    checklist: Optional[str] = None  # JSON string
    requires_shutdown: bool = False
    assigned_to: Optional[int] = None
    next_due_date: Optional[date] = None


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    maintenance_type: Optional[str] = None
    frequency: Optional[str] = None
    frequency_days: Optional[int] = None
    estimated_duration_hours: Optional[float] = None
    priority: Optional[str] = None
    checklist: Optional[str] = None
    requires_shutdown: Optional[bool] = None
    assigned_to: Optional[int] = None
    next_due_date: Optional[date] = None
    is_active: Optional[bool] = None


class WorkOrderCreate(BaseModel):
    schedule_id: Optional[int] = None
    work_center_id: int
    maintenance_type: str = "preventive"
    priority: str = "medium"
    title: str
    description: Optional[str] = None
    checklist_results: Optional[str] = None
    scheduled_date: Optional[date] = None
    due_date: Optional[date] = None
    requires_shutdown: bool = False
    assigned_to: Optional[int] = None


class WorkOrderUpdate(BaseModel):
    priority: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    checklist_results: Optional[str] = None
    scheduled_date: Optional[date] = None
    due_date: Optional[date] = None
    requires_shutdown: Optional[bool] = None
    assigned_to: Optional[int] = None
    notes: Optional[str] = None


class WorkOrderComplete(BaseModel):
    checklist_results: Optional[str] = None
    findings: Optional[str] = None
    notes: Optional[str] = None
    parts_used: Optional[str] = None
    labor_cost: float = 0
    parts_cost: float = 0
    downtime_minutes: float = 0
    actual_duration_hours: Optional[float] = None


class LogCreate(BaseModel):
    work_center_id: int
    maintenance_wo_id: Optional[int] = None
    event_type: str
    description: str
    cost: float = 0
    event_date: Optional[datetime] = None


# ── Helper ────────────────────────────────────────────────────────────────

def _generate_wo_number(db: Session) -> str:
    year = datetime.utcnow().year
    prefix = f"MWO-{year}-"
    last = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.wo_number.like(f"{prefix}%"))
        .order_by(MaintenanceWorkOrder.id.desc())
        .first()
    )
    if last:
        try:
            seq = int(last.wo_number.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


def _calc_next_due(frequency: MaintenanceFrequency, frequency_days: Optional[int], from_date: date) -> date:
    days = FREQUENCY_DAYS_MAP.get(frequency, frequency_days or 30)
    return from_date + timedelta(days=days)


def _serialize_schedule(s: MaintenanceSchedule, wc: Optional[WorkCenter] = None) -> dict:
    return {
        "id": s.id,
        "work_center_id": s.work_center_id,
        "work_center_name": (wc.name if wc else (s.work_center.name if s.work_center else None)),
        "name": s.name,
        "description": s.description,
        "maintenance_type": s.maintenance_type.value if hasattr(s.maintenance_type, "value") else s.maintenance_type,
        "frequency": s.frequency.value if hasattr(s.frequency, "value") else s.frequency,
        "frequency_days": s.frequency_days,
        "estimated_duration_hours": s.estimated_duration_hours,
        "priority": s.priority.value if hasattr(s.priority, "value") else s.priority,
        "checklist": s.checklist,
        "requires_shutdown": s.requires_shutdown,
        "assigned_to": s.assigned_to,
        "last_completed_date": s.last_completed_date.isoformat() if s.last_completed_date else None,
        "next_due_date": s.next_due_date.isoformat() if s.next_due_date else None,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_wo(wo: MaintenanceWorkOrder) -> dict:
    return {
        "id": wo.id,
        "schedule_id": wo.schedule_id,
        "work_center_id": wo.work_center_id,
        "work_center_name": wo.work_center.name if wo.work_center else None,
        "wo_number": wo.wo_number,
        "maintenance_type": wo.maintenance_type.value if hasattr(wo.maintenance_type, "value") else wo.maintenance_type,
        "priority": wo.priority.value if hasattr(wo.priority, "value") else wo.priority,
        "status": wo.status.value if hasattr(wo.status, "value") else wo.status,
        "title": wo.title,
        "description": wo.description,
        "checklist_results": wo.checklist_results,
        "scheduled_date": wo.scheduled_date.isoformat() if wo.scheduled_date else None,
        "due_date": wo.due_date.isoformat() if wo.due_date else None,
        "started_at": wo.started_at.isoformat() if wo.started_at else None,
        "completed_at": wo.completed_at.isoformat() if wo.completed_at else None,
        "actual_duration_hours": wo.actual_duration_hours,
        "requires_shutdown": wo.requires_shutdown,
        "downtime_minutes": wo.downtime_minutes,
        "parts_used": wo.parts_used,
        "labor_cost": wo.labor_cost,
        "parts_cost": wo.parts_cost,
        "total_cost": wo.total_cost,
        "assigned_to": wo.assigned_to,
        "completed_by": wo.completed_by,
        "notes": wo.notes,
        "findings": wo.findings,
        "created_at": wo.created_at.isoformat() if wo.created_at else None,
        "updated_at": wo.updated_at.isoformat() if wo.updated_at else None,
    }


def _serialize_log(log: MaintenanceLog) -> dict:
    return {
        "id": log.id,
        "work_center_id": log.work_center_id,
        "work_center_name": log.work_center.name if log.work_center else None,
        "maintenance_wo_id": log.maintenance_wo_id,
        "event_type": log.event_type,
        "description": log.description,
        "performed_by": log.performed_by,
        "event_date": log.event_date.isoformat() if log.event_date else None,
        "cost": log.cost,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ── Schedule Endpoints ────────────────────────────────────────────────────

@router.get("/schedules")
def list_schedules(
    work_center_id: Optional[int] = None,
    is_active: Optional[bool] = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List PM schedules with optional filters"""
    query = db.query(MaintenanceSchedule)
    if is_active is not None:
        query = query.filter(MaintenanceSchedule.is_active == is_active)
    if work_center_id:
        query = query.filter(MaintenanceSchedule.work_center_id == work_center_id)
    schedules = query.order_by(MaintenanceSchedule.next_due_date).all()
    return [_serialize_schedule(s) for s in schedules]


@router.get("/schedules/{schedule_id}")
def get_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get schedule detail"""
    schedule = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _serialize_schedule(schedule)


@router.post("/schedules")
def create_schedule(
    data: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new PM schedule"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == data.work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    schedule = MaintenanceSchedule(
        work_center_id=data.work_center_id,
        name=data.name,
        description=data.description,
        maintenance_type=MaintenanceType(data.maintenance_type),
        frequency=MaintenanceFrequency(data.frequency),
        frequency_days=data.frequency_days,
        estimated_duration_hours=data.estimated_duration_hours,
        priority=MaintenancePriority(data.priority),
        checklist=data.checklist,
        requires_shutdown=data.requires_shutdown,
        assigned_to=data.assigned_to,
        next_due_date=data.next_due_date or date.today(),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return _serialize_schedule(schedule, wc)


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    data: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a PM schedule"""
    schedule = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    update_data = data.model_dump(exclude_unset=True)
    if "maintenance_type" in update_data:
        update_data["maintenance_type"] = MaintenanceType(update_data["maintenance_type"])
    if "frequency" in update_data:
        update_data["frequency"] = MaintenanceFrequency(update_data["frequency"])
    if "priority" in update_data:
        update_data["priority"] = MaintenancePriority(update_data["priority"])

    for field, value in update_data.items():
        setattr(schedule, field, value)

    db.commit()
    db.refresh(schedule)
    return _serialize_schedule(schedule)


@router.delete("/schedules/{schedule_id}")
def deactivate_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a PM schedule (soft delete)"""
    schedule = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.is_active = False
    db.commit()
    return {"message": "Schedule deactivated"}


# ── Work Order Endpoints ──────────────────────────────────────────────────

@router.get("/work-orders")
def list_work_orders(
    status: Optional[str] = None,
    work_center_id: Optional[int] = None,
    maintenance_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List maintenance work orders with filters"""
    query = db.query(MaintenanceWorkOrder)
    if status:
        query = query.filter(MaintenanceWorkOrder.status == status)
    if work_center_id:
        query = query.filter(MaintenanceWorkOrder.work_center_id == work_center_id)
    if maintenance_type:
        query = query.filter(MaintenanceWorkOrder.maintenance_type == maintenance_type)
    if start_date:
        query = query.filter(MaintenanceWorkOrder.scheduled_date >= start_date)
    if end_date:
        query = query.filter(MaintenanceWorkOrder.scheduled_date <= end_date)

    # Auto-mark overdue
    today = date.today()
    wos = query.order_by(MaintenanceWorkOrder.scheduled_date.desc()).all()
    for wo in wos:
        if wo.status == MaintenanceStatus.SCHEDULED and wo.due_date and wo.due_date < today:
            wo.status = MaintenanceStatus.OVERDUE
    db.commit()

    return [_serialize_wo(wo) for wo in wos]


@router.get("/work-orders/overdue")
def get_overdue_work_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all overdue maintenance work orders"""
    today = date.today()
    wos = db.query(MaintenanceWorkOrder).filter(
        MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
        MaintenanceWorkOrder.due_date < today,
    ).order_by(MaintenanceWorkOrder.due_date).all()

    for wo in wos:
        if wo.status == MaintenanceStatus.SCHEDULED:
            wo.status = MaintenanceStatus.OVERDUE
    db.commit()

    return [_serialize_wo(wo) for wo in wos]


@router.get("/work-orders/{wo_id}")
def get_work_order(
    wo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get maintenance work order detail"""
    wo = db.query(MaintenanceWorkOrder).filter(MaintenanceWorkOrder.id == wo_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    return _serialize_wo(wo)


@router.post("/work-orders")
def create_work_order(
    data: WorkOrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new maintenance work order"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == data.work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    wo_number = _generate_wo_number(db)

    wo = MaintenanceWorkOrder(
        schedule_id=data.schedule_id,
        work_center_id=data.work_center_id,
        wo_number=wo_number,
        maintenance_type=MaintenanceType(data.maintenance_type),
        priority=MaintenancePriority(data.priority),
        status=MaintenanceStatus.SCHEDULED,
        title=data.title,
        description=data.description,
        checklist_results=data.checklist_results,
        scheduled_date=data.scheduled_date or date.today(),
        due_date=data.due_date or data.scheduled_date or date.today(),
        requires_shutdown=data.requires_shutdown,
        assigned_to=data.assigned_to,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return _serialize_wo(wo)


@router.put("/work-orders/{wo_id}")
def update_work_order(
    wo_id: int,
    data: WorkOrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a maintenance work order"""
    wo = db.query(MaintenanceWorkOrder).filter(MaintenanceWorkOrder.id == wo_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")

    update_data = data.model_dump(exclude_unset=True)
    if "priority" in update_data:
        update_data["priority"] = MaintenancePriority(update_data["priority"])
    if "status" in update_data:
        update_data["status"] = MaintenanceStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(wo, field, value)

    db.commit()
    db.refresh(wo)
    return _serialize_wo(wo)


@router.post("/work-orders/{wo_id}/start")
def start_work_order(
    wo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a maintenance work order"""
    wo = db.query(MaintenanceWorkOrder).filter(MaintenanceWorkOrder.id == wo_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    if wo.status not in (MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE, MaintenanceStatus.ON_HOLD):
        raise HTTPException(status_code=400, detail=f"Cannot start work order in status '{wo.status.value}'")

    wo.status = MaintenanceStatus.IN_PROGRESS
    wo.started_at = datetime.utcnow()
    db.commit()
    db.refresh(wo)

    # Log the event
    log = MaintenanceLog(
        work_center_id=wo.work_center_id,
        maintenance_wo_id=wo.id,
        event_type="started",
        description=f"Maintenance work order {wo.wo_number} started",
        performed_by=current_user.id,
        event_date=datetime.utcnow(),
    )
    db.add(log)
    db.commit()

    return _serialize_wo(wo)


@router.post("/work-orders/{wo_id}/complete")
def complete_work_order(
    wo_id: int,
    data: WorkOrderComplete,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Complete a maintenance work order"""
    wo = db.query(MaintenanceWorkOrder).filter(MaintenanceWorkOrder.id == wo_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    if wo.status not in (MaintenanceStatus.IN_PROGRESS, MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE):
        raise HTTPException(status_code=400, detail=f"Cannot complete work order in status '{wo.status.value}'")

    now = datetime.utcnow()
    wo.status = MaintenanceStatus.COMPLETED
    wo.completed_at = now
    wo.completed_by = current_user.id
    wo.checklist_results = data.checklist_results
    wo.findings = data.findings
    wo.notes = data.notes
    wo.parts_used = data.parts_used
    wo.labor_cost = data.labor_cost
    wo.parts_cost = data.parts_cost
    wo.total_cost = data.labor_cost + data.parts_cost
    wo.downtime_minutes = data.downtime_minutes

    # Calculate duration
    if data.actual_duration_hours is not None:
        wo.actual_duration_hours = data.actual_duration_hours
    elif wo.started_at:
        duration = (now - wo.started_at).total_seconds() / 3600.0
        wo.actual_duration_hours = round(duration, 2)

    # Update schedule if linked
    if wo.schedule_id:
        schedule = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.id == wo.schedule_id).first()
        if schedule:
            schedule.last_completed_date = now.date()
            schedule.next_due_date = _calc_next_due(schedule.frequency, schedule.frequency_days, now.date())

    db.commit()
    db.refresh(wo)

    # Log the event
    log = MaintenanceLog(
        work_center_id=wo.work_center_id,
        maintenance_wo_id=wo.id,
        event_type="completed",
        description=f"Maintenance work order {wo.wo_number} completed. Cost: ${wo.total_cost:.2f}",
        performed_by=current_user.id,
        event_date=now,
        cost=wo.total_cost,
    )
    db.add(log)
    db.commit()

    return _serialize_wo(wo)


# ── Calendar ──────────────────────────────────────────────────────────────

@router.get("/calendar")
def get_calendar(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get calendar view data for scheduled maintenance within date range"""
    wos = db.query(MaintenanceWorkOrder).filter(
        MaintenanceWorkOrder.scheduled_date >= start_date,
        MaintenanceWorkOrder.scheduled_date <= end_date,
    ).order_by(MaintenanceWorkOrder.scheduled_date).all()

    return [_serialize_wo(wo) for wo in wos]


# ── Dashboard ─────────────────────────────────────────────────────────────

@router.get("/dashboard")
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard metrics for maintenance management"""
    today = date.today()
    week_end = today + timedelta(days=7)
    month_start = today.replace(day=1)

    # Overdue count
    overdue_count = db.query(func.count(MaintenanceWorkOrder.id)).filter(
        MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
        MaintenanceWorkOrder.due_date < today,
    ).scalar() or 0

    # Due this week
    due_this_week = db.query(func.count(MaintenanceWorkOrder.id)).filter(
        MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
        MaintenanceWorkOrder.due_date >= today,
        MaintenanceWorkOrder.due_date <= week_end,
    ).scalar() or 0

    # Completed this month
    completed_this_month = db.query(func.count(MaintenanceWorkOrder.id)).filter(
        MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
        MaintenanceWorkOrder.completed_at >= month_start,
    ).scalar() or 0

    # Total this month (completed + in-progress + scheduled for this month)
    total_this_month = db.query(func.count(MaintenanceWorkOrder.id)).filter(
        MaintenanceWorkOrder.scheduled_date >= month_start,
        MaintenanceWorkOrder.scheduled_date <= today,
    ).scalar() or 0

    completion_rate = round((completed_this_month / total_this_month * 100), 1) if total_this_month > 0 else 0

    # Total cost this month
    total_cost_month = db.query(func.sum(MaintenanceWorkOrder.total_cost)).filter(
        MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
        MaintenanceWorkOrder.completed_at >= month_start,
    ).scalar() or 0

    # In-progress
    in_progress = db.query(func.count(MaintenanceWorkOrder.id)).filter(
        MaintenanceWorkOrder.status == MaintenanceStatus.IN_PROGRESS,
    ).scalar() or 0

    # MTBF / MTTR per work center (last 90 days)
    ninety_days_ago = today - timedelta(days=90)
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    wc_metrics = []
    for wc in work_centers:
        completed_wos = db.query(MaintenanceWorkOrder).filter(
            MaintenanceWorkOrder.work_center_id == wc.id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
            MaintenanceWorkOrder.completed_at >= ninety_days_ago,
        ).order_by(MaintenanceWorkOrder.completed_at).all()

        wo_count = len(completed_wos)
        if wo_count == 0:
            continue

        # MTTR = average actual duration
        durations = [w.actual_duration_hours for w in completed_wos if w.actual_duration_hours]
        mttr = round(sum(durations) / len(durations), 2) if durations else 0

        # MTBF = total operating hours / number of failures (corrective/emergency only)
        failure_count = sum(1 for w in completed_wos if w.maintenance_type in (MaintenanceType.CORRECTIVE, MaintenanceType.EMERGENCY))
        operating_hours = 90 * (wc.capacity_hours_per_day or 8)
        mtbf = round(operating_hours / failure_count, 1) if failure_count > 0 else operating_hours

        total_downtime = sum(w.downtime_minutes or 0 for w in completed_wos)
        total_wc_cost = sum(w.total_cost or 0 for w in completed_wos)

        wc_metrics.append({
            "work_center_id": wc.id,
            "work_center_name": wc.name,
            "work_center_code": wc.code,
            "wo_count": wo_count,
            "mtbf_hours": mtbf,
            "mttr_hours": mttr,
            "total_downtime_minutes": total_downtime,
            "total_cost": round(total_wc_cost, 2),
        })

    return {
        "overdue_count": overdue_count,
        "due_this_week": due_this_week,
        "completed_this_month": completed_this_month,
        "completion_rate": completion_rate,
        "total_cost_month": round(total_cost_month, 2),
        "in_progress": in_progress,
        "work_center_metrics": wc_metrics,
    }


# ── History ───────────────────────────────────────────────────────────────

@router.get("/history/{work_center_id}")
def get_history(
    work_center_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get full maintenance history for a work center"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    logs = db.query(MaintenanceLog).filter(
        MaintenanceLog.work_center_id == work_center_id,
    ).order_by(MaintenanceLog.event_date.desc()).limit(limit).all()

    return {
        "work_center_id": wc.id,
        "work_center_name": wc.name,
        "logs": [_serialize_log(log) for log in logs],
    }


# ── Log ───────────────────────────────────────────────────────────────────

@router.post("/log")
def create_log(
    data: LogCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a maintenance log entry"""
    wc = db.query(WorkCenter).filter(WorkCenter.id == data.work_center_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    log = MaintenanceLog(
        work_center_id=data.work_center_id,
        maintenance_wo_id=data.maintenance_wo_id,
        event_type=data.event_type,
        description=data.description,
        performed_by=current_user.id,
        event_date=data.event_date or datetime.utcnow(),
        cost=data.cost,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _serialize_log(log)
