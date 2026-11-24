"""Quality-gate evaluation for work-order / operation completion (Batch 4, rank 7).

WARN-AND-RECORD posture (the product owner chose NOT to hard-block): completion
still SUCCEEDS when a quality gate is unsatisfied, but every bypass must leave a
tamper-evident record and surface a warning so it is discoverable in the audit
trail. This module owns ONLY the *detection* (read-only, tenant-scoped) and the
recording helper; the completion endpoints/finalizer call it and never block on
the result.

Gates detected (audit findings QG-1, QG-3, BLK-2):

* ``inspection_incomplete`` (QG-1): ``operation.requires_inspection and not
  operation.inspection_complete`` -- evaluated on the already-loaded operation row,
  so it costs NO extra query (this is the one gate cheap enough to also run on the
  read/reconcile path, QG-4).
* ``open_ncr`` (QG-3): a non-closed/non-void NCR (or one whose disposition is still
  PENDING) linked to this work order, company-scoped.
* ``fai_not_passed`` (QG-3): the LATEST First Article Inspection per part linked to
  this work order is not accepted (PASSED, or CONDITIONAL with the customer's
  approval on record), company-scoped. A superseded FAI (an earlier FAILED or open one
  followed by a newer one for the same part) is ignored. The data model has no "FAI
  required" flag (no operation_id / required marker on the FAI row), so this only
  fires when an FAI EXISTS and is not passed -- documented limitation.
* ``open_blocker`` (BLK-2): an OPEN/ACKNOWLEDGED ``WorkOrderBlocker`` on the
  operation or the work order, company-scoped.

Detection is strictly read-only -- it never mutates a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.quality import (
    FAIStatus,
    FirstArticleInspection,
    NCRDisposition,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus
from app.services.audit_service import AuditService
from app.services.fai_service import CUSTOMER_APPROVABLE_STATUSES
from app.services.fai_service import FINAL_STATUSES as FAI_FINAL_STATUSES
from app.services.operational_event_service import OperationalEventService

# G1: imported from work_order_state_service. No circular import: that module imports
# only the work_order / time_entry models, never this one -- so quality_gate_service
# may depend on it (one-directional).
from app.services.work_order_state_service import incomplete_child_work_orders

# Audit action verb stamped on the tamper-evident chain when a completion proceeds
# despite an unsatisfied quality gate. Distinct from a plain COMPLETE so the bypass
# is greppable in the audit trail.
QUALITY_EXCEPTION_AUDIT_ACTION = "COMPLETED_WITH_QUALITY_EXCEPTION"
QUALITY_EXCEPTION_EVENT_TYPE = "quality_exception_on_completion"

# NCR states that are considered RESOLVED for the purposes of the completion gate.
# Anything else (OPEN / UNDER_REVIEW / PENDING_DISPOSITION) -- or a disposition still
# left at PENDING -- counts as an unresolved quality hold.
_RESOLVED_NCR_STATUSES = (NCRStatus.CLOSED, NCRStatus.VOID)

# FAI acceptance for the gate is judged on the LATEST first article per (work order,
# part) -- see ``fai_is_accepted``. The status meanings come from ``fai_service`` rather
# than being restated here: PASSED is accepted outright; a status the customer may
# approve (CONDITIONAL) is accepted only once ``customer_approved`` is on record; FAILED
# is final and answered by raising a NEW FAI, which then becomes the latest.
_PASSED_FAI_STATUSES = (FAIStatus.PASSED,)

# Blocker states that are still "open" for the gate.
_OPEN_BLOCKER_STATUSES = (
    WorkOrderBlockerStatus.OPEN.value,
    WorkOrderBlockerStatus.ACKNOWLEDGED.value,
)


@dataclass(frozen=True)
class QualityException:
    """One unsatisfied quality gate detected at completion time.

    ``severity`` is advisory metadata for the warn-and-record posture -- it never
    blocks completion. ``reference_type`` / ``reference_id`` point at the offending
    record (the operation, the NCR, the FAI, or the blocker) so an auditor can jump
    straight to it.
    """

    code: str
    severity: str
    message: str
    reference_type: str
    reference_id: Optional[int]

    def as_dict(self) -> dict:
        """Serializable shape for the API response + audit ``extra_data``."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "reference_type": self.reference_type,
            "reference_id": self.reference_id,
        }


