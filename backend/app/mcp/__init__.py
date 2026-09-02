"""Werco ERP over the Model Context Protocol.

The package turns the FastAPI application's OpenAPI document into an MCP tool catalog
and dispatches every tool call back through the real routers, as the calling user --
the router is the RBAC / tenancy / audit boundary, so nothing here imports a service
or builds a user; the one database read is the HTTP door's API-token row check
(``auth.live_api_token_principal``, the same ``check_api_token`` the routes run). See
``docs/MCP.md``.

Public surface:

- ``build_server(executor, *, catalog, ...)`` -- ``app.mcp.server``
- ``mount_mcp(app)`` -- ``app.mcp.http`` (the Streamable HTTP door; off by default)
- ``python -m app.mcp`` -- the stdio bridge / catalog printer (``app.mcp.__main__``)

The two exports are resolved lazily on purpose: ``python -m app.mcp`` imports this
package BEFORE ``__main__`` runs, and ``__main__`` must be able to set the remote-mode
environment defaults before anything imports ``app.core.config``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_server", "mount_mcp"]


def __getattr__(name: str) -> Any:
    if name == "build_server":
        from app.mcp.server import build_server

        return build_server
    if name == "mount_mcp":
        from app.mcp.http import mount_mcp

        return mount_mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
