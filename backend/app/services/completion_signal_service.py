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
) -> None:
    """Emit an ``operation_completed`` OperationalEvent (EVT-1/EVT-2).

    In-process, tenant-scoped (``OperationalEventService.emit`` validates the
    WO/op belong to ``company_id``). Best-effort: a signal failure must not fail
    the completion, so emission errors are swallowed and logged.
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
) -> None:
    """Emit a ``work_order_completed`` OperationalEvent (EVT-1/EVT-2).

    In-process, tenant-scoped, best-effort (see ``emit_operation_completed_event``).
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
            },
        )
    except Exception:  # pragma: no cover - signal failure must not fail completion
        logger.exception("work_order_completed event emit failed for WO %s (company %s)", work_order.id, company_id)


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
