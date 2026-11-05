"""NCR and CAR writes: tenant-validated references, final-record immutability, required audit.

Every NCR / CAR mutation behind ``/api/v1/quality/ncr`` and ``/api/v1/quality/car`` runs
here, inside :func:`quality_record_transaction`: the change and its ``audit_log`` row
commit together or not at all, and a lost audit row rolls the change back and answers
**503** (the ``fai_service`` posture). These are AS9100D 8.7 / 10.2 records -- who
dispositioned a nonconformance, who closed it, who verified a corrective action -- so a
write whose evidence could silently vanish is not one this module performs. The
``OperationalEvent`` each write still emits is notification / timeline telemetry, never
a substitute for that row.

Rules every writer here keeps:

* **Tenant references are resolved, never trusted.** ``part_id`` and ``work_order_id``
  (create) must name a live row of the active company; ``assigned_to`` must name an
  active user of it; ``car_id`` must name one of its CARs that is not void. Anything
  else is a 404 raised BEFORE the first mutation. A foreign ``part_id`` used to be
  stored and then echoed back through ``NCRResponse.part`` -- another tenant's part
  number and name. Reads load ``part`` through the same company criterion
  (:func:`ncr_query`), so a legacy row carrying such an id shows no part rather than
  someone else's.
* **A CLOSED or VOID record is final.** ``PUT`` refuses (409) any change to one, except
  a request whose ONLY change is moving a CLOSED record's ``status`` to an open state
  -- the reopen. Reopening clears ``closed_date`` / ``closed_by`` (the record is no
  longer closed); the audit row keeps the values it cleared. Content edits happen after
  the reopen, as their own audited save. A VOID record has no way back through ``PUT``.
* **Only real changes are written.** The detail screen re-sends every field on save, so
  each requested value is compared with the stored one (``""`` equals ``None``,
  ``Decimal`` equals the stored float) and only the fields that actually differ are
  applied and audited. A save that changes nothing writes nothing, so re-saving a closed
  record unchanged is a 200 rather than a refusal.
* **One audit row per request**, holding every changed field old -> new (plus the
  ``changes`` diff ``AuditService.log_update`` writes). Its action is ``STATUS_CHANGE``
  when the status moved -- closure, reopen, verification -- and ``UPDATE`` otherwise; a
  disposition change is always on it.
* **Creating a CAR from an NCR is idempotent**: an NCR already linked to a CAR that is
  not void answers 409 naming that CAR instead of minting a second one and silently
  relinking. A CAR is retired with the reasoned :func:`void_car`, after which the NCR may
  raise a fresh one.

Lock order: every row lock (``SELECT ... FOR UPDATE``) and number-generator advisory
lock is taken BEFORE the first audit row, because the audit chain lock is global.
"""

import enum
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Query, Session, selectinload

from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.quality import CARCreate, CARUpdate, NCRCreate, NCRUpdate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operational_event_service import OperationalEventService
from app.services.quality_numbering import generate_ncr_number

# Who may edit, disposition, close, link, void or restore an NCR / CAR. Filing an NCR
# or opening a CAR stays open to any authenticated user of the company (unchanged).
WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]

NCR_RESOURCE = "ncr"
CAR_RESOURCE = "car"

REFERENCE_NOT_FOUND = {
    "part_id": "Part not found",
    "work_order_id": "Work order not found",
    "assigned_to": "Assigned user not found",
    "car_id": "CAR not found",
}

# Columns a request may never null out (each has a model default and readers that
# assume a value). ``quantity_rejected`` is also refused by ``NCRUpdate`` itself.
_NCR_NON_NULLABLE = ("status", "disposition", "car_required", "quantity_rejected")
_CAR_NON_NULLABLE = ("status", "priority")


@contextmanager
def quality_record_transaction(db: Session):
    """Commit the write with its required audit row, or roll both back (503 on audit loss)."""
    try:
        with atomic_transaction(db):
            yield
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc


# ----------------------------------------------------------------------------- values


def _value(value: Any) -> Any:
    """JSON-safe form of a column value for the audit row."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _comparable(value: Any) -> Any:
    """Normalize a stored or requested value so "the same value" compares equal."""
    value = _value(value)
    if value == "":
        return None
    return value


def _snapshot(row: Any) -> Dict[str, Any]:
    return {column.key: _value(getattr(row, column.key)) for column in inspect(type(row)).columns}


def _changed_fields(row: Any, requested: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of ``requested`` whose value differs from what ``row`` holds."""
    return {
        field: value for field, value in requested.items() if _comparable(getattr(row, field)) != _comparable(value)
    }


