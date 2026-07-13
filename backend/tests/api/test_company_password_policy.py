"""Password-strength coverage for the company-creation schemas (PR fix/ia-password-gaps).

The first-admin password on both company-creation paths must meet the SAME
canonical AS9100D/CMMC strength policy (``schemas.user.validate_password_strength``)
as ``/auth/register`` -- otherwise a weak first-admin credential slips in through a
door the per-user create/reset paths already guard.

Two paths are covered:

* ``POST /api/v1/companies/register`` -- **UNAUTHENTICATED** self-registration
  (``CompanyRegister.admin_password``). This is the key regression: the validator
  previously OMITTED the common-substring check, so ``"Password1234!"`` was accepted
  here even though ``/auth/register`` rejects it.
* ``POST /api/v1/platform/companies`` -- platform-admin-only company creation
  (``CompanyCreate.admin_password``), which previously had NO complexity validator
  at all (any 12-char password passed).

Direct-model ``ValidationError`` unit tests back the HTTP tests so the schema
contract is pinned even independent of routing/auth.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import User, UserRole
from app.schemas.company import CompanyCreate, CompanyRegister

# A password that breaks EXACTLY the common-substring rule: >= 12 chars, has an
# uppercase, lowercase, digit and special char, but lower-cases to contain
# "password". This is the string that USED to be accepted on /companies/register.
COMMON_SUBSTRING_PASSWORD = "Password1234!"

# A fully compliant password: >= 12 chars, upper + lower + digit + special, and no
# common weak substring.
STRONG_PASSWORD = "Str0ng&Unique!Pass"

# Each of these breaks exactly one complexity rule while staying >= 12 chars so the
# Field(min_length=12) guard doesn't short-circuit the strength validator.
WEAK_COMPLEXITY_PASSWORDS = {
    "missing_uppercase": "zephyr9!quills",
    "missing_lowercase": "ZEPHYR9!QUILLS",
    "missing_digit": "Zephyr!Quills",
    "missing_special": "Zephyr9Quills",
    "common_substring": COMMON_SUBSTRING_PASSWORD,
}


def _register_payload(**overrides) -> dict:
    """A schema-valid POST /companies/register body; override any field per test."""
    payload = {
        "company_name": "Acme Precision Machining",
        "admin_email": "founder@acme-precision.com",
        "admin_first_name": "Ada",
        "admin_last_name": "Founder",
        "admin_password": STRONG_PASSWORD,
    }
    payload.update(overrides)
    return payload


def _platform_create_payload(**overrides) -> dict:
    """A schema-valid POST /platform/companies body; override any field per test."""
    payload = {
        "name": "Beta Aerospace Fabrication",
        "admin_email": "admin@beta-aero.com",
        "admin_first_name": "Bea",
        "admin_last_name": "Admin",
        "admin_password": STRONG_PASSWORD,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def platform_admin_headers(db_session) -> dict:
    """Auth headers for a PLATFORM_ADMIN so the /platform/companies path is reachable."""
    user = User(
        email="platform-admin@werco.com",
        employee_id="EMP-PA-COMPANY",
        first_name="Platform",
        last_name="Admin",
        hashed_password="x",  # never used: we mint the token directly
        role=UserRole.PLATFORM_ADMIN,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.mark.api
class TestCompanyRegisterPasswordPolicy:
    """Unauthenticated self-registration must enforce the canonical strength policy."""

    def test_register_common_substring_password_rejected(self, client: TestClient, db_session):
        """KEY REGRESSION: a password containing a common substring
        (``"Password1234!"``) is now 422 on /companies/register -- it used to be
        accepted here because this path omitted the common-substring check. No
        company/user row is written."""
        response = client.post(
            "/api/v1/companies/register", json=_register_payload(admin_password=COMMON_SUBSTRING_PASSWORD)
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        # Nothing was persisted for the rejected registration.
        assert db_session.query(User).filter(User.email == "founder@acme-precision.com").count() == 0
        assert db_session.query(Company).filter(Company.name == "Acme Precision Machining").count() == 0

    @pytest.mark.parametrize("label,password", sorted(WEAK_COMPLEXITY_PASSWORDS.items()))
    def test_register_weak_complexity_password_rejected(self, client: TestClient, label, password):
        """Every single-rule complexity failure (no upper/lower/digit/special, or a
        common substring) is a 422 on /companies/register."""
        response = client.post("/api/v1/companies/register", json=_register_payload(admin_password=password))
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, f"{label}: {response.text}"

    def test_register_too_short_password_rejected(self, client: TestClient):
        """A password shorter than 12 chars is rejected (Field min_length -> 422)."""
        response = client.post("/api/v1/companies/register", json=_register_payload(admin_password="Ab1!xyz"))
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text

    def test_register_strong_password_accepted(self, client: TestClient, db_session):
        """A compliant first-admin password registers the company + admin (2xx) and
        the token response never echoes a password/hash."""
        response = client.post("/api/v1/companies/register", json=_register_payload())
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        # The nested user must not leak a secret.
        assert "hashed_password" not in body.get("user", {})
        assert "password" not in body.get("user", {})
        # The company + admin were actually created.
        assert db_session.query(User).filter(User.email == "founder@acme-precision.com").count() == 1
        assert db_session.query(Company).filter(Company.name == "Acme Precision Machining").count() == 1


@pytest.mark.api
class TestPlatformCompanyCreatePasswordPolicy:
    """Platform-admin company creation must enforce the same strength policy
    (previously it had none)."""

    def test_platform_create_common_substring_password_rejected(
        self, client: TestClient, platform_admin_headers, db_session
    ):
        """A common-substring password is 422 on /platform/companies and writes no row."""
        response = client.post(
            "/api/v1/platform/companies",
            headers=platform_admin_headers,
            json=_platform_create_payload(admin_password=COMMON_SUBSTRING_PASSWORD),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        assert db_session.query(Company).filter(Company.name == "Beta Aerospace Fabrication").count() == 0

    def test_platform_create_weak_password_rejected(self, client: TestClient, platform_admin_headers):
        """A missing-complexity password is 422 on /platform/companies."""
        response = client.post(
            "/api/v1/platform/companies",
            headers=platform_admin_headers,
            json=_platform_create_payload(admin_password="Zephyr9Quills"),  # no special char
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text

    def test_platform_create_strong_password_accepted(self, client: TestClient, platform_admin_headers, db_session):
        """A compliant password creates the company (2xx)."""
        response = client.post(
            "/api/v1/platform/companies",
            headers=platform_admin_headers,
            json=_platform_create_payload(),
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["name"] == "Beta Aerospace Fabrication"
        assert db_session.query(Company).filter(Company.name == "Beta Aerospace Fabrication").count() == 1


@pytest.mark.unit
class TestCompanySchemaPasswordValidators:
    """Direct model-level coverage: both schemas run ``admin_password`` through the
    canonical strength policy, independent of routing/auth."""

    def test_company_register_rejects_common_substring(self):
        with pytest.raises(ValidationError, match="common pattern"):
            CompanyRegister(**_register_payload(admin_password=COMMON_SUBSTRING_PASSWORD))

    def test_company_register_accepts_strong(self):
        model = CompanyRegister(**_register_payload())
        assert model.admin_password == STRONG_PASSWORD

    def test_company_create_rejects_common_substring(self):
        with pytest.raises(ValidationError, match="common pattern"):
            CompanyCreate(**_platform_create_payload(admin_password=COMMON_SUBSTRING_PASSWORD))

    def test_company_create_rejects_missing_special(self):
        with pytest.raises(ValidationError, match="special character"):
            CompanyCreate(**_platform_create_payload(admin_password="Zephyr9Quills"))

    def test_company_create_accepts_strong(self):
        model = CompanyCreate(**_platform_create_payload())
        assert model.admin_password == STRONG_PASSWORD
