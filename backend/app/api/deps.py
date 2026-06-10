from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import verify_display_token, verify_token
from app.db.database import get_db
from app.models.display_token import DisplayToken
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
SAFE_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_read_only_exempt_path(path: str) -> bool:
    """Allow session-management requests needed to leave read-only mode."""
    return path.endswith("/auth/logout") or "/auth/switch-company/" in path


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = verify_token(token)
    if payload is None:
        raise credentials_exception

    user_id = payload.get("user_id")
    if user_id is None:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    # Attach active company context from JWT (may differ from user.company_id
    # when a platform admin switches to view another company)
    token_company_id = payload.get("company_id")
    user._active_company_id = token_company_id if token_company_id is not None else user.company_id
    user._read_only_company_context = bool(payload.get("read_only", False))

    if (
        user._read_only_company_context
        and request.method.upper() not in SAFE_READ_ONLY_METHODS
        and not _is_read_only_exempt_path(request.url.path)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only company context cannot modify data",
        )

    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return current_user


def get_current_company_id(current_user: User = Depends(get_current_user)) -> int:
    """Get the active company_id for the current request.
    For normal users this is their own company.
    For platform admins who switched context, this is the viewed company."""
    return current_user._active_company_id


def require_role(allowed_roles: list[UserRole]):
    """Dependency to require specific roles"""

    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.is_superuser:
            return current_user
        if current_user.role == UserRole.PLATFORM_ADMIN:
            return current_user
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user

    return role_checker


def require_platform_admin(current_user: User = Depends(get_current_user)) -> User:
    """Require PLATFORM_ADMIN role or superuser status."""
    if current_user.role == UserRole.PLATFORM_ADMIN or current_user.is_superuser:
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin access required")


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_superuser and current_user.role not in (UserRole.ADMIN, UserRole.PLATFORM_ADMIN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def get_audit_service(
    request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> AuditService:
    """Dependency to get an AuditService instance with user and request context."""
    return AuditService(db, current_user, request)


@dataclass
class WallboardPrincipal:
    """Resolved caller identity for the TV wallboard read endpoint (A0.5).

    ``kind`` is ``"user"`` (a normal authenticated user) or ``"display"`` (an
    unattended TV holding a scoped display token). ``company_id`` is the ONLY
    field tenant scoping may use — for display tokens it comes from the
    ``display_tokens`` DB row, never from the client.
    """

    company_id: int
    kind: str
    user: Optional[User] = None
    display_label: Optional[str] = None


def get_display_or_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> WallboardPrincipal:
    """Accept EITHER a normal user access token OR a display token.

    SECURITY (A0.5): this is the ONLY dependency that honors display tokens,
    and it must only ever guard the read-only wallboard endpoint. Everywhere
    else auth flows through ``get_current_user``/``verify_token``, which
    reject any JWT whose ``type`` claim is not ``"access"`` — so a display
    token presented to any other endpoint gets a 401.

    Display-token path checks, in order:
      1. signature + JWT expiry + ``type == "display"`` (``verify_display_token``)
      2. the ``display_tokens`` row exists for the JWT's ``jti``
      3. the row is not revoked and not past its DB ``expires_at``
      4. the JWT's ``cid`` claim matches the row's ``company_id``
    The active company comes from the DB row (authoritative), so a forged or
    stale ``cid`` claim can never widen tenant scope.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Normal user token first — get_current_user applies the full user checks
    # (active flag, platform-admin company context, read-only context).
    if verify_token(token) is not None:
        user = get_current_user(request=request, db=db, token=token)
        return WallboardPrincipal(company_id=user._active_company_id, kind="user", user=user)

    claims = verify_display_token(token)
    if claims is None or not claims.get("jti"):
        raise credentials_exception

    record = db.query(DisplayToken).filter(DisplayToken.jti == claims["jti"]).first()
    if record is None or record.revoked:
        raise credentials_exception
    if record.expires_at is None or record.expires_at <= datetime.utcnow():
        raise credentials_exception
    if claims.get("company_id") != record.company_id:
        raise credentials_exception

    return WallboardPrincipal(company_id=record.company_id, kind="display", display_label=record.label)
