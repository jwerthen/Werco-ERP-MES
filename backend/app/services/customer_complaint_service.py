"""Tenant-scoped customer complaint and RMA workflow (AS9100D 8.2.1, 8.7, 10.2).

The router in ``api/endpoints/customer_complaints.py`` stays thin; every lookup,
reference check, state transition and audit row lives here. The contracts, kept in
one place because they were all missing together before:

* **Tenancy.** Every lookup is scoped to the ACTIVE company (the router passes
  ``get_current_company_id``) through ``tenant_query``. A foreign or missing id is
  404, with no hint that another tenant's record exists. Neither model is
  soft-deletable (``TenantMixin`` only, and there is no delete verb), so there is no
  tombstone filter to add. Relationship loads (``part`` / ``customer`` / ``rmas``)
  are scoped too: rows written before references were validated may point across
  tenants, and an owned parent must not expose another tenant's part number,
  customer name or RMA through them (the ``fai_service.fai_query`` pattern).
* **References.** A NEW reference is a selection, so the customer, part and work order
  must be live (not soft-deleted) rows of the active company, the assignee an active
  user of it, and an RMA's complaint a complaint of it. Each failure is a 422 raised
  before the first mutation. An UNCHANGED reference on update is a record and is not
  re-checked, so retiring a part never strands the complaints that name it. An RMA's
  ``customer_id`` / ``part_id`` equal to the ones its complaint already names is a
  record COPIED from that complaint (the page sends exactly that), so it is checked
  for ownership only. A complaint's own customer / part / work order stay selections
  (chosen from the live pickers): a complaint about an already-retired item is
  recorded without the link and names it in the text.
* **Status.** Status moves only through the workflow verbs, each with a from-state
  guard (400, as the RMA verbs always answered). ``ComplaintUpdate`` carries no
  ``status`` and ``RMAUpdate`` no ``disposition``.
* **PUT cannot do a verb's job.** The update schemas carry no field a workflow verb
  owns: none of investigate's or resolve's fields on a complaint, and no
  ``credit_amount`` (dispose) on an RMA. Otherwise an author-tier caller could rewrite
  an approver's decision, or write findings outside the verb's from-state guard. The
  RMA fields a later decision rests on are editable only in their own window
  (``_RMA_FIELD_WINDOWS``): ``quantity`` / ``reason`` while REQUESTED (approve
  authorizes them), ``inspection_findings`` while UNDER_INSPECTION (dispose decides
  on them). ``replacement_wo_id`` links only on an RMA dispositioned ``replace``. A
  CLOSED / VOID complaint and a COMPLETED / DENIED RMA are closed quality records, so
  PUT refuses them outright (the FAI rule: final status makes the evidence immutable,
  invariant 5). Every PUT refusal is a 409 raised before the first ``setattr``.
* **Audit.** Every write records through ``AuditService``. The decisions and the
  quality records they raise use ``log_required``: resolve, close, RMA approve / deny /
  dispose / complete, the NCR or CAR a complaint raises, and the complaint's link to
  it. Their evidence commits with the change or nothing commits (503 via
  ``complaint_transaction``). The other writes (create, PUT, investigate, receive,
  inspect) keep best-effort semantics.
* **Roles.** ``AUTHOR_ROLES`` (the ``quality:inspect`` tier) record and investigate.
  ``APPROVER_ROLES`` (the ``quality:approve`` tier) decide: they resolve, close,
  raise NCRs/CARs, and approve, deny, dispose and complete RMAs. Reads are
  read-broad within the tenant, like every other operational module.
"""

import enum
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from fastapi import HTTPException
from sqlalchemy import extract, func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Query, Session, selectinload

from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.customer import Customer
from app.models.customer_complaint import (
    ComplaintStatus,
    CustomerComplaint,
    ReturnMaterialAuthorization,
    RMAStatus,
)
from app.models.part import Part
from app.models.quality import CARStatus, CorrectiveActionRequest, NCRSource, NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operational_event_service import OperationalEventService
from app.services.quality_numbering import generate_car_number, generate_ncr_number

# Record and investigate a complaint; create, edit, receive and inspect an RMA.
# This is the quality:inspect tier (docs/RBAC_PERMISSIONS.md -> Quality -> Inspect).
AUTHOR_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]
# Decide: resolve and close a complaint, raise an NCR or CAR from it, and approve,
# deny, dispose and complete an RMA. This is the quality:approve tier.
APPROVER_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]

COMPLAINT_RESOURCE = "customer_complaint"
RMA_RESOURCE = "rma"

