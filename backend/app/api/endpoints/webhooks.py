from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.webhook import Webhook
from app.schemas.webhook import (
    WebhookCreate,
    WebhookUpdate,
    WebhookResponse,
    WebhookDeliveryResponse,
    WebhookTestPayload
)
from app.services.webhook_service import WebhookService
from app.core.queue import enqueue_job
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=List[WebhookResponse])
def list_webhooks(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all webhooks (admin only)"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhooks = db.query(Webhook).offset(skip).limit(limit).all()
    return webhooks


@router.post("", response_model=WebhookResponse, status_code=201)
def create_webhook(
    webhook_data: WebhookCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new webhook (admin only)"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook_service = WebhookService(db)

    webhook = webhook_service.create_webhook(
        name=webhook_data.name,
        url=webhook_data.url,
        events=webhook_data.events,
        secret=webhook_data.secret,
        description=webhook_data.description,
        created_by=current_user.username
    )

    return webhook


@router.get("/{webhook_id}", response_model=WebhookResponse)
def get_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get webhook by ID"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    return webhook


@router.put("/{webhook_id}", response_model=WebhookResponse)
def update_webhook(
    webhook_id: int,
    webhook_data: WebhookUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update webhook"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    # Update fields
    if webhook_data.name is not None:
        webhook.name = webhook_data.name
    if webhook_data.url is not None:
        webhook.url = webhook_data.url
    if webhook_data.events is not None:
        webhook.events = webhook_data.events
    if webhook_data.description is not None:
        webhook.description = webhook_data.description
    if webhook_data.is_active is not None:
        webhook.is_active = webhook_data.is_active

    db.commit()
    db.refresh(webhook)

    return webhook


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete webhook"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    db.delete(webhook)
    db.commit()

    return None


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: int,
    test_payload: WebhookTestPayload = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Send test payload to webhook"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    # Send test webhook
    payload = test_payload.test_data if test_payload else {"message": "Test webhook"}

    await enqueue_job(
        "send_webhook_job",
        webhook_id=webhook_id,
        event="test",
        payload=payload
    )

    return {"message": "Test webhook queued for delivery"}


@router.get("/{webhook_id}/deliveries", response_model=List[WebhookDeliveryResponse])
def get_webhook_deliveries(
    webhook_id: int,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get delivery history for webhook"""

    if current_user.role not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    webhook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    webhook_service = WebhookService(db)
    deliveries = webhook_service.get_deliveries(webhook_id, limit=limit)

    return deliveries
