"""The test suite's reduced bcrypt work factor must never reach production.

``tests/conftest.py`` rebinds ``app.core.security.pwd_context`` to a context built
with ``bcrypt__rounds=4`` so the suite stops paying ~208 ms per hash/verify. That is
safe *because it lives in the test package* -- the app package still builds its
context with no rounds argument and therefore keeps passlib's default of 12.

The whole arrangement rests on that one distinction, and nothing else in the repo
enforces it. Someone chasing a slow login endpoint, or copying the conftest line
while "cleaning up duplication", could move the override into ``app/core/security.py``
and every test in the suite would still pass -- while production silently dropped to
a cost factor roughly 256x cheaper to brute-force. This file is the tripwire for that.

Deliberately asserted against the SOURCE and against a freshly-built context rather
than against the live ``pwd_context``: conftest has already rebound the live one by
the time any test runs, so reading it would only re-measure the test override.
"""

import re
from pathlib import Path

import pytest
from passlib.context import CryptContext

pytestmark = pytest.mark.unit

SECURITY_MODULE = Path(__file__).resolve().parents[1] / "app" / "core" / "security.py"

# bcrypt's own ceiling on what counts as "production strength" here. passlib's default
# for bcrypt is 12; this is a floor, not an equality, so deliberately RAISING the cost
# factor is allowed and lowering it is not.
MINIMUM_PRODUCTION_ROUNDS = 12


def _security_source() -> str:
    return SECURITY_MODULE.read_text(encoding="utf-8")


def test_app_security_module_sets_no_bcrypt_rounds_override() -> None:
    """The app package must not pin a cost factor at all -- it inherits passlib's."""
    offenders = [
        line.strip()
        for line in _security_source().splitlines()
        if re.search(r"\bbcrypt__rounds\b|\brounds\s*=", line) and not line.strip().startswith("#")
    ]
    assert not offenders, (
        f"{SECURITY_MODULE.name} now pins a bcrypt cost factor: {offenders}. "
        "The reduced work factor in tests/conftest.py is TEST-ONLY and must stay there. "
        "If you genuinely mean to change production hashing cost, raise it deliberately "
        "and update MINIMUM_PRODUCTION_ROUNDS in this file with a note explaining why."
    )


def test_production_context_still_hashes_at_the_full_cost_factor() -> None:
    """Rebuild the context exactly as the app does, and read the cost out of the hash."""
    source = _security_source()
    assert 'CryptContext(schemes=["bcrypt"], deprecated="auto")' in source, (
        f"{SECURITY_MODULE.name} no longer builds its CryptContext the way this test "
        "reproduces it. Update the construction below to match, then re-check the cost."
    )

    production_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    digest = production_context.hash("a-throwaway-value-for-cost-inspection")

    # bcrypt modular-crypt format: $2b$<rounds>$<22-char salt><31-char hash>
    match = re.match(r"^\$2[aby]\$(\d{2})\$", digest)
    assert match, f"Unrecognized bcrypt hash format: {digest[:12]!r}"

    rounds = int(match.group(1))
    assert rounds >= MINIMUM_PRODUCTION_ROUNDS, (
        f"Production bcrypt cost factor is {rounds}, below the required "
        f"{MINIMUM_PRODUCTION_ROUNDS}. Each step down halves the cost of an offline "
        "brute-force against the stored password hashes."
    )


def test_the_test_override_is_actually_in_effect() -> None:
    """The other half: if the conftest override stops working, say so loudly.

    Without this, a refactor that broke the rebind (importing ``pwd_context`` by value
    somewhere, or moving the assignment below the first hash) would not fail anything
    -- the suite would just quietly get slower again, which is precisely the kind of
    regression nobody notices until it has been there for months.
    """
    from app.core.security import get_password_hash

    digest = get_password_hash("another-throwaway-value")
    match = re.match(r"^\$2[aby]\$(\d{2})\$", digest)
    assert match, f"Unrecognized bcrypt hash format: {digest[:12]!r}"

    rounds = int(match.group(1))
    assert rounds < MINIMUM_PRODUCTION_ROUNDS, (
        f"Hashing inside the test process is running at cost factor {rounds}, so "
        "tests/conftest.py's test-only override is no longer taking effect. The suite "
        "still passes but pays the full production KDF cost on every hash and verify. "
        "Check that the rebind still precedes TEST_PASSWORD_HASH and that nothing "
        "imports pwd_context by value."
    )
