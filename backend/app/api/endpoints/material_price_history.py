"""Tenant-scoped purchase cost history, protected by effective purchasing access."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.part import PartType
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.schemas.material_price_history import (
    PriceHistoryDetailResponse,
    PriceHistoryListResponse,
    PriceHistorySort,
    PriceHistoryTrend,
)
from app.services.material_price_history_service import (
    get_price_history,
    list_price_history,
)

router = APIRouter()


def require_price_history_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
) -> User:
    if current_user.is_superuser or current_user.role == UserRole.PLATFORM_ADMIN:
        return current_user
    override = (
        db.query(RolePermission)
        .filter(
            RolePermission.company_id == company_id,
            RolePermission.role == current_user.role,
        )
        .first()
    )
    permissions = override.permissions if override is not None else DEFAULT_ROLE_PERMISSIONS.get(current_user.role, [])
    if not isinstance(permissions, list) or "purchasing:view" not in permissions:
        raise HTTPException(403, "Purchase price history requires purchasing:view")
    return current_user


@router.get("/price-history", response_model=PriceHistoryListResponse)
def list_material_price_history(
    search: str | None = Query(None, max_length=200),
    part_type: PartType | None = None,
    trend: PriceHistoryTrend = "all",
    sort: PriceHistorySort = "recent",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_price_history_read),
    company_id: int = Depends(get_current_company_id),
):
    """Items bought on committed POs, with latest-versus-previous PO costs.

    Includes any part type actually purchased, even inactive inventory. Counts
    apply search and type before the selected trend, and before pagination.
    Sparklines contain each item's latest 12 purchase orders chronologically.
    Currency is unknown; UOM is the current catalog UOM, not a historic snapshot.
    """
    return list_price_history(
        db,
        company_id,
        search=search,
        part_type=part_type,
        trend=trend,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/price-history/{part_id}", response_model=PriceHistoryDetailResponse)
def get_material_price_history(
    part_id: int = Path(..., gt=0),
    vendor_id: int | None = Query(None, gt=0),
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_price_history_read),
    company_id: int = Depends(get_current_company_id),
):
    """Full PO history table and filtered cost statistics for a purchased item.

    Dates are inclusive. Stats and consecutive-PO comparisons apply the vendor
    and date filters; part and supplier options retain all-time context. Table
    pagination never changes stats. Chart contains the latest 500 matching POs
    chronologically; chart_truncated identifies when older points are omitted.
    """
    if start_date and end_date and start_date > end_date:
        raise HTTPException(422, "Start date must be on or before end date")
    return get_price_history(
        db,
        company_id,
        part_id,
        vendor_id=vendor_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )
