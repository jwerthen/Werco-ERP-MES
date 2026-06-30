from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(
    subject: str | Any,
    expires_delta: Optional[timedelta] = None,
    company_id: Optional[int] = None,
    read_only: bool = False,
) -> str:
    """Create a short-lived access token (default 15 minutes)."""
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
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(
    subject: str | Any,
    session_id: Optional[str] = None,
    company_id: Optional[int] = None,
    read_only: bool = False,
) -> Tuple[str, str, datetime]:
    """
    Create a longer-lived refresh token (default 7 days).
    Returns: (token, session_id, expiry_time)
    """
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # Absolute session timeout - even with refresh, can't exceed this
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
    except JWTError:
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
    except JWTError:
        return None


def verify_token(token: str) -> Optional[dict]:
    """Verify an access token and return user context claims.

    SECURITY: the ``type == "access"`` check below is what fences display
    tokens (``type == "display"``, see ``create_display_token``) and refresh
    tokens out of every user-auth dependency. Do not relax it.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        return {
            "user_id": payload.get("sub"),
            "company_id": payload.get("cid"),  # None for legacy tokens
            "read_only": bool(payload.get("ro", False)),
        }
    except JWTError:
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
        }
    except JWTError:
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
