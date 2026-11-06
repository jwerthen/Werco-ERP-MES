from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query as OrmQuery
from sqlalchemy.orm import Session, joinedload, with_loader_criteria

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType, DowntimeReasonCode
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

router = APIRouter()

# Who may write downtime. Until this change every handler here was a bare
# ``get_current_user``, so a VIEWER could start, edit and close downtime events --
# the OEE availability input -- with no audit trail.
#
# Reporting verbs (start / resolve an event) stay open to every role that works on
# the floor; the Downtime page is route-gated on ``work_orders:view`` (every role) and
# operators log machine stoppages from it, so only the read-only Viewer loses them.
# Correcting an event after the fact (``PUT``) and managing the reason-code vocabulary
# match the OEE write roles (``oee.OEE_WRITE_ROLES``: Admin / Manager / Supervisor),
# because both rewrite what the OEE availability figure is computed from.
# ``require_role`` always admits PLATFORM_ADMIN and superusers. Reads stay open.
DOWNTIME_REPORT_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
]
DOWNTIME_MANAGE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

# Fields ``PUT /downtime/{id}`` and ``PUT /downtime/reason-codes/{id}`` may write,
# applied explicitly -- never a blind setattr over the request body.
_EVENT_EDITABLE_FIELDS = (
    "work_center_id",
    "work_order_id",
    "category",
    "planned_type",
    "reason_code",
    "description",
    "resolution",
)
_REASON_CODE_EDITABLE_FIELDS = ("code", "name", "category", "description", "is_active", "display_order")


def _refuse_nulls(model: BaseModel, fields: tuple) -> None:
    nulled = [field for field in fields if field in model.model_fields_set and getattr(model, field) is None]
    if nulled:
        raise ValueError(f"{', '.join(nulled)} cannot be null")


# ============== Pydantic Schemas ==============


class DowntimeEventCreate(BaseModel):
    work_center_id: int
    work_order_id: Optional[int] = None
    start_time: Optional[datetime] = None
    category: DowntimeCategory = DowntimeCategory.OTHER
    planned_type: DowntimePlannedType = DowntimePlannedType.UNPLANNED
    reason_code: Optional[str] = None
    description: Optional[str] = None


class DowntimeEventUpdate(BaseModel):
    work_center_id: Optional[int] = None
    work_order_id: Optional[int] = None
    category: Optional[DowntimeCategory] = None
    planned_type: Optional[DowntimePlannedType] = None
    reason_code: Optional[str] = None
    description: Optional[str] = None
    resolution: Optional[str] = None

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "DowntimeEventUpdate":
        # NOT NULL columns: an explicit null would otherwise reach the INSERT as a 500.
        _refuse_nulls(self, ("work_center_id", "category", "planned_type"))
        return self


class DowntimeResolve(BaseModel):
    end_time: Optional[datetime] = None
    resolution: Optional[str] = None