def evaluate_inspection_exception(operation: WorkOrderOperation) -> Optional[QualityException]:
    """The cheapest gate (QG-1): no DB query, reads only the loaded operation row.

    Reused on the reconcile/read path (QG-4) where running the full evaluator would
    be too heavy -- ``inspection_incomplete`` is recorded there at minimum.
    """
    if operation is None:
        return None
    if getattr(operation, "requires_inspection", False) and not getattr(operation, "inspection_complete", False):
        return QualityException(
            code="inspection_incomplete",
            severity="high",
            message=(
                f"Operation {operation.operation_number or operation.id} requires inspection "
                "but inspection is not recorded as complete."
            ),
            reference_type="work_order_operation",
            reference_id=operation.id,
        )
    return None


def fai_is_accepted(fai: FirstArticleInspection) -> bool:
    """True when this first article clears the gate.

    PASSED clears. A status a customer may approve that is not PASSED (CONDITIONAL)
    clears only with the customer's approval on record -- recorded through the
    existing ``POST /quality/fai/{id}/customer-approval`` verb. Everything else
    (PENDING / IN_PROGRESS / FAILED, or CONDITIONAL with no customer approval) does not.
    """
    status_ = fai.status
    if status_ in _PASSED_FAI_STATUSES:
        return True
    if status_ in CUSTOMER_APPROVABLE_STATUSES and status_ in FAI_FINAL_STATUSES:
        return bool(fai.customer_approved)
    return False


def latest_fais_by_work_order(
    db: Session, company_id: int, work_order_ids: Iterable[int]
) -> dict[int, list[FirstArticleInspection]]:
    """The newest FAI per (work order, part) for each work order, in ONE query.

    A failed first article is frozen and answered by raising a new one, so only the
    newest record per part speaks for the job: an earlier FAILED or still-open FAI
    followed by a newer one is superseded. Grouped by the FAI's OWN ``part_id`` -- an
    FAI raised against a component part of an assembly job is judged apart from the
    job's top-level part. "Newest" is ``created_at`` with ``id`` breaking ties (and
    standing in for a NULL ``created_at``). Tenant-scoped.
    """
    ids = sorted({int(i) for i in work_order_ids if i is not None})
    if not ids:
        return {}
    rows = (
        db.query(FirstArticleInspection)
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.work_order_id.in_(ids),
        )
        .all()
    )
    latest: dict[tuple[int, Optional[int]], FirstArticleInspection] = {}
    for fai in rows:
        key = (fai.work_order_id, fai.part_id)
        current = latest.get(key)
        if current is None or _fai_sort_key(fai) > _fai_sort_key(current):
            latest[key] = fai
    grouped: dict[int, list[FirstArticleInspection]] = {}
    for (wo_id, _part_id), fai in sorted(latest.items(), key=lambda item: item[1].id):
        grouped.setdefault(wo_id, []).append(fai)
    return grouped


def latest_fai_exceptions_by_work_order(
    db: Session, company_id: int, work_order_ids: Iterable[int]
) -> dict[int, list[QualityException]]:
    """``fai_not_passed`` for each work order whose latest FAI for a part is not accepted."""
    result: dict[int, list[QualityException]] = {}
    for wo_id, fais in latest_fais_by_work_order(db, company_id, work_order_ids).items():
        for fai in fais:
            if fai_is_accepted(fai):
                continue
            detail = f"status={_enum_value(fai.status)}"
            if fai.status in CUSTOMER_APPROVABLE_STATUSES and fai.status not in _PASSED_FAI_STATUSES:
                detail += ", customer approval not recorded"
            result.setdefault(wo_id, []).append(
                QualityException(
                    code="fai_not_passed",
                    severity="high",
                    message=(f"First Article Inspection {fai.fai_number} on this work order is not passed ({detail})."),
                    reference_type="fai",
                    reference_id=fai.id,
                )
            )
    return result


def _fai_sort_key(fai: FirstArticleInspection) -> tuple:
    created = fai.created_at
    return (created is not None, created or datetime.min, fai.id or 0)


