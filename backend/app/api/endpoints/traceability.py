"""Tenant-scoped traceability routes using the shared genealogy read service."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.services import traceability_service
from app.services.traceability_service import LotTraceResponse

router = APIRouter()


@router.get('/lot/{lot_number}', response_model=LotTraceResponse)
def trace_lot(
    lot_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return traceability_service.trace_lot(lot_number, db, company_id)


@router.get('/serial/{serial_number}')
def trace_serial(
    serial_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return traceability_service.trace_serial(serial_number, db, company_id)


@router.get('/search')
def search_lots(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    return traceability_service.search_lots(q, db, company_id)