class ReasonCodeCreate(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    category: DowntimeCategory
    description: Optional[str] = None
    is_active: bool = True
    display_order: int = 0


class ReasonCodeUpdate(BaseModel):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    category: Optional[DowntimeCategory] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "ReasonCodeUpdate":
        _refuse_nulls(self, ("code", "name", "category", "is_active", "display_order"))
        return self


class WorkCenterInfo(BaseModel):
    id: int
    code: str
    name: str

    class Config:
        from_attributes = True


class UserInfo(BaseModel):
    id: int
    # ``User`` has no ``username`` column. As a required field this failed response
    # validation for every event (``reported_by`` is NOT NULL), so each create, list,
    # get and resolve 500'd -- the create AFTER committing. Kept, always null, so the
    # response shape the client declares does not change.
    username: Optional[str] = None
    full_name: Optional[str] = None

    class Config:
        from_attributes = True


class WorkOrderInfo(BaseModel):
    id: int
    wo_number: Optional[str] = None

    class Config:
        from_attributes = True


class DowntimeEventResponse(BaseModel):
    id: int
    work_center_id: int
    work_order_id: Optional[int] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: Optional[float] = None
    category: str
    planned_type: str
    reason_code: Optional[str] = None
    description: Optional[str] = None
    resolution: Optional[str] = None
    reported_by: int
    resolved_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    work_center: Optional[WorkCenterInfo] = None
    reporter: Optional[UserInfo] = None
    resolver: Optional[UserInfo] = None
    work_order: Optional[WorkOrderInfo] = None

    class Config:
        from_attributes = True


class ReasonCodeResponse(BaseModel):
    id: int
    code: str
    name: str
    category: str
    description: Optional[str] = None
    is_active: bool
    display_order: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DowntimeSummary(BaseModel):
    total_downtime_hours: float
    planned_hours: float
    unplanned_hours: float
    planned_percentage: float
    unplanned_percentage: float
    by_category: list
    top_reasons: list
    event_count: int


class WorkCenterDowntime(BaseModel):
    work_center_id: int
    work_center_code: str
    work_center_name: str
    total_hours: float
    event_count: int


# ============== Helpers ==============


def _event_query(db: Session, company_id: int) -> OrmQuery:
    """Tenant-scoped downtime events with the response's relationships eager-loaded.

    The loader criteria close EGRESS: a legacy row written before ``work_center_id`` was
    validated is owned by the caller and passes the ``company_id`` filter, yet its
    relationship would render another tenant's machine / job. Such a relation now reads
    as absent rather than leaking. (The reporter / resolver are deliberately not
    criteria'd: a context-switched platform admin's home company is not the active one.)
    """
    return (
        tenant_query(db, DowntimeEvent, company_id)
        .options(
            joinedload(DowntimeEvent.work_center),
            joinedload(DowntimeEvent.reporter),
            joinedload(DowntimeEvent.resolver),
            joinedload(DowntimeEvent.work_order),
            with_loader_criteria(WorkCenter, WorkCenter.company_id == company_id),
            with_loader_criteria(WorkOrder, WorkOrder.company_id == company_id),
        )
        .populate_existing()
    )


def _event_or_404(db: Session, event_id: int, company_id: int, *, for_update: bool = False) -> DowntimeEvent:
    """Resolve an event inside the caller's tenant, or a flat 404.

    ``for_update`` row-locks the event (``FOR UPDATE OF downtime_events`` on Postgres; a no-op
    on SQLite) for the write verbs: ``DowntimeEvent`` has no ``version`` column, so two
    simultaneous resolves would both pass the ``end_time is None`` check and both write.
    """
    query = tenant_query(db, DowntimeEvent, company_id).filter(DowntimeEvent.id == event_id)
    if for_update:
        query = query.with_for_update(of=DowntimeEvent)
    event = query.first()
    if not event:
        raise HTTPException(status_code=404, detail="Downtime event not found")
    return event


def _assert_event_refs_in_company(
    db: Session, company_id: int, *, work_center_id: Optional[int], work_order_id: Optional[int]
) -> None:
    """Refuse a work center / work order that is not the caller's (flat 404, never 403).

    Unvalidated, a caller could log downtime against another tenant's machine -- and the
    response renders ``work_center.name`` straight back, so the write doubled as a read.
    Deliberately does NOT require the work center to be active: downtime is exactly what
    you record against a machine that is down. A work order link is a new SELECTION, so
    a soft-deleted one is refused (invariant 3).
    """
    if work_center_id is not None:
        if not tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first():
            raise HTTPException(status_code=404, detail="Work center not found")
    if work_order_id is not None:
        live = (
            tenant_query(db, WorkOrder, company_id)
            .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted == False)  # noqa: E712
            .first()
        )
        if not live:
            raise HTTPException(status_code=404, detail="Work order not found")


def _changed_ref(update_data: dict, field: str, current: Optional[int]) -> Optional[int]:
    """The id ``field`` is being re-pointed AT, or None when the body leaves it unchanged."""
    if field not in update_data or update_data[field] == current:
        return None
    return update_data[field]


def _event_identifier(event: DowntimeEvent) -> str:
    return f"DT-{event.id}"


