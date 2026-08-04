from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.models.maintenance import (
    FREQUENCY_DAYS_MAP,
    MaintenanceFrequency,
    MaintenanceLog,
    MaintenancePriority,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService

router = APIRouter()

# Who may change maintenance records. Until this change every endpoint in the
# file was a bare ``get_current_user``, so a VIEWER could open, start and close
# maintenance work orders on any machine.
#
# Planning verbs (schedules, opening/editing a maintenance WO) match the
# ``work_orders:create``/``work_orders:edit`` role set the frontend already uses
# to gate production work-order planning. Performing verbs (start / complete /
# log an event) additionally admit OPERATOR, because the maintenance tech doing
# the work signs in as one -- the same split as ``work_orders:complete``.
# ``require_role`` always admits PLATFORM_ADMIN and superusers.
_PLANNING_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
_PERFORMING_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR]

# Widest calendar window a single request may ask for. Unbounded, the endpoint
# was one query-string edit away from serializing every maintenance work order
# a tenant has ever had.
_MAX_CALENDAR_DAYS = 366


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


def _work_center_in_company(db: Session, work_center_id: int, company_id: int) -> WorkCenter:
    """Resolve a work center inside the caller's tenant, or 404.

    Every maintenance row hangs off a work center, and until now none of the
    write paths checked whose machine it was: a caller could open a PM schedule,
    a maintenance work order or a log entry against another tenant's equipment
    -- and the serializers render ``work_center.name`` straight back, so the
    write doubled as a read of the foreign machine's name.

    Flat 404 (not 403), matching ``_assert_work_center_in_company`` in
    work_orders.py: a foreign work center must be indistinguishable from a
    nonexistent one, so the status code cannot be used as an existence oracle.
    Deliberately does NOT require ``is_active`` -- maintenance is exactly the
    work you schedule against a machine that is down.
    """
    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")
    return wc


def _generate_wo_number(db: Session) -> str:
    """Allocate the next MWO number.

    This scan is deliberately NOT tenant-scoped: ``MaintenanceWorkOrder.wo_number``
    carries a GLOBAL unique constraint, so a per-tenant sequence would hand two
    companies the same number and the second insert would fail. It reads only the
    highest number, never any other tenant's row content.
    """
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


def _same_tenant(row: Any, related: Any) -> Any:
    """Return ``related`` only when it belongs to the same tenant as ``row``.

    The write paths in this file now validate ``work_center_id`` against the
    caller's company, which stops NEW cross-tenant rows -- but it does nothing
    about rows written BEFORE that guard existed, and the relationship the
    serializers traverse carries no predicate of its own. A legacy maintenance
    row owned by company B pointing at company A's machine passes every
    ``company_id`` filter in the query (it really is B's row) and would still
    render company A's ``work_center.name`` straight back.

    Ingress is closed by the ``_work_center_in_company`` checks; this closes
    egress. A foreign relation reads as absent rather than raising, so a legacy
    row stays listable and correctable instead of 500-ing the whole page.
    """
    if related is None:
        return None
    return related if related.company_id == row.company_id else None


def _serialize_schedule(s: MaintenanceSchedule, wc: Optional[WorkCenter] = None) -> dict:
    wc = wc or _same_tenant(s, s.work_center)
    return {
        "id": s.id,
        "work_center_id": s.work_center_id,
        "work_center_name": wc.name if wc else None,
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
        "created_at": to_utc_iso(s.created_at),
        "updated_at": to_utc_iso(s.updated_at),
    }


def _serialize_wo(wo: MaintenanceWorkOrder) -> dict:
    wc = _same_tenant(wo, wo.work_center)
    return {
        "id": wo.id,
        "schedule_id": wo.schedule_id,
        "work_center_id": wo.work_center_id,
        "work_center_name": wc.name if wc else None,
        "wo_number": wo.wo_number,
        "maintenance_type": wo.maintenance_type.value if hasattr(wo.maintenance_type, "value") else wo.maintenance_type,
        "priority": wo.priority.value if hasattr(wo.priority, "value") else wo.priority,
        "status": wo.status.value if hasattr(wo.status, "value") else wo.status,
        "title": wo.title,
        "description": wo.description,
        "checklist_results": wo.checklist_results,
        "scheduled_date": wo.scheduled_date.isoformat() if wo.scheduled_date else None,
        "due_date": wo.due_date.isoformat() if wo.due_date else None,
        "started_at": to_utc_iso(wo.started_at),
        "completed_at": to_utc_iso(wo.completed_at),
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
        "created_at": to_utc_iso(wo.created_at),
        "updated_at": to_utc_iso(wo.updated_at),
    }


