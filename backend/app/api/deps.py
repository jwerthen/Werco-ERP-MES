from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    verify_api_token,
    verify_display_token,
    verify_kiosk_token,
    verify_signin_token,
    verify_token,
)
from app.db.database import get_db
from app.models.display_token import DisplayToken
from app.models.kiosk_station import KioskStation
from app.models.signin_station import SigninStation
from app.models.user import User, UserRole
from app.services.api_token_service import resolve_api_token_user
from app.services.audit_service import AuditService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
SAFE_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

# Path fence for kiosk-scoped OPERATOR access tokens (scope=="kiosk", minted by
# POST /auth/kiosk-badge-token). A badge-scanned operator on a shared crew
# station may only drive the shop-floor endpoints (+ the employee-logout audit
# write); everywhere else the token is 403. Tokens without a scope claim are
# unaffected.
KIOSK_TOKEN_PATH_PREFIXES = ("/api/v1/shop-floor",)
# employee-logout is authenticated and derives the actor from the TOKEN. It
# previously took no bearer at all and trusted the request body, which made it an
# unauthenticated audit-forgery and cross-tenant badge-enumeration surface; this
# entry is therefore now load-bearing rather than defensive — it is what lets a
# badge-minted crew-station token record its own logout.
KIOSK_TOKEN_EXACT_PATHS = ("/api/v1/auth/employee-logout",)
# Deny-list carved out of the shop-floor prefix: the crew station never needs
# these, and a badge-minted 5-minute token for a MANAGER/ADMIN must not be able
# to persist access (station PIN reset/revoke), approve labor (G5-A is a
# desktop supervisor workflow) or drive the manager dispatch board from the
# shared terminal. The public kiosk-stations/station-login route never reaches
# get_current_user, so excluding the whole prefix is safe. The dispatch board is
# an exact route rather than a subtree, but the same prefix test covers it.
KIOSK_TOKEN_DENIED_PREFIXES = (
    "/api/v1/shop-floor/kiosk-stations",
    "/api/v1/shop-floor/dispatch-board",
)
_KIOSK_DENIED_APPROVAL_MARKER = "/time-entries/"
_KIOSK_DENIED_APPROVAL_SUFFIXES = ("/approve", "/unapprove")
# The run-order rewrite (PUT /shop-floor/work-centers/{id}/run-order) is a
# planner tool: dictating what the whole shop runs next must not be reachable
# from a shared crew terminal, even on a scanned manager badge. Operators keep
# READING their rank — the queue endpoint is a different path and stays allowed.
_KIOSK_DENIED_RUN_ORDER_MARKER = "/work-centers/"
_KIOSK_DENIED_RUN_ORDER_SUFFIXES = ("/run-order",)


def _is_kiosk_scope_allowed_path(path: str) -> bool:
    """True when a scope=='kiosk' access token may be honored on this path."""
    for denied in KIOSK_TOKEN_DENIED_PREFIXES:
        if path == denied or path.startswith(denied + "/"):
            return False
    if _KIOSK_DENIED_APPROVAL_MARKER in path and path.endswith(_KIOSK_DENIED_APPROVAL_SUFFIXES):
        return False
    if _KIOSK_DENIED_RUN_ORDER_MARKER in path and path.endswith(_KIOSK_DENIED_RUN_ORDER_SUFFIXES):
        return False
    for prefix in KIOSK_TOKEN_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return path in KIOSK_TOKEN_EXACT_PATHS


def _is_read_only_exempt_path(path: str) -> bool:
    """Allow session-management requests needed to leave read-only mode."""
    return path.endswith("/auth/logout") or "/auth/switch-company/" in path


# Path fence for long-lived per-user API TOKENS (type=="api", minted by
# POST /api-tokens/ for a bot or an MCP client). The token is honored everywhere a
# user access token is, as that user, EXCEPT under these two prefixes: no refresh,
# no logout, no kiosk-badge mint, no company switch, no register, no display-token
# verbs -- and no minting, listing or revoking of API tokens, which is an Admin's
# interactive act, never something a token can do to itself. 403 -- not 401 --
# because the token IS valid; it just cannot reach this resource. Same prefix-test
# style as the kiosk fence above. Derived from the mounted prefix
# (``settings.API_V1_PREFIX``) rather than spelled out, so a re-mount can never
# leave the fence pointing at paths nothing serves.
_API_PREFIX = settings.API_V1_PREFIX.rstrip("/")
API_TOKEN_DENIED_PREFIXES = (f"{_API_PREFIX}/auth", f"{_API_PREFIX}/api-tokens")
# ``User._token_scope`` value for a request authenticated by an API token. Every
# consumer of ``_token_scope`` tests ``== "kiosk"`` only, so "api" takes the desktop
# branch (labor source = the client's declared channel, never KIOSK).
API_TOKEN_SCOPE = "api"


def _is_api_token_denied_path(path: str) -> bool:
    """True when an API token must be refused on this path (see API_TOKEN_DENIED_PREFIXES)."""
    for denied in API_TOKEN_DENIED_PREFIXES:
        if path == denied or path.startswith(denied + "/"):
            return True
    return False