def _event_audit_values(event: DowntimeEvent) -> dict:
    return {field: getattr(event, field) for field in _EVENT_EDITABLE_FIELDS}


# ============== Downtime Event Endpoints ==============


@router.get("/active", response_model=List[DowntimeEventResponse])
def get_active_downtime(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all currently active (ongoing) downtime events"""
    events = (
        _event_query(db, company_id)
        .filter(DowntimeEvent.end_time.is_(None))
        .order_by(DowntimeEvent.start_time.desc())
        .all()
    )
    return events


@router.get("/summary")
def get_downtime_summary(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get summary statistics for downtime events"""
    query = tenant_query(db, DowntimeEvent, company_id).filter(DowntimeEvent.end_time.isnot(None))

    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))
    if work_center_id:
        query = query.filter(DowntimeEvent.work_center_id == work_center_id)

    events = query.all()

    total_minutes = sum(e.duration_minutes or 0 for e in events)
    planned_minutes = sum(e.duration_minutes or 0 for e in events if e.planned_type == DowntimePlannedType.PLANNED)
    unplanned_minutes = total_minutes - planned_minutes

    # By category breakdown
    category_map: dict = {}
    for e in events:
        cat = e.category.value if e.category else "other"
        if cat not in category_map:
            category_map[cat] = 0.0
        category_map[cat] += (e.duration_minutes or 0) / 60.0

    by_category = [
        {"category": cat, "hours": round(hrs, 2)} for cat, hrs in sorted(category_map.items(), key=lambda x: -x[1])
    ]

    # Top reasons (Pareto)
    reason_map: dict = {}
    for e in events:
        reason = e.reason_code or e.description or "Unspecified"
        if reason not in reason_map:
            reason_map[reason] = 0.0
        reason_map[reason] += (e.duration_minutes or 0) / 60.0

    top_reasons = [
        {"reason": reason, "hours": round(hrs, 2)} for reason, hrs in sorted(reason_map.items(), key=lambda x: -x[1])
    ][:15]

    return {
        "total_downtime_hours": round(total_minutes / 60.0, 2),
        "planned_hours": round(planned_minutes / 60.0, 2),
        "unplanned_hours": round(unplanned_minutes / 60.0, 2),
        "planned_percentage": round((planned_minutes / total_minutes * 100) if total_minutes > 0 else 0, 1),
        "unplanned_percentage": round((unplanned_minutes / total_minutes * 100) if total_minutes > 0 else 0, 1),
        "by_category": by_category,
        "top_reasons": top_reasons,
        "event_count": len(events),
    }


