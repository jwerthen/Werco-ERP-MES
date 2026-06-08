import logging
import os
from typing import Dict, Optional

from cryptography.fernet import Fernet
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.queue import enqueue_job
from app.models.webhook import Webhook, WebhookDelivery

logger = logging.getLogger(__name__)

# Encryption key for webhook secrets
WEBHOOK_ENCRYPTION_KEY = os.getenv("WEBHOOK_ENCRYPTION_KEY", Fernet.generate_key().decode())
cipher = Fernet(WEBHOOK_ENCRYPTION_KEY.encode())


class WebhookService:
    """Webhook management and delivery service"""

    def __init__(self, db: Session):
        self.db = db

    def create_webhook(
        self,
        name: str,
        url: str,
        events: list,
        secret: str,
        description: str = None,
        created_by: str = None,
        company_id: Optional[int] = None,
    ) -> Webhook:
        """Create a new webhook subscription.

        COMPLIANCE (invariant #1): a webhook MUST be stamped with the owning
        tenant so ``get_webhooks_for_event`` can scope dispatch to it. ``Webhook``
        carries a non-null ``company_id`` (``TenantMixin``); pass the active
        company when creating one.
        """

        # Encrypt secret
        encrypted_secret = cipher.encrypt(secret.encode()).decode()

        webhook = Webhook(
            name=name,
            url=url,
            events=events,
            secret=encrypted_secret,
            description=description,
            created_by=created_by,
            company_id=company_id,
        )

        self.db.add(webhook)
        self.db.commit()
        self.db.refresh(webhook)

        return webhook

    def get_secret(self, webhook: Webhook) -> str:
        """Decrypt webhook secret"""
        return cipher.decrypt(webhook.secret.encode()).decode()

    def get_webhooks_for_event(self, event: str, company_id: Optional[int] = None) -> list[Webhook]:
        """Get all active webhooks subscribed to an event.

        COMPLIANCE (invariant #1, tenant isolation): ``Webhook`` carries a
        ``company_id`` via ``TenantMixin``. When ``company_id`` is provided the
        lookup is scoped to that single tenant so a company only ever receives
        events for its OWN registered endpoints -- dispatching a completion to a
        webhook registered by another company would leak that work order's data
        cross-tenant. ``None`` is retained only for the (un-scoped) admin/test
        listing path; every live dispatch MUST pass the active company.
        """
        query = self.db.query(Webhook).filter(Webhook.is_active == True)  # noqa: E712
        if company_id is not None:
            query = query.filter(Webhook.company_id == company_id)
        webhooks = query.all()

        # Filter webhooks that subscribe to this event
        matching = []
        for webhook in webhooks:
            if event in webhook.events:
                matching.append(webhook)

        return matching

    async def dispatch_event(self, event: str, payload: Dict, company_id: int):
        """
        Dispatch event to all subscribed webhooks

        Args:
            event: Event name (e.g., "work_order.created")
            payload: Event data
            company_id: Active tenant. COMPLIANCE (foot-gun guard): scopes
                ``get_webhooks_for_event`` so only THIS company's registered
                endpoints are notified. ``company_id`` is REQUIRED on the dispatch
                boundary -- an unscoped dispatch would fan one tenant's event out to
                every company's webhooks (a cross-tenant data leak), so we refuse it
                outright rather than silently broadcasting.
        """
        if company_id is None:
            raise ValueError("dispatch_event requires a company_id; refusing an unscoped (cross-tenant) dispatch")
        webhooks = self.get_webhooks_for_event(event, company_id=company_id)

        if not webhooks:
            logger.debug(f"No webhooks subscribed to event: {event}")
            return

        # Enqueue delivery jobs for each webhook. Pass company_id so the delivery
        # task stamps a tenant-consistent WebhookDelivery row (the webhook is loaded
        # under this company, so webhook.company_id == company_id here).
        for webhook in webhooks:
            await enqueue_job(
                "send_webhook_job", webhook_id=webhook.id, event=event, payload=payload, company_id=company_id
            )

        logger.info(f"Dispatched event {event} to {len(webhooks)} webhooks")

    def record_delivery(
        self,
        webhook_id: int,
        event: str,
        payload: Dict,
        status_code: int = None,
        response_body: str = None,
        error: str = None,
        delivered: bool = False,
        company_id: Optional[int] = None,
    ) -> WebhookDelivery:
        """Record webhook delivery attempt.

        COMPLIANCE (invariant #1): ``WebhookDelivery`` is a ``TenantMixin`` table with
        a non-null ``company_id``, so the delivery row MUST be stamped or the INSERT
        fails on Postgres. The owning webhook is the source of truth for the tenant --
        we derive ``company_id`` from the loaded ``webhook.company_id`` (and only fall
        back to the caller-supplied ``company_id`` if the webhook can't be reloaded),
        keeping the delivery row and the webhook's failure counters tenant-consistent.
        """

        # Load the owning webhook first so the delivery row can inherit its tenant.
        webhook = self.db.query(Webhook).filter(Webhook.id == webhook_id).first()
        delivery_company_id = webhook.company_id if webhook is not None else company_id

        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event=event,
            payload=payload,
            status_code=status_code,
            response_body=response_body,
            error=error,
            delivered=delivered,
            company_id=delivery_company_id,
        )

        self.db.add(delivery)

        # Update webhook failure tracking
        if webhook:
            if not delivered:
                webhook.failed_deliveries += 1
                webhook.last_failure = func.now()

                # Disable webhook after too many consecutive failures
                if webhook.failed_deliveries >= 10:
                    webhook.is_active = False
                    logger.warning(f"Disabled webhook {webhook_id} after 10 consecutive failures")
            else:
                # Reset failure counter on successful delivery so transient
                # outages don't permanently disable a healthy webhook.
                if webhook.failed_deliveries:
                    webhook.failed_deliveries = 0

        self.db.commit()
        self.db.refresh(delivery)

        return delivery

    def get_deliveries(self, webhook_id: int, limit: int = 50, delivered_only: bool = False) -> list[WebhookDelivery]:
        """Get delivery history for a webhook"""

        query = self.db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == webhook_id)

        if delivered_only:
            query = query.filter(WebhookDelivery.delivered == True)

        return query.order_by(WebhookDelivery.sent_at.desc()).limit(limit).all()
