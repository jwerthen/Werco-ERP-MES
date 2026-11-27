"""Read-only API tokens at the MCP boundary (Claude-plugin brief P0.1 / P0.2).

The API layer refuses a read-only token on every write and every denied tag
(``tests/test_api_token_read_only.py``); the MCP server additionally never SHOWS such a
caller what it would be refused, and refuses a write or denied tool called by name BEFORE
argument validation or dispatch -- with the brief's exact texts. Pinned here:

- **The door.** ``tools/list`` for a read-only token is exactly ``read_only_view``: no tool
  that writes, no tool under a tag in ``WERCO_MCP_READ_ONLY_DENY_TAGS``, and the eight
  read convenience tools. A full token's listing is the whole catalog, unchanged.
- **Refusal by name.** A write tool -> 403 ``"Read-only API token: this tool writes."``,
  with nothing written (the shop-floor verbs, create/start work order); a denied tool ->
  403 ``"Read-only API token: this tool is not available to read-only tokens."``; both
  answer 403 (never 404: every tool stays in the registry) even with invalid arguments.
  A read tool still dispatches.
- **The verifier** carries the ROW's scope in ``AccessToken.claims`` (``scopes`` stays
  ``[]``, ``live_api_token_principal`` keeps its tuple shape).
- **stdio.** A static read-only ``WERCO_ERP_TOKEN`` narrows the listing from the token's
  own claims and refuses before dispatch; a full or refresh-only bridge lists everything
  and never logs in just to list.
- **Convenience tools are classified by what they dispatch**: each is driven through a
  recording executor; a read tool sends only GET, a write tool at least one non-GET, and
  nothing goes to a route its declaration does not name.
- ``python -m app.mcp --print-catalog --read-only`` prints the read-only catalog;
  ``--read-only`` alone is a usage error.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.credentials import READ_ONLY_TOOL_DENIED_DETAIL, READ_ONLY_TOOL_WRITES_DETAIL
from app.main import app
from app.mcp import __main__ as cli
from app.mcp.auth import (
    CLAIM_API_TOKEN_AI_EXTERNAL,
    CLAIM_API_TOKEN_ID,
    CLAIM_API_TOKEN_SCOPE,
    FULL_SCOPE,
    AuthContext,
    CallerScope,
    ErpTokenVerifier,
    TokenSource,
    api_token_scope_hint,
    live_api_token_grant,
    live_api_token_principal,
)
from app.mcp.catalog import build_catalog
from app.mcp.convenience import (
    CONVENIENCE_TOOL_NAMES,
    CONVENIENCE_TOOL_ROUTES,
    CONVENIENCE_TOOLS,
    SHADOWED_OPERATIONS,
    ToolContext,
    convenience_tool_reads,
    convenience_tool_tag,
)
from app.mcp.http import McpDoor, mount_mcp, unmount_mcp
from app.mcp.results import ExecResult
from app.mcp.server import build_registry, build_server, caller_scope, read_only_refusal, read_only_view
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center
from tests.test_mcp_cli import BACKEND_ROOT, clean_env
from tests.test_mcp_server import connected

pytestmark = pytest.mark.api

DOOR = "/mcp"
API_TOKENS_URL = "/api/v1/api-tokens/"
ACCEPT = {"Accept": "application/json, text/event-stream"}
READ_CONVENIENCE = {
    "search",
    "list_work_centers",
    "get_work_order",
    "get_shop_floor_dashboard",
    "list_inventory",
    "list_purchase_orders",
    "list_quality_ncrs",
    "list_parts",
}
WRITE_CONVENIENCE = set(CONVENIENCE_TOOL_NAMES) - READ_CONVENIENCE


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


@pytest.fixture(scope="module")
def deny_tags():
    return settings.read_only_deny_tags


def _rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _door_auth(token: str) -> Dict[str, str]:
    return {**ACCEPT, "Authorization": f"Bearer {token}"}


def _issue(client: TestClient, admin_headers: dict, user: User, **extra) -> Tuple[int, str]:
    response = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": f"bot for {user.email}", **extra}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["token"]


@pytest.fixture
def door(client, monkeypatch) -> Iterator[McpDoor]:
    assert settings.WERCO_MCP_HTTP_ENABLED is False, "the door must be off by default"
    monkeypatch.setattr(settings, "WERCO_MCP_HTTP_ENABLED", True)
    mounted = mount_mcp(app)
    assert mounted is not None
    try:
        yield mounted
    finally:
        unmount_mcp(app)


@pytest.fixture
def door_client(door) -> Iterator[TestClient]:
    with TestClient(app) as armed:
        assert door.running
        yield armed


def _listed_names(response) -> List[str]:
    assert response.status_code == 200, response.status_code
    return [tool["name"] for tool in response.json()["result"]["tools"]]


def _tool_result(response) -> Dict[str, Any]:
    assert response.status_code == 200, response.status_code
    return response.json()["result"]


def _first_get_tool_per_tag(catalog, tags) -> Dict[str, str]:
    picked: Dict[str, str] = {}
    for tool in catalog:
        if tool.method != "GET":
            continue
        for tag in tool.tags:
            if tag in tags and tag not in picked:
                picked[tag] = tool.name
    return picked


# --------------------------------------------------------------------------- the door


class TestDoorListing:
    def test_a_read_only_token_is_shown_only_reads_outside_the_denied_tags(
        self, door_client: TestClient, admin_headers: dict, test_user: User, catalog, deny_tags
    ):
        # Not AI-external: an AI-external token's narrower listing is tests/test_mcp_ai_external.py's.
        _id, token = _issue(door_client, admin_headers, test_user, scope="read_only", ai_external=False)
        names = _listed_names(door_client.post(DOOR, json=_rpc("tools/list"), headers=_door_auth(token)))

        generated, convenience = read_only_view(catalog, CONVENIENCE_TOOLS, deny_tags=deny_tags)
        assert names == [tool.name for tool in convenience] + [tool.name for tool in generated]
        assert {tool.name for tool in convenience} == READ_CONVENIENCE
        by_name = {tool.name: tool for tool in catalog}
        for name in names:
            if name in CONVENIENCE_TOOL_NAMES:
                continue
            tool = by_name[name]
            assert tool.method == "GET", f"{name} writes"
            assert not set(tool.tags) & deny_tags, f"{name} is under a denied tag"
        # Concretely: no shop-floor verb, no create, no audit/user/document tool.
        for absent in ("shop_floor_start_operation", "shop_floor_complete_operation", "create_work_order"):
            assert absent not in names
        for tag in deny_tags:
            assert not any(tag in by_name[name].tags for name in names if name in by_name)
        assert len(names) > 250, "a read-only token still reads broadly"

    def test_a_full_tokens_listing_is_unchanged(
        self, door_client: TestClient, admin_headers: dict, test_user: User, catalog
    ):
        _id, token = _issue(door_client, admin_headers, test_user)
        names = _listed_names(door_client.post(DOOR, json=_rpc("tools/list"), headers=_door_auth(token)))
        assert names == [tool.name for tool in CONVENIENCE_TOOLS] + sorted(tool.name for tool in catalog)

    def test_a_persons_listing_is_unchanged(self, door_client: TestClient, auth_headers: dict, catalog):
        names = _listed_names(
            door_client.post(
                DOOR, json=_rpc("tools/list"), headers={**ACCEPT, **{"Authorization": auth_headers["Authorization"]}}
            )
        )
        assert len(names) == len(CONVENIENCE_TOOLS) + len(catalog)


class TestDoorRefusals:
    @pytest.fixture
    def read_only(self, door_client: TestClient, admin_headers: dict, db_session: Session) -> Dict[str, Any]:
        """An OPERATOR -- a role that may start and complete -- holding a read-only token."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        _id, token = _issue(door_client, admin_headers, operator, scope="read_only")
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        return {"headers": _door_auth(token), "wo": wo, "op": op}

    @pytest.mark.parametrize(
        "tool,arguments",
        [
            ("shop_floor_start_operation", lambda ctx: {"operation_id": ctx["op"].id}),
            ("shop_floor_complete_operation", lambda ctx: {"operation_id": ctx["op"].id}),
            ("work_orders_start_work_order", lambda ctx: {"work_order_id": ctx["wo"].id}),
            ("release_work_order", lambda ctx: {"work_order_id": ctx["wo"].id}),
            ("create_work_order", lambda ctx: {"part_id": 1, "quantity_ordered": 1}),
            ("update_work_order", lambda ctx: {"work_order_id": ctx["wo"].id, "version": 1, "priority": 1}),
        ],
    )
    def test_a_write_tool_is_403_before_dispatch_and_writes_nothing(
        self, door_client: TestClient, db_session: Session, read_only, tool: str, arguments
    ):
        work_orders_before = db_session.query(WorkOrder).count()
        result = _tool_result(
            door_client.post(DOOR, json=_call(tool, arguments(read_only)), headers=read_only["headers"])
        )
        assert result["isError"] is True
        assert result["structuredContent"]["status"] == 403
        assert result["structuredContent"]["detail"] == READ_ONLY_TOOL_WRITES_DETAIL

        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == work_orders_before
        op = db_session.get(WorkOrderOperation, read_only["op"].id)
        assert op.status == OperationStatus.READY and op.started_by is None
        assert db_session.get(WorkOrder, read_only["wo"].id).status == WorkOrderStatus.RELEASED

    def test_the_refusal_precedes_argument_validation(self, door_client: TestClient, read_only):
        """403, not 422: a write tool with garbage arguments is still refused as a write."""
        result = _tool_result(
            door_client.post(DOOR, json=_call("create_work_order", {"nonsense": True}), headers=read_only["headers"])
        )
        assert result["structuredContent"]["status"] == 403
        assert result["structuredContent"]["detail"] == READ_ONLY_TOOL_WRITES_DETAIL

    def test_a_tool_under_every_denied_tag_is_403_by_name(self, door_client: TestClient, read_only, catalog, deny_tags):
        picked = _first_get_tool_per_tag(catalog, deny_tags)
        assert set(picked) == set(deny_tags), f"no GET tool for {set(deny_tags) - set(picked)}"
        for tag, name in sorted(picked.items()):
            result = _tool_result(door_client.post(DOOR, json=_call(name, {}), headers=read_only["headers"]))
            assert result["isError"] is True, (tag, name)
            assert result["structuredContent"]["status"] == 403, (tag, name, result)
            assert result["structuredContent"]["detail"] == READ_ONLY_TOOL_DENIED_DETAIL, (tag, name)

    def test_a_write_under_a_denied_tag_is_told_it_writes(self, door_client: TestClient, read_only, catalog, deny_tags):
        writer = next(tool for tool in catalog if tool.method != "GET" and set(tool.tags) & deny_tags)
        result = _tool_result(door_client.post(DOOR, json=_call(writer.name, {}), headers=read_only["headers"]))
        assert result["structuredContent"]["detail"] == READ_ONLY_TOOL_WRITES_DETAIL

    def test_reads_still_dispatch(self, door_client: TestClient, read_only):
        for name, arguments in (
            ("list_work_centers", {}),
            ("get_work_order", {"work_order_id": read_only["wo"].id}),
            ("work_orders_list_work_orders", {}),
        ):
            result = _tool_result(door_client.post(DOOR, json=_call(name, arguments), headers=read_only["headers"]))
            assert result.get("isError") is not True, (name, result)

    def test_an_unknown_tool_is_still_404(self, door_client: TestClient, read_only):
        result = _tool_result(door_client.post(DOOR, json=_call("no_such_tool", {}), headers=read_only["headers"]))
        assert result["structuredContent"]["status"] == 404

    def test_a_full_token_still_writes_through_the_door(
        self, door_client: TestClient, admin_headers: dict, db_session: Session
    ):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        _id, token = _issue(door_client, admin_headers, operator)
        wc = make_work_center(db_session)
        _wo, op = make_wo_with_operation(db_session, work_center=wc)
        result = _tool_result(
            door_client.post(
                DOOR, json=_call("shop_floor_start_operation", {"operation_id": op.id}), headers=_door_auth(token)
            )
        )
        assert result.get("isError") is not True, result
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS


# --------------------------------------------------------------------------- the verifier


class TestVerifierCarriesTheRow:
    async def test_a_read_only_tokens_access_token_carries_the_row_scope(
        self, client: TestClient, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(client, admin_headers, test_user, scope="read_only")
        principal = await ErpTokenVerifier().verify_token(token)
        assert isinstance(principal, AccessToken)
        assert principal.scopes == []
        assert principal.claims[CLAIM_API_TOKEN_SCOPE] == "read_only"
        assert principal.claims[CLAIM_API_TOKEN_AI_EXTERNAL] is True
        # AI-external: the row's company and identity ride along for the door's egress guard
        # (tests/test_mcp_ai_external.py pins the full set).
        assert principal.claims[CLAIM_API_TOKEN_ID] == token_id
        grant = live_api_token_grant(token)
        assert (grant.user_id, grant.scope, grant.ai_external) == (test_user.id, "read_only", True)
        assert live_api_token_principal(token) == (test_user.id, None), "the tuple shape is unchanged"

    async def test_a_full_tokens_access_token_says_full(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, token = _issue(client, admin_headers, test_user)
        principal = await ErpTokenVerifier().verify_token(token)
        assert principal.claims == {CLAIM_API_TOKEN_SCOPE: "full", CLAIM_API_TOKEN_AI_EXTERNAL: False}


class TestCallerScope:
    def _http_ctx(self, bearer: str, claims: Optional[Dict[str, Any]] = None, user: bool = True):
        principal = SimpleNamespace(access_token=SimpleNamespace(claims=claims)) if user else None
        request = SimpleNamespace(headers={"authorization": f"Bearer {bearer}"}, scope={"user": principal})
        return SimpleNamespace(request=request)

    def test_the_row_claims_decide_on_the_door(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, token = _issue(client, admin_headers, test_user)
        ctx = self._http_ctx(token, {CLAIM_API_TOKEN_SCOPE: "read_only", CLAIM_API_TOKEN_AI_EXTERNAL: False})
        assert caller_scope(ctx, None) == CallerScope(read_only=True, ai_external=False)

    def test_an_api_jwt_with_no_row_record_fails_closed(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, token = _issue(client, admin_headers, test_user)
        assert caller_scope(self._http_ctx(token, None), None) == CallerScope(read_only=True, ai_external=True)

    def test_the_remote_passthrough_narrows_on_the_tokens_own_claims(
        self, client: TestClient, admin_headers: dict, test_user: User
    ):
        _id, full = _issue(client, admin_headers, test_user)
        _id, narrowed = _issue(client, admin_headers, test_user, scope="read_only")
        assert caller_scope(self._http_ctx(full, None), None, verify_bearer=False) == FULL_SCOPE
        assert caller_scope(self._http_ctx(narrowed, None), None, verify_bearer=False).read_only is True

    def test_a_persons_access_token_is_full(self, auth_headers: dict):
        bearer = auth_headers["Authorization"].split(" ", 1)[1]
        assert caller_scope(self._http_ctx(bearer, None), None) == FULL_SCOPE

    def test_a_malformed_row_record_fails_closed(self):
        assert caller_scope(self._http_ctx("x", {CLAIM_API_TOKEN_SCOPE: "weird"}), None) == CallerScope(True, True)

    def test_the_hint_only_reads_api_jwts(self, auth_headers: dict):
        assert api_token_scope_hint(auth_headers["Authorization"].split(" ", 1)[1]) == FULL_SCOPE
        assert api_token_scope_hint("not-a-jwt") == FULL_SCOPE


class TestReadOnlyRefusalRule:
    def test_writes_come_first(self):
        assert read_only_refusal(reads=False, tags={"Audit"}, deny_tags={"Audit"}) == READ_ONLY_TOOL_WRITES_DETAIL
        assert read_only_refusal(reads=False, tags={"Parts"}, deny_tags={"Audit"}) == READ_ONLY_TOOL_WRITES_DETAIL

    def test_a_read_under_a_denied_tag(self):
        assert (
            read_only_refusal(reads=True, tags={"Parts", "Audit"}, deny_tags={"Audit"}) == READ_ONLY_TOOL_DENIED_DETAIL
        )

    def test_a_read_elsewhere_is_allowed(self):
        assert read_only_refusal(reads=True, tags={"Parts"}, deny_tags={"Audit"}) is None

    def test_the_exact_brief_texts(self):
        assert READ_ONLY_TOOL_WRITES_DETAIL == "Read-only API token: this tool writes."
        assert READ_ONLY_TOOL_DENIED_DETAIL == "Read-only API token: this tool is not available to read-only tokens."


# --------------------------------------------------------------------------- stdio


class _NeverExecutor:
    """Any dispatch is a test failure: listing and refusal must not reach the API."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    async def request(self, **kwargs: Any) -> ExecResult:
        self.calls.append((kwargs["method"], kwargs["path"]))
        return ExecResult(status=500, content=b'{"detail": "should not be reached"}', content_type="application/json")

    async def aclose(self) -> None:
        return None


class TestStdio:
    async def test_a_static_read_only_token_narrows_the_listing_and_refuses_before_dispatch(
        self, client: TestClient, admin_headers: dict, test_user: User, catalog, deny_tags
    ):
        _id, token = _issue(client, admin_headers, test_user, scope="read_only", ai_external=False)
        executor = _NeverExecutor()
        server = build_server(executor, catalog=catalog, token_source=TokenSource(executor, access_token=token))
        async with connected(server) as session:
            listing = await session.list_tools()
            refused = await session.call_tool("create_work_order", {"part_id": 1, "quantity_ordered": 1})
            denied = await session.call_tool(_first_get_tool_per_tag(catalog, {"Audit"})["Audit"], {})
        generated, convenience = read_only_view(catalog, CONVENIENCE_TOOLS, deny_tags=deny_tags)
        assert [tool.name for tool in listing.tools] == [t.name for t in convenience] + [t.name for t in generated]
        assert refused.is_error and refused.structured_content["detail"] == READ_ONLY_TOOL_WRITES_DETAIL
        assert denied.is_error and denied.structured_content["detail"] == READ_ONLY_TOOL_DENIED_DETAIL
        assert executor.calls == [], "nothing was dispatched"

    async def test_every_write_and_every_denied_tool_is_refused_by_name_before_dispatch(
        self, client: TestClient, admin_headers: dict, test_user: User, catalog, deny_tags
    ):
        """Brief P0.1 / P0.2 acceptance, exhaustively: EVERY non-GET generated tool and every
        writing convenience tool answers 403 "this tool writes", and EVERY GET tool under a denied
        tag answers 403 "not available" -- each called by name with empty arguments (refusal
        precedes validation) and none dispatched. The expected sets come from the catalog's HTTP
        methods and tags and the hand-kept convenience split, NOT from the server's own
        classification, so a write the registry mislabels as a read fails here."""
        _id, token = _issue(client, admin_headers, test_user, scope="read_only", ai_external=False)
        writes = sorted({tool.name for tool in catalog if tool.method != "GET"} | WRITE_CONVENIENCE)
        denied = sorted(tool.name for tool in catalog if tool.method == "GET" and set(tool.tags) & deny_tags)
        assert len(writes) > 300 and len(denied) >= len(deny_tags)
        assert "shop_floor_start_operation" in writes and "shop_floor_complete_operation" in writes

        executor = _NeverExecutor()
        server = build_server(executor, catalog=catalog, token_source=TokenSource(executor, access_token=token))
        wrong: List[Tuple[str, Any]] = []
        async with connected(server) as session:
            for names, detail in ((writes, READ_ONLY_TOOL_WRITES_DETAIL), (denied, READ_ONLY_TOOL_DENIED_DETAIL)):
                for name in names:
                    result = await session.call_tool(name, {})
                    content = result.structured_content or {}
                    if not (result.is_error and content.get("status") == 403 and content.get("detail") == detail):
                        wrong.append((name, content))
        assert wrong == [], f"{len(wrong)} tool(s) not refused as expected: {wrong[:10]}"
        assert executor.calls == [], "a refused tool reached the API"

    async def test_a_full_static_token_lists_everything(
        self, client: TestClient, admin_headers: dict, test_user: User, catalog
    ):
        _id, token = _issue(client, admin_headers, test_user)
        executor = _NeverExecutor()
        server = build_server(executor, catalog=catalog, token_source=TokenSource(executor, access_token=token))
        async with connected(server) as session:
            listing = await session.list_tools()
        assert len(listing.tools) == len(CONVENIENCE_TOOLS) + len(catalog)

    async def test_a_refresh_only_bridge_lists_everything_without_logging_in(self, catalog):
        executor = _NeverExecutor()
        source = TokenSource(executor, refresh_token="refresh-sentinel")
        assert source.static_token is None
        server = build_server(executor, catalog=catalog, token_source=source)
        async with connected(server) as session:
            listing = await session.list_tools()
        assert len(listing.tools) == len(CONVENIENCE_TOOLS) + len(catalog)
        assert executor.calls == [], "listing never exchanges a credential"


# --------------------------------------------------------------------------- convenience classification


def _template_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$")


class _ScriptedExecutor:
    """Answers every request with a plausible canned body; records (method, path)."""

    PARENT_ID = 7  # a production parent whose laser child is 99
    DRAFT_LASER_ID = 8
    CHILD_ID = 99

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def _work_order(self, wo_id: int) -> Dict[str, Any]:
        ready_op = {"id": 11, "sequence": 10, "name": "Laser", "status": "ready", "version": 1, "laser_nest": None}
        if wo_id == self.PARENT_ID:
            return {
                "id": wo_id,
                "work_order_number": "WO-7",
                "status": "released",
                "version": 1,
                "work_order_type": "standard",
                "part_id": 3,
                "operations": [ready_op],
            }
        status = "draft" if wo_id == self.DRAFT_LASER_ID else "released"
        return {
            "id": wo_id,
            "work_order_number": f"WO-{wo_id}",
            "status": status,
            "version": 1,
            "work_order_type": "laser_cutting",
            "operations": [ready_op],
        }

    def _answer(self, method: str, path: str) -> Any:
        tail = path[len("/api/v1") :]
        if method == "GET":
            if tail == "/work-centers/":
                return [{"id": 1, "name": "Laser", "code": "LZ", "work_center_type": "laser", "is_active": True}]
            if tail == "/work-orders/":
                return [
                    {"id": self.CHILD_ID, "parent_work_order_id": self.PARENT_ID, "work_order_type": "laser_cutting"}
                ]
            match = re.fullmatch(r"/work-orders/(\d+)", tail)
            if match:
                return self._work_order(int(match.group(1)))
            if tail.startswith("/work-orders/by-number/"):
                return self._work_order(5)
            if tail.startswith("/parts/"):
                return {"id": 3, "part_number": "P-3"}
            return []
        if method == "POST" and tail.endswith("/laser-nest-packages/import") or tail.endswith("/standalone/import"):
            return {"child_work_order": self._work_order(self.CHILD_ID)}
        if method == "PUT" and re.fullmatch(r"/work-orders/\d+", tail):
            body = self._work_order(int(tail.rsplit("/", 1)[1]))
            return {**body, "status": "draft", "version": 2}
        if method == "PUT" and tail.startswith("/work-orders/operations/"):
            return {"id": 11, "status": "pending", "version": 2}
        return {"id": 1, "status": "draft", "version": 1, "work_order_number": "WO-1"}

    async def request(self, **kwargs: Any) -> ExecResult:
        method, path = kwargs["method"], kwargs["path"]
        self.calls.append((method, path))
        return ExecResult(
            status=200, content=json.dumps(self._answer(method, path)).encode(), content_type="application/json"
        )

    async def aclose(self) -> None:
        return None


# Argument sets that walk each tool down its dispatching branches.
_SCENARIOS: Dict[str, List[Dict[str, Any]]] = {
    "search": [{"q": "bracket"}],
    "list_work_centers": [{}, {"name": "laser"}],
    "create_work_order": [{"part_id": 1, "quantity_ordered": 2}],
    # A DRAFT work order: add_operation refuses any other (a released job's routing is a person's change).
    "add_operation": [{"work_order_id": _ScriptedExecutor.DRAFT_LASER_ID, "name": "Weld", "work_center": "Laser"}],
    "get_work_order": [{"work_order_id": 5}, {"work_order_number": "WO-5"}],
    "update_work_order": [{"work_order_id": 5, "version": 1, "priority": 2}],
    "duplicate_work_order": [{"work_order_id": 5}],
    "release_work_order": [{"work_order_id": 5}],
    "import_laser_nest_package": [
        {"source_path": "/srv/nests/pkg.zip"},
        {"work_order_id": _ScriptedExecutor.PARENT_ID, "source_path": "/srv/nests/pkg.zip"},
        {"source_path": "/srv/nests/pkg.zip", "release": True},
    ],
    "add_laser_nest": [
        {"work_order_id": _ScriptedExecutor.DRAFT_LASER_ID, "cnc_number": "1001", "planned_runs": 1},
        {"work_order_id": _ScriptedExecutor.PARENT_ID, "cnc_number": "1002", "planned_runs": 1},
    ],
    "get_shop_floor_dashboard": [{}, {"include_dispatch_board": True}],
    "list_inventory": [{}, {"summary": True}],
    "list_purchase_orders": [{}],
    "list_quality_ncrs": [{}],
    "list_parts": [{}],
    "mark_shipped": [{"shipment_id": 7}, {"shipment_id": 7, "tracking_number": "1Z999"}],
}


class TestConvenienceClassification:
    def test_every_tool_has_a_route_declaration_and_a_scenario(self):
        assert set(CONVENIENCE_TOOL_ROUTES) == set(CONVENIENCE_TOOL_NAMES)
        assert set(_SCENARIOS) == set(CONVENIENCE_TOOL_NAMES)
        assert {tool.name for tool in CONVENIENCE_TOOLS if convenience_tool_reads(tool)} == READ_CONVENIENCE

    def test_every_declared_route_is_a_real_operation(self):
        paths = app.openapi()["paths"]
        for name, routes in CONVENIENCE_TOOL_ROUTES.items():
            for method, path in routes:
                assert method.lower() in paths.get(path, {}), f"{name}: {method} {path} is not an API operation"

    def test_a_read_tools_routes_all_carry_the_tag_it_claims(self):
        """The server takes a read tool's tags from its claimed [Tag] for shadowed routes: sound only if true."""
        paths = app.openapi()["paths"]
        for tool in CONVENIENCE_TOOLS:
            if not convenience_tool_reads(tool):
                continue
            claimed = convenience_tool_tag(tool)
            for method, path in CONVENIENCE_TOOL_ROUTES[tool.name]:
                assert set(paths[path][method.lower()]["tags"]) == {claimed}, (tool.name, method, path)

    def test_the_annotation_agrees_with_what_the_tool_dispatches(self):
        for tool in CONVENIENCE_TOOLS:
            assert tool.hints.read_only is convenience_tool_reads(tool), tool.name

    @pytest.mark.parametrize("tool", CONVENIENCE_TOOLS, ids=lambda tool: tool.name)
    async def test_dispatches_only_declared_routes_and_reads_send_only_get(self, tool):
        declared = [(method, _template_regex(path)) for method, path in CONVENIENCE_TOOL_ROUTES[tool.name]]
        sent: List[Tuple[str, str]] = []
        for arguments in _SCENARIOS[tool.name]:
            executor = _ScriptedExecutor()
            context = ToolContext(
                executor,
                AuthContext(token="t"),
                max_result_chars=200_000,
                max_blob_bytes=1_000_000,
                max_upload_bytes=1_000_000,
            )
            result = await tool.handler(context, dict(arguments))
            assert result.is_error is not True, (tool.name, arguments, result)
            sent.extend(executor.calls)

        assert sent, f"{tool.name} dispatched nothing"
        undeclared = [
            (method, path) for method, path in sent if not any(m == method and rx.match(path) for m, rx in declared)
        ]
        assert undeclared == [], f"{tool.name} dispatched routes its declaration does not name: {undeclared}"
        methods = {method for method, _path in sent}
        if tool.name in READ_CONVENIENCE:
            assert methods == {"GET"}, f"{tool.name} is a read but sent {methods}"
        else:
            assert methods - {"GET"}, f"{tool.name} is a write but sent only GETs"

    def test_the_registry_classifies_entries_the_same_way(self, catalog, deny_tags):
        registry = build_registry(catalog, CONVENIENCE_TOOLS)
        for name in CONVENIENCE_TOOL_NAMES:
            assert registry[name].reads is (name in READ_CONVENIENCE), name
        for tool in catalog:
            assert registry[tool.name].reads is (tool.method == "GET")
            assert registry[tool.name].tags == frozenset(tool.tags)


# --------------------------------------------------------------------------- the CLI


class TestCli:
    pytestmark = pytest.mark.unit

    def test_read_only_is_parsed(self):
        args = cli._parse_args(["--print-catalog", "--read-only"])
        assert args.print_catalog is True and args.read_only is True
        assert cli._parse_args([]).read_only is False

    def test_read_only_alone_is_a_usage_error(self):
        with pytest.raises(SystemExit) as raised:
            cli._parse_args(["--read-only"])
        assert raised.value.code == 2


@pytest.fixture(scope="module")
def read_only_catalog_run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.mcp", "--print-catalog", "--read-only"],
        cwd=BACKEND_ROOT,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestPrintReadOnlyCatalog:
    pytestmark = pytest.mark.integration

    def test_prints_the_read_only_catalog(self, read_only_catalog_run: subprocess.CompletedProcess, catalog, deny_tags):
        assert read_only_catalog_run.returncode == 0, read_only_catalog_run.stderr[-3000:]
        payload = json.loads(read_only_catalog_run.stdout)
        assert set(payload) == {
            "server",
            "version",
            "convenience_tools",
            "generated_tools",
            "shadowed_operations",
            "counts",
            "scope",
            "deny_tags",
        }
        assert payload["scope"] == "read_only"
        assert payload["deny_tags"] == sorted(deny_tags)
        generated, convenience = read_only_view(catalog, CONVENIENCE_TOOLS, deny_tags=deny_tags)
        assert [row["name"] for row in payload["generated_tools"]] == [tool.name for tool in generated]
        assert {row["name"] for row in payload["convenience_tools"]} == READ_CONVENIENCE
        assert {row["method"] for row in payload["generated_tools"]} == {"GET"}
        assert not any(row["tag"] in deny_tags for row in payload["generated_tools"])
        assert payload["counts"] == {
            "convenience": len(convenience),
            "generated": len(generated),
            "shadowed": len(SHADOWED_OPERATIONS),
        }

    def test_read_only_without_print_catalog_exits_2(self):
        result = subprocess.run(
            [sys.executable, "-m", "app.mcp", "--read-only"],
            cwd=BACKEND_ROOT,
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 2
        assert "--read-only is only valid together with --print-catalog" in result.stderr