OPEN_COMPLAINT_STATUSES = (
    ComplaintStatus.RECEIVED,
    ComplaintStatus.UNDER_INVESTIGATION,
    ComplaintStatus.PENDING_RESOLUTION,
)
RESOLVABLE_COMPLAINT_STATUSES = (ComplaintStatus.UNDER_INVESTIGATION, ComplaintStatus.PENDING_RESOLUTION)
OPEN_RMA_STATUSES = (
    RMAStatus.REQUESTED,
    RMAStatus.APPROVED,
    RMAStatus.MATERIAL_RECEIVED,
    RMAStatus.UNDER_INSPECTION,
    RMAStatus.DISPOSITION_DECIDED,
)
VALID_DISPOSITIONS = ("replace", "repair", "credit", "scrap", "return_to_customer")

# Closed quality records: PUT refuses them outright (409). No verb leaves these states.
FROZEN_COMPLAINT_STATUSES = (ComplaintStatus.CLOSED, ComplaintStatus.VOID)
FROZEN_RMA_STATUSES = (RMAStatus.COMPLETED, RMAStatus.DENIED)

# RMA fields a later decision rests on -> the only statuses PUT may change them in.
# quantity / reason are what approve authorizes; inspection_findings are what dispose
# decides on (inspect writes them first, from MATERIAL_RECEIVED).
_RMA_FIELD_WINDOWS: Dict[str, tuple] = {
    "quantity": (RMAStatus.REQUESTED,),
    "reason": (RMAStatus.REQUESTED,),
    "inspection_findings": (RMAStatus.UNDER_INSPECTION,),
}
# An RMA references these through its complaint too; a matching value is copied, not chosen.
_COMPLAINT_COPIED_REFERENCES = ("customer_id", "part_id")

# NOT NULL columns, or response fields typed non-optional: an explicit null on update
# would otherwise fail at INSERT/serialization time as a 500.
_COMPLAINT_REQUIRED_FIELDS = frozenset(
    {"customer_name", "title", "description", "quantity_affected", "severity", "estimated_cost"}
)
_RMA_REQUIRED_FIELDS = frozenset({"quantity", "reason"})

# Body field -> (model, label) for references a caller can SELECT.
_REFERENCES: Dict[str, tuple] = {
    "customer_id": (Customer, "customer"),
    "part_id": (Part, "part"),
    "work_order_id": (WorkOrder, "work order"),
    "replacement_wo_id": (WorkOrder, "work order"),
    "assigned_to": (User, "user"),
    "complaint_id": (CustomerComplaint, "complaint"),
}

_SNAPSHOT_SKIP = frozenset({"created_at", "updated_at"})
_TITLE_MAX = 255  # NonConformanceReport.title / CorrectiveActionRequest.title are String(255)


@contextmanager
def complaint_transaction(db: Session) -> Iterator[None]:
    """Commit on success, roll back on any error; a lost required audit row is a 503."""
    try:
        with atomic_transaction(db):
            yield
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail="Unable to save audit record. No changes were committed.") from exc


# ============== Lookups ==============


def complaint_query(db: Session, company_id: int) -> Query:
    """Active-company complaints with relationship loads scoped to the same company.

    Historical part/customer labels stay readable after those rows are soft-deleted.
    """
    return (
        tenant_query(db, CustomerComplaint, company_id)
        .options(
            selectinload(CustomerComplaint.part.and_(Part.company_id == company_id)),
            selectinload(CustomerComplaint.customer.and_(Customer.company_id == company_id)),
            selectinload(CustomerComplaint.rmas.and_(ReturnMaterialAuthorization.company_id == company_id)),
        )
        .populate_existing()
    )


def rma_query(db: Session, company_id: int) -> Query:
    """Active-company RMAs with relationship loads scoped to the same company."""
    return (
        tenant_query(db, ReturnMaterialAuthorization, company_id)
        .options(
            selectinload(ReturnMaterialAuthorization.part.and_(Part.company_id == company_id)),
            selectinload(ReturnMaterialAuthorization.customer.and_(Customer.company_id == company_id)),
        )
        .populate_existing()
    )


def get_complaint_or_404(db: Session, company_id: int, complaint_id: int) -> CustomerComplaint:
    complaint = complaint_query(db, company_id).filter(CustomerComplaint.id == complaint_id).first()
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return complaint


def get_rma_or_404(db: Session, company_id: int, rma_id: int) -> ReturnMaterialAuthorization:
    rma = rma_query(db, company_id).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if rma is None:
        raise HTTPException(status_code=404, detail="RMA not found")
    return rma