def evaluate_completion_quality_exceptions(
    db: Session,
    work_order: WorkOrder,
    operation: Optional[WorkOrderOperation],
    company_id: int,
) -> list[QualityException]:
    """Detect every unsatisfied quality gate for a completing operation / work order.

    Read-only and tenant-scoped (every query filters ``company_id``). Returns a
    structured list -- the caller records each exception (audit + operational event)
    and returns them in the API response. Never raises on a "gate failed"; only the
    presence of an exception in the list signals it.

    ``operation`` may be ``None`` for a pure work-order-level completion (the
    inspection gate is then skipped; NCR/FAI/blocker gates still run against the WO).
    """
    exceptions: list[QualityException] = []

    # QG-1: inspection gate (no query, loaded row).
    inspection_exc = evaluate_inspection_exception(operation) if operation is not None else None
    if inspection_exc is not None:
        exceptions.append(inspection_exc)

    if work_order is None or work_order.id is None:
        return exceptions

    # QG-3: open NCR for this work order, company-scoped. "Open" = status not in
    # {closed, void} OR disposition still PENDING.
    open_ncrs = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.work_order_id == work_order.id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for ncr in open_ncrs:
        status_unresolved = ncr.status not in _RESOLVED_NCR_STATUSES
        disposition_pending = ncr.disposition == NCRDisposition.PENDING
        if status_unresolved or disposition_pending:
            exceptions.append(
                QualityException(
                    code="open_ncr",
                    severity="high",
                    message=(
                        f"NCR {ncr.ncr_number} on this work order is unresolved "
                        f"(status={_enum_value(ncr.status)}, disposition={_enum_value(ncr.disposition)})."
                    ),
                    reference_type="ncr",
                    reference_id=ncr.id,
                )
            )

    # QG-3: the LATEST FAI per part on this work order that is not accepted, company-
    # scoped. Limitation: the FAI model carries no "FAI required" flag and no
    # operation_id, so we can only detect an EXISTING non-accepted FAI -- a missing-
    # but-required FAI is not detectable here. Documented in the module docstring.
    exceptions.extend(latest_fai_exceptions_by_work_order(db, company_id, [work_order.id]).get(work_order.id, []))

    # BLK-2: open/acknowledged blockers on this operation or work order.
    blocker_query = db.query(WorkOrderBlocker).filter(
        WorkOrderBlocker.company_id == company_id,
        WorkOrderBlocker.status.in_(_OPEN_BLOCKER_STATUSES),
    )
    if operation is not None and operation.id is not None:
        blocker_query = blocker_query.filter(
            (WorkOrderBlocker.work_order_id == work_order.id) | (WorkOrderBlocker.operation_id == operation.id)
        )
    else:
        blocker_query = blocker_query.filter(WorkOrderBlocker.work_order_id == work_order.id)
    for blocker in blocker_query.all():
        exceptions.append(
            QualityException(
                code="open_blocker",
                severity=str(blocker.severity or "medium"),
                message=(
                    f"Blocker '{blocker.title}' ({_enum_value(blocker.category)}) is still "
                    f"{_enum_value(blocker.status)} on this work order."
                ),
                reference_type="work_order_blocker",
                reference_id=blocker.id,
            )
        )

    # G1: a parent WO must not silently complete while a laser-nest child WO is still
    # open. We only run this once THIS completion drove the WO terminal -- at every
    # live eval call site the finalizer has already flipped status to COMPLETE -- so a
    # mere progress/partial update never triggers it. Read-only + tenant-scoped (the
    # child query filters company_id / is_deleted / LASER_CUTTING / non-terminal).
    if work_order.status == WorkOrderStatus.COMPLETE:
        incomplete = incomplete_child_work_orders(db, work_order, company_id)
        if incomplete:
            exceptions.append(
                QualityException(
                    code="child_work_orders_incomplete",
                    severity="high",
                    message=(
                        f"Work order {work_order.work_order_number} completed with {len(incomplete)} "
                        f"incomplete child work order(s): {', '.join(c.work_order_number for c in incomplete)}."
                    ),
                    reference_type="work_order",
                    reference_id=work_order.id,
                )
            )

    return exceptions


# ---------------------------------------------------------------------------
# Shipment release check (shipping/2). A narrower rule than the completion gate: it
# asks "is anything open that should stop this job's certificate going out
# automatically", not "was every gate satisfied when the work finished".
# ---------------------------------------------------------------------------

# Dispositions that send the pieces back into (or keep them in) the shipped product, so
# the NCR counts until it is CLOSED. SCRAP and RETURN_TO_VENDOR take the pieces out of
# the shipment, so an NCR so dispositioned never holds the certificate back, even while
# its paperwork is still open.
SHIPMENT_RELEASE_EXCEPTION_CODES = ("open_ncr", "fai_not_passed", "open_blocker")


