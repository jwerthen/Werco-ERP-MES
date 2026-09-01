"""``python -m app.mcp`` -- the stdio bridge, the dev HTTP server, and ``--print-catalog``.

Modes (decided by the environment, see ``docs/MCP.md``):

- ``WERCO_ERP_URL`` set  -> REMOTE: tools go over HTTPS to that deployment; credentials
  come from ``WERCO_ERP_TOKEN`` / ``WERCO_ERP_REFRESH_TOKEN`` / ``WERCO_ERP_EMAIL`` +
  ``WERCO_ERP_PASSWORD``. The catalog is still built locally from ``app.openapi()``,
  which needs ``app.main`` importable -- so before importing it, any MISSING
  ``DATABASE_URL`` / ``SECRET_KEY`` / ``REFRESH_TOKEN_SECRET_KEY`` / ``ENVIRONMENT`` /
  ``RATE_LIMIT_ENABLED`` is defaulted to a placeholder. The placeholders are never used
  for anything: no connection is opened and the REMOTE server signs the tokens.
- ``WERCO_ERP_URL`` unset -> IN-PROCESS: tools dispatch into the local app object, which
  needs the real environment (a DATABASE_URL, the real SECRET_KEY). Dev only.

stdout is the MCP wire. The app's own logging handler writes to ``sys.stdout``
(``app/core/logging.py``), so the wire is captured FIRST and ``sys.stdout`` is
re-pointed at stderr before ``app.main`` is imported; everything the process prints
from then on lands on stderr, and the SDK is handed the captured wire explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import IO, Any, Optional, Sequence

_PLACEHOLDER_ENV = {
    "DATABASE_URL": "sqlite://",
    "SECRET_KEY": "mcp-remote-placeholder-secret-key-never-used-to-sign-anything-0",
    "REFRESH_TOKEN_SECRET_KEY": "mcp-remote-placeholder-refresh-key-never-used-to-sign-anything-0",
    "ENVIRONMENT": "development",
    "RATE_LIMIT_ENABLED": "false",
}

ENV_URL = "WERCO_ERP_URL"
ENV_TRANSPORT = "WERCO_MCP_TRANSPORT"
ENV_HOST = "WERCO_MCP_HOST"
ENV_PORT = "WERCO_MCP_PORT"


def install_placeholder_env(environ: Optional[dict] = None) -> None:
    """Default the app-boot variables the remote bridge does not need (never overrides a set one)."""
    env = os.environ if environ is None else environ
    for key, value in _PLACEHOLDER_ENV.items():
        env.setdefault(key, value)


def _capture_wire() -> IO[str]:
    """Keep the real stdout for the MCP wire; send everything else to stderr."""
    wire = sys.stdout
    sys.stdout = sys.stderr
    return wire


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.mcp", description="Werco ERP MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="stdio (default; the bridge agents spawn) or http (a DEV-ONLY local Streamable HTTP server).",
    )
    parser.add_argument("--host", default=None, help="http transport bind host (default WERCO_MCP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="http transport port (default WERCO_MCP_PORT or 8765)")
    parser.add_argument(
        "--print-catalog",
        action="store_true",
        help="Print the tool catalog (convenience + generated) as JSON to stdout and exit. Needs no token.",
    )
    return parser.parse_args(argv)


async def _serve_stdio(server: Any, wire: IO[str], executor: Any) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    try:
        async with stdio_server(stdout=anyio.wrap_file(wire)) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        # The remote executor holds a pooled httpx client for the whole session; release it
        # on the way out rather than leaving it to garbage collection at loop teardown.
        await executor.aclose()


def _serve_http(server: Any, *, host: str, port: int, verifier: Any, max_body: int, executor: Any) -> None:
    """DEV ONLY: a local Streamable HTTP endpoint at ``settings.WERCO_MCP_HTTP_PATH``."""
    from contextlib import asynccontextmanager

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route

    from app.core.config import settings
    from app.mcp.http import McpDoor

    door = McpDoor(server, verifier=verifier, max_request_body_size=max_body)

    @asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        try:
            async with door.lifespan():
                yield
        finally:
            await executor.aclose()

    starlette_app = Starlette(routes=[Route(settings.WERCO_MCP_HTTP_PATH, endpoint=door)], lifespan=lifespan)
    logging.getLogger(__name__).info("MCP dev HTTP server on http://%s:%s%s", host, port, settings.WERCO_MCP_HTTP_PATH)
    uvicorn.run(starlette_app, host=host, port=port, log_level="info")


class _RemoteBearerPassthrough:
    """Door verifier for the dev HTTP bridge in front of a REMOTE ERP.

    This process cannot verify tokens the remote deployment signed, so the door only
    insists a bearer token is PRESENT; the remote routes authenticate every call.
    """

    async def verify_token(self, token: str) -> Any:
        from mcp.server.auth.provider import AccessToken

        if not token.strip():
            return None
        return AccessToken(token=token, client_id="remote", scopes=[], expires_at=None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    remote_url = (os.environ.get(ENV_URL) or "").strip().rstrip("/") or None
    if remote_url or args.print_catalog:
        install_placeholder_env()

    wire = _capture_wire()
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO").upper())
    log = logging.getLogger("app.mcp")

    # Imported only now: the placeholders above must be in place first.
    from app.core.config import settings
    from app.main import app
    from app.mcp.auth import ErpTokenVerifier, TokenSource
    from app.mcp.catalog import build_catalog, catalog_summary
    from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS
    from app.mcp.executor import InProcessExecutor, RemoteExecutor
    from app.mcp.server import build_server

    catalog = build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)

    if args.print_catalog:
        payload = {
            "server": "werco-erp",
            "version": app.version,
            "convenience_tools": [
                {"name": tool.name, "description": tool.description, "annotations": tool.hints.as_dict()}
                for tool in CONVENIENCE_TOOLS
            ],
            "generated_tools": catalog_summary(catalog),
            "shadowed_operations": sorted(list(pair) for pair in SHADOWED_OPERATIONS),
            "counts": {
                "convenience": len(CONVENIENCE_TOOLS),
                "generated": len(catalog),
                "shadowed": len(SHADOWED_OPERATIONS),
            },
        }
        try:
            json.dump(payload, wire, indent=1)
            wire.write("\n")
            wire.flush()
        except BrokenPipeError:
            # `... | head` closed the pipe; that is the reader's choice, not an error.
            pass
        return 0

    executor: Any
    if remote_url:
        executor = RemoteExecutor(remote_url, version=app.version)
        log.info("Werco MCP bridge: REMOTE mode -> %s", remote_url)
    else:
        executor = InProcessExecutor(app, version=app.version)
        log.info("Werco MCP bridge: IN-PROCESS mode (local database)")

    token_source = TokenSource.from_env(executor)
    if token_source is None:
        log.warning(
            "No ERP credentials configured (WERCO_ERP_TOKEN / WERCO_ERP_REFRESH_TOKEN / WERCO_ERP_EMAIL+PASSWORD); "
            "every tool call will answer 401 until one is set."
        )
    else:
        log.info("Credentials configured: %s", token_source.describe())

    server = build_server(
        executor,
        catalog=catalog,
        token_source=token_source,
        version=app.version,
        verify_bearer=remote_url is None,
    )
    log.info("Catalog: %d convenience + %d generated tools", len(CONVENIENCE_TOOLS), len(catalog))

    transport = args.transport or (os.environ.get(ENV_TRANSPORT) or "stdio").strip().lower()
    if transport == "http":
        host = args.host or os.environ.get(ENV_HOST) or "127.0.0.1"
        port = args.port or int(os.environ.get(ENV_PORT) or "8765")
        verifier: Any = _RemoteBearerPassthrough() if remote_url else ErpTokenVerifier()
        _serve_http(
            server,
            host=host,
            port=port,
            verifier=verifier,
            max_body=settings.WERCO_MCP_MAX_UPLOAD_BYTES,
            executor=executor,
        )
        return 0

    asyncio.run(_serve_stdio(server, wire, executor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
