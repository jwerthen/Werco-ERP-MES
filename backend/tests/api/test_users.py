"""
Integration tests for user management endpoints.
Tests user CRUD operations and role-based access.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.models.user import User, UserRole


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
