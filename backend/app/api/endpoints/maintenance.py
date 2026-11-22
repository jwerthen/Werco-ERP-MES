import json
import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.audit_guard import required_audit
from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.credentials import is_api_token_credential
from app.core.time_utils import local_today, to_utc_iso
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.maintenance import (
    LOG_EVENT_COMPLETED,
    LOG_EVENT_STARTED,
    OPEN_MAINTENANCE_STATUSES,
    OPEN_SCHEDULE_WORK_ORDER_INDEX,
    MaintenanceAsset,
    MaintenanceFrequency,
    MaintenanceLog,
    MaintenanceLogEntryType,
    MaintenancePriority,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.maintenance import (
    MaintenanceAssetCreate,
    MaintenanceAssetResponse,
    MaintenanceAssetUpdate,
    PmImportResponse,
)
from app.services import downtime_service
from app.services.audit_service import AuditService
from app.services.company_time import company_today, company_window, company_zone
from app.services.import_service import MAX_IMPORT_FILE_BYTES, ImportFileError
from app.services.maintenance_checklist import (
    API_TOKEN_CHECKLIST_REMOVAL,
    API_TOKEN_INCOMPLETE_CHECKLIST,
    API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL,
    describe_items,
    parse_checklist,
    removed_checklist_items,
    removed_items,
    unticked_checklist_items,
)
from app.services.maintenance_due_service import DUE_WINDOW_DAYS, maintenance_due
from app.services.maintenance_interval import next_due_date
from app.services.maintenance_schedule_import_service import commit_pm_import, preview_pm_import

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

# A maintenance work order in one of these is over: any downtime it holds open is settled.
_TERMINAL_STATUSES = (MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED)
# ...and in one of these it is still open: its PM schedule takes no second one until it ends (the
# partial unique index ``uq_maintenance_work_orders_open_schedule`` holds the same set, migration 132).
_OPEN_STATUSES = OPEN_MAINTENANCE_STATUSES

# Widest calendar window a single request may ask for. Unbounded, the endpoint
# was one query-string edit away from serializing every maintenance work order
# a tenant has ever had.
_MAX_CALENDAR_DAYS = 366


# ── Pydantic Schemas ──────────────────────────────────────────────────────


# A maintenance record targets EXACTLY ONE of a production work center or a non-production
# equipment asset (migration 124's CHECK holds the same rule in the database). Enum fields are
# typed, so an unknown value -- the old form's ``usage_based`` frequency -- is a 422 rather
# than the ValueError 500 a hand-rolled ``MaintenanceFrequency(...)`` used to raise.
_TARGET_REQUIRED = "Exactly one of work_center_id or asset_id is required"
_TARGET_BOTH = "Give work_center_id or asset_id, not both"
_CUSTOM_NEEDS_DAYS = "frequency_days is required when frequency is custom"


def _refuse_nulls(model: BaseModel, fields: Tuple[str, ...]) -> None:
    nulled = [name for name in fields if name in model.model_fields_set and getattr(model, name) is None]
    if nulled:
        raise ValueError(f"{', '.join(nulled)} cannot be null")


def _strip_required(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("must not be blank")
    return text


class ScheduleCreate(BaseModel):
    work_center_id: Optional[int] = None
    asset_id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    maintenance_type: MaintenanceType = MaintenanceType.PREVENTIVE
    frequency: MaintenanceFrequency = MaintenanceFrequency.MONTHLY
    frequency_days: Optional[int] = Field(None, ge=1, le=3650, description="Days between PMs; custom only")
    estimated_duration_hours: float = Field(1.0, ge=0)
    priority: MaintenancePriority = MaintenancePriority.MEDIUM
    checklist: Optional[str] = None  # JSON array of strings (one item per line is still read)
    requires_shutdown: bool = False
    assigned_to: Optional[int] = None
    next_due_date: Optional[date] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return _strip_required(value)

    @model_validator(mode="after")
    def _validate(self) -> "ScheduleCreate":
        if (self.work_center_id is None) == (self.asset_id is None):
            raise ValueError(_TARGET_BOTH if self.work_center_id is not None else _TARGET_REQUIRED)
        if self.frequency == MaintenanceFrequency.CUSTOM and not self.frequency_days:
            raise ValueError(_CUSTOM_NEEDS_DAYS)
        return self


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    maintenance_type: Optional[MaintenanceType] = None
    frequency: Optional[MaintenanceFrequency] = None
    frequency_days: Optional[int] = Field(None, ge=1, le=3650)
    estimated_duration_hours: Optional[float] = Field(None, ge=0)
    priority: Optional[MaintenancePriority] = None
    checklist: Optional[str] = None
    requires_shutdown: Optional[bool] = None
    assigned_to: Optional[int] = None
    next_due_date: Optional[date] = None
    is_active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return _strip_required(value)

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "ScheduleUpdate":
        # NOT NULL / always-set columns: an explicit null would otherwise be written verbatim.
        _refuse_nulls(
            self,
            ("name", "maintenance_type", "frequency", "estimated_duration_hours", "priority", "requires_shutdown"),
        )
        _refuse_nulls(self, ("is_active",))
        return self


class WorkOrderCreate(BaseModel):
    schedule_id: Optional[int] = None
    # Exactly one target; both omitted with a schedule_id inherits the schedule's target.
    work_center_id: Optional[int] = None
    asset_id: Optional[int] = None
    maintenance_type: MaintenanceType = MaintenanceType.PREVENTIVE
    priority: MaintenancePriority = MaintenancePriority.MEDIUM
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    checklist_results: Optional[str] = None
    scheduled_date: Optional[date] = None
    due_date: Optional[date] = None
    requires_shutdown: bool = False
    assigned_to: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: Optional[str]) -> Optional[str]:
        return _strip_required(value)

    @model_validator(mode="after")
    def _one_target(self) -> "WorkOrderCreate":
        if self.work_center_id is not None and self.asset_id is not None:
            raise ValueError(_TARGET_BOTH)
        return self


class WorkOrderUpdate(BaseModel):
    priority: Optional[MaintenancePriority] = None
    status: Optional[MaintenanceStatus] = None
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    checklist_results: Optional[str] = None
    scheduled_date: Optional[date] = None
    due_date: Optional[date] = None
    requires_shutdown: Optional[bool] = None
    assigned_to: Optional[int] = None
    notes: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: Optional[str]) -> Optional[str]:
        return _strip_required(value)

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "WorkOrderUpdate":
        _refuse_nulls(self, ("priority", "status", "title", "requires_shutdown"))
        return self


class WorkOrderComplete(BaseModel):
    # Omitted keeps the checklist the work order already carries (the Maintenance page's Start PM
    # opens a PM work order with the schedule's checklist; the create endpoint stores whatever
    # checklist_results it is sent and never copies one); a value -- or an explicit null -- replaces it.
    checklist_results: Optional[str] = None
    findings: Optional[str] = None
    # Same rule (owner decision 2026-11-20: completion notes REPLACE): omitted keeps the notes the work
    # order already carries (a planner's, written by PUT), a value replaces them, an explicit null
    # clears them. A change is recorded, old and new, in the completion's audit row.
    notes: Optional[str] = Field(
        None,
        description=(
            "Replaces the work order's notes. Omit to keep the notes it already carries; send null (or blank "
            "text) to clear them. A change is recorded (old and new) in the completion's audit row."
        ),
    )

    @field_validator("notes")
    @classmethod
    def _blank_notes_clear(cls, value: Optional[str]) -> Optional[str]:
        # Blank text is "no notes", stored as NULL, not as an empty string.
        return value if value is None or value.strip() else None

    parts_used: Optional[str] = None
    labor_cost: float = 0
    parts_cost: float = 0
    # 0 (the default) on a work order that recorded downtime is filled from its linked
    # DowntimeEvent(s); a positive value is the technician's own figure and is kept.
    downtime_minutes: float = Field(0, ge=0)
    actual_duration_hours: Optional[float] = None
    # Required (409 without it) when the work order completes with any checklist item unticked;
    # recorded in the completion's audit row and log entry. Ignored when every item is ticked.
    incomplete_checklist_reason: Optional[str] = Field(
        None,
        max_length=2000,
        description=(
            "Why the work order is completed with checklist items unticked. Required when any item is "
            "unticked (409 without it); ignored when every item is ticked. Recorded in the audit trail. "
            "Only a person signed in to the app may give one: an API token is refused (403) while any "
            "item is unticked."
        ),
    )


# How far back a log entry may be dated (owner decision 2026-11-20), and the clock skew allowed forward.
LOG_MAX_BACKDATE_DAYS = 30
_LOG_FUTURE_SKEW = timedelta(minutes=5)
_LOG_MAX_DESCRIPTION = 4000
_LOG_MAX_COST = 10_000_000


