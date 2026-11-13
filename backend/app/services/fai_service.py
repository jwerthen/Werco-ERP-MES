"""Tenant-scoped FAI authoring, immutable final inspection evidence, and the FAI log of record.

The ERP's FAI module is the company's FAI log of record (it replaces the paper/Excel log, form
QF 7.5-3.4, which stays a frozen archive). :func:`fai_log_query` / :func:`fai_log_rows` build
that log for ``GET /quality/fai/log`` and its export ``GET /exports/fai/export``.

A final disposition (PASSED / FAILED / CONDITIONAL, or a completion date) freezes the
inspection evidence. The customer's approval normally arrives AFTER that disposition, so it is
its own verb (:func:`record_customer_approval`): server-checked, once, never by an API token,
and it writes only ``customer_approved`` and ``customer_approval_date``.
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, func, inspect, or_
from sqlalchemy.orm import Query, Session, selectinload

from app.core.credentials import is_api_token_credential
from app.core.time_utils import CENTRAL_TIME_ZONE, central_today, ensure_utc
from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.part import Part
from app.models.quality import FAICharacteristic, FAIStatus, FirstArticleInspection
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.quality import (
    FAICharacteristicCreate,
    FAICharacteristicUpdate,
    FAICreate,
    FAICustomerApprovalRequest,
    FAILogRow,
    FAIResponse,
    FAIUpdate,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operational_event_service import OperationalEventService

WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]
APPROVE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]
FINAL_STATUSES = {FAIStatus.PASSED, FAIStatus.FAILED, FAIStatus.CONDITIONAL}
# A customer can accept a first article the shop passed, or passed with a documented
# deviation. A FAILED inspection is closed out by a new inspection, never by a customer
# approval written onto the failed one.
CUSTOMER_APPROVABLE_STATUSES = {FAIStatus.PASSED, FAIStatus.CONDITIONAL}

# A standing machine credential (the Werco Assistant over MCP, any bot) may not record the
# customer's approval: it asserts that a person saw the customer's acceptance.
API_TOKEN_CUSTOMER_APPROVAL = (
    "A standing API token cannot record an FAI's customer approval. A person signed in to the app must record it."
)
# Nor the final disposition itself (passed / failed / conditional): it is the inspection's
# quality judgment, it stamps the approver, and it freezes the evidence for good.
API_TOKEN_FAI_DISPOSITION = (
    "A standing API token cannot record an FAI's final disposition. A person signed in to the app must record it."
)


@contextmanager
def fai_transaction(db: Session):
    """Required evidence failure rolls back and reports a retryable service error."""
    try:
        with atomic_transaction(db):
            yield
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc


def fai_query(db: Session, company_id: int):
    # Scope relationship loads too: legacy malformed foreign keys must not expose
    # another tenant's part or characteristic through an otherwise-owned parent.
    # Historical part labels deliberately remain readable after part soft deletion.
    return (
        tenant_query(db, FirstArticleInspection, company_id)
        .options(
            selectinload(FirstArticleInspection.part.and_(Part.company_id == company_id)),
            selectinload(FirstArticleInspection.characteristics.and_(FAICharacteristic.company_id == company_id)),
        )
        .populate_existing()
    )


def get_fai(db: Session, company_id: int, fai_id: int) -> FirstArticleInspection:
    fai = fai_query(db, company_id).filter(FirstArticleInspection.id == fai_id).first()
    if fai is None:
        raise HTTPException(status_code=404, detail="FAI not found")
    return fai


def _locked_fai(db: Session, company_id: int, fai_id: int) -> FirstArticleInspection:
    # Every writer, including prefill, final disposition and the customer approval, locks
    # this same row before checking its state. Finalization cannot race an evidence edit.
    fai = (
        tenant_query(db, FirstArticleInspection, company_id)
        .filter(FirstArticleInspection.id == fai_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if fai is None:
        raise HTTPException(status_code=404, detail="FAI not found")
    return fai


def editable_fai(db: Session, company_id: int, fai_id: int) -> FirstArticleInspection:
    fai = _locked_fai(db, company_id, fai_id)
    if fai.status in FINAL_STATUSES or fai.completed_date is not None:
        raise HTTPException(status_code=409, detail="Completed FAI evidence cannot be changed; create a new inspection")
    return fai


def _characteristic(db: Session, company_id: int, fai_id: int, char_id: int) -> FAICharacteristic:
    char = (
        tenant_query(db, FAICharacteristic, company_id)
        .filter(FAICharacteristic.fai_id == fai_id, FAICharacteristic.id == char_id)
        .populate_existing()
        .first()
    )
    if char is None:
        raise HTTPException(status_code=404, detail="Characteristic not found")
    return char


def _snapshot(row):
    values = {}
    for column in inspect(type(row)).columns:
        value = getattr(row, column.key)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif hasattr(value, "value"):
            value = value.value
        values[column.key] = value
    return values


def _audit(audit, action, fai, *, char=None, old=None, extra=None):
    record = char if char is not None else fai
    audit.log_required(
        action,
        "fai_characteristic" if char is not None else "fai",
        resource_id=record.id,
        resource_identifier=f"{fai.fai_number} / {char.char_number}" if char is not None else fai.fai_number,
        company_id=fai.company_id,
        old_values=old,
        new_values=None if action == "DELETE" else _snapshot(record),
        extra_data={"fai_id": fai.id, **(extra or {})},
    )


def _counts(fai):
    return {
        field: getattr(fai, field)
        for field in ("total_characteristics", "characteristics_passed", "characteristics_failed")
    }


def _number(db, company_id):
    acquire_generator_lock(db, "fai_number", company_id)
    prefix = f"FAI-{datetime.now():%Y%m%d}-"
    last = (
        tenant_query(db, FirstArticleInspection, company_id)
        .filter(FirstArticleInspection.fai_number.like(f"{prefix}%"))
        .order_by(FirstArticleInspection.fai_number.desc())
        .first()
    )
    number = int(last.fai_number.rsplit("-", 1)[-1]) + 1 if last else 1
    return f"{prefix}{number:03d}"


def _require_job_builds_part(db: Session, company_id: int, work_order: WorkOrder, part: Part) -> None:
    """422 unless ``work_order`` builds ``part``: its own part, or a component of its operations.

    The FAI log of record names the linked job's number, customer and customer PO, so an FAI
    linked to another part's job would put that job's customer on this part's first article.
    A part-less job (a standalone laser-nest work order) names no part and is not judged.
    """
    if work_order.part_id is None or work_order.part_id == part.id:
        return
    component = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id, WorkOrderOperation.component_part_id == part.id)
        .with_entities(WorkOrderOperation.id)
        .first()
    )
    if component is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Work order {work_order.work_order_number} does not build part {part.part_number}. "
                "Link the FAI to a job for that part, or to an assembly job that builds it as a component."
            ),
        )


def create_fai(db: Session, company_id: int, data: FAICreate, user: User, audit: AuditService):
    with fai_transaction(db):
        part = tenant_query(db, Part, company_id).filter(Part.id == data.part_id, Part.is_deleted.is_(False)).first()
        if part is None:
            raise HTTPException(status_code=404, detail="Part not found")
        if data.work_order_id is not None:
            work_order = (
                tenant_query(db, WorkOrder, company_id)
                .filter(WorkOrder.id == data.work_order_id, WorkOrder.is_deleted.is_(False))
                .first()
            )
            if work_order is None:
                raise HTTPException(status_code=404, detail="Work order not found")
            _require_job_builds_part(db, company_id, work_order, part)
        values = data.model_dump()
        if values.get("part_revision") is None:
            # The inspection is of the drawing revision in force NOW. Stamp it on the record
            # so the log keeps naming the inspected revision after the part revs up.
            values["part_revision"] = part.revision
        fai = FirstArticleInspection(company_id=company_id, fai_number=_number(db, company_id), **values)
        db.add(fai)
        db.flush()
        _audit(audit, "CREATE", fai)
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="fai_created",
            source_module="quality",
            entity_type="fai",
            entity_id=fai.id,
            work_order_id=fai.work_order_id,
            user_id=user.id,
            severity="medium",
            event_payload={
                "fai_number": fai.fai_number,
                "part_id": fai.part_id,
                "status": fai.status.value,
                "fai_type": fai.fai_type,
                "reason": fai.reason,
                "due_date": fai.due_date.isoformat() if fai.due_date else None,
            },
        )
    return get_fai(db, company_id, fai.id)


def update_fai(db: Session, company_id: int, fai_id: int, data: FAIUpdate, user: User, audit: AuditService):
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    finalizing = changes.get("status") in FINAL_STATUSES
    if finalizing and is_api_token_credential(user):
        # Before the row lock and before any write: the disposition is a person's judgment.
        raise HTTPException(status_code=403, detail=API_TOKEN_FAI_DISPOSITION)
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        if finalizing and not (user.is_superuser or user.role == UserRole.PLATFORM_ADMIN or user.role in APPROVE_ROLES):
            raise HTTPException(status_code=403, detail="FAI approval requires Admin, Manager or Quality")
        if "status" in changes and changes["status"] is None:
            raise HTTPException(status_code=422, detail="FAI status cannot be null")
        if changes.get("inspector_id") is not None:
            inspector = tenant_query(db, User, company_id).filter(User.id == changes["inspector_id"]).first()
            if inspector is None:
                raise HTTPException(status_code=404, detail="Inspector not found")
        old = _snapshot(fai)
        for field, value in changes.items():
            setattr(fai, field, value)
        if finalizing:
            # The shop's day, not the server host's: on a UTC host an evening disposition
            # would otherwise be dated tomorrow in the FAI log.
            fai.completed_date = central_today()
            fai.approved_by = user.id
        if _snapshot(fai) != old:
            action = "STATUS_CHANGE" if old["status"] != fai.status.value else "UPDATE"
            _audit(audit, action, fai, old=old)
            OperationalEventService(db).emit_best_effort(
                company_id=company_id,
                event_type="fai_updated",
                source_module="quality",
                entity_type="fai",
                entity_id=fai.id,
                work_order_id=fai.work_order_id,
                user_id=user.id,
                severity="high" if fai.status == FAIStatus.FAILED else "info",
                event_payload={
                    "fai_number": fai.fai_number,
                    "changed_fields": list(changes),
                    "previous_status": old["status"],
                    "status": fai.status.value,
                    "characteristics_passed": fai.characteristics_passed,
                    "characteristics_failed": fai.characteristics_failed,
                },
            )
    return get_fai(db, company_id, fai.id)


def add_characteristic(db: Session, company_id: int, fai_id: int, data: FAICharacteristicCreate, audit: AuditService):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        old_counts = _counts(fai)
        char = FAICharacteristic(company_id=company_id, fai_id=fai.id, **data.model_dump())
        db.add(char)
        fai.total_characteristics += 1
        db.flush()
        _audit(audit, "CREATE", fai, char=char, extra={"old_counts": old_counts, "new_counts": _counts(fai)})
    db.refresh(char)
    return char


def update_characteristic(
    db: Session,
    company_id: int,
    fai_id: int,
    char_id: int,
    data: FAICharacteristicUpdate,
    user: User,
    audit: AuditService,
):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        char = _characteristic(db, company_id, fai_id, char_id)
        old = _snapshot(char)
        old_counts = _counts(fai)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(char, field, value)
        # Null means unrecorded, not failed. Treat all three dispositions explicitly.
        fai.characteristics_passed += int(char.is_conforming is True) - int(old["is_conforming"] is True)
        fai.characteristics_failed += int(char.is_conforming is False) - int(old["is_conforming"] is False)
        if _snapshot(char) != old:
            _audit(
                audit, "UPDATE", fai, char=char, old=old, extra={"old_counts": old_counts, "new_counts": _counts(fai)}
            )
            if "is_conforming" in changes:
                OperationalEventService(db).emit_best_effort(
                    company_id=company_id,
                    event_type="fai_characteristic_recorded",
                    source_module="quality",
                    entity_type="fai_characteristic",
                    entity_id=char.id,
                    work_order_id=fai.work_order_id,
                    user_id=user.id,
                    severity="high" if char.is_conforming is False else "info",
                    event_payload={
                        "fai_id": fai.id,
                        "fai_number": fai.fai_number,
                        "char_number": char.char_number,
                        "is_conforming": char.is_conforming,
                        "characteristics_passed": fai.characteristics_passed,
                        "characteristics_failed": fai.characteristics_failed,
                    },
                )
    db.refresh(char)
    return char


def delete_characteristic(db: Session, company_id: int, fai_id: int, char_id: int, audit: AuditService):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        char = _characteristic(db, company_id, fai_id, char_id)
        old_counts = _counts(fai)
        fai.total_characteristics -= 1
        fai.characteristics_passed -= int(char.is_conforming is True)
        fai.characteristics_failed -= int(char.is_conforming is False)
        _audit(
            audit,
            "DELETE",
            fai,
            char=char,
            old=_snapshot(char),
            extra={"old_counts": old_counts, "new_counts": _counts(fai), "soft_delete": False},
        )
        # This model has no SoftDeleteMixin. Unfinished characteristic deletion is
        # allowed and preserves its full prior contents in the required audit row.
        db.delete(char)
    return {"message": "Characteristic deleted"}


# --------------------------------------------------------------------- customer approval


def record_customer_approval(
    db: Session,
    company_id: int,
    fai_id: int,
    data: Optional[FAICustomerApprovalRequest],
    user: User,
    audit: AuditService,
) -> FirstArticleInspection:
    """Record that the customer approved the FAI: ``customer_approved`` and its date, nothing else.

    Allowed after the final disposition -- that is when a customer approval arrives -- and only
    on a PASSED or CONDITIONAL inspection: an undispositioned FAI is refused (409, record the
    disposition first) and so is a FAILED one (409, a failure is answered by a new inspection).
    Recorded once (409 when already recorded), so the record keeps the first approval. The
    date defaults to today (Central); a past date back-enters an approval received earlier, but
    not one before the FAI's recorded inspection date, and a future one is refused (422). The
    body is optional (no body = today). A standing API token is refused (403). The row is
    locked first; the APPROVE audit row commits with the approval or neither is saved (503).
    """
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_CUSTOMER_APPROVAL)
    today = central_today()
    approval_date = (data.customer_approval_date if data is not None else None) or today
    if approval_date > today:
        raise HTTPException(status_code=422, detail="The customer approval date cannot be in the future.")
    with fai_transaction(db):
        fai = _locked_fai(db, company_id, fai_id)
        status = fai.status or FAIStatus.PENDING
        if status == FAIStatus.FAILED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"FAI {fai.fai_number} failed inspection; a failed FAI cannot carry a customer approval. "
                    "Create a new inspection."
                ),
            )
        if status not in CUSTOMER_APPROVABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Record FAI {fai.fai_number}'s final disposition (Passed or Conditional) before its "
                    "customer approval."
                ),
            )
        if fai.customer_approved or fai.customer_approval_date is not None:
            raise HTTPException(
                status_code=409,
                detail=f"FAI {fai.fai_number} already has its customer approval; it is recorded once.",
            )
        if fai.inspection_date is not None and approval_date < fai.inspection_date:
            # The customer approves an inspected first article; it cannot predate the inspection.
            raise HTTPException(
                status_code=422,
                detail=(
                    "The customer approval date cannot be before the FAI's inspection date "
                    f"({fai.inspection_date:%m/%d/%Y})."
                ),
            )
        old: Dict[str, Any] = {
            "customer_approved": bool(fai.customer_approved),
            "customer_approval_date": fai.customer_approval_date.isoformat() if fai.customer_approval_date else None,
        }
        fai.customer_approved = True
        fai.customer_approval_date = approval_date
        new: Dict[str, Any] = {"customer_approved": True, "customer_approval_date": approval_date.isoformat()}
        audit.log_required(
            "APPROVE",
            "fai",
            resource_id=fai.id,
            resource_identifier=fai.fai_number,
            company_id=fai.company_id,
            description=f"Recorded customer approval of FAI {fai.fai_number}",
            old_values=old,
            new_values=new,
            extra_data={
                "fai_id": fai.id,
                "customer_approval": True,
                "status": status.value,
                "customer_approval_required": bool(fai.customer_approval_required),
            },
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="fai_updated",
            source_module="quality",
            entity_type="fai",
            entity_id=fai.id,
            work_order_id=fai.work_order_id,
            user_id=user.id,
            severity="info",
            event_payload={
                "fai_number": fai.fai_number,
                "changed_fields": ["customer_approved", "customer_approval_date"],
                # Unchanged on purpose: the fai.completed notification gates on the
                # TRANSITION into a final status, so this emits no second "completed".
                "previous_status": status.value,
                "status": status.value,
                "characteristics_passed": fai.characteristics_passed,
                "characteristics_failed": fai.characteristics_failed,
            },
        )
    return get_fai(db, company_id, fai_id)


# ------------------------------------------------------------------------ FAI responses


def _person(user: Optional[User]) -> Optional[str]:
    return user.full_name.strip() or None if user is not None else None


def _customer_label(work_order: Optional[WorkOrder], part: Optional[Part]) -> Optional[str]:
    """The log's Customer: the job's customer, else the part's (both free text on those rows)."""
    for value in (
        work_order.customer_name if work_order is not None else None,
        part.customer_name if part is not None else None,
    ):
        if value is not None and value.strip():
            return value.strip()
    return None


def fai_responses(db: Session, company_id: int, fais: Sequence[FirstArticleInspection]) -> List[FAIResponse]:
    """``FAIResponse`` for each FAI with names, job, customer and PO resolved in batched reads.

    Work orders and parts are read WITHOUT an ``is_deleted`` filter on purpose: an FAI is a
    historical record and keeps naming the job and part it inspected after either is retired
    (invariant 3's records exception). The tenant is always enforced, so a legacy row whose
    FK points at another company's record shows blanks rather than that company's labels.
    ``inspector_id`` is a client selection and resolves tenant-scoped only; ``approved_by`` is
    stamped by the server at final disposition, so a platform principal who dispositioned in
    this company's context (not one of its users) is still named.
    """
    fais = list(fais)
    if not fais:
        return []

    def ids(field: str) -> set:
        return {getattr(fai, field) for fai in fais if getattr(fai, field) is not None}

    inspector_ids, approver_ids = ids("inspector_id"), ids("approved_by")
    user_ids = inspector_ids | approver_ids
    users = {u.id: u for u in tenant_query(db, User, company_id).filter(User.id.in_(user_ids))} if user_ids else {}
    stamped_elsewhere = approver_ids - set(users)
    platform = (
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
    wo_ids, part_ids = ids("work_order_id"), ids("part_id")
    work_orders = (
        {wo.id: wo for wo in tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id.in_(wo_ids))} if wo_ids else {}
    )
    parts = {p.id: p for p in tenant_query(db, Part, company_id).filter(Part.id.in_(part_ids))} if part_ids else {}

    responses = []
    for fai in fais:
        work_order = work_orders.get(fai.work_order_id)
        part = parts.get(fai.part_id)
        responses.append(
            FAIResponse.model_validate(fai).model_copy(
                update={
                    "inspector_name": _person(users.get(fai.inspector_id)),
                    "approved_by_name": _person(users.get(fai.approved_by) or platform.get(fai.approved_by)),
                    "work_order_number": work_order.work_order_number if work_order is not None else None,
                    "customer_name": _customer_label(work_order, part),
                    "customer_po": (work_order.customer_po or None) if work_order is not None else None,
                    "part_current_revision": part.revision if part is not None else None,
                }
            )
        )
    return responses


