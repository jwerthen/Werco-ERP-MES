"""Password-strength coverage for company creation (PR fix/ia-password-gaps).

The first-admin password on the company-creation path must meet the SAME
canonical strength policy (``schemas.user.validate_password_strength``) as
``/auth/register`` -- otherwise a weak first-admin credential slips in through a
door the per-user create/reset paths already guard.

Updated 2026-07-29: that canonical policy dropped its four character-class rules
in favor of length + an expanded blocklist (NIST SP 800-63B 5.1.1.2). Both
directions are pinned: ``REJECTED_PASSWORDS`` (blocklist, incl. the new entries)
and ``NEWLY_ACCEPTED_PASSWORDS`` (the composition relaxation).

Updated 2026-11-23: the unauthenticated ``POST /api/v1/companies/register``
self-registration route and its ``CompanyRegister`` schema were DELETED (owner
decision: Werco is the only company on this system). The one remaining
company-creation door is ``POST /api/v1/platform/companies`` -- platform-admin-only
(``CompanyCreate.admin_password``), which previously had NO complexity validator at
all (any 12-char password passed). ``TestCompanySelfRegistrationRemoved`` pins that
the deleted door stays shut.

Direct-model ``ValidationError`` unit tests back the HTTP tests so the schema
contract is pinned even independent of routing/auth.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.schemas.company as company_schemas
from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import User, UserRole
from app.schemas.company import CompanyCreate

# A password that breaks EXACTLY the common-substring rule: >= 12 chars, but
# lower-cases to contain "password".
COMMON_SUBSTRING_PASSWORD = "Password1234!"

# A fully compliant password: >= 12 chars and no blocklisted substring.
STRONG_PASSWORD = "Str0ng&Unique!Pass"

# Passwords the company-creation paths must still REFUSE after the 2026-07-29
# policy relaxation.
#
# This dict used to be WEAK_COMPLEXITY_PASSWORDS and held one violator per
# character-class rule (missing upper / lower / digit / special). Those four rules
# were removed -- four of its five entries became VALID passwords -- so it is
# rebuilt around the rule that survived and was expanded: the blocklist. Every
# entry is >= 12 characters, so the blocklist is the sole cause of the 422 and the
# Field(min_length=12) guard never short-circuits the strength validator.
REJECTED_PASSWORDS = {
    "common_substring": COMMON_SUBSTRING_PASSWORD,
    "expanded_top100_word": "Thundering-dragon-42",
    "expanded_local_shop_name": "Quiet-werco-morning",
    "expanded_keyboard_walk": "Silver-qazwsx-lantern",
    "expanded_leetspeak": "Copper-passw0rd-vault",
    "blocklist_is_case_insensitive": "Thundering-DRAGON-42",
}

# The companion set: values these paths used to refuse for missing a character
# class and must now ACCEPT. Pinned so the relaxation is verified in both
# directions -- deleting the reject-tests alone would leave the accept side
# unproven, and a quietly reinstated composition rule would go unnoticed.
NEWLY_ACCEPTED_PASSWORDS = {
    "no_digit": "Zephyr!Quills",
    "no_special_character": "Zephyr9Quills",
    "no_uppercase": "zephyr9!quills",
    "no_lowercase": "ZEPHYR9!QUILLS",
    "passphrase_letters_and_spaces_only": "correct horse battery staple",
}


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
class TestCompanySelfRegistrationRemoved:
    """The unauthenticated company self-registration door is gone and stays gone.

    It used to mint a whole tenant plus an active Admin with live tokens from one
    anonymous call. Werco is the only company on this system (owner decision
    2026-11-23), so the route, its schema and its rate-limit entry were deleted.
    """

    def test_register_route_is_not_mounted_and_writes_nothing(self, client: TestClient, db_session):
        companies_before = db_session.query(Company).count()
        users_before = db_session.query(User).count()
        response = client.post(
            "/api/v1/companies/register",
            json={
                "company_name": "Acme Precision Machining",
                "admin_email": "founder@acme-precision.com",
                "admin_first_name": "Ada",
                "admin_last_name": "Founder",
                "admin_password": STRONG_PASSWORD,
            },
        )
        assert response.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_405_METHOD_NOT_ALLOWED), response.text
        assert db_session.query(Company).count() == companies_before
        assert db_session.query(User).count() == users_before

    def test_register_route_is_absent_from_openapi(self, client: TestClient):
        paths = client.app.openapi()["paths"]
        assert "/api/v1/companies/register" not in paths

    def test_self_registration_schema_is_deleted(self):
        assert not hasattr(company_schemas, "CompanyRegister")


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
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert db_session.query(Company).filter(Company.name == "Beta Aerospace Fabrication").count() == 0

    def test_platform_create_blocklisted_password_rejected(self, client: TestClient, platform_admin_headers):
        """A password hitting an EXPANDED blocklist entry is 422 on /platform/companies.

        (This case previously used ``"Zephyr9Quills"`` to exercise the
        missing-special-character rule; that rule is gone and the string is now a
        valid password -- see ``test_platform_create_newly_accepted_password``.)
        """
        response = client.post(
            "/api/v1/platform/companies",
            headers=platform_admin_headers,
            json=_platform_create_payload(admin_password="Thundering-dragon-42"),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

    def test_platform_create_newly_accepted_password(self, client: TestClient, platform_admin_headers, db_session):
        """``"Zephyr9Quills"`` (no special character) now creates the company."""
        response = client.post(
            "/api/v1/platform/companies",
            headers=platform_admin_headers,
            json=_platform_create_payload(
                admin_password="Zephyr9Quills",
                name="Gamma Turbine Works",
                admin_email="admin@gamma-turbine.com",
            ),
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert db_session.query(Company).filter(Company.name == "Gamma Turbine Works").count() == 1

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
    """Direct model-level coverage: ``CompanyCreate`` runs ``admin_password`` through the
    canonical strength policy, independent of routing/auth."""

    def test_company_create_rejects_common_substring(self):
        with pytest.raises(ValidationError, match="common word or pattern"):
            CompanyCreate(**_platform_create_payload(admin_password=COMMON_SUBSTRING_PASSWORD))

    @pytest.mark.parametrize("label,password", sorted(REJECTED_PASSWORDS.items()))
    def test_schema_rejects_blocklisted(self, label, password):
        """The company schema runs the SAME blocklist as the per-user paths."""
        with pytest.raises(ValidationError, match="common word or pattern"):
            CompanyCreate(**_platform_create_payload(admin_password=password))

    @pytest.mark.parametrize("label,password", sorted(NEWLY_ACCEPTED_PASSWORDS.items()))
    def test_schema_accepts_newly_valid(self, label, password):
        """The schema keeps no private copy of the retired composition rules."""
        assert CompanyCreate(**_platform_create_payload(admin_password=password)).admin_password == password

    def test_company_create_accepts_strong(self):
        model = CompanyCreate(**_platform_create_payload())
        assert model.admin_password == STRONG_PASSWORD