class LogCreate(BaseModel):
    # With ``maintenance_wo_id`` both may be omitted: the entry takes the work order's target (an
    # explicit target that differs from the work order's is 409). Without one, exactly one is required.
    work_center_id: Optional[int] = None
    asset_id: Optional[int] = None
    maintenance_wo_id: Optional[int] = None
    event_type: MaintenanceLogEntryType = Field(
        ...,
        description=(
            "inspection, repair, part_replacement, observation or note. 'started' and 'completed' entries "
            "are written by the work order's Start and Complete verbs only."
        ),
    )
    description: str = Field(..., min_length=1, description=f"At most {_LOG_MAX_DESCRIPTION} characters once trimmed.")
    cost: float = Field(0, ge=0, le=_LOG_MAX_COST, allow_inf_nan=False)
    event_date: Optional[datetime] = Field(
        None,
        description=(
            f"When it happened (UTC); defaults to now. At most {LOG_MAX_BACKDATE_DAYS} days back, and not in "
            "the future."
        ),
    )

    @field_validator("description")
    @classmethod
    def _description(cls, value: Optional[str]) -> Optional[str]:
        text = _strip_required(value)
        if text is not None and len(text) > _LOG_MAX_DESCRIPTION:
            raise ValueError(f"must be at most {_LOG_MAX_DESCRIPTION} characters")
        return text

    @model_validator(mode="after")
    def _one_target(self) -> "LogCreate":
        if self.work_center_id is not None and self.asset_id is not None:
            raise ValueError(_TARGET_BOTH)
        if self.maintenance_wo_id is None and self.work_center_id is None and self.asset_id is None:
            raise ValueError(_TARGET_REQUIRED + " (or a maintenance_wo_id, whose target the entry takes)")
        return self


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


def _asset_in_company(db: Session, asset_id: int, company_id: int) -> MaintenanceAsset:
    """Resolve an equipment asset inside the caller's tenant, or a flat 404 (same posture as
    ``_work_center_in_company``: a foreign asset reads exactly like a missing one). A retired
    (inactive) asset still resolves -- its history stays readable and correctable."""
    asset = (
        db.query(MaintenanceAsset)
        .filter(MaintenanceAsset.id == asset_id, MaintenanceAsset.company_id == company_id)
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def _resolve_target(
    db: Session, company_id: int, *, work_center_id: Optional[int], asset_id: Optional[int]
) -> Tuple[Optional[WorkCenter], Optional[MaintenanceAsset]]:
    """The ONE target of a maintenance record (the schemas already refused both / neither)."""
    if work_center_id is not None:
        return _work_center_in_company(db, work_center_id, company_id), None
    if asset_id is not None:
        return None, _asset_in_company(db, asset_id, company_id)
    raise HTTPException(status_code=422, detail=_TARGET_REQUIRED)


def _schedule_or_404(db: Session, schedule_id: int, company_id: int, *, for_update: bool = False):
    query = db.query(MaintenanceSchedule).filter(
        MaintenanceSchedule.id == schedule_id, MaintenanceSchedule.company_id == company_id
    )
    if for_update:
        # ``populate_existing``: the locked read must be the row as it is now, not a copy an earlier
        # read left in the session (the snapshot and the schedule edit's ``old_values`` read it).
        query = query.with_for_update().populate_existing()
    schedule = query.first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


def _wo_or_404(db: Session, wo_id: int, company_id: int, *, for_update: bool = False) -> MaintenanceWorkOrder:
    """Tenant-scoped maintenance WO, or a flat 404. ``for_update`` row-locks it (a no-op on
    SQLite): the state verbs read-check-write ``status`` and now open / resolve downtime, so
    two simultaneous starts must not both pass the status check and open two events."""
    query = db.query(MaintenanceWorkOrder).filter(
        MaintenanceWorkOrder.id == wo_id, MaintenanceWorkOrder.company_id == company_id
    )
    if for_update:
        query = query.with_for_update().populate_existing()
    wo = query.first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    return wo


def _open_work_order_for_schedule(
    db: Session, company_id: int, schedule_id: int, *, judging_live_checklist: bool = False
) -> Optional[MaintenanceWorkOrder]:
    """The schedule's open maintenance work order (scheduled / overdue / in progress / on hold),
    tenant-scoped; the oldest if legacy data holds more than one (migration 132 refuses to install
    its unique index over such data, so none survives an upgrade). A completed or cancelled work
    order -- the module's terminal records; maintenance work orders have no soft delete -- never
    counts.

    ``judging_live_checklist`` narrows it to an open work order with NO checklist snapshot: one
    opened before migration 132, which completion still judges on the schedule's CURRENT checklist,
    so an edit to the schedule's steps changes what it must tick."""
    query = db.query(MaintenanceWorkOrder).filter(
        MaintenanceWorkOrder.company_id == company_id,
        MaintenanceWorkOrder.schedule_id == schedule_id,
        MaintenanceWorkOrder.status.in_(_OPEN_STATUSES),
    )
    if judging_live_checklist:
        query = query.filter(MaintenanceWorkOrder.schedule_checklist_snapshot.is_(None))
    return query.order_by(MaintenanceWorkOrder.id).first()


_WO_NUMBER_LOCK = "maintenance_work_order_number"
# ASCII digits only: ``str.isdigit`` also accepts e.g. a superscript two, which ``int`` then refuses.
_WO_NUMBER_SUFFIX = re.compile(r"[0-9]+")
# The unique constraints a new maintenance work order can hit (the partial index's name on PostgreSQL;
# SQLite names the columns instead).
_OPEN_SCHEDULE_CONFLICT_MARKERS = (OPEN_SCHEDULE_WORK_ORDER_INDEX, "maintenance_work_orders.schedule_id")
_WO_NUMBER_CONFLICT_MARKERS = ("maintenance_work_orders_wo_number", "maintenance_work_orders.wo_number")


def _generate_wo_number(db: Session, year: int) -> str:
    """Allocate the next MWO number for ``year`` -- the COMPANY's calendar year (the caller passes
    ``company_today(...).year``), so a work order opened on the evening of Dec 31 in Central time is
    numbered in the year it was opened, not the server's UTC year.

    This scan is deliberately NOT tenant-scoped: ``MaintenanceWorkOrder.wo_number``
    carries a GLOBAL unique constraint, so a per-tenant sequence would hand two
    companies the same number and the second insert would fail. It reads only the
    numbers, never any other tenant's row content. For the same reason the allocation is
    serialized install-wide (a transaction-scoped advisory lock on PostgreSQL, released at the
    commit): two simultaneous creates would otherwise read the same highest number and the second
    insert would fail the unique constraint. Lock order: the PM schedule's row lock (taken first
    by ``create_work_order``), then this lock, then the audit chain.

    The next number follows the highest NUMERIC suffix of the year; a legacy number whose suffix is
    not a number is skipped (it used to restart the sequence at 0001, colliding with the first).
    """
    acquire_generator_lock(db, _WO_NUMBER_LOCK)
    prefix = f"MWO-{year}-"
    highest = 0
    for (number,) in db.query(MaintenanceWorkOrder.wo_number).filter(MaintenanceWorkOrder.wo_number.like(f"{prefix}%")):
        suffix = number[len(prefix) :]
        if _WO_NUMBER_SUFFIX.fullmatch(suffix):
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:04d}"


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


def _target_fields(row: Any) -> Dict[str, Any]:
    """The record's target, both kinds, tenant-guarded on egress (see ``_same_tenant``)."""
    wc = _same_tenant(row, row.work_center)
    asset = _same_tenant(row, row.asset)
    if row.asset_id is not None:
        target_type, target_name, target_code = (
            "asset",
            asset.name if asset else None,
            asset.asset_tag if asset else None,
        )
    else:
        target_type, target_name, target_code = "work_center", wc.name if wc else None, wc.code if wc else None
    return {
        "work_center_id": row.work_center_id,
        "work_center_name": wc.name if wc else None,
        "asset_id": row.asset_id,
        "asset_tag": asset.asset_tag if asset else None,
        "asset_name": asset.name if asset else None,
        "target_type": target_type,
        "target_code": target_code,
        "target_name": target_name,
    }


def _work_orders_with_targets(db: Session):
    """A work-order query that loads each row's target with it: the serializers read
    ``work_center`` and ``asset`` on every row, one lazy load apiece otherwise."""
    return db.query(MaintenanceWorkOrder).options(
        joinedload(MaintenanceWorkOrder.work_center),
        joinedload(MaintenanceWorkOrder.asset),
        joinedload(MaintenanceWorkOrder.schedule),
    )


