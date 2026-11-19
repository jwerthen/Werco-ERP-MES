from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, List, Optional, Sequence, Tuple

import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Authentication-methods-reference claim (RFC 8176 ``amr``): HOW the person signed in.
# Written ONLY on a badge / employee-ID session -- ``["badge"]``, or ``["badge", "pin"]`` once
# a floor PIN was verified (``app.core.floor_pin``) -- so a password-login token carries no
# such claim and stays byte-identical to what it always was. ``POST /auth/refresh`` and
# ``POST /auth/switch-company`` carry it from the presented token into the new ones, and
# ``get_current_user`` copies it onto ``user._auth_methods``, which ``AuditService`` folds
# into every audit row the session writes.
AMR_CLAIM = "amr"
# PIN-set stamp (``pst``): on a session whose ``amr`` includes "pin", the ``floor_pin_set_at``
# of the PIN that was verified, as integer epoch-microseconds (``app.core.floor_pin.floor_pin_stamp``).
# ``resolve_access_identity``, refresh and switch-company refuse the session once the user's PIN
# is gone or was set again since (it no longer equals the stamp). Written only when given, so a
# password or badge-only token is unchanged; carried through refresh and switch-company with ``amr``.
PIN_SET_CLAIM = "pst"
# A bound on what the claim may hold; every value this app mints has one or two entries.
_MAX_AUTH_METHODS = 8
_MAX_AUTH_METHOD_LENGTH = 32


def normalize_pin_set_stamp(value: Any) -> Optional[int]:
    """The ``pst`` claim as an int, or None when absent or not an integer (a bool is not one)."""
    return value if type(value) is int else None


def normalize_auth_methods(value: Any) -> Optional[List[str]]:
    """The ``amr`` claim as a list of method names, or ``None`` when absent or malformed.

    The claim is signed by this app, so this is a shape check, not a trust decision: a
    non-list, an empty list, or a list holding anything but short non-empty strings reads
    as "no claim" (``None``) -- never a partial list that could under-report the methods.
    """
    if not isinstance(value, (list, tuple)) or not value or len(value) > _MAX_AUTH_METHODS:
        return None
    methods: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_AUTH_METHOD_LENGTH:
            return None
        methods.append(item)
    return methods


