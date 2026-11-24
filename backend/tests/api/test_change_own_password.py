"""Self-service password change (``POST /users/change-password``) and the admin reset stamp.

* A signed-in person changes their own password: the new hash, ``password_changed_at`` and one
  ``PASSWORD_CHANGE`` audit row (``source = self_service``) commit together.
* The audit row is REQUIRED: an audit outage is a 503 and nothing is saved.
* A wrong current password is a 400 that changes nothing and writes no row.
* A standing API token and a crew-station badge token are refused 403 before anything changes.
* The admin reset stamps ``password_changed_at`` too.
* The route is not an MCP tool.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.credentials import PASSWORD_CHANGE_PEOPLE_ONLY
from app.core.security import create_access_token, create_kiosk_token, get_password_hash, verify_password
from app.main import app
from app.mcp.catalog import (
    EXCLUDED_OPERATIONS,
    PASSWORD_OPERATIONS,
    build_catalog,
    iter_secured_operations,
)
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.export_control_helpers import issue_token

pytestmark = pytest.mark.api

URL = "/api/v1/users/change-password"
XHR = {"X-Requested-With": "XMLHttpRequest"}
NEW_PASSWORD = "BrandNewSecret!42"
LONG_AGO = datetime(2025, 1, 1, 12, 0, 0)


def _state(db: Session, user_id: int) -> tuple:
    db.expire_all()
    row = db.get(User, user_id)
    return row.hashed_password, row.password_changed_at


def _password_rows(db: Session, user_id: int) -> List[AuditLog]:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "PASSWORD_CHANGE", AuditLog.resource_id == user_id)
        .order_by(AuditLog.id)
        .all()
    )


def _age_password(db: Session, user: User) -> None:
    user.password_changed_at = LONG_AGO
    db.commit()


def _body(current: str, new: Optional[str] = NEW_PASSWORD) -> dict:
    return {"current_password": current, "new_password": new}


class TestSelfServiceChange:
    def test_change_stamps_password_changed_at_and_writes_one_row(
        self, client: TestClient, db_session, test_user, auth_headers, test_user_credentials
    ):
        _age_password(db_session, test_user)
        started = datetime.utcnow() - timedelta(seconds=5)

        response = client.post(URL, headers=auth_headers, json=_body(test_user_credentials["password"]))

        assert response.status_code == 200, response.text
        assert response.json() == {"message": "Password changed successfully"}
        hashed, changed_at = _state(db_session, test_user.id)
        assert verify_password(NEW_PASSWORD, hashed)
        assert not verify_password(test_user_credentials["password"], hashed)
        assert changed_at is not None and changed_at >= started
        rows = _password_rows(db_session, test_user.id)
        assert len(rows) == 1
        assert (rows[0].extra_data or {}).get("source") == "self_service"
        assert rows[0].user_id == test_user.id

    def test_the_new_password_signs_in_and_the_old_one_does_not(
        self, client: TestClient, db_session, test_user, auth_headers, test_user_credentials
    ):
        old = test_user_credentials["password"]
        assert client.post(URL, headers=auth_headers, json=_body(old)).status_code == 200

        login = "/api/v1/auth/login"
        refused = client.post(login, data={"username": test_user.email, "password": old}, headers=XHR)
        accepted = client.post(login, data={"username": test_user.email, "password": NEW_PASSWORD}, headers=XHR)
        assert refused.status_code == 401, refused.text
        assert accepted.status_code == 200, accepted.text

    def test_wrong_current_password_is_400_and_changes_nothing(
        self, client: TestClient, db_session, test_user, auth_headers
    ):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        response = client.post(URL, headers=auth_headers, json=_body("NotMyPasswordAtAll!93"))

        assert response.status_code == 400
        assert response.json()["detail"] == "Current password is incorrect"
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []

    def test_weak_new_password_is_422_and_changes_nothing(
        self, client: TestClient, db_session, test_user, auth_headers, test_user_credentials
    ):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        response = client.post(URL, headers=auth_headers, json=_body(test_user_credentials["password"], "short1"))

        assert response.status_code == 422
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []

    def test_audit_loss_is_a_503_with_nothing_saved(
        self, client: TestClient, db_session, test_user, auth_headers, test_user_credentials, monkeypatch
    ):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        def refuse(self, action, resource_type, **kwargs):
            raise AuditWriteError("simulated audit outage")

        monkeypatch.setattr(AuditService, "log_required", refuse)

        response = client.post(URL, headers=auth_headers, json=_body(test_user_credentials["password"]))

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "Unable to save audit record"
        assert _state(db_session, test_user.id) == before, "a lost audit row rolls the password change back"
        assert _password_rows(db_session, test_user.id) == []


class TestNonPersonCredentials:
    def test_a_full_api_token_is_refused_before_anything_changes(
        self, client: TestClient, db_session, test_user, admin_headers, test_user_credentials
    ):
        _id, token = issue_token(client, admin_headers, test_user)
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        response = client.post(
            URL,
            headers={**XHR, "Authorization": f"Bearer {token}"},
            json=_body(test_user_credentials["password"]),
        )

        assert response.status_code == 403
        assert response.json()["detail"] == PASSWORD_CHANGE_PEOPLE_ONLY
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []

    def test_a_token_is_refused_even_with_a_wrong_current_password(
        self, client: TestClient, db_session, test_user, admin_headers
    ):
        """The credential refusal comes first, so a token cannot use the route as a password oracle."""
        _id, token = issue_token(client, admin_headers, test_user)

        response = client.post(
            URL,
            headers={**XHR, "Authorization": f"Bearer {token}"},
            json=_body("NotMyPasswordAtAll!93"),
        )

        assert response.status_code == 403
        assert response.json()["detail"] == PASSWORD_CHANGE_PEOPLE_ONLY

    def test_a_kiosk_badge_token_is_refused(self, client: TestClient, db_session, test_user, test_user_credentials):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)
        token = create_access_token(subject=test_user.id, company_id=test_user.company_id, scope="kiosk")

        response = client.post(
            URL,
            headers={**XHR, "Authorization": f"Bearer {token}"},
            json=_body(test_user_credentials["password"]),
        )

        assert response.status_code == 403
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []


class TestAdminResetStamp:
    def test_admin_reset_stamps_password_changed_at(self, client: TestClient, db_session, test_user, admin_headers):
        _age_password(db_session, test_user)
        started = datetime.utcnow() - timedelta(seconds=5)

        response = client.post(
            f"/api/v1/users/{test_user.id}/reset-password",
            headers=admin_headers,
            json={"new_password": NEW_PASSWORD},
        )

        assert response.status_code == 200, response.text
        hashed, changed_at = _state(db_session, test_user.id)
        assert verify_password(NEW_PASSWORD, hashed)
        assert changed_at is not None and changed_at >= started

    def test_admin_reset_audit_loss_leaves_the_stamp_alone(
        self, client: TestClient, db_session, test_user, admin_headers, monkeypatch
    ):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        def refuse(self, action, resource_type, **kwargs):
            raise AuditWriteError("simulated audit outage")

        monkeypatch.setattr(AuditService, "log_required", refuse)

        response = client.post(
            f"/api/v1/users/{test_user.id}/reset-password",
            headers=admin_headers,
            json={"new_password": NEW_PASSWORD},
        )

        assert response.status_code == 503
        assert _state(db_session, test_user.id) == before


class TestAnyRoleOwnRowOnly:
    """Every role may change its own password, and the change touches the caller's row only."""

    def test_an_operator_changes_their_own_password(
        self, client: TestClient, db_session, operator_user, operator_headers, test_user, test_user_credentials
    ):
        _age_password(db_session, operator_user)
        _age_password(db_session, test_user)
        bystander_before = _state(db_session, test_user.id)

        response = client.post(URL, headers=operator_headers, json=_body(test_user_credentials["password"]))

        assert response.status_code == 200, response.text
        hashed, changed_at = _state(db_session, operator_user.id)
        assert verify_password(NEW_PASSWORD, hashed)
        assert changed_at is not None and changed_at > LONG_AGO
        assert _state(db_session, test_user.id) == bystander_before, "another user's row is untouched"
        assert _password_rows(db_session, test_user.id) == []
        assert len(_password_rows(db_session, operator_user.id)) == 1

    def test_a_viewer_changes_their_own_password(self, client: TestClient, db_session, test_user_credentials):
        viewer = User(
            email="viewer-pw@werco.com",
            employee_id="EMP-VIEW-PW",
            first_name="Vera",
            last_name="Viewer",
            hashed_password=get_password_hash(test_user_credentials["password"]),
            role=UserRole.VIEWER,
            is_active=True,
            company_id=1,
        )
        db_session.add(viewer)
        db_session.commit()
        headers = {**XHR, "Authorization": f"Bearer {create_access_token(subject=viewer.id, company_id=1)}"}

        response = client.post(URL, headers=headers, json=_body(test_user_credentials["password"]))

        assert response.status_code == 200, response.text
        assert verify_password(NEW_PASSWORD, _state(db_session, viewer.id)[0])

    def test_the_audit_row_carries_no_password_or_hash(
        self, client: TestClient, db_session, test_user, auth_headers, test_user_credentials
    ):
        response = client.post(URL, headers=auth_headers, json=_body(test_user_credentials["password"]))
        assert response.status_code == 200, response.text

        hashed, _changed = _state(db_session, test_user.id)
        (row,) = _password_rows(db_session, test_user.id)
        assert row.old_values is None and row.new_values is None
        assert row.company_id == test_user.company_id
        blob = repr([row.description, row.old_values, row.new_values, row.extra_data, row.resource_identifier])
        for secret in (NEW_PASSWORD, test_user_credentials["password"], hashed):
            assert secret not in blob


