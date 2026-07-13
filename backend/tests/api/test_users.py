"""
Integration tests for user management endpoints.
Tests user CRUD operations and role-based access.
"""

import json

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole


def _committed_user_audit_rows(db: Session, *, resource_id: int, action: str):
    """Return COMMITTED AuditLog rows for a user resource (mirrors the
    tests/api/test_qms_soft_delete_audit.py gold-standard pattern).

    The ``client`` fixture overrides ``get_db`` to yield the one shared, never
    -closed ``db_session``, so the endpoint and the test live in the same open
    transaction. ``AuditService.log()`` only ``flush()``es; the handler owns the
    ``commit()``. Rolling back BEFORE querying discards any flushed-but-uncommitted
    row while a truly COMMITTED audit row survives — so this proves the endpoint
    committed the audit entry, not merely staged it.
    """
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "user",
            AuditLog.resource_id == resource_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.sequence_number.desc())
        .all()
    )


@pytest.mark.api
class TestUsersAPI:
    """Test user management API endpoints."""

    def test_get_current_user(self, client: TestClient, auth_headers, test_user_credentials):
        """Test getting current user info."""
        response = client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["email"] == test_user_credentials["email"]
        assert "hashed_password" not in data

    def test_list_users_as_admin(self, client: TestClient, admin_headers):
        """Test admin can list all users."""
        response = client.get("/api/v1/users/", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list) or "items" in data

    def test_list_users_forbidden_for_operator(self, client: TestClient, operator_headers):
        """Test operator cannot list users."""
        response = client.get("/api/v1/users/", headers=operator_headers)
        # Either 403 or limited results
        assert response.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_200_OK]

    def test_pending_approvals_only_inactive_viewers(self, client: TestClient, admin_headers, db_session):
        """Pending approvals are inactive viewer accounts from public signup."""
        pending = User(
            email="pending-approval@werco.com",
            employee_id="EMP-PENDING-001",
            first_name="Pending",
            last_name="Approval",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.VIEWER,
            is_active=False,
            company_id=1,
        )
        inactive_operator = User(
            email="inactive-operator@werco.com",
            employee_id="EMP-INACTIVE-OP",
            first_name="Inactive",
            last_name="Operator",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.OPERATOR,
            is_active=False,
            company_id=1,
        )
        db_session.add_all([pending, inactive_operator])
        db_session.commit()

        response = client.get("/api/v1/users/pending-approvals", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [user["email"] for user in data] == ["pending-approval@werco.com"]

        summary_response = client.get("/api/v1/users/pending-approvals/summary", headers=admin_headers)
        assert summary_response.status_code == status.HTTP_200_OK
        assert summary_response.json()["count"] == 1

    def test_approve_pending_user_assigns_role_and_activates(self, client: TestClient, admin_headers, db_session):
        """Admin can approve a self-registered user in one action."""
        pending = User(
            email="approve-me@werco.com",
            employee_id="EMP-APPROVE-001",
            first_name="Approve",
            last_name="Me",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.VIEWER,
            is_active=False,
            company_id=1,
        )
        db_session.add(pending)
        db_session.commit()
        db_session.refresh(pending)

        response = client.post(
            f"/api/v1/users/{pending.id}/approve",
            headers=admin_headers,
            json={"role": "quality", "department": "Quality"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["is_active"] is True
        assert data["role"] == "quality"
        assert data["department"] == "Quality"

    def test_approve_pending_user_forbidden_for_manager(self, client: TestClient, manager_headers, db_session):
        """Managers can view users but cannot approve pending accounts."""
        pending = User(
            email="manager-cannot-approve@werco.com",
            employee_id="EMP-MGR-NOAPPROVE",
            first_name="No",
            last_name="Approve",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.VIEWER,
            is_active=False,
            company_id=1,
        )
        db_session.add(pending)
        db_session.commit()
        db_session.refresh(pending)

        response = client.post(
            f"/api/v1/users/{pending.id}/approve",
            headers=manager_headers,
            json={"role": "operator"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_get_user_by_id(self, client: TestClient, admin_headers, created_user):
        """Test getting user by ID."""
        response = client.get(f"/api/v1/users/{created_user['id']}", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == created_user["id"]

    def test_get_nonexistent_user(self, client: TestClient, admin_headers):
        """Test getting non-existent user returns 404."""
        response = client.get("/api/v1/users/99999", headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_user_as_admin(self, client: TestClient, admin_headers, created_user):
        """Test admin can update users."""
        update_data = {"first_name": "Updated", "department": "Quality", "version": created_user.get("version", 0)}
        response = client.put(f"/api/v1/users/{created_user['id']}", headers=admin_headers, json=update_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["first_name"] == "Updated"

    def test_update_own_profile(self, client: TestClient, auth_headers):
        """Test user can update own profile."""
        # First get current user
        me_response = client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_response.json()["id"]
        version = me_response.json().get("version", 0)

        update_data = {"department": "Engineering", "version": version}
        response = client.put(f"/api/v1/users/{user_id}", headers=auth_headers, json=update_data)
        # User may or may not be able to update themselves depending on implementation
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_403_FORBIDDEN]

    def test_import_users_csv_success(self, client: TestClient, admin_headers):
        """Test admin can import multiple users from CSV."""
        csv_content = (
            "employee_id,first_name,last_name,role,department\n"
            "EMP-CSV-001,Jane,Doe,operator,Fabrication\n"
            "EMP-CSV-002,John,Smith,supervisor,Assembly\n"
        )
        response = client.post(
            "/api/v1/users/import-csv",
            headers=admin_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
            data={"default_password": "SecureP@ss123!"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_rows"] == 2
        assert data["created_count"] == 2
        assert data["skipped_count"] == 0
        assert len(data["created_ids"]) == 2
        assert data["errors"] == []

        users_response = client.get("/api/v1/users/", headers=admin_headers)
        assert users_response.status_code == status.HTTP_200_OK
        users = users_response.json()
        imported_user = next(u for u in users if u["employee_id"] == "EMP-CSV-001")
        assert imported_user["email"].endswith("@users.werco.com")

    def test_import_users_csv_partial_success_with_errors(
        self,
        client: TestClient,
        admin_headers,
    ):
        """Test CSV import creates valid rows and skips invalid rows."""
        csv_content = (
            "employee_id,first_name,last_name,email,password,role\n"
            "EMP-ADMIN-001,Dup,User,dup@werco.com,SecureP@ss123!,operator\n"
            "EMP-CSV-003,No,Password,nopassword@werco.com,,operator\n"
            "EMP-CSV-004,Bad,Role,badrole@werco.com,SecureP@ss123!,not-a-role\n"
            "EMP-CSV-005,Needs,Password,manager@werco.com,,manager\n"
            "EMP-CSV-006,Valid,User,valid@werco.com,SecureP@ss123!,operator\n"
        )
        response = client.post(
            "/api/v1/users/import-csv",
            headers=admin_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_rows"] == 5
        assert data["created_count"] == 2
        assert data["skipped_count"] == 3
        assert len(data["errors"]) == 3

    def test_import_users_csv_operator_without_password_allowed(self, client: TestClient, admin_headers):
        """Operators can be imported without a password for employee-ID login."""
        csv_content = "employee_id,first_name,last_name,role\n" "EMP-CSV-777,Floor,Operator,operator\n"
        response = client.post(
            "/api/v1/users/import-csv",
            headers=admin_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total_rows"] == 1
        assert data["created_count"] == 1
        assert data["skipped_count"] == 0

    def test_import_users_csv_forbidden_for_manager(self, client: TestClient, manager_headers):
        """Test non-admin cannot import CSV users."""
        csv_content = "employee_id,first_name,last_name\nEMP-CSV-900,Jane,Doe\n"
        response = client.post(
            "/api/v1/users/import-csv",
            headers=manager_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
            data={"default_password": "SecureP@ss123!"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_import_users_csv_rejects_platform_admin_role(self, client: TestClient, admin_headers, db_session):
        """A company admin must not be able to mint a cross-company platform
        admin from a spreadsheet row — rejected as a row-level error."""
        csv_content = (
            "employee_id,first_name,last_name,password,role\n"
            "EMP-CSV-PA1,Eve,Escalator,SecureP@ss123!,platform_admin\n"
            "EMP-CSV-PA2,Norm,Operator,SecureP@ss123!,operator\n"
        )
        response = client.post(
            "/api/v1/users/import-csv",
            headers=admin_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["created_count"] == 1  # the operator row still imports
        assert len(data["errors"]) == 1
        assert data["errors"][0]["reason"] == "role 'platform_admin' cannot be assigned via import"
        assert db_session.query(User).filter_by(employee_id="EMP-CSV-PA1").count() == 0
        assert db_session.query(User).filter_by(employee_id="EMP-CSV-PA2").count() == 1

    def test_import_users_csv_invalid_role_error_does_not_advertise_platform_admin(
        self, client: TestClient, admin_headers
    ):
        """The 'valid roles' hint must not list platform_admin."""
        csv_content = "employee_id,first_name,last_name,password,role\n" "EMP-CSV-BR1,Bad,Role,SecureP@ss123!,wizard\n"
        response = client.post(
            "/api/v1/users/import-csv",
            headers=admin_headers,
            files={"file": ("users.csv", csv_content, "text/csv")},
        )
        assert response.status_code == status.HTTP_200_OK
        reason = response.json()["errors"][0]["reason"]
        assert "Invalid role" in reason
        assert "platform_admin" not in reason


@pytest.mark.api
class TestUserRoles:
    """Test role-based access control."""

    def test_admin_has_full_access(self, client: TestClient, admin_headers):
        """Test admin role has full access."""
        response = client.get("/api/v1/users/", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK

    def test_manager_can_view_users(self, client: TestClient, manager_headers):
        """Test manager can view users."""
        response = client.get("/api/v1/users/", headers=manager_headers)
        assert response.status_code == status.HTTP_200_OK

    def test_role_in_token(self, client: TestClient, auth_headers):
        """Test user role is included in response."""
        response = client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "role" in data


@pytest.mark.api
class TestUserValidation:
    """Test user input validation."""

    def test_invalid_email_format(self, client: TestClient, admin_headers, fake_data):
        """Test registration with invalid email format."""
        user_data = {
            "email": "invalid-email",
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": "Test",
            "last_name": "User",
            "password": "SecureP@ss123!",
            "role": "operator",
        }
        response = client.post("/api/v1/auth/register", headers=admin_headers, json=user_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_weak_password_rejected(self, client: TestClient, admin_headers, fake_data):
        """Test weak password is rejected."""
        user_data = {
            "email": fake_data.email(),
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": "Test",
            "last_name": "User",
            "password": "weak",  # Too short, no complexity
            "role": "operator",
        }
        response = client.post("/api/v1/auth/register", headers=admin_headers, json=user_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_invalid_role_rejected(self, client: TestClient, admin_headers, fake_data):
        """Test invalid role is rejected."""
        user_data = {
            "email": fake_data.email(),
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": "Test",
            "last_name": "User",
            "password": "SecureP@ss123!",
            "role": "invalid_role",
        }
        response = client.post("/api/v1/auth/register", headers=admin_headers, json=user_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def _valid_user_payload(**overrides) -> dict:
    """A schema-valid POST /users/ body; override any field per test."""
    payload = {
        "email": "new-user@werco.com",
        "employee_id": "EMP-NEW-001",
        "first_name": "New",
        "last_name": "User",
        "password": "SecureP@ss123!",
        "role": "operator",
    }
    payload.update(overrides)
    return payload


@pytest.mark.api
class TestUserManagementRBAC:
    """RBAC gating on the write endpoints (create/update are Admin-only).

    These pin the core coverage gap: managers can VIEW users (users:view) but
    must not be able to CREATE or UPDATE them, and supervisors have no users:*
    access at all. The role fixtures are explicit — the suite has a wart where
    ``auth_headers``/``manager_headers`` both resolve to the same MANAGER user,
    so we never lean on ``auth_headers`` for a low-privilege assertion.
    """

    # --- Manager: read allowed, writes forbidden ---------------------------

    def test_create_user_forbidden_for_manager(self, client: TestClient, manager_headers):
        """A manager cannot create users (POST /users/ is Admin-only)."""
        response = client.post(
            "/api/v1/users/",
            headers=manager_headers,
            json=_valid_user_payload(email="mgr-create@werco.com", employee_id="EMP-MGR-CR"),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_user_forbidden_for_manager(self, client: TestClient, manager_headers, created_user):
        """A manager cannot update users (PUT /users/{id} is Admin-only)."""
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=manager_headers,
            json={"first_name": "Renamed"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    # --- Supervisor: no users:* at all -------------------------------------

    def test_list_users_forbidden_for_supervisor(self, client: TestClient, supervisor_headers):
        """A supervisor cannot list users (GET /users/ is Admin/Manager-only)."""
        response = client.get("/api/v1/users/", headers=supervisor_headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_user_forbidden_for_supervisor(self, client: TestClient, supervisor_headers):
        """A supervisor cannot create users."""
        response = client.post(
            "/api/v1/users/",
            headers=supervisor_headers,
            json=_valid_user_payload(email="sup-create@werco.com", employee_id="EMP-SUP-CR"),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_user_forbidden_for_supervisor(self, client: TestClient, supervisor_headers, created_user):
        """A supervisor cannot update users."""
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=supervisor_headers,
            json={"first_name": "Renamed"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    # --- Admin: the happy path still works ----------------------------------

    def test_create_user_as_admin_succeeds(self, client: TestClient, admin_headers):
        """An admin can create a normal (operator) user."""
        response = client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json=_valid_user_payload(email="admin-created@werco.com", employee_id="EMP-ADM-CR"),
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        data = response.json()
        assert data["email"] == "admin-created@werco.com"
        assert data["role"] == "operator"
        assert "hashed_password" not in data

    def test_admin_can_create_admin_role(self, client: TestClient, admin_headers):
        """Assigning role=admin is legitimate per the RBAC matrix (only
        platform_admin is blocked from this tenant-scoped path)."""
        response = client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json=_valid_user_payload(email="new-admin@werco.com", employee_id="EMP-ADM-NEW", role="admin"),
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["role"] == "admin"

    def test_update_user_as_admin_succeeds(self, client: TestClient, admin_headers, created_user):
        """An admin can update another user's ordinary fields."""
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=admin_headers,
            json={"first_name": "Renamed", "department": "Quality"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        data = response.json()
        assert data["first_name"] == "Renamed"
        assert data["department"] == "Quality"


@pytest.mark.api
class TestUserRoleGuards:
    """platform_admin assignment guard + self role-escalation guard."""

    def test_create_platform_admin_rejected(self, client: TestClient, admin_headers):
        """A tenant admin cannot mint a cross-company platform_admin via create."""
        response = client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json=_valid_user_payload(email="pa@werco.com", employee_id="EMP-PA-1", role="platform_admin"),
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Platform admin role cannot be assigned"

    def test_update_to_platform_admin_rejected(self, client: TestClient, admin_headers, created_user):
        """A tenant admin cannot promote anyone to platform_admin via update."""
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=admin_headers,
            json={"role": "platform_admin"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Platform admin role cannot be assigned"

    def test_admin_cannot_change_own_role(self, client: TestClient, admin_headers, admin_user):
        """Self role-escalation guard: an admin cannot change their OWN role."""
        response = client.put(
            f"/api/v1/users/{admin_user.id}",
            headers=admin_headers,
            json={"role": "manager"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "You cannot change your own role"

    def test_admin_can_update_own_other_fields(self, client: TestClient, admin_headers, admin_user):
        """Editing one's OWN name/other fields (no role change) stays allowed."""
        response = client.put(
            f"/api/v1/users/{admin_user.id}",
            headers=admin_headers,
            json={"first_name": "SelfEdited"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        data = response.json()
        assert data["first_name"] == "SelfEdited"
        assert data["role"] == "admin"  # unchanged

    def test_admin_resubmitting_own_same_role_is_allowed(self, client: TestClient, admin_headers, admin_user):
        """The self-role guard only trips on an ACTUAL change — re-sending the
        admin's current role alongside another edit is fine."""
        response = client.put(
            f"/api/v1/users/{admin_user.id}",
            headers=admin_headers,
            json={"role": "admin", "first_name": "StillAdmin"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["first_name"] == "StillAdmin"

    def test_admin_can_change_another_users_role(self, client: TestClient, admin_headers, created_user):
        """An admin CAN change ANOTHER user's role (operator -> manager)."""
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=admin_headers,
            json={"role": "manager"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["role"] == "manager"


@pytest.mark.api
class TestUserAuditLogging:
    """State changes on users must land in the tamper-evident audit log."""

    def test_create_user_emits_create_audit(self, client: TestClient, admin_headers, db_session):
        """POST /users/ emits a COMMITTED CREATE audit row for the new user,
        tagged with the caller's company. Secrets are never logged."""
        response = client.post(
            "/api/v1/users/",
            headers=admin_headers,
            json=_valid_user_payload(email="audited-create@werco.com", employee_id="EMP-AUD-CR"),
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        new_id = response.json()["id"]

        rows = _committed_user_audit_rows(db_session, resource_id=new_id, action="CREATE")
        assert len(rows) == 1, "expected exactly one committed CREATE audit row for the new user"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.resource_id == new_id
        assert row.company_id == 1
        # create deliberately passes NO new_values, so new_values is empty here.
        # (A ``"hashed_password" not in new_values`` check would be vacuous — the
        # real secret-exclusion coverage lives in the update/reset tests below,
        # where values ARE passed.)
        assert row.new_values in (None, {})

    def test_update_user_emits_update_audit(self, client: TestClient, admin_headers, created_user, db_session):
        """PUT /users/{id} that changes a field emits a COMMITTED UPDATE audit row.

        update passes the full model as new_values, so this is where the secret
        -exclusion guarantee is actually meaningful: hashed_password must not
        appear in old_values / new_values / the computed changes diff.
        """
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=admin_headers,
            json={"first_name": "AuditedRename"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_user_audit_rows(db_session, resource_id=created_user["id"], action="UPDATE")
        assert len(rows) >= 1, "expected at least one committed UPDATE audit row for the user"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.company_id == 1
        # The model carries hashed_password, yet _model_to_dict drops it on BOTH
        # sides — prove it never reaches old_values/new_values or the changes diff.
        assert "hashed_password" not in (row.old_values or {})
        assert "hashed_password" not in (row.new_values or {})
        assert "hashed_password" not in (row.extra_data or {}).get("changes", {})
        # first_name IS the change we made.
        assert "first_name" in (row.extra_data or {}).get("changes", {})

    def test_approve_user_emits_audit(self, client: TestClient, admin_headers, db_session):
        """POST /users/{id}/approve emits a committed 'approve' audit row that
        captures the role grant + activation transition."""
        pending = User(
            email="approve-audit@werco.com",
            employee_id="EMP-APV-AUD",
            first_name="Approve",
            last_name="Audit",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.VIEWER,
            is_active=False,
            company_id=1,
        )
        db_session.add(pending)
        db_session.commit()
        db_session.refresh(pending)

        response = client.post(
            f"/api/v1/users/{pending.id}/approve",
            headers=admin_headers,
            json={"role": "operator"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        # log_update records the custom verb uppercased.
        rows = _committed_user_audit_rows(db_session, resource_id=pending.id, action="APPROVE")
        assert len(rows) == 1, "expected exactly one committed approve audit row"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.company_id == 1
        changes = (row.extra_data or {}).get("changes", {})
        # The role grant (viewer -> operator) and activation (False -> True) are both captured.
        assert "role" in changes and changes["role"]["old"] != changes["role"]["new"]
        assert "is_active" in changes and changes["is_active"]["old"] != changes["is_active"]["new"]
        # No secret on either side of the diff.
        assert "hashed_password" not in (row.old_values or {})
        assert "hashed_password" not in (row.new_values or {})

    def test_reset_password_emits_audit_without_any_secret(
        self, client: TestClient, admin_headers, created_user, db_session
    ):
        """POST /users/{id}/reset-password emits a committed PASSWORD_CHANGE row,
        and neither the new password nor its hash appears ANYWHERE in the row's
        value fields (this endpoint deliberately passes no values, so the
        assertion is meaningful, not vacuous)."""
        new_password = "BrandNewP@ssw0rd!42"
        response = client.post(
            f"/api/v1/users/{created_user['id']}/reset-password",
            headers=admin_headers,
            json={"new_password": new_password},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_user_audit_rows(db_session, resource_id=created_user["id"], action="PASSWORD_CHANGE")
        assert len(rows) == 1, "expected exactly one committed PASSWORD_CHANGE audit row"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.company_id == 1

        # The stored hash is durable after the endpoint's commit (survives the
        # rollback in the helper); grab it to assert it is NOT in the audit row.
        stored_hash = db_session.query(User).filter(User.id == created_user["id"]).first().hashed_password

        # old_values / new_values / changes / extra_data must ALL be clear of the
        # plaintext password AND its hash.
        assert row.old_values in (None, {})
        assert row.new_values in (None, {})
        assert "changes" not in (row.extra_data or {})
        value_blob = json.dumps({"old": row.old_values, "new": row.new_values, "extra": row.extra_data})
        assert new_password not in value_blob
        assert stored_hash not in value_blob
        assert "hashed_password" not in value_blob

    def test_deactivate_user_emits_status_change_audit(
        self, client: TestClient, admin_headers, created_user, db_session
    ):
        """DELETE /users/{id} emits a committed STATUS_CHANGE row: active -> inactive."""
        response = client.delete(f"/api/v1/users/{created_user['id']}", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_user_audit_rows(db_session, resource_id=created_user["id"], action="STATUS_CHANGE")
        assert len(rows) == 1, "expected exactly one committed STATUS_CHANGE audit row"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.company_id == 1
        assert row.old_values == {"status": "active"}
        assert row.new_values == {"status": "inactive"}

    def test_activate_user_emits_status_change_audit(
        self, client: TestClient, admin_headers, inactive_user, db_session
    ):
        """POST /users/{id}/activate emits a committed STATUS_CHANGE row: inactive -> active."""
        response = client.post(f"/api/v1/users/{inactive_user.id}/activate", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_user_audit_rows(db_session, resource_id=inactive_user.id, action="STATUS_CHANGE")
        assert len(rows) == 1, "expected exactly one committed STATUS_CHANGE audit row"
        row = rows[0]
        assert row.resource_type == "user"
        assert row.company_id == 1
        assert row.old_values == {"status": "inactive"}
        assert row.new_values == {"status": "active"}