@router.get("/by-work-center")
def get_downtime_by_work_center(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get downtime hours grouped by work center"""
    query = (
        db.query(
            DowntimeEvent.work_center_id,
            WorkCenter.code,
            WorkCenter.name,
            func.sum(DowntimeEvent.duration_minutes).label("total_minutes"),
            func.count(DowntimeEvent.id).label("event_count"),
        )
        .join(WorkCenter, DowntimeEvent.work_center_id == WorkCenter.id)
        .filter(DowntimeEvent.end_time.isnot(None))
    )
    # Both sides of the join: a legacy event pointing at another tenant's machine must
    # not render that machine's code and name.
    query = tenant_filter(tenant_filter(query, DowntimeEvent, company_id), WorkCenter, company_id)

    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))

    results = (
        query.group_by(DowntimeEvent.work_center_id, WorkCenter.code, WorkCenter.name)
        .order_by(func.sum(DowntimeEvent.duration_minutes).desc())
        .all()
    )

    return [
        {
            "work_center_id": r.work_center_id,
            "work_center_code": r.code,
            "work_center_name": r.name,
            "total_hours": round((r.total_minutes or 0) / 60.0, 2),
            "event_count": r.event_count,
        }
        for r in results
    ]


@router.get("/reason-codes", response_model=List[ReasonCodeResponse])
def list_reason_codes(
    category: Optional[DowntimeCategory] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all downtime reason codes"""
    query = db.query(DowntimeReasonCode).filter(DowntimeReasonCode.company_id == company_id)
    if category:
        query = query.filter(DowntimeReasonCode.category == category)
    if active_only:
        query = query.filter(DowntimeReasonCode.is_active == True)
    return query.order_by(DowntimeReasonCode.display_order, DowntimeReasonCode.code).all()


@router.post("/reason-codes", response_model=ReasonCodeResponse)
def create_reason_code(
    data: ReasonCodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOWNTIME_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new downtime reason code. **Role: ADMIN / MANAGER / SUPERVISOR.**"""
    existing = tenant_query(db, DowntimeReasonCode, company_id).filter(DowntimeReasonCode.code == data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Reason code already exists")

    reason_code = DowntimeReasonCode(**data.model_dump())
    reason_code.company_id = company_id
    db.add(reason_code)
    try:
        db.flush()
    except IntegrityError:
        # ``downtime_reason_codes.code`` still carries a GLOBAL unique constraint, so a
        # code another tenant already uses fails here rather than in the probe above.
        db.rollback()
        raise HTTPException(status_code=400, detail="Reason code already exists")
    audit.log_create("downtime_reason_code", reason_code.id, reason_code.code, new_values=reason_code)
    db.commit()
    db.refresh(reason_code)
    return reason_code


@router.put("/reason-codes/{reason_code_id}", response_model=ReasonCodeResponse)
def update_reason_code(
    reason_code_id: int,
    data: ReasonCodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOWNTIME_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a downtime reason code. **Role: ADMIN / MANAGER / SUPERVISOR.**"""
    reason_code = (
        tenant_query(db, DowntimeReasonCode, company_id).filter(DowntimeReasonCode.id == reason_code_id).first()
    )
    if not reason_code:
        raise HTTPException(status_code=404, detail="Reason code not found")

    update_data = data.model_dump(exclude_unset=True)
    new_code = update_data.get("code")
    if new_code is not None and new_code != reason_code.code:
        duplicate = (
            tenant_query(db, DowntimeReasonCode, company_id)
            .filter(DowntimeReasonCode.code == new_code, DowntimeReasonCode.id != reason_code.id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail="Reason code already exists")

    old_values = {field: getattr(reason_code, field) for field in _REASON_CODE_EDITABLE_FIELDS}
    for field in _REASON_CODE_EDITABLE_FIELDS:
        if field in update_data:
            setattr(reason_code, field, update_data[field])

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Reason code already exists")
    audit.log_update(
        "downtime_reason_code",
        reason_code.id,
        reason_code.code,
        old_values=old_values,
        new_values={field: getattr(reason_code, field) for field in _REASON_CODE_EDITABLE_FIELDS},
    )
    db.commit()
    db.refresh(reason_code)
    return reason_code


@router.get("/", response_model=List[DowntimeEventResponse])
def list_downtime_events(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    work_center_id: Optional[int] = None,
    category: Optional[DowntimeCategory] = None,
    planned_type: Optional[DowntimePlannedType] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List downtime events with optional filters"""
    query = _event_query(db, company_id)

    if work_center_id:
        query = query.filter(DowntimeEvent.work_center_id == work_center_id)
    if category:
        query = query.filter(DowntimeEvent.category == category)
    if planned_type:
        query = query.filter(DowntimeEvent.planned_type == planned_type)
    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))
    if active_only:
        query = query.filter(DowntimeEvent.end_time.is_(None))

    return query.order_by(DowntimeEvent.start_time.desc()).offset(skip).limit(limit).all()


@router.get("/{event_id}", response_model=DowntimeEventResponse)
def get_downtime_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a single downtime event by ID"""
    event = _event_query(db, company_id).filter(DowntimeEvent.id == event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Downtime event not found")
    return event


@router.post("/", response_model=DowntimeEventResponse)
def create_downtime_event(
    data: DowntimeEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOWNTIME_REPORT_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Start a new downtime event. **Role: any tenant role except VIEWER.**

    ``work_center_id`` and ``work_order_id`` must belong to the active company (404).
    """
    _assert_event_refs_in_company(db, company_id, work_center_id=data.work_center_id, work_order_id=data.work_order_id)
    event = DowntimeEvent(
        work_center_id=data.work_center_id,
        work_order_id=data.work_order_id,
        start_time=data.start_time or datetime.utcnow(),
        category=data.category,
        planned_type=data.planned_type,
        reason_code=data.reason_code,
        description=data.description,
        reported_by=current_user.id,
    )
    event.company_id = company_id
    db.add(event)
    db.flush()
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="downtime_started",
        source_module="downtime",
        entity_type="downtime_event",
        entity_id=event.id,
        work_order_id=event.work_order_id,
        user_id=current_user.id,
        severity="medium" if data.planned_type == DowntimePlannedType.UNPLANNED else "info",
        event_payload={
            "work_center_id": event.work_center_id,
            "category": data.category.value if hasattr(data.category, "value") else data.category,
            "planned_type": data.planned_type.value if hasattr(data.planned_type, "value") else data.planned_type,
            "reason_code": data.reason_code,
        },
    )
    audit.log_create("downtime_event", event.id, _event_identifier(event), new_values=event)
    db.commit()

    # Reload with relationships
    return _event_query(db, company_id).filter(DowntimeEvent.id == event.id).first()


@router.put("/{event_id}", response_model=DowntimeEventResponse)
def update_downtime_event(
    event_id: int,
    data: DowntimeEventUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOWNTIME_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Correct a downtime event. **Role: ADMIN / MANAGER / SUPERVISOR.**

    A re-pointed ``work_center_id`` / ``work_order_id`` must belong to the active company (404).
    Only a CHANGED reference is validated: re-sending the one the event already names is not a
    new selection, so a correction to another field still saves after that work order was
    soft-deleted -- a historical record keeps naming what it named (invariant 3).
    """
    event = _event_or_404(db, event_id, company_id, for_update=True)

    update_data = data.model_dump(exclude_unset=True)
    _assert_event_refs_in_company(
        db,
        company_id,
        work_center_id=_changed_ref(update_data, "work_center_id", event.work_center_id),
        work_order_id=_changed_ref(update_data, "work_order_id", event.work_order_id),
    )

    old_values = _event_audit_values(event)
    for field in _EVENT_EDITABLE_FIELDS:
        if field in update_data:
            setattr(event, field, update_data[field])

    db.flush()
    audit.log_update(
        "downtime_event",
        event.id,
        _event_identifier(event),
        old_values=old_values,
        new_values=_event_audit_values(event),
    )
    db.commit()

    return _event_query(db, company_id).filter(DowntimeEvent.id == event.id).first()


@router.post("/{event_id}/resolve", response_model=DowntimeEventResponse)
def resolve_downtime_event(
    event_id: int,
    data: DowntimeResolve,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOWNTIME_REPORT_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """End/resolve a downtime event - sets end_time and calculates duration.

    **Role: any tenant role except VIEWER.**
    """
    event = _event_or_404(db, event_id, company_id, for_update=True)

    if event.end_time is not None:
        raise HTTPException(status_code=400, detail="Downtime event is already resolved")

    end_time = data.end_time or datetime.utcnow()
    if end_time <= event.start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")

    event.end_time = end_time
    event.duration_minutes = round((end_time - event.start_time).total_seconds() / 60.0, 2)
    event.resolved_by = current_user.id
    if data.resolution:
        event.resolution = data.resolution

    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="downtime_resolved",
        source_module="downtime",
        entity_type="downtime_event",
        entity_id=event.id,
        work_order_id=event.work_order_id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "work_center_id": event.work_center_id,
            "duration_minutes": event.duration_minutes,
            "resolution": event.resolution,
        },
    )
    audit.log_status_change(
        "downtime_event",
        event.id,
        _event_identifier(event),
        old_status="active",
        new_status="resolved",
        description=f"Resolved downtime event {_event_identifier(event)}",
        extra_data={
            "end_time": end_time.isoformat(),
            "duration_minutes": event.duration_minutes,
            "resolution": event.resolution,
        },
    )
    db.commit()

    return _event_query(db, company_id).filter(DowntimeEvent.id == event.id).first()