class TestNarrowedAndStationCredentials:
    @pytest.mark.parametrize(
        "extra",
        [{"scope": "read_only"}, {"scope": "read_only", "ai_external": True}],
        ids=["read_only", "ai_external"],
    )
    def test_a_narrowed_api_token_is_refused_and_changes_nothing(
        self, client: TestClient, db_session, test_user, admin_headers, test_user_credentials, extra
    ):
        _id, token = issue_token(client, admin_headers, test_user, **extra)
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)

        response = client.post(
            URL,
            headers={**XHR, "Authorization": f"Bearer {token}"},
            json=_body(test_user_credentials["password"]),
        )

        assert response.status_code == 403, response.text
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []

    def test_a_crew_station_token_is_not_a_user_credential(
        self, client: TestClient, db_session, test_user, test_user_credentials
    ):
        _age_password(db_session, test_user)
        before = _state(db_session, test_user.id)
        token = create_kiosk_token(station_id=1, company_id=test_user.company_id, label="Press brake crew")

        response = client.post(
            URL,
            headers={**XHR, "Authorization": f"Bearer {token}"},
            json=_body(test_user_credentials["password"]),
        )

        assert response.status_code == 401
        assert _state(db_session, test_user.id) == before
        assert _password_rows(db_session, test_user.id) == []


