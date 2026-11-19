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
  the reopen, as their own audited save. A VOID record has no way back through ``PUT``;
  :func:`restore_ncr` / :func:`restore_car` return it to OPEN, and clear any closure
  fields it still carries for the same reason. The reopen captures no reason yet (who
  and when are on the audit row; why is not) -- a reasoned reopen is a follow-up.
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
  relinking. A CAR is retired with the reasoned :func:`void_car` (undone by
  :func:`restore_car`), after which the NCR may raise a fresh one.
* **The CAR form fields** (Werco's controlled form, migration ``122_car_form_quote_rev``)
  follow the same rules: a NEW ``part_id`` / ``customer_id`` must name a live row of the
  company and a new ``vendor_id`` one of its vendors -- inactive or removed included, since
  a supplier CAR may be the record of that removal (invariant 3) -- while an unchanged
  reference is never re-checked; the customer's name is snapshotted at selection; the
  reason ``other`` needs its text (422); the cost total is computed at read time, never
  stored. The Quality Manager approval is its own verb (:func:`approve_car_quality`):
  server-stamped, once, never by an API token, never from a request body.
* **Who decided an NCR's disposition is stamped, never sent** (AS9100D 8.7.1, migration
  ``127_ncr_car_records``). A ``PUT`` that changes ``disposition`` to anything but PENDING
  stamps ``dispositioned_by`` / ``dispositioned_at`` (the caller, now); back to PENDING
  clears both. Both ride on that request's one audit row, old -> new. A disposition change
  also clears any stored re-verification (result, notes, who, when -- kept on the audit row):
  it vouched for the previous disposition's correction, not the new one's.
* **Judgments are a person's.** A standing API token (``is_api_token_credential``) may not
  CHANGE an NCR's disposition or customer concession (``concession_*``) -- 403
  ``API_TOKEN_NCR_JUDGMENT`` before any write; re-sending the stored values with another edit
  is not a change. The three stamped verbs -- the CAR quality approval, the NCR
  re-verification and the CAR effectiveness verification -- refuse a token outright (403).
  ``concession_date`` records a past event: never later than today (Central) (422).
* **Closure needs its evidence**, judged on the MERGED record (the request's value, else the
  stored one) and refused 422 naming everything missing before any write. An NCR needs a
  non-PENDING disposition and ``action_taken``; use-as-is and repair also need a customer
  ``concession_reference``; rework and repair also need a STORED ``conforming``
  re-verification. A CAR needs root cause, corrective action, verification method and
  results; a corrective one (``car_type`` corrective, or NULL) also needs
  ``human_factors_review``, ``similar_nonconformities_review`` and a STORED ``effective``
  effectiveness verification. Preventive / improvement CARs keep the original four.
* **Re-verification and effectiveness are their own verbs, server-stamped and re-recordable**
  (:func:`record_ncr_reverification`, :func:`record_car_effectiveness`): the result and its
  notes / evidence come from the body, who and when never do, and a later recording
  overwrites the earlier one with the audit row keeping what it replaced. No ``PUT`` body
  carries ``reverification_*`` / ``effectiveness_result`` / ``*_verified_*``. Once an
  effectiveness result is recorded, the evidence it reviewed (``effectiveness_check``) is
  part of that record: a ``PUT`` that changes it is 409. ``not_effective`` returns the CAR
  from VERIFICATION to CORRECTIVE_ACTION on the same (STATUS_CHANGE) audit row.
* **A verification vouches for what it reviewed, and no more.** A ``PUT`` that changes an
  NCR's disposition clears its re-verification; one that changes a CAR's ``root_cause`` or
  ``corrective_action`` clears its effectiveness result, verifier and instant (the evidence
  text stays, unlocked, for the next recording). The cleared values ride old -> new on that
  request's audit row, and a close in the same request is judged as if already cleared.

