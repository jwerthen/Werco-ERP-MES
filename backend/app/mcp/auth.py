"""Credentials for the MCP server: where a call's user JWT comes from, and the door check.

Two halves, one rule: there is NO god token (brief rule 2). Every tool call carries a
real ERP user's access token and the route it dispatches to authenticates it exactly
as it would the SPA's request -- ``get_current_user`` resolves the user, tenancy
comes from the token's active company, ``require_role`` decides, and audit rows are
written as that user.

- ``TokenSource`` is the stdio bridge's side: the token comes from the environment
  (a static access token, a refresh token to rotate through ``POST /auth/refresh``,
  or an email + password to log in through ``POST /auth/login``). Access tokens live
  15 minutes, so a bridge that only held a static token would go dark mid-session;
  refresh-on-401 is what keeps a long agent session alive without ever widening what
  the token can do.
- ``ErpTokenVerifier`` is the HTTP door's side: the SDK's bearer middleware asks it
  whether a token is acceptable BEFORE any JSON-RPC is parsed, so an expired or
  kiosk-scoped token gets one clean 401 + ``WWW-Authenticate`` at the door instead of
  ~650 identical tool errors. The routes still re-validate on every dispatch; the door
  check is a courtesy, not the control.

Nothing here logs a token or a password.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from mcp.server.auth.provider import AccessToken

from app.core.security import verify_token as _verify_erp_access_token
from app.mcp.results import ExecResult

KIOSK_SCOPE = "kiosk"
LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"

ENV_TOKEN = "WERCO_ERP_TOKEN"
ENV_REFRESH_TOKEN = "WERCO_ERP_REFRESH_TOKEN"
ENV_EMAIL = "WERCO_ERP_EMAIL"
ENV_PASSWORD = "WERCO_ERP_PASSWORD"  # nosec B105 - the NAME of an env var, not a secret


class TokenExchange(Protocol):
    """The slice of an executor a ``TokenSource`` needs: an unauthenticated POST."""

    async def request(
        self,
        *,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        files: Any = None,
        form: Optional[Mapping[str, Any]] = None,
        auth: Optional["AuthContext"] = None,
    ) -> ExecResult: ...


@dataclass
class AuthContext:
    """Everything the executor forwards about WHO is calling, resolved once per tool call.

    ``client_host`` keys the app's per-IP rate limits: the MCP caller's address on the
    HTTP door, ``127.0.0.1`` on stdio. ``host_header`` lets an in-process dispatch
    present the same ``Host`` the caller used so ``TrustedHostMiddleware`` sees a
    request it would have accepted from the SPA. ``token_source`` is set only when the
    token came from the bridge-side ``TokenSource`` -- that is the one case a 401 may
    be retried after a refresh; a door caller's expired token is their own to renew.
    """

    token: str
    client_host: str = "127.0.0.1"
    host_header: Optional[str] = None
    token_source: Optional["TokenSource"] = None


def bearer_token_from_header(value: Optional[str]) -> Optional[str]:
    """``"Bearer abc"`` -> ``"abc"``; anything else -> None."""
    if not value:
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def access_token_claims(token: str) -> Optional[Mapping[str, Any]]:
    """The app's own verification (signature, expiry, ``type == "access"``) or None.

    Kiosk-scoped badge tokens (``scope == "kiosk"``) are rejected here as well: they are
    path-fenced to the shop-floor routes by ``get_current_user`` and would 403 on almost
    every tool, so refusing them up front is the honest answer.
    """
    claims = _verify_erp_access_token(token)
    if claims is None or claims.get("scope") == KIOSK_SCOPE or not claims.get("user_id"):
        return None
    return claims


def token_is_acceptable(token: str) -> bool:
    return access_token_claims(token) is not None


def unverified_expiry(token: str) -> Optional[int]:
    """The ``exp`` claim read WITHOUT verification, purely as metadata.

    Only ever called after ``verify_token`` accepted the token; the SDK's bearer
    backend uses it to short-circuit an already-expired token without a second
    signature check.
    """
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (IndexError, ValueError, TypeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


class ErpTokenVerifier:
    """``mcp.server.auth.provider.TokenVerifier`` backed by the app's ``verify_token``."""

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        claims = access_token_claims(token)
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims["user_id"]),
            scopes=[],
            expires_at=unverified_expiry(token),
        )


