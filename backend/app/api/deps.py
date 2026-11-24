from dataclasses import dataclass
from datetime import datetime
from typing import Any, FrozenSet, Iterable, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.credentials import (
    API_TOKEN_SCOPE_FULL,
    READ_ONLY_ROUTE_DENIED_DETAIL,
    READ_ONLY_ROUTE_WRITES_DETAIL,
)
from app.core.export_control import (
    AI_EGRESS_DISABLED_DETAIL,
    AI_EXTERNAL_QUERY_DENIED_DETAIL,
    AI_EXTERNAL_ROUTE_DENIED_DETAIL,
    EXPORT_CONTROL_STATE_KEY,
    REASON_AI_EGRESS_DISABLED,
    REASON_QUERY_NOT_ALLOWLISTED,
    REASON_ROUTE_NOT_ALLOWLISTED,
    ExportControlMarker,
    ai_external_operations,
    ai_external_query_allowed,
    relative_route_path,
)
from app.core.observability import set_sentry_tags
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
from app.services import llm_client
from app.services.access_identity import require_active_company, resolve_access_identity
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
KIOSK_TOKEN_EXACT_PATHS = (
    "/api/v1/auth/employee-logout",
    "/api/v1/runtime-metrics/config",
    "/api/v1/runtime-metrics/samples",
)
# Deny-list carved out of the shop-floor prefix: the crew station never needs
# these, and a badge-minted 5-minute token for a MANAGER/ADMIN must not be able
# to persist access (station PIN reset/revoke), approve labor (G5-A is a
# desktop supervisor workflow) or drive the manager dispatch board from the
# shared terminal. Station-login and workstation selection use station auth,
# never get_current_user, so excluding the whole prefix is safe. The dispatch board is
# an exact route rather than a subtree, but the same prefix test covers it.
KIOSK_TOKEN_DENIED_PREFIXES = (
    "/api/v1/shop-floor/kiosk-stations",
    "/api/v1/shop-floor/dispatch-board",
    # Labor Review: the list read, approve / unapprove and the supervisor set-clock-out are
    # a desk workflow. The whole subtree is denied, so a new labor-review route is fenced
    # by default (the approve / unapprove suffix test below predates it and stays).
    "/api/v1/shop-floor/time-entries",
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
# ``POST /auth/switch-company/{id}`` checks a badge+PIN session's PIN stamp itself (with an
# audit row), so ``get_current_user`` leaves that one check to it.
_SWITCH_COMPANY_PREFIX = f"{_API_PREFIX}/auth/switch-company/"
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


# (METHOD, full route template) an AI-external API token may call (gate 4). Derived from
# ``app.core.export_control.AI_EXTERNAL_ALLOWED_ROUTES`` -- the one list the MCP door also
# lists from -- with the mounted prefix applied.
AI_EXTERNAL_ALLOWED_OPERATIONS: FrozenSet[Tuple[str, str]] = ai_external_operations(settings.API_V1_PREFIX)


def _matched_route_template(request: Request) -> Optional[str]:
    """The matched route's template (``/api/v1/parts/{part_id}``), or None when no route matched."""
    route = request.scope.get("route")
    if route is None:
        return None
    return getattr(route, "path_format", None) or getattr(route, "path", None)


def ai_external_route_allowed(request: Request) -> bool:
    """Gate 4: is ``(method, matched route template)`` on the AI-external allowlist? No route -> False."""
    template = _matched_route_template(request)
    return template is not None and (request.method.upper(), template) in AI_EXTERNAL_ALLOWED_OPERATIONS


def ai_external_query_params_allowed(request: Request) -> bool:
    """Gate 4, second half: is every query parameter (and value) on the route's AI-external list?

    ``app.core.export_control.AI_EXTERNAL_QUERY_PARAMS`` says why: a free-text filter over a
    field the redaction hides is an oracle on that field. Read from the raw query string,
    every repeated value included; an undeclared parameter is refused, not ignored.
    """
    route_path = relative_route_path(_matched_route_template(request), settings.API_V1_PREFIX)
    return ai_external_query_allowed(route_path, request.query_params.multi_items())


def _export_control_marker(request: Request, user: User, record: Any, *, refusal_reason: Optional[str] = None) -> None:
    """Write the marker ``app.middleware.export_control`` reads into ``request.scope["state"]`` (gate 5).

    Every value comes from the api_tokens ROW. ``refusal_reason`` set means a gate is about
    to refuse this request and the middleware records the refusal in the ledger.
    """
    marker = ExportControlMarker(
        company_id=int(record.company_id),
        user_id=getattr(user, "id", None),
        api_token_id=record.id,
        jti_prefix=getattr(record, "jti_prefix", None),
        label=getattr(record, "label", None),
        read_only=True,
        ai_external=record.ai_external is not False,
        route=_matched_route_template(request) or request.url.path,
        refusal_reason=refusal_reason,
    )
    request.scope.setdefault("state", {})[EXPORT_CONTROL_STATE_KEY] = marker


def read_only_api_token_refusal(
    request: Request, deny_tags: Optional[FrozenSet[str]] = None
) -> Optional[HTTPException]:
    """The 403 a READ-ONLY API token gets on this request, or None (brief P0.1 / P0.2).

    Two gates, in order, both pure (no database):

    2. **Method.** Anything but ``GET`` / ``HEAD`` / ``OPTIONS`` is refused -- whatever the
       bound user's role, and before anything is written (FastAPI decodes a JSON body before
       it solves dependencies, so a malformed body still answers 422 first; nothing is
       written either way). This is what makes the token unable to write through
       ``/api/v1`` directly, not just through the MCP door.
    3. **Tag.** The matched route (``request.scope["route"]``, which FastAPI sets before any
       dependency runs) must carry no tag in ``WERCO_MCP_READ_ONLY_DENY_TAGS``. No matched
       route at all fails CLOSED -- a request this gate cannot place is refused, not waved
       through.

    (Gate 1 -- AI egress -- and gate 4 -- the AI-external route allowlist -- are separate
    checks for an AI-external token, and gate 5 writes the response middleware's marker;
    see ``get_current_user``.) Full tokens and people never reach this function.
    """
    if request.method.upper() not in SAFE_READ_ONLY_METHODS:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=READ_ONLY_ROUTE_WRITES_DETAIL)
    route = request.scope.get("route")
    tags = getattr(route, "tags", None)
    if route is None or tags is None:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=READ_ONLY_ROUTE_DENIED_DETAIL)
    denied = settings.read_only_deny_tags if deny_tags is None else deny_tags
    if any(str(tag) in denied for tag in tags):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=READ_ONLY_ROUTE_DENIED_DETAIL)
    return None