Lock order: every row lock (``SELECT ... FOR UPDATE``) and number-generator advisory
lock is taken BEFORE the first audit row, because the audit chain lock is global.
"""

import enum
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import inspect, or_
from sqlalchemy.orm import Query, Session, selectinload

from app.core.credentials import is_api_token_credential
from app.core.time_utils import CENTRAL_TIME_ZONE, central_today, ensure_utc
from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.company import Company
from app.models.customer import Customer
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.quality import (
    CAREffectivenessResult,
    CARInitiationReason,
    CARStatus,
    CARType,
    CorrectiveActionRequest,
    NCRDisposition,
    NCRReverificationResult,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.quality import (
    CARCreate,
    CAREffectivenessRequest,
    CARResponse,
    CARUpdate,
    NCRCreate,
    NCRResponse,
    NCRReverificationRequest,
    NCRUpdate,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operational_event_service import OperationalEventService
from app.services.quality_numbering import generate_car_number as _generate_car_number
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
    "customer_id": "Customer not found",
    "vendor_id": "Vendor not found",
}

# A standing machine credential (the Werco Assistant over MCP, any bot) may not record the
# Quality Manager approval: it is a person's judgment that the corrective action is
# acceptable, and the stamp names that person. ``is_api_token_credential`` -- a crew-station
# badge is a person, but the verb is not reachable from the kiosk anyway.
API_TOKEN_QUALITY_APPROVAL = (
    "A standing API token cannot record a CAR's quality approval. A person signed in to the app must record it."
)
# The form's vendor-management and customer approval boxes are the same kind of assertion --
# that a person saw the vendor's / customer's sign-off -- so a standing API token may not
# write or change them either (on create or update; leaving them out is fine).
CAR_SIGNOFF_FIELDS = (
    "vendor_approval_name",
    "vendor_approval_title",
    "vendor_approval_date",
    "customer_approval_name",
    "customer_approval_date",
)
API_TOKEN_CAR_SIGNOFF = (
    "A standing API token cannot record or change a CAR's vendor or customer approval. "
    "A person signed in to the app must record it."
)
# Dates on the form that record something that already happened: never later than today (Central).
_CAR_RECORD_DATES = {
    "submitted_date": "Date Submitted",
    "vendor_approval_date": "The vendor approval date",
    "customer_approval_date": "The customer approval date",
}
# The same rule for the NCR's customer concession (AS9100D 8.7.1 d): it was authorized on that day.
_NCR_RECORD_DATES = {"concession_date": "The concession date"}

# An NCR's disposition and the customer concession behind a use-as-is / repair are a person's
# judgment (AS9100D 8.7.1), and the disposition stamp names that person -- so a standing API
# token may not record or change them, and may not record a re-verification of reworked /
# repaired product. ``is_api_token_credential``, as for the CAR approval above.
NCR_JUDGMENT_FIELDS = ("disposition", "concession_reference", "concession_authorized_by", "concession_date")
API_TOKEN_NCR_JUDGMENT = (
    "A standing API token cannot record an NCR's disposition or customer concession. "
    "A person signed in to the app must record it."
)
API_TOKEN_NCR_REVERIFICATION = (
    "A standing API token cannot record an NCR's re-verification. A person signed in to the app must record it."
)
# The CAR's effectiveness verification (AS9100D 10.2.1 d) is the same kind of judgment.
API_TOKEN_CAR_EFFECTIVENESS = (
    "A standing API token cannot record a CAR's effectiveness verification. "
    "A person signed in to the app must record it."
)
# Dispositions that release product only with the customer's concession (use-as-is, repair),
# and those that change the product and so must be re-verified before closure (rework, repair).
_CONCESSION_DISPOSITIONS = (NCRDisposition.USE_AS_IS, NCRDisposition.REPAIR)
_REVERIFY_DISPOSITIONS = (NCRDisposition.REWORK, NCRDisposition.REPAIR)

# Columns a request may never null out (each has a model default and readers that
# assume a value). ``quantity_rejected`` is also refused by ``NCRUpdate`` itself, and
# ``estimated_cost`` / ``actual_cost`` are non-optional on ``NCRResponse`` -- a stored
# NULL there would 500 the PUT and every later list read of the company's NCRs.
_NCR_NON_NULLABLE = (
    "status",
    "disposition",
    "car_required",
    "quantity_rejected",
    "estimated_cost",
    "actual_cost",
)
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


def _clear_closure(row: Any, old: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Clear a reopened / restored record's closure fields, recording them when set."""
    if row.closed_date is None and row.closed_by is None:
        return
    old.update(closed_date=_value(row.closed_date), closed_by=row.closed_by)
    new.update(closed_date=None, closed_by=None)
    row.closed_date = None
    row.closed_by = None


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


def _live_customer_or_404(db: Session, company_id: int, customer_id: int) -> Customer:
    """A live customer of the active company -- naming one on a CAR is a selection."""
    customer = (
        tenant_query(db, Customer, company_id)
        .filter(Customer.id == customer_id, Customer.is_deleted.is_(False))
        .first()
    )
    if customer is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["customer_id"])
    return customer


def _company_vendor_or_404(db: Session, company_id: int, vendor_id: int) -> Vendor:
    """A vendor of the active company -- deliberately NOT filtered on ``is_deleted`` / ``is_active``.

    A supplier CAR is a RECORD, not a selection (invariant 3's records-vs-selection rule):
    it may be the very document that records why a supplier was switched off or removed
    (AS9100D 8.4), so gating it on a live vendor would make that record the one nobody can
    file. Only the tenant is enforced.
    """
    vendor = tenant_query(db, Vendor, company_id).filter(Vendor.id == vendor_id).first()
    if vendor is None:
        raise HTTPException(status_code=404, detail=REFERENCE_NOT_FOUND["vendor_id"])
    return vendor


def _linkable_car_or_404(db: Session, company_id: int, car_id: int) -> CorrectiveActionRequest:
    """A non-void CAR of the company, row-locked so a concurrent ``void_car`` cannot land
    between this check and the NCR's commit. Lock order is NCR then CAR; every CAR-only
    writer (``update_car`` / ``void_car`` / ``restore_car``) takes the CAR lock alone."""
    car = (
        tenant_query(db, CorrectiveActionRequest, company_id)
        .filter(CorrectiveActionRequest.id == car_id)
        .with_for_update()
        .first()
    )
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
        # Both refusals see only CHANGED fields: re-sending the stored disposition or concession
        # alongside another edit is not a judgment, and an unchanged past date is not re-checked.
        if is_api_token_credential(user) and any(field in changes for field in NCR_JUDGMENT_FIELDS):
            raise HTTPException(status_code=403, detail=API_TOKEN_NCR_JUDGMENT)
        _check_record_dates(changes, _NCR_RECORD_DATES)
        if changes:
            _write_ncr_changes(db, company_id, ncr, changes, user, audit)
    # Read back AFTER the commit (which expires every loaded attribute) so the response's
    # ``part`` comes through the tenant-scoped loader, not an unscoped lazy load.
    return get_ncr(db, company_id, ncr_id)


def _blank(value: Any) -> bool:
    return not str(value or "").strip()


_NCR_REVERIFICATION_FIELDS = ("reverification_result", "reverification_notes", "reverified_by", "reverified_at")


