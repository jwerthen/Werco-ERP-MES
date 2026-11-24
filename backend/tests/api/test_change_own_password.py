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
from app.core.security import create_access_token, verify_password
from app.main import app
from app.mcp.catalog import (
    EXCLUDED_OPERATIONS,
    PASSWORD_OPERATIONS,
    build_catalog,
    iter_secured_operations,
)
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.user import User
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
