"""Consumables checkout, stock availability, and employee usage history."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.user import User
from app.schemas.consumables import (
    ConsumableCheckoutCreate,
    ConsumableCheckoutList,
    ConsumableCheckoutResponse,
    ConsumableMetadata,
    ConsumableStockList,
)
from app.services import consumable_service
from app.services.audit_service import AuditService

router = APIRouter()


def history_filters(
    q: Optional[str] = Query(None, max_length=200),
    employee_id: Optional[int] = Query(None, gt=0),
    part_id: Optional[int] = Query(None, gt=0),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
):
    return dict(q=q, employee_id=employee_id, part_id=part_id, date_from=date_from, date_to=date_to)


@router.get("/metadata", response_model=ConsumableMetadata)
def metadata(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List names/departments of active and historical recipients and caller capabilities.

    Badge identifiers are authentication credentials and are never disclosed here.
    """
    return consumable_service.metadata(db, company_id, user)


@router.get("/stock", response_model=ConsumableStockList)
def stock(
    q: Optional[str] = Query(None, max_length=200),
    low_stock: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=250),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Page consumable stock with eligibility and work-order reservations applied."""
    return consumable_service.list_stock(db, company_id, q=q, low_stock=low_stock, skip=skip, limit=limit)


@router.post("/checkouts", response_model=ConsumableCheckoutResponse, status_code=201)
def checkout(
    payload: ConsumableCheckoutCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(consumable_service.MANAGE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Issue stock to an employee with server UTC time and authenticated issuer.

    Admin/Manager/Supervisor only. A request UUID identifies one immutable issue;
    retrying the same body returns its original result without another stock draw.
    Stock, ledger, employee attribution and required audit evidence commit together.
    """
    return consumable_service.checkout(db, company_id, user, audit, payload)


@router.get("/checkouts", response_model=ConsumableCheckoutList)
def history(
    filters: dict = Depends(history_filters),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=250),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Page immutable checkout history; date bounds are inclusive America/Chicago dates."""
    return consumable_service.list_history(db, company_id, skip=skip, limit=limit, **filters)


@router.get("/checkouts/export.csv")
def export(
    filters: dict = Depends(history_filters),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(consumable_service.EXPORT_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Export filtered CSV (Admin/Manager), audited before download; over 10,000 rows is refused."""
    return Response(
        content=consumable_service.export_history(db, company_id, audit, **filters),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="consumables-checkouts.csv"'},
    )
