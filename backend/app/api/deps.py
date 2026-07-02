from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import verify_display_token, verify_kiosk_token, verify_signin_token, verify_token
from app.db.database import get_db
from app.models.display_token import DisplayToken
from app.models.kiosk_station import KioskStation
from app.models.signin_station import SigninStation
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
SAFE_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

# Path fence for kiosk-scoped OPERATOR access tokens (scope=="kiosk", minted by
# POST /auth/kiosk-badge-token). A badge-scanned operator on a shared crew
# station may only drive the shop-floor endpoints (+ the employee-logout audit
# write); everywhere else the token is 403. Tokens without a scope claim are
# unaffected.
KIOSK_TOKEN_PATH_PREFIXES = ("/api/v1/shop-floor",)
# employee-logout takes its identity from the request body today (no bearer
# auth), so this entry is defensive: it keeps kiosk tokens working if that
# endpoint ever moves onto get_current_user.
KIOSK_TOKEN_EXACT_PATHS = ("/api/v1/auth/employee-logout",)
# Deny-list carved out of the shop-floor prefix: the crew station never needs
# these, and a badge-minted 5-minute token for a MANAGER/ADMIN must not be able
# to persist access (station PIN reset/revoke) or approve labor (G5-A is a
# desktop supervisor workflow) from the shared terminal. The public
# kiosk-stations/station-login route never reaches get_current_user, so
# excluding the whole prefix is safe.
KIOSK_TOKEN_DENIED_PREFIXES = ("/api/v1/shop-floor/kiosk-stations",)
_KIOSK_DENIED_APPROVAL_MARKER = "/time-entries/"
_KIOSK_DENIED_APPROVAL_SUFFIXES = ("/approve", "/unapprove")


def _is_kiosk_scope_allowed_path(path: str) -> bool:
    """True when a scope=='kiosk' access token may be honored on this path."""
    for denied in KIOSK_TOKEN_DENIED_PREFIXES:
        if path == denied or path.startswith(denied + "/"):
            return False
    if _KIOSK_DENIED_APPROVAL_MARKER in path and path.endswith(_KIOSK_DENIED_APPROVAL_SUFFIXES):
        return False
    for prefix in KIOSK_TOKEN_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return path in KIOSK_TOKEN_EXACT_PATHS


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

    # Kiosk-scope path fence: a badge-minted operator token (scope=="kiosk") is
    # only honored on the shop-floor paths (+ employee-logout). 403 — not 401 —
    # everywhere else: the token IS valid, it just cannot reach this resource.
    # Tokens without a scope claim skip this entirely.
    if payload.get("scope") == "kiosk" and not _is_kiosk_scope_allowed_path(request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kiosk-scoped token cannot access this resource",
        )

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


@dataclass
class SigninPrincipal:
    """Resolved caller identity for the two visitor-write endpoints.

    ``kind`` is ``"user"`` (a normal authenticated staff member) or
    ``"station"`` (a PIN-unlocked entrance tablet holding a scoped signin
    token). ``company_id`` is the ONLY field tenant scoping may use — for a
    station it comes from the ``signin_stations`` DB row, never from the client.

    On the station path ``user`` is ``None`` and the audit actor is the
    ``station_label`` (recorded explicitly by the write path). On the user path
    ``station_id`` / ``station_label`` are ``None``.
    """

    company_id: int
    kind: str  # "user" | "station"
    station_id: Optional[int] = None
    station_label: Optional[str] = None
    user: Optional[User] = None


