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
- Two-tier rate limiting. A tool call served here dispatches an inner request, and
  that INNER hit is kept byte-identical to the SPA's (100/60 s per IP by default, plus
  the per-path limits), so the door is registered with the app limiter's ``exempt``
  and a tool call is charged once, not twice. The door PATH then carries its own
  ceiling through ``main.py``'s per-path resolver -- ``WERCO_MCP_HTTP_RATE_LIMIT``,
  300/minute by default, above the inner default so it never binds a tool call --
  because ``tools/list`` (~0.5 MB of JSON per call), ``initialize``, schema-rejected
  calls and unauthenticated 401 probes dispatch nothing inward and would otherwise have
  no ceiling at all.
- ``GET`` is 405. In stateless mode there is no server-push stream to open (the door
  never writes to one), and the SDK's own stateless handler answers a GET the same
  way; answering here also keeps the app's ``GZipMiddleware`` from holding an SSE
  response's headers until the transport's first 15 s keepalive ping.
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
from starlette.responses import JSONResponse, Response
from starlette.routing import Match
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
        if scope.get("type") == "http" and scope.get("method") in ("GET", "HEAD"):
            # Stateless: no push stream exists to open (see the module docstring).
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
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

    path = settings.WERCO_MCP_HTTP_PATH
    refuse_occupied_path(app, path)
    door = build_door(app)
    app.add_route(path, door, include_in_schema=False, name=DOOR_ROUTE_NAME)

    limiter = getattr(app.state, "limiter", None)
    if limiter is not None:
        # Waive the OUTER default limit on the door; the inner route hit stays.
        limiter.exempt(door)

    app.state.mcp_door = door
    return door


def refuse_occupied_path(app: Any, path: str) -> None:
    """Raise at boot if any existing route already answers ``path``.

    Starlette matches routes in order, so a door added at a path an API route (or a
    docs route, or a health probe) already owns would never be reached -- MCP would
    silently not be served -- while ``main.py``'s ``MAX_JSON_BODY_BYTES`` exemption,
    which keys on the path string alone, would waive the JSON cap for THAT route
    instead. ``config.validate_mcp_http_path`` refuses the reserved prefixes up front;
    this is the belt to that brace, checked against the live route table, and it
    fails the deploy loudly rather than serving an uncapped write route.
    """
    probe: Scope = {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": [], "query_string": b""}
    for route in app.router.routes:
        match, _child_scope = route.matches(probe)
        if match is not Match.NONE:  # PARTIAL = same path, different method: still occupied
            raise RuntimeError(
                f"WERCO_MCP_HTTP_PATH={path!r} is already served by {route!r}; the MCP door needs a path no "
                "route uses (the default /mcp is free)."
            )


def unmount_mcp(app: Any) -> None:
    """Remove a mounted door again (test support; production never unmounts)."""
    door = getattr(app.state, "mcp_door", None)
    if door is None:
        return
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "endpoint", None) is not door]
    del app.state.mcp_door