def live_route_tags(routes: Iterable[Any]) -> FrozenSet[str]:
    """Every tag any FastAPI route in ``routes`` carries (``app.routes``; non-API routes skipped)."""
    tags = set()
    for route in routes:
        for tag in getattr(route, "tags", None) or ():
            tags.add(str(tag))
    return frozenset(tags)


def refuse_unknown_read_only_deny_tags(routes: Iterable[Any], deny_tags: Optional[FrozenSet[str]] = None) -> None:
    """Boot check: raise ``RuntimeError`` when ``WERCO_MCP_READ_ONLY_DENY_TAGS`` names a tag no route carries.

    Tags are case-sensitive (``API Tokens``, not ``API tokens``), and a misspelled entry would
    otherwise silently deny NOTHING to every read-only token -- so ``main.py`` calls this after
    every router is included and the deploy fails loudly instead.
    """
    configured = settings.read_only_deny_tags if deny_tags is None else deny_tags
    unknown = sorted(configured - live_route_tags(routes))
    if unknown:
        raise RuntimeError(
            "WERCO_MCP_READ_ONLY_DENY_TAGS names tag(s) no API route carries: "
            + ", ".join(repr(tag) for tag in unknown)
            + ". Tags are case-sensitive and must match the routers' OpenAPI tags exactly."
        )


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


def _tag_sentry_principal(user: User) -> None:
    """Tag this request's Sentry events with who made it and in which ACTIVE company.

    ``company_id`` is ``_active_company_id`` -- the company the request is scoped to,
    which differs from ``user.company_id`` for a platform admin who switched context.
    Ids and the role only: no name or email reaches Sentry. No-op without Sentry.
    """
    role = getattr(user.role, "value", user.role)
    set_sentry_tags(company_id=user._active_company_id, user_id=user.id, role=role)


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
        # Kiosk-scope path fence: a badge-minted operator token (scope=="kiosk") is
        # only honored on the shop-floor paths (+ employee-logout). 403 — not 401 —
        # everywhere else: the token IS valid, it just cannot reach this resource.
        # Tokens without a scope claim skip this entirely.
        if payload.get("scope") == "kiosk" and not _is_kiosk_scope_allowed_path(request.url.path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Kiosk-scoped token cannot access this resource",
            )

        # Session exit must remain possible after the viewed tenant is disabled.
        # This never permits data access; the existing logout/switch routes own
        # their further checks. WebSockets never receive this narrow exemption.
        identity = resolve_access_identity(
            db,
            payload,
            allow_inactive_company=_is_read_only_exempt_path(request.url.path),
            # Stored platform principals retain platform company administration
            # so they can reactivate even their home/sole tenant. This is not a
            # tenant-data exemption, and the resolver verifies the live role.
            allow_inactive_platform_company=request.url.path.startswith(f"{_API_PREFIX}/platform/"),
            # A badge+PIN session whose PIN changed is refused 401 here on every request --
            # except switch-company, which runs the same check itself to write its audit row.
            check_pin_session=not request.url.path.startswith(_SWITCH_COMPANY_PREFIX),
        )
        user = identity.user

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    if api_token_record is not None:
        require_active_company(db, api_token_record.company_id)
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
        # The narrowing (migration 119), copied from the ROW -- never from a claim.
        # ``app.core.credentials.is_read_only_api_token`` / ``is_ai_external_credential``
        # read these, and fail closed on anything but exactly "full" / False.
        user._api_token_scope = api_token_record.scope
        user._api_token_ai_external = bool(api_token_record.ai_external)
        # A standing token is not a sign-on: no ``amr``, so ``AuditService`` folds no
        # ``auth_methods`` into its rows (the ``credential`` marker says what it is).
        user._auth_methods = None
        user._pin_set_stamp = None
        _tag_sentry_principal(user)

        # Narrowed tokens only; a FULL token is byte-identical to before. Gate order:
        #   1. AI-external + company AI egress off -> 403 (Claude-plugin brief P0.3), ledgered.
        #   2. read-only + not GET/HEAD/OPTIONS -> 403   } read_only_api_token_refusal
        #   3. read-only + a denied tag on the route -> 403 }
        #   4. AI-external + (method, route) outside the AI-external allowlist -> 403, ledgered;
        #      then a query parameter / value outside that route's list (a text filter) -> 403, ledgered.
        #   5. the export-control marker for the response middleware (app.middleware.export_control):
        #      file bytes refused to every read-only token; AI-external JSON redacted.
        # An AI-external row is treated as read-only whatever its scope says (the mint refuses
        # the combination; a tampered row must not widen anything).
        ai_external = api_token_record.ai_external is not False
        if api_token_record.scope != API_TOKEN_SCOPE_FULL or ai_external:
            if ai_external and not llm_client.ai_egress_allowed(api_token_record.company_id):
                _export_control_marker(request, user, api_token_record, refusal_reason=REASON_AI_EGRESS_DISABLED)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AI_EGRESS_DISABLED_DETAIL)
            refusal = read_only_api_token_refusal(request)
            if refusal is not None:
                raise refusal
            if ai_external and not ai_external_route_allowed(request):
                _export_control_marker(request, user, api_token_record, refusal_reason=REASON_ROUTE_NOT_ALLOWLISTED)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AI_EXTERNAL_ROUTE_DENIED_DETAIL)
            if ai_external and not ai_external_query_params_allowed(request):
                _export_control_marker(request, user, api_token_record, refusal_reason=REASON_QUERY_NOT_ALLOWLISTED)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AI_EXTERNAL_QUERY_DENIED_DETAIL)
            _export_control_marker(request, user, api_token_record)
        return user

    # An interactive credential: clear the markers explicitly. The same ORM
    # instance can serve more than one request (a shared identity map), and an
    # unset marker is what tells ``AuditService`` this write was the person's own
    # -- and what keeps a person from inheriting a token's read-only narrowing.
    user._api_token_id = None
    user._api_token_scope = None
    user._api_token_ai_external = False
    # How this person signed in, from the access JWT's ``amr`` claim: ``["badge"]`` or
    # ``["badge", "pin"]`` on a badge / employee-ID session (``POST /auth/employee-login``,
    # ``POST /auth/kiosk-badge-token`` and the refreshes / company switches that carry it),
    # ``None`` on a password session. ``AuditService`` folds it into every row this request
    # writes (``extra_data.auth_methods``). Set on every request, never left over from the
    # last one, for the shared-identity-map reason above.
    user._auth_methods = list(identity.auth_methods) if identity.auth_methods else None
    # The PIN-set stamp (``pst``) the session verified, carried by switch-company into its new
    # tokens exactly as ``amr`` is; None on every other session.
    user._pin_set_stamp = identity.pin_set_stamp

    # Attach active company context from JWT (may differ from user.company_id
    # when a platform admin switches to view another company)
    user._active_company_id = identity.company_id
    user._read_only_company_context = identity.read_only
    # Token scope ("kiosk" for badge-minted crew-station operator tokens, None
    # otherwise) — surfaced so shop-floor writes can derive their adoption-telemetry
    # channel (TimeEntrySource KIOSK vs DESKTOP) from the credential, not the client.
    user._token_scope = identity.scope

    if (
        user._read_only_company_context
        and request.method.upper() not in SAFE_READ_ONLY_METHODS
        and not _is_read_only_exempt_path(request.url.path)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only company context cannot modify data",
        )

    _tag_sentry_principal(user)
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


