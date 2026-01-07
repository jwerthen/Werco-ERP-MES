from sqlalchemy.orm import Session
from typing import Dict, Optional
from app.models.webhook import Webhook, WebhookDelivery
from app.core.queue import enqueue_job
from cryptography.fernet import Fernet
import os
import logging

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
        created_by: str = None
    ) -> Webhook:
        """Create a new webhook subscription"""

        # Encrypt secret
        encrypted_secret = cipher.encrypt(secret.encode()).decode()

        webhook = Webhook(
            name=name,
            url=url,
            events=events,
            secret=encrypted_secret,
            description=description,
            created_by=created_by
        )

        self.db.add(webhook)
        self.db.commit()
        self.db.refresh(webhook)

        return webhook

    def get_secret(self, webhook: Webhook) -> str:
        """Decrypt webhook secret"""
        return cipher.decrypt(webhook.secret.encode()).decode()

    def get_webhooks_for_event(self, event: str) -> list[Webhook]:
        """Get all active webhooks subscribed to an event"""
        webhooks = self.db.query(Webhook).filter(
            Webhook.is_active == True
        ).all()

        # Filter webhooks that subscribe to this event
        matching = []
        for webhook in webhooks:
            if event in webhook.events:
                matching.append(webhook)

        return matching

    async def dispatch_event(self, event: str, payload: Dict):
        """
        Dispatch event to all subscribed webhooks

        Args:
            event: Event name (e.g., "work_order.created")
            payload: Event data
        """
        webhooks = self.get_webhooks_for_event(event)

        if not webhooks:
            logger.debug(f"No webhooks subscribed to event: {event}")
            return

        # Enqueue delivery jobs for each webhook
        for webhook in webhooks:
            await enqueue_job(
                "send_webhook_job",
                webhook_id=webhook.id,
                event=event,
                payload=payload
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
        delivered: bool = False
    ) -> WebhookDelivery:
        """Record webhook delivery attempt"""

        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event=event,
            payload=payload,
            status_code=status_code,
            response_body=response_body,
            error=error,
            delivered=delivered
        )

        self.db.add(delivery)

        # Update webhook failure count
        if not delivered:
            webhook = self.db.query(Webhook).filter(Webhook.id == webhook_id).first()
            if webhook:
                webhook.failed_deliveries += 1
                webhook.last_failure = func.now()

                # Disable webhook after too many failures
                if webhook.failed_deliveries >= 10:
                    webhook.is_active = False
                    logger.warning(f"Disabled webhook {webhook_id} after 10 consecutive failures")

        self.db.commit()
        self.db.refresh(delivery)

        return delivery

    def get_deliveries(
        self,
        webhook_id: int,
        limit: int = 50,
        delivered_only: bool = False
    ) -> list[WebhookDelivery]:
        """Get delivery history for a webhook"""

        query = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.webhook_id == webhook_id
        )

        if delivered_only:
            query = query.filter(WebhookDelivery.delivered == True)

        return query.order_by(
            WebhookDelivery.sent_at.desc()
        ).limit(limit).all()