def fai_response(db: Session, company_id: int, fai: FirstArticleInspection) -> FAIResponse:
    return fai_responses(db, company_id, [fai])[0]


# ---------------------------------------------------------------------- FAI log of record

FAILogQueryRow = Tuple[FirstArticleInspection, Optional[Part], Optional[WorkOrder]]


def _central_day_start_utc(day: date) -> datetime:
    """The UTC instant (naive, as ``created_at`` is stored) at which ``day`` begins in Central."""
    return datetime.combine(day, time.min, tzinfo=CENTRAL_TIME_ZONE).astimezone(timezone.utc).replace(tzinfo=None)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fai_log_query(
    db: Session,
    company_id: int,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[FAIStatus] = None,
    customer: Optional[str] = None,
    part_id: Optional[int] = None,
    work_order_id: Optional[int] = None,
    search: Optional[str] = None,
) -> Query:
    """The FAI log: (FAI, part, work order) rows of the active company, newest FAI first.

    Historical record: deliberately NO ``is_deleted`` filter on the part or the work order
    (invariant 3's records exception) -- the log keeps naming the part and job an inspection
    was for after either is retired. Both outer joins carry the tenant predicate in the ON
    clause, so a legacy row whose FK points at another company's part or work order joins
    nothing and shows blanks, never that company's part, customer or PO.

    ``start_date`` / ``end_date`` are inclusive Central dates compared with the log's date
    (completion date, else inspection date, else the Central day the FAI was created).
    ``customer`` matches the log's Customer (the job's customer, else the part's); ``search``
    matches FAI #, part # / name, job #, customer PO and serial number. Both are substring,
    case-insensitive.
    """
    fai = FirstArticleInspection
    query = (
        db.query(fai, Part, WorkOrder)
        .outerjoin(Part, and_(Part.id == fai.part_id, Part.company_id == company_id))
        .outerjoin(WorkOrder, and_(WorkOrder.id == fai.work_order_id, WorkOrder.company_id == company_id))
    )
    query = tenant_filter(query, fai, company_id)

    recorded = func.coalesce(fai.completed_date, fai.inspection_date)
    if start_date is not None:
        query = query.filter(
            or_(
                recorded >= start_date,
                and_(recorded.is_(None), fai.created_at >= _central_day_start_utc(start_date)),
            )
        )
    if end_date is not None:
        query = query.filter(
            or_(
                recorded <= end_date,
                and_(recorded.is_(None), fai.created_at < _central_day_start_utc(end_date + timedelta(days=1))),
            )
        )
    if status is not None:
        query = query.filter(fai.status == status)
    if part_id is not None:
        query = query.filter(fai.part_id == part_id)
    if work_order_id is not None:
        query = query.filter(fai.work_order_id == work_order_id)
    # A blank or whitespace-only filter is no filter: matching "%%" would drop every row whose
    # Customer is empty.
    customer = (customer or "").strip()
    search = (search or "").strip()
    if customer:
        shown_customer = func.coalesce(func.nullif(func.trim(WorkOrder.customer_name), ""), Part.customer_name)
        query = query.filter(shown_customer.ilike(_like(customer), escape="\\"))
    if search:
        pattern = _like(search)
        query = query.filter(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        fai.fai_number,
                        fai.serial_number,
                        Part.part_number,
                        Part.name,
                        WorkOrder.work_order_number,
                        WorkOrder.customer_po,
                    )
                )
            )
        )
    return query.order_by(fai.created_at.desc(), fai.id.desc())