def _serialize_wo_with_derived_overdue(wo: MaintenanceWorkOrder, today: date) -> dict:
    """``_serialize_wo`` with the SCHEDULED -> OVERDUE label applied in the payload.

    The one place the overdue label is computed for a response. It is a label,
    not a write: see ``list_work_orders`` for why no GET in this file persists
    the transition any more.
    """
    row = _serialize_wo(wo)
    if wo.status == MaintenanceStatus.SCHEDULED and wo.due_date and wo.due_date < today:
        row["status"] = MaintenanceStatus.OVERDUE.value
    return row


def _serialize_log(log: MaintenanceLog) -> dict:
    wc = _same_tenant(log, log.work_center)
    return {
        "id": log.id,
        "work_center_id": log.work_center_id,
        "work_center_name": wc.name if wc else None,
        "maintenance_wo_id": log.maintenance_wo_id,
        "event_type": log.event_type,
        "description": log.description,
        "performed_by": log.performed_by,
        "event_date": to_utc_iso(log.event_date),
        "cost": log.cost,
        "created_at": to_utc_iso(log.created_at),
    }


# ── Schedule Endpoints ────────────────────────────────────────────────────


@router.get("/schedules")
def list_schedules(
    work_center_id: Optional[int] = None,
    is_active: Optional[bool] = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List PM schedules with optional filters"""
    query = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.company_id == company_id)
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
    company_id: int = Depends(get_current_company_id),
):
    """Get schedule detail"""
    schedule = (
        db.query(MaintenanceSchedule)
        .filter(MaintenanceSchedule.id == schedule_id, MaintenanceSchedule.company_id == company_id)
        .first()
    )
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _serialize_schedule(schedule)


@router.post("/schedules")
def create_schedule(
    data: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new PM schedule"""
    wc = _work_center_in_company(db, data.work_center_id, company_id)

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
    schedule.company_id = company_id
    db.add(schedule)
    # Logged BEFORE the terminal commit so the audit row commits atomically with
    # the change -- AuditService.log() only flushes, and a call placed after
    # db.commit() lands in a fresh transaction that get_db teardown rolls back.
    db.flush()
    audit.log_create("maintenance_schedule", schedule.id, schedule.name, new_values=schedule)
    db.commit()
    db.refresh(schedule)
    return _serialize_schedule(schedule, wc)


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    data: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a PM schedule"""
    schedule = (
        db.query(MaintenanceSchedule)
        .filter(MaintenanceSchedule.id == schedule_id, MaintenanceSchedule.company_id == company_id)
        .first()
    )
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    update_data = data.model_dump(exclude_unset=True)
    if "maintenance_type" in update_data:
        update_data["maintenance_type"] = MaintenanceType(update_data["maintenance_type"])
    if "frequency" in update_data:
        update_data["frequency"] = MaintenanceFrequency(update_data["frequency"])
    if "priority" in update_data:
        update_data["priority"] = MaintenancePriority(update_data["priority"])

    old_values = {c.key: getattr(schedule, c.key) for c in schedule.__table__.columns}

    for field, value in update_data.items():
        setattr(schedule, field, value)

    db.flush()
    audit.log_update(
        resource_type="maintenance_schedule",
        resource_id=schedule.id,
        resource_identifier=schedule.name,
        old_values=old_values,
        new_values=schedule,
    )
    db.commit()
    db.refresh(schedule)
    return _serialize_schedule(schedule)


@router.delete("/schedules/{schedule_id}")
def deactivate_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Deactivate a PM schedule.

    ``MaintenanceSchedule`` has no soft-delete mixin; ``is_active`` is the flag,
    so this is logged as an update carrying the flip (the shape
    ``delete_work_center`` uses for the same situation), not as a delete.
    """
    schedule = (
        db.query(MaintenanceSchedule)
        .filter(MaintenanceSchedule.id == schedule_id, MaintenanceSchedule.company_id == company_id)
        .first()
    )
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    old_values = {"is_active": schedule.is_active}
    schedule.is_active = False
    db.flush()
    audit.log_update(
        resource_type="maintenance_schedule",
        resource_id=schedule.id,
        resource_identifier=schedule.name,
        old_values=old_values,
        new_values={"is_active": schedule.is_active},
        description=f"Deactivated PM schedule {schedule.name}",
    )
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
    company_id: int = Depends(get_current_company_id),
):
    """List maintenance work orders with filters.

    A PURE READ, like ``/work-orders/overdue``. This endpoint used to walk the
    result set flipping SCHEDULED -> OVERDUE and ``db.commit()`` it: a status
    change persisted from a GET, with no actor behind it, no reason recorded and
    no ``AuditService`` row (invariant 2). A poll is not an actor. The label is
    derived into the payload instead, so the response is unchanged; the stored
    status only moves when someone acts (``PUT /work-orders/{id}``).

    Nothing reads a persisted OVERDUE any more either: ``get_dashboard`` and
    ``auto_evidence_service`` both compute it from ``due_date``, which is
    strictly more accurate -- their counts no longer depend on whether a human
    has loaded this page today.
    """
    query = db.query(MaintenanceWorkOrder).filter(MaintenanceWorkOrder.company_id == company_id)
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

    today = date.today()
    wos = query.order_by(MaintenanceWorkOrder.scheduled_date.desc()).all()
    return [_serialize_wo_with_derived_overdue(wo, today) for wo in wos]


