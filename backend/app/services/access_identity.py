"""Live access-JWT identity shared by HTTP and WebSocket admission.

The caller verifies the signature and credential type first. Company switching
is a platform capability; an old tenant token cannot keep a reassigned account
in its former company. No transaction or database connection is retained here.
"""

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User, UserRole


@dataclass(frozen=True)
class AccessIdentity:
    user: User
    company_id: int
    read_only: bool
    scope: Optional[str]


def _invalid_credentials() -> HTTPException:
    return HTTPException(401, "Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})


def require_active_company(db: Session, company_id: int, *, allow_inactive: bool = False) -> None:
    if type(company_id) is not int or company_id <= 0:
        raise _invalid_credentials()
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None or (not company.is_active and not allow_inactive):
        raise HTTPException(403, "Company is unavailable")


def resolve_access_identity(
    db: Session,
    payload: dict,
    *,
    allow_inactive_company: bool = False,
    allow_inactive_platform_company: bool = False,
) -> AccessIdentity:
    """Resolve verified claims against the current account and active company."""
    raw_user_id = payload.get("user_id")
    if not isinstance(raw_user_id, (str, int)) or isinstance(raw_user_id, bool):
        raise _invalid_credentials()
    try:
        user_id = int(raw_user_id)
    except (ValueError, TypeError):
        raise _invalid_credentials() from None
    if user_id <= 0 or user_id > 2**31 - 1:
        raise _invalid_credentials()

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise _invalid_credentials()
    if not user.is_active:
        raise HTTPException(403, "User account is disabled")
    scope = payload.get("scope")
    if scope not in (None, "kiosk"):
        raise HTTPException(403, "Credential scope cannot access this resource")

    company_id = payload.get("company_id")
    if company_id is None:
        company_id = user.company_id
    if type(company_id) is not int or company_id <= 0 or company_id > 2**31 - 1:
        raise _invalid_credentials()
    platform = user.is_superuser or user.role == UserRole.PLATFORM_ADMIN
    if not platform and company_id != user.company_id:
        raise _invalid_credentials()
    require_active_company(
        db, company_id, allow_inactive=allow_inactive_company or (allow_inactive_platform_company and platform)
    )
    return AccessIdentity(user, company_id, bool(payload.get("read_only", False)), scope)
