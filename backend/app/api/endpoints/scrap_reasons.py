"""Scrap reason code management (Lean Phase 1 / issue #88).

Mirrors the downtime reason-code trio (list / create / update, deactivate-not-
delete -- see ``endpoints/downtime.py``) with two deliberate upgrades over that
precedent: writes are role-gated (ADMIN/MANAGER/QUALITY -- the same set that
owns NCR vocabulary in ``endpoints/quality.py``) and audited through
``AuditService`` on the tamper-evident chain. Reads stay on ``get_current_user``
so the kiosk/desktop scrap pickers work for operators.

Mounted under the ``/quality`` prefix as a sibling router (quality.py is ~770
lines of NCR/CAR/FAI already), so the paths are ``/quality/scrap-reason-codes``.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.scrap_reason import ScrapCategory, ScrapReasonCode
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

router = APIRouter()

# RBAC: managing the scrap-reason vocabulary is a quality-system configuration
# task -- same write set as the NCR/CAR endpoints in quality.py.
SCRAP_REASON_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]


# ============== Pydantic Schemas ==============


class ScrapReasonCodeCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=255)
    category: ScrapCategory = ScrapCategory.OTHER
    description: Optional[str] = None
    is_active: bool = True
    display_order: int = 0


class ScrapReasonCodeUpdate(BaseModel):
    code: Optional[str] = Field(None, min_length=1, max_length=50)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[ScrapCategory] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class ScrapReasonCodeResponse(BaseModel):
    id: int
    code: str
    name: str
    category: str
    description: Optional[str] = None
    is_active: bool
    display_order: int

    model_config = {"from_attributes": True}


def _code_values(code: ScrapReasonCode) -> dict:
    """Column snapshot for audit old/new values."""
    return {
        "code": code.code,
        "name": code.name,
        "category": code.category,
        "description": code.description,
        "is_active": code.is_active,
        "display_order": code.display_order,
    }


# ============== Endpoints ==============


@router.get("/scrap-reason-codes", response_model=List[ScrapReasonCodeResponse])
def list_scrap_reason_codes(
    category: Optional[ScrapCategory] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List scrap reason codes for the active company (active only by default)."""
    query = tenant_query(db, ScrapReasonCode, company_id)
    if category:
        query = query.filter(ScrapReasonCode.category == category.value)
    if not include_inactive:
        query = query.filter(ScrapReasonCode.is_active == True)  # noqa: E712
    return query.order_by(ScrapReasonCode.display_order, ScrapReasonCode.code).all()


@router.post("/scrap-reason-codes", response_model=ScrapReasonCodeResponse)
def create_scrap_reason_code(
    data: ScrapReasonCodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(SCRAP_REASON_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a scrap reason code (unique per tenant on ``code``)."""
    existing = tenant_query(db, ScrapReasonCode, company_id).filter(ScrapReasonCode.code == data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Scrap reason code already exists")

    reason_code = ScrapReasonCode(
        code=data.code,
        name=data.name,
        category=data.category.value,
        description=data.description,
        is_active=data.is_active,
        display_order=data.display_order,
    )
    reason_code.company_id = company_id
    db.add(reason_code)

    # Audit (tamper-evident) BEFORE the terminal commit so the row commits
    # atomically with the create. Flush so the PK is populated. The INSERT
    # executes at THIS flush, so uq_scrap_reason_codes_company_code surfaces
    # here when the pre-check lost a race with a concurrent create -- catch it
    # at the flush too (same pattern as endpoints/oee.py), not only at commit,
    # so the race returns a clean 400 instead of a 500.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Scrap reason code already exists") from exc
    audit.log_create(
        resource_type="scrap_reason_code",
        resource_id=reason_code.id,
        resource_identifier=reason_code.code,
        new_values=_code_values(reason_code),
        description=f"Created scrap reason code {reason_code.code} ({reason_code.name})",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        # uq_scrap_reason_codes_company_code lost a race with a concurrent create.
        db.rollback()
        raise HTTPException(status_code=400, detail="Scrap reason code already exists") from exc
    db.refresh(reason_code)
    return reason_code


@router.put("/scrap-reason-codes/{reason_code_id}", response_model=ScrapReasonCodeResponse)
def update_scrap_reason_code(
    reason_code_id: int,
    data: ScrapReasonCodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(SCRAP_REASON_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a scrap reason code. Deactivate via ``is_active=false`` -- there is
    deliberately NO delete endpoint: historical scrap rows reference these ids
    (traceability), so retirement is a flag, never a row removal."""
    reason_code = tenant_query(db, ScrapReasonCode, company_id).filter(ScrapReasonCode.id == reason_code_id).first()
    if not reason_code:
        raise HTTPException(status_code=404, detail="Scrap reason code not found")

    old_values = _code_values(reason_code)

    update_data = data.model_dump(exclude_unset=True)
    if "code" in update_data and update_data["code"] != reason_code.code:
        duplicate = (
            tenant_query(db, ScrapReasonCode, company_id)
            .filter(ScrapReasonCode.code == update_data["code"], ScrapReasonCode.id != reason_code_id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail="Scrap reason code already exists")
    if "category" in update_data and update_data["category"] is not None:
        update_data["category"] = update_data["category"].value
    for field, value in update_data.items():
        setattr(reason_code, field, value)

    # The UPDATE executes at THIS flush, so a code rename that lost a race with
    # a concurrent writer trips uq_scrap_reason_codes_company_code here -- catch
    # it at the flush too (same pattern as endpoints/oee.py) for a clean 400.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Scrap reason code already exists") from exc
    audit.log_update(
        resource_type="scrap_reason_code",
        resource_id=reason_code.id,
        resource_identifier=reason_code.code,
        old_values=old_values,
        new_values=_code_values(reason_code),
        description=f"Updated scrap reason code {reason_code.code}",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Scrap reason code already exists") from exc
    db.refresh(reason_code)
    return reason_code