def _check_ncr_closure(ncr: NonConformanceReport, changes: Dict[str, Any]) -> None:
    """422 (before any write) naming the AS9100D 8.7 evidence a closing NCR still lacks.

    Judged on the MERGED record (a value this request changes, else the stored one), so the
    evidence may be supplied in the closing request itself -- except the re-verification,
    which only its own verb records and so is always the stored result. The schema-level
    disposition / root-cause check on ``NCRUpdate`` still runs first.
    """

    def merged(field: str) -> Any:
        return changes.get(field, getattr(ncr, field))

    disposition = merged("disposition")
    missing: List[str] = []
    if disposition is None or disposition == NCRDisposition.PENDING:
        missing.append("a disposition")
    if _blank(merged("action_taken")):
        missing.append("action taken")
    if disposition in _CONCESSION_DISPOSITIONS and _blank(merged("concession_reference")):
        missing.append("a customer concession reference (use-as-is and repair)")
    # A disposition change in this same request clears the stored re-verification (see
    # ``_write_ncr_changes``), so it cannot vouch for the disposition being closed.
    reverification = None if "disposition" in changes else ncr.reverification_result
    if disposition in _REVERIFY_DISPOSITIONS and reverification != NCRReverificationResult.CONFORMING:
        missing.append("a conforming re-verification (rework and repair)")
    if missing:
        raise HTTPException(status_code=422, detail="Closure requires " + ", ".join(missing))


def _write_ncr_changes(
    db: Session,
    company_id: int,
    ncr: NonConformanceReport,
    changes: Dict[str, Any],
    user: User,
    audit: AuditService,
) -> None:
    reopening = _guard_final_record(ncr.status, changes, f"NCR {ncr.ncr_number}", NCRStatus.CLOSED, NCRStatus.VOID)
    closing = changes.get("status") == NCRStatus.CLOSED
    if closing:
        _check_ncr_closure(ncr, changes)
    if changes.get("assigned_to") is not None:
        _assignable_user_or_404(db, company_id, changes["assigned_to"])
    if changes.get("car_id") is not None:
        _linkable_car_or_404(db, company_id, changes["car_id"])

    dispositioning = "disposition" in changes
    # A re-verification vouches for the correction of ONE disposition: a rework verified
    # conforming says nothing about a later repair. Changing the disposition clears it (the
    # audit row keeps the cleared values), so the close gate needs a fresh one.
    clearing_reverification = dispositioning and any(
        getattr(ncr, field) is not None for field in _NCR_REVERIFICATION_FIELDS
    )
    side_fields: List[str] = (
        (["dispositioned_by", "dispositioned_at"] if dispositioning else [])
        + (list(_NCR_REVERIFICATION_FIELDS) if clearing_reverification else [])
        + (["closed_date", "closed_by"] if closing or reopening else [])
    )
    tracked = list(changes) + side_fields
    old = {field: _value(getattr(ncr, field)) for field in tracked}
    previous_status = ncr.status
    previous_disposition = ncr.disposition
    for field, value in changes.items():
        setattr(ncr, field, value)
    if dispositioning:
        # Who decided the disposition and when (AS9100D 8.7.1): server-stamped, never sent.
        # Back to PENDING means nobody has decided it, so the stamp clears.
        if changes["disposition"] == NCRDisposition.PENDING:
            ncr.dispositioned_by = None
            ncr.dispositioned_at = None
        else:
            ncr.dispositioned_by = user.id
            ncr.dispositioned_at = datetime.utcnow()
    if clearing_reverification:
        for field in _NCR_REVERIFICATION_FIELDS:
            setattr(ncr, field, None)
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
    """Restore a voided NCR. The pre-void status is not preserved: it reopens to OPEN.

    An NCR voided while CLOSED comes back OPEN, so -- as with a reopen -- it stops naming
    a closer: ``closed_date`` / ``closed_by`` are cleared and the RESTORE row keeps them.
    """
    with quality_record_transaction(db):
        ncr = _ncr_any_state_for_update_or_404(db, company_id, ncr_id)
        if not ncr.is_deleted:
            raise HTTPException(status_code=400, detail="NCR is not voided")
        old: Dict[str, Any] = {"is_deleted": True, "status": NCRStatus.VOID.value}
        new: Dict[str, Any] = {"is_deleted": False, "status": NCRStatus.OPEN.value}
        _clear_closure(ncr, old, new)
        ncr.restore()
        ncr.status = NCRStatus.OPEN
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


def record_ncr_reverification(
    db: Session, company_id: int, ncr_id: int, data: NCRReverificationRequest, user: User, audit: AuditService
) -> NonConformanceReport:
    """Record the re-verification of reworked or repaired product (AS9100D 8.7.1): server-stamped.

    The result and notes come from the request; who recorded it and when are stamped here and
    never taken from a body. Re-recordable: a later re-verification overwrites the earlier
    one, and the audit row keeps the values it replaced (old -> new). Applies only while the
    disposition is rework or repair (409 otherwise) and the NCR is open (a CLOSED or VOID NCR
    is 409 -- reopen it first). A standing API token is refused (403) before anything is read:
    the stamp names a person. The NCR row is locked first, so two concurrent re-verifications
    serialize; the UPDATE audit row commits with the stamp or neither does (503). A
    ``conforming`` result is what closing a rework / repair NCR requires.
    """
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_NCR_REVERIFICATION)
    with quality_record_transaction(db):
        ncr = _live_ncr_for_update_or_404(db, company_id, ncr_id)
        if ncr.status == NCRStatus.VOID:
            raise HTTPException(
                status_code=409, detail=f"NCR {ncr.ncr_number} is void; a voided record cannot be changed."
            )
        if ncr.status == NCRStatus.CLOSED:
            raise HTTPException(
                status_code=409, detail=f"NCR {ncr.ncr_number} is closed; reopen it to record a re-verification."
            )
        if ncr.disposition not in _REVERIFY_DISPOSITIONS:
            disposition = _value(ncr.disposition) or NCRDisposition.PENDING.value
            raise HTTPException(
                status_code=409,
                detail=(
                    "Re-verification applies to rework and repair dispositions; "
                    f"NCR {ncr.ncr_number} is dispositioned {disposition}."
                ),
            )
        tracked = _NCR_REVERIFICATION_FIELDS
        old = {field: _value(getattr(ncr, field)) for field in tracked}
        ncr.reverification_result = data.result.value
        ncr.reverification_notes = data.notes
        ncr.reverified_by = user.id
        ncr.reverified_at = datetime.utcnow()
        new = {field: _value(getattr(ncr, field)) for field in tracked}
        _audit(
            audit,
            "UPDATE",
            NCR_RESOURCE,
            ncr,
            ncr.ncr_number,
            description=f"Recorded re-verification of NCR {ncr.ncr_number}: {data.result.value}",
            old=old,
            new=new,
            extra={
                "changes": _diff(old, new),
                "reverification": True,
                "disposition": _value(ncr.disposition),
                "status": _value(ncr.status),
            },
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="ncr_updated",
            source_module="quality",
            entity_type="ncr",
            entity_id=ncr.id,
            work_order_id=ncr.work_order_id,
            user_id=user.id,
            severity="info",
            event_payload={
                "ncr_number": ncr.ncr_number,
                "changed_fields": sorted(tracked),
                "previous_status": _value(ncr.status),
                "status": _value(ncr.status),
                "previous_disposition": _value(ncr.disposition),
                "disposition": _value(ncr.disposition),
            },
        )
    return get_ncr(db, company_id, ncr_id)