def ncr_holds_shipment(ncr: NonConformanceReport) -> bool:
    """True when this NCR holds a shipment's automatic certificate back.

    VOID never counts. An NCR not yet dispositioned (PENDING / NULL) counts whatever its
    status. Every other disposition -- rework, repair, use-as-is, and also scrap and
    return-to-vendor -- counts until the NCR is CLOSED.

    Scrap / return-to-vendor are deliberately NOT exempt on disposition alone: nothing
    links an NCR to the scrap booked against its job, so "dispositioned scrap" cannot
    tell whether the pieces actually left the job's completed quantity. Closing the NCR
    is the record that the disposition was carried out. (Narrower than the plan's "scrap
    NCRs whose pieces are already scrapped do not count" until such a link exists; an
    owner-visible default, and never a hard stop -- Quality may accept it with a reason.)
    """
    if ncr.status == NCRStatus.VOID:
        return False
    if ncr.disposition is None or ncr.disposition == NCRDisposition.PENDING:
        return True
    return ncr.status != NCRStatus.CLOSED


def shipment_release_exceptions(
    db: Session, company_id: int, work_order_ids: Iterable[int]
) -> dict[int, list[QualityException]]:
    """Open quality items that withhold a shipment's automatic CoC, per work order.

    Read only, tenant-scoped, three queries for any number of work orders:

    * ``open_ncr`` -- an NCR on the job that ``ncr_holds_shipment``;
    * ``fai_not_passed`` -- the latest first article per part is not accepted (the same
      rule the completion gate uses, ``latest_fai_exceptions_by_work_order``);
    * ``open_blocker`` -- an OPEN / ACKNOWLEDGED ``quality_hold`` blocker on the job.

    An unsigned inspection point is deliberately NOT an exception here: information
    only. Inspection sign-off now has its screen (``POST /shop-floor/operations/{id}/inspection``,
    on Work Order Detail and the shop-floor list), but making an unsigned point hold a
    shipment is a separate decision nobody has taken, so the rule is unchanged. Work orders
    with nothing open are absent from the result.
    """
    ids = sorted({int(i) for i in work_order_ids if i is not None})
    if not ids:
        return {}
    result: dict[int, list[QualityException]] = {}

    ncrs = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.work_order_id.in_(ids),
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .order_by(NonConformanceReport.id)
        .all()
    )
    for ncr in ncrs:
        if not ncr_holds_shipment(ncr):
            continue
        result.setdefault(ncr.work_order_id, []).append(
            QualityException(
                code="open_ncr",
                severity="high",
                message=(
                    f"NCR {ncr.ncr_number} on this work order is open "
                    f"(status={_enum_value(ncr.status)}, disposition={_enum_value(ncr.disposition) or 'pending'})."
                ),
                reference_type="ncr",
                reference_id=ncr.id,
            )
        )

    for wo_id, fai_excs in latest_fai_exceptions_by_work_order(db, company_id, ids).items():
        result.setdefault(wo_id, []).extend(fai_excs)

    blockers = (
        db.query(WorkOrderBlocker)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.work_order_id.in_(ids),
            WorkOrderBlocker.category == WorkOrderBlockerCategory.QUALITY_HOLD.value,
            WorkOrderBlocker.status.in_(_OPEN_BLOCKER_STATUSES),
        )
        .order_by(WorkOrderBlocker.id)
        .all()
    )
    for blocker in blockers:
        result.setdefault(blocker.work_order_id, []).append(
            QualityException(
                code="open_blocker",
                severity=str(blocker.severity or "medium"),
                message=f"Quality hold '{blocker.title}' is still {_enum_value(blocker.status)} on this work order.",
                reference_type="work_order_blocker",
                reference_id=blocker.id,
            )
        )
    return result


