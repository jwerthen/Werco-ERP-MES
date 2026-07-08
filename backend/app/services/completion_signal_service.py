"""Request-side helper that enqueues outbound completion signals.

Batch 5 / rank 8 (EVT-3). The completion request handlers call
``enqueue_work_order_completion_signals`` once a work order reaches COMPLETE or
CLOSED. It enqueues the ARQ ``dispatch_work_order_completion_signals_job`` (which
runs the email + webhook legs in the worker, tenant-scoped) WITHOUT blocking the
request thread and WITHOUT ever raising -- a signal-enqueue failure must not fail an
already-committed completion (Batch 5 correctness rule). Kept out of
``work_order_state_service`` on purpose: that module is a pure, I/O-free rules
library; outbound side-effects live here.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.queue import enqueue_job_best_effort
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)


def emit_operation_completed_event(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    user_id: Optional[int],
    source_module: str,
    source: Optional[str] = None,
) -> None:
    """Emit an ``operation_completed`` OperationalEvent (EVT-1/EVT-2).

    In-process, tenant-scoped (``OperationalEventService.emit`` validates the
    WO/op belong to ``company_id``). Best-effort: a signal failure must not fail
    the completion, so emission errors are swallowed and logged.

    ``source`` is the A0.1 adoption-telemetry client channel
    (kiosk/desktop/scanner/import/backfill) when the triggering request supplied
    one; None means unknown/not reported (e.g. office or reconcile paths).
    """
    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type="operation_completed",
            source_module=source_module,
            entity_type="work_order_operation",
            entity_id=operation.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            user_id=user_id,
            severity="info",
            event_payload={
                "work_order_number": work_order.work_order_number,
                "operation_number": operation.operation_number,
                "quantity_complete": float(operation.quantity_complete or 0),
                "quantity_scrapped": float(operation.quantity_scrapped or 0),
                "source": source,
            },
        )
    except Exception:  # pragma: no cover - signal failure must not fail completion
        logger.exception("operation_completed event emit failed for op %s (company %s)", operation.id, company_id)


def emit_work_order_completed_event(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    user_id: Optional[int],
    source_module: str,
    source: Optional[str] = None,
) -> None:
    """Emit a ``work_order_completed`` OperationalEvent (EVT-1/EVT-2).

    In-process, tenant-scoped, best-effort (see ``emit_operation_completed_event``,
    including the A0.1 ``source`` channel semantics).

    Also captures AI learning outcomes (OTD / scrap / cost) so the always-on
    learning loop gets fuel without a human posting ``/ai/outcomes``.
    """
    try:
        status = work_order.status.value if hasattr(work_order.status, "value") else work_order.status
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type="work_order_completed",
            source_module=source_module,
            entity_type="work_order",
            entity_id=work_order.id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="info",
            event_payload={
                "work_order_number": work_order.work_order_number,
                "status": status,
                "quantity_complete": float(work_order.quantity_complete or 0),
                "quantity_scrapped": float(work_order.quantity_scrapped or 0),
                "source": source,
            },
        )
    except Exception:  # pragma: no cover - signal failure must not fail completion
        logger.exception("work_order_completed event emit failed for WO %s (company %s)", work_order.id, company_id)

    # Phase 0 always-on AI: auto-record outcomes. Nested try so learning never
    # interferes with the operational event path above (or the completion itself).
    try:
        from app.services.ai_outcome_capture_service import record_work_order_completion_outcomes

        record_work_order_completion_outcomes(
            db,
            company_id=company_id,
            work_order=work_order,
            user_id=user_id,
            source_module=source_module or "production",
        )
    except Exception:  # pragma: no cover - signal failure must not fail completion
        logger.exception(
            "AI WO outcome capture failed for WO %s (company %s)", work_order.id, company_id
        )


def record_parent_children_complete(
    db: Session,
    *,
    parent_work_order: WorkOrder,
    child_work_order: WorkOrder,
    company_id: int,
    user_id: Optional[int],
    audit: AuditService,
    source: str = "completion",
) -> None:
    """Record that the LAST laser child of a parent WO has completed (G1 advance).

    Decision: this is a SIGNAL only -- we do NOT auto-complete the parent. Parent and
    child work orders are NOT operation-coupled in the data model, so mutating the
    parent's route from a child completion would be wrong. Instead we leave a
    tamper-evident audit row + one ``child_work_orders_complete`` OperationalEvent so
    the parent surfaces as "all children done, ready to advance".

    No-double-fire: ``find_parent_to_advance`` returns the parent only when ALL laser
    children are terminal, which becomes true exactly once (when the last child
    flips). Completion handlers are idempotent and reconcile-on-read never re-flips a
    terminal child, so this records at most once per parent.

    Best-effort: ``audit.log`` already swallows its own failures and only flushes
    (never commits) so the row commits atomically with the caller's unit of work; the
    OperationalEvent emit is additionally wrapped so a signal failure can never break
    the child completion the caller is committing.
    """
    audit.log(
        action="CHILD_WORK_ORDERS_COMPLETE",
        resource_type="work_order",
        resource_id=parent_work_order.id,
        resource_identifier=parent_work_order.work_order_number,
        description=(
            f"All child work orders of {parent_work_order.work_order_number} are complete "
            f"(last child {child_work_order.work_order_number} reached terminal status)."
        ),
        extra_data={
            "source": source,
            "child_work_order_id": child_work_order.id,
            "parent_work_order_id": parent_work_order.id,
        },
        company_id=company_id,
    )

    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type="child_work_orders_complete",
            source_module="completion_signal",
            entity_type="work_order",
            entity_id=parent_work_order.id,
            work_order_id=parent_work_order.id,
            user_id=user_id,
            severity="info",
            event_payload={
                "source": source,
                "parent_work_order_number": parent_work_order.work_order_number,
                "child_work_order_id": child_work_order.id,
                "child_work_order_number": child_work_order.work_order_number,
            },
        )
    except Exception:  # pragma: no cover - a signal failure must not break completion
        logger.exception(
            "child_work_orders_complete event emit failed for parent WO %s (company %s)",
            parent_work_order.id,
            company_id,
        )


def enqueue_work_order_completion_signals(*, work_order_id: int, company_id: int, status: str) -> None:
    """Enqueue notification + webhook dispatch for a finished work order.

    Best-effort: any failure to enqueue is swallowed (logged) so it can never turn
    a successful completion into an error. Call AFTER the completion has committed.

    ``status`` is the terminal WO status: "COMPLETE" (op/WO completion paths) or
    "CLOSED" (shipping ``mark_shipped``).
    """
    enqueue_job_best_effort(
        "dispatch_work_order_completion_signals_job",
        work_order_id=work_order_id,
        company_id=company_id,
        status=status,
    )