# --------------------------------------------------------------------------- CAR writes


def generate_car_number(db: Session, company_id: Optional[int] = None) -> str:
    # One numbering rule for every CAR writer (this service, the complaint escalation verb):
    # ``app.services.quality_numbering``. The lock is handed in from this module so the
    # generator lock stays spy-able here.
    return _generate_car_number(db, company_id, lock=acquire_generator_lock)


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


def _normalize_car_values(values: Dict[str, Any]) -> None:
    """Store the reason as its plain string value (the column is a String + CHECK, not an Enum)."""
    reason = values.get("initiation_reason")
    if isinstance(reason, enum.Enum):
        values["initiation_reason"] = reason.value


def _refuse_token_signoff(user: User, values: Dict[str, Any], *, changes: bool) -> None:
    """403 (before any write) when an API token writes a vendor / customer sign-off box.

    ``changes=False``: ``values`` is a create body, and a box left blank (absent, null or
    whitespace) sets nothing. ``changes=True``: ``values`` holds only the CHANGED fields of an
    update, so any sign-off key in it -- clearing one included -- is a change, while re-sending
    the stored value never reaches here.
    """
    if not is_api_token_credential(user):
        return
    for field in CAR_SIGNOFF_FIELDS:
        if field not in values:
            continue
        value = values[field]
        if changes or (value is not None and not (isinstance(value, str) and not value.strip())):
            raise HTTPException(status_code=403, detail=API_TOKEN_CAR_SIGNOFF)


def _check_record_dates(values: Dict[str, Any], labels: Optional[Dict[str, str]] = None) -> None:
    """422 (before any write) when a date recording a past event is after today (Central)."""
    today = central_today()
    for field, label in (_CAR_RECORD_DATES if labels is None else labels).items():
        value = values.get(field)
        if value is not None and value > today:
            raise HTTPException(status_code=422, detail=f"{label} cannot be in the future.")


def _check_initiation_reason(reason: Optional[str], other_text: Optional[str]) -> None:
    """The form's "Other ____" box needs its blank filled in (422 before anything is written)."""
    if reason == CARInitiationReason.OTHER.value and not str(other_text or "").strip():
        raise HTTPException(
            status_code=422,
            detail="initiation_reason_other is required when the reason for initiation is 'other'",
        )