@router.get("/work-orders/overdue")
def get_overdue_work_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all overdue maintenance work orders.

    A PURE READ. It used to run an unscoped SELECT and then commit a
    SCHEDULED -> OVERDUE status change on every matching row in EVERY tenant --
    a GET that rewrote other companies' maintenance records, with no actor
    intent behind it and no audit row recording who or why. The transition is
    no longer persisted here; it is derived for the response instead, which
    keeps the payload byte-identical for the caller's own rows.

    ``GET /maintenance/work-orders`` no longer persists it either -- see that
    handler. Both consumers of the overdue signal (``get_dashboard`` here, and
    ``auto_evidence_service._query_maintenance``) derive it from ``due_date``
    rather than reading a stored flag, so nothing depends on a page load having
    happened.
    """
    today = date.today()
    wos = (
        db.query(MaintenanceWorkOrder)
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
            MaintenanceWorkOrder.due_date < today,
        )
        .order_by(MaintenanceWorkOrder.due_date)
        .all()
    )
    return [_serialize_wo_with_derived_overdue(wo, today) for wo in wos]


@router.get("/work-orders/{wo_id}")
def get_work_order(
    wo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get maintenance work order detail"""
    wo = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.id == wo_id, MaintenanceWorkOrder.company_id == company_id)
        .first()
    )
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    return _serialize_wo(wo)


@router.post("/work-orders")
def create_work_order(
    data: WorkOrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new maintenance work order"""
    _work_center_in_company(db, data.work_center_id, company_id)

    # A foreign schedule_id would otherwise be accepted verbatim, and
    # complete_work_order writes last_completed_date / next_due_date straight
    # onto whatever schedule it points at -- a cross-tenant write laundered
    # through a legitimately-owned work order.
    if data.schedule_id is not None:
        owns_schedule = (
            db.query(MaintenanceSchedule.id)
            .filter(MaintenanceSchedule.id == data.schedule_id, MaintenanceSchedule.company_id == company_id)
            .first()
        )
        if not owns_schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")

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
    wo.company_id = company_id
    db.add(wo)
    db.flush()
    audit.log_create("maintenance_work_order", wo.id, wo.wo_number, new_values=wo)
    db.commit()
    db.refresh(wo)
    return _serialize_wo(wo)


@router.put("/work-orders/{wo_id}")
def update_work_order(
    wo_id: int,
    data: WorkOrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a maintenance work order"""
    wo = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.id == wo_id, MaintenanceWorkOrder.company_id == company_id)
        .first()
    )
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")

    update_data = data.model_dump(exclude_unset=True)
    if "priority" in update_data:
        update_data["priority"] = MaintenancePriority(update_data["priority"])
    if "status" in update_data:
        update_data["status"] = MaintenanceStatus(update_data["status"])

    old_values = {c.key: getattr(wo, c.key) for c in wo.__table__.columns}

    for field, value in update_data.items():
        setattr(wo, field, value)

    db.flush()
    audit.log_update(
        resource_type="maintenance_work_order",
        resource_id=wo.id,
        resource_identifier=wo.wo_number,
        old_values=old_values,
        new_values=wo,
    )
    db.commit()
    db.refresh(wo)
    return _serialize_wo(wo)