def create_access_token(
    subject: str | Any,
    expires_delta: Optional[timedelta] = None,
    company_id: Optional[int] = None,
    read_only: bool = False,
    scope: Optional[str] = None,
    auth_methods: Optional[Sequence[str]] = None,
    pin_set_stamp: Optional[int] = None,
) -> str:
    """Create a short-lived access token (default 15 minutes).

    ``scope`` optionally constrains WHERE the token is honored: a
    ``scope="kiosk"`` token (minted by ``POST /auth/kiosk-badge-token``) is
    path-fenced by ``get_current_user`` to the shop-floor endpoints (+
    employee-logout) and 403s everywhere else. Tokens without a scope claim
    are unaffected.

    ``auth_methods`` writes the ``amr`` claim (see ``AMR_CLAIM``) and ONLY when given:
    a call that does not pass it -- password login, company registration -- mints
    exactly the claims it always did. ``pin_set_stamp`` writes ``pst`` (``PIN_SET_CLAIM``)
    the same way, for a session whose PIN was verified.
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "type": "access",
        "iat": datetime.utcnow(),
        "ro": read_only,
    }
    if company_id is not None:
        to_encode["cid"] = company_id
    if scope is not None:
        to_encode["scope"] = scope
    if auth_methods is not None:
        to_encode[AMR_CLAIM] = list(auth_methods)
    if pin_set_stamp is not None:
        to_encode[PIN_SET_CLAIM] = pin_set_stamp
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(
    subject: str | Any,
    session_id: Optional[str] = None,
    company_id: Optional[int] = None,
    read_only: bool = False,
    auth_methods: Optional[Sequence[str]] = None,
    pin_set_stamp: Optional[int] = None,
) -> Tuple[str, str, datetime]:
    """
    Create a longer-lived refresh token (default 7 days).
    Returns: (token, session_id, expiry_time)

    ``auth_methods`` writes the ``amr`` claim only when given (see ``AMR_CLAIM``), so
    ``POST /auth/refresh`` can hand a badge session's methods to the next access token
    without a password session's refresh token ever gaining the claim. ``pin_set_stamp``
    writes ``pst`` (``PIN_SET_CLAIM``) the same way.
    """
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # Session idle window, NOT a hard ceiling despite the claim name. This function
    # takes no parameter to carry an existing stamp forward, and POST /auth/refresh
    # re-mints through here on every rotation, so each refresh restarts the clock.
    # What it actually bounds is how long a session can sit UNUSED. Corrected
    # 2026-07-29; the old comment claimed "even with refresh, can't exceed this",
    # which the rotation path has never honored.
    absolute_timeout = datetime.utcnow() + timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS)

    # Generate or reuse session ID for tracking
    if not session_id:
        session_id = secrets.token_urlsafe(32)

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "type": "refresh",
        "session_id": session_id,
        "absolute_timeout": absolute_timeout.isoformat(),
        "iat": datetime.utcnow(),
        "ro": read_only,
    }
    if company_id is not None:
        to_encode["cid"] = company_id
    if auth_methods is not None:
        to_encode[AMR_CLAIM] = list(auth_methods)
    if pin_set_stamp is not None:
        to_encode[PIN_SET_CLAIM] = pin_set_stamp
    encoded_jwt = jwt.encode(to_encode, settings.REFRESH_TOKEN_SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt, session_id, expire


def create_display_token(jti: str, company_id: int, label: str, expires_at: datetime) -> str:
    """Create a long-lived, scope-limited token for an unattended TV wallboard (A0.5).

    The ``type`` claim is ``"display"`` — NOT ``"access"`` — so ``verify_token``
    (and therefore ``get_current_user`` and every dependency built on it)
    rejects it. Display tokens only authenticate via the dedicated
    ``get_display_or_user`` dependency on the read-only wallboard endpoint.
    ``jti`` ties the JWT to a ``display_tokens`` row so admins can revoke it.
    """
    to_encode = {
        "exp": expires_at,
        "sub": f"display:{jti}",
        "type": "display",
        "jti": jti,
        "cid": company_id,
        "label": label,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_display_token(token: str) -> Optional[dict]:
    """Verify a display token and return its claims, or None.

    Only checks signature/expiry/type; the caller MUST additionally check the
    ``display_tokens`` row (exists, not revoked, not past ``expires_at``) —
    the DB row is the revocation authority.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "display":
            return None
        return {
            "jti": payload.get("jti"),
            "company_id": payload.get("cid"),
            "label": payload.get("label"),
        }
    except InvalidTokenError:
        return None


API_TOKEN_TYPE = "api"
# Narrowing claims, present ONLY on a narrowed token so a FULL token's JWT is byte-for-byte
# the shape it always was. ``api_scope`` -- not ``scope``, which access JWTs already use for
# the kiosk badge. The api_tokens ROW stays authoritative: ``check_api_token`` refuses a
# token whose claims disagree with its row, so a claim can only ever NARROW (the stdio
# bridge reads it to filter its tool listing without a database).
API_SCOPE_CLAIM = "api_scope"
AI_EXTERNAL_CLAIM = "ai_ext"
_API_SCOPE_FULL = "full"