def _central_date(value: Optional[datetime]) -> date:
    stamped = ensure_utc(value)
    return stamped.astimezone(CENTRAL_TIME_ZONE).date() if stamped is not None else central_today()


def fai_log_rows(rows: Iterable[FAILogQueryRow]) -> List[FAILogRow]:
    """Shape :func:`fai_log_query` rows into the log's typed lines. Pure; writes nothing."""
    lines = []
    for fai, part, work_order in rows:
        status = fai.status or FAIStatus.PENDING
        lines.append(
            FAILogRow(
                fai_id=fai.id,
                fai_number=fai.fai_number,
                log_date=fai.completed_date or fai.inspection_date or _central_date(fai.created_at),
                part_id=fai.part_id,
                part_number=part.part_number if part is not None else None,
                part_name=part.name if part is not None else None,
                part_revision=fai.part_revision,
                part_current_revision=part.revision if part is not None else None,
                customer=_customer_label(work_order, part),
                customer_po=(work_order.customer_po or None) if work_order is not None else None,
                work_order_id=fai.work_order_id,
                job_number=work_order.work_order_number if work_order is not None else None,
                serial_number=fai.serial_number,
                status=status,
                is_complete=status in FINAL_STATUSES or fai.completed_date is not None,
                fai_type=fai.fai_type,
                reason=fai.reason,
                total_characteristics=fai.total_characteristics or 0,
                characteristics_passed=fai.characteristics_passed or 0,
                characteristics_failed=fai.characteristics_failed or 0,
                inspection_date=fai.inspection_date,
                completed_date=fai.completed_date,
                customer_approval_required=bool(fai.customer_approval_required),
                customer_approved=bool(fai.customer_approved),
                customer_approval_date=fai.customer_approval_date,
                created_at=fai.created_at,
            )
        )
    return lines
