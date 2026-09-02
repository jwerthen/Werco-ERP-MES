"""Smoke test for long-lived per-user API tokens: the whole lifecycle at the wire.

An Admin issues a token for a MANAGER user; the token then acts as that Manager
on a normal route, is fenced (403) from ``/auth`` and ``/api-tokens``, carries
the Manager's role (403 on an Admin-only route while an Admin-user token is
200 there), touches ``last_used_at`` once and not again inside five minutes,
and answers 401 on the very next request after an Admin revokes it. The audit
rows the lifecycle writes never contain the plaintext token. The dedicated
suites (``test_api_tokens.py`` / ``test_api_token_auth.py`` /
``test_mcp_api_tokens.py``) cover the individual refusals in depth; this file
is the one end-to-end proof.

The token is never printed: assertions compare it against stored text and
never echo it into a failure message.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.user import User

pytestmark = pytest.mark.api

API_TOKENS_URL = "/api/v1/api-tokens/"
FENCE_DETAIL = "API token cannot access this resource"
ADMIN_ONLY_URL = "/api/v1/admin/settings/materials"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _audit_text(row: AuditLog) -> str:
    return json.dumps(
        [row.description, row.resource_identifier, row.new_values, row.old_values, row.extra_data, row.error_message],
        default=str,
    )


def test_api_token_lifecycle_smoke(
    client: TestClient, db_session: Session, admin_headers: dict, admin_user: User, test_user: User
):
    assert test_user.role.value == "manager"

    # 1. An Admin issues a never-expiring token for the Manager: 201, shown once.
    issued = client.post(
        API_TOKENS_URL, json={"user_id": test_user.id, "label": "Werco Assistant"}, headers=admin_headers
    )
    assert issued.status_code == 201, issued.status_code
    body = issued.json()
    token = body["token"]
    assert token.count(".") == 2, "a real three-segment JWT"
    assert body["user_id"] == test_user.id
    assert body["expires_at"] is None, "the owner's default: never expires"
    assert body["revoked"] is False and body["last_used_at"] is None
    assert body["created_by"] == admin_user.id
    assert len(body["jti_prefix"]) == 8

    row = db_session.get(ApiToken, body["id"])
    assert row is not None and row.company_id == 1 and row.jti.startswith(body["jti_prefix"])
    assert row.jti != token and token not in row.jti, "only the jti is stored, never the JWT"

    # Issuance wrote an api_token audit row and an API_TOKEN_ISSUED auth event -- metadata only.
    rows = db_session.query(AuditLog).all()
    assert any(r.resource_type == "api_token" and r.resource_id == body["id"] for r in rows)
    assert any(r.action == "API_TOKEN_ISSUED" and r.resource_id == test_user.id for r in rows)
    assert all(token not in _audit_text(r) for r in rows), "the plaintext token must never reach the audit chain"

    # 2. The token acts as the Manager on a normal route, and last_used_at is touched ONCE.
    api = _bearer(token)
    assert client.get("/api/v1/parts/", headers=api).status_code == 200
    db_session.refresh(row)
    first_touch = row.last_used_at
    assert first_touch is not None
    assert client.get("/api/v1/parts/", headers=api).status_code == 200
    db_session.refresh(row)
    assert row.last_used_at == first_touch, "a second call inside five minutes does not rewrite the marker"

    # 3. The fence: every /auth and /api-tokens path is 403 with the exact detail, before any body validation.
    for method, path in (
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/display-token"),
        ("GET", API_TOKENS_URL),
        ("POST", API_TOKENS_URL),
        ("POST", f"{API_TOKENS_URL}{body['id']}/revoke"),
    ):
        response = client.request(method, path, headers=api, json={} if method == "POST" else None)
        assert response.status_code == 403, (method, path, response.status_code)
        assert response.json()["detail"] == FENCE_DETAIL, (method, path)
    # POST /auth/refresh never reads a bearer (it is a PUBLIC route keyed on the body), so the
    # fence cannot fire there -- but an API token is not a refresh credential either: 401.
    refused = client.post("/api/v1/auth/refresh", json={"refresh_token": token}, headers=api)
    assert refused.status_code == 401
    # POST /auth/kiosk-badge-token reads the raw bearer as a STATION token (not through
    # get_current_user); an API token is not one, so the badge mint refuses it: 401.
    badge = client.post("/api/v1/auth/kiosk-badge-token", json={"employee_id": test_user.employee_id}, headers=api)
    assert badge.status_code == 401

    # 4. The token carries the user's ROLE: the Manager's token is refused on an Admin-only route by
    #    RBAC (not the fence), while an Admin-user token opens the same route.
    denied = client.get(ADMIN_ONLY_URL, headers=api)
    assert denied.status_code == 403 and denied.json()["detail"] == "Insufficient permissions"
    admin_issued = client.post(
        API_TOKENS_URL, json={"user_id": admin_user.id, "label": "Admin bot"}, headers=admin_headers
    )
    assert admin_issued.status_code == 201
    admin_api = _bearer(admin_issued.json()["token"])
    assert client.get(ADMIN_ONLY_URL, headers=admin_api).status_code == 200
    # ...and even the Admin-user token is fenced from minting tokens: that is an interactive act.
    assert client.get(API_TOKENS_URL, headers=admin_api).status_code == 403

    # 5. Revoke with a reason: 200, the record is one-way, and the holder is 401 on the very next call.
    revoked = client.post(
        f"{API_TOKENS_URL}{body['id']}/revoke", json={"reason": "Bot decommissioned"}, headers=admin_headers
    )
    assert revoked.status_code == 200, revoked.status_code
    assert revoked.json()["revoked"] is True
    assert revoked.json()["revoke_reason"] == "Bot decommissioned"
    assert revoked.json()["revoked_by"] == admin_user.id and revoked.json()["revoked_at"]
    assert "token" not in revoked.json()

    assert client.get("/api/v1/parts/", headers=api).status_code == 401
    again = client.post(f"{API_TOKENS_URL}{body['id']}/revoke", json={"reason": "second time"}, headers=admin_headers)
    assert again.status_code == 409
    db_session.refresh(row)
    assert row.revoke_reason == "Bot decommissioned", "the first revocation's reason is the record"
    assert any(r.action == "API_TOKEN_REVOKED" for r in db_session.query(AuditLog).all())
    assert all(token not in _audit_text(r) for r in db_session.query(AuditLog).all())

    # 6. The Admin's listing hides secrets and, by default, revoked rows.
    listing = client.get(API_TOKENS_URL, headers=admin_headers).json()["api_tokens"]
    assert [entry["id"] for entry in listing] == [admin_issued.json()["id"]]
    assert "token" not in listing[0] and "jti" not in listing[0] and listing[0]["jti_prefix"]
    with_revoked = client.get(API_TOKENS_URL, params={"include_revoked": "true"}, headers=admin_headers).json()
    assert {entry["id"] for entry in with_revoked["api_tokens"]} == {body["id"], admin_issued.json()["id"]}

    # The Admin's own interactive token still works on /api-tokens (the fence is about credential KIND).
    assert client.get(API_TOKENS_URL, headers=admin_headers).status_code == 200


# --------------------------------------------------------------------------- the MCP door


@pytest.fixture
def door(client, monkeypatch):
    """The Streamable HTTP door mounted on the shared app for one test (the test_mcp_http_door recipe)."""
    from app.core.config import settings
    from app.main import app
    from app.mcp.http import mount_mcp, unmount_mcp

    assert settings.WERCO_MCP_HTTP_ENABLED is False, "the door must be off by default"
    monkeypatch.setattr(settings, "WERCO_MCP_HTTP_ENABLED", True)
    mounted = mount_mcp(app)
    assert mounted is not None
    try:
        yield mounted
    finally:
        unmount_mcp(app)


@pytest.fixture
def door_client(door):
    from app.main import app

    with TestClient(app) as armed:
        assert door.running
        yield armed


def _rpc(method: str, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_api_token_opens_the_mcp_door_until_revoked(
    door_client: TestClient, db_session: Session, admin_headers: dict, test_user: User
):
    """The door runs the SAME row check as the routes: a live API token is in, a revoked one is 401 at the door."""
    issued = door_client.post(
        API_TOKENS_URL, json={"user_id": test_user.id, "label": "Werco Assistant"}, headers=admin_headers
    )
    assert issued.status_code == 201
    token = issued.json()["token"]
    accept = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}

    listing = door_client.post("/mcp", json=_rpc("tools/list"), headers=accept)
    assert listing.status_code == 200, listing.status_code
    names = {tool["name"] for tool in listing.json()["result"]["tools"]}
    assert {"create_api_token", "list_api_tokens", "revoke_api_token"} <= names

    # A read dispatches as the Manager (the inner route ran get_current_user on the API token).
    read = door_client.post(
        "/mcp", json=_rpc("tools/call", {"name": "list_work_centers", "arguments": {}}), headers=accept
    )
    assert read.status_code == 200, read.status_code
    assert read.json()["result"].get("isError") is not True, read.json()["result"]

    # The fence holds THROUGH the door: minting a token with an API token is the route's 403, verbatim.
    mint = door_client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "create_api_token", "arguments": {"user_id": test_user.id, "label": "x"}}),
        headers=accept,
    )
    assert mint.status_code == 200
    result = mint.json()["result"]
    assert result.get("isError") is True
    assert FENCE_DETAIL in json.dumps(result["content"])

    # Revoke as the Admin: the next door call is refused BEFORE any JSON-RPC is parsed.
    revoked = door_client.post(
        f"{API_TOKENS_URL}{issued.json()['id']}/revoke", json={"reason": "Bot decommissioned"}, headers=admin_headers
    )
    assert revoked.status_code == 200
    refused = door_client.post("/mcp", json=_rpc("tools/list"), headers=accept)
    assert refused.status_code == 401
    assert "www-authenticate" in refused.headers
