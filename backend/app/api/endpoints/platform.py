"""
Platform administration endpoints for Werco oversight.
All endpoints require PLATFORM_ADMIN role.
Provides read-only cross-company browsing and company management.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from app.db.database import get_db
from app.models.company import Company
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_center import WorkCenter
from app.models.part import Part
from app.models.inventory import InventoryItem
from app.schemas.company import CompanyCreate, CompanyResponse, CompanyUpdate, CompanyListResponse
from app.schemas.user import UserResponse
from app.api.deps import require_platform_admin
from app.services.company_onboarding import onboard_company

router = APIRouter()


# --- Company Management ---

@router.get("/companies", response_model=List[CompanyListResponse], summary="List all companies")
def list_companies(
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """List all companies with summary metrics."""
    companies = db.query(Company).order_by(Company.name).all()

    results = []
    for company in companies:
        user_count = db.query(func.count(User.id)).filter(
            User.company_id == company.id, User.is_active == True
        ).scalar()
        active_wos = db.query(func.count(WorkOrder.id)).filter(
            WorkOrder.company_id == company.id,
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
        ).scalar()
        results.append(CompanyListResponse(
            id=company.id,
            name=company.name,
            slug=company.slug,
            logo_url=company.logo_url,
            is_active=company.is_active,
            user_count=user_count,
            active_work_orders=active_wos,
        ))

    return results


@router.post("/companies", response_model=CompanyResponse, summary="Create a company")
def create_company(
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Create a new company with initial admin user."""
    # Check email uniqueness
    if db.query(User).filter(User.email == payload.admin_email).first():
        raise HTTPException(status_code=400, detail="Admin email already registered")

    company, admin_user = onboard_company(
        db=db,
        name=payload.name,
        slug=payload.slug,
        admin_email=payload.admin_email,
        admin_first_name=payload.admin_first_name,
        admin_last_name=payload.admin_last_name,
        admin_password=payload.admin_password,
        parent_company_id=payload.parent_company_id,
        logo_url=payload.logo_url,
        timezone=payload.timezone,
    )

    user_count = db.query(User).filter(User.company_id == company.id).count()
    response = CompanyResponse.model_validate(company)
    response.user_count = user_count
    return response


@router.get("/companies/{company_id}", response_model=CompanyResponse, summary="Get company details")
def get_company(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Get detailed information about a specific company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    user_count = db.query(User).filter(User.company_id == company.id).count()
    response = CompanyResponse.model_validate(company)
    response.user_count = user_count
    return response


@router.put("/companies/{company_id}", response_model=CompanyResponse, summary="Update a company")
def update_company(
    company_id: int,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Update company details (activate/deactivate, etc.)."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(company, field, value)

    db.commit()
    db.refresh(company)
    return CompanyResponse.model_validate(company)


# --- Read-Only Browsing ---

@router.get("/companies/{company_id}/users", response_model=List[UserResponse], summary="Browse company users")
def browse_company_users(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Browse all users belonging to a specific company (read-only)."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    users = db.query(User).filter(User.company_id == company_id).order_by(User.last_name).all()
    return [UserResponse.model_validate(u) for u in users]


@router.get("/companies/{company_id}/dashboard", summary="Company dashboard summary")
def company_dashboard(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Get a dashboard summary for a specific company (read-only)."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    user_count = db.query(func.count(User.id)).filter(
        User.company_id == company_id, User.is_active == True
    ).scalar()

    total_wos = db.query(func.count(WorkOrder.id)).filter(
        WorkOrder.company_id == company_id
    ).scalar()

    active_wos = db.query(func.count(WorkOrder.id)).filter(
        WorkOrder.company_id == company_id,
        WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
    ).scalar()

    work_center_count = db.query(func.count(WorkCenter.id)).filter(
        WorkCenter.company_id == company_id, WorkCenter.is_active == True
    ).scalar()

    part_count = db.query(func.count(Part.id)).filter(
        Part.company_id == company_id
    ).scalar()

    return {
        "company": CompanyResponse.model_validate(company),
        "active_users": user_count,
        "total_work_orders": total_wos,
        "active_work_orders": active_wos,
        "work_centers": work_center_count,
        "parts": part_count,
    }


@router.get("/overview", summary="Cross-company overview")
def platform_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_platform_admin)
):
    """Get a high-level overview across all companies."""
    companies = db.query(Company).filter(Company.is_active == True).all()

    overview = []
    for company in companies:
        user_count = db.query(func.count(User.id)).filter(
            User.company_id == company.id, User.is_active == True
        ).scalar()
        active_wos = db.query(func.count(WorkOrder.id)).filter(
            WorkOrder.company_id == company.id,
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
        ).scalar()
        overview.append({
            "id": company.id,
            "name": company.name,
            "slug": company.slug,
            "logo_url": company.logo_url,
            "active_users": user_count,
            "active_work_orders": active_wos,
        })

    total_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar()
    total_active_wos = db.query(func.count(WorkOrder.id)).filter(
        WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
    ).scalar()

    return {
        "total_companies": len(companies),
        "total_active_users": total_users,
        "total_active_work_orders": total_active_wos,
        "companies": overview,
    }