class TestExplicitRefusalIsTheRouteOwn:
    """The refusal holds even if the path fences in ``get_current_user`` were moved or widened.

    Called directly with a principal already resolved, so no fence runs: the route itself must
    refuse a badge- or API-token principal before it reads the current password.
    """

    @pytest.mark.parametrize(
        "marker",
        [{"_token_scope": "kiosk"}, {"_token_scope": "api", "_api_token_id": 7}],
        ids=["badge", "api_token"],
    )
    def test_route_refuses_before_checking_the_password(self, db_session, test_user, monkeypatch, marker):
        from fastapi import HTTPException

        from app.api.endpoints import users as users_endpoint
        from app.api.endpoints.users import PasswordChange, change_own_password

        checked = []
        monkeypatch.setattr(users_endpoint, "verify_password", lambda *a, **k: checked.append(a) or True)
        for name, value in marker.items():
            setattr(test_user, name, value)
        audit = AuditService(db_session, test_user)

        with pytest.raises(HTTPException) as refused:
            change_own_password(
                password_data=PasswordChange(current_password="whatever", new_password=NEW_PASSWORD),
                db=db_session,
                current_user=test_user,
                audit=audit,
            )

        assert refused.value.status_code == 403
        assert refused.value.detail == PASSWORD_CHANGE_PEOPLE_ONLY
        assert checked == [], "the credential refusal comes before the password check"


