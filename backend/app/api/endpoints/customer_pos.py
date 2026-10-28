"""Read-only dashboards for customer POs, distinct from supplier purchasing."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.api.endpoints.user_workspaces import require_workspace_access
from app.db.database import get_db
from app.models.user import User
from app.schemas.customer_po import (
    CustomerPODashboardResponse,
    CustomerPOListResponse,
    CustomerPOSuggestionsResponse,
)
from app.services.customer_po_service import (
    customer_po_dashboard,
    customer_po_suggestions,
    list_customer_pos,
)

router = APIRouter()


@router.get("/customer-po-suggestions", response_model=CustomerPOSuggestionsResponse)
def suggest_customer_purchase_orders(
    response: Response,
    search: str = Query("", max_length=100),
    customer_name: str | None = Query(None, max_length=255),
    limit: int = Query(10, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    require_workspace_access(db, current_user, company_id, "work-orders")
    response.headers["Cache-Control"] = "no-store"
    return customer_po_suggestions(db, company_id, search=search, customer_name=customer_name, limit=limit)


@router.get("/customer-pos", response_model=CustomerPOListResponse)
def customer_purchase_orders(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=255),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    require_workspace_access(db, current_user, company_id, "work-orders")
    response.headers["Cache-Control"] = "no-store"
    return list_customer_pos(db, company_id, skip=skip, limit=limit, search=search)


@router.get("/customer-po-dashboard", response_model=CustomerPODashboardResponse)
def customer_purchase_order_dashboard(
    response: Response,
    customer_po: str = Query(..., min_length=1, max_length=100),
    customer_name: str = Query("", max_length=255),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    require_workspace_access(db, current_user, company_id, "work-orders")
    if not customer_po.strip():
        raise HTTPException(status_code=422, detail="Customer PO must not be blank")
    response.headers["Cache-Control"] = "no-store"
    return customer_po_dashboard(
        db,
        company_id,
        customer_name=customer_name,
        customer_po=customer_po,
        skip=skip,
        limit=limit,
    )
