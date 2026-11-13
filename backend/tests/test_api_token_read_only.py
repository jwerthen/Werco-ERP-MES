"""Read-only API tokens at the API layer (Claude-plugin brief P0.1 / P0.2, migration 119).

A token minted ``scope = "read_only"`` must be unable to write -- whatever its user's
role -- and unable to read the tags in ``WERCO_MCP_READ_ONLY_DENY_TAGS``, when it is sent
STRAIGHT to ``/api/v1`` (the MCP door's refusals are covered in
``tests/test_mcp_read_only.py``). Pinned here:

- **Mint.** ``scope`` defaults to ``full``; ``ai_external`` defaults to true for a
  read-only token and false for a full one; AI-external + full is 422 and writes nothing;
  an unknown scope is 422. The row, the response, the JWT's narrowing claims, the
  ``log_create`` row and the ``API_TOKEN_ISSUED`` event all carry both. A FULL token's
  JWT claims are exactly what they always were.
- **The row is authoritative.** A token whose claims disagree with its row (either way)
  is refused 401 -- a claim can never stand in for the row.
- **Gate 2 (method).** EVERY non-GET route a user credential can reach answers 403
  ``"Read-only API token cannot modify data."`` -- enumerated from ``app.routes`` so a new
  write route is covered the day it ships -- and the two shop-floor operation verbs are
  pinned by name with nothing written.
- **Gate 3 (tag).** A GET under each default deny tag is 403
  ``"Read-only API token cannot access this resource."``; an ordinary GET still works;
  a request with no matched route fails CLOSED.
- **Full tokens and people are unchanged**, and a person never inherits a token's
  narrowing through the shared identity map.
- The deny list is validated at boot, and global search drops people for a read-only token.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import jwt
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api import deps
from app.core.config import settings
from app.core.credentials import (
    READ_ONLY_ROUTE_DENIED_DETAIL,
    READ_ONLY_ROUTE_WRITES_DETAIL,
    is_ai_external_credential,
    is_read_only_api_token,
)
from app.core.security import verify_api_token
from app.main import app
from app.mcp.catalog import catalog_tags
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.api_token_service import AI_EXTERNAL_REQUIRES_READ_ONLY_DETAIL, AUTH_EVENT_ISSUED
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center
from tests.test_mcp_cli import BACKEND_ROOT, clean_env

pytestmark = pytest.mark.api

API_TOKENS_URL = "/api/v1/api-tokens/"
FENCE_DETAIL = "API token cannot access this resource"
DEFAULT_DENY_TAGS = frozenset(
    {
        "Documents",
        "Document Deliveries",
        "Data Export",
        "Users",
        "Audit",
        "Admin Settings",
        "Carrier Integrations",
        "Platform Administration",
        "Company Management",
        "API Tokens",
        "Hank Intake",
    }
)
# The four principals that resolve a user bearer (an API token included) through
# get_current_user; a route reaching none of them never sees an API token at all.
USER_PRINCIPALS = (deps.get_current_user, deps.get_display_or_user, deps.get_signin_principal, deps.get_kiosk_or_user)
API_PREFIX = settings.API_V1_PREFIX.rstrip("/")


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _issue(client: TestClient, admin_headers: dict, user: User, **extra) -> Tuple[int, str, dict]:
    response = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": f"bot for {user.email}", **extra}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["token"], body


@pytest.fixture
def read_only_token(client: TestClient, admin_headers: dict, test_user: User) -> Tuple[int, str]:
    """(row id, token): a READ-ONLY token bound to the MANAGER ``test_user`` (a role that may write).

    Minted ``ai_external=False`` so these tests see the read-only gates alone; the AI-external
    narrowing (egress, route allowlist, redaction) is ``tests/api/test_ai_external_token.py``'s.
    """
    token_id, token, _body = _issue(client, admin_headers, test_user, scope="read_only", ai_external=False)
    return token_id, token


def _reset_limiter() -> None:
    """The enumerations send hundreds of requests in one test; reset the limiter before each."""
    limiter = getattr(app.state, "limiter", None)
    if limiter is not None:
        limiter.reset()


def _dependency_calls(route: APIRoute) -> List[Any]:
    calls: List[Any] = []
    stack = [route.dependant]
    while stack:
        current = stack.pop()
        if current.call is not None:
            calls.append(current.call)
        stack.extend(current.dependencies)
    return calls


def _user_bearer_routes() -> List[Tuple[str, str, APIRoute]]:
    """(METHOD, path template, route) for every /api/v1 APIRoute a user bearer can authenticate on."""
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path_format.startswith(API_PREFIX):
            continue
        calls = _dependency_calls(route)
        if not any(call is principal for call in calls for principal in USER_PRINCIPALS):
            continue
        for method in sorted(route.methods):
            out.append((method, route.path_format, route))
    return out


def _concrete(path_template: str) -> str:
    """Fill every ``{param}`` (``{param:path}`` included) with ``1``."""
    out, depth, i = [], 0, 0
    while i < len(path_template):
        char = path_template[i]
        if char == "{":
            depth += 1
            if depth == 1:
                out.append("1")
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
        i += 1
    return "".join(out)


def _is_fenced(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in deps.API_TOKEN_DENIED_PREFIXES)


def _request_for(method: str = "GET", route: Any = None, path: str = "/api/v1/parts/") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    if route is not None:
        scope["route"] = route
    return Request(scope)


# --------------------------------------------------------------------------- mint


class TestMint:
    def test_default_is_full_and_the_jwt_is_unchanged(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, token, body = _issue(client, admin_headers, test_user)
        assert body["scope"] == "full"
        assert body["ai_external"] is False
        claims = verify_api_token(token)
        assert set(claims) == {"jti", "user_id", "company_id"}, "a full token's claims are exactly what they were"
        raw = jwt.decode(token, options={"verify_signature": False})
        assert "api_scope" not in raw and "ai_ext" not in raw

    def test_read_only_defaults_to_ai_external(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, token, body = _issue(client, admin_headers, test_user, scope="read_only")
        assert (body["scope"], body["ai_external"]) == ("read_only", True)
        row = db_session.get(ApiToken, token_id)
        assert (row.scope, row.ai_external) == ("read_only", True)
        claims = verify_api_token(token)
        assert claims["api_scope"] == "read_only" and claims["ai_ext"] is True

    def test_read_only_may_opt_out_of_ai_external(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, token, body = _issue(client, admin_headers, test_user, scope="read_only", ai_external=False)
        assert (body["scope"], body["ai_external"]) == ("read_only", False)
        assert db_session.get(ApiToken, token_id).ai_external is False
        claims = verify_api_token(token)
        assert claims["api_scope"] == "read_only" and "ai_ext" not in claims

    def test_full_with_explicit_false_is_accepted(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, _token, body = _issue(client, admin_headers, test_user, scope="full", ai_external=False)
        assert (body["scope"], body["ai_external"]) == ("full", False)

    def test_ai_external_full_token_is_422_and_writes_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        before = db_session.query(ApiToken).count()
        response = client.post(
            API_TOKENS_URL,
            json={"user_id": test_user.id, "label": "nope", "scope": "full", "ai_external": True},
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == AI_EXTERNAL_REQUIRES_READ_ONLY_DETAIL
        assert db_session.query(ApiToken).count() == before
        # Omitting scope means full: the same refusal.
        response = client.post(
            API_TOKENS_URL, json={"user_id": test_user.id, "label": "nope", "ai_external": True}, headers=admin_headers
        )
        assert response.status_code == 422
        assert db_session.query(ApiToken).count() == before

    @pytest.mark.parametrize("scope", ["admin", "READ_ONLY", "", "write"])
    def test_unknown_scope_is_422(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User, scope: str
    ):
        before = db_session.query(ApiToken).count()
        response = client.post(
            API_TOKENS_URL, json={"user_id": test_user.id, "label": "nope", "scope": scope}, headers=admin_headers
        )
        assert response.status_code == 422
        assert db_session.query(ApiToken).count() == before

    def test_mint_is_audited_with_the_scope(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        token_id, _token, _body = _issue(client, admin_headers, test_user, scope="read_only")
        create = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "api_token", AuditLog.resource_id == token_id, AuditLog.action == "CREATE"
            )
            .one()
        )
        assert create.new_values["scope"] == "read_only"
        assert create.new_values["ai_external"] is True
        assert "read-only, AI-external" in create.description
        issued = (
            db_session.query(AuditLog)
            .filter(AuditLog.action == AUTH_EVENT_ISSUED, AuditLog.resource_id == test_user.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert issued.extra_data["api_token_id"] == token_id
        assert issued.extra_data["scope"] == "read_only"
        assert issued.extra_data["ai_external"] is True

    def test_list_response_carries_scope_and_ai_external(
        self, client: TestClient, admin_headers: dict, test_user: User
    ):
        token_id, _token, _body = _issue(client, admin_headers, test_user, scope="read_only")
        listed = client.get(API_TOKENS_URL, headers=admin_headers).json()["api_tokens"]
        row = next(item for item in listed if item["id"] == token_id)
        assert (row["scope"], row["ai_external"]) == ("read_only", True)
        assert "token" not in row


class TestRowIsAuthoritative:
    @pytest.mark.parametrize(
        "minted,tampered",
        [
            ({"scope": "read_only"}, {"scope": "full"}),
            ({}, {"scope": "read_only"}),
            ({"scope": "read_only"}, {"ai_external": False}),
            ({"scope": "read_only", "ai_external": False}, {"ai_external": True}),
        ],
    )
    def test_claims_that_disagree_with_the_row_are_refused(
        self,
        client: TestClient,
        db_session: Session,
        admin_headers: dict,
        test_user: User,
        minted: dict,
        tampered: dict,
    ):
        token_id, token, _body = _issue(client, admin_headers, test_user, **minted)
        assert client.get("/api/v1/parts/", headers=_bearer(token)).status_code == 200
        row = db_session.get(ApiToken, token_id)
        for key, value in tampered.items():
            setattr(row, key, value)
        db_session.commit()
        response = client.get("/api/v1/parts/", headers=_bearer(token))
        assert response.status_code == 401, "a claim can never stand in for the row"


# --------------------------------------------------------------------------- gate 2: method


class TestMethodGate:
    def test_every_non_get_route_refuses_a_read_only_token(self, client: TestClient, read_only_token):
        """Enumerated from app.routes: a write route added tomorrow is covered the day it ships."""
        _token_id, token = read_only_token
        headers = _bearer(token)
        writes = [(method, path) for method, path, _route in _user_bearer_routes() if method not in ("GET", "HEAD")]
        assert len(writes) > 300, "the enumeration found the API's write routes"
        assert ("PUT", f"{API_PREFIX}/shop-floor/operations/{{operation_id}}/start") in writes
        assert ("POST", f"{API_PREFIX}/shop-floor/operations/{{operation_id}}/complete") in writes

        wrong = []
        for method, template in writes:
            _reset_limiter()
            path = _concrete(template)
            response = client.request(method, path, headers=headers)
            expected = FENCE_DETAIL if _is_fenced(path) else READ_ONLY_ROUTE_WRITES_DETAIL
            detail = (
                response.json().get("detail")
                if response.headers.get("content-type", "").startswith("application/json")
                else None
            )
            if response.status_code != 403 or detail != expected:
                wrong.append((method, template, response.status_code, detail))
        assert wrong == [], f"{len(wrong)} write route(s) did not refuse a read-only token: {wrong[:15]}"

    @pytest.mark.parametrize("verb", ["start", "complete"])
    def test_the_two_shop_floor_operation_verbs_refuse_and_write_nothing(
        self, client: TestClient, db_session: Session, admin_headers: dict, verb: str
    ):
        """Brief P0.1 acceptance, by name: even an OPERATOR's read-only token cannot start or
        complete -- the operation is untouched, no labor row, no audit row."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        _id, token, _body = _issue(client, admin_headers, operator, scope="read_only")
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        audit_before = db_session.query(AuditLog).count()

        if verb == "start":
            response = client.put(f"/api/v1/shop-floor/operations/{op.id}/start", headers=_bearer(token))
        else:
            response = client.post(
                f"/api/v1/shop-floor/operations/{op.id}/complete", json={"source": "kiosk"}, headers=_bearer(token)
            )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL

        db_session.expire_all()
        fresh = db_session.get(WorkOrderOperation, op.id)
        assert fresh.status == OperationStatus.READY
        assert fresh.actual_start is None and fresh.started_by is None and fresh.completed_by is None
        assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
        assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).count() == 0
        # The only rows written are the mint's (none by the refused request).
        assert db_session.query(AuditLog).count() == audit_before

    def test_a_write_body_is_never_validated_first(self, client: TestClient, db_session: Session, read_only_token):
        """403, not 422: the gate runs before the route reads a byte of the body."""
        _id, token = read_only_token
        before = db_session.query(Part).count()
        response = client.post("/api/v1/parts/", json={"bogus": True}, headers=_bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        assert db_session.query(Part).count() == before


# --------------------------------------------------------------------------- gate 3: tag


class TestTagGate:
    def test_ordinary_reads_still_work(self, client: TestClient, read_only_token):
        _id, token = read_only_token
        for path in ("/api/v1/parts/", "/api/v1/work-orders/", "/api/v1/work-centers/"):
            response = client.get(path, headers=_bearer(token))
            assert response.status_code == 200, (path, response.text)

    def test_a_get_under_every_default_deny_tag_is_refused(self, client: TestClient, read_only_token):
        _id, token = read_only_token
        sampled = {}
        for method, template, route in _user_bearer_routes():
            if method != "GET":
                continue
            for tag in route.tags:
                if tag in DEFAULT_DENY_TAGS and tag not in sampled:
                    sampled[tag] = template
        assert set(sampled) == DEFAULT_DENY_TAGS, f"no GET route found for {DEFAULT_DENY_TAGS - set(sampled)}"

        for tag, template in sorted(sampled.items()):
            _reset_limiter()
            path = _concrete(template)
            response = client.get(path, headers=_bearer(token))
            assert response.status_code == 403, (tag, path, response.text)
            expected = FENCE_DETAIL if _is_fenced(path) else READ_ONLY_ROUTE_DENIED_DETAIL
            assert response.json()["detail"] == expected, (tag, path)

    def test_users_me_is_denied_to_a_read_only_token(self, client: TestClient, read_only_token):
        """Denying the Users tag removes /users/me too -- a documented consequence."""
        _id, token = read_only_token
        response = client.get("/api/v1/users/me", headers=_bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == READ_ONLY_ROUTE_DENIED_DETAIL

    def test_full_token_is_unchanged_on_the_same_routes(self, client: TestClient, admin_headers: dict, test_user: User):
        _id, token, _body = _issue(client, admin_headers, test_user)
        assert client.get("/api/v1/users/me", headers=_bearer(token)).status_code == 200
        created = client.post(
            "/api/v1/parts/",
            json={"part_number": "RO-FULL-1", "name": "Full token part", "part_type": "manufactured"},
            headers=_bearer(token),
        )
        assert created.status_code in (200, 201), created.text


class TestRefusalFunction:
    """``read_only_api_token_refusal`` directly: the gate's decisions without the HTTP stack."""

    def _route(self, tags):
        return SimpleNamespace(tags=list(tags))

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_non_safe_methods_are_writes(self, method: str):
        refusal = deps.read_only_api_token_refusal(_request_for(method, self._route(["Parts"])))
        assert isinstance(refusal, HTTPException)
        assert (refusal.status_code, refusal.detail) == (403, READ_ONLY_ROUTE_WRITES_DETAIL)

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods_pass_the_method_gate(self, method: str):
        assert deps.read_only_api_token_refusal(_request_for(method, self._route(["Parts"]))) is None

    def test_no_matched_route_fails_closed(self):
        refusal = deps.read_only_api_token_refusal(_request_for("GET", None))
        assert (refusal.status_code, refusal.detail) == (403, READ_ONLY_ROUTE_DENIED_DETAIL)

    def test_a_route_without_tags_metadata_fails_closed(self):
        refusal = deps.read_only_api_token_refusal(_request_for("GET", SimpleNamespace()))
        assert (refusal.status_code, refusal.detail) == (403, READ_ONLY_ROUTE_DENIED_DETAIL)

    def test_any_denied_tag_on_the_route_is_enough(self):
        refusal = deps.read_only_api_token_refusal(_request_for("GET", self._route(["Parts", "Audit"])))
        assert refusal.detail == READ_ONLY_ROUTE_DENIED_DETAIL

    def test_an_explicit_deny_set_overrides_the_setting(self):
        request = _request_for("GET", self._route(["Parts"]))
        assert deps.read_only_api_token_refusal(request, frozenset({"Parts"})).detail == READ_ONLY_ROUTE_DENIED_DETAIL
        assert deps.read_only_api_token_refusal(request, frozenset()) is None


# --------------------------------------------------------------------------- people and full tokens


class TestMarkers:
    def test_the_user_carries_the_rows_narrowing(self, client: TestClient, db_session: Session, read_only_token):
        token_id, token = read_only_token
        user = deps.get_current_user(
            request=_request_for("GET", SimpleNamespace(tags=["Parts"])), db=db_session, token=token
        )
        assert user._api_token_id == token_id
        assert (user._api_token_scope, user._api_token_ai_external) == ("read_only", False)
        assert is_read_only_api_token(user) and not is_ai_external_credential(user)

    def test_an_interactive_credential_clears_a_stale_narrowing(
        self, db_session: Session, test_user: User, auth_headers: dict
    ):
        """One ORM instance serves several requests (a shared identity map): a person must
        never inherit the read-only narrowing a token's request left on it."""
        test_user._api_token_id = 99
        test_user._api_token_scope = "read_only"
        test_user._api_token_ai_external = True
        access = auth_headers["Authorization"].split(" ", 1)[1]
        user = deps.get_current_user(request=_request_for("POST", None), db=db_session, token=access)
        assert user is test_user
        assert user._api_token_id is None
        assert user._api_token_scope is None and user._api_token_ai_external is False
        assert not is_read_only_api_token(user) and not is_ai_external_credential(user)

    def test_a_person_writes_after_their_read_only_token_was_refused(
        self, client: TestClient, read_only_token, auth_headers: dict
    ):
        _id, token = read_only_token
        refused = client.post(
            "/api/v1/parts/",
            json={"part_number": "RO-P-1", "name": "x", "part_type": "manufactured"},
            headers=_bearer(token),
        )
        assert refused.status_code == 403
        created = client.post(
            "/api/v1/parts/",
            json={"part_number": "RO-P-2", "name": "Person part", "part_type": "manufactured"},
            headers=auth_headers,
        )
        assert created.status_code in (200, 201), created.text

    def test_a_full_token_is_neither_read_only_nor_ai_external(
        self, client: TestClient, db_session: Session, admin_headers: dict, test_user: User
    ):
        _id, token, _body = _issue(client, admin_headers, test_user)
        user = deps.get_current_user(request=_request_for("POST", None), db=db_session, token=token)
        assert (user._api_token_scope, user._api_token_ai_external) == ("full", False)
        assert not is_read_only_api_token(user) and not is_ai_external_credential(user)


# --------------------------------------------------------------------------- the deny-tag setting


class TestDenyTagSetting:
    def test_the_default_is_the_brief_list_and_every_entry_is_a_live_tag(self):
        assert settings.read_only_deny_tags == DEFAULT_DENY_TAGS
        live = catalog_tags(app.openapi())
        assert DEFAULT_DENY_TAGS <= live, f"not live: {DEFAULT_DENY_TAGS - live}"
        assert deps.live_route_tags(app.routes) >= DEFAULT_DENY_TAGS

    def test_hank_intake_has_its_own_tag(self):
        intake = [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.path_format.startswith(f"{API_PREFIX}/hank/intake")
        ]
        assert intake and all(route.tags == ["Hank Intake"] for route in intake)

    def test_the_property_strips_and_drops_empties(self, monkeypatch):
        monkeypatch.setattr(settings, "WERCO_MCP_READ_ONLY_DENY_TAGS", " Audit , ,Users,")
        assert settings.read_only_deny_tags == frozenset({"Audit", "Users"})
        monkeypatch.setattr(settings, "WERCO_MCP_READ_ONLY_DENY_TAGS", "")
        assert settings.read_only_deny_tags == frozenset()

    def test_the_boot_check_passes_on_the_default(self):
        deps.refuse_unknown_read_only_deny_tags(app.routes)

    @pytest.mark.parametrize("typo", ["API tokens", "audit", "CAD Source Storage"])
    def test_the_boot_check_refuses_a_tag_no_route_carries(self, typo: str):
        with pytest.raises(RuntimeError) as raised:
            deps.refuse_unknown_read_only_deny_tags(app.routes, frozenset({"Audit", typo}))
        assert repr(typo) in str(raised.value)

    @pytest.mark.integration
    def test_a_misspelled_deny_tag_fails_the_real_boot(self):
        """The check is WIRED: importing ``app.main`` with a typo in the setting raises, so a deploy
        fails loudly instead of silently denying nothing. (The function alone is pinned above;
        this pins that ``main.py`` calls it.)"""
        # Built from scratch (clean_env's OS plumbing) plus just enough to import the app: an
        # in-memory database nothing ever touches, throwaway keys, and the misspelled list.
        env = {
            **clean_env(),
            "DATABASE_URL": "sqlite:///file:p0-boot-check?mode=memory&cache=shared&uri=true",
            "SECRET_KEY": "boot-check-secret-key-abcdefghijklmnopqrstuvwxyz12",
            "REFRESH_TOKEN_SECRET_KEY": "boot-check-refresh-key-abcdefghijklmnopqrstuvwxyz",
            "ENVIRONMENT": "test",
            "SENTRY_DSN": "",
            "WERCO_MCP_READ_ONLY_DENY_TAGS": "Audit,API tokens",
        }
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode != 0, "a misspelled deny tag must fail the boot"
        assert "WERCO_MCP_READ_ONLY_DENY_TAGS names tag(s) no API route carries: 'API tokens'" in result.stderr


# --------------------------------------------------------------------------- global search


class TestSearchDropsPeople:
    def test_a_read_only_token_never_finds_users(
        self, client: TestClient, admin_headers: dict, admin_user: User, test_user: User
    ):
        _id, full_token, _body = _issue(client, admin_headers, admin_user)
        # Not AI-external: global search is not on the AI-external allowlist at all.
        _id, ro_token, _body = _issue(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        query = {"q": test_user.email.split("@")[0], "types": "user"}

        full = client.get("/api/v1/search/", params=query, headers=_bearer(full_token))
        assert full.status_code == 200, full.text
        assert any(result["type"] == "user" for result in full.json()["results"]), "an Admin finds people"

        narrowed = client.get("/api/v1/search/", params=query, headers=_bearer(ro_token))
        assert narrowed.status_code == 200, narrowed.text
        assert narrowed.json()["results"] == []
        assert "user" not in narrowed.json()["categories"]