def _serialize_schedule(s: MaintenanceSchedule) -> dict:
    return {
        "id": s.id,
        **_target_fields(s),
        "name": s.name,
        "description": s.description,
        "maintenance_type": s.maintenance_type.value if hasattr(s.maintenance_type, "value") else s.maintenance_type,
        "frequency": s.frequency.value if hasattr(s.frequency, "value") else s.frequency,
        "frequency_days": s.frequency_days,
        "estimated_duration_hours": s.estimated_duration_hours,
        "priority": s.priority.value if hasattr(s.priority, "value") else s.priority,
        "checklist": s.checklist,
        # The checklist parsed tolerantly (a JSON array, else one item per line).
        "checklist_items": parse_checklist(s.checklist),
        "requires_shutdown": s.requires_shutdown,
        "assigned_to": s.assigned_to,
        "last_completed_date": s.last_completed_date.isoformat() if s.last_completed_date else None,
        "next_due_date": s.next_due_date.isoformat() if s.next_due_date else None,
        "is_active": s.is_active,
        "created_at": to_utc_iso(s.created_at),
        "updated_at": to_utc_iso(s.updated_at),
    }


def _open_work_order_conflict(schedule_name: str, open_wo: MaintenanceWorkOrder) -> HTTPException:
    """The 409 for a second open work order on one PM schedule (the check under the schedule's row
    lock and the database backstop answer the same)."""
    return HTTPException(
        status_code=409,
        detail=(
            f"PM schedule '{schedule_name}' already has an open maintenance work order, "
            f"{open_wo.wo_number} ({MaintenanceStatus(open_wo.status).value.replace('_', ' ')}). "
            "Complete or cancel it before opening another."
        ),
    )


def _schedule_checklist_items(wo: MaintenanceWorkOrder, schedule: Optional[MaintenanceSchedule] = None) -> List[str]:
    """The PM schedule steps this work order answers for (``[]`` without a schedule) -- the ONE
    choice every read and completion's judgment make, so the two cannot disagree.

    The snapshot taken when the work order was opened (migration 132), so a later schedule edit or
    PM import never changes what an open work order is judged on, nor what a finished one reports.
    A work order opened before migration 132 has no snapshot and reads the tenant-guarded
    schedule's CURRENT checklist, as every work order did before, until ``/complete`` (or a cancel)
    writes the steps it answered for (a known limitation for those completed or cancelled before
    then: never backfilled). ``schedule`` is that schedule already resolved -- row-locked and
    tenant-scoped -- by a caller that holds it; otherwise the relationship is read, tenant-guarded."""
    if wo.schedule_checklist_snapshot is not None:
        return parse_checklist(wo.schedule_checklist_snapshot)
    if schedule is None and wo.schedule_id is not None:
        schedule = _same_tenant(wo, wo.schedule)
    return parse_checklist(schedule.checklist) if schedule is not None else []