def get_kiosk_station(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> KioskStation:
    """Accept ONLY a crew-station token, for station selection and badge minting.

    Station-token path checks, in order (the wallboard/signin two-layer pattern):
      1. signature + JWT expiry + ``type == "kiosk"`` (``verify_kiosk_token``)
      2. the ``kiosk_stations`` row exists for the JWT's ``sid``
      3. the row is not revoked
      4. the JWT's ``cid`` claim matches the row's ``company_id``
    The active company AND the selected work center come from the DB row
    (authoritative), so a forged or stale claim can never widen tenant scope or
    point the station at another work center.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    claims = verify_kiosk_token(token)
    if claims is None or not claims.get("station_id"):
        raise credentials_exception

    station = db.query(KioskStation).filter(KioskStation.id == claims["station_id"]).first()
    if station is None or station.revoked:
        raise credentials_exception
    if claims.get("company_id") != station.company_id:
        raise credentials_exception

    return station


def get_kiosk_or_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> KioskReadPrincipal:
    """Accept a user or station token for the read-only work-center queue.

    A station may only read its currently selected work center's queue. Its
    station token also gates workstation selection and badge minting through
    ``get_kiosk_station``; normal user endpoints still reject kiosk JWTs.
    """
    # Normal user token first — get_current_user applies all user checks,
    # including the kiosk-scope path fence for badge-minted operator tokens.
    if is_user_bearer(token):
        user = get_current_user(request=request, db=db, token=token)
        return KioskReadPrincipal(company_id=user._active_company_id, kind="user", user=user)

    station = get_kiosk_station(db=db, token=token)
    return KioskReadPrincipal(
        company_id=station.company_id,
        kind="station",
        station_id=station.id,
        station_label=station.label,
        work_center_id=station.work_center_id,
    )
