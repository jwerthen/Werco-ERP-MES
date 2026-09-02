"""The API-token branch of ``get_current_user`` and everything that hangs off it.

What is proven here, each against a real HTTP status/detail or a DB row:

- a live API token resolves its user on a normal route and is exactly as
  powerful as that user (RBAC decides, not the credential);
- the ROW pins tenancy (``_active_company_id``), never a claim;
- the path fence (403 on ``/auth`` and ``/api-tokens``) fires before body validation;
- forged claims, wrong signature, unknown jti, wrong type are 401 with ``WWW-Authenticate``;
- ``last_used_at`` is touched once and not again inside five minutes (a
  monkeypatched service clock -- ``freezegun`` is not in requirements-dev);
- a disabled user is 403 and is never touched;
- the wallboard / kiosk-queue / visitor sign-in principals accept the token
  and delegate the row check (a revoked token is 401 on all three);
- a shop-floor clock-in with an API token records the client's declared
  ``TimeEntrySource`` (desktop) or NULL -- never the kiosk value.

The plaintext token is never printed: wherever a failure message would echo
it, the comparison is bound to a bare boolean first.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api import deps
from app.api.deps import (
    API_TOKEN_DENIED_PREFIXES,
    API_TOKEN_SCOPE,
    _is_api_token_denied_path,
    get_current_user,
    is_user_bearer,
)
from app.core.config import settings
from app.core.security import create_access_token, create_api_token, create_display_token
from app.models.api_token import ApiToken
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntrySource
from app.models.user import User
from app.models.visitor_log import VisitorLog
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import api_token_service
from app.services.api_token_service import LAST_USED_TOUCH_INTERVAL, touch_api_token_last_used
from app.services.audit_service import AuditService

pytestmark = pytest.mark.api

API_TOKENS_URL = "/api/v1/api-tokens/"
PARTS_URL = "/api/v1/parts/"
ME_URL = "/api/v1/users/me"
ADMIN_ONLY_URL = "/api/v1/admin/settings/materials"
WALLBOARD_URL = "/api/v1/shop-floor/wallboard"
SIGN_IN_URL = "/api/v1/visitor-logs/sign-in"
CLOCK_IN_URL = "/api/v1/shop-floor/clock-in"
FENCE_DETAIL = "API token cannot access this resource"
CREDENTIALS_DETAIL = "Could not validate credentials"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _issue(client: TestClient, admin_headers: dict, user: User, **extra) -> tuple[int, str]:
    response = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": f"bot for {user.email}", **extra}, headers=admin_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], response.json()["token"]


def _revoke(client: TestClient, admin_headers: dict, token_id: int) -> None:
    response = client.post(f"{API_TOKENS_URL}{token_id}/revoke", json={"reason": "test revoke"}, headers=admin_headers)
    assert response.status_code == 200, response.text


def _request_for(path: str, method: str = "GET") -> Request:
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
    return Request(scope)


@pytest.fixture
def manager_token(client: TestClient, admin_headers: dict, test_user: User) -> tuple[int, str]:
    """(row id, token) for the MANAGER ``test_user``."""
    return _issue(client, admin_headers, test_user)


# --------------------------------------------------------------------------- resolves the user


class TestResolvesTheUser:
    def test_normal_route_and_identity(self, client: TestClient, manager_token, test_user: User):
        _token_id, token = manager_token
        assert client.get(PARTS_URL, headers=_bearer(token)).status_code == 200
        me = client.get(ME_URL, headers=_bearer(token))
        assert me.status_code == 200, me.text
        assert me.json()["id"] == test_user.id
        assert me.json()["email"] == test_user.email
        assert me.json()["role"] == "manager"

    def test_carries_the_users_role(
        self, client: TestClient, admin_headers: dict, manager_token, admin_user: User, test_user: User
    ):
        _id, manager_api = manager_token
        denied = client.get(ADMIN_ONLY_URL, headers=_bearer(manager_api))
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Insufficient permissions", "RBAC refused it, not the fence"

        _id, admin_api = _issue(client, admin_headers, admin_user)
        assert client.get(ADMIN_ONLY_URL, headers=_bearer(admin_api)).status_code == 200

    def test_a_write_is_attributed_to_the_tokens_user_and_marked_as_the_tokens(
        self, client: TestClient, db_session: Session, manager_token, test_user: User
    ):
        """The audit row names the bound user AND carries the credential marker; an interactive write does not.

        Without the marker a token's writes were indistinguishable from the person's
        own -- a non-repudiation gap on quality records (an Admin can mint a token for
        a human inspector). ``extra_data.credential`` answers "which credential did this".
        """
        token_id, token = manager_token
        api_row = db_session.get(ApiToken, token_id)

        def _create(part_number: str, headers: dict) -> AuditLog:
            response = client.post(
                PARTS_URL,
                json={
                    "part_number": part_number,
                    "name": "Made through the seam under test",
                    "part_type": "manufactured",
                    "unit_of_measure": "each",
                },
                headers=headers,
            )
            assert response.status_code in (200, 201), response.text
            part = db_session.query(Part).filter(Part.part_number == part_number).one()
            assert part.company_id == 1
            return (
                db_session.query(AuditLog)
                .filter(AuditLog.resource_type == "part", AuditLog.resource_id == part.id, AuditLog.action == "CREATE")
                .one()
            )

        by_token = _create("API-TOKEN-PART-1", _bearer(token))
        assert by_token.user_id == test_user.id, "attributed to the bound user, exactly as the SPA would be"
        assert by_token.extra_data[AuditService.CREDENTIAL_KEY] == {
            "kind": "api_token",
            "api_token_id": token_id,
            "jti_prefix": api_row.jti_prefix,
            "label": api_row.label,
        }
        marker_text = json.dumps(by_token.extra_data)
        assert (token not in marker_text) is True and (api_row.jti not in marker_text) is True

        # The same person, interactively, on the SAME ORM instance: no marker.
        by_person = _create("API-TOKEN-PART-2", _bearer(create_access_token(subject=test_user.id, company_id=1)))
        assert by_person.user_id == test_user.id
        assert AuditService.CREDENTIAL_KEY not in (by_person.extra_data or {})

    def test_pins_active_company_to_the_row_never_read_only_scope_api(
        self, client: TestClient, db_session: Session, manager_token, test_user: User
    ):
        token_id, token = manager_token
        row = db_session.get(ApiToken, token_id)
        user = get_current_user(_request_for(PARTS_URL), db=db_session, token=token)
        assert user.id == test_user.id
        assert user._active_company_id == row.company_id == 1
        assert user._read_only_company_context is False
        assert user._token_scope == API_TOKEN_SCOPE == "api"
        assert deps.get_current_company_id(user) == 1
        assert user._api_token_id == token_id, "the credential marker AuditService folds into every row"

        # The same instance serving an interactive credential next: the marker is cleared.
        again = get_current_user(
            _request_for(PARTS_URL), db=db_session, token=create_access_token(subject=test_user.id, company_id=1)
        )
        assert again is user and again._api_token_id is None

    def test_platform_principals_never_hold_or_ride_an_api_token(
        self, client: TestClient, db_session: Session, admin_headers: dict, admin_user: User
    ):
        """An access token can carry a switched company_id; an API token's company is the ROW's --
        and a superuser / PLATFORM_ADMIN holds no API token at all (409 at mint, 401 at use):
        ``require_role`` waves those principals through every gate and ``/platform/*`` addresses
        other companies by explicit id, which no row pin constrains."""
        from tests.test_api_tokens import seed_second_company

        seed_second_company(db_session)
        _id, token = _issue(client, admin_headers, admin_user)  # a plain tenant Admin: fine
        row = db_session.query(ApiToken).one()
        assert row.company_id == 1
        assert get_current_user(_request_for(PARTS_URL), db=db_session, token=token)._active_company_id == 1
        forged = jwt.encode(
            {"sub": f"api:{row.jti}", "type": "api", "jti": row.jti, "user_id": admin_user.id, "company_id": 2},
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        with pytest.raises(HTTPException) as excinfo:
            get_current_user(_request_for(PARTS_URL), db=db_session, token=forged)
        assert excinfo.value.status_code == 401

        # Promote the holder: their ACCESS token may switch company; the standing token is refused.
        admin_user.is_superuser = True
        db_session.commit()
        switched = create_access_token(subject=admin_user.id, company_id=2)
        assert get_current_user(_request_for(PARTS_URL), db=db_session, token=switched)._active_company_id == 2
        with pytest.raises(HTTPException) as excinfo:
            get_current_user(_request_for(PARTS_URL), db=db_session, token=token)
        assert excinfo.value.status_code == 401, "refused, never widened"
        assert client.get(PARTS_URL, headers=_bearer(token)).status_code == 401
        refused = client.post(
            API_TOKENS_URL, json={"user_id": admin_user.id, "label": "for the superuser"}, headers=admin_headers
        )
        assert refused.status_code == 409
        assert db_session.query(ApiToken).count() == 1


# --------------------------------------------------------------------------- fence


class TestFence:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/api/v1/auth", True),
            ("/api/v1/auth/", True),
            ("/api/v1/auth/refresh", True),
            ("/api/v1/auth/display-token", True),
            ("/api/v1/api-tokens", True),
            ("/api/v1/api-tokens/", True),
            ("/api/v1/api-tokens/3/revoke", True),
            ("/api/v1/authors", False),
            ("/api/v1/api-tokens-report", False),
            ("/api/v1/parts/", False),
            ("/api/v1/shop-floor/clock-in", False),
            ("/", False),
        ],
    )
    def test_denied_path_predicate(self, path: str, expected: bool):
        assert _is_api_token_denied_path(path) is expected

    def test_denied_prefixes_derive_from_the_mounted_api_prefix(self):
        """The fence follows ``settings.API_V1_PREFIX`` -- a re-mount can never leave it inert."""
        prefix = settings.API_V1_PREFIX.rstrip("/")
        assert API_TOKEN_DENIED_PREFIXES == (f"{prefix}/auth", f"{prefix}/api-tokens")
        assert settings.API_V1_PREFIX == "/api/v1", "the docs, the SPA client and the MCP door all spell this prefix"

    def test_fence_is_403_before_body_validation(self, client: TestClient, admin_headers: dict, manager_token):
        token_id, token = manager_token
        api = _bearer(token)
        for method, path in (
            ("POST", "/api/v1/auth/logout"),
            ("GET", "/api/v1/auth/display-token"),
            ("POST", "/api/v1/auth/display-token"),
            ("GET", API_TOKENS_URL),
            ("POST", API_TOKENS_URL),
            ("POST", f"{API_TOKENS_URL}{token_id}/revoke"),
        ):
            response = client.request(method, path, headers=api, json={} if method == "POST" else None)
            assert response.status_code == 403, (method, path, response.status_code)
            assert response.json()["detail"] == FENCE_DETAIL, (method, path)
        # The same interactive Admin can still reach the fenced verbs -- the fence is about credential KIND.
        assert client.get(API_TOKENS_URL, headers=admin_headers).status_code == 200

    def test_refresh_and_badge_mint_refuse_the_token_without_minting_a_session(
        self, client: TestClient, manager_token, test_user: User
    ):
        _id, token = manager_token
        api = _bearer(token)
        # /auth/refresh is public and keyed on the body: an API token is not a refresh credential.
        refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": token}, headers=api)
        assert refresh.status_code == 401, refresh.text
        assert "access_token" not in refresh.json()
        # /auth/kiosk-badge-token reads the raw bearer as a STATION token: an API token is not one.
        badge = client.post("/api/v1/auth/kiosk-badge-token", json={"employee_id": test_user.employee_id}, headers=api)
        assert badge.status_code == 401, badge.text
        assert "access_token" not in badge.json()

    def test_a_revoked_token_is_401_on_a_fenced_path_not_403(
        self, client: TestClient, admin_headers: dict, manager_token
    ):
        """The row check runs first: a dead token is 'bad credentials' everywhere, live-but-fenced is 403."""
        token_id, token = manager_token
        _revoke(client, admin_headers, token_id)
        response = client.get(API_TOKENS_URL, headers=_bearer(token))
        assert response.status_code == 401
        assert response.json()["detail"] == CREDENTIALS_DETAIL


# --------------------------------------------------------------------------- refusals


class TestRefusals:
    def test_forged_wrong_signature_unknown_jti_and_wrong_type_are_401(
        self, client: TestClient, db_session: Session, manager_token, admin_user: User, test_user: User
    ):
        from tests.test_api_tokens import seed_second_company

        seed_second_company(db_session)
        token_id, token = manager_token
        row = db_session.get(ApiToken, token_id)

        def forge(**claims) -> str:
            base = {"sub": f"api:{row.jti}", "type": "api", "jti": row.jti, "user_id": test_user.id, "company_id": 1}
            base.update(claims)
            secret = base.pop("_secret", settings.SECRET_KEY)
            return jwt.encode(base, secret, algorithm=settings.ALGORITHM)

        cases = {
            "wrong company_id": forge(company_id=2),
            "wrong user_id": forge(user_id=admin_user.id),
            "unknown jti": forge(jti="unknown-jti-value", sub="api:unknown-jti-value"),
            "missing jti": forge(jti=None),
            "wrong signature": forge(_secret="not-the-app-secret-key-at-all-0123456789"),
            "display type": forge(type="display"),
            "signin type": forge(type="signin"),
            "garbage": "not.a.jwt",
            "exp in the past": create_api_token(
                jti=row.jti, user_id=test_user.id, company_id=1, expires_at=datetime.utcnow() - timedelta(seconds=1)
            ),
        }
        for name, bad in cases.items():
            response = client.get(PARTS_URL, headers=_bearer(bad))
            assert response.status_code == 401, name
            assert response.json()["detail"] == CREDENTIALS_DETAIL, name
            assert response.headers.get("www-authenticate") == "Bearer", name
        db_session.refresh(row)
        assert row.last_used_at is None, "a refused token is never touched"
        assert client.get(PARTS_URL, headers=_bearer(token)).status_code == 200

    def test_is_user_bearer_is_signature_only(
        self, db_session: Session, client: TestClient, admin_headers: dict, test_user: User
    ):
        token_id, token = _issue(client, admin_headers, test_user)
        access = create_access_token(subject=test_user.id, company_id=1)
        display = create_display_token("jti-x", 1, "TV", datetime.utcnow() + timedelta(days=1))
        assert is_user_bearer(token) is True
        assert is_user_bearer(access) is True
        assert is_user_bearer(display) is False
        assert is_user_bearer("garbage") is False
        _revoke(client, admin_headers, token_id)
        assert is_user_bearer(token) is True, "signature only -- the row check inside get_current_user refuses it"
        with pytest.raises(HTTPException) as excinfo:
            get_current_user(_request_for(PARTS_URL), db=db_session, token=token)
        assert excinfo.value.status_code == 401

    def test_disabled_user_is_403_and_never_touched(
        self, client: TestClient, db_session: Session, manager_token, test_user: User
    ):
        token_id, token = manager_token
        test_user.is_active = False
        db_session.commit()
        response = client.get(PARTS_URL, headers=_bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"] == "User account is disabled"
        row = db_session.get(ApiToken, token_id)
        db_session.refresh(row)
        assert row.last_used_at is None, "a disabled user's token is not marked as used"

        test_user.is_active = True
        db_session.commit()
        assert client.get(PARTS_URL, headers=_bearer(token)).status_code == 200


# --------------------------------------------------------------------------- last_used_at throttle


class _FrozenDatetime(datetime):
    """``datetime`` whose ``utcnow`` is a dial the test turns (the service imports the class directly)."""

    current = datetime(2026, 9, 2, 12, 0, 0)

    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return cls.current


@pytest.fixture
def service_clock(monkeypatch):
    _FrozenDatetime.current = datetime(2026, 9, 2, 12, 0, 0)
    monkeypatch.setattr(api_token_service, "datetime", _FrozenDatetime)
    return _FrozenDatetime


class TestLastUsedThrottle:
    def test_touched_once_then_not_again_inside_five_minutes(
        self, client: TestClient, db_session: Session, manager_token, service_clock
    ):
        token_id, token = manager_token
        api = _bearer(token)
        row = db_session.get(ApiToken, token_id)
        assert row.last_used_at is None
        t0 = service_clock.current

        assert client.get(PARTS_URL, headers=api).status_code == 200
        db_session.refresh(row)
        assert row.last_used_at == t0, "first use stamps the marker"

        service_clock.current = t0 + timedelta(minutes=4, seconds=59)
        assert client.get(PARTS_URL, headers=api).status_code == 200
        assert client.get(ME_URL, headers=api).status_code == 200
        db_session.refresh(row)
        assert row.last_used_at == t0, "inside the interval nothing is rewritten"

        service_clock.current = t0 + timedelta(minutes=5)
        assert client.get(PARTS_URL, headers=api).status_code == 200
        db_session.refresh(row)
        assert row.last_used_at == t0, "exactly five minutes is not yet OLDER than five minutes"

        service_clock.current = t0 + timedelta(minutes=5, seconds=1)
        assert client.get(PARTS_URL, headers=api).status_code == 200
        db_session.refresh(row)
        assert row.last_used_at == t0 + timedelta(minutes=5, seconds=1), "past the interval it is stamped again"

        service_clock.current = t0 + timedelta(minutes=8)
        assert client.get(PARTS_URL, headers=api).status_code == 200
        db_session.refresh(row)
        assert row.last_used_at == t0 + timedelta(minutes=5, seconds=1), "and the new stamp starts a new interval"

    def test_touch_helper_is_conditional_and_committed(self, client: TestClient, db_session: Session, manager_token):
        token_id, _token = manager_token
        row = db_session.get(ApiToken, token_id)
        t0 = datetime(2026, 9, 2, 8, 0, 0)
        assert LAST_USED_TOUCH_INTERVAL == timedelta(minutes=5)

        assert touch_api_token_last_used(db_session, row, now=t0) is True
        db_session.refresh(row)
        assert row.last_used_at == t0
        assert touch_api_token_last_used(db_session, row, now=t0 + timedelta(minutes=1)) is False
        db_session.refresh(row)
        assert row.last_used_at == t0
        # The WHERE re-checks the interval even when the caller's copy of the row is stale
        # (a detached stub that still believes last_used_at is NULL): the DB wins, no rewrite.
        stale = ApiToken(id=token_id, last_used_at=None)
        assert touch_api_token_last_used(db_session, stale, now=t0 + timedelta(minutes=2)) is False
        db_session.expire_all()
        assert db_session.get(ApiToken, token_id).last_used_at == t0
        # Strictly OLDER than the interval: the boundary itself is not a rewrite.
        assert touch_api_token_last_used(db_session, stale, now=t0 + timedelta(minutes=5)) is False
        db_session.expire_all()
        assert db_session.get(ApiToken, token_id).last_used_at == t0
        later = t0 + timedelta(minutes=5, seconds=1)
        assert touch_api_token_last_used(db_session, db_session.get(ApiToken, token_id), now=later) is True
        db_session.expire_all()
        assert db_session.get(ApiToken, token_id).last_used_at == later


# --------------------------------------------------------------------------- sibling principals


class TestSiblingPrincipals:
    def test_wallboard_kiosk_queue_and_visitor_signin_accept_the_token_until_revoked(
        self,
        client: TestClient,
        db_session: Session,
        admin_headers: dict,
        manager_token,
        test_work_center: WorkCenter,
    ):
        token_id, token = manager_token
        api = _bearer(token)
        queue_url = f"/api/v1/shop-floor/work-center-queue/{test_work_center.id}"
        signin_payload = {
            "visitor_name": "Jane Caller",
            "visitor_company": "Acme Supply",
            "visitor_phone": "555-0100",
            "host_name": None,
            "purpose": "meeting",
            "purpose_note": None,
            "safety_acknowledged": True,
        }

        wall = client.get(WALLBOARD_URL, headers=api)
        assert wall.status_code == 200, wall.text
        queue = client.get(queue_url, headers=api)
        assert queue.status_code == 200, queue.text
        signed = client.post(SIGN_IN_URL, json=signin_payload, headers=api)
        assert signed.status_code == 201, signed.text
        visit = db_session.get(VisitorLog, signed.json()["id"])
        assert visit is not None and visit.company_id == 1
        assert visit.signin_station_id is None, "a user principal, not a station"

        # All three delegate the ROW check: a revoked token is 401 on each.
        _revoke(client, admin_headers, token_id)
        assert client.get(WALLBOARD_URL, headers=api).status_code == 401
        assert client.get(queue_url, headers=api).status_code == 401
        assert client.post(SIGN_IN_URL, json=signin_payload, headers=api).status_code == 401
        assert db_session.query(VisitorLog).count() == 1


# --------------------------------------------------------------------------- shop-floor labor source


def _make_started_operation(db: Session) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    part = Part(
        part_number="API-TOKEN-CLOCK-PART",
        name="Clock-in part",
        description="fixture",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name="API-TOKEN-WC",
        code="API-TOKEN-WC",
        work_center_type="welding",
        description="fixture",
        hourly_rate=100.0,
        is_active=True,
        company_id=1,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number="API-TOKEN-WO-00001",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=1,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="10",
        name="Weld",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
        company_id=1,
    )
    db.add(op)
    db.commit()
    return wo, op, wc


class TestLaborSource:
    def test_clock_in_with_an_api_token_records_the_desktop_channel_never_kiosk(
        self, client: TestClient, db_session: Session, admin_headers: dict, operator_user: User
    ):
        _id, token = _issue(client, admin_headers, operator_user)
        wo, op, wc = _make_started_operation(db_session)
        payload = {"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"}

        declared = client.post(CLOCK_IN_URL, json={**payload, "source": "desktop"}, headers=_bearer(token))
        assert declared.status_code == 200, declared.text
        entry = db_session.get(TimeEntry, declared.json()["id"])
        assert entry.user_id == operator_user.id and entry.company_id == 1
        assert entry.source == TimeEntrySource.DESKTOP.value
        assert declared.json()["source"] == "desktop"

        # Clock out, then clock in again with no declared channel: NULL, never KIOSK.
        out = client.post(
            f"/api/v1/shop-floor/clock-out/{entry.id}", json={"quantity_produced": 0}, headers=_bearer(token)
        )
        assert out.status_code == 200, out.text
        silent = client.post(CLOCK_IN_URL, json=payload, headers=_bearer(token))
        assert silent.status_code == 200, silent.text
        second = db_session.get(TimeEntry, silent.json()["id"])
        assert second.source is None
        assert second.source != TimeEntrySource.KIOSK.value
