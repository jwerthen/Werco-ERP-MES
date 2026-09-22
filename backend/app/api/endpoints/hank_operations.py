"""Read-only operational checks and source-linked knowledge; no inferred approval."""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_operations import HankOperationalReport
from app.services.hank_operations_service import HankOperationsService

router = APIRouter()


@router.get('/work-orders/{work_order_id}/readiness', response_model=HankOperationalReport)
def readiness(
    work_order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Check current permitted material, instruction, blocker and quality evidence without authorizing production."""
    return HankOperationsService(db, user, company_id).readiness(work_order_id)


@router.get('/work-orders/{work_order_id}/knowledge', response_model=HankOperationalReport)
def knowledge(
    work_order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Link released records and bounded historical prior-run notes, never promote notes into instructions."""
    return HankOperationsService(db, user, company_id).knowledge(work_order_id)


@router.get('/purchase-orders/{po_id}/impact', response_model=HankOperationalReport)
def purchasing_impact(
    po_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Explain outstanding supply and direct demand matches; return a draft supplier message without sending it."""
    return HankOperationsService(db, user, company_id).impact(po_id)


@router.get('/work-orders/{work_order_id}/shipping-packet', response_model=HankOperationalReport)
def shipping_packet(
    work_order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Assemble existing document and packing-slip links; never issue CoCs or dispatch goods."""
    return HankOperationsService(db, user, company_id).shipping_packet(work_order_id)


@router.get('/trace/{kind}/{value:path}', response_model=HankOperationalReport)
def trace(
    kind: Literal['lot', 'serial'],
    value: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read scoped recorded lot or serial genealogy, retaining historical evidence and stating coverage limits."""
    return HankOperationsService(db, user, company_id).trace(kind, value)