def _serialize_wo(wo: MaintenanceWorkOrder) -> dict:
    return {
        "id": wo.id,
        "schedule_id": wo.schedule_id,
        **_target_fields(wo),
        "wo_number": wo.wo_number,
        "maintenance_type": wo.maintenance_type.value if hasattr(wo.maintenance_type, "value") else wo.maintenance_type,
        "priority": wo.priority.value if hasattr(wo.priority, "value") else wo.priority,
        "status": wo.status.value if hasattr(wo.status, "value") else wo.status,
        "title": wo.title,
        "description": wo.description,
        "checklist_results": wo.checklist_results,
        # The PM schedule's checklist as snapshotted when the work order was opened (the live one for
        # a pre-132 work order), which completion also judges (a step the work order no longer carries
        # still needs a tick or a reason) -- exactly as completion reads it, so the Complete dialog
        # lists the same steps the server will.
        "schedule_checklist_items": _schedule_checklist_items(wo),
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


def _performer_names(db: Session, company_id: int, logs: List[MaintenanceLog]) -> Dict[int, str]:
    """``{user id: full name}`` for the people who performed ``logs``, in ONE read: users of this
    company, and platform principals (a platform admin or superuser working in it -- ``require_role``
    admits them to every maintenance verb). A user of another company (a legacy row) reads as no
    name, never as the foreign user's."""
    ids = {log.performed_by for log in logs if log.performed_by is not None}
    if not ids:
        return {}
    rows = (
        db.query(User.id, User.first_name, User.last_name)
        .filter(
            User.id.in_(ids),
            or_(
                User.company_id == company_id,
                User.is_superuser.is_(True),
                User.role == UserRole.PLATFORM_ADMIN,
            ),
        )
        .all()
    )
    return {user_id: f"{first} {last}".strip() for user_id, first, last in rows}


def _serialize_log(log: MaintenanceLog, names: Optional[Dict[int, str]] = None) -> dict:
    return {
        "id": log.id,
        **_target_fields(log),
        "maintenance_wo_id": log.maintenance_wo_id,
        "event_type": log.event_type,
        "description": log.description,
        "performed_by": log.performed_by,
        "performed_by_name": (names or {}).get(log.performed_by) if log.performed_by is not None else None,
        # A standing API token filed it (the token's bound user is ``performed_by``): the record says so.
        "filed_via_api_token": log.api_token_id is not None,
        "event_date": to_utc_iso(log.event_date),
        "cost": log.cost,
        "created_at": to_utc_iso(log.created_at),
    }


def _serialize_logs(db: Session, company_id: int, logs: List[MaintenanceLog]) -> List[dict]:
    names = _performer_names(db, company_id, logs)
    return [_serialize_log(log, names) for log in logs]


def _logs_with_targets(db: Session):
    """A log query that loads each row's target with it (the serializer reads both on every row)."""
    return db.query(MaintenanceLog).options(joinedload(MaintenanceLog.work_center), joinedload(MaintenanceLog.asset))


# ── Schedule Endpoints ────────────────────────────────────────────────────


@router.get("/schedules")
def list_schedules(
    work_center_id: Optional[int] = None,
    asset_id: Optional[int] = None,
    is_active: Optional[bool] = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List PM schedules with optional filters (``work_center_id`` or ``asset_id`` narrows to
    one target). Each row names its target: ``target_type`` is ``work_center`` or ``asset``."""
    query = (
        db.query(MaintenanceSchedule)
        .options(joinedload(MaintenanceSchedule.work_center), joinedload(MaintenanceSchedule.asset))
        .filter(MaintenanceSchedule.company_id == company_id)
    )
    if is_active is not None:
        query = query.filter(MaintenanceSchedule.is_active == is_active)
    if work_center_id:
        query = query.filter(MaintenanceSchedule.work_center_id == work_center_id)
    if asset_id:
        query = query.filter(MaintenanceSchedule.asset_id == asset_id)
    schedules = query.order_by(MaintenanceSchedule.next_due_date).all()
    return [_serialize_schedule(s) for s in schedules]


def _read_upload(file: UploadFile) -> bytes:
    # Bounded read: one byte past the parser's cap is enough for it to refuse the file.
    return file.file.read(MAX_IMPORT_FILE_BYTES + 1)


# Declared before ``/schedules/{schedule_id}`` so the literal paths are never parsed as an id.
@router.post(
    "/schedules/import/preview",
    response_model=PmImportResponse,
    summary="Preview a PM schedule import (dry run)",
)
def preview_pm_schedule_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Upload a PM schedule sheet (the ``maintenance-schedules`` template, one row per task, as
    .xlsx or .csv) and see what a commit would do. WRITES NOTHING.

    Each row targets a work center (``work_center_code``) OR an equipment asset
    (``asset_tag``; a new tag with ``asset_name`` is created on commit). Rows sharing a
    target, frequency and schedule name become one schedule whose checklist is their task
    list; an existing active schedule with that target and name is updated. "Yearly" reads as
    annual; "as necessary" rows are skipped with a warning. Returns per-row outcomes, the
    schedules to create / update (with field changes), the assets to create, and
    ``source_hash``, which the commit must echo back."""
    content = _read_upload(file)
    try:
        return preview_pm_import(
            db,
            company_id=company_id,
            filename=file.filename,
            content=content,
            api_token=is_api_token_credential(current_user),
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/schedules/import/commit",
    response_model=PmImportResponse,
    summary="Commit a PM schedule import",
)
def commit_pm_schedule_import(
    file: UploadFile = File(...),
    expected_source_hash: str = Form(..., min_length=64, max_length=64),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Apply a previewed PM schedule sheet.

    ``expected_source_hash`` is the preview's ``source_hash`` (409 if the file differs).
    ALL-OR-NOTHING: any row error refuses 409 with the row errors and writes nothing. Each
    created asset and each created / updated schedule writes a required audit row, plus one
    summary row; a re-run of the same sheet changes nothing and writes no audit row.

    A changed checklist binds each schedule's NEXT work order: an open one keeps the checklist it was
    opened with (a row warning names it). An open work order opened before checklist snapshots
    existed is judged on the schedule's current checklist, so the warning says the import changes
    its steps, and a standing API token whose import takes steps away from such a schedule gets a
    row error (so the commit is 409)."""
    content = _read_upload(file)
    try:
        with required_audit(db, "The audit record for the PM schedule import could not be saved; nothing was changed."):
            result = commit_pm_import(
                db,
                company_id=company_id,
                filename=file.filename,
                content=content,
                expected_source_hash=expected_source_hash,
                audit=audit,
                api_token=is_api_token_credential(current_user),
            )
            db.commit()
    except ImportFileError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="The maintenance data changed while importing (e.g. an asset tag was just created); "
            "nothing was imported. Preview again, then commit.",
        ) from exc
    except HTTPException:
        db.rollback()
        raise
    return result


@router.get("/schedules/{schedule_id}")
def get_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get schedule detail"""
    return _serialize_schedule(_schedule_or_404(db, schedule_id, company_id))


@router.post("/schedules")
def create_schedule(
    data: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new PM schedule on a work center OR an equipment asset (exactly one).

    ``frequency`` is one of daily, weekly, biweekly, monthly, quarterly, semi_annual, annual,
    custom (422 otherwise); ``custom`` requires ``frequency_days``. A blank ``next_due_date`` is
    the company's calendar day."""
    _resolve_target(db, company_id, work_center_id=data.work_center_id, asset_id=data.asset_id)

    schedule = MaintenanceSchedule(
        work_center_id=data.work_center_id,
        asset_id=data.asset_id,
        name=data.name,
        description=data.description,
        maintenance_type=data.maintenance_type,
        frequency=data.frequency,
        frequency_days=data.frequency_days,
        estimated_duration_hours=data.estimated_duration_hours,
        priority=data.priority,
        checklist=data.checklist,
        requires_shutdown=data.requires_shutdown,
        assigned_to=data.assigned_to,
        next_due_date=data.next_due_date or company_today(db, company_id),
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
    return _serialize_schedule(schedule)


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    data: ScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a PM schedule (enum fields 422 on an unknown value; a schedule left on
    ``custom`` must carry ``frequency_days``). The target is not changed here. A changed ``checklist``
    binds the NEXT work order: an open one keeps the steps it was opened with. The exception is an
    open work order opened before checklist snapshots existed (migration 132), which is still judged
    on the schedule's current steps -- a standing API token that takes items off ``checklist`` while
    the schedule has such a work order is **403**."""
    # Row-locked (a no-op on SQLite), like completion's roll-forward and Start PM: the audit row's
    # ``old_values`` then cannot predate a concurrent completion that moved the due dates.
    schedule = _schedule_or_404(db, schedule_id, company_id, for_update=True)

    update_data = data.model_dump(exclude_unset=True)
    if (
        "checklist" in update_data
        and is_api_token_credential(current_user)
        and removed_items(parse_checklist(schedule.checklist), parse_checklist(update_data["checklist"]))
        and _open_work_order_for_schedule(db, company_id, schedule.id, judging_live_checklist=True) is not None
    ):
        # That work order is judged on the schedule's live steps: emptying them under it is a skip.
        raise HTTPException(status_code=403, detail=API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL)
    frequency = update_data.get("frequency", schedule.frequency)
    frequency_days = update_data["frequency_days"] if "frequency_days" in update_data else schedule.frequency_days
    if frequency == MaintenanceFrequency.CUSTOM and not frequency_days:
        raise HTTPException(status_code=422, detail=_CUSTOM_NEEDS_DAYS)

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
    schedule = _schedule_or_404(db, schedule_id, company_id)
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
    asset_id: Optional[int] = None,
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
    query = _work_orders_with_targets(db).filter(MaintenanceWorkOrder.company_id == company_id)
    if status:
        query = query.filter(MaintenanceWorkOrder.status == status)
    if work_center_id:
        query = query.filter(MaintenanceWorkOrder.work_center_id == work_center_id)
    if asset_id:
        query = query.filter(MaintenanceWorkOrder.asset_id == asset_id)
    if maintenance_type:
        query = query.filter(MaintenanceWorkOrder.maintenance_type == maintenance_type)
    if start_date:
        query = query.filter(MaintenanceWorkOrder.scheduled_date >= start_date)
    if end_date:
        query = query.filter(MaintenanceWorkOrder.scheduled_date <= end_date)

    # The company's day (the dashboard's), not the server's UTC day.
    today = company_today(db, company_id)
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
    today = company_today(db, company_id)
    wos = (
        _work_orders_with_targets(db)
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
    return _serialize_wo(_wo_or_404(db, wo_id, company_id))


@router.get("/work-orders/{wo_id}/logs")
def list_work_order_logs(
    wo_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """The maintenance log entries filed against one maintenance work order, newest first
    (``event_date``): its ``started`` / ``completed`` entries and every entry filed with its
    ``maintenance_wo_id`` (``POST /maintenance/log``). Each carries ``performed_by_name``. A work order
    of another company is 404."""
    wo = _wo_or_404(db, wo_id, company_id)
    logs = (
        _logs_with_targets(db)
        .filter(MaintenanceLog.company_id == company_id, MaintenanceLog.maintenance_wo_id == wo.id)
        .order_by(MaintenanceLog.event_date.desc().nullslast(), MaintenanceLog.id.desc())
        .limit(limit)
        .all()
    )
    return {"maintenance_wo_id": wo.id, "wo_number": wo.wo_number, "logs": _serialize_logs(db, company_id, logs)}


@router.post("/work-orders")
def create_work_order(
    data: WorkOrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new maintenance work order on a work center OR an equipment asset. A PM schedule
    takes one open work order at a time: a second with the same ``schedule_id`` is 409.

    Give exactly one of ``work_center_id`` / ``asset_id``, or neither together with a
    ``schedule_id`` to inherit that schedule's target (an explicit target that differs from the
    schedule's is **409**: completing the work order advances that schedule). A schedule takes
    ONE open work order at a time: while one is scheduled, overdue, in progress or on hold, a
    second with the same ``schedule_id`` is **409** naming it (a completed or cancelled one does
    not count). ``requires_shutdown`` on a WORK-CENTER work order means starting it records
    machine downtime (an asset never does). Defaulted dates are the company's day, and the
    ``wo_number`` year is the company's calendar year.

    A PM work order snapshots its schedule's checklist as it stands now: completion judges those
    steps (and every response serves them as ``schedule_checklist_items``), so a later edit or import
    of the schedule's checklist binds the next work order, not this one."""
    # A foreign schedule_id would otherwise be accepted verbatim, and
    # complete_work_order writes last_completed_date / next_due_date straight
    # onto whatever schedule it points at -- a cross-tenant write laundered
    # through a legitimately-owned work order.
    schedule: Optional[MaintenanceSchedule] = None
    if data.schedule_id is not None:
        # Row-locked (a no-op on SQLite) until the commit, so two simultaneous Start PMs on one
        # schedule serialize here and the second sees the first's work order below.
        schedule = _schedule_or_404(db, data.schedule_id, company_id, for_update=True)

    work_center_id, asset_id = data.work_center_id, data.asset_id
    if schedule is not None:
        if work_center_id is None and asset_id is None:
            work_center_id, asset_id = schedule.work_center_id, schedule.asset_id
        elif (work_center_id, asset_id) != (schedule.work_center_id, schedule.asset_id):
            # Completing the work order advances THIS schedule's due date, so work done on another
            # machine would be recorded as this machine's PM.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This work order's target must be its schedule's ('{schedule.name}'). Leave the work center "
                    "and asset empty to use the schedule's, or open the work order without the schedule."
                ),
            )
    _resolve_target(db, company_id, work_center_id=work_center_id, asset_id=asset_id)

    if schedule is not None:
        open_wo = _open_work_order_for_schedule(db, company_id, schedule.id)
        if open_wo is not None:
            # Two open work orders on one PM: completing either advances the schedule, so the
            # other would be performed (or reported overdue) against a due date it no longer has.
            raise _open_work_order_conflict(schedule.name, open_wo)

    today = company_today(db, company_id)
    wo_number = _generate_wo_number(db, today.year)

    wo = MaintenanceWorkOrder(
        schedule_id=data.schedule_id,
        work_center_id=work_center_id,
        asset_id=asset_id,
        wo_number=wo_number,
        maintenance_type=data.maintenance_type,
        priority=data.priority,
        status=MaintenanceStatus.SCHEDULED,
        title=data.title,
        description=data.description,
        checklist_results=data.checklist_results,
        # Read under the schedule's row lock, so it is the checklist no concurrent edit or import
        # changed between this read and the commit.
        schedule_checklist_snapshot=json.dumps(parse_checklist(schedule.checklist)) if schedule is not None else None,
        scheduled_date=data.scheduled_date or today,
        due_date=data.due_date or data.scheduled_date or today,
        requires_shutdown=data.requires_shutdown,
        assigned_to=data.assigned_to,
    )
    wo.company_id = company_id
    db.add(wo)
    try:
        db.flush()
    except IntegrityError as exc:
        # The database backstop (``uq_maintenance_work_orders_open_schedule``) for the open-work-order
        # rule checked above: on PostgreSQL the schedule's row lock makes that check authoritative,
        # so this fires only for a writer that skipped it. Same 409 as the check. A ``wo_number``
        # collision (the advisory lock makes one impossible on PostgreSQL) is a retryable 409; any
        # other violation -- an unknown ``assigned_to`` -- is not a conflict and is re-raised.
        message = str(getattr(exc, "orig", exc))
        open_schedule_conflict = any(marker in message for marker in _OPEN_SCHEDULE_CONFLICT_MARKERS)
        if not open_schedule_conflict and not any(marker in message for marker in _WO_NUMBER_CONFLICT_MARKERS):
            raise
        db.rollback()
        open_wo = (
            _open_work_order_for_schedule(db, company_id, data.schedule_id)
            if open_schedule_conflict and data.schedule_id is not None
            else None
        )
        if open_wo is not None:
            name = open_wo.schedule.name if open_wo.schedule is not None else str(data.schedule_id)
            raise _open_work_order_conflict(name, open_wo) from exc
        raise HTTPException(
            status_code=409,
            detail="The maintenance work order could not be saved because another one was saved at the same time. "
            "Nothing was changed; try again.",
        ) from exc
    audit.log_create("maintenance_work_order", wo.id, wo.wo_number, new_values=wo)
    db.commit()
    db.refresh(wo)
    return _serialize_wo(wo)


def _same_as_record(field: str, current: Any, sent: Any) -> bool:
    """Whether a ``PUT`` value repeats what the work order already holds. Enum members compare by
    value; ``requires_shutdown`` by truth, as its started-WO rule below reads it (a legacy NULL is
    "no shutdown"); ``title`` trimmed, since the request's is (a legacy title stored with stray
    spaces can still be echoed). Anything else must be equal as stored -- text is not normalized."""
    if field == "requires_shutdown":
        return bool(current) == bool(sent)
    if field == "title" and isinstance(current, str):
        current = current.strip()
    current = current.value if isinstance(current, Enum) else current
    sent = sent.value if isinstance(sent, Enum) else sent
    return current == sent


def _assert_wo_edit_allowed(wo: MaintenanceWorkOrder, update_data: Dict[str, Any]) -> None:
    """Refuse (409), before the first ``setattr``, the edits only a verb may make.

    ``PUT`` used to ``setattr`` any status, so ``in_progress`` started a work order with no
    ``started_at``, no log and no downtime event; ``completed`` closed one without advancing its
    PM schedule or recording its downtime minutes; and a started work order could be returned
    to ``scheduled`` with its downtime event still open, or have ``requires_shutdown`` flipped
    so the event it opened was never closed (or one it should have opened never was).

    A COMPLETED or CANCELLED work order is a final record: no status change leaves it. A reopened
    one carried its ``started_at`` (and ``completed_at``) into on hold, where it looked like a
    work order still holding the machine down and took over another work order's downtime event
    -- which then never closed -- and a second ``/complete`` overwrote the maintenance record and
    advanced the PM schedule again. A new work order is the correction.

    Nor does any other field change on one. Its checklist, notes, findings, dates and assignee are
    the record of what was done (or why it was not), and a ``PUT`` could re-tick a completed PM's
    checklist after the fact -- the very steps ``/complete`` demanded a written reason to leave
    unticked. ``notes`` included: ``/complete`` writes them as part of the completion, and a later
    note belongs in a maintenance log entry on the work order (``POST /maintenance/log``), which is
    dated and signed instead of rewriting the record. A value identical to the record's is an echo,
    not a change, and is accepted."""
    current = MaintenanceStatus(wo.status)
    # Started: in progress, or on hold after a start (a work order can also be held before one).
    started = current == MaintenanceStatus.IN_PROGRESS or (
        current == MaintenanceStatus.ON_HOLD and wo.started_at is not None
    )
    target = update_data.get("status")
    if target is not None and MaintenanceStatus(target) != current:
        target = MaintenanceStatus(target)
        if current in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Maintenance work order {wo.wo_number} is {current.value}, a final record; it cannot be "
                    f"moved to {target.value}. Open a new maintenance work order instead."
                ),
            )
        if target == MaintenanceStatus.IN_PROGRESS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Start maintenance work order {wo.wo_number} with Start (POST /maintenance/work-orders/"
                    f"{wo.id}/start): it records the start and any machine downtime."
                ),
            )
        if target == MaintenanceStatus.COMPLETED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Complete maintenance work order {wo.wo_number} with Complete (POST /maintenance/work-orders/"
                    f"{wo.id}/complete): it records the findings, costs and downtime and advances its PM schedule."
                ),
            )
        if started and target in (MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Maintenance work order {wo.wo_number} has been started; it can be put on hold, completed "
                    f"or cancelled, not returned to {target.value}."
                ),
            )
    if current in _TERMINAL_STATUSES:
        changed = [
            field for field, value in update_data.items() if not _same_as_record(field, getattr(wo, field), value)
        ]
        if changed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Maintenance work order {wo.wo_number} is {current.value}, a final record; its "
                    f"{', '.join(changed)} can no longer change. Add a later note as a maintenance log entry on "
                    "the work order, or open a new maintenance work order for further work."
                ),
            )
    if (
        "requires_shutdown" in update_data
        and bool(update_data["requires_shutdown"]) != bool(wo.requires_shutdown)
        and started
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Maintenance work order {wo.wo_number} has been started, so whether it shuts the machine down "
                "can no longer change: its downtime is already being recorded (or not). Cancel it and open a new "
                "work order instead."
            ),
        )


