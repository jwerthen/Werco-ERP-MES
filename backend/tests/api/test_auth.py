"""
Integration tests for authentication endpoints.
Tests login, logout, token refresh, and account security features.
"""
import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.models.user import User, UserRole


@pytest.mark.api
class TestAuthLogin:
    """Test authentication login endpoint."""

    def test_login_success(self, client: TestClient, test_user, test_user_credentials):
        """Test successful login with valid credentials."""
        response = client.post(
            "/api/v1/auth/login",
            data={
                "username": test_user_credentials["email"],
                "password": test_user_credentials["password"]
            }
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert "user" in data
        assert data["user"]["email"] == test_user_credentials["email"]

    def test_login_invalid_email(self, client: TestClient):
        """Test login with non-existent email."""
        response = client.post(
            "/api/v1/auth/login",
            data={
                "username": "nonexistent@example.com",
                "password": "anypassword123"
            }
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Invalid email or password" in response.json()["detail"]

    def test_login_invalid_password(self, client: TestClient, test_user, test_user_credentials):
        """Test login with wrong password."""
        response = client.post(
            "/api/v1/auth/login",
            data={
                "username": test_user_credentials["email"],
                "password": "wrongpassword123"
            }
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Invalid email or password" in response.json()["detail"]

    def test_login_inactive_user(self, client: TestClient, inactive_user, inactive_user_credentials):
        """Test login with inactive user account."""
        response = client.post(
            "/api/v1/auth/login",
            data={
                "username": inactive_user_credentials["email"],
                "password": inactive_user_credentials["password"]
            }
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "disabled" in response.json()["detail"].lower()

    def test_login_returns_user_info(self, client: TestClient, test_user, test_user_credentials):
        """Test that login response includes user information."""
        response = client.post(
            "/api/v1/auth/login",
            data={
                "username": test_user_credentials["email"],
                "password": test_user_credentials["password"]
            }
        )
        assert response.status_code == status.HTTP_200_OK
        user_data = response.json()["user"]
        assert "id" in user_data
        assert "email" in user_data
        assert "role" in user_data
        assert "hashed_password" not in user_data  # Security check


@pytest.mark.api
class TestAuthTokenRefresh:
    """Test token refresh functionality."""

    def test_refresh_token_success(self, client: TestClient, test_user, test_user_credentials):
        """Test successful token refresh."""
        # First login to get tokens
        login_response = client.post(
            "/api/v1/auth/login",
            data={
                "username": test_user_credentials["email"],
                "password": test_user_credentials["password"]
            }
        )
        refresh_token = login_response.json()["refresh_token"]
        
        # Use refresh token to get new access token
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data  # Token rotation

    def test_refresh_with_invalid_token(self, client: TestClient):
        """Test refresh with invalid token."""
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid-token-here"}
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_refresh_with_expired_token(self, client: TestClient):
        """Test refresh with expired token."""
        # This would require creating an expired token
        # For now, just test with malformed token
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.expired"}
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.api
class TestAuthLogout:
    """Test logout functionality."""

    def test_logout_success(self, client: TestClient, auth_headers):
        """Test successful logout."""
        response = client.post("/api/v1/auth/logout", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        assert "logged out" in response.json()["message"].lower()

    def test_logout_without_auth(self, client: TestClient):
        """Test logout without authentication."""
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.api
class TestAuthRegister:
    """Test user registration (admin only)."""

    def test_register_as_admin(self, client: TestClient, admin_headers, fake_data):
        """Test admin can register new users."""
        new_user_data = {
            "email": fake_data.email(),
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": fake_data.first_name(),
            "last_name": fake_data.last_name(),
            "password": "SecureP@ss123!",
            "role": "operator"
        }
        response = client.post(
            "/api/v1/auth/register",
            headers=admin_headers,
            json=new_user_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["email"] == new_user_data["email"]
        assert data["employee_id"] == new_user_data["employee_id"]

    def test_register_without_auth(self, client: TestClient, fake_data):
        """Test registration without authentication fails."""
        new_user_data = {
            "email": fake_data.email(),
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": fake_data.first_name(),
            "last_name": fake_data.last_name(),
            "password": "SecureP@ss123!",
            "role": "operator"
        }
        response = client.post("/api/v1/auth/register", json=new_user_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_register_as_non_admin(self, client: TestClient, auth_headers, fake_data):
        """Test non-admin cannot register users."""
        new_user_data = {
            "email": fake_data.email(),
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": fake_data.first_name(),
            "last_name": fake_data.last_name(),
            "password": "SecureP@ss123!",
            "role": "operator"
        }
        response = client.post(
            "/api/v1/auth/register",
            headers=auth_headers,
            json=new_user_data
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_register_duplicate_email(self, client: TestClient, admin_headers, test_user, test_user_credentials, fake_data):
        """Test registration with existing email fails."""
        new_user_data = {
            "email": test_user_credentials["email"],
            "employee_id": f"EMP-{fake_data.pyint(min_value=1000, max_value=9999)}",
            "first_name": fake_data.first_name(),
            "last_name": fake_data.last_name(),
            "password": "SecureP@ss123!",
            "role": "operator"
        }
        response = client.post(
            "/api/v1/auth/register",
            headers=admin_headers,
            json=new_user_data
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.api
class TestAuthSecurity:
    """Test authentication security features."""

    def test_protected_endpoint_without_token(self, client: TestClient):
        """Test accessing protected endpoint without token."""
        response = client.get("/api/v1/users/me")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_protected_endpoint_with_invalid_token(self, client: TestClient):
        """Test accessing protected endpoint with invalid token."""
        headers = {"Authorization": "Bearer invalid-token"}
        response = client.get("/api/v1/users/me", headers=headers)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_protected_endpoint_with_valid_token(self, client: TestClient, auth_headers):
        """Test accessing protected endpoint with valid token."""
        response = client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.api
class TestEmployeeIdLogin:
    """Test employee-ID based authentication paths."""

    def test_employee_login_with_exact_employee_id(self, client: TestClient, test_user):
        """Full employee IDs should be accepted."""
        response = client.post(
            "/api/v1/auth/employee-login",
            json={"employee_id": test_user.employee_id},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["id"] == test_user.id

    def test_employee_login_with_4_digit_badge_id(self, client: TestClient, test_user):
        """4-digit badge IDs should match normalized employee IDs."""
        response = client.post(
            "/api/v1/auth/employee-login",
            json={"employee_id": "0001"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["id"] == test_user.id

    def test_employee_login_with_last4_digits_of_long_id(self, client: TestClient, db_session):
        """Numeric employee IDs longer than 4 should be reachable by last 4 digits."""
        user = User(
            email="badge-login@werco.com",
            employee_id="12345",
            first_name="Badge",
            last_name="Login",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.OPERATOR,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        response = client.post(
            "/api/v1/auth/employee-login",
            json={"employee_id": "2345"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["id"] == user.id

    def test_employee_login_repairs_legacy_local_email(self, client: TestClient, db_session):
        """Legacy @werco.local users should be auto-repaired and still login."""
        user = User(
            email="emp-339@werco.local",
            employee_id="339",
            first_name="Legacy",
            last_name="Local",
            hashed_password=get_password_hash("SecureP@ss123!"),
            role=UserRole.OPERATOR,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        response = client.post(
            "/api/v1/auth/employee-login",
            json={"employee_id": "0339"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["user"]["id"] == user.id
        assert data["user"]["email"].endswith("@users.werco.com")