@router.post("/work-orders/{wo_id}/start")
def start_work_order(
    wo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PERFORMING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Start a maintenance work order"""
    wo = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.id == wo_id, MaintenanceWorkOrder.company_id == company_id)
        .first()
    )
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    if wo.status not in (MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE, MaintenanceStatus.ON_HOLD):
        raise HTTPException(status_code=400, detail=f"Cannot start work order in status '{wo.status.value}'")

    old_status = wo.status.value if hasattr(wo.status, "value") else wo.status
    wo.status = MaintenanceStatus.IN_PROGRESS
    wo.started_at = datetime.utcnow()

    # Log the event. company_id was missing here, and MaintenanceLog.company_id
    # is NOT NULL (migration 026 drops the interim server_default), so this
    # insert raised IntegrityError and the endpoint 500'd -- AFTER the status
    # change above had already been committed by its own db.commit(). The two
    # writes now share one commit, so the request either fully succeeds or
    # fully rolls back.
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=wo.work_center_id,
        maintenance_wo_id=wo.id,
        event_type="started",
        description=f"Maintenance work order {wo.wo_number} started",
        performed_by=current_user.id,
        event_date=datetime.utcnow(),
    )
    db.add(log)
    db.flush()
    audit.log_status_change(
        "maintenance_work_order", wo.id, wo.wo_number, old_status, MaintenanceStatus.IN_PROGRESS.value
    )
    db.commit()
    db.refresh(wo)

    return _serialize_wo(wo)


@router.post("/work-orders/{wo_id}/complete")
def complete_work_order(
    wo_id: int,
    data: WorkOrderComplete,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PERFORMING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Complete a maintenance work order"""
    wo = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.id == wo_id, MaintenanceWorkOrder.company_id == company_id)
        .first()
    )
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    if wo.status not in (MaintenanceStatus.IN_PROGRESS, MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE):
        raise HTTPException(status_code=400, detail=f"Cannot complete work order in status '{wo.status.value}'")

    now = datetime.utcnow()
    old_status = wo.status.value if hasattr(wo.status, "value") else wo.status
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

    # Update schedule if linked. Scoped: pre-fix rows may still carry a foreign
    # schedule_id (create_work_order accepted one until this change), and this
    # is the write that would advance another tenant's PM due date.
    if wo.schedule_id:
        schedule = (
            db.query(MaintenanceSchedule)
            .filter(MaintenanceSchedule.id == wo.schedule_id, MaintenanceSchedule.company_id == company_id)
            .first()
        )
        if schedule:
            schedule.last_completed_date = now.date()
            schedule.next_due_date = _calc_next_due(schedule.frequency, schedule.frequency_days, now.date())

    # Log the event. Same NOT NULL omission as start_work_order: the insert
    # raised IntegrityError and the endpoint 500'd after the completion had
    # already been committed. One commit now covers both.
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=wo.work_center_id,
        maintenance_wo_id=wo.id,
        event_type="completed",
        description=f"Maintenance work order {wo.wo_number} completed. Cost: ${wo.total_cost:.2f}",
        performed_by=current_user.id,
        event_date=now,
        cost=wo.total_cost,
    )
    db.add(log)
    db.flush()
    audit.log_status_change(
        "maintenance_work_order",
        wo.id,
        wo.wo_number,
        old_status,
        MaintenanceStatus.COMPLETED.value,
        extra_data={
            "total_cost": wo.total_cost,
            "downtime_minutes": wo.downtime_minutes,
            "actual_duration_hours": wo.actual_duration_hours,
            "schedule_id": wo.schedule_id,
        },
    )
    db.commit()
    db.refresh(wo)

    return _serialize_wo(wo)


# ── Calendar ──────────────────────────────────────────────────────────────


