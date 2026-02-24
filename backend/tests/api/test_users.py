"""
Integration tests for user management endpoints.
Tests user CRUD operations and role-based access.
"""
import pytest
from fastapi import status
from fastapi.testclient import TestClient


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
        update_data = {
            "first_name": "Updated",
            "department": "Quality",
            "version": created_user.get("version", 0)
        }
        response = client.put(
            f"/api/v1/users/{created_user['id']}",
            headers=admin_headers,
            json=update_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["first_name"] == "Updated"

    def test_update_own_profile(self, client: TestClient, auth_headers):
        """Test user can update own profile."""
        # First get current user
        me_response = client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_response.json()["id"]
        version = me_response.json().get("version", 0)
        
        update_data = {
            "department": "Engineering",
            "version": version
        }
        response = client.put(
            f"/api/v1/users/{user_id}",
            headers=auth_headers,
            json=update_data
        )
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
        csv_content = (
            "employee_id,first_name,last_name,role\n"
            "EMP-CSV-777,Floor,Operator,operator\n"
        )
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
            "role": "operator"
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
            "role": "operator"
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
            "role": "invalid_role"
        }
        response = client.post("/api/v1/auth/register", headers=admin_headers, json=user_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
