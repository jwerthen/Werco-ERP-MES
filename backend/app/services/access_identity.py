"""Live access-JWT identity shared by HTTP and WebSocket admission.

The caller verifies the signature and credential type first. Company switching
is a platform capability; an old tenant token cannot keep a reassigned account
in its former company. No transaction or database connection is retained here.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.floor_pin import (
    AUTH_METHOD_PIN,
    FLOOR_PIN_REAUTH_CODE,
    FLOOR_PIN_REAUTH_MESSAGE,
    pin_session_still_valid,
)
from app.models.company import Company
from app.models.user import User, UserRole


@dataclass(frozen=True)
class AccessIdentity:
    user: User
    company_id: int
    read_only: bool
    scope: Optional[str]
    # The token's ``amr`` claim (how the person signed in: ``("badge",)`` or
    # ``("badge", "pin")``); ``None`` for a password session or a token without the claim.
    auth_methods: Optional[Tuple[str, ...]] = None
    # The token's ``pst`` claim: the ``floor_pin_set_at`` (epoch-microseconds) of the PIN a
    # ``["badge", "pin"]`` session verified; ``None`` on every other session.
    pin_set_stamp: Optional[int] = None


def _invalid_credentials() -> HTTPException:
    return HTTPException(401, "Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})


def require_active_company(db: Session, company_id: int, *, allow_inactive: bool = False) -> None:
    if type(company_id) is not int or company_id <= 0:
        raise _invalid_credentials()
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None or (not company.is_active and not allow_inactive):
        raise HTTPException(403, "Company is unavailable")


def pin_session_reauth_required() -> HTTPException:
    """401 ``floor_pin_reauth_required``: the session must sign in again with badge and PIN."""
    return HTTPException(401, {"code": FLOOR_PIN_REAUTH_CODE, "message": FLOOR_PIN_REAUTH_MESSAGE})


def resolve_access_identity(
    db: Session,
    payload: dict,
    *,
    allow_inactive_company: bool = False,
    allow_inactive_platform_company: bool = False,
    check_pin_session: bool = True,
) -> AccessIdentity:
    """Resolve verified claims against the current account and active company.

    A ``["badge", "pin"]`` session ends when its PIN does: if the user's PIN is gone (cleared,
    or cleared by a promotion) or was set again since the session verified it (the token's
    ``pst`` no longer equals ``floor_pin_set_at``), the answer is **401**
    ``floor_pin_reauth_required`` on every request, HTTP and WebSocket alike. A lock does not
    end it. ``check_pin_session=False`` only for ``POST /auth/switch-company``, which runs the
    same check itself so it can write its failure audit row.
    """
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
    raw_methods = payload.get("auth_methods")
    auth_methods = tuple(raw_methods) if raw_methods else None
    pin_set_stamp = payload.get("pin_set_stamp")
    if (
        check_pin_session
        and auth_methods
        and AUTH_METHOD_PIN in auth_methods
        and not pin_session_still_valid(user.floor_pin_hash, user.floor_pin_set_at, pin_set_stamp)
    ):
        raise pin_session_reauth_required()
    return AccessIdentity(user, company_id, bool(payload.get("read_only", False)), scope, auth_methods, pin_set_stamp)
