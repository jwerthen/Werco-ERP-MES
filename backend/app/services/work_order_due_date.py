"""The one rule for changing a work order's due date, and the one write that applies it.

Two doors move ``WorkOrder.due_date``: the generic ``PUT /work-orders/{id}`` (its blind
``setattr`` loop) and Hank's reviewed ``change_due_date`` task. Both judge the change with
:func:`assert_due_date_change_allowed`, so the refusal text and the rule cannot drift apart.

The rule: a finished job's due date is its PROMISE date. On-time delivery scores against
``coalesce(must_ship_by, due_date)``, so moving it on a COMPLETE / CLOSED / CANCELLED work order
silently rewrites a delivery result that is already recorded -- and the rewrite is
indistinguishable, after the fact, from the job having always been due then. It is refused 409.

Deliberately NARROW, exactly as the PUT has always applied it: it judges only the due date (other
fields stay editable at any status), it refuses only an actual CHANGE (re-sending the stored value
on a terminal work order is idempotent), and it reads the work order's CURRENT status, before any
write in the same request.

Nothing here commits, audits, emits or broadcasts on its own: :func:`apply_due_date_change`
sets the column and flushes, and its callers own the transaction, the audit row and the event.
:func:`emit_due_date_changed_event` is the event Hank's door writes, in the PUT's shape.
"""

import logging
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.work_order import WorkOrder
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

logger = logging.getLogger(__name__)


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else status


def due_date_change_refusal(work_order: WorkOrder, new_due_date: Optional[date]) -> Optional[str]:
    """The 409 text when moving ``work_order``'s due date to ``new_due_date`` is refused, else None.

    Pure: reads the work order's current status and due date, writes nothing.
    """
    if work_order.status in TERMINAL_WO_STATUSES and new_due_date != work_order.due_date:
        return (
            f"cannot change the due date of a work order in terminal status '{_status_value(work_order.status)}': "
            "its due date is the promise date its delivery performance was scored against."
        )
    return None


def assert_due_date_change_allowed(work_order: WorkOrder, new_due_date: Optional[date]) -> None:
    """Raise 409 when the due-date rule refuses the change. Writes nothing."""
    refusal = due_date_change_refusal(work_order, new_due_date)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


def apply_due_date_change(db: Session, work_order: WorkOrder, new_due_date: Optional[date]) -> None:
    """Judge the change, set ``due_date`` and flush. Never commits, audits, emits or broadcasts.

    Only ``due_date`` is written -- never ``must_ship_by``, the promise date that takes precedence
    over it. The flush bumps the work order's ``version`` (``version_id_col``), so a concurrent
    stale write surfaces as ``StaleDataError`` (409) rather than a lost update.
    """
    assert_due_date_change_allowed(work_order, new_due_date)
    work_order.due_date = new_due_date
    db.flush()


def emit_due_date_changed_event(db: Session, *, company_id: int, actor: User, work_order: WorkOrder) -> None:
    """Emit ``work_order_updated`` for a due-date move, BEST-EFFORT, in its own savepoint.

    The payload is the shape ``PUT /work-orders/{id}`` emits (``_emit_work_order_event``): the
    work-order number, its status, ``updated_fields`` and ``operations_returned_to_pending``. It
    deliberately carries NO reason: ``reason`` is an email detail key
    (``notification_dispatch._DETAIL_KEYS``), and the reason lives on the audit row instead.

    The savepoint is the ``comment_service._emit_comment_events`` pattern: a failed event flush
    rolls back only the savepoint, so the caller's transaction (and its required audit rows)
    stays usable rather than failing later with ``PendingRollbackError``.
    """
    savepoint = db.begin_nested()
    try:
        event = OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="work_order_updated",
            source_module="work_orders",
            entity_type="work_order",
            entity_id=work_order.id,
            work_order_id=work_order.id,
            user_id=actor.id,
            severity="info",
            event_payload={
                "work_order_number": work_order.work_order_number,
                "status": _status_value(work_order.status),
                "updated_fields": ["due_date"],
                "operations_returned_to_pending": 0,
            },
        )
        if event is None:
            raise RuntimeError(f"work_order_updated emit failed for work order {work_order.id}")
        savepoint.commit()
    except Exception:  # noqa: BLE001 - telemetry must never fail the due-date change it describes
        logger.warning(
            "work_order_updated event skipped (best-effort): company_id=%s work_order_id=%s",
            company_id,
            work_order.id,
            exc_info=True,
        )
        try:
            savepoint.rollback()
        except Exception:  # noqa: BLE001 - already failing; the outer transaction decides
            logger.warning("work_order_updated savepoint rollback failed", exc_info=True)