def _refuse_nulls(requested: Dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        if field in requested and requested[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")


def _check_expected_updated_at(row: Any, expected: Optional[datetime]) -> None:
    if expected and row.updated_at and expected.replace(tzinfo=None) != row.updated_at.replace(tzinfo=None):
        raise HTTPException(status_code=409, detail="This record changed since you opened it. Reload it before saving.")


def _audit(
    audit: AuditService,
    action: str,
    resource_type: str,
    row: Any,
    identifier: str,
    *,
    description: str,
    old: Optional[Dict[str, Any]] = None,
    new: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the required audit row; ``AuditWriteError`` rolls the whole write back."""
    audit.log_required(
        action,
        resource_type,
        resource_id=row.id,
        resource_identifier=identifier,
        description=description,
        old_values=old,
        new_values=new,
        extra_data=extra,
        company_id=row.company_id,
    )


def _diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {key: {"old": old.get(key), "new": new.get(key)} for key in new if old.get(key) != new.get(key)}


def _guard_final_record(status: Any, changes: Dict[str, Any], label: str, closed_status: Any, void_status: Any) -> bool:
    """Refuse changes to a CLOSED / VOID record; return True when the request is a reopen."""
    if status == void_status:
        raise HTTPException(status_code=409, detail=f"{label} is void; a voided record cannot be changed.")
    if status != closed_status:
        return False
    if set(changes) != {"status"}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{label} is closed; its record cannot be edited. Reopen it first by changing only its "
                "status, then make the edit."
            ),
        )
    return True


# ------------------------------------------------------------------ reference resolvers


def _live_part_or_404(db: Session, company_id: int, part_id: int) -> Part:
    part = tenant_query(db, Part, company_id).filter(Part.id == part_id, Part.is_deleted.is_(False)).first()
    if part is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["part_id"])
    return part


def _live_work_order_or_404(db: Session, company_id: int, work_order_id: int) -> WorkOrder:
    work_order = (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted.is_(False))
        .first()
    )
    if work_order is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["work_order_id"])
    return work_order


def _assignable_user_or_404(db: Session, company_id: int, user_id: int) -> User:
    """An ACTIVE user of the active company -- assignment is a selection, not a record."""
    user = tenant_query(db, User, company_id).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["assigned_to"])
    return user


def _linkable_car_or_404(db: Session, company_id: int, car_id: int) -> CorrectiveActionRequest:
    car = tenant_query(db, CorrectiveActionRequest, company_id).filter(CorrectiveActionRequest.id == car_id).first()
    if car is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["car_id"])
    if car.status == CARStatus.VOID:
        raise HTTPException(status_code=409, detail=f"CAR {car.car_number} is void and cannot be linked")
    return car


# --------------------------------------------------------------------------- NCR reads


def ncr_query(db: Session, company_id: int) -> Query:
    """Live NCRs of the company, with ``part`` loaded only when it is the company's own.

    The relationship criterion matters for legacy rows: an NCR whose ``part_id`` names
    another tenant's part (stored before references were validated) serializes with no
    part instead of echoing that part's number and name. A soft-deleted part of the same
    company still shows -- an NCR is a historical record and keeps naming what it named.
    """
    return (
        tenant_query(db, NonConformanceReport, company_id)
        .filter(NonConformanceReport.is_deleted.is_(False))
        .options(selectinload(NonConformanceReport.part.and_(Part.company_id == company_id)))
        .populate_existing()
    )


def get_ncr(db: Session, company_id: int, ncr_id: int) -> NonConformanceReport:
    ncr = ncr_query(db, company_id).filter(NonConformanceReport.id == ncr_id).first()
    if ncr is None:
        raise HTTPException(status_code=404, detail="NCR not found")
    return ncr


def _live_ncr_for_update_or_404(db: Session, company_id: int, ncr_id: int) -> NonConformanceReport:
    """Lock a live NCR for a write.

    ``void_ncr`` and ``restore_ncr`` deliberately do not use this: one must see the
    tombstone to refuse a double void, the other to undo one.
    """
    ncr = (
        tenant_query(db, NonConformanceReport, company_id)
        .filter(NonConformanceReport.id == ncr_id, NonConformanceReport.is_deleted.is_(False))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if ncr is None:
        raise HTTPException(status_code=404, detail="NCR not found")
    return ncr


def _ncr_any_state_for_update_or_404(db: Session, company_id: int, ncr_id: int) -> NonConformanceReport:
    ncr = (
        tenant_query(db, NonConformanceReport, company_id)
        .filter(NonConformanceReport.id == ncr_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if ncr is None:
        raise HTTPException(status_code=404, detail="NCR not found")
    return ncr


# -------------------------------------------------------------------------- NCR writes


def create_ncr(db: Session, company_id: int, data: NCRCreate, user: User, audit: AuditService) -> NonConformanceReport:
    with quality_record_transaction(db):
        if data.part_id is not None:
            _live_part_or_404(db, company_id, data.part_id)
        if data.work_order_id is not None:
            _live_work_order_or_404(db, company_id, data.work_order_id)
        # detected_date is a field on NCRCreate, so it is already in model_dump() -- resolve
        # its default in place rather than passing it a second time (which raised
        # "got multiple values for keyword argument 'detected_date'").
        ncr_data = data.model_dump()
        ncr_data["detected_date"] = ncr_data.get("detected_date") or date.today()
        ncr = NonConformanceReport(
            ncr_number=generate_ncr_number(db, company_id),
            **ncr_data,
            detected_by=user.id,
        )
        ncr.company_id = company_id
        db.add(ncr)
        db.flush()
        _audit(
            audit,
            "CREATE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Created NCR {ncr.ncr_number}",
            new=_snapshot(ncr),
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="ncr_created",
            source_module="quality",
            entity_type="ncr",
            entity_id=ncr.id,
            work_order_id=ncr.work_order_id,
            user_id=user.id,
            severity="high" if ncr.source == NCRSource.CUSTOMER_RETURN else "medium",
            event_payload={
                "ncr_number": ncr.ncr_number,
                "title": ncr.title,
                "source": _value(ncr.source),
                "status": _value(ncr.status),
                "disposition": _value(ncr.disposition),
                "part_id": ncr.part_id,
                "quantity_affected": float(ncr.quantity_affected or 0),
            },
        )
        created_id = ncr.id
    return get_ncr(db, company_id, created_id)


def update_ncr(
    db: Session, company_id: int, ncr_id: int, data: NCRUpdate, user: User, audit: AuditService
) -> NonConformanceReport:
    with quality_record_transaction(db):
        ncr = _live_ncr_for_update_or_404(db, company_id, ncr_id)
        requested = data.model_dump(exclude_unset=True)
        expected = requested.pop("expected_updated_at", None)
        requested.pop("version", None)  # legacy schema field; models use updated_at for conflict detection
        _check_expected_updated_at(ncr, expected)
        _refuse_nulls(requested, _NCR_NON_NULLABLE)
        if requested.get("status") == NCRStatus.VOID:
            raise HTTPException(status_code=422, detail="Use the reasoned void action to void a quality record")
        if requested.get("quantity_rejected", ncr.quantity_rejected or 0) > ncr.quantity_affected:
            raise HTTPException(status_code=422, detail="Rejected quantity cannot exceed affected quantity")
        changes = _changed_fields(ncr, requested)
        if changes:
            _write_ncr_changes(db, company_id, ncr, changes, user, audit)
    # Read back AFTER the commit (which expires every loaded attribute) so the response's
    # ``part`` comes through the tenant-scoped loader, not an unscoped lazy load.
    return get_ncr(db, company_id, ncr_id)


def _write_ncr_changes(
    db: Session,
    company_id: int,
    ncr: NonConformanceReport,
    changes: Dict[str, Any],
    user: User,
    audit: AuditService,
) -> None:
    reopening = _guard_final_record(ncr.status, changes, f"NCR {ncr.ncr_number}", NCRStatus.CLOSED, NCRStatus.VOID)
    if changes.get("assigned_to") is not None:
        _assignable_user_or_404(db, company_id, changes["assigned_to"])
    if changes.get("car_id") is not None:
        _linkable_car_or_404(db, company_id, changes["car_id"])

    closing = changes.get("status") == NCRStatus.CLOSED
    tracked = list(changes) + (["closed_date", "closed_by"] if closing or reopening else [])
    old = {field: _value(getattr(ncr, field)) for field in tracked}
    previous_status = ncr.status
    previous_disposition = ncr.disposition
    for field, value in changes.items():
        setattr(ncr, field, value)
    if closing:
        ncr.closed_date = date.today()
        ncr.closed_by = user.id
    elif reopening:
        ncr.closed_date = None
        ncr.closed_by = None
    new = {field: _value(getattr(ncr, field)) for field in tracked}

    extra: Dict[str, Any] = {"changes": _diff(old, new)}
    if closing:
        extra["closure"] = True
    if reopening:
        extra["reopen"] = True
    if "disposition" in changes:
        extra["disposition_change"] = True
    status_moved = "status" in changes
    _audit(
        audit,
        "STATUS_CHANGE" if status_moved else "UPDATE",
        NCR_RESOURCE,
        ncr,
        ncr.ncr_number,
        description=(
            f"Changed NCR {ncr.ncr_number} status from '{old['status']}' to '{new['status']}'"
            if status_moved
            else f"Updated NCR {ncr.ncr_number}: {', '.join(sorted(changes))}"
        ),
        old=old,
        new=new,
        extra=extra,
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="ncr_updated",
        source_module="quality",
        entity_type="ncr",
        entity_id=ncr.id,
        work_order_id=ncr.work_order_id,
        user_id=user.id,
        severity="info" if ncr.status == previous_status else "medium",
        event_payload={
            "ncr_number": ncr.ncr_number,
            "changed_fields": sorted(changes),
            "previous_status": _value(previous_status),
            "status": _value(ncr.status),
            "previous_disposition": _value(previous_disposition),
            "disposition": _value(ncr.disposition),
        },
    )


def void_ncr(db: Session, company_id: int, ncr_id: int, reason: str, user: User, audit: AuditService) -> str:
    """Void (soft-delete) an NCR; refused while it still actively gates a work order."""
    with quality_record_transaction(db):
        ncr = _ncr_any_state_for_update_or_404(db, company_id, ncr_id)
        if ncr.is_deleted:
            raise HTTPException(status_code=400, detail="NCR is already voided")

        # GUARDRAIL: refuse to void an NCR that is still actively gating a work order. An
        # "active" blocker is OPEN or ACKNOWLEDGED (RESOLVED/DISMISSED are terminal) -- the
        # same condition used everywhere else (quality_gate_service, scheduling_service, ...).
        active_blocker = (
            tenant_query(db, WorkOrderBlocker, company_id)
            .filter(
                WorkOrderBlocker.ncr_id == ncr.id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .first()
        )
        if active_blocker:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot void NCR {ncr.ncr_number}: it is blocking work order(s). Resolve the blocker first.",
            )

        old_status = _value(ncr.status)
        ncr.soft_delete(user.id)
        ncr.status = NCRStatus.VOID
        _audit(
            audit,
            "STATUS_CHANGE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Voided NCR: {reason}",
            old={"status": old_status},
            new={"status": NCRStatus.VOID.value},
            extra={"reason": reason},
        )
        _audit(
            audit,
            "DELETE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Soft Deleted ncr: {ncr.ncr_number}",
            extra={"soft_delete": True, "reason": reason},
        )
        number = ncr.ncr_number
    return number


def restore_ncr(db: Session, company_id: int, ncr_id: int, audit: AuditService) -> str:
    """Restore a voided NCR. The pre-void status is not preserved: it reopens to OPEN."""
    with quality_record_transaction(db):
        ncr = _ncr_any_state_for_update_or_404(db, company_id, ncr_id)
        if not ncr.is_deleted:
            raise HTTPException(status_code=400, detail="NCR is not voided")
        ncr.restore()
        ncr.status = NCRStatus.OPEN
        old = {"is_deleted": True, "status": NCRStatus.VOID.value}
        new = {"is_deleted": False, "status": NCRStatus.OPEN.value}
        _audit(
            audit,
            "RESTORE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Restore ncr: {ncr.ncr_number}",
            old=old,
            new=new,
            extra={"changes": _diff(old, new)},
        )
        number = ncr.ncr_number
    return number


# --------------------------------------------------------------------------- CAR writes


def generate_car_number(db: Session, company_id: Optional[int] = None) -> str:
    acquire_generator_lock(db, "car_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CAR-{today}-"
    query = db.query(CorrectiveActionRequest).filter(CorrectiveActionRequest.car_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(CorrectiveActionRequest.company_id == company_id)
    last = query.order_by(CorrectiveActionRequest.car_number.desc()).first()
    num = int(last.car_number.split("-")[-1]) + 1 if last else 1
    return f"{prefix}{num:03d}"


def get_car(db: Session, company_id: int, car_id: int) -> CorrectiveActionRequest:
    car = (
        tenant_query(db, CorrectiveActionRequest, company_id)
        .filter(CorrectiveActionRequest.id == car_id)
        .populate_existing()
        .first()
    )
    if car is None:
        raise HTTPException(status_code=404, detail="CAR not found")
    return car


def _car_for_update_or_404(db: Session, company_id: int, car_id: int) -> CorrectiveActionRequest:
    car = (
        tenant_query(db, CorrectiveActionRequest, company_id)
        .filter(CorrectiveActionRequest.id == car_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if car is None:
        raise HTTPException(status_code=404, detail="CAR not found")
    return car


def create_car_from_ncr(
    db: Session, company_id: int, ncr_id: int, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    """Raise a CAR for an NCR, once.

    The NCR row is locked first, so two concurrent calls serialize and the second sees
    the first one's link. Allowed on a CLOSED NCR too: deciding a nonconformance needs
    corrective action can follow its disposition (AS9100D 10.2.1 b), and this verb
    touches only the link fields, never the closed disposition / root-cause evidence.
    """
    with quality_record_transaction(db):
        ncr = _live_ncr_for_update_or_404(db, company_id, ncr_id)
        if ncr.car_id is not None:
            existing = (
                tenant_query(db, CorrectiveActionRequest, company_id)
                .filter(CorrectiveActionRequest.id == ncr.car_id)
                .first()
            )
            if existing is not None and existing.status != CARStatus.VOID:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"NCR {ncr.ncr_number} already has corrective action {existing.car_number} "
                        f"(id {existing.id}). Open that CAR instead of creating another."
                    ),
                )
        car = CorrectiveActionRequest(
            car_number=generate_car_number(db, company_id),
            title=f"CAR for {ncr.ncr_number}: {ncr.title}"[:255],
            problem_description=ncr.description,
            initiated_by=user.id,
        )
        car.company_id = company_id
        db.add(car)
        db.flush()

        old_link = {"car_required": ncr.car_required, "car_id": ncr.car_id}
        ncr.car_required = True
        ncr.car_id = car.id
        new_link = {"car_required": True, "car_id": car.id}

        _audit(
            audit,
            "CREATE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=f"Created CAR {car.car_number} from NCR {ncr.ncr_number}",
            new=_snapshot(car),
            extra={"ncr_id": ncr.id, "ncr_number": ncr.ncr_number},
        )
        _audit(
            audit,
            "UPDATE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Linked NCR {ncr.ncr_number} to CAR {car.car_number}",
            old=old_link,
            new=new_link,
            extra={"changes": _diff(old_link, new_link), "car_number": car.car_number},
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="car_created_from_ncr",
            source_module="quality",
            entity_type="car",
            entity_id=car.id,
            work_order_id=ncr.work_order_id,
            user_id=user.id,
            severity="medium",
            event_payload={
                "car_number": car.car_number,
                "ncr_id": ncr.id,
                "ncr_number": ncr.ncr_number,
                "title": car.title,
                "priority": car.priority,
            },
        )
        created_id = car.id
    return get_car(db, company_id, created_id)


def create_car(
    db: Session, company_id: int, data: CARCreate, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    with quality_record_transaction(db):
        car = CorrectiveActionRequest(
            car_number=generate_car_number(db, company_id), **data.model_dump(), initiated_by=user.id
        )
        car.company_id = company_id
        db.add(car)
        db.flush()
        _audit(
            audit,
            "CREATE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=f"Created CAR {car.car_number}",
            new=_snapshot(car),
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="car_created",
            source_module="quality",
            entity_type="car",
            entity_id=car.id,
            user_id=user.id,
            severity="high" if car.priority <= 2 else "medium",
            event_payload={
                "car_number": car.car_number,
                "title": car.title,
                "car_type": _value(car.car_type),
                "status": _value(car.status),
                "priority": car.priority,
                "due_date": car.due_date.isoformat() if car.due_date else None,
            },
        )
        created_id = car.id
    return get_car(db, company_id, created_id)


def update_car(
    db: Session, company_id: int, car_id: int, data: CARUpdate, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    with quality_record_transaction(db):
        car = _car_for_update_or_404(db, company_id, car_id)
        requested = data.model_dump(exclude_unset=True)
        expected = requested.pop("expected_updated_at", None)
        requested.pop("version", None)  # legacy schema field; models use updated_at for conflict detection
        _check_expected_updated_at(car, expected)
        _refuse_nulls(requested, _CAR_NON_NULLABLE)
        if requested.get("status") == CARStatus.VOID:
            raise HTTPException(status_code=422, detail="Use the reasoned void action to void a quality record")
        changes = _changed_fields(car, requested)
        if changes:
            _write_car_changes(db, company_id, car, requested, changes, user, audit)
    return get_car(db, company_id, car_id)


def _write_car_changes(
    db: Session,
    company_id: int,
    car: CorrectiveActionRequest,
    requested: Dict[str, Any],
    changes: Dict[str, Any],
    user: User,
    audit: AuditService,
) -> None:
    reopening = _guard_final_record(car.status, changes, f"CAR {car.car_number}", CARStatus.CLOSED, CARStatus.VOID)
    closing = changes.get("status") == CARStatus.CLOSED
    verifying = changes.get("status") == CARStatus.VERIFICATION
    if closing:
        missing = [
            field.replace("_", " ")
            for field in ("root_cause", "corrective_action", "verification_method", "verification_results")
            if not str(requested.get(field, getattr(car, field)) or "").strip()
        ]
        if missing:
            raise HTTPException(status_code=422, detail="Closure requires " + ", ".join(missing))
    if changes.get("assigned_to") is not None:
        _assignable_user_or_404(db, company_id, changes["assigned_to"])

    side_fields: List[str] = []
    if verifying:
        side_fields = ["verified_by"]
    elif closing or reopening:
        side_fields = ["closed_date", "closed_by"]
    tracked = list(changes) + side_fields
    old = {field: _value(getattr(car, field)) for field in tracked}
    previous_status = car.status
    for field, value in changes.items():
        setattr(car, field, value)
    if verifying:
        car.verified_by = user.id
    elif closing:
        car.closed_date = date.today()
        car.closed_by = user.id
    elif reopening:
        car.closed_date = None
        car.closed_by = None
    new = {field: _value(getattr(car, field)) for field in tracked}

    extra: Dict[str, Any] = {"changes": _diff(old, new)}
    if closing:
        extra["closure"] = True
    if verifying:
        extra["verification"] = True
    if reopening:
        extra["reopen"] = True
    status_moved = "status" in changes
    _audit(
        audit,
        "STATUS_CHANGE" if status_moved else "UPDATE",
        CAR_RESOURCE,
        car,
        car.car_number,
        description=(
            f"Changed CAR {car.car_number} status from '{old['status']}' to '{new['status']}'"
            if status_moved
            else f"Updated CAR {car.car_number}: {', '.join(sorted(changes))}"
        ),
        old=old,
        new=new,
        extra=extra,
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="car_updated",
        source_module="quality",
        entity_type="car",
        entity_id=car.id,
        user_id=user.id,
        severity="info" if car.status == previous_status else "medium",
        event_payload={
            "car_number": car.car_number,
            "changed_fields": sorted(changes),
            "previous_status": _value(previous_status),
            "status": _value(car.status),
            "priority": car.priority,
        },
    )


def void_car(
    db: Session, company_id: int, car_id: int, reason: str, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    """Retire a CAR -- a duplicate, or one raised in error -- with a recorded reason.

    ``VOID`` is the CAR's tombstone (it has no ``SoftDeleteMixin``): the row stays
    listed and readable, leaves the open-CAR count, and refuses every later ``PUT``.
    A CLOSED CAR is a completed corrective action and is refused (409): voiding it would
    rewrite verified evidence. An NCR linked to a voided CAR keeps the link (history) and
    may raise a fresh CAR through ``create_car_from_ncr``.
    """
    with quality_record_transaction(db):
        car = _car_for_update_or_404(db, company_id, car_id)
        if car.status == CARStatus.VOID:
            raise HTTPException(status_code=409, detail=f"CAR {car.car_number} is already void")
        if car.status == CARStatus.CLOSED:
            raise HTTPException(
                status_code=409,
                detail=f"CAR {car.car_number} is closed; a completed corrective action cannot be voided.",
            )
        previous_status = car.status
        car.status = CARStatus.VOID
        _audit(
            audit,
            "STATUS_CHANGE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=f"Voided CAR: {reason}",
            old={"status": _value(previous_status)},
            new={"status": CARStatus.VOID.value},
            extra={"reason": reason},
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="car_updated",
            source_module="quality",
            entity_type="car",
            entity_id=car.id,
            user_id=user.id,
            severity="medium",
            event_payload={
                "car_number": car.car_number,
                "changed_fields": ["status"],
                "previous_status": _value(previous_status),
                "status": CARStatus.VOID.value,
                "priority": car.priority,
            },
        )
    return get_car(db, company_id, car_id)
