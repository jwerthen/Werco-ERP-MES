"""The Streamable HTTP door: ``mount_mcp(app)``.

Serves the MCP server at ``settings.WERCO_MCP_HTTP_PATH`` (default ``/mcp``) ON THE
API HOST, so an agent (Cursor, Claude Code, a bot) configures one URL plus a bearer
header and every tool call dispatches back into this very process as that user.

Why it is shaped the way it is:

- OFF BY DEFAULT. ``WERCO_MCP_HTTP_ENABLED`` false means ``mount_mcp`` returns
  ``None`` and touches nothing: the API is byte-identical to a build without this
  package.
- A ``Route`` at the exact path, not a ``Mount``. Starlette's ``Mount`` matches only
  ``<path>/...`` and the router answers a bare ``/mcp`` with a 307 to ``/mcp/`` --
  which MCP clients do not follow for POST.
- STATELESS + JSON responses. Railway runs ``uvicorn --workers 2``; a stateful
  session pinned to one worker's memory would break on the next request, so
  ``stateless=True`` (one transport per POST, no session id). ``json_response=True``
  keeps each call a plain request/response the SPA's proxies already understand.
- The bearer chain is assembled HERE rather than via ``Server.streamable_http_app``:
  in mcp 2.1.1 that helper installs ``RequireAuthMiddleware`` when given a
  ``token_verifier`` but only installs the ``AuthenticationMiddleware`` that populates
  ``scope["user"]`` when a full OAuth ``AuthSettings`` is also passed -- without it the
  verifier is never consulted and every request is a 401. The chain below is exactly
  the one the SDK builds for the OAuth case, minus the OAuth metadata routes this
  server does not serve.
- Its own body cap. A nest PDF arrives base64-encoded INSIDE the JSON-RPC envelope,
  far over the app's 256 KB ``MAX_JSON_BODY_BYTES``; ``main.py`` exempts this path
  from that cap and the SDK's ``RequestBodyLimitMiddleware`` bounds it at
  ``WERCO_MCP_MAX_UPLOAD_BYTES`` instead (413 over it, before parsing).
- The OUTER rate-limit hit is waived. A tool call served here costs the caller one
  hit on this path plus one on the inner route; the inner hit is kept byte-identical
  to the SPA's, and this path is registered with the app limiter's ``exempt`` so agents
  are limited exactly like the SPA rather than at half rate.
- Lifespan. The SDK's session manager runs in a task group that must outlive every
  request, and Starlette never runs a sub-application's lifespan, so ``McpDoor.lifespan``
  is entered from ``main.py``'s own lifespan. Each entry builds a FRESH session manager
  (the SDK allows exactly one ``run()`` per instance), which is what lets the test
  suite's per-test ``TestClient`` lifespans re-enter it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.mcp.auth import ErpTokenVerifier
from app.mcp.catalog import build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.mcp.executor import Executor, InProcessExecutor
from app.mcp.server import build_server

DOOR_ROUTE_NAME = "mcp_door"


class McpDoor:
    """ASGI callable for the door, plus the lifespan that arms it.

    ``__name__`` / ``__module__`` are what slowapi's ``Limiter.exempt`` and its
    middleware's route-name lookup read; a plain instance has no ``__name__``.
    """

    __name__ = DOOR_ROUTE_NAME

    def __init__(self, server: Server, *, verifier: TokenVerifier, max_request_body_size: int) -> None:
        self.server = server
        self.verifier = verifier
        self.max_request_body_size = max_request_body_size
        self._app: Optional[ASGIApp] = None
        self.session_manager: Optional[StreamableHTTPSessionManager] = None

    def _build(self) -> tuple[StreamableHTTPSessionManager, ASGIApp]:
        manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=True,
            stateless=True,
            # Host/Origin pinning is the app's job (TrustedHostMiddleware, CSRF); the
            # SDK's DNS-rebinding guard would only duplicate it with a second allowlist.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            max_request_body_size=self.max_request_body_size,
        )
        transport: ASGIApp = StreamableHTTPASGIApp(manager)
        guarded: ASGIApp = RequireAuthMiddleware(transport, required_scopes=[], resource_metadata_url=None)
        chain: ASGIApp = AuthenticationMiddleware(
            AuthContextMiddleware(guarded), backend=BearerAuthBackend(self.verifier)
        )
        return manager, chain

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Arm the door for the duration of the app's lifespan (re-entrant across lifespans)."""
        manager, chain = self._build()
        async with manager.run():
            self.session_manager = manager
            self._app = chain
            try:
                yield
            finally:
                self._app = None
                self.session_manager = None

    @property
    def running(self) -> bool:
        return self._app is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self._app
        if app is None:
            response = JSONResponse(
                status_code=503,
                content={"detail": "MCP door is not running: the application lifespan has not been entered."},
            )
            await response(scope, receive, send)
            return
        await app(scope, receive, send)


def build_door(app: Any, *, executor: Optional[Executor] = None, verifier: Optional[TokenVerifier] = None) -> McpDoor:
    """Build (but do not mount) the door for ``app``: in-process executor, catalog from ``app.openapi()``."""
    executor = executor or InProcessExecutor(app, version=str(getattr(app, "version", "0")))
    catalog = build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)
    server = build_server(executor, catalog=catalog, version=str(getattr(app, "version", "0")))
    return McpDoor(
        server,
        verifier=verifier or ErpTokenVerifier(),
        max_request_body_size=settings.WERCO_MCP_MAX_UPLOAD_BYTES,
    )


def mount_mcp(app: Any) -> Optional[McpDoor]:
    """Mount the door on ``app`` when ``WERCO_MCP_HTTP_ENABLED``; otherwise do nothing.

    Call it AFTER every router is included (the catalog is read from ``app.openapi()``
    at this moment) and enter the returned door's ``lifespan()`` from the app's own
    lifespan. Idempotent: a second call returns the door already mounted.
    """
    if not settings.WERCO_MCP_HTTP_ENABLED:
        return None
    existing = getattr(app.state, "mcp_door", None)
    if isinstance(existing, McpDoor):
        return existing

    door = build_door(app)
    path = settings.WERCO_MCP_HTTP_PATH
    app.add_route(path, door, include_in_schema=False, name=DOOR_ROUTE_NAME)

    limiter = getattr(app.state, "limiter", None)
    if limiter is not None:
        # Waive the OUTER default limit on the door; the inner route hit stays.
        limiter.exempt(door)

    app.state.mcp_door = door
    return door


def unmount_mcp(app: Any) -> None:
    """Remove a mounted door again (test support; production never unmounts)."""
    door = getattr(app.state, "mcp_door", None)
    if door is None:
        return
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "endpoint", None) is not door]
    del app.state.mcp_door
