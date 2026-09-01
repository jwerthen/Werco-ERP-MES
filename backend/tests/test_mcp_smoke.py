"""Smoke test for the Werco ERP MCP server (``app/mcp``).

Proves the four things the package must get right before anything else is worth
testing: the catalog builds from the live ``app.openapi()`` with every name unique and
wire-valid; an MCP client connected over the SDK's in-memory transport can list tools
and create a work order THROUGH the real router as the test manager -- and it lands
DRAFT even though the caller asked for RELEASED; a call with no token is answered with a
401-shaped ``is_error`` result rather than a request; and the HTTP door, when mounted,
is waived from the app's OUTER default rate limit (the inner route hit is kept).

The full behavioural suite (``test_mcp_catalog.py`` / ``test_mcp_server.py`` /
``test_mcp_http_door.py`` / ``test_mcp_remote_executor.py``) builds on these.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams

from app.core.config import settings
from app.main import app
from app.mcp.auth import TokenSource
from app.mcp.catalog import build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS
from app.mcp.executor import InProcessExecutor
from app.mcp.naming import TOOL_NAME_PATTERN
from app.mcp.server import build_server
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = pytest.mark.api

# The live API has ~703 operations; after the unsecured, excluded-tag and shadowed ones
# are removed, well over 600 generated tools remain. A drop below this means a whole
# router (or the security declarations) went missing from the catalog.
MIN_GENERATED_TOOLS = 600


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _bearer(headers: dict) -> str:
    return headers["Authorization"].split(" ", 1)[1]


@asynccontextmanager
async def connected(server: Server) -> AsyncIterator[ClientSession]:
    """An initialized ``ClientSession`` talking to ``server`` over in-memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run, server_streams[0], server_streams[1], server.create_initialization_options(), True
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            task_group.cancel_scope.cancel()


class TestCatalog:
    def test_catalog_builds_from_openapi_with_unique_wire_valid_names(self, catalog):
        assert len(catalog) > MIN_GENERATED_TOOLS
        names = [tool.name for tool in catalog]
        assert len(set(names)) == len(names), "generated tool names must be unique"
        invalid = [name for name in names if not TOOL_NAME_PATTERN.match(name)]
        assert not invalid, f"names outside ^[a-zA-Z0-9_-]{{1,64}}$: {invalid}"

    def test_shadowed_routes_have_no_generated_twin(self, catalog):
        generated_keys = {tool.key for tool in catalog}
        assert generated_keys.isdisjoint(SHADOWED_OPERATIONS)
        convenience_names = {tool.name for tool in CONVENIENCE_TOOLS}
        assert convenience_names.isdisjoint({tool.name for tool in catalog})

    def test_collisions_are_prefixed_with_the_tag(self, catalog):
        names = {tool.name for tool in catalog}
        # start_operation exists on both the office and shop-floor routers.
        assert "start_operation" not in names
        assert {"work_orders_start_operation", "shop_floor_start_operation"} <= names
        # Shadowing the Work Orders add_operation must not hand the bare name to the
        # Routing router's add_operation: names are assigned before shadowing.
        assert "routing_add_operation" in names


class TestInMemoryClient:
    async def test_lists_tools_and_creates_a_draft_work_order_as_the_manager(
        self, client, db_session, manager_headers, test_part, catalog
    ):
        executor = InProcessExecutor(app, version=app.version)
        token_source = TokenSource(executor, access_token=_bearer(manager_headers))
        server = build_server(executor, catalog=catalog, token_source=token_source, version=app.version)

        async with connected(server) as session:
            listing = await session.list_tools()
            names = [tool.name for tool in listing.tools]
            assert len(names) > MIN_GENERATED_TOOLS
            assert names[: len(CONVENIENCE_TOOLS)] == [tool.name for tool in CONVENIENCE_TOOLS]
            assert "create_work_order" in names

            result = await session.call_tool(
                "create_work_order",
                {"part_id": test_part.id, "quantity_ordered": 5, "status": "released"},
            )

        assert not result.is_error, result.content
        created = result.structured_content
        assert created["status"] == "draft"
        assert created["part_id"] == test_part.id

        row = db_session.query(WorkOrder).filter(WorkOrder.id == created["id"]).one()
        assert row.status == WorkOrderStatus.DRAFT

    async def test_call_without_a_token_is_a_401_error_result(self, client, test_part, catalog):
        executor = InProcessExecutor(app, version=app.version)
        server = build_server(executor, catalog=catalog, token_source=None, version=app.version)

        async with connected(server) as session:
            result = await session.call_tool("create_work_order", {"part_id": test_part.id, "quantity_ordered": 1})

        assert result.is_error
        assert result.structured_content["status"] == 401
        assert "credentials" in result.structured_content["detail"].lower()


class TestHttpDoorRateLimitExemption:
    def test_mounted_door_is_exempt_from_the_outer_default_limit(self, monkeypatch):
        """The door path is registered with the limiter's ``exempt`` mechanism.

        slowapi's middleware resolves the matched route's endpoint and skips the
        default limit when its name is in the exempt set; this pins that resolution
        for the exact door path. The inner route hit (the real API request the tool
        dispatches) is untouched, so an agent is limited exactly like the SPA.
        """
        from slowapi.middleware import _find_route_handler, _should_exempt

        from app.mcp.http import mount_mcp, unmount_mcp

        assert settings.RATE_LIMIT_ENABLED, "test assumes the limiter is configured (default)"
        monkeypatch.setattr(settings, "WERCO_MCP_HTTP_ENABLED", True)
        door = mount_mcp(app)
        try:
            assert door is not None
            scope = {
                "type": "http",
                "method": "POST",
                "path": settings.WERCO_MCP_HTTP_PATH,
                "root_path": "",
                "headers": [],
            }
            handler = _find_route_handler(app.routes, scope)
            assert handler is door
            assert _should_exempt(app.state.limiter, handler)
        finally:
            unmount_mcp(app)
        assert all(getattr(route, "endpoint", None) is not door for route in app.routes)