def get_signin_principal(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> SigninPrincipal:
    """Accept EITHER a normal staff access token OR a station signin token.

    SECURITY (visitor sign-in): this dependency, alongside ``get_display_or_user``,
    is one of the only two that honor a non-``"access"`` JWT type, and it must
    only ever guard the two visitor write endpoints (sign-in / sign-out).
    Everywhere else auth flows through ``get_current_user`` / ``verify_token``,
    which reject any JWT whose ``type`` is not ``"access"`` — so a signin token
    presented to any other endpoint gets a 401. ``get_display_or_user`` is left
    untouched (the read-only wallboard path stays uncontaminated).

    Station-token path checks, in order (the wallboard two-layer pattern):
      1. signature + JWT expiry + ``type == "signin"`` (``verify_signin_token``)
      2. the ``signin_stations`` row exists for the JWT's ``sid``
      3. the row is not revoked
      4. the JWT's ``cid`` claim matches the row's ``company_id``
    The active company comes from the DB row (authoritative), so a forged or
    stale ``cid`` claim can never widen tenant scope.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Normal staff token first — get_current_user applies the full user checks
    # (active flag, platform-admin company context, read-only context).
    if verify_token(token) is not None:
        user = get_current_user(request=request, db=db, token=token)
        return SigninPrincipal(company_id=user._active_company_id, kind="user", user=user)

    claims = verify_signin_token(token)
    if claims is None or not claims.get("station_id"):
        raise credentials_exception

    station = db.query(SigninStation).filter(SigninStation.id == claims["station_id"]).first()
    if station is None or station.revoked:
        raise credentials_exception
    if claims.get("company_id") != station.company_id:
        raise credentials_exception

    return SigninPrincipal(
        company_id=station.company_id,
        kind="station",
        station_id=station.id,
        station_label=station.label,
    )


@dataclass
class KioskReadPrincipal:
    """Resolved caller identity for the roster-enriched work-center-queue read.

    ``kind`` is ``"user"`` (a normal authenticated user) or ``"station"`` (a
    PIN-unlocked crew-station kiosk holding a scoped kiosk token).
    ``company_id`` is the ONLY field tenant scoping may use — for a station it
    comes from the ``kiosk_stations`` DB row, never from the client.

    On the station path ``work_center_id`` is the row's bound work center: the
    caller MUST enforce that a station only reads its OWN work center's queue.
    On the user path ``station_id`` / ``station_label`` / ``work_center_id``
    are ``None`` (users may read any queue in their company, as today).
    """

    company_id: int
    kind: str  # "user" | "station"
    station_id: Optional[int] = None
    station_label: Optional[str] = None
    work_center_id: Optional[int] = None
    user: Optional[User] = None


def get_kiosk_or_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> KioskReadPrincipal:
    """Accept EITHER a normal user access token OR a crew-station kiosk token.

    SECURITY (crew-station kiosk): this dependency — alongside
    ``get_display_or_user`` and ``get_signin_principal`` — is one of the only
    three that honor a non-``"access"`` JWT type, and it must only ever guard
    the read-only work-center-queue endpoint. Everywhere else auth flows
    through ``get_current_user`` / ``verify_token``, which reject any JWT whose
    ``type`` is not ``"access"`` — so a kiosk station token presented to any
    other endpoint gets a 401. (The badge-token mint validates the station
    token itself against the same DB-row checks; it does not use this
    dependency's user branch.)

    Station-token path checks, in order (the wallboard/signin two-layer pattern):
      1. signature + JWT expiry + ``type == "kiosk"`` (``verify_kiosk_token``)
      2. the ``kiosk_stations`` row exists for the JWT's ``sid``
      3. the row is not revoked
      4. the JWT's ``cid`` claim matches the row's ``company_id``
    The active company AND the bound work center come from the DB row
    (authoritative), so a forged or stale claim can never widen tenant scope or
    point the station at another work center.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Normal user token first — get_current_user applies the full user checks
    # (active flag, platform-admin company context, read-only context, and the
    # kiosk-scope path fence for badge-minted operator tokens).
    if verify_token(token) is not None:
        user = get_current_user(request=request, db=db, token=token)
        return KioskReadPrincipal(company_id=user._active_company_id, kind="user", user=user)

    claims = verify_kiosk_token(token)
    if claims is None or not claims.get("station_id"):
        raise credentials_exception

    station = db.query(KioskStation).filter(KioskStation.id == claims["station_id"]).first()
    if station is None or station.revoked:
        raise credentials_exception
    if claims.get("company_id") != station.company_id:
        raise credentials_exception

    return KioskReadPrincipal(
        company_id=station.company_id,
        kind="station",
        station_id=station.id,
        station_label=station.label,
        work_center_id=station.work_center_id,
    )