def _resolve_car_references(db: Session, company_id: int, values: Dict[str, Any]) -> Optional[Customer]:
    """Resolve every NEW form reference in ``values`` tenant-scoped (404 before any write).

    Only the keys present with a non-null value are checked -- on update ``values`` is the
    CHANGED fields only, so an unchanged reference (a record of what the CAR already names,
    possibly since retired) is never re-checked. A new part or customer must be live; a
    vendor need only be the company's (see :func:`_company_vendor_or_404`). Returns the
    resolved customer, so its name can be snapshotted.
    """
    if values.get("part_id") is not None:
        _live_part_or_404(db, company_id, values["part_id"])
    if values.get("vendor_id") is not None:
        _company_vendor_or_404(db, company_id, values["vendor_id"])
    if values.get("customer_id") is not None:
        return _live_customer_or_404(db, company_id, values["customer_id"])
    return None


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
        # Prefill from the NCR (the form's "Part number" and vendor "P.O number"). The NCR's
        # part is a RECORD the CAR inherits, not a new selection, so a since-retired part is
        # still carried; a legacy foreign part id (pre-validation rows) is not -- reads already
        # treat it as no part (``ncr_query``), and it must not cross into a new record.
        part_id = None
        if ncr.part_id is not None:
            owned = tenant_query(db, Part, company_id).filter(Part.id == ncr.part_id).with_entities(Part.id).first()
            part_id = ncr.part_id if owned is not None else None
        car = CorrectiveActionRequest(
            car_number=generate_car_number(db, company_id),
            title=f"CAR for {ncr.ncr_number}: {ncr.title}"[:255],
            problem_description=ncr.description,
            initiated_by=user.id,
            submitted_date=central_today(),
            part_id=part_id,
            vendor_po_number=(ncr.po_number or None),
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
    values = data.model_dump()
    _refuse_token_signoff(user, values, changes=False)
    _normalize_car_values(values)
    _check_initiation_reason(values.get("initiation_reason"), values.get("initiation_reason_other"))
    _check_record_dates(values)
    with quality_record_transaction(db):
        # Every reference is resolved before the number generator's lock and the first write.
        customer = _resolve_car_references(db, company_id, values)
        values["customer_name"] = customer.name if customer is not None else None
        if values.get("submitted_date") is None:
            values["submitted_date"] = central_today()
        car = CorrectiveActionRequest(car_number=generate_car_number(db, company_id), **values, initiated_by=user.id)
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
        _normalize_car_values(requested)
        changes = _changed_fields(car, requested)
        _refuse_token_signoff(user, changes, changes=True)
        _check_record_dates(changes)
        if changes:
            _write_car_changes(db, company_id, car, requested, changes, user, audit)
    return get_car(db, company_id, car_id)


# What a CAR's effectiveness verification reviewed: the cause it found and the action taken
# against it. Changing either clears the recorded result (and its verifier and instant), so a
# closed CAR can never name someone as having found effective an action they never reviewed.
_CAR_EFFECTIVENESS_REVIEWED_FIELDS = ("root_cause", "corrective_action")
_CAR_EFFECTIVENESS_RESULT_FIELDS = ("effectiveness_result", "effectiveness_verified_by", "effectiveness_verified_at")


def _check_car_closure(
    car: CorrectiveActionRequest, requested: Dict[str, Any], *, clearing_effectiveness: bool = False
) -> None:
    """422 (before any write) naming the evidence a closing CAR still lacks.

    Judged on the MERGED record (the requested value, else the stored one). A corrective CAR
    -- ``car_type`` corrective, or NULL, which has always meant corrective -- also needs the
    AS9100D 10.2.1 reviews (human factors, similar nonconformities) and a recorded
    ``effective`` effectiveness verification (10.2.1 d), which only its own verb writes and so
    is always the stored result. Preventive and improvement CARs keep the original four.
    """

    def blank(field: str) -> bool:
        return _blank(requested.get(field, getattr(car, field)))

    missing = [
        field.replace("_", " ")
        for field in ("root_cause", "corrective_action", "verification_method", "verification_results")
        if blank(field)
    ]
    if car.car_type is None or car.car_type == CARType.CORRECTIVE:
        if blank("human_factors_review"):
            missing.append("human factors review")
        if blank("similar_nonconformities_review"):
            missing.append("similar nonconformities review")
        result = None if clearing_effectiveness else car.effectiveness_result
        if result != CAREffectivenessResult.EFFECTIVE:
            missing.append("an effective effectiveness verification")
    if missing:
        raise HTTPException(status_code=422, detail="Closure requires " + ", ".join(missing))


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
    clearing_effectiveness = any(field in changes for field in _CAR_EFFECTIVENESS_REVIEWED_FIELDS) and any(
        getattr(car, field) is not None for field in _CAR_EFFECTIVENESS_RESULT_FIELDS
    )
    if "effectiveness_check" in changes and car.effectiveness_result is not None and not clearing_effectiveness:
        # The evidence a recorded verification reviewed is part of that record: it changes only
        # by recording a new verification, which re-stamps who reviewed it and when.
        raise HTTPException(
            status_code=409,
            detail=(
                f"CAR {car.car_number} has a recorded effectiveness verification; record a new one with the "
                "effectiveness action to change its evidence."
            ),
        )
    closing = changes.get("status") == CARStatus.CLOSED
    verifying = changes.get("status") == CARStatus.VERIFICATION
    if closing:
        _check_car_closure(car, requested, clearing_effectiveness=clearing_effectiveness)
    if changes.get("assigned_to") is not None:
        _assignable_user_or_404(db, company_id, changes["assigned_to"])
    if (
        "initiation_reason" in changes
        and changes["initiation_reason"] != CARInitiationReason.OTHER.value
        and "initiation_reason_other" not in requested
        and car.initiation_reason_other is not None
    ):
        # Moving the reason off "Other" clears its "____" text (recorded old -> None), so the
        # record and the print never carry an orphaned Other text beside another box.
        changes["initiation_reason_other"] = None
    if "initiation_reason" in changes or "initiation_reason_other" in changes:
        _check_initiation_reason(
            changes.get("initiation_reason", car.initiation_reason),
            changes.get("initiation_reason_other", car.initiation_reason_other),
        )
    # Only CHANGED references are resolved: an unchanged one is a record, not a new selection.
    customer = _resolve_car_references(db, company_id, changes)
    snapshot_customer = "customer_id" in changes

    # Independent side effects: a CLOSED CAR reopened straight into VERIFICATION both
    # records its verifier AND stops naming a closer. (closing and verifying cannot both
    # hold; closing and reopening cannot either -- a reopen leaves CLOSED.) A new customer
    # re-snapshots its name; clearing the customer clears the name with it.
    side_fields: List[str] = (
        (["verified_by"] if verifying else [])
        + (["closed_date", "closed_by"] if closing or reopening else [])
        + (["customer_name"] if snapshot_customer else [])
        + (list(_CAR_EFFECTIVENESS_RESULT_FIELDS) if clearing_effectiveness else [])
    )
    tracked = list(changes) + side_fields
    old = {field: _value(getattr(car, field)) for field in tracked}
    previous_status = car.status
    for field, value in changes.items():
        setattr(car, field, value)
    if snapshot_customer:
        car.customer_name = customer.name if customer is not None else None
    if verifying:
        car.verified_by = user.id
    if clearing_effectiveness:
        for field in _CAR_EFFECTIVENESS_RESULT_FIELDS:
            setattr(car, field, None)
    if closing:
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
    listed and readable, leaves the open-CAR count, and refuses every later ``PUT``;
    :func:`restore_car` undoes it. A CLOSED CAR is refused (409) so a completed corrective
    action is never voided in one step -- but this is a speed bump, not a control: the
    same roles can reopen it with a status-only ``PUT`` (no reason captured yet) and then
    void it. Both steps are required-audited, so the reopen is on the record either way.
    An NCR linked to a voided CAR keeps the link (history) and may raise a fresh CAR
    through ``create_car_from_ncr``.
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


def restore_car(db: Session, company_id: int, car_id: int, user: User, audit: AuditService) -> CorrectiveActionRequest:
    """Undo a CAR void. The pre-void status is not preserved: it reopens to OPEN.

    The NCR restore posture (a safe reset; the void's audit row still says what the
    status was). Closure fields a legacy VOID row may carry -- a pre-validation ``PUT``
    could move a CLOSED CAR straight to void -- are cleared and recorded, since an OPEN
    CAR names no closer. An NCR that raised a fresh CAR after the void keeps that link;
    the restored CAR is not relinked.
    """
    with quality_record_transaction(db):
        car = _car_for_update_or_404(db, company_id, car_id)
        if car.status != CARStatus.VOID:
            raise HTTPException(status_code=409, detail=f"CAR {car.car_number} is not void")
        old: Dict[str, Any] = {"status": CARStatus.VOID.value}
        new: Dict[str, Any] = {"status": CARStatus.OPEN.value}
        _clear_closure(car, old, new)
        car.status = CARStatus.OPEN
        _audit(
            audit,
            "RESTORE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=f"Restored CAR {car.car_number}",
            old=old,
            new=new,
            extra={"changes": _diff(old, new)},
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
                "changed_fields": sorted(new),
                "previous_status": CARStatus.VOID.value,
                "status": CARStatus.OPEN.value,
                "priority": car.priority,
            },
        )
    return get_car(db, company_id, car_id)