def create_api_token(
    jti: str,
    user_id: int,
    company_id: int,
    expires_at: Optional[datetime] = None,
    *,
    scope: str = _API_SCOPE_FULL,
    ai_external: bool = False,
) -> str:
    """Create a long-lived, revocable API token bound to ONE real user.

    The ``type`` claim is ``"api"`` -- NOT ``"access"`` -- so ``verify_token`` rejects
    it; API tokens authenticate only through the dedicated branch in
    ``app.api.deps.get_current_user``, which resolves the ``api_tokens`` row by
    ``jti`` on EVERY request (``app.services.api_token_service.check_api_token``)
    and treats the ROW, never these claims, as authoritative for the user, the
    company, revocation and expiry.

    ``exp`` is set ONLY when ``expires_at`` is given: the owner's default for a
    standing bot credential is "never expires", and PyJWT accepts a token
    with no ``exp``. Rotating ``SECRET_KEY`` invalidates every API token at once
    -- that is a feature, not a hazard.

    ``scope`` / ``ai_external`` mirror the row (migration ``119``) and add the
    ``api_scope`` / ``ai_ext`` claims only for a narrowed token; a full token's claims
    are unchanged.
    """
    to_encode: dict[str, Any] = {
        "sub": f"api:{jti}",
        "type": API_TOKEN_TYPE,
        "jti": jti,
        "user_id": user_id,
        "company_id": company_id,
        "iat": datetime.utcnow(),
    }
    if expires_at is not None:
        to_encode["exp"] = expires_at
    if scope != _API_SCOPE_FULL:
        to_encode[API_SCOPE_CLAIM] = scope
    if ai_external:
        to_encode[AI_EXTERNAL_CLAIM] = True
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_api_token(token: str) -> Optional[dict]:
    """Verify an API token's signature (+ ``exp`` when present) and ``type == "api"``.

    Mirrors ``verify_display_token``: this is the SIGNATURE half only. The caller
    MUST additionally run the ``api_tokens`` row checks (exists for ``jti``, not
    revoked, not past the row's ``expires_at``, claims match the row, user
    active) -- ``app.services.api_token_service.check_api_token`` is the one
    function that does, for the API and the MCP door alike.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != API_TOKEN_TYPE:
            return None
        claims = {
            "jti": payload.get("jti"),
            "user_id": payload.get("user_id"),
            "company_id": payload.get("company_id"),
        }
        # The narrowing claims pass through only when present, so a full token's claim
        # dict is exactly the three keys it always was.
        for narrowing in (API_SCOPE_CLAIM, AI_EXTERNAL_CLAIM):
            if narrowing in payload:
                claims[narrowing] = payload[narrowing]
        return claims
    except InvalidTokenError:
        return None


def create_signin_token(station_id: int, company_id: int, label: str, ttl_hours: int = 24) -> str:
    """Create a scope-limited token for an unattended visitor sign-in tablet.

    Twin of ``create_display_token``: the ``type`` claim is ``"signin"`` — NOT
    ``"access"`` — so ``verify_token`` (and therefore ``get_current_user`` and
    every dependency built on it) rejects it. Signin tokens only authenticate
    via the dedicated ``get_signin_principal`` dependency on the two visitor
    write endpoints. ``sid`` ties the JWT to a ``signin_stations`` row so admins
    can revoke it; ``cid`` is cross-checked against that row (the DB row is
    authoritative).
    """
    expire = datetime.utcnow() + timedelta(hours=ttl_hours)
    to_encode = {
        "exp": expire,
        "sub": f"signin:{station_id}",
        "type": "signin",
        "sid": station_id,
        "cid": company_id,
        "label": label,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_signin_token(token: str) -> Optional[dict]:
    """Verify a station signin token and return its claims, or None.

    Twin of ``verify_display_token``: only checks signature/expiry/type. The
    caller MUST additionally check the ``signin_stations`` row (exists, not
    revoked, ``cid`` matches the row's ``company_id``) — the DB row is the
    revocation authority and the tenant-scoping source of truth.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "signin":
            return None
        return {
            "station_id": payload.get("sid"),
            "company_id": payload.get("cid"),
            "label": payload.get("label"),
        }
    except InvalidTokenError:
        return None


def create_kiosk_token(station_id: int, company_id: int, label: str, ttl_hours: int = 24) -> str:
    """Create a scope-limited token for an unattended crew-station kiosk tablet.

    Twin of ``create_signin_token``: the ``type`` claim is ``"kiosk"`` — NOT
    ``"access"`` — so ``verify_token`` (and therefore ``get_current_user`` and
    every dependency built on it) rejects it. Kiosk tokens only authenticate
    via ``get_kiosk_station`` for workstation selection and badge minting,
    and via ``get_kiosk_or_user`` for the roster-enriched queue read.
    ``sid`` ties the JWT to a
    ``kiosk_stations`` row so admins can revoke it; ``cid`` is cross-checked
    against that row (the DB row is authoritative).
    """
    expire = datetime.utcnow() + timedelta(hours=ttl_hours)
    to_encode = {
        "exp": expire,
        "sub": f"kiosk:{station_id}",
        "type": "kiosk",
        "sid": station_id,
        "cid": company_id,
        "label": label,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_kiosk_token(token: str) -> Optional[dict]:
    """Verify a crew-station kiosk token and return its claims, or None.

    Twin of ``verify_signin_token``: only checks signature/expiry/type. The
    caller MUST additionally check the ``kiosk_stations`` row (exists, not
    revoked, ``cid`` matches the row's ``company_id``) — the DB row is the
    revocation authority and the tenant-scoping source of truth.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "kiosk":
            return None
        return {
            "station_id": payload.get("sid"),
            "company_id": payload.get("cid"),
            "label": payload.get("label"),
        }
    except InvalidTokenError:
        return None


def verify_token(token: str) -> Optional[dict]:
    """Verify an access token and return user context claims.

    SECURITY: the ``type == "access"`` check below is what fences display
    tokens (``type == "display"``, see ``create_display_token``), signin/kiosk
    station tokens, and refresh tokens out of every user-auth dependency. Do
    not relax it.

    ``scope`` is surfaced so ``get_current_user`` can path-fence
    kiosk-scoped operator tokens (``scope == "kiosk"``); ``None`` for every
    token minted without a scope claim.

    ``auth_methods`` is the ``amr`` claim (``normalize_auth_methods``); ``None`` for a
    password session and every token minted before the claim existed.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        return {
            "user_id": payload.get("sub"),
            "company_id": payload.get("cid"),  # None for legacy tokens
            "read_only": bool(payload.get("ro", False)),
            "scope": payload.get("scope"),  # None for unscoped tokens
            "auth_methods": normalize_auth_methods(payload.get(AMR_CLAIM)),  # None without amr
            "pin_set_stamp": normalize_pin_set_stamp(payload.get(PIN_SET_CLAIM)),  # None without pst
        }
    except InvalidTokenError:
        return None


def verify_refresh_token(token: str) -> Optional[dict]:
    """
    Verify a refresh token and return payload with user_id, session_id, and absolute_timeout.
    Returns None if invalid or expired.
    """
    try:
        payload = jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            return None

        # Check absolute session timeout
        absolute_timeout_str = payload.get("absolute_timeout")
        if absolute_timeout_str:
            absolute_timeout = datetime.fromisoformat(absolute_timeout_str)
            if datetime.utcnow() > absolute_timeout:
                return None  # Session has exceeded absolute timeout

        return {
            "user_id": payload.get("sub"),
            "session_id": payload.get("session_id"),
            "absolute_timeout": absolute_timeout_str,
            "company_id": payload.get("cid"),
            "read_only": bool(payload.get("ro", False)),
            # The badge session's sign-on methods, carried into the rotated tokens.
            "auth_methods": normalize_auth_methods(payload.get(AMR_CLAIM)),
            "pin_set_stamp": normalize_pin_set_stamp(payload.get(PIN_SET_CLAIM)),
        }
    except InvalidTokenError:
        return None


async def get_current_user_from_token(token: str, db: AsyncSession) -> User:
    """Get current user from JWT token (async version for WebSockets)."""

    payload = verify_token(token)
    if payload is None:
        raise Exception("Could not validate credentials")

    user_id = payload.get("user_id")
    if user_id is None:
        raise Exception("Could not validate credentials")

    user = await db.execute(select(User).filter(User.id == int(user_id)))
    user = user.scalar_one_or_none()

    if user is None:
        raise Exception("User not found")

    if not user.is_active:
        raise Exception("User account is disabled")

    return user


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)