@router.put("/work-orders/{wo_id}")
def update_work_order(
    wo_id: int,
    data: WorkOrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an open maintenance work order: its details, and ``status`` among scheduled,
    overdue, on hold and cancelled. A completed or cancelled work order is a final record: changing
    any field of it is 409 (add a later note as a maintenance log entry instead).

    The verbs own the rest of the lifecycle, because they own its records: **409** for
    ``status: in_progress`` (use ``POST .../start`` -- it stamps the start and records the
    machine downtime), for ``status: completed`` (use ``POST .../complete`` -- findings, costs,
    downtime minutes and the PM schedule's next due date), for returning a STARTED work order
    (in progress / on hold) to scheduled or overdue, and for changing ``requires_shutdown`` on
    a started work order (its downtime is already being recorded, or not), and for ANY change to
    a completed or cancelled work order -- its status, checklist, notes, title, description, dates,
    priority, assignee or ``requires_shutdown`` (a final record: a later note is a maintenance log
    entry, ``POST /maintenance/log``, and further work a new work order). Re-sending the current
    value is accepted; on a completed or cancelled work order such a request writes nothing. A
    standing API token that takes items off ``checklist_results`` (dropping, renaming or clearing
    them) is **403**; ticking or adding is allowed. Moving to ``cancelled`` settles any downtime
    the work order holds: resolved, handed to another shutdown work order still holding the machine down, or
    -- for an operator's event the work order adopted -- handed back to the machine, still
    open. ``on_hold`` keeps the machine down (the downtime runs on until the work order ends)."""
    wo = _wo_or_404(db, wo_id, company_id, for_update=True)

    update_data = data.model_dump(exclude_unset=True)
    _assert_wo_edit_allowed(wo, update_data)
    if wo.status in _TERMINAL_STATUSES:
        # A final record: everything the guard let through repeats it, so there is nothing to
        # write -- not even the legacy NULL ``requires_shutdown`` an echoed ``false`` would replace.
        return _serialize_wo(wo)
    if (
        "checklist_results" in update_data
        and is_api_token_credential(current_user)
        and removed_checklist_items(wo.checklist_results, update_data["checklist_results"])
    ):
        # Else a token could PUT the unticked steps away and then complete without a person's reason.
        raise HTTPException(status_code=403, detail=API_TOKEN_CHECKLIST_REMOVAL)
    # Cancelling a work order opened before migration 132 (no checklist snapshot) freezes the schedule
    # steps it answered for, so the final record stops following later schedule edits -- as completing
    # one does. The schedule row is locked after the work order (completion's order) and before any
    # audit row (the downtime settlement below writes some).
    freeze_schedule: Optional[MaintenanceSchedule] = None
    if (
        update_data.get("status") == MaintenanceStatus.CANCELLED
        and wo.schedule_checklist_snapshot is None
        and wo.schedule_id is not None
    ):
        freeze_schedule = (
            db.query(MaintenanceSchedule)
            .filter(MaintenanceSchedule.id == wo.schedule_id, MaintenanceSchedule.company_id == company_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
    old_values = {c.key: getattr(wo, c.key) for c in wo.__table__.columns}

    for field, value in update_data.items():
        setattr(wo, field, value)
    if freeze_schedule is not None:
        # Recorded by the ``log_update`` below (NULL -> the steps).
        wo.schedule_checklist_snapshot = json.dumps(_schedule_checklist_items(wo, freeze_schedule))

    # Open until now (a final record returned above): a move to cancelled settles its downtime.
    if wo.status in _TERMINAL_STATUSES:
        downtime_service.end_maintenance_downtime(
            db,
            wo,
            company_id=company_id,
            actor_id=current_user.id,
            audit=audit,
            ended_at=datetime.utcnow(),
            resolution=f"Maintenance work order {wo.wo_number} set to {wo.status.value}",
            cancelled=wo.status == MaintenanceStatus.CANCELLED,
        )

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
    """Start a maintenance work order.

    A WORK-CENTER work order with ``requires_shutdown`` also records the stoppage: it opens a
    downtime event on that machine (preventive / predictive -> planned maintenance, outside
    OEE loss; corrective / emergency -> mechanical, unplanned), or adopts an open event an
    operator already logged on it rather than doubling the minutes. ``/complete`` and a
    cancel resolve it. An equipment-asset work order never records downtime."""
    wo = _wo_or_404(db, wo_id, company_id, for_update=True)
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
        asset_id=wo.asset_id,
        maintenance_wo_id=wo.id,
        event_type=LOG_EVENT_STARTED,
        description=f"Maintenance work order {wo.wo_number} started",
        performed_by=current_user.id,
        event_date=datetime.utcnow(),
    )
    db.add(log)
    db.flush()
    downtime = downtime_service.open_or_link_maintenance_downtime(
        db, wo, company_id=company_id, actor_id=current_user.id, audit=audit, started_at=wo.started_at
    )
    audit.log_status_change(
        "maintenance_work_order",
        wo.id,
        wo.wo_number,
        old_status,
        MaintenanceStatus.IN_PROGRESS.value,
        extra_data=downtime.audit_extra() if downtime is not None else None,
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
    """Complete a maintenance work order. Completing with any checklist item unticked needs
    ``incomplete_checklist_reason`` (409 without it), which the audit trail records, and a person
    signed in to the app (an API token is refused 403); a PM work order rolls its schedule forward
    from the company's calendar day.

    Resolves the downtime event the work order holds open (see ``/start``) at completion,
    and -- when ``downtime_minutes`` is left at 0 -- records the linked events' minutes.

    A work order carrying a ``schedule_id`` advances that PM schedule: ``last_completed_date``
    becomes today -- the COMPANY's calendar day (Central by default), not the server's UTC day,
    so an evening completion is not stamped tomorrow -- and ``next_due_date`` today plus the
    schedule's interval. The response then carries the schedule's new ``schedule_next_due_date``
    (``null`` when nothing was advanced).

    ``checklist_results`` omitted keeps the checklist the work order already carries. Completing
    with ANY checklist item unticked needs ``incomplete_checklist_reason`` (**409** without one,
    nothing saved); an item the work order carried that ``checklist_results`` leaves out counts as
    unticked, and so does an item of a PM work order's SCHEDULE checklist that is not ticked. A standing API token may complete only with every item ticked: with any unticked it is
    **403**, whatever reason it sends (owner decision 2026-11-17). The reason and the unticked items are recorded in the completion's audit row (then
    required evidence: **503**, nothing saved, if it cannot be written) and its log entry."""
    wo = _wo_or_404(db, wo_id, company_id, for_update=True)
    if wo.status not in (MaintenanceStatus.IN_PROGRESS, MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE):
        raise HTTPException(status_code=400, detail=f"Cannot complete work order in status '{wo.status.value}'")

    # The PM schedule this completion advances, row-locked now (a no-op on SQLite): row locks
    # first, the global audit-chain lock after -- the downtime resolution below writes audit rows,
    # and a schedule edit takes the schedule's row lock before ITS audit row, so locking the
    # schedule after them could deadlock against it. The lock also keeps the roll-forward's audit
    # ``old_values`` exact. Scoped: pre-fix rows may still carry a foreign schedule_id
    # (create_work_order accepted one once), and this is the write that would advance another
    # tenant's PM due date.
    schedule: Optional[MaintenanceSchedule] = None
    if wo.schedule_id:
        schedule = (
            db.query(MaintenanceSchedule)
            .filter(MaintenanceSchedule.id == wo.schedule_id, MaintenanceSchedule.company_id == company_id)
            .with_for_update()
            .populate_existing()
            .first()
        )

    # Judged before the first write. The checklist is the record of what the PM actually covered,
    # so closing with part of it undone is allowed only with a reason someone wrote down.
    completed_checklist = (
        data.checklist_results if "checklist_results" in data.model_fields_set else wo.checklist_results
    )
    # A PM work order also answers for its schedule's checklist: a step dropped from the work order
    # (at Start PM, or by a PUT) is still a step of the PM. The steps as snapshotted when the work
    # order was opened (a work order opened before migration 132 reads the locked schedule's current
    # ones, as it always did). With no in-tenant schedule, a legacy row answers for none.
    schedule_steps = (
        _schedule_checklist_items(wo, schedule)
        if wo.schedule_checklist_snapshot is not None or schedule is not None
        else []
    )
    unticked, checklist_total = unticked_checklist_items(wo.checklist_results, completed_checklist, schedule_steps)
    checklist_reason = (data.incomplete_checklist_reason or "").strip()
    if unticked and is_api_token_credential(current_user):
        # Whatever reason it sends: only a person records why PM steps were skipped.
        raise HTTPException(status_code=403, detail=API_TOKEN_INCOMPLETE_CHECKLIST)
    if unticked and not checklist_reason:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(unticked)} of {checklist_total} checklist items on maintenance work order {wo.wo_number} "
                f"are not ticked ({describe_items(unticked)}). Tick them, or give a reason for completing "
                "without them."
            ),
        )

    now = datetime.utcnow()
    # The company's calendar day at the same instant as ``completed_at``.
    today = local_today(company_zone(db, company_id), now)
    old_status = wo.status.value if hasattr(wo.status, "value") else wo.status
    old_notes = wo.notes
    wo.status = MaintenanceStatus.COMPLETED
    wo.completed_at = now
    wo.completed_by = current_user.id
    if "checklist_results" in data.model_fields_set:
        wo.checklist_results = data.checklist_results
    wo.findings = data.findings
    snapshot_written_at_completion = wo.schedule_checklist_snapshot is None and schedule is not None
    if snapshot_written_at_completion:
        # A work order opened before migration 132: freeze the schedule steps this completion judged,
        # so the finished record stops following later schedule edits from here on.
        wo.schedule_checklist_snapshot = json.dumps(schedule_steps)
    if "notes" in data.model_fields_set:
        # Replace (or, with an explicit null, clear); omitted keeps the planner's notes. The text it
        # replaces is kept in the completion's audit row below.
        wo.notes = data.notes
    notes_changed = wo.notes != old_notes
    wo.parts_used = data.parts_used
    wo.labor_cost = data.labor_cost
    wo.parts_cost = data.parts_cost
    wo.total_cost = data.labor_cost + data.parts_cost
    wo.downtime_minutes = data.downtime_minutes

    resolved = downtime_service.end_maintenance_downtime(
        db,
        wo,
        company_id=company_id,
        actor_id=current_user.id,
        audit=audit,
        ended_at=now,
        resolution=f"Maintenance work order {wo.wo_number} completed",
    )
    if not data.downtime_minutes:
        linked_minutes = downtime_service.linked_downtime_minutes(db, company_id, wo)
        if linked_minutes is not None:
            wo.downtime_minutes = linked_minutes

    # Calculate duration
    if data.actual_duration_hours is not None:
        wo.actual_duration_hours = data.actual_duration_hours
    elif wo.started_at:
        duration = (now - wo.started_at).total_seconds() / 3600.0
        wo.actual_duration_hours = round(duration, 2)

    # Advance the PM schedule locked above.
    advanced: Optional[MaintenanceSchedule] = None
    advanced_due: Optional[str] = None
    schedule_old_values: Dict[str, Any] = {}
    if schedule is not None:
        schedule_old_values = {
            "last_completed_date": schedule.last_completed_date,
            "next_due_date": schedule.next_due_date,
        }
        schedule.last_completed_date = today
        schedule.next_due_date = next_due_date(schedule.frequency, schedule.frequency_days, today)
        advanced = schedule
        # Computed once, before the commit: the response must report the date THIS completion
        # wrote, not whatever a re-read after commit finds.
        advanced_due = schedule.next_due_date.isoformat()

    # Log the event. Same NOT NULL omission as start_work_order: the insert
    # raised IntegrityError and the endpoint 500'd after the completion had
    # already been committed. One commit now covers both.
    log_description = f"Maintenance work order {wo.wo_number} completed. Cost: ${wo.total_cost:.2f}"
    if unticked:
        log_description += (
            f" Checklist: {len(unticked)} of {checklist_total} items not ticked "
            f"({'; '.join(unticked)}). Reason: {checklist_reason}"
        )
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=wo.work_center_id,
        asset_id=wo.asset_id,
        maintenance_wo_id=wo.id,
        event_type=LOG_EVENT_COMPLETED,
        description=log_description,
        performed_by=current_user.id,
        event_date=now,
        cost=wo.total_cost,
    )
    db.add(log)
    db.flush()
    status_old_values: Dict[str, Any] = {"status": old_status}
    status_new_values: Dict[str, Any] = {"status": MaintenanceStatus.COMPLETED.value}
    if notes_changed:
        # The notes this completion replaced survive only here.
        status_old_values["notes"] = old_notes
        status_new_values["notes"] = wo.notes
    completion_extra: Dict[str, Any] = {
        "total_cost": wo.total_cost,
        "downtime_minutes": wo.downtime_minutes,
        "actual_duration_hours": wo.actual_duration_hours,
        "schedule_id": wo.schedule_id,
        "schedule_next_due_date": advanced_due,
        "resolved_downtime_event_ids": [event.id for event in resolved],
    }
    if snapshot_written_at_completion:
        completion_extra["schedule_checklist_snapshot"] = schedule_steps
    status_description = (
        f"Changed maintenance_work_order status: {wo.wo_number} from '{old_status}' to "
        f"'{MaintenanceStatus.COMPLETED.value}'"
    )
    if unticked:
        completion_extra.update(
            {
                "checklist_item_count": checklist_total,
                "checklist_unticked_items": unticked,
                "incomplete_checklist_reason": checklist_reason,
            }
        )
        status_description += f" with {len(unticked)} of {checklist_total} checklist items not ticked"
    # Required evidence -- if it cannot be written, nothing is saved (503) -- when it is the only record
    # of something: WHY part of the checklist was left undone, or notes this completion replaced or
    # cleared (their text survives nowhere else).
    replaced_notes = notes_changed and bool(old_notes)
    if unticked or replaced_notes:
        with required_audit(
            db,
            f"The audit record for completing maintenance work order {wo.wo_number} could not be saved; "
            "nothing was changed. Retry.",
        ):
            audit.log_required(
                audit.ACTIONS["STATUS_CHANGE"],
                "maintenance_work_order",
                resource_id=wo.id,
                resource_identifier=wo.wo_number,
                description=status_description,
                old_values=status_old_values,
                new_values=status_new_values,
                extra_data=completion_extra,
            )
    else:
        # ``log_status_change``'s row, plus the notes when the completion added them.
        audit.log(
            action=audit.ACTIONS["STATUS_CHANGE"],
            resource_type="maintenance_work_order",
            resource_id=wo.id,
            resource_identifier=wo.wo_number,
            description=status_description,
            old_values=status_old_values,
            new_values=status_new_values,
            extra_data=completion_extra,
        )
    if advanced is not None:
        # The roll-forward is a change to the SCHEDULE, so it gets the schedule's own row too:
        # its history then shows every due-date move, by completion or by an edit.
        audit.log_update(
            resource_type="maintenance_schedule",
            resource_id=advanced.id,
            resource_identifier=advanced.name,
            old_values=schedule_old_values,
            new_values={
                "last_completed_date": advanced.last_completed_date,
                "next_due_date": advanced.next_due_date,
            },
            description=f"PM schedule {advanced.name} advanced by completing maintenance work order {wo.wo_number}",
            extra_data={"maintenance_work_order_id": wo.id},
        )
    db.commit()
    db.refresh(wo)

    body = _serialize_wo(wo)
    body["schedule_next_due_date"] = advanced_due
    return body


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
        _work_orders_with_targets(db)
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
    """Dashboard metrics for maintenance management.

    "Today" is the COMPANY's calendar day (Central by default) -- the day completion stamps on a
    PM schedule and the QMS evidence rule reads -- never the server's UTC day.

    ``overdue_count`` / ``due_this_week`` count maintenance WORK ORDERS (scheduled or overdue,
    by ``due_date``) -- unchanged. A PM schedule that has come due with no work order being
    worked is invisible to them, so ``schedules_overdue`` / ``schedules_due_this_week`` count
    those: ACTIVE schedules by ``next_due_date`` (before today; today through today + 7) with no
    work order that is scheduled, overdue or in progress carrying their ``schedule_id``. A schedule
    with such a work order is represented by it (by the work order's due date, or in
    ``in_progress``), so the two pairs never count the same PM twice. A PM whose work order is ON
    HOLD is not done, so its schedule still counts here by the schedule's own due date (an on-hold
    work order is on no work-order count). ``completed_this_month`` counts completions since the
    company-local first of the month."""
    today = company_today(db, company_id)
    month_start = today.replace(day=1)
    # ``completed_at`` is a UTC instant: the month starts at the company's local midnight.
    month_start_at, _ = company_window(db, company_id, month_start, today)

    # The four due / overdue counts come from the same rule the daily due / overdue alerts read
    # (``app.services.maintenance_due_service``), so a tile and an alert never disagree.
    due = maintenance_due(db, company_id, today, window_days=DUE_WINDOW_DAYS)
    schedules_overdue = len(due.schedules_overdue)
    schedules_due_this_week = len(due.schedules_due)
    overdue_count = len(due.work_orders_overdue)
    due_this_week = len(due.work_orders_due)

    # Completed this month
    completed_this_month = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
            MaintenanceWorkOrder.completed_at >= month_start_at,
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
            MaintenanceWorkOrder.completed_at >= month_start_at,
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
        "schedules_overdue": schedules_overdue,
        "schedules_due_this_week": schedules_due_this_week,
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
    """A work center's maintenance history: its log entries, newest first (``event_date``), each with
    ``performed_by_name``."""
    wc = _work_center_in_company(db, work_center_id, company_id)

    logs = (
        _logs_with_targets(db)
        .filter(
            MaintenanceLog.company_id == company_id,
            MaintenanceLog.work_center_id == work_center_id,
        )
        .order_by(MaintenanceLog.event_date.desc().nullslast(), MaintenanceLog.id.desc())
        .limit(limit)
        .all()
    )

    return {
        "work_center_id": wc.id,
        "work_center_name": wc.name,
        "logs": _serialize_logs(db, company_id, logs),
    }


@router.get("/assets/{asset_id}/history")
def get_maintenance_asset_history(
    asset_id: int,
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Full maintenance history (log entries) for an equipment asset, newest first, each with
    ``performed_by_name``."""
    asset = _asset_in_company(db, asset_id, company_id)
    logs = (
        _logs_with_targets(db)
        .filter(MaintenanceLog.company_id == company_id, MaintenanceLog.asset_id == asset_id)
        .order_by(MaintenanceLog.event_date.desc().nullslast(), MaintenanceLog.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "asset_id": asset.id,
        "asset_tag": asset.asset_tag,
        "asset_name": asset.name,
        "logs": _serialize_logs(db, company_id, logs),
    }


# ── Equipment assets ──────────────────────────────────────────────────────
#
# Non-production equipment that is maintained but never scheduled, dispatched, run on the
# kiosk or counted in OEE (owner decision 2026-11-13). Reads are open to every signed-in
# role; writes are the planning roles, like schedules. No physical delete: retiring is
# ``is_active = false`` (DELETE), and its schedules / work orders / history stay.

_ASSET_EDITABLE_FIELDS = (
    "asset_tag",
    "name",
    "asset_type",
    "manufacturer",
    "model",
    "serial_number",
    "location",
    "notes",
    "is_active",
)


def _asset_values(asset: MaintenanceAsset) -> Dict[str, Any]:
    return {field: getattr(asset, field) for field in _ASSET_EDITABLE_FIELDS}


def _asset_counts(db: Session, company_id: int, asset_ids: List[int]) -> Dict[int, Tuple[int, int]]:
    """``{asset_id: (active schedules, open work orders)}`` in two grouped reads."""
    if not asset_ids:
        return {}
    schedules = dict(
        db.query(MaintenanceSchedule.asset_id, func.count(MaintenanceSchedule.id))
        .filter(
            MaintenanceSchedule.company_id == company_id,
            MaintenanceSchedule.asset_id.in_(asset_ids),
            MaintenanceSchedule.is_active == True,  # noqa: E712
        )
        .group_by(MaintenanceSchedule.asset_id)
        .all()
    )
    open_wos = dict(
        db.query(MaintenanceWorkOrder.asset_id, func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.asset_id.in_(asset_ids),
            MaintenanceWorkOrder.status.notin_(_TERMINAL_STATUSES),
        )
        .group_by(MaintenanceWorkOrder.asset_id)
        .all()
    )
    return {asset_id: (int(schedules.get(asset_id, 0)), int(open_wos.get(asset_id, 0))) for asset_id in asset_ids}


def _asset_response(asset: MaintenanceAsset, counts: Optional[Tuple[int, int]] = None) -> MaintenanceAssetResponse:
    response = MaintenanceAssetResponse.model_validate(asset)
    if counts is not None:
        response.active_schedule_count, response.open_work_order_count = counts
    return response


def _refuse_duplicate_tag(db: Session, company_id: int, asset_tag: str, *, exclude_id: Optional[int] = None) -> None:
    """``asset_tag`` is unique per company, case-insensitively (the DB key is exact-case)."""
    query = db.query(MaintenanceAsset.id).filter(
        MaintenanceAsset.company_id == company_id,
        func.lower(MaintenanceAsset.asset_tag) == asset_tag.lower(),
    )
    if exclude_id is not None:
        query = query.filter(MaintenanceAsset.id != exclude_id)
    if query.first():
        raise HTTPException(status_code=409, detail=f"An asset tagged '{asset_tag}' already exists")


@router.get("/assets", response_model=List[MaintenanceAssetResponse])
def list_maintenance_assets(
    include_inactive: bool = Query(False, description="Include retired (inactive) assets"),
    search: Optional[str] = Query(None, max_length=100, description="Matches tag, name or type"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the company's equipment assets (non-production machines), by tag, each with its
    active schedule and open work order counts."""
    query = db.query(MaintenanceAsset).filter(MaintenanceAsset.company_id == company_id)
    if not include_inactive:
        query = query.filter(MaintenanceAsset.is_active == True)  # noqa: E712
    if search and search.strip():
        pattern = f"%{search.strip().lower()}%"
        query = query.filter(
            func.lower(MaintenanceAsset.asset_tag).like(pattern)
            | func.lower(MaintenanceAsset.name).like(pattern)
            | func.lower(func.coalesce(MaintenanceAsset.asset_type, "")).like(pattern)
        )
    assets = query.order_by(MaintenanceAsset.asset_tag).all()
    counts = _asset_counts(db, company_id, [asset.id for asset in assets])
    return [_asset_response(asset, counts.get(asset.id)) for asset in assets]


@router.get("/assets/{asset_id}", response_model=MaintenanceAssetResponse)
def get_maintenance_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get one equipment asset."""
    asset = _asset_in_company(db, asset_id, company_id)
    return _asset_response(asset, _asset_counts(db, company_id, [asset.id]).get(asset.id))


@router.post("/assets", response_model=MaintenanceAssetResponse)
def create_maintenance_asset(
    data: MaintenanceAssetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Register a non-production equipment asset (forklift, compressor, manual mill...).
    ``asset_tag`` must be unique in the company, ignoring case (409)."""
    _refuse_duplicate_tag(db, company_id, data.asset_tag)
    asset = MaintenanceAsset(**data.model_dump(), company_id=company_id, is_active=True, version=1)
    db.add(asset)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"An asset tagged '{data.asset_tag}' already exists") from exc
    audit.log_create("maintenance_asset", asset.id, asset.asset_tag, new_values=_asset_values(asset))
    db.commit()
    db.refresh(asset)
    return _asset_response(asset, (0, 0))


@router.put("/assets/{asset_id}", response_model=MaintenanceAssetResponse)
def update_maintenance_asset(
    asset_id: int,
    data: MaintenanceAssetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit an equipment asset. ``version`` must be the asset's current version (409 if
    someone else saved first); only the fields sent are changed."""
    asset = (
        db.query(MaintenanceAsset)
        .filter(MaintenanceAsset.id == asset_id, MaintenanceAsset.company_id == company_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if data.version != asset.version:
        raise HTTPException(
            status_code=409,
            detail=f"This asset was changed by someone else (now version {asset.version}); reload it and retry.",
        )
    update_data = data.model_dump(exclude_unset=True, exclude={"version"})
    if "asset_tag" in update_data and update_data["asset_tag"] != asset.asset_tag:
        _refuse_duplicate_tag(db, company_id, update_data["asset_tag"], exclude_id=asset.id)

    old_values = _asset_values(asset)
    for field in _ASSET_EDITABLE_FIELDS:
        if field in update_data:
            setattr(asset, field, update_data[field])
    new_values = _asset_values(asset)
    if new_values != old_values:
        asset.version = asset.version + 1
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="An asset with that tag already exists") from exc
        audit.log_update("maintenance_asset", asset.id, asset.asset_tag, old_values=old_values, new_values=new_values)
        db.commit()
        db.refresh(asset)
    return _asset_response(asset, _asset_counts(db, company_id, [asset.id]).get(asset.id))


@router.delete("/assets/{asset_id}", response_model=MaintenanceAssetResponse)
def retire_maintenance_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Retire an equipment asset (``is_active = false``; never a physical delete).

    Its schedules, work orders and history are kept; deactivate its schedules separately if
    they should stop coming due. ``PUT`` with ``is_active: true`` brings it back."""
    asset = (
        db.query(MaintenanceAsset)
        .filter(MaintenanceAsset.id == asset_id, MaintenanceAsset.company_id == company_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.is_active:
        asset.is_active = False
        asset.version = asset.version + 1
        db.flush()
        audit.log_update(
            "maintenance_asset",
            asset.id,
            asset.asset_tag,
            old_values={"is_active": True},
            new_values={"is_active": False},
            description=f"Retired equipment asset {asset.asset_tag}",
        )
        db.commit()
        db.refresh(asset)
    return _asset_response(asset, _asset_counts(db, company_id, [asset.id]).get(asset.id))


# ── Log ───────────────────────────────────────────────────────────────────


@router.post("/log")
def create_log(
    data: LogCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_PERFORMING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """File a maintenance log entry: an inspection, repair, part replacement, observation or note
    (``event_type``; anything else -- ``started`` and ``completed`` included, which only the work
    order's Start and Complete write -- is **422**).

    Filed against a maintenance work order (``maintenance_wo_id``, open or finished -- a later note on
    a completed work order belongs here), the entry takes the work order's target: leave
    ``work_center_id`` and ``asset_id`` empty, or give the work order's own (a different one is
    **409**). Without a work order, give exactly one target. ``event_date`` defaults to now and may be
    at most 30 days back, never in the future (**422**). ``cost`` is 0 or more. A standing API token
    may file entries: the entry then records it (``filed_via_api_token``), and so does the audit row
    (``extra_data.credential``). The audit row is required evidence (**503**, nothing saved, if it
    cannot be written)."""
    target_work_center_id, target_asset_id = data.work_center_id, data.asset_id
    if data.maintenance_wo_id is not None:
        wo = _wo_or_404(db, data.maintenance_wo_id, company_id)
        given = (data.work_center_id, data.asset_id)
        if given != (None, None) and given != (wo.work_center_id, wo.asset_id):
            # The history reads file the entry under its target: an entry naming one machine's work
            # order against another machine would put that work in the wrong machine's history.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A log entry on maintenance work order {wo.wo_number} is filed against that work order's own "
                    "work center or asset. Leave the work center and asset empty to use it, or file the entry "
                    "without the work order."
                ),
            )
        target_work_center_id, target_asset_id = wo.work_center_id, wo.asset_id
    # Re-resolved inside the tenant even when inherited: a legacy work order pointing at another
    # company's machine must read as a flat 404, never file history against it.
    _resolve_target(db, company_id, work_center_id=target_work_center_id, asset_id=target_asset_id)

    now = datetime.utcnow()
    event_date = now
    if data.event_date is not None:
        event_date = data.event_date
        if event_date.tzinfo is not None:
            event_date = event_date.astimezone(timezone.utc).replace(tzinfo=None)
        if event_date > now + _LOG_FUTURE_SKEW:
            raise HTTPException(status_code=422, detail="event_date cannot be in the future.")
        if event_date < now - timedelta(days=LOG_MAX_BACKDATE_DAYS):
            raise HTTPException(
                status_code=422,
                detail=f"event_date can be at most {LOG_MAX_BACKDATE_DAYS} days back.",
            )

    # company_id was missing here once; MaintenanceLog.company_id is NOT NULL, so
    # every call to this endpoint raised IntegrityError and 500'd.
    log = MaintenanceLog(
        company_id=company_id,
        work_center_id=target_work_center_id,
        asset_id=target_asset_id,
        maintenance_wo_id=data.maintenance_wo_id,
        event_type=data.event_type.value,
        description=data.description,
        performed_by=current_user.id,
        event_date=event_date,
        cost=data.cost,
        api_token_id=getattr(current_user, "_api_token_id", None) if is_api_token_credential(current_user) else None,
    )
    db.add(log)
    db.flush()
    # Required evidence: the entry is part of the maintenance record, and for a token's entry this row
    # is the second record of what filed it -- so if it cannot be written, nothing is saved (503).
    with required_audit(db, "The audit record for this maintenance log entry could not be saved; nothing was saved."):
        audit.log_required(
            audit.ACTIONS["CREATE"],
            "maintenance_log",
            resource_id=log.id,
            resource_identifier=log.event_type,
            description=f"Created maintenance_log: {log.event_type}",
            new_values={
                "event_type": log.event_type,
                "description": log.description,
                "maintenance_wo_id": log.maintenance_wo_id,
                "work_center_id": log.work_center_id,
                "asset_id": log.asset_id,
                "performed_by": log.performed_by,
                "api_token_id": log.api_token_id,
                "event_date": to_utc_iso(log.event_date),
                "cost": log.cost,
            },
        )
    db.commit()
    db.refresh(log)
    return _serialize_log(log, _performer_names(db, company_id, [log]))