def approve_car_quality(
    db: Session, company_id: int, car_id: int, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    """Record the form's "Quality Manager Approval": server-stamped approver and instant.

    Once per CAR: an already-approved CAR is refused (409) rather than re-stamped, so the
    record keeps naming who approved it first; a VOID CAR is refused (409). Allowed in every
    other status, CLOSED included -- the approval does not gate closure (the existing
    closure evidence rules are unchanged) and does not freeze the CAR's other fields. A
    standing API token is refused (403): the stamp names a person. The CAR row is locked
    first so two concurrent approvals serialize and the second sees the first's stamp; the
    APPROVE audit row commits with the stamp or neither does (503).
    """
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_QUALITY_APPROVAL)
    with quality_record_transaction(db):
        car = _car_for_update_or_404(db, company_id, car_id)
        if car.status == CARStatus.VOID:
            raise HTTPException(
                status_code=409, detail=f"CAR {car.car_number} is void; a voided CAR cannot be approved."
            )
        if car.quality_approved_at is not None or car.quality_approved_by is not None:
            raise HTTPException(
                status_code=409,
                detail=f"CAR {car.car_number} already has its quality approval; it is recorded once.",
            )
        old: Dict[str, Any] = {"quality_approved_by": None, "quality_approved_at": None}
        car.quality_approved_by = user.id
        car.quality_approved_at = datetime.utcnow()
        new: Dict[str, Any] = {
            "quality_approved_by": car.quality_approved_by,
            "quality_approved_at": _value(car.quality_approved_at),
        }
        _audit(
            audit,
            "APPROVE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=f"Recorded quality manager approval of CAR {car.car_number}",
            old=old,
            new=new,
            extra={"changes": _diff(old, new), "quality_approval": True, "status": _value(car.status)},
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="car_updated",
            source_module="quality",
            entity_type="car",
            entity_id=car.id,
            user_id=user.id,
            severity="info",
            event_payload={
                "car_number": car.car_number,
                "changed_fields": ["quality_approved_at", "quality_approved_by"],
                "previous_status": _value(car.status),
                "status": _value(car.status),
                "priority": car.priority,
            },
        )
    return get_car(db, company_id, car_id)


def record_car_effectiveness(
    db: Session, company_id: int, car_id: int, data: CAREffectivenessRequest, user: User, audit: AuditService
) -> CorrectiveActionRequest:
    """Record the review of a corrective action's effectiveness (AS9100D 10.2.1 d): server-stamped.

    Only while the CAR is in VERIFICATION (409 otherwise, VOID and CLOSED included). Writes the
    evidence reviewed (``effectiveness_check``) and the result; who recorded it and when are
    stamped here, never taken from a body. Re-recordable: a later verification overwrites the
    earlier one and the audit row keeps what it replaced. ``not_effective`` also returns the
    CAR to CORRECTIVE_ACTION (a STATUS_CHANGE row) -- the action did not work, so the work is
    not done; ``effective`` leaves the status alone and is what closing a corrective CAR
    requires. A standing API token is refused (403) before anything is read: the stamp names a
    person. The CAR row is locked first; the audit row commits with the stamp or neither does
    (503).
    """
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_CAR_EFFECTIVENESS)
    with quality_record_transaction(db):
        car = _car_for_update_or_404(db, company_id, car_id)
        if car.status != CARStatus.VERIFICATION:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Record effectiveness once CAR {car.car_number} is in Verification; "
                    f"it is {_value(car.status)}."
                ),
            )
        not_effective = data.result == CAREffectivenessResult.NOT_EFFECTIVE
        tracked = (
            "effectiveness_check",
            "effectiveness_result",
            "effectiveness_verified_by",
            "effectiveness_verified_at",
        ) + (("status",) if not_effective else ())
        old = {field: _value(getattr(car, field)) for field in tracked}
        previous_status = car.status
        car.effectiveness_check = data.evidence
        car.effectiveness_result = data.result.value
        car.effectiveness_verified_by = user.id
        car.effectiveness_verified_at = datetime.utcnow()
        if not_effective:
            car.status = CARStatus.CORRECTIVE_ACTION
        new = {field: _value(getattr(car, field)) for field in tracked}
        _audit(
            audit,
            "STATUS_CHANGE" if not_effective else "UPDATE",
            CAR_RESOURCE,
            car,
            car.car_number,
            description=(
                f"Effectiveness verification of CAR {car.car_number} found it not effective; returned it from "
                "'verification' to 'corrective_action'"
                if not_effective
                else f"Recorded effectiveness verification of CAR {car.car_number}: effective"
            ),
            old=old,
            new=new,
            extra={"changes": _diff(old, new), "effectiveness_verification": True},
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="car_updated",
            source_module="quality",
            entity_type="car",
            entity_id=car.id,
            user_id=user.id,
            severity="medium" if not_effective else "info",
            event_payload={
                "car_number": car.car_number,
                "changed_fields": sorted(tracked),
                "previous_status": _value(previous_status),
                "status": _value(car.status),
                "priority": car.priority,
            },
        )
    return get_car(db, company_id, car_id)


