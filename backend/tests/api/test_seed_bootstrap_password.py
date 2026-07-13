"""Coverage for the generated seed-database bootstrap password (PR fix/ia-password-gaps).

``POST /api/v1/admin/settings/seed-database`` no longer hands out the hardcoded
``admin123`` / ``password123`` credentials. Each seeded user now gets a distinct
runtime-generated password from ``_generate_bootstrap_password()`` that must satisfy
the canonical AS9100D/CMMC strength policy by construction. These unit tests pin
that helper directly (no HTTP round-trip needed): every generated value passes
``validate_password_strength`` unchanged, is unique per call, and is never one of
the old well-known weak strings.
"""

import pytest

from app.api.endpoints.admin_settings import _generate_bootstrap_password
from app.schemas.user import validate_password_strength

# The hardcoded credentials the seed endpoint used to mint. The generator must
# never reproduce any of these.
KNOWN_WEAK_STRINGS = {"admin123", "password123", "admin", "password", "changeme"}


@pytest.mark.unit
class TestBootstrapPasswordGeneration:
    def test_generated_password_passes_strength_policy(self):
        """A generated bootstrap password satisfies the canonical policy and is
        returned unchanged by the validator (no ``ValueError`` raised)."""
        password = _generate_bootstrap_password()
        assert validate_password_strength(password) == password

    def test_generated_password_is_not_a_known_weak_string(self):
        """The generator never emits one of the retired hardcoded credentials."""
        password = _generate_bootstrap_password()
        assert password not in KNOWN_WEAK_STRINGS
        # None of the retired weak strings appear as a substring either.
        lowered = password.lower()
        for weak in KNOWN_WEAK_STRINGS:
            assert weak not in lowered

    def test_generated_passwords_are_unique_per_call(self):
        """Each call yields a distinct secret (per-user credentials, not a shared one)."""
        passwords = {_generate_bootstrap_password() for _ in range(20)}
        assert len(passwords) == 20