@router.get("/calendar")
def get_calendar(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get calendar view data for scheduled maintenance within date range.

    The window is capped (``_MAX_CALENDAR_DAYS``). This was the one unbounded
    read left in the file -- ``start_date``/``end_date`` are caller-supplied and
    there is no ``limit``, so a single request could serialize every maintenance
    work order a tenant has. Same posture as the list/export bounds in #193.
    """
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must not be before start_date")
    if (end_date - start_date).days > _MAX_CALENDAR_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range must not exceed {_MAX_CALENDAR_DAYS} days")

    wos = (
        db.query(MaintenanceWorkOrder)
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.scheduled_date >= start_date,
            MaintenanceWorkOrder.scheduled_date <= end_date,
        )
        .order_by(MaintenanceWorkOrder.scheduled_date)
        .all()
    )

    return [_serialize_wo(wo) for wo in wos]


# ── Dashboard ─────────────────────────────────────────────────────────────


@router.get("/dashboard")
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Dashboard metrics for maintenance management"""
    today = date.today()
    week_end = today + timedelta(days=7)
    month_start = today.replace(day=1)

    # Overdue count
    overdue_count = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
            MaintenanceWorkOrder.due_date < today,
        )
        .scalar()
        or 0
    )

    # Due this week
    due_this_week = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
            MaintenanceWorkOrder.due_date >= today,
            MaintenanceWorkOrder.due_date <= week_end,
        )
        .scalar()
        or 0
    )

    # Completed this month
    completed_this_month = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
            MaintenanceWorkOrder.completed_at >= month_start,
        )
        .scalar()
        or 0
    )

    # Total this month (completed + in-progress + scheduled for this month)
    total_this_month = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.scheduled_date >= month_start,
            MaintenanceWorkOrder.scheduled_date <= today,
        )
        .scalar()
        or 0
    )

    completion_rate = round((completed_this_month / total_this_month * 100), 1) if total_this_month > 0 else 0

    # Total cost this month
    total_cost_month = (
        db.query(func.sum(MaintenanceWorkOrder.total_cost))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
            MaintenanceWorkOrder.completed_at >= month_start,
        )
        .scalar()
        or 0
    )

    # In-progress
    in_progress = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.IN_PROGRESS,
        )
        .scalar()
        or 0
    )

    # MTBF / MTTR per work center (last 90 days). Both the work-center list and
    # the completed-WO read are tenant-scoped; the per-work-center query is also
    # collapsed into one .in_() read rather than one round trip per machine.
    ninety_days_ago = today - timedelta(days=90)
    work_centers = db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True).all()
    completed_by_wc: dict = {}
    if work_centers:
        completed_rows = (
            db.query(MaintenanceWorkOrder)
            .filter(
                MaintenanceWorkOrder.company_id == company_id,
                MaintenanceWorkOrder.work_center_id.in_([wc.id for wc in work_centers]),
                MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
                MaintenanceWorkOrder.completed_at >= ninety_days_ago,
            )
            .order_by(MaintenanceWorkOrder.completed_at)
            .all()
        )
        for row in completed_rows:
            completed_by_wc.setdefault(row.work_center_id, []).append(row)

    wc_metrics = []
    for wc in work_centers:
        completed_wos = completed_by_wc.get(wc.id, [])

        wo_count = len(completed_wos)
        if wo_count == 0:
            continue

        # MTTR = average actual duration
        durations = [w.actual_duration_hours for w in completed_wos if w.actual_duration_hours]
        mttr = round(sum(durations) / len(durations), 2) if durations else 0

        # MTBF = total operating hours / number of failures (corrective/emergency only)
        failure_count = sum(
            1 for w in completed_wos if w.maintenance_type in (MaintenanceType.CORRECTIVE, MaintenanceType.EMERGENCY)
        )
        operating_hours = 90 * (wc.capacity_hours_per_day or 8)
        mtbf = round(operating_hours / failure_count, 1) if failure_count > 0 else operating_hours

        total_downtime = sum(w.downtime_minutes or 0 for w in completed_wos)
        total_wc_cost = sum(w.total_cost or 0 for w in completed_wos)

        wc_metrics.append(
            {
                "work_center_id": wc.id,
                "work_center_name": wc.name,
                "work_center_code": wc.code,
                "wo_count": wo_count,
                "mtbf_hours": mtbf,
                "mttr_hours": mttr,
                "total_downtime_minutes": total_downtime,
                "total_cost": round(total_wc_cost, 2),
            }
        )

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
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get full maintenance history for a work center"""
    wc = _work_center_in_company(db, work_center_id, company_id)

    logs = (
        db.query(MaintenanceLog)
        .filter(
            MaintenanceLog.company_id == company_id,
            MaintenanceLog.work_center_id == work_center_id,
        )
        .order_by(MaintenanceLog.event_date.desc())
        .limit(limit)
        .all()
    )

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
    current_user: User = Depends(require_role(_PERFORMING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add a maintenance log entry"""
    _work_center_in_company(db, data.work_center_id, company_id)

    if data.maintenance_wo_id is not None:
        owns_wo = (
            db.query(MaintenanceWorkOrder.id)
            .filter(
                MaintenanceWorkOrder.id == data.maintenance_wo_id,
                MaintenanceWorkOrder.company_id == company_id,
            )
            .first()
        )
        if not owns_wo:
            raise HTTPException(status_code=404, detail="Work order not found")

    # company_id was missing here too; MaintenanceLog.company_id is NOT NULL, so
    # every call to this endpoint raised IntegrityError and 500'd.
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=data.work_center_id,
        maintenance_wo_id=data.maintenance_wo_id,
        event_type=data.event_type,
        description=data.description,
        performed_by=current_user.id,
        event_date=data.event_date or datetime.utcnow(),
        cost=data.cost,
    )
    db.add(log)
    db.flush()
    audit.log_create("maintenance_log", log.id, log.event_type, new_values=log)
    db.commit()
    db.refresh(log)
    return _serialize_log(log)