def car_reference_count(
    db: Session, company_id: int, *, part_id: Optional[int] = None, customer_id: Optional[int] = None
) -> int:
    """How many of the company's CARs name this part or customer (any status, VOID included).

    The physical-delete probe for ``DELETE /parts|materials|customers/{id}?hard_delete=true``:
    a CAR holds a real foreign key (``fk_cars_part_id`` / ``fk_cars_customer_id``) to what it
    names, so deleting that row would fail at COMMIT on PostgreSQL (a 500) -- and a CAR is a
    quality record that must keep naming it. Tenant-scoped: every CAR writer resolves the part
    and customer within the company, so no other company's CAR can reference them.
    """
    if part_id is None and customer_id is None:
        return 0
    query = tenant_query(db, CorrectiveActionRequest, company_id)
    if part_id is not None:
        query = query.filter(CorrectiveActionRequest.part_id == part_id)
    if customer_id is not None:
        query = query.filter(CorrectiveActionRequest.customer_id == customer_id)
    return query.count()


def car_reference_refusal(count: int, label: str) -> Optional[str]:
    """The 400 detail a hard delete answers when CARs name the record, else ``None``."""
    if count <= 0:
        return None
    noun = "corrective action report" if count == 1 else "corrective action reports"
    return f"Cannot hard delete: {label} is named on {count} {noun}; soft delete it instead"


# ------------------------------------------------------------------------- CAR reads

# Users a CAR names. ``assigned_to`` is a client SELECTION, validated tenant-scoped at write
# time, so it resolves tenant-scoped only. The other four are server-stamped ATTRIBUTION
# facts (the caller at create / verification / closure / approval); a platform admin acting
# in this company's context is not one of its users, so those fall back to a lookup by id
# restricted to platform principals (superuser / PLATFORM_ADMIN, as ``fai_responses``) rather
# than printing a blank approver -- never an ordinary user of another company.
_CAR_SELECTED_USER_FIELDS = ("assigned_to",)
_CAR_STAMPED_USER_FIELDS = (
    "initiated_by",
    "verified_by",
    "closed_by",
    "quality_approved_by",
    "effectiveness_verified_by",
)
_CAR_COST_FIELDS = ("cost_labor", "cost_material", "cost_outside_services", "cost_shipping")


def car_cost_total(car: Any) -> Optional[Decimal]:
    """The form's cost total, computed at read time (never stored): ``None`` when nothing is entered."""
    entered = [Decimal(str(getattr(car, field))) for field in _CAR_COST_FIELDS if getattr(car, field) is not None]
    return sum(entered, Decimal("0")) if entered else None


def car_responses(db: Session, company_id: int, cars: Sequence[CorrectiveActionRequest]) -> List[CARResponse]:
    """``CARResponse`` for each CAR, with display names resolved in four batched, tenant-scoped reads.

    Part and vendor names are read WITHOUT an ``is_deleted`` filter on purpose: a CAR is a
    historical record and keeps naming the part / supplier it was raised against after
    either is retired (invariant 3's records exception). The tenant is always enforced, so a
    legacy row carrying another company's id shows no name rather than theirs. The customer
    name is the snapshot stored on the CAR.
    """
    cars = list(cars)
    if not cars:
        return []

    def ids(fields: Iterable[str]) -> set:
        return {getattr(car, field) for car in cars for field in fields if getattr(car, field) is not None}

    user_name = _user_namer(db, company_id, cars, _CAR_SELECTED_USER_FIELDS, _CAR_STAMPED_USER_FIELDS)
    part_ids, vendor_ids = ids(("part_id",)), ids(("vendor_id",))
    parts = {p.id: p for p in tenant_query(db, Part, company_id).filter(Part.id.in_(part_ids))} if part_ids else {}
    vendors = (
        {v.id: v for v in tenant_query(db, Vendor, company_id).filter(Vendor.id.in_(vendor_ids))} if vendor_ids else {}
    )

    responses = []
    for car in cars:
        values = {column.key: getattr(car, column.key) for column in inspect(type(car)).columns}
        part = parts.get(car.part_id)
        vendor = vendors.get(car.vendor_id)
        values.update(
            {f"{field}_name": user_name(car, field) for field in _CAR_SELECTED_USER_FIELDS + _CAR_STAMPED_USER_FIELDS},
            part_number=part.part_number if part is not None else None,
            part_name=part.name if part is not None else None,
            part_current_revision=part.revision if part is not None else None,
            vendor_name=vendor.name if vendor is not None else None,
            cost_total=car_cost_total(car),
        )
        responses.append(CARResponse.model_validate(values))
    return responses


def car_response(db: Session, company_id: int, car: CorrectiveActionRequest) -> CARResponse:
    return car_responses(db, company_id, [car])[0]


# ------------------------------------------------------------------------- NCR responses

# Users an NCR names on its response. Both are server-stamped ATTRIBUTION facts (who decided
# the disposition; who recorded the re-verification), resolved exactly like the CAR's stamped
# fields: tenant-scoped, else a platform principal acting in this company's context.
_NCR_STAMPED_USER_FIELDS = ("dispositioned_by", "reverified_by")


def ncr_responses(db: Session, company_id: int, ncrs: Sequence[NonConformanceReport]) -> List[NCRResponse]:
    """``NCRResponse`` for each NCR (loaded through :func:`ncr_query`), with stamped user names
    resolved in at most two batched reads -- never an ordinary user of another company."""
    ncrs = list(ncrs)
    if not ncrs:
        return []
    user_name = _user_namer(db, company_id, ncrs, (), _NCR_STAMPED_USER_FIELDS)
    return [
        NCRResponse.model_validate(ncr).model_copy(
            update={f"{field}_name": user_name(ncr, field) for field in _NCR_STAMPED_USER_FIELDS}
        )
        for ncr in ncrs
    ]


