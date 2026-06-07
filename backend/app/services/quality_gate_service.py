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
* ``fai_not_passed`` (QG-3): a First Article Inspection linked to this work order
  that is not in a PASSED state, company-scoped. The data model has no "FAI
  required" flag (no operation_id / required marker on the FAI row), so this only
  fires when an FAI EXISTS and is not passed -- documented limitation.
* ``open_blocker`` (BLK-2): an OPEN/ACKNOWLEDGED ``WorkOrderBlocker`` on the
  operation or the work order, company-scoped.

Detection is strictly read-only -- it never mutates a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models.quality import (
    FAIStatus,
    FirstArticleInspection,
    NCRDisposition,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

# Audit action verb stamped on the tamper-evident chain when a completion proceeds
# despite an unsatisfied quality gate. Distinct from a plain COMPLETE so the bypass
# is greppable in the audit trail.
QUALITY_EXCEPTION_AUDIT_ACTION = "COMPLETED_WITH_QUALITY_EXCEPTION"
QUALITY_EXCEPTION_EVENT_TYPE = "quality_exception_on_completion"

# NCR states that are considered RESOLVED for the purposes of the completion gate.
# Anything else (OPEN / UNDER_REVIEW / PENDING_DISPOSITION) -- or a disposition still
# left at PENDING -- counts as an unresolved quality hold.
_RESOLVED_NCR_STATUSES = (NCRStatus.CLOSED, NCRStatus.VOID)

# FAI states that count as "passed" for the gate. Anything else (PENDING /
# IN_PROGRESS / FAILED / CONDITIONAL) is treated as not-passed.
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

    # QG-3: FAI linked to this work order that is not PASSED, company-scoped.
    # Limitation: the FAI model carries no "FAI required" flag and no operation_id,
    # so we can only detect an EXISTING non-passed FAI -- a missing-but-required FAI
    # is not detectable here. Documented in the module docstring and the report.
    fais = (
        db.query(FirstArticleInspection)
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.work_order_id == work_order.id,
        )
        .all()
    )
    for fai in fais:
        if fai.status not in _PASSED_FAI_STATUSES:
            exceptions.append(
                QualityException(
                    code="fai_not_passed",
                    severity="high",
                    message=(
                        f"First Article Inspection {fai.fai_number} on this work order "
                        f"is not passed (status={_enum_value(fai.status)})."
                    ),
                    reference_type="fai",
                    reference_id=fai.id,
                )
            )

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

    return exceptions


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
