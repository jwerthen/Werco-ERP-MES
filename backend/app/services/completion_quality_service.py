"""Labor data-quality signal at work-order completion (Batch 7 / rank 10).

The operator-accuracy concern: a WO can reach COMPLETE with one or more operations
that recorded ZERO labor (no TimeEntry, or only zero-duration entries). That is a
process signal worth surfacing -- the cost/hour rollup for such a WO is built on
incomplete labor capture -- but it must NEVER block a completion.

This REUSES the Batch-4 warn-and-record mechanism (``quality_gate_service``): it emits
the SAME tamper-evident ``COMPLETED_WITH_QUALITY_EXCEPTION`` audit row + the
``quality_exception_on_completion`` warning ``OperationalEvent`` and returns the SAME
``QualityException`` shape on the existing ``quality_exceptions`` response field -- just
with a new exception code ``no_labor_recorded``. So the data-quality signal rides the
existing channel rather than inventing a parallel one.

Posture:

* **Fires regardless of the cost-rollup flag.** It is a process/operator-accuracy
  signal, not a cost figure, so it is evaluated whether or not
  ``LABOR_COST_ROLLUP_ENABLED`` is on.
* **Warn-and-record, never blocks.** Detection is read-only; recording reuses
  ``record_completion_quality_exceptions`` (audit flush, no commit) so it commits
  atomically with the completion via the caller's single commit.
* **Tenant-scoped + read-safe on reconcile.** Every TimeEntry lookup filters
  ``company_id``; the reconcile helper is wrapped so it can never 500 a GET.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.quality_gate_service import QualityException, record_completion_quality_exceptions

logger = logging.getLogger(__name__)

NO_LABOR_RECORDED_CODE = "no_labor_recorded"


def _operations_with_no_labor(db: Session, work_order: WorkOrder, company_id: int) -> list[WorkOrderOperation]:
    """Operations on the WO that recorded ZERO labor (no entry / only zero duration).

    An operation is flagged when the SUM of its TimeEntries' ``duration_hours`` is 0
    (covers "no TimeEntry at all" and "entries that all have zero/NULL duration").
    Tenant-scoped: only the WO's own operations are considered and the duration sum is
    company-scoped. Read-only.
    """
    operations = [op for op in (work_order.operations or []) if op.id is not None]
    if not operations:
        return []

    op_ids = [op.id for op in operations]
    hours_by_op: dict[int, float] = {}
    for op_id, hours in (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.duration_hours), 0.0),
        )
        .filter(
            TimeEntry.operation_id.in_(op_ids),
            TimeEntry.company_id == company_id,
        )
        .group_by(TimeEntry.operation_id)
        .all()
    ):
        if op_id is not None:
            hours_by_op[op_id] = float(hours or 0)

    return [op for op in operations if hours_by_op.get(op.id, 0.0) <= 0]


def _no_labor_exceptions(operations: list[WorkOrderOperation]) -> list[QualityException]:
    return [
        QualityException(
            code=NO_LABOR_RECORDED_CODE,
            severity="medium",
            message=(
                f"Operation {op.operation_number or op.id} completed with no labor recorded "
                "(no time entry / zero duration); cost and hour actuals for this work order "
                "may be understated."
            ),
            reference_type="work_order_operation",
            reference_id=op.id,
        )
        for op in operations
    ]


def evaluate_and_record_labor_data_quality(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    audit: AuditService,
    user: Optional[User] = None,
    source: str = "completion",
) -> list[QualityException]:
    """Detect + record ``no_labor_recorded`` for a completing WO; return the exceptions.

    Called on the live completion paths when a WO reaches COMPLETE. Read-only detection,
    then -- if any operation recorded zero labor -- records the bypass via the shared
    Batch-4 helper (audit row + warning event) and returns the ``QualityException``s so
    the caller can merge them into the ``quality_exceptions`` response field. Fires
    regardless of the cost-rollup flag. Warn-only: never raises on a flagged operation.
    """
    flagged = _operations_with_no_labor(db, work_order, company_id)
    if not flagged:
        return []
    exceptions = _no_labor_exceptions(flagged)
    record_completion_quality_exceptions(
        db,
        company_id=company_id,
        work_order=work_order,
        operation=None,
        exceptions=exceptions,
        audit=audit,
        user=user,
        source=source,
    )
    return exceptions


def record_reconcile_labor_data_quality(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    audit: AuditService,
    user: Optional[User] = None,
) -> None:
    """Record ``no_labor_recorded`` for a reconcile-driven WO completion (read-safe).

    The reconcile-on-read counterpart: a WO can reach COMPLETE on a GET, and the
    data-quality signal should still fire. Wrapped so it can NEVER 500 a read -- a
    failure degrades the signal but the GET still serves. Only considers the WO's own
    operations (tenant-scoped). Caller must already have confirmed this WO reached
    COMPLETE on this reconcile.
    """
    try:
        # Only meaningful once the route is complete; if the WO somehow isn't complete,
        # skip (the live path will catch it on the next real completion).
        if not all(op.status == OperationStatus.COMPLETE for op in (work_order.operations or [])):
            return
        evaluate_and_record_labor_data_quality(
            db,
            company_id=company_id,
            work_order=work_order,
            audit=audit,
            user=user,
            source="reconcile_on_read",
        )
    except Exception:  # pragma: no cover - reads must never 500 on a data-quality signal
        logger.exception(
            "no_labor_recorded signal failed for WO %s (company %s) on reconcile; read is unaffected",
            getattr(work_order, "id", None),
            company_id,
        )
