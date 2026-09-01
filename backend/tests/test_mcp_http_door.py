"""The Streamable HTTP door (``app/mcp/http.py`` + the ``main.py`` wiring).

The door is OFF by default; these tests arm it the way production does -- flip
``settings.WERCO_MCP_HTTP_ENABLED``, ``mount_mcp(app)`` after the routers, and let the
app's own lifespan enter the door's -- by opening a fresh ``TestClient`` after
mounting. ``app.main`` is never reloaded (every other test's ``TestClient`` holds the
same ``app`` object); ``unmount_mcp`` puts the router back on teardown.

What is proven, each at the wire:

- no / garbage / kiosk-scoped bearer -> 401 with ``WWW-Authenticate`` from the door,
  before any JSON-RPC is parsed;
- an operator can list tools, and a ``create_work_order`` call is the route's 403
  with its exact detail; a manager's call lands a DRAFT row;
- the auth for a door call comes from the HTTP request (the server is built with
  NO token source), i.e. ``ServerRequestContext.request`` carries the request;
- the app's 256 KB JSON cap is waived for the door path only, and the door's own
  ``WERCO_MCP_MAX_UPLOAD_BYTES`` bound answers 413 above it (base64 is larger than the
  bytes it encodes, so at the door the wire cap always fires first; the decode-time
  cap is exercised in ``test_mcp_server.py``);
- the door path is exempt from the OUTER default rate limit while the INNER route
  hit is still recorded, so an agent is limited exactly like the SPA;
- disabled (the default), ``/mcp`` does not exist.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_access_token
from app.main import app
from app.mcp.http import McpDoor, mount_mcp, unmount_mcp
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = pytest.mark.api

DOOR = "/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream"}
# Between the app's MAX_JSON_BODY_BYTES (256 KB) and the door's upload cap set below.
APP_CAP = 256 * 1024
SMALL_UPLOAD_CAP = 600_000


def _rpc(method: str, params: Dict[str, Any] | None = None, *, request_id: int = 1) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _auth(headers: Dict[str, str]) -> Dict[str, str]:
    return {**ACCEPT, "Authorization": headers["Authorization"]}


def _padded(body: Dict[str, Any], total_bytes: int) -> str:
    """The JSON-RPC envelope padded (in ``params._meta``) to about ``total_bytes``."""
    padded = json.loads(json.dumps(body))
    padded.setdefault("params", {}).setdefault("_meta", {})["padding"] = ""
    room = total_bytes - len(json.dumps(padded))
    padded["params"]["_meta"]["padding"] = "x" * max(room, 0)
    return json.dumps(padded)


@pytest.fixture
def door(client, monkeypatch) -> Iterator[McpDoor]:
    """The door mounted on the shared app for the duration of one test."""
    assert settings.WERCO_MCP_HTTP_ENABLED is False, "the door must be off by default"
    assert settings.WERCO_MCP_HTTP_PATH == DOOR
    monkeypatch.setattr(settings, "WERCO_MCP_HTTP_ENABLED", True)
    monkeypatch.setattr(settings, "WERCO_MCP_MAX_UPLOAD_BYTES", SMALL_UPLOAD_CAP)
    mounted = mount_mcp(app)
    assert mounted is not None
    assert mount_mcp(app) is mounted, "mounting is idempotent"
    try:
        yield mounted
    finally:
        unmount_mcp(app)
        assert all(getattr(route, "endpoint", None) is not mounted for route in app.routes)
        assert not hasattr(app.state, "mcp_door")


@pytest.fixture
def door_client(door) -> Iterator[TestClient]:
    """A client whose lifespan armed the door (``client`` keeps the DB override alive)."""
    with TestClient(app) as armed:
        assert door.running
        yield armed
    assert not door.running


class TestAuthAtTheDoor:
    def test_missing_bearer_is_401_with_www_authenticate(self, door_client):
        response = door_client.post(DOOR, json=_rpc("tools/list"), headers=ACCEPT)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
        assert response.json()["error"] == "invalid_token"

    def test_garbage_bearer_is_401(self, door_client):
        response = door_client.post(
            DOOR, json=_rpc("tools/list"), headers={**ACCEPT, "Authorization": "Bearer not.a.jwt"}
        )
        assert response.status_code == 401
        assert "www-authenticate" in response.headers

    def test_kiosk_scoped_token_is_refused_at_the_door(self, door_client, operator_user):
        kiosk = create_access_token(subject=operator_user.id, company_id=operator_user.company_id, scope="kiosk")
        response = door_client.post(
            DOOR, json=_rpc("tools/list"), headers={**ACCEPT, "Authorization": f"Bearer {kiosk}"}
        )
        assert response.status_code == 401
        assert "www-authenticate" in response.headers
        # The same user's UNSCOPED token opens the door: the refusal is the scope, not the user.
        plain = create_access_token(subject=operator_user.id, company_id=operator_user.company_id)
        response = door_client.post(
            DOOR, json=_rpc("tools/list"), headers={**ACCEPT, "Authorization": f"Bearer {plain}"}
        )
        assert response.status_code == 200

    def test_refresh_and_display_tokens_are_not_access_tokens(self, door_client, test_user):
        from app.core.security import create_refresh_token

        refresh = create_refresh_token(subject=test_user.id, company_id=test_user.company_id)
        response = door_client.post(
            DOOR, json=_rpc("tools/list"), headers={**ACCEPT, "Authorization": f"Bearer {refresh}"}
        )
        assert response.status_code == 401


class TestCallsThroughTheDoor:
    def test_operator_lists_tools_and_is_refused_on_create_with_the_exact_detail(
        self, door_client, db_session, operator_headers, test_part
    ):
        listing = door_client.post(DOOR, json=_rpc("tools/list"), headers=_auth(operator_headers))
        assert listing.status_code == 200
        names = [tool["name"] for tool in listing.json()["result"]["tools"]]
        assert "create_work_order" in names and len(names) > 600

        response = door_client.post(
            DOOR,
            json=_call("create_work_order", {"part_id": test_part.id, "quantity_ordered": 2}),
            headers=_auth(operator_headers),
        )
        assert response.status_code == 200, "a refused tool call is still a JSON-RPC result"
        result = response.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"] == {
            "status": 403,
            "detail": "Insufficient permissions",
            "method": "POST",
            "path": "/api/v1/work-orders/",
        }
        assert db_session.query(WorkOrder).count() == 0

    def test_manager_creates_a_draft_row_from_the_bearer_on_the_request(
        self, door_client, db_session, manager_headers, test_user, test_part
    ):
        response = door_client.post(
            DOOR,
            json=_call("create_work_order", {"part_id": test_part.id, "quantity_ordered": 2, "status": "released"}),
            headers=_auth(manager_headers),
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        created = result["structuredContent"]
        assert created["status"] == "draft"
        db_session.expire_all()
        row = db_session.query(WorkOrder).filter(WorkOrder.id == created["id"]).one()
        assert row.status == WorkOrderStatus.DRAFT
        assert row.created_by == test_user.id, "written as the bearer's user, from the HTTP request"

    def test_stateless_calls_need_no_initialize_and_carry_no_session(self, door_client, manager_headers):
        response = door_client.post(
            DOOR, json=_call("get_work_order", {"work_order_id": 424242}), headers=_auth(manager_headers)
        )
        assert response.status_code == 200
        assert "mcp-session-id" not in {key.lower() for key in response.headers}
        result = response.json()["result"]
        assert result["isError"] is True and result["structuredContent"]["status"] == 404
        assert result["structuredContent"]["detail"] == "Work order not found"

    def test_door_answers_503_until_the_lifespan_arms_it(self, door, manager_headers):
        unarmed = TestClient(app)  # no context manager: the lifespan never runs
        response = unarmed.post(DOOR, json=_rpc("tools/list"), headers=_auth(manager_headers))
        assert response.status_code == 503
        assert "lifespan" in response.json()["detail"]


class TestBodyBounds:
    def test_envelope_over_the_app_cap_but_under_the_door_cap_passes(self, door_client, manager_headers):
        body = _padded(_rpc("tools/list"), APP_CAP + 100_000)
        assert APP_CAP < len(body) < SMALL_UPLOAD_CAP
        headers = {**_auth(manager_headers), "Content-Type": "application/json"}
        response = door_client.post(DOOR, content=body, headers=headers)
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) > 600
        # The app cap itself is untouched: the same bytes to a JSON route are 413.
        api = door_client.post("/api/v1/work-orders/", content=body, headers={**manager_headers, **headers})
        assert api.status_code == 413

    def test_envelope_over_the_door_cap_is_413_from_the_door(self, door_client, manager_headers):
        body = _padded(_rpc("tools/list"), SMALL_UPLOAD_CAP + 50_000)
        headers = {**_auth(manager_headers), "Content-Type": "application/json"}
        response = door_client.post(DOOR, content=body, headers=headers)
        assert response.status_code == 413
        assert "too large" in response.text.lower()


class TestRateLimitExemption:
    def test_door_is_registered_exempt_from_the_default_limit(self, door):
        from slowapi.middleware import _find_route_handler, _should_exempt

        assert settings.RATE_LIMIT_ENABLED, "test assumes the limiter is configured (default)"
        scope = {"type": "http", "method": "POST", "path": DOOR, "root_path": "", "headers": []}
        handler = _find_route_handler(app.routes, scope)
        assert handler is door
        assert _should_exempt(app.state.limiter, handler)
        assert "app.mcp.http.mcp_door" in app.state.limiter._exempt_routes

    def test_outer_hit_is_waived_and_inner_hit_is_kept(self, door_client, manager_headers, test_work_order):
        """slowapi keys the default limit on ``LIMITER/<ip>/<path>/...`` in its storage.

        After a tool call through the door the storage must hold the INNER route's
        key (the real API request, keyed like the SPA's) and NO key for the door
        path itself -- one hit per call, not two.
        """
        storage = getattr(app.state.limiter, "_storage", None)
        counters = getattr(storage, "storage", None)
        assert isinstance(counters, dict), "memory storage expected under test"
        counters.clear()

        for _ in range(3):
            response = door_client.post(
                DOOR,
                json=_call("get_work_order", {"work_order_id": test_work_order.id}),
                headers=_auth(manager_headers),
            )
            assert response.status_code == 200 and response.json()["result"].get("isError") is not True

        keys = list(counters)
        inner = [key for key in keys if f"/api/v1/work-orders/{test_work_order.id}" in key]
        assert inner, keys
        assert counters[inner[0]] == 3, "the inner route is charged once per tool call"
        assert not [key for key in keys if f"/{DOOR.strip('/')}/" in key or key.endswith(DOOR)], keys


class TestDisabledByDefault:
    def test_without_the_flag_nothing_is_mounted(self, client, manager_headers):
        assert settings.WERCO_MCP_HTTP_ENABLED is False
        assert mount_mcp(app) is None
        assert not hasattr(app.state, "mcp_door")
        response = client.post(DOOR, json=_rpc("tools/list"), headers=_auth(manager_headers))
        assert response.status_code == 404
