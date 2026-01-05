import httpx
import hmac
import hashlib
import json
from datetime import datetime
from app.db.session import SessionLocal
from app.services.webhook_service import WebhookService
from app.models.webhook import Webhook
import logging

logger = logging.getLogger(__name__)

# Rate limiting: max 100 deliveries per minute per webhook
RATE_LIMIT = 100
RATE_LIMIT_WINDOW = 60


async def send_webhook_task(webhook_id: int, event: str, payload: dict):
    """
    Send webhook delivery

    Args:
        webhook_id: Webhook ID
        event: Event name
        payload: Event payload
    """
    db = SessionLocal()
    try:
        webhook_service = WebhookService(db)

        # Get webhook
        webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
        if not webhook or not webhook.is_active:
            logger.warning(f"Webhook {webhook_id} not found or inactive")
            return {"delivered": False, "error": "Webhook not found or inactive"}

        # Check rate limit
        if not await _check_rate_limit(webhook_id):
            logger.warning(f"Rate limit exceeded for webhook {webhook_id}")
            webhook_service.record_delivery(
                webhook_id=webhook_id,
                event=event,
                payload=payload,
                error="Rate limit exceeded",
                delivered=False
            )
            return {"delivered": False, "error": "Rate limit exceeded"}

        # Prepare payload
        full_payload = {
            "event": event,
            "timestamp": datetime.utcnow().isoformat(),
            "data": payload
        }

        # Get secret and sign payload
        secret = webhook_service.get_secret(webhook)
        signature = _sign_payload(full_payload, secret)

        # Send HTTP request
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": event,
            "X-Webhook-Signature": signature,
            "User-Agent": "Werco-ERP-Webhook/1.0"
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    webhook.url,
                    json=full_payload,
                    headers=headers
                )

                # Record successful delivery
                if response.status_code in [200, 201, 202, 204]:
                    webhook_service.record_delivery(
                        webhook_id=webhook_id,
                        event=event,
                        payload=full_payload,
                        status_code=response.status_code,
                        response_body=response.text[:1000],  # First 1000 chars
                        delivered=True
                    )

                    # Reset failure count on success
                    webhook.failed_deliveries = 0
                    db.commit()

                    logger.info(f"Webhook {webhook_id} delivered successfully to {webhook.url}")
                    return {"delivered": True, "status_code": response.status_code}

                else:
                    # Record failed delivery
                    webhook_service.record_delivery(
                        webhook_id=webhook_id,
                        event=event,
                        payload=full_payload,
                        status_code=response.status_code,
                        response_body=response.text[:1000],
                        error=f"HTTP {response.status_code}",
                        delivered=False
                    )

                    logger.warning(f"Webhook {webhook_id} failed: HTTP {response.status_code}")
                    return {"delivered": False, "status_code": response.status_code}

            except httpx.TimeoutException as e:
                webhook_service.record_delivery(
                    webhook_id=webhook_id,
                    event=event,
                    payload=full_payload,
                    error="Request timeout",
                    delivered=False
                )
                logger.error(f"Webhook {webhook_id} timeout: {e}")
                return {"delivered": False, "error": "timeout"}

            except httpx.RequestError as e:
                webhook_service.record_delivery(
                    webhook_id=webhook_id,
                    event=event,
                    payload=full_payload,
                    error=str(e)[:500],
                    delivered=False
                )
                logger.error(f"Webhook {webhook_id} request error: {e}")
                return {"delivered": False, "error": str(e)}

    except Exception as e:
        logger.error(f"Webhook job failed: {e}")
        raise
    finally:
        db.close()


def _sign_payload(payload: dict, secret: str) -> str:
    """Sign webhook payload with HMAC-SHA256"""
    payload_str = json.dumps(payload, sort_keys=True)
    signature = hmac.new(
        secret.encode(),
        payload_str.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"


async def _check_rate_limit(webhook_id: int) -> bool:
    """Check if webhook is within rate limit"""
    # TODO: Implement Redis-based rate limiting
    # For now, always allow
    return True
