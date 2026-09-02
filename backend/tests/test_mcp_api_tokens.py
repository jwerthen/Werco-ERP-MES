"""API tokens at the MCP boundary: the Streamable HTTP door and the stdio bridge.

- The door (``ErpTokenVerifier``) accepts a LIVE API token -- ``tools/list`` and a
  read dispatch as the token's user, and the dispatched route touches the row --
  and refuses a revoked, row-expired or disabled-user token with one clean 401 +
  ``WWW-Authenticate`` BEFORE any JSON-RPC is parsed.
- The fence holds THROUGH the door: ``create_api_token`` with an API token is the
  route's 403 detail verbatim and writes no row; with an Admin's interactive JWT
  the same tool mints a row (201 semantics: a ``token`` in ``structuredContent``,
  an ``api_tokens`` row, audit rows), ``list_api_tokens`` hides secrets, and
  ``revoke_api_token`` shuts the door on the holder.
- The bridge's ``TokenSource`` treats ``WERCO_ERP_TOKEN=<api token>`` as a static
  credential: no refresh, no login, ever -- even after a 401.

The plaintext token is never printed; comparisons that would echo it on failure
are bound to a bare boolean first.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.mcp.auth import (
    ENV_TOKEN,
    LOGIN_PATH,
    REFRESH_PATH,
    AuthContext,
    ErpTokenVerifier,
    TokenSource,
    api_token_claims,
    live_api_token_principal,
    token_is_acceptable,
)
from app.mcp.executor import RemoteExecutor
from app.mcp.http import McpDoor, mount_mcp, unmount_mcp
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.api_token_service import AUTH_EVENT_ISSUED, AUTH_EVENT_REVOKED

pytestmark = pytest.mark.api

DOOR = "/mcp"
API_TOKENS_URL = "/api/v1/api-tokens/"
FENCE_DETAIL = "API token cannot access this resource"
ACCEPT = {"Accept": "application/json, text/event-stream"}


def _rpc(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _door_auth(token: str) -> Dict[str, str]:
    return {**ACCEPT, "Authorization": f"Bearer {token}"}


def _auth(headers: Dict[str, str]) -> Dict[str, str]:
    return {**ACCEPT, "Authorization": headers["Authorization"]}


def _issue(client: TestClient, admin_headers: dict, user: User, **extra) -> tuple[int, str]:
    response = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": f"bot for {user.email}", **extra}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["token"]


def _revoke(client: TestClient, admin_headers: dict, token_id: int) -> None:
    response = client.post(f"{API_TOKENS_URL}{token_id}/revoke", json={"reason": "test revoke"}, headers=admin_headers)
    assert response.status_code == 200, response.text


@pytest.fixture
def door(client, monkeypatch) -> Iterator[McpDoor]:
    """The door mounted on the shared app for one test (the test_mcp_http_door recipe)."""
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


# --------------------------------------------------------------------------- the door


class TestDoor:
    def test_live_api_token_lists_tools_and_dispatches_as_its_user(
        self, door_client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(door_client, admin_headers, test_user)
        headers = _door_auth(token)

        listing = door_client.post(DOOR, json=_rpc("tools/list"), headers=headers)
        assert listing.status_code == 200, listing.status_code
        names = {tool["name"] for tool in listing.json()["result"]["tools"]}
        assert {"create_api_token", "list_api_tokens", "revoke_api_token"} <= names
        assert "list_work_centers" in names

        read = door_client.post(DOOR, json=_call("list_work_centers", {}), headers=headers)
        assert read.status_code == 200, read.status_code
        result = read.json()["result"]
        assert result.get("isError") is not True, result

        # The dispatched route ran get_current_user on the API token: the row was touched.
        row = db_session.get(ApiToken, token_id)
        db_session.refresh(row)
        assert row.last_used_at is not None

    def test_revoked_token_is_401_at_the_door_before_json_rpc_is_parsed(
        self, door_client: TestClient, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(door_client, admin_headers, test_user)
        headers = _door_auth(token)
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=headers).status_code == 200

        _revoke(door_client, admin_headers, token_id)
        refused = door_client.post(DOOR, json=_rpc("tools/list"), headers=headers)
        assert refused.status_code == 401
        assert "www-authenticate" in refused.headers
        # Not even a parseable envelope is required: the refusal is at the door, not in JSON-RPC.
        garbage = door_client.post(DOOR, content=b"{not json", headers={**headers, "Content-Type": "application/json"})
        assert garbage.status_code == 401

    def test_row_expired_and_disabled_user_are_401_at_the_door(
        self, door_client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(door_client, admin_headers, test_user, expires_days=3)
        headers = _door_auth(token)
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=headers).status_code == 200

        row = db_session.get(ApiToken, token_id)
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db_session.commit()
        assert api_token_claims(token) is not None, "the JWT itself is still signature-valid for three days"
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=headers).status_code == 401, "the ROW expired"

        row.expires_at = None
        db_session.commit()
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=headers).status_code == 200
        test_user.is_active = False
        db_session.commit()
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=headers).status_code == 401, "disabled user"

    def test_create_api_token_with_an_api_token_is_the_fence_403_and_writes_nothing(
        self, door_client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        # Even an ADMIN-user API token is fenced: minting is an interactive act.
        _id, token = _issue(door_client, admin_headers, admin_user)
        before = db_session.query(ApiToken).count()

        mint = door_client.post(
            DOOR,
            json=_call("create_api_token", {"user_id": test_user.id, "label": "should not exist"}),
            headers=_door_auth(token),
        )
        assert mint.status_code == 200
        result = mint.json()["result"]
        assert result.get("isError") is True
        assert FENCE_DETAIL in json.dumps(result["content"])
        assert "403" in json.dumps(result["content"])
        assert db_session.query(ApiToken).count() == before
        assert db_session.query(ApiToken).filter(ApiToken.label == "should not exist").count() == 0

        for tool, arguments in (
            ("list_api_tokens", {}),
            ("revoke_api_token", {"token_id": _id, "reason": "via my own token"}),
        ):
            response = door_client.post(DOOR, json=_call(tool, arguments), headers=_door_auth(token))
            assert response.status_code == 200
            result = response.json()["result"]
            assert result.get("isError") is True, tool
            assert FENCE_DETAIL in json.dumps(result["content"]), tool
        row = db_session.get(ApiToken, _id)
        db_session.refresh(row)
        assert row.revoked is False

    def test_admin_jwt_mints_lists_and_revokes_through_the_door(
        self, door_client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
    ):
        mint = door_client.post(
            DOOR,
            json=_call("create_api_token", {"user_id": test_user.id, "label": "Minted via MCP"}),
            headers=_auth(admin_headers),
        )
        assert mint.status_code == 200
        result = mint.json()["result"]
        assert result.get("isError") is not True, result
        created = result["structuredContent"]
        token = created["token"]
        assert isinstance(token, str) and token.count(".") == 2
        assert created["user_id"] == test_user.id and created["revoked"] is False and created["expires_at"] is None
        assert created["created_by"] == admin_user.id

        row = db_session.get(ApiToken, created["id"])
        assert row is not None and row.label == "Minted via MCP" and row.company_id == 1
        jwt_not_stored = token not in row.jti and token != row.jti
        assert jwt_not_stored
        audit_actions = {r.action for r in db_session.query(AuditLog).all()}
        assert AUTH_EVENT_ISSUED in audit_actions
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "api_token").count() == 1

        # The minted token opens the door itself.
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=_door_auth(token)).status_code == 200

        listing = door_client.post(DOOR, json=_call("list_api_tokens", {}), headers=_auth(admin_headers))
        entries = listing.json()["result"]["structuredContent"]["api_tokens"]
        assert [e["id"] for e in entries] == [created["id"]]
        assert "token" not in entries[0] and "jti" not in entries[0]
        assert token not in json.dumps(listing.json())

        revoke = door_client.post(
            DOOR,
            json=_call("revoke_api_token", {"token_id": created["id"], "reason": "rotated via MCP"}),
            headers=_auth(admin_headers),
        )
        assert revoke.status_code == 200
        revoked = revoke.json()["result"]
        assert revoked.get("isError") is not True, revoked
        assert revoked["structuredContent"]["revoked"] is True
        assert revoked["structuredContent"]["revoke_reason"] == "rotated via MCP"
        db_session.refresh(row)
        assert row.revoked is True and row.revoked_by == admin_user.id
        assert AUTH_EVENT_REVOKED in {r.action for r in db_session.query(AuditLog).all()}
        assert door_client.post(DOOR, json=_rpc("tools/list"), headers=_door_auth(token)).status_code == 401

    def test_manager_jwt_cannot_mint_through_the_door(
        self, door_client: TestClient, db_session: Session, manager_headers: dict, test_user: User
    ):
        response = door_client.post(
            DOOR,
            json=_call("create_api_token", {"user_id": test_user.id, "label": "nope"}),
            headers=_auth(manager_headers),
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is True
        assert "Insufficient permissions" in json.dumps(result["content"])
        assert db_session.query(ApiToken).count() == 0


# --------------------------------------------------------------------------- the verifier


class TestVerifier:
    async def test_verifier_shares_the_row_check(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(client, admin_headers, test_user, expires_days=5)
        row = db_session.get(ApiToken, token_id)
        assert token_is_acceptable(token) is True

        principal = await ErpTokenVerifier().verify_token(token)
        assert isinstance(principal, AccessToken)
        assert principal.client_id == str(test_user.id)
        assert principal.scopes == []
        assert principal.expires_at == calendar.timegm(row.expires_at.utctimetuple()), "the ROW's expiry, as epoch"
        assert live_api_token_principal(token) == (test_user.id, principal.expires_at)

        db_session.refresh(row)
        assert row.last_used_at is None, "the door check is a pure read; only a dispatched route touches"

        # The shared check refuses a holder promoted to a platform principal -- at the door too.
        test_user.is_superuser = True
        db_session.commit()
        assert await ErpTokenVerifier().verify_token(token) is None, "a platform principal never rides an API token"
        assert live_api_token_principal(token) is None
        test_user.is_superuser = False
        db_session.commit()
        assert await ErpTokenVerifier().verify_token(token) is not None

        _revoke(client, admin_headers, token_id)
        assert token_is_acceptable(token) is True, "signature-only, by design; the row check is what refuses"
        assert await ErpTokenVerifier().verify_token(token) is None
        assert live_api_token_principal(token) is None

    async def test_never_expiring_token_has_no_expiry_at_the_door(
        self, client: TestClient, admin_headers: dict, test_user: User
    ):
        _id, token = _issue(client, admin_headers, test_user)
        principal = await ErpTokenVerifier().verify_token(token)
        assert principal is not None and principal.expires_at is None

    async def test_signature_valid_but_unknown_jti_is_refused(self, client: TestClient, db_session: Session):
        from app.core.security import create_api_token

        orphan = create_api_token(jti="never-issued", user_id=1, company_id=1)
        assert api_token_claims(orphan) is not None
        assert token_is_acceptable(orphan) is True
        assert await ErpTokenVerifier().verify_token(orphan) is None
        assert live_api_token_principal(orphan) is None


# --------------------------------------------------------------------------- the stdio bridge


class Scripted:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.answers: Dict[tuple, List[httpx.Response]] = {}

    def on(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.answers.setdefault((method, path), []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.answers.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(599, json={"detail": f"unscripted {request.method} {request.url.path}"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sent(self) -> List[tuple]:
        return [(r.method, r.url.path) for r in self.requests]


def _executor(script: Scripted) -> RemoteExecutor:
    return RemoteExecutor("https://erp.example.test", version="9.9", transport=httpx.MockTransport(script))


@pytest.mark.unit
class TestBridgeStaticToken:
    async def test_static_api_token_is_sent_as_is_and_never_refreshed(self):
        from app.core.security import create_api_token

        token = create_api_token(jti="bridge-jti", user_id=7, company_id=1)
        script = Scripted()
        script.on("GET", "/api/v1/parts/", httpx.Response(200, json=[]))
        executor = _executor(script)
        source = TokenSource.from_env(executor, environ={ENV_TOKEN: token})
        assert source is not None
        assert source.describe() == "access-token"
        assert source.can_refresh is False and source.can_login is False

        same = (await source.get_token()) == token
        assert same, "the static token is handed back without any exchange"
        assert script.requests == []

        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token=token, token_source=source)
        )
        assert result.status == 200
        assert script.sent() == [("GET", "/api/v1/parts/")]
        sent_as_is = script.requests[0].headers.get("authorization") == f"Bearer {token}"
        assert sent_as_is
        await executor.aclose()

    async def test_a_401_with_a_static_api_token_is_surfaced_with_no_refresh_or_login(self):
        from app.core.security import create_api_token

        token = create_api_token(jti="bridge-jti-revoked", user_id=7, company_id=1)
        script = Scripted()
        script.on("GET", "/api/v1/parts/", httpx.Response(401, json={"detail": "Could not validate credentials"}))
        executor = _executor(script)
        source = TokenSource(executor, access_token=token)

        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token=token, token_source=source)
        )
        assert result.status == 401
        assert result.json()["detail"] == "Could not validate credentials"
        assert script.sent() == [("GET", "/api/v1/parts/")], "no POST /auth/refresh, no POST /auth/login"
        assert all(path not in (REFRESH_PATH, LOGIN_PATH) for _m, path in script.sent())
        assert await source.refresh_after_401(token) is None
        assert script.sent() == [("GET", "/api/v1/parts/")]
        await executor.aclose()