def _locked_complaint(db: Session, company_id: int, complaint_id: int) -> CustomerComplaint:
    # Every writer locks the row before checking its state, so two concurrent
    # transitions cannot both pass the same from-state guard.
    complaint = (
        tenant_query(db, CustomerComplaint, company_id)
        .filter(CustomerComplaint.id == complaint_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return complaint


def _locked_rma(db: Session, company_id: int, rma_id: int) -> ReturnMaterialAuthorization:
    rma = (
        tenant_query(db, ReturnMaterialAuthorization, company_id)
        .filter(ReturnMaterialAuthorization.id == rma_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if rma is None:
        raise HTTPException(status_code=404, detail="RMA not found")
    return rma


# ============== Validation and audit helpers ==============


def _value(value: Any) -> Any:
    return value.value if isinstance(value, enum.Enum) else value


def _snapshot(row: Any) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for column in sa_inspect(type(row)).columns:
        if column.key in _SNAPSHOT_SKIP:
            continue
        value = getattr(row, column.key)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        values[column.key] = _value(value)
    return values


def _refuse_cleared(values: Dict[str, Any], required: Iterable[str]) -> None:
    cleared = sorted(field for field in required if field in values and values[field] is None)
    if cleared:
        raise HTTPException(status_code=422, detail=f"{', '.join(cleared)} cannot be cleared")


def _validate_references(
    db: Session,
    company_id: int,
    values: Dict[str, Any],
    current: Optional[Any] = None,
    records: Iterable[str] = (),
) -> None:
    """422 unless every reference names a row of the active company.

    A new selection must also be live: not soft-deleted, and for a user, active. A field
    in ``records`` names what an existing record already names, so only ownership is
    checked. An unchanged reference on update (``current``) is not re-checked at all.
    """
    for field, ref_id in values.items():
        target = _REFERENCES.get(field)
        if target is None or ref_id is None:
            continue
        if current is not None and getattr(current, field) == ref_id:
            continue  # unchanged: a record, not a new selection
        model, label = target
        live = field not in records
        query = tenant_query(db, model, company_id).filter(model.id == ref_id)
        if live and hasattr(model, "is_deleted"):
            query = query.filter(model.is_deleted.is_(False))
            label = f"live {label}"
        if live and model is User:
            query = query.filter(User.is_active.is_(True))
            label = f"active {label}"
        if query.with_entities(model.id).first() is None:
            article = "an" if label[0] in "aeiou" else "a"
            raise HTTPException(status_code=422, detail=f"{field} does not reference {article} {label} in your company")


def _refuse_frozen(row: Any, frozen: Iterable[Any], noun: str) -> None:
    """409 on any edit of a closed quality record."""
    if row.status in tuple(frozen):
        raise HTTPException(
            status_code=409,
            detail=f"A {_value(row.status)} {noun} is a closed quality record and cannot be edited",
        )


def _refuse_outside_window(rma: ReturnMaterialAuthorization, values: Dict[str, Any]) -> None:
    """409 when PUT would change a field after the decision that rests on it."""
    refused = [
        f"{field} can only be changed while the RMA is {' or '.join(_value(s) for s in statuses)}"
        for field, statuses in sorted(_RMA_FIELD_WINDOWS.items())
        if field in values and values[field] != getattr(rma, field) and rma.status not in statuses
    ]
    changes_link = "replacement_wo_id" in values and values["replacement_wo_id"] != rma.replacement_wo_id
    if changes_link and rma.disposition != "replace":
        refused.append("replacement_wo_id can only be linked on an RMA dispositioned 'replace'")
    if refused:
        raise HTTPException(status_code=409, detail=f"{'; '.join(refused)} (current status: {_value(rma.status)})")


def _require_owned(db: Session, company_id: int, model: Any, ref_id: Optional[int], label: str) -> None:
    """409 when a stored reference points outside the active company (legacy rows only).

    Deliberately not a liveness check: an NCR raised from a complaint documents the
    part and job the complaint names, even if either has since been soft-deleted.
    """
    if ref_id is None:
        return
    if tenant_query(db, model, company_id).filter(model.id == ref_id).with_entities(model.id).first() is None:
        raise HTTPException(
            status_code=409,
            detail=f"The complaint references a {label} outside your company; correct the complaint first",
        )


def _audit_transition(
    audit: AuditService,
    resource_type: str,
    row: Any,
    identifier: str,
    before: Dict[str, Any],
    *,
    required: bool = False,
) -> None:
    """A STATUS_CHANGE row when the status moved, else an UPDATE row, shaped like the
    ``log_status_change`` / ``log_update`` helpers; ``required`` makes it fail closed."""
    after = _snapshot(row)
    changes = {key: {"old": before.get(key), "new": value} for key, value in after.items() if before.get(key) != value}
    old_status, new_status = before.get("status"), after.get("status")
    if not required:
        if old_status != new_status:
            audit.log_status_change(
                resource_type, row.id, identifier, old_status, new_status, extra_data={"changes": changes}
            )
        else:
            audit.log_update(resource_type, row.id, identifier, old_values=before, new_values=after)
        return
    if old_status != new_status:
        audit.log_required(
            "STATUS_CHANGE",
            resource_type,
            resource_id=row.id,
            resource_identifier=identifier,
            description=f"Changed {resource_type} status: {identifier} from '{old_status}' to '{new_status}'",
            old_values={"status": old_status},
            new_values={"status": new_status},
            extra_data={"changes": changes},
            company_id=row.company_id,
        )
        return
    audit.log_required(
        "UPDATE",
        resource_type,
        resource_id=row.id,
        resource_identifier=identifier,
        description=f"Updated {resource_type}: {identifier}",
        old_values=before,
        new_values=after,
        extra_data={"changes": changes},
        company_id=row.company_id,
    )


def _audit_escalation(audit: AuditService, resource_type: str, row: Any, identifier: str, complaint: Any) -> None:
    """Required CREATE row for the NCR / CAR a complaint raises."""
    audit.log_required(
        "CREATE",
        resource_type,
        resource_id=row.id,
        resource_identifier=identifier,
        description=f"Created {resource_type}: {identifier} from complaint {complaint.complaint_number}",
        new_values=_snapshot(row),
        extra_data={"complaint_id": complaint.id, "complaint_number": complaint.complaint_number},
        company_id=row.company_id,
    )


def _title(text: str) -> str:
    return text if len(text) <= _TITLE_MAX else text[: _TITLE_MAX - 3] + "..."


# ============== Numbering ==============
#
# Each company has its own CC-YYYY-NNNN / RMA-YYYY-NNNN sequence. complaint_number and
# rma_number are unique per company, as (company_id, number) (migration
# 116_business_keys_per_company re-keyed the old GLOBAL uniques), so two tenants may
# both hold CC-2026-0001. The read-max-plus-one scan sees only the active company's
# rows, and the advisory lock is per (namespace, company), so concurrent creates in one
# company serialize while different companies never wait on each other. The scan
# deliberately reads every row of the company: the constraint has no partial predicate,
# so a number stays taken for good.


def generate_complaint_number(db: Session, company_id: int) -> str:
    acquire_generator_lock(db, "complaint_number", company_id)
    prefix = f"CC-{datetime.now():%Y}-"
    last = (
        tenant_query(db, CustomerComplaint, company_id)
        .with_entities(CustomerComplaint.complaint_number)
        .filter(CustomerComplaint.complaint_number.like(f"{prefix}%"))
        .order_by(CustomerComplaint.complaint_number.desc())
        .first()
    )
    num = int(last[0].split("-")[-1]) + 1 if last else 1
    return f"{prefix}{num:04d}"


def generate_rma_number(db: Session, company_id: int) -> str:
    acquire_generator_lock(db, "rma_number", company_id)
    prefix = f"RMA-{datetime.now():%Y}-"
    last = (
        tenant_query(db, ReturnMaterialAuthorization, company_id)
        .with_entities(ReturnMaterialAuthorization.rma_number)
        .filter(ReturnMaterialAuthorization.rma_number.like(f"{prefix}%"))
        .order_by(ReturnMaterialAuthorization.rma_number.desc())
        .first()
    )
    num = int(last[0].split("-")[-1]) + 1 if last else 1
    return f"{prefix}{num:04d}"


# ============== Dashboard ==============


def complaint_dashboard(db: Session, company_id: int) -> Dict[str, Any]:
    """Every aggregate is scoped to the active company."""

    def scoped(query: Query) -> Query:
        return tenant_filter(query, CustomerComplaint, company_id)

    open_complaints = (
        scoped(db.query(func.count(CustomerComplaint.id)))
        .filter(CustomerComplaint.status.in_(OPEN_COMPLAINT_STATUSES))
        .scalar()
        or 0
    )

    resolved = (
        scoped(db.query(CustomerComplaint.date_received, CustomerComplaint.resolved_date))
        .filter(CustomerComplaint.resolved_date.isnot(None), CustomerComplaint.date_received.isnot(None))
        .all()
    )
    avg_days = (
        round(sum((resolved_date - received).days for received, resolved_date in resolved) / len(resolved), 1)
        if resolved
        else None
    )

    severity_counts = (
        scoped(db.query(CustomerComplaint.severity, func.count(CustomerComplaint.id)))
        .filter(CustomerComplaint.status.in_(OPEN_COMPLAINT_STATUSES))
        .group_by(CustomerComplaint.severity)
        .all()
    )
    by_severity = {_value(severity): count for severity, count in severity_counts}

    by_customer_rows = (
        scoped(db.query(CustomerComplaint.customer_name, func.count(CustomerComplaint.id).label("count")))
        .group_by(CustomerComplaint.customer_name)
        .order_by(func.count(CustomerComplaint.id).desc())
        .limit(10)
        .all()
    )
    by_customer = [{"customer": name, "count": count} for name, count in by_customer_rows]

    satisfied_total = (
        scoped(db.query(func.count(CustomerComplaint.id)))
        .filter(CustomerComplaint.customer_satisfied.isnot(None))
        .scalar()
        or 0
    )
    satisfied_yes = (
        scoped(db.query(func.count(CustomerComplaint.id)))
        .filter(CustomerComplaint.customer_satisfied.is_(True))
        .scalar()
        or 0
    )
    satisfaction_rate = round((satisfied_yes / satisfied_total) * 100, 1) if satisfied_total > 0 else None

    twelve_months_ago = date.today() - timedelta(days=365)
    trend_rows = (
        scoped(
            db.query(
                extract("year", CustomerComplaint.date_received).label("year"),
                extract("month", CustomerComplaint.date_received).label("month"),
                func.count(CustomerComplaint.id).label("count"),
            )
        )
        .filter(CustomerComplaint.date_received >= twelve_months_ago)
        .group_by("year", "month")
        .order_by("year", "month")
        .all()
    )
    trend = [{"year": int(row.year), "month": int(row.month), "count": row.count} for row in trend_rows]

    open_rmas = (
        tenant_filter(db.query(func.count(ReturnMaterialAuthorization.id)), ReturnMaterialAuthorization, company_id)
        .filter(ReturnMaterialAuthorization.status.in_(OPEN_RMA_STATUSES))
        .scalar()
        or 0
    )

    return {
        "open_complaints": open_complaints,
        "avg_resolution_days": avg_days,
        "by_severity": by_severity,
        "by_customer": by_customer,
        "satisfaction_rate": satisfaction_rate,
        "trend": trend,
        "open_rmas": open_rmas,
    }


# ============== Complaint writes ==============


def create_complaint(
    db: Session, company_id: int, values: Dict[str, Any], user: User, audit: AuditService
) -> CustomerComplaint:
    _validate_references(db, company_id, values)
    fields = dict(values)
    fields["date_received"] = fields.get("date_received") or date.today()
    complaint = CustomerComplaint(
        company_id=company_id,
        complaint_number=generate_complaint_number(db, company_id),
        received_by=user.id,
        **fields,
    )
    db.add(complaint)
    db.flush()
    audit.log_create(COMPLAINT_RESOURCE, complaint.id, complaint.complaint_number, new_values=_snapshot(complaint))
    return complaint


def update_complaint(
    db: Session, company_id: int, complaint_id: int, values: Dict[str, Any], audit: AuditService
) -> CustomerComplaint:
    complaint = _locked_complaint(db, company_id, complaint_id)
    _refuse_frozen(complaint, FROZEN_COMPLAINT_STATUSES, "complaint")
    _refuse_cleared(values, _COMPLAINT_REQUIRED_FIELDS)
    _validate_references(db, company_id, values, current=complaint)
    before = _snapshot(complaint)
    for field, value in values.items():
        setattr(complaint, field, value)
    db.flush()
    audit.log_update(
        COMPLAINT_RESOURCE, complaint.id, complaint.complaint_number, old_values=before, new_values=_snapshot(complaint)
    )
    return complaint


def _complaint_transition(
    db: Session,
    company_id: int,
    complaint_id: int,
    audit: AuditService,
    *,
    allowed_from: Iterable[ComplaintStatus],
    refusal: str,
    apply: Callable[[CustomerComplaint], None],
    required: bool = False,
) -> CustomerComplaint:
    complaint = _locked_complaint(db, company_id, complaint_id)
    if complaint.status not in tuple(allowed_from):
        raise HTTPException(status_code=400, detail=f"{refusal} (current status: {_value(complaint.status)})")
    before = _snapshot(complaint)
    apply(complaint)
    db.flush()
    _audit_transition(audit, COMPLAINT_RESOURCE, complaint, complaint.complaint_number, before, required=required)
    return complaint


def investigate_complaint(
    db: Session,
    company_id: int,
    complaint_id: int,
    audit: AuditService,
    *,
    investigation_findings: str,
    root_cause: Optional[str],
    containment_action: Optional[str],
) -> CustomerComplaint:
    def apply(complaint: CustomerComplaint) -> None:
        complaint.status = ComplaintStatus.UNDER_INVESTIGATION
        complaint.investigation_findings = investigation_findings
        if root_cause:
            complaint.root_cause = root_cause
        if containment_action:
            complaint.containment_action = containment_action

    return _complaint_transition(
        db,
        company_id,
        complaint_id,
        audit,
        allowed_from=OPEN_COMPLAINT_STATUSES,
        refusal="Only an open complaint can be investigated",
        apply=apply,
    )


def resolve_complaint(
    db: Session,
    company_id: int,
    complaint_id: int,
    audit: AuditService,
    *,
    resolution_description: str,
    corrective_action: Optional[str],
    preventive_action: Optional[str],
    actual_cost: Optional[float],
) -> CustomerComplaint:
    def apply(complaint: CustomerComplaint) -> None:
        complaint.status = ComplaintStatus.RESOLVED
        complaint.resolution_description = resolution_description
        complaint.resolved_date = date.today()
        if corrective_action:
            complaint.corrective_action = corrective_action
        if preventive_action:
            complaint.preventive_action = preventive_action
        if actual_cost is not None:
            complaint.actual_cost = actual_cost

    return _complaint_transition(
        db,
        company_id,
        complaint_id,
        audit,
        allowed_from=RESOLVABLE_COMPLAINT_STATUSES,
        refusal="A complaint can only be resolved after its investigation is recorded",
        apply=apply,
        required=True,
    )


def close_complaint(
    db: Session,
    company_id: int,
    complaint_id: int,
    audit: AuditService,
    *,
    customer_satisfied: Optional[bool],
    satisfaction_notes: Optional[str],
) -> CustomerComplaint:
    def apply(complaint: CustomerComplaint) -> None:
        complaint.status = ComplaintStatus.CLOSED
        complaint.closed_date = date.today()
        if customer_satisfied is not None:
            complaint.customer_satisfied = customer_satisfied
        if satisfaction_notes:
            complaint.satisfaction_notes = satisfaction_notes

    return _complaint_transition(
        db,
        company_id,
        complaint_id,
        audit,
        allowed_from=(ComplaintStatus.RESOLVED,),
        refusal="Only a resolved complaint can be closed",
        apply=apply,
        required=True,
    )


def _link_record(db: Session, complaint: CustomerComplaint, field: str, record_id: int, audit: AuditService) -> None:
    before = _snapshot(complaint)
    setattr(complaint, field, record_id)
    db.flush()
    _audit_transition(audit, COMPLAINT_RESOURCE, complaint, complaint.complaint_number, before, required=True)


def _linked_live(db: Session, company_id: int, model: Any, record_id: int) -> bool:
    """Whether a complaint's linked NCR / CAR still counts as its live link.

    An NCR is retired by soft delete (void sets ``is_deleted``); a CAR has no soft delete and
    is retired by ``status == VOID``. A link to a row this company cannot see (a legacy
    cross-tenant id) is not a live link either.
    """
    query = tenant_query(db, model, company_id).filter(model.id == record_id)
    if model is NonConformanceReport:
        query = query.filter(NonConformanceReport.is_deleted == False)  # noqa: E712
    else:
        query = query.filter(CorrectiveActionRequest.status != CARStatus.VOID)
    return query.with_entities(model.id).first() is not None


def create_ncr_from_complaint(
    db: Session, company_id: int, complaint_id: int, user: User, audit: AuditService
) -> CustomerComplaint:
    """Raise a customer-return NCR from a complaint; same numbering, event and tenant as ``POST /quality/ncr``.

    Refused while the complaint links a LIVE NCR. A voided NCR (``DELETE /quality/ncr/{id}``)
    no longer blocks: a fresh one is raised and the link moves to it, the complaint's
    required UPDATE row recording the old -> new ``ncr_id``.
    """
    complaint = _locked_complaint(db, company_id, complaint_id)
    if complaint.ncr_id and _linked_live(db, company_id, NonConformanceReport, complaint.ncr_id):
        raise HTTPException(status_code=400, detail="Complaint already has a linked NCR")
    _require_owned(db, company_id, Part, complaint.part_id, "part")
    _require_owned(db, company_id, WorkOrder, complaint.work_order_id, "work order")

    ncr = NonConformanceReport(
        company_id=company_id,
        ncr_number=generate_ncr_number(db, company_id),
        title=_title(f"NCR from {complaint.complaint_number}: {complaint.title}"),
        description=complaint.description,
        source=NCRSource.CUSTOMER_RETURN,
        part_id=complaint.part_id,
        work_order_id=complaint.work_order_id,
        lot_number=complaint.lot_number,
        serial_number=complaint.serial_number,
        quantity_affected=complaint.quantity_affected,
        detected_by=user.id,
        detected_date=date.today(),
    )
    db.add(ncr)
    db.flush()
    # Same event as quality.create_ncr, so the ncr.created notification fires.
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="ncr_created",
        source_module="quality",
        entity_type="ncr",
        entity_id=ncr.id,
        work_order_id=ncr.work_order_id,
        user_id=user.id,
        severity="high",
        event_payload={
            "ncr_number": ncr.ncr_number,
            "title": ncr.title,
            "source": _value(ncr.source),
            "status": _value(ncr.status),
            "disposition": _value(ncr.disposition),
            "part_id": ncr.part_id,
            "quantity_affected": float(ncr.quantity_affected or 0),
            "complaint_id": complaint.id,
            "complaint_number": complaint.complaint_number,
        },
    )
    _audit_escalation(audit, "ncr", ncr, ncr.ncr_number, complaint)
    _link_record(db, complaint, "ncr_id", ncr.id, audit)
    return complaint


def create_car_from_complaint(
    db: Session, company_id: int, complaint_id: int, user: User, audit: AuditService
) -> CustomerComplaint:
    """Raise a CAR from a complaint; same numbering, event and tenant as ``POST /quality/car``.

    Refused while the complaint links a non-VOID CAR. A CAR voided through
    ``POST /quality/car/{id}/void`` no longer blocks: a fresh one is raised and the link
    moves to it, the same rule ``POST /quality/ncr/{id}/create-car`` follows, with the
    complaint's required UPDATE row recording the old -> new ``car_id``.
    """
    complaint = _locked_complaint(db, company_id, complaint_id)
    if complaint.car_id and _linked_live(db, company_id, CorrectiveActionRequest, complaint.car_id):
        raise HTTPException(status_code=400, detail="Complaint already has a linked CAR")

    car = CorrectiveActionRequest(
        company_id=company_id,
        car_number=generate_car_number(db, company_id),
        title=_title(f"CAR from {complaint.complaint_number}: {complaint.title}"),
        problem_description=complaint.description,
        initiated_by=user.id,
    )
    db.add(car)
    db.flush()
    # Same event as quality.create_car, so the car.created notification fires.
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="car_created",
        source_module="quality",
        entity_type="car",
        entity_id=car.id,
        user_id=user.id,
        severity="high" if (car.priority or 3) <= 2 else "medium",
        event_payload={
            "car_number": car.car_number,
            "title": car.title,
            "car_type": _value(car.car_type),
            "status": _value(car.status),
            "priority": car.priority,
            "due_date": car.due_date.isoformat() if car.due_date else None,
            "complaint_id": complaint.id,
            "complaint_number": complaint.complaint_number,
        },
    )
    _audit_escalation(audit, "car", car, car.car_number, complaint)
    _link_record(db, complaint, "car_id", car.id, audit)
    return complaint


# ============== RMA writes ==============


def _copied_from_complaint(db: Session, company_id: int, values: Dict[str, Any]) -> frozenset:
    """The RMA reference fields whose value is the one its complaint already names.

    The page builds the RMA from the complaint and sends those ids back, so they are a
    record of what the complaint named, not a new choice: retiring the part or customer
    after the complaint was logged must not block the return (create-ncr follows the
    same rule). A foreign complaint resolves to nothing here and is refused (422) by
    ``_validate_references`` on ``complaint_id`` itself.
    """
    complaint_id = values.get("complaint_id")
    if complaint_id is None:
        return frozenset()
    complaint = tenant_query(db, CustomerComplaint, company_id).filter(CustomerComplaint.id == complaint_id).first()
    if complaint is None:
        return frozenset()
    return frozenset(
        field
        for field in _COMPLAINT_COPIED_REFERENCES
        if values.get(field) is not None and values[field] == getattr(complaint, field)
    )


def create_rma(
    db: Session, company_id: int, values: Dict[str, Any], audit: AuditService
) -> ReturnMaterialAuthorization:
    _validate_references(db, company_id, values, records=_copied_from_complaint(db, company_id, values))
    rma = ReturnMaterialAuthorization(company_id=company_id, rma_number=generate_rma_number(db, company_id), **values)
    db.add(rma)
    db.flush()
    audit.log_create(RMA_RESOURCE, rma.id, rma.rma_number, new_values=_snapshot(rma))
    return rma


def update_rma(
    db: Session, company_id: int, rma_id: int, values: Dict[str, Any], audit: AuditService
) -> ReturnMaterialAuthorization:
    rma = _locked_rma(db, company_id, rma_id)
    _refuse_frozen(rma, FROZEN_RMA_STATUSES, "RMA")
    _refuse_cleared(values, _RMA_REQUIRED_FIELDS)
    _refuse_outside_window(rma, values)
    _validate_references(db, company_id, values, current=rma)
    before = _snapshot(rma)
    for field, value in values.items():
        setattr(rma, field, value)
    db.flush()
    audit.log_update(RMA_RESOURCE, rma.id, rma.rma_number, old_values=before, new_values=_snapshot(rma))
    return rma


def _rma_transition(
    db: Session,
    company_id: int,
    rma_id: int,
    audit: AuditService,
    *,
    allowed_from: Iterable[RMAStatus],
    refusal: str,
    apply: Callable[[ReturnMaterialAuthorization], None],
    required: bool = False,
) -> ReturnMaterialAuthorization:
    rma = _locked_rma(db, company_id, rma_id)
    if rma.status not in tuple(allowed_from):
        raise HTTPException(status_code=400, detail=refusal)
    before = _snapshot(rma)
    apply(rma)
    db.flush()
    _audit_transition(audit, RMA_RESOURCE, rma, rma.rma_number, before, required=required)
    return rma


def decide_rma(
    db: Session, company_id: int, rma_id: int, user: User, audit: AuditService, *, approve: bool
) -> ReturnMaterialAuthorization:
    verb = "approved" if approve else "denied"

    def apply(rma: ReturnMaterialAuthorization) -> None:
        rma.status = RMAStatus.APPROVED if approve else RMAStatus.DENIED
        rma.authorized_by = user.id
        rma.authorized_date = date.today()

    return _rma_transition(
        db,
        company_id,
        rma_id,
        audit,
        allowed_from=(RMAStatus.REQUESTED,),
        refusal=f"RMA can only be {verb} from REQUESTED status",
        apply=apply,
        required=True,
    )


def receive_rma(
    db: Session, company_id: int, rma_id: int, audit: AuditService, *, shipping_tracking: Optional[str]
) -> ReturnMaterialAuthorization:
    def apply(rma: ReturnMaterialAuthorization) -> None:
        rma.status = RMAStatus.MATERIAL_RECEIVED
        rma.received_date = date.today()
        if shipping_tracking:
            rma.shipping_tracking = shipping_tracking

    return _rma_transition(
        db,
        company_id,
        rma_id,
        audit,
        allowed_from=(RMAStatus.APPROVED,),
        refusal="RMA must be approved before receiving material",
        apply=apply,
    )


def inspect_rma(
    db: Session, company_id: int, rma_id: int, audit: AuditService, *, inspection_findings: str
) -> ReturnMaterialAuthorization:
    def apply(rma: ReturnMaterialAuthorization) -> None:
        rma.status = RMAStatus.UNDER_INSPECTION
        rma.inspection_date = date.today()
        rma.inspection_findings = inspection_findings

    return _rma_transition(
        db,
        company_id,
        rma_id,
        audit,
        allowed_from=(RMAStatus.MATERIAL_RECEIVED,),
        refusal="Material must be received before inspection",
        apply=apply,
    )


def dispose_rma(
    db: Session,
    company_id: int,
    rma_id: int,
    audit: AuditService,
    *,
    disposition: str,
    credit_amount: Optional[float],
    replacement_wo_id: Optional[int],
    notes: Optional[str],
) -> ReturnMaterialAuthorization:
    def apply(rma: ReturnMaterialAuthorization) -> None:
        # Both refusals precede the first mutation; the transaction rolls back either way.
        if disposition not in VALID_DISPOSITIONS:
            raise HTTPException(status_code=400, detail=f"Disposition must be one of: {', '.join(VALID_DISPOSITIONS)}")
        if replacement_wo_id:
            _validate_references(db, company_id, {"replacement_wo_id": replacement_wo_id}, current=rma)
        rma.status = RMAStatus.DISPOSITION_DECIDED
        rma.disposition = disposition
        if credit_amount is not None:
            rma.credit_amount = credit_amount
        if replacement_wo_id:
            rma.replacement_wo_id = replacement_wo_id
        if notes:
            rma.notes = notes

    return _rma_transition(
        db,
        company_id,
        rma_id,
        audit,
        allowed_from=(RMAStatus.UNDER_INSPECTION, RMAStatus.MATERIAL_RECEIVED),
        refusal="RMA must be under inspection or material received for disposition",
        apply=apply,
        required=True,
    )


def complete_rma(db: Session, company_id: int, rma_id: int, audit: AuditService) -> ReturnMaterialAuthorization:
    def apply(rma: ReturnMaterialAuthorization) -> None:
        rma.status = RMAStatus.COMPLETED

    return _rma_transition(
        db,
        company_id,
        rma_id,
        audit,
        allowed_from=(RMAStatus.DISPOSITION_DECIDED,),
        refusal="RMA disposition must be decided before completing",
        apply=apply,
        required=True,
    )