def record_completion_quality_exceptions(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    operation: Optional[WorkOrderOperation],
    exceptions: list[QualityException],
    audit: AuditService,
    user: Optional[User] = None,
    source: str = "completion",
) -> None:
    """Leave a tamper-evident record + a realtime signal for each bypassed gate.

    WARN-AND-RECORD: completion has already succeeded (or is about to commit in the
    same unit of work); this records the bypass so it is discoverable. It does NOT
    block and does NOT commit -- it only flushes via the shared ``AuditService.log``
    /``OperationalEventService.emit`` (both flush, never commit), so the rows commit
    atomically with the completion via the caller's single commit (flush -> audit ->
    commit, the established pattern).

    * Writes ONE ``audit_log`` row (action ``COMPLETED_WITH_QUALITY_EXCEPTION``)
      against the operation when present, else the work order, carrying the exception
      codes + references in ``extra_data``. Never writes ``audit_log`` directly.
    * Emits ONE ``OperationalEvent`` (severity ``warning``) for AI / realtime context.

    Best-effort and defensive: a failure to record a warning must never break the
    completion itself, so both writes are wrapped. ``AuditService.log`` already
    swallows its own failures; the ``OperationalEvent`` emit is guarded here.
    """
    if not exceptions:
        return

    resource_type = "work_order_operation" if operation is not None else "work_order"
    resource_id = operation.id if operation is not None else work_order.id
    resource_identifier = (
        (operation.operation_number or str(operation.id)) if operation is not None else work_order.work_order_number
    )
    codes = [exc.code for exc in exceptions]
    payload_exceptions = [exc.as_dict() for exc in exceptions]

    audit.log(
        action=QUALITY_EXCEPTION_AUDIT_ACTION,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_identifier=resource_identifier,
        description=(
            f"{resource_type.replace('_', ' ').title()} {resource_identifier} on WO "
            f"{work_order.work_order_number} completed with {len(exceptions)} unsatisfied "
            f"quality gate(s): {', '.join(codes)}"
        ),
        new_values={"quality_exceptions": codes},
        extra_data={
            "source": source,
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "operation_id": operation.id if operation is not None else None,
            "quality_exceptions": payload_exceptions,
        },
        company_id=company_id,
    )

    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=QUALITY_EXCEPTION_EVENT_TYPE,
            source_module="quality_gate",
            entity_type=resource_type,
            entity_id=resource_id,
            work_order_id=work_order.id,
            operation_id=operation.id if operation is not None else None,
            user_id=user.id if user is not None else None,
            severity="warning",
            event_payload={
                "source": source,
                "work_order_number": work_order.work_order_number,
                "quality_exception_codes": codes,
                "quality_exceptions": payload_exceptions,
            },
        )
    except Exception:  # pragma: no cover - a warning signal must never break completion
        # The audit row above is the compliance record; the operational event is an
        # AI/realtime convenience. If emitting it fails (e.g. a transient session
        # issue), swallow so the completion the caller is committing is unaffected.
        pass


def record_reconcile_inspection_exception(
    db: Session,
    *,
    operation_id: int,
    audit: AuditService,
    user: Optional[User] = None,
) -> None:
    """QG-4 (partial): record ``inspection_incomplete`` for a reconcile-driven op COMPLETE.

    Shared by both endpoints' reconcile-on-read audit paths so they can't drift.
    Cheapest gate ONLY (no extra quality query): reads ``requires_inspection`` /
    ``inspection_complete`` off the operation row already resident in the session.
    The NCR/FAI/blocker gates (which need extra queries) are intentionally NOT run on
    the read path -- that partial coverage is documented in the module docstring and
    the report; they are caught on the next live completion. Must never raise on a
    read path: the caller wraps this, but we also keep it side-effect-light.
    """
    operation = db.get(WorkOrderOperation, operation_id)
    if operation is None:
        return
    inspection_exc = evaluate_inspection_exception(operation)
    if inspection_exc is None:
        return
    work_order = operation.work_order
    if work_order is None or work_order.id is None:
        return
    record_completion_quality_exceptions(
        db,
        company_id=operation.company_id,
        work_order=work_order,
        operation=operation,
        exceptions=[inspection_exc],
        audit=audit,
        user=user,
        source="reconcile_on_read",
    )


def evaluate_and_record_completion_quality_exceptions(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    operation: Optional[WorkOrderOperation],
    audit: AuditService,
    user: Optional[User] = None,
    source: str = "completion",
) -> list[QualityException]:
    """Detect + record in one call; returns the exceptions for the API response.

    The single entry point the completion endpoints use on the live (locked) write
    path: it runs the full read-only evaluator against the already-loaded/locked
    rows, records each bypass (audit + event), and hands the list back so the
    response schema can surface ``quality_exceptions`` to the client. Warn-only --
    it never raises on a failed gate.
    """
    exceptions = evaluate_completion_quality_exceptions(db, work_order, operation, company_id)
    if exceptions:
        record_completion_quality_exceptions(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            exceptions=exceptions,
            audit=audit,
            user=user,
            source=source,
        )
    return exceptions


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value) if value is not None else ""