class TokenSource:
    """Bridge-side credential store: yields the current access token, rotates on 401.

    Precedence (brief 3.3): a static access token is used first; when the ERP answers
    401, a refresh token is tried, then an email/password login, and if neither is
    configured the 401 is surfaced as-is. Rotation is serialised with a lock because
    ``POST /auth/refresh`` ROTATES the refresh token -- two concurrent tool calls that
    both tried to refresh would invalidate each other's new token.
    """

    def __init__(
        self,
        exchange: TokenExchange,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._exchange = exchange
        self._access_token = access_token or None
        self._refresh_token = refresh_token or None
        self._email = email or None
        self._password = password or None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, exchange: TokenExchange, environ: Optional[Mapping[str, str]] = None) -> Optional["TokenSource"]:
        """Build from ``WERCO_ERP_*`` variables; None when nothing usable is set."""
        env = os.environ if environ is None else environ
        source = cls(
            exchange,
            access_token=(env.get(ENV_TOKEN) or "").strip(),
            refresh_token=(env.get(ENV_REFRESH_TOKEN) or "").strip(),
            email=(env.get(ENV_EMAIL) or "").strip(),
            password=env.get(ENV_PASSWORD) or "",
        )
        return source if source.configured else None

    @property
    def configured(self) -> bool:
        return bool(self._access_token or self._refresh_token or self.can_login)

    @property
    def can_login(self) -> bool:
        return bool(self._email and self._password)

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token)

    def describe(self) -> str:
        """A log-safe summary of WHICH credential kinds are configured (never their values)."""
        kinds = []
        if self._access_token:
            kinds.append("access-token")
        if self._refresh_token:
            kinds.append("refresh-token")
        if self.can_login:
            kinds.append("password-login")
        return ", ".join(kinds) or "none"

    async def get_token(self) -> Optional[str]:
        """The token to send on the next call, acquiring one first if only refresh/login is configured."""
        if self._access_token:
            return self._access_token
        async with self._lock:
            if self._access_token:
                return self._access_token
            return await self._acquire_locked()

    async def refresh_after_401(self, failed_token: str) -> Optional[str]:
        """Called by the executor after a 401: returns a NEW token to retry with, or None.

        If another call already rotated the token, hand that one back without hitting
        the server again.
        """
        async with self._lock:
            if self._access_token and self._access_token != failed_token:
                return self._access_token
            self._access_token = None
            return await self._acquire_locked()

    async def _acquire_locked(self) -> Optional[str]:
        token = None
        if self.can_refresh:
            token = await self._try_refresh()
        if token is None and self.can_login:
            token = await self._try_login()
        if token:
            self._access_token = token
        return token

    async def _try_refresh(self) -> Optional[str]:
        result = await self._exchange.request(
            method="POST", path=REFRESH_PATH, json={"refresh_token": self._refresh_token}, auth=None
        )
        payload = _json_or_none(result)
        if result.status != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            # A rejected refresh token is spent: forget it so the next attempt goes
            # straight to login instead of failing the same way again.
            if result.status in (400, 401, 403):
                self._refresh_token = None
            return None
        if payload.get("refresh_token"):
            self._refresh_token = str(payload["refresh_token"])
        return str(payload["access_token"])

    async def _try_login(self) -> Optional[str]:
        result = await self._exchange.request(
            method="POST",
            path=LOGIN_PATH,
            form={"username": self._email, "password": self._password},
            auth=None,
        )
        payload = _json_or_none(result)
        if result.status != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            return None
        if payload.get("refresh_token"):
            self._refresh_token = str(payload["refresh_token"])
        return str(payload["access_token"])


def _json_or_none(result: ExecResult) -> Any:
    if not result.content:
        return None
    try:
        return result.json()
    except ValueError:
        return None
