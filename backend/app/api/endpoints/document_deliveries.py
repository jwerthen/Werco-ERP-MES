"""Reviewed quote/PO attachments with explicit SMTP send and durable outcomes."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.db.database import atomic_transaction, get_db
from app.models.user import User
from app.schemas.document_delivery import (
    DeliveryEntity,
    DocumentDeliveryPreview,
    DocumentDeliveryReconcile,
    DocumentDeliveryResponse,
    DocumentDeliverySend,
)
from app.services.audit_service import AuditService
from app.services.document_delivery_service import DocumentDeliveryService

router = APIRouter()


def service(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    return DocumentDeliveryService(db, company_id, user, audit)


@router.get('', response_model=list[DocumentDeliveryResponse])
def list_document_deliveries(
    entity_type: DeliveryEntity, entity_id: int = Query(gt=0), delivery: DocumentDeliveryService = Depends(service)
):
    """Last 50 delivery snapshots/attempts, newest first, for this company/source."""
    return delivery.history(entity_type, entity_id)


@router.post('/preview', response_model=DocumentDeliveryResponse)
def prepare_document_delivery(payload: DocumentDeliveryPreview, delivery: DocumentDeliveryService = Depends(service)):
    with atomic_transaction(delivery.db):
        result = delivery.preview(payload.entity_type, payload.entity_id, new_attempt=payload.new_attempt)
    return result


@router.get('/{delivery_id}', response_model=DocumentDeliveryResponse)
def get_document_delivery(delivery_id: int, delivery: DocumentDeliveryService = Depends(service)):
    return delivery.response(delivery.get(delivery_id))


@router.get('/{delivery_id}/attachment')
def get_document_delivery_attachment(delivery_id: int, delivery: DocumentDeliveryService = Depends(service)):
    record = delivery.get(delivery_id)
    # Server-generated filename, stripped of header metacharacters as a backstop
    # for legacy document numbers that predate input validation.
    name = record.attachment_name.replace('"', '').replace('\r', '').replace('\n', '')
    return Response(
        record.attachment,
        media_type='application/pdf',
        headers={
            'Content-Disposition': f'inline; filename="{name}"',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.post('/{delivery_id}/send', response_model=DocumentDeliveryResponse)
async def send_document_delivery(
    delivery_id: int, payload: DocumentDeliverySend, delivery: DocumentDeliveryService = Depends(service)
):
    try:
        return await delivery.send(delivery_id, payload)
    except IntegrityError as exc:
        delivery.db.rollback()
        raise HTTPException(
            409, 'This delivery request conflicts with an existing attempt. Refresh its status.'
        ) from exc
    except Exception:
        delivery.db.rollback()
        raise


@router.post('/{delivery_id}/reconcile', response_model=DocumentDeliveryResponse)
def reconcile_document_delivery(
    delivery_id: int, payload: DocumentDeliveryReconcile, delivery: DocumentDeliveryService = Depends(service)
):
    """Record a manager's verified outcome; never calls SMTP or claims a receipt."""
    with atomic_transaction(delivery.db):
        return delivery.reconcile(delivery_id, payload)
