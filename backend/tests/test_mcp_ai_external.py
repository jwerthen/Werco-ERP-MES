"""AI-external API tokens at the MCP boundary (Claude-plugin brief P0.3).

- **The door refuses outright while AI egress is off.** HTTP 403 ``{"detail": ...}`` before
  the MCP transport, so ``initialize`` and ``tools/list`` are refused, not only tool calls --
  and the refusal is ledgered naming the token. A person, and a read-only token that is not
  AI-external, are unaffected.
- **The listing** for an AI-external token is exactly ``ai_external_view``: tools whose every
  route is on ``AI_EXTERNAL_ALLOWED_ROUTES`` (convenience read tools only when every route
  they dispatch is). Any other tool called by name is 403 before dispatch -- the API layer's
  own text -- while a write or a denied tag keeps the read-only texts.
- **What comes back is redacted**: a tool dispatched in-process passes the API's
  export-control middleware, so an ITAR work order reaches the agent with its canary gone.
- **The verifier** carries the row's company and identity, and the company's egress, in
  ``AccessToken.claims`` for an AI-external token; stdio narrows a static AI-external token.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.credentials import READ_ONLY_TOOL_DENIED_DETAIL, READ_ONLY_TOOL_WRITES_DETAIL
from app.core.export_control import (
    AI_EGRESS_DISABLED_DETAIL,
    AI_EXTERNAL_ROUTE_DENIED_DETAIL,
    REDACTED_VALUE,
    ai_external_operations,
)
from app.main import app
from app.mcp.auth import (
    CLAIM_API_TOKEN_AI_EGRESS,
    CLAIM_API_TOKEN_AI_EXTERNAL,
    CLAIM_API_TOKEN_COMPANY_ID,
    CLAIM_API_TOKEN_ID,
    CLAIM_API_TOKEN_JTI_PREFIX,
    CLAIM_API_TOKEN_LABEL,
    CLAIM_API_TOKEN_SCOPE,
    ErpTokenVerifier,
    TokenSource,
)
from app.mcp.catalog import build_catalog
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOL_ROUTES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS
from app.mcp.http import McpDoor, mount_mcp, unmount_mcp
from app.mcp.results import ExecResult
from app.mcp.server import ai_external_refusal, ai_external_view, build_registry, build_server
from app.models.api_token import ApiToken
from app.models.governance import ControlledAccessEvent
from app.models.user import User
from app.services import llm_client
from tests.api.export_control_helpers import contains_canary, issue_token, make_viewer, seed_world
from tests.test_mcp_server import connected

pytestmark = pytest.mark.api

DOOR = "/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream"}
AI_CONVENIENCE = {"list_work_centers", "get_work_order", "list_purchase_orders", "list_quality_ncrs", "list_parts"}


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


@pytest.fixture(scope="module")
def deny_tags():
    return settings.read_only_deny_tags


@pytest.fixture
def door(client, monkeypatch) -> Iterator[McpDoor]:
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


@pytest.fixture
def viewer(db_session: Session) -> User:
    return make_viewer(db_session)


def _rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


INITIALIZE = _rpc(
    "initialize",
    {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude", "version": "1"}},
)


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _auth(token: str) -> Dict[str, str]:
    return {**ACCEPT, "Authorization": f"Bearer {token}"}


def _names(response) -> List[str]:
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return [tool["name"] for tool in response.json()["result"]["tools"]]


def _result(response) -> Dict[str, Any]:
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return response.json()["result"]


def _egress(monkeypatch, allowed: bool) -> None:
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: allowed)


# --------------------------------------------------------------------------- the door's egress guard


class TestDoorEgressGuard:
    @pytest.mark.parametrize(
        "body", [INITIALIZE, _rpc("tools/list"), _call("list_parts", {})], ids=["initialize", "list", "call"]
    )
    def test_egress_off_is_403_at_the_door(
        self, door_client: TestClient, db_session: Session, admin_headers: dict, viewer: User, monkeypatch, body
    ):
        token_id, token = issue_token(door_client, admin_headers, viewer, scope="read_only")
        _egress(monkeypatch, False)
        response = door_client.post(DOOR, json=body, headers=_auth(token))
        assert response.status_code == 403
        assert response.json() == {"detail": AI_EGRESS_DISABLED_DETAIL}

        db_session.expire_all()
        rows = db_session.query(ControlledAccessEvent).all()
        assert len(rows) == 1
        row = rows[0]
        record = db_session.get(ApiToken, token_id)
        assert (row.action, row.denial_reason, row.record_id, row.allowed) == (
            "ai_external_refused",
            "ai_egress_disabled",
            None,
            False,
        )
        assert row.user_id == viewer.id and row.company_id == viewer.company_id
        assert row.extra_data["credential"] == {
            "kind": "api_token",
            "api_token_id": token_id,
            "jti_prefix": record.jti_prefix,
            "label": record.label,
        }
        assert row.extra_data["route"] == DOOR

    def test_others_are_unaffected_by_the_switch(
        self, door_client: TestClient, admin_headers: dict, viewer: User, auth_headers: dict, monkeypatch
    ):
        _id, read_only = issue_token(door_client, admin_headers, viewer, scope="read_only", ai_external=False)
        _egress(monkeypatch, False)
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=_auth(read_only)).status_code == 200
        person = {**ACCEPT, "Authorization": auth_headers["Authorization"]}
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=person).status_code == 200

    def test_egress_on_lets_the_handshake_through(
        self, door_client: TestClient, admin_headers: dict, viewer: User, monkeypatch
    ):
        _id, token = issue_token(door_client, admin_headers, viewer, scope="read_only")
        _egress(monkeypatch, True)
        response = door_client.post(DOOR, json=INITIALIZE, headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "werco-erp"


# --------------------------------------------------------------------------- listing and refusals


class TestAiExternalListing:
    def test_the_listing_is_the_ai_external_view(
        self, door_client: TestClient, admin_headers: dict, viewer: User, catalog, deny_tags
    ):
        _id, token = issue_token(door_client, admin_headers, viewer, scope="read_only")
        names = _names(door_client.post(DOOR, json=_rpc("tools/list"), headers=_auth(token)))
        generated, convenience = ai_external_view(catalog, CONVENIENCE_TOOLS, deny_tags=deny_tags)
        assert names == [tool.name for tool in convenience] + [tool.name for tool in generated]
        assert {tool.name for tool in convenience} == AI_CONVENIENCE
        allowed = ai_external_operations(settings.API_V1_PREFIX)
        assert all(tool.key in allowed for tool in generated)
        assert {tool.key for tool in generated} | {
            route for name in AI_CONVENIENCE for route in CONVENIENCE_TOOL_ROUTES[name]
        } == set(allowed), "every allowlisted route is reachable by some listed tool"
        assert "search" not in names and "get_shop_floor_dashboard" not in names

    def test_registry_classification(self, catalog):
        allowed = ai_external_operations(settings.API_V1_PREFIX)
        registry = build_registry(catalog, CONVENIENCE_TOOLS)
        for tool in catalog:
            assert registry[tool.name].ai_external_allowed is (tool.key in allowed), tool.name
        for name in CONVENIENCE_TOOL_NAMES:
            assert registry[name].ai_external_allowed is (name in AI_CONVENIENCE), name

    def test_the_refusal_rule(self):
        assert ai_external_refusal(reads=False, tags={"Parts"}, deny_tags=set(), allowed=True) == (
            READ_ONLY_TOOL_WRITES_DETAIL
        )
        assert ai_external_refusal(reads=True, tags={"Audit"}, deny_tags={"Audit"}, allowed=True) == (
            READ_ONLY_TOOL_DENIED_DETAIL
        )
        assert ai_external_refusal(reads=True, tags={"Parts"}, deny_tags=set(), allowed=False) == (
            AI_EXTERNAL_ROUTE_DENIED_DETAIL
        )
        assert ai_external_refusal(reads=True, tags={"Parts"}, deny_tags=set(), allowed=True) is None


class TestAiExternalCalls:
    @pytest.fixture
    def token(self, door_client: TestClient, admin_headers: dict, viewer: User) -> Tuple[int, str]:
        return issue_token(door_client, admin_headers, viewer, scope="read_only")

    @pytest.mark.parametrize(
        "tool,detail",
        [
            ("search", AI_EXTERNAL_ROUTE_DENIED_DETAIL),
            ("get_shop_floor_dashboard", AI_EXTERNAL_ROUTE_DENIED_DETAIL),
            (("GET", "/api/v1/customers/"), AI_EXTERNAL_ROUTE_DENIED_DETAIL),
            ("create_work_order", READ_ONLY_TOOL_WRITES_DETAIL),
            (("GET", "/api/v1/audit/"), READ_ONLY_TOOL_DENIED_DETAIL),
        ],
        ids=["search", "dashboard", "customers", "create_work_order", "audit"],
    )
    def test_a_tool_off_the_allowlist_is_403_by_name(
        self, door_client: TestClient, db_session: Session, token, catalog, tool, detail
    ):
        if isinstance(tool, tuple):  # a generated tool: its name is derived, so find it by operation
            tool = next(entry.name for entry in catalog if entry.key == tool)
        _id, bearer = token
        result = _result(door_client.post(DOOR, json=_call(tool, {"q": "x"}), headers=_auth(bearer)))
        assert result["isError"] is True
        assert result["structuredContent"]["status"] == 403
        assert result["structuredContent"]["detail"] == detail
        db_session.expire_all()
        assert db_session.query(ControlledAccessEvent).count() == 0, "refused before dispatch: nothing reached the API"

    def test_what_a_read_tool_returns_is_redacted(
        self, door_client: TestClient, db_session: Session, token, auth_headers: dict
    ):
        world = seed_world(db_session)
        _id, bearer = token
        result = _result(
            door_client.post(
                DOOR, json=_call("get_work_order", {"work_order_id": world.itar_wo.id}), headers=_auth(bearer)
            )
        )
        assert result.get("isError") is not True, result
        text = json.dumps(result)
        assert not contains_canary(text.encode())
        assert REDACTED_VALUE in text
        assert world.itar_wo.work_order_number in text, "the business number stays"

        db_session.expire_all()
        redacted = db_session.query(ControlledAccessEvent).filter(
            ControlledAccessEvent.action == "ai_external_redacted"
        )
        assert (
            redacted.filter(
                ControlledAccessEvent.record_type == "work_order", ControlledAccessEvent.record_id == world.itar_wo.id
            ).count()
            == 1
        )

        # The same tool, as a person through the door: the full record.
        person = {**ACCEPT, "Authorization": auth_headers["Authorization"]}
        full = _result(
            door_client.post(DOOR, json=_call("get_work_order", {"work_order_id": world.itar_wo.id}), headers=person)
        )
        assert contains_canary(json.dumps(full).encode())


# --------------------------------------------------------------------------- verifier and stdio


class TestVerifier:
    async def test_an_ai_external_tokens_claims_carry_the_row_and_the_switch(
        self, client: TestClient, admin_headers: dict, viewer: User, db_session: Session, monkeypatch
    ):
        token_id, token = issue_token(client, admin_headers, viewer, scope="read_only")
        record = db_session.get(ApiToken, token_id)
        principal = await ErpTokenVerifier().verify_token(token)
        assert principal.scopes == []
        assert principal.claims == {
            CLAIM_API_TOKEN_SCOPE: "read_only",
            CLAIM_API_TOKEN_AI_EXTERNAL: True,
            CLAIM_API_TOKEN_COMPANY_ID: viewer.company_id,
            CLAIM_API_TOKEN_ID: token_id,
            CLAIM_API_TOKEN_JTI_PREFIX: record.jti_prefix,
            CLAIM_API_TOKEN_LABEL: record.label,
            CLAIM_API_TOKEN_AI_EGRESS: True,
        }
        _egress(monkeypatch, False)
        assert (await ErpTokenVerifier().verify_token(token)).claims[CLAIM_API_TOKEN_AI_EGRESS] is False

    async def test_a_token_that_is_not_ai_external_carries_no_identity(
        self, client: TestClient, admin_headers: dict, viewer: User
    ):
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only", ai_external=False)
        principal = await ErpTokenVerifier().verify_token(token)
        assert principal.claims == {CLAIM_API_TOKEN_SCOPE: "read_only", CLAIM_API_TOKEN_AI_EXTERNAL: False}


class _NeverExecutor:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    async def request(self, **kwargs: Any) -> ExecResult:
        self.calls.append((kwargs["method"], kwargs["path"]))
        return ExecResult(status=500, content=b'{"detail": "should not be reached"}', content_type="application/json")

    async def aclose(self) -> None:
        return None


class TestStdio:
    async def test_a_static_ai_external_token_narrows_the_listing_and_refuses_before_dispatch(
        self, client: TestClient, admin_headers: dict, viewer: User, catalog, deny_tags
    ):
        _id, token = issue_token(client, admin_headers, viewer, scope="read_only")
        executor = _NeverExecutor()
        server = build_server(executor, catalog=catalog, token_source=TokenSource(executor, access_token=token))
        async with connected(server) as session:
            listing = await session.list_tools()
            refused = await session.call_tool("search", {"q": "x"})
        generated, convenience = ai_external_view(catalog, CONVENIENCE_TOOLS, deny_tags=deny_tags)
        assert [tool.name for tool in listing.tools] == [t.name for t in convenience] + [t.name for t in generated]
        assert refused.is_error and refused.structured_content["detail"] == AI_EXTERNAL_ROUTE_DENIED_DETAIL
        assert executor.calls == []
