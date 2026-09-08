"""Read-only material provenance; deliberately separate from production laser nests."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.role_permission import DEFAULT_ROLE_PERMISSIONS, RolePermission
from app.models.user import User, UserRole
from app.schemas.quote_nesting_materials import (
    MaterialCatalogResponse,
    MaterialResolutionRequest,
    MaterialResolutionResponse,
)
from app.services.quote_nesting_materials import list_catalog, resolve_material

router = APIRouter()


def require_material_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(list(UserRole))),
    company_id: int = Depends(get_current_company_id),
) -> User:
    """Match /nest's effective purchasing:view, including tenant role overrides.

    The global authentication dependency still enforces disabled-user, kiosk/API-token
    and read-only switched-company boundaries. No exception is added for this POST.
    """
    if current_user.is_superuser or current_user.role == UserRole.PLATFORM_ADMIN:
        return current_user
    override = tenant_query(db, RolePermission, company_id).filter(RolePermission.role == current_user.role).first()
    permissions = override.permissions if override is not None else DEFAULT_ROLE_PERMISSIONS.get(current_user.role, [])
    if not isinstance(permissions, list) or "purchasing:view" not in permissions:
        raise HTTPException(403, "Material catalog access requires purchasing:view")
    return current_user


@router.get("/materials", response_model=MaterialCatalogResponse)
def get_material_catalog(
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_material_read),
    company_id: int = Depends(get_current_company_id),
):
    """Read active catalog rows for the active company using offset/limit, without seeding."""
    return list_catalog(db, company_id, offset=offset, limit=limit)


@router.post("/material-resolution", response_model=MaterialResolutionResponse)
def post_material_resolution(
    payload: MaterialResolutionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_material_read),
    company_id: int = Depends(get_current_company_id),
):
    """Return unconfirmed calculations without writes; reject stale source hashes with 409."""
    return resolve_material(db, company_id, payload)