class TestAdminResetFences:
    def test_another_companys_user_is_404_with_nothing_changed(self, client: TestClient, db_session, admin_headers):
        if db_session.get(Company, 2) is None:
            db_session.add(Company(id=2, name="Fictional Fabrication Co", slug="fictional-fab", is_active=True))
            db_session.commit()
        outsider = User(
            email="outsider@fictional-fab.example",
            employee_id="EMP-OUT-001",
            first_name="Otto",
            last_name="Outsider",
            hashed_password=get_password_hash("Original-Outsider-Pass-1"),
            role=UserRole.OPERATOR,
            is_active=True,
            company_id=2,
            password_changed_at=LONG_AGO,
        )
        db_session.add(outsider)
        db_session.commit()
        before = _state(db_session, outsider.id)

        response = client.post(
            f"/api/v1/users/{outsider.id}/reset-password",
            headers=admin_headers,
            json={"new_password": NEW_PASSWORD},
        )

        assert response.status_code == 404
        assert _state(db_session, outsider.id) == before
        assert _password_rows(db_session, outsider.id) == []

    def test_a_manager_cannot_reset_and_nothing_changes(
        self, client: TestClient, db_session, auth_headers, operator_user
    ):
        _age_password(db_session, operator_user)
        before = _state(db_session, operator_user.id)

        response = client.post(
            f"/api/v1/users/{operator_user.id}/reset-password",
            headers=auth_headers,
            json={"new_password": NEW_PASSWORD},
        )

        assert response.status_code == 403
        assert _state(db_session, operator_user.id) == before
        assert _password_rows(db_session, operator_user.id) == []

    def test_admin_reset_writes_one_required_row(
        self, client: TestClient, db_session, test_user, admin_headers, admin_user
    ):
        response = client.post(
            f"/api/v1/users/{test_user.id}/reset-password",
            headers=admin_headers,
            json={"new_password": NEW_PASSWORD},
        )

        assert response.status_code == 200, response.text
        rows = _password_rows(db_session, test_user.id)
        assert len(rows) == 1
        assert rows[0].user_id == admin_user.id
        assert rows[0].old_values is None and rows[0].new_values is None


class TestMcpExclusion:
    @pytest.fixture(scope="class")
    def spec(self):
        return app.openapi()

    def test_the_route_is_the_password_exclusion(self):
        assert set(PASSWORD_OPERATIONS) == {("POST", "/api/v1/users/change-password")}
        assert set(PASSWORD_OPERATIONS) <= set(EXCLUDED_OPERATIONS)

    def test_it_is_a_real_secured_route_so_the_exclusion_is_load_bearing(self, spec):
        secured = {(method, path) for method, path, _op, _params in iter_secured_operations(spec)}
        assert set(PASSWORD_OPERATIONS) <= secured

    def test_it_is_not_a_tool_and_only_the_exclusion_keeps_it_out(self, spec):
        catalog = build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)
        assert {tool.key for tool in catalog}.isdisjoint(PASSWORD_OPERATIONS)
        assert "change_own_password" not in {tool.name for tool in catalog}

        without = build_catalog(
            spec,
            shadowed=SHADOWED_OPERATIONS,
            reserved_names=CONVENIENCE_TOOL_NAMES,
            excluded_operations=set(EXCLUDED_OPERATIONS) - set(PASSWORD_OPERATIONS),
        )
        assert set(PASSWORD_OPERATIONS) <= {tool.key for tool in without}


class TestPublishedDescription:
    """The route's docstring is its OpenAPI description at /docs: it must not promise that other
    sessions die within an access-token lifetime, because refresh never reads password_changed_at."""

    def test_description_does_not_claim_other_sessions_end(self):
        description = app.openapi()["paths"][URL]["post"]["description"]
        assert "15 minutes" not in description
        assert "does NOT end other signed-in sessions" in description
        assert "7-day idle window" in description

    def test_refresh_path_really_does_not_read_password_changed_at(self):
        """Pins the fact the description relies on; change both together."""
        import inspect

        from app.api.endpoints import auth

        assert "password_changed_at" not in inspect.getsource(auth)
