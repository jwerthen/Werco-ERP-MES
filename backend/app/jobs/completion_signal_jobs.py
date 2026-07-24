"""Outbound completion-signal WEBHOOK dispatch for a finished work order.

Batch 5 / rank 8 (EVT-3). When a work order reaches COMPLETE or CLOSED on a live
completion path, the request handler enqueues ``dispatch_work_order_completion_signals_job``
rather than calling the webhook machinery inline -- outbound I/O must never block (or fail)
the completion request. This task runs in the ARQ worker with its own DB session and
dispatches the ``work_order.completed`` / ``work_order.closed`` webhook event,
TENANT-SCOPED via ``WebhookService.dispatch_event(..., company_id=...)`` so only the OWNING
company's registered endpoints are called (cross-tenant delivery would leak the work
order's data).

The wo.completed / wo.closed IN-APP + EMAIL notification leg is now owned by the
transactional-outbox notification pipeline (the emitted ``work_order_completed`` /
``work_order_closed`` OperationalEvents drive it), so it is deliberately NOT sent here -- a
second copy would double-fire. The webhook leg is config-gated (no subscribed endpoints ==
no-op) and its failure only fails THIS background job, never the (already-committed)
completion.
"""

import logging
from datetime import datetime

from app.db.session import SessionLocal
from app.models.work_order import WorkOrder
from app.services.operational_event_service import redact_event_payload
from app.services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

# Map the WO terminal status to the webhook event name (EVT-3).
_WEBHOOK_EVENT_BY_STATUS = {
    "COMPLETE": "work_order.completed",
    "CLOSED": "work_order.closed",
}


async def dispatch_work_order_completion_signals_task(
    work_order_id: int,
    company_id: int,
    status: str,
) -> dict:
    """Dispatch the work_order.(completed|closed) webhook for a finished work order.

    ``status`` is the WO's terminal status ("COMPLETE" or "CLOSED"). All lookups
    are scoped to ``company_id``; the work order is re-loaded under that scope so a
    stale / cross-tenant id is a safe no-op. The in-app/email notification is handled
    by the outbox pipeline, not here.
    """
    db = SessionLocal()
    dispatched = False
    try:
        work_order = (
            db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
        )
        if not work_order:
            logger.warning("completion signals: work order %s not found for company %s", work_order_id, company_id)
            return {"webhook_dispatched": False, "reason": "work_order_not_found"}

        # --- Webhook leg (tenant-scoped subscribers) -------------------------
        # CUI MINIMIZATION: the webhook payload EGRESSES to an arbitrary external,
        # subscriber-controlled URL, unlike the internal notification above. Ship only
        # the structured identifiers a subscriber legitimately needs to react and
        # re-fetch detail via the authenticated API (keyed on work_order_id) -- DROP
        # customer_name (the clearest CUI) and any free-text/notes fields. A richer
        # outbound payload must be an explicit opt-in / data-classification decision,
        # not the default. Then run it through redact_event_payload as a belt-and-
        # suspenders pass (catches any sensitively-named key + over-long strings),
        # exactly as the OperationalEvent path does.
        webhook_event = _WEBHOOK_EVENT_BY_STATUS.get(status)
        if webhook_event:
            try:
                webhook_payload = redact_event_payload(
                    {
                        "work_order_id": work_order.id,
                        "work_order_number": work_order.work_order_number,
                        "part_id": work_order.part_id,
                        "status": status,
                        "quantity_complete": float(work_order.quantity_complete or 0),
                        "quantity_scrapped": float(work_order.quantity_scrapped or 0),
                        "company_id": company_id,
                        "completed_at": datetime.utcnow().isoformat(),
                    }
                )
                await WebhookService(db).dispatch_event(webhook_event, webhook_payload, company_id=company_id)
                dispatched = True
            except Exception:  # pragma: no cover - webhook failure must not fail the job
                logger.exception(
                    "completion signals: webhook leg failed for WO %s (company %s)",
                    work_order_id,
                    company_id,
                )

        return {"webhook_dispatched": dispatched, "status": status}
    finally:
        db.close()
