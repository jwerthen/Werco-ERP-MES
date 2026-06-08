"""Outbound completion signals (notifications + webhooks) for a finished work order.

Batch 5 / rank 8 (EVT-3). When a work order reaches COMPLETE or CLOSED on a live
completion path, the request handler enqueues ``dispatch_work_order_completion_signals_job``
rather than calling the email / webhook machinery inline -- outbound I/O must never
block (or fail) the completion request. This task runs in the ARQ worker with its
own DB session and:

* sends the ``WO_COMPLETED`` notification to the company's supervisors / managers
  (and the WO creator) -- recipients are TENANT-SCOPED via ``company_id`` so a
  completion never notifies another tenant's users;
* dispatches the ``work_order.completed`` / ``work_order.closed`` webhook event,
  TENANT-SCOPED via ``WebhookService.dispatch_event(..., company_id=...)`` so only
  the OWNING company's registered endpoints are called (cross-tenant delivery would
  leak the work order's data).

Both legs are config-gated: no recipients / no subscribed endpoints == no-op. Each
leg is independently guarded so one failing channel cannot suppress the other, and a
total failure only fails THIS background job, never the (already-committed) completion.
"""

import logging
from datetime import datetime
from typing import Optional

from app.db.session import SessionLocal
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services.notification_service import (
    NotificationEvent,
    NotificationService,
    get_notification_recipients,
)
from app.services.operational_event_service import redact_event_payload
from app.services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

# Map the WO terminal status to the webhook event name (EVT-3).
_WEBHOOK_EVENT_BY_STATUS = {
    "COMPLETE": "work_order.completed",
    "CLOSED": "work_order.closed",
}


def _notification_recipients(db, company_id: int, created_by: Optional[int]) -> list[User]:
    """Tenant-scoped recipients for a WO_COMPLETED notification.

    Supervisors + managers in the active company, plus the WO creator when known.
    Every query is company-scoped (invariant #1) -- a completion in one tenant can
    never notify another tenant's users. De-duplicated by user id.
    """
    recipients: dict[int, User] = {}
    for role in (UserRole.SUPERVISOR, UserRole.MANAGER):
        role_value = role.value if hasattr(role, "value") else role
        for user in get_notification_recipients(db, role=role_value, company_id=company_id):
            recipients[user.id] = user
    if created_by is not None:
        creator = (
            db.query(User)
            .filter(User.id == created_by, User.company_id == company_id, User.is_active == True)  # noqa: E712
            .first()
        )
        if creator is not None:
            recipients[creator.id] = creator
    return list(recipients.values())


async def dispatch_work_order_completion_signals_task(
    work_order_id: int,
    company_id: int,
    status: str,
) -> dict:
    """Send the WO_COMPLETED notification + work_order.(completed|closed) webhook.

    ``status`` is the WO's terminal status ("COMPLETE" or "CLOSED"). All lookups
    are scoped to ``company_id``; the work order is re-loaded under that scope so a
    stale / cross-tenant id is a safe no-op.
    """
    db = SessionLocal()
    notified = 0
    dispatched = False
    try:
        work_order = (
            db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
        )
        if not work_order:
            logger.warning("completion signals: work order %s not found for company %s", work_order_id, company_id)
            return {"notified": 0, "webhook_dispatched": False, "reason": "work_order_not_found"}

        # Internal notification payload -- stays inside the tenant (email to the
        # company's own users), so it may carry richer context like customer_name.
        payload = {
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "status": status,
            "company_id": company_id,
            "part_id": work_order.part_id,
            "customer_name": work_order.customer_name,
            "quantity_complete": float(work_order.quantity_complete or 0),
            "quantity_scrapped": float(work_order.quantity_scrapped or 0),
        }

        # --- Notification leg (tenant-scoped recipients) ---------------------
        try:
            recipients = _notification_recipients(db, company_id, work_order.created_by)
            if recipients:
                notification_service = NotificationService(db)
                await notification_service.send_notification(
                    event_type=NotificationEvent.WO_COMPLETED,
                    users=recipients,
                    subject=f"Work order {work_order.work_order_number} {status.lower()}",
                    context=payload,
                    template="wo_completed",
                    related_type="WorkOrder",
                    related_id=work_order.id,
                )
                notified = len(recipients)
        except Exception:  # pragma: no cover - one channel must not block the other
            logger.exception(
                "completion signals: notification leg failed for WO %s (company %s)",
                work_order_id,
                company_id,
            )

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
            except Exception:  # pragma: no cover - webhook failure must not block notify
                logger.exception(
                    "completion signals: webhook leg failed for WO %s (company %s)",
                    work_order_id,
                    company_id,
                )

        return {"notified": notified, "webhook_dispatched": dispatched, "status": status}
    finally:
        db.close()