def is_user_bearer(token: str) -> bool:
    """Is this bearer a USER credential -- an access JWT or an API JWT -- by signature?

    The three sibling principals (``get_display_or_user``, ``get_signin_principal``,
    ``get_kiosk_or_user``) branch on this before delegating to ``get_current_user``,
    which then applies the row checks and the API-token fence; a station/display
    token is neither and falls through to its own branch. Signature only -- a
    revoked API token is still a user bearer here and is refused (401) inside
    ``get_current_user``, exactly where an expired access token is.
    """
    return verify_token(token) is not None or verify_api_token(token) is not None


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
    api_token_record = None
    if payload is None:
        # Not an access token: an API token (type=="api") is the only other USER
        # credential. resolve_api_token_user runs the ONE row check (signature,
        # row by jti, not revoked, not past the ROW's expires_at, claims equal to
        # the row, user in the row's company) and the throttled last_used_at
        # touch; None is the same 401 a bad access token gets. The row -- never
        # the JWT -- says who and which company; the fence says where.
        resolved = resolve_api_token_user(db, token)
        if resolved is None:
            raise credentials_exception
        user, api_token_record = resolved
        if _is_api_token_denied_path(request.url.path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API token cannot access this resource",
            )
    else:
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

    if api_token_record is not None:
        # The ROW pins tenancy: never the user's current company, never a claim,
        # and never a platform admin's switched context -- an API token cannot
        # switch company (the fence refuses /auth/switch-company too). Never
        # read-only, and scoped "api" so labor telemetry treats it as a desktop.
        user._active_company_id = api_token_record.company_id
        user._read_only_company_context = False
        user._token_scope = API_TOKEN_SCOPE
        # The credential marker ``AuditService`` folds into every row this
        # request writes (``extra_data.credential``): a token's writes must be
        # tellable from the bound user's own interactive ones, and from each
        # other's -- ``last_used_at`` is a liveness marker, not a trail.
        user._api_token_id = api_token_record.id
        user._api_token_jti_prefix = api_token_record.jti_prefix
        user._api_token_label = api_token_record.label
        return user

    # An interactive credential: clear the marker explicitly. The same ORM
    # instance can serve more than one request (a shared identity map), and an
    # unset marker is what tells ``AuditService`` this write was the person's own.
    user._api_token_id = None

    # Attach active company context from JWT (may differ from user.company_id
    # when a platform admin switches to view another company)
    token_company_id = payload.get("company_id")
    user._active_company_id = token_company_id if token_company_id is not None else user.company_id
    user._read_only_company_context = bool(payload.get("read_only", False))
    # Token scope ("kiosk" for badge-minted crew-station operator tokens, None
    # otherwise) — surfaced so shop-floor writes can derive their adoption-telemetry
    # channel (TimeEntrySource KIOSK vs DESKTOP) from the credential, not the client.
    user._token_scope = payload.get("scope")

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


# Signed-in roles allowed to see customer names on the wallboard: the office
# roles that also manage displays, plus platform admins. Everyone else gets the
# public-safe (redacted) board — matching an un-flagged public display token.
_WALLBOARD_CUSTOMER_ROLES = (UserRole.PLATFORM_ADMIN, UserRole.ADMIN, UserRole.MANAGER)


@dataclass
class WallboardPrincipal:
    """Resolved caller identity for the TV wallboard read endpoint (A0.5).

    ``kind`` is ``"user"`` (a normal authenticated user) or ``"display"`` (an
    unattended TV holding a scoped display token). ``company_id`` is the ONLY
    field tenant scoping may use — for display tokens it comes from the
    ``display_tokens`` DB row, never from the client.

    ``show_customer`` gates whether the wallboard payload may reveal work-order
    customer names (default False = public-safe). For a display token it is the
    row's ``show_customer_names`` opt-in; for a user it is True only for the
    privileged office roles that also manage displays — everyone else, and every
    un-flagged/public TV, sees a redacted board.
    """

    company_id: int
    kind: str
    user: Optional[User] = None
    display_label: Optional[str] = None
    show_customer: bool = False


def get_display_or_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> WallboardPrincipal:
    """Accept EITHER a normal user access token OR a display token.

    SECURITY (A0.5): this is the ONLY dependency that honors display tokens,
    and it must only ever guard the read-only wallboard endpoint. Everywhere
    else auth flows through ``get_current_user``, which accepts only
    ``type == "access"`` JWTs and (through the ``api_tokens`` row check)
    ``type == "api"`` API tokens — so a display token presented to any other
    endpoint gets a 401.

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
    # (active flag, platform-admin company context, read-only context; for an
    # API token, the row checks and the /auth + /api-tokens fence).
    if is_user_bearer(token):
        user = get_current_user(request=request, db=db, token=token)
        # Customer names on the board are for the privileged office roles that
        # also provision displays; operators/quality/shipping/viewers previewing
        # the board in-app see the same redacted view a public TV does.
        show_customer = user.role in _WALLBOARD_CUSTOMER_ROLES
        return WallboardPrincipal(
            company_id=user._active_company_id, kind="user", user=user, show_customer=show_customer
        )

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

    # Display path: customer names appear ONLY when the row explicitly opted in.
    # ``bool(...)`` coerces a NULL from a pre-migration row to False (public-safe).
    return WallboardPrincipal(
        company_id=record.company_id,
        kind="display",
        display_label=record.label,
        show_customer=bool(record.show_customer_names),
    )


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
    Everywhere else auth flows through ``get_current_user``, which accepts only
    ``type == "access"`` JWTs and (via the ``api_tokens`` row check) ``type ==
    "api"`` API tokens — so a signin token presented to any other endpoint gets
    a 401. ``get_display_or_user`` is left
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
    # (active flag, platform-admin company context, read-only context; for an
    # API token, the row checks and the /auth + /api-tokens fence).
    if is_user_bearer(token):
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
    through ``get_current_user``, which accepts only ``type == "access"`` JWTs
    and (via the ``api_tokens`` row check) ``type == "api"`` API tokens — so a
    kiosk station token presented to any other endpoint gets a 401. (The badge-token mint validates the station
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
    # (active flag, platform-admin company context, read-only context, the
    # kiosk-scope path fence for badge-minted operator tokens, and for an API
    # token the row checks and the /auth + /api-tokens fence).
    if is_user_bearer(token):
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