def ncr_response(db: Session, company_id: int, ncr: NonConformanceReport) -> NCRResponse:
    return ncr_responses(db, company_id, [ncr])[0]


def _user_namer(
    db: Session,
    company_id: int,
    rows: Sequence[Any],
    selected_fields: Sequence[str],
    stamped_fields: Sequence[str],
) -> Callable[[Any, str], Optional[str]]:
    """A ``(row, field) -> full name | None`` resolver over every user id ``rows`` name.

    A SELECTED field (a client's choice, validated tenant-scoped at write time) resolves only
    to a user of the company. A STAMPED field (the server's record of who acted) falls back to
    a lookup by id restricted to platform principals (superuser / ``PLATFORM_ADMIN``, as
    ``fai_responses`` does), so a platform admin acting in this company's context is still
    named -- but an ordinary user of another company (a legacy or imported row, a moved
    account) never is.
    """

    def ids(fields: Iterable[str]) -> set:
        return {getattr(row, field) for row in rows for field in fields if getattr(row, field) is not None}

    user_ids = ids(tuple(selected_fields) + tuple(stamped_fields))
    users = {u.id: u for u in tenant_query(db, User, company_id).filter(User.id.in_(user_ids))} if user_ids else {}
    stamped_elsewhere = ids(stamped_fields) - set(users)
    attributed = (
        {
            u.id: u
            for u in db.query(User).filter(
                User.id.in_(stamped_elsewhere),
                or_(User.is_superuser.is_(True), User.role == UserRole.PLATFORM_ADMIN),
            )
        }
        if stamped_elsewhere
        else {}
    )

    def user_name(row: Any, field: str) -> Optional[str]:
        user_id = getattr(row, field)
        found = users.get(user_id) or (attributed.get(user_id) if field in stamped_fields else None)
        return found.full_name if found is not None else None

    return user_name


# ------------------------------------------------------------------------ printable CAR


def _form_date(value: Optional[date]) -> Optional[str]:
    return value.strftime("%m/%d/%Y") if value else None


def _form_money(value: Optional[Decimal]) -> Optional[str]:
    return f"{Decimal(str(value)):,.2f}" if value is not None else None


def _central_stamp_date(value: Optional[datetime]) -> Optional[str]:
    """The Central-calendar date of a stored UTC stamp, in the form's date format."""
    stamped = ensure_utc(value)
    return _form_date(stamped.astimezone(CENTRAL_TIME_ZONE).date()) if stamped is not None else None


_EFFECTIVENESS_LABELS = {
    CAREffectivenessResult.EFFECTIVE: "Effective",
    CAREffectivenessResult.NOT_EFFECTIVE: "Not effective",
}


def car_print_context(db: Session, company_id: int, car: CorrectiveActionRequest) -> Dict[str, Any]:
    """Every value the printable CAR shows, resolved tenant-scoped and formatted. Writes nothing.

    The header is the active company's own record (name / address / phone) -- never a
    hard-coded block -- and the Quality Manager approval and effectiveness verification dates
    are the Central-calendar dates of their UTC stamps (store UTC, display Central).
    """
    view = car_response(db, company_id, car)
    company = db.query(Company).filter(Company.id == company_id).first()
    return dict(
        car_number=view.car_number,
        company_name=company.name if company is not None else "",
        company_address=company.address if company is not None else None,
        company_phone=company.phone if company is not None else None,
        status_label=view.status.value.replace("_", " ").title(),
        initiated_by=view.initiated_by_name,
        rma_number=view.rma_number,
        submitted_date=_form_date(view.submitted_date),
        due_date=_form_date(view.due_date),
        part_number=view.part_number,
        vendor_po_number=view.vendor_po_number,
        vendor_name=view.vendor_name,
        customer_name=view.customer_name,
        initiation_reason=view.initiation_reason.value if view.initiation_reason else None,
        initiation_reason_other=view.initiation_reason_other,
        problem_sections=[(None, view.title), (None, view.problem_description)],
        root_cause_sections=[(None, view.root_cause), ("Root cause analysis", view.root_cause_analysis)],
        corrective_action_sections=[
            (None, view.corrective_action),
            ("Containment action", view.containment_action),
            ("Preventive action", view.preventive_action),
        ],
        cost_labor=_form_money(view.cost_labor),
        cost_material=_form_money(view.cost_material),
        cost_outside_services=_form_money(view.cost_outside_services),
        cost_shipping=_form_money(view.cost_shipping),
        cost_total=_form_money(view.cost_total),
        vendor_approval_name=view.vendor_approval_name,
        vendor_approval_title=view.vendor_approval_title,
        vendor_approval_date=_form_date(view.vendor_approval_date),
        management_action=view.management_action,
        customer_approval_name=view.customer_approval_name,
        customer_approval_date=_form_date(view.customer_approval_date),
        quality_approved_by=view.quality_approved_by_name,
        quality_approved_date=_central_stamp_date(view.quality_approved_at),
        human_factors_review=view.human_factors_review,
        similar_nonconformities_review=view.similar_nonconformities_review,
        effectiveness_result=(
            _EFFECTIVENESS_LABELS[view.effectiveness_result] if view.effectiveness_result is not None else None
        ),
        effectiveness_evidence=view.effectiveness_check,
        effectiveness_verified_by=view.effectiveness_verified_by_name,
        effectiveness_verified_date=_central_stamp_date(view.effectiveness_verified_at),
    )


def render_car_pdf(db: Session, company_id: int, car_id: int) -> tuple:
    """``(car_number, pdf_bytes)`` for one CAR of the active company (404 otherwise)."""
    from app.services.car_pdf_service import build_corrective_action_report_pdf

    car = get_car(db, company_id, car_id)
    context = car_print_context(db, company_id, car)
    return car.car_number, build_corrective_action_report_pdf(**context)
