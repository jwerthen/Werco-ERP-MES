"""Floor PIN: the second factor on badge / employee-ID sign-on (AS9100D 8.5.2, 7.5.3).

``POST /auth/employee-login`` and ``POST /auth/kiosk-badge-token`` authenticate on a badge
or employee ID, which is printed on a card and is weak evidence of who recorded a completion
or an inspection. A user an administrator has given a floor PIN must also enter it; a company
can require one of every badge user (``Company.require_floor_pin``). The sign-on step itself
is one helper in ``app.api.endpoints.auth`` shared by both routes; this module holds what both
that step and the user-administration routes need:

* :func:`validate_floor_pin` -- the ONE rule set for a new PIN (422 text names the rule and
  never echoes the value);
* :func:`hash_floor_pin` / :func:`verify_floor_pin` -- bcrypt through
  ``app.core.security``'s password context (resolved at call time, so the test suite's
  reduced work factor applies to PINs exactly as it does to passwords);
* :func:`badge_last_four` -- how a badge is read as its last four digits, shared with the
  badge resolver in ``app.api.endpoints.auth`` so the "not your own badge" rule can never
  read a badge differently from the sign-on lookup;
* the ``amr`` method names, the attempt limit and the lock length.

A PIN is a credential: it is never logged, audited, serialized or echoed in an error.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, List, Optional

from pydantic import BeforeValidator, Field

from app.core.security import get_password_hash, verify_password

FLOOR_PIN_MIN_LENGTH = 4
FLOOR_PIN_MAX_LENGTH = 6

# Wrong PINs before the PIN locks, and for how long. Separate counters from the password
# lockout (``users.failed_login_attempts`` / ``locked_until``), so a PIN lockout never
# locks the person's password login and a password lockout never touches the PIN.
FLOOR_PIN_MAX_FAILURES = 5
FLOOR_PIN_LOCKOUT_MINUTES = 15

# ``amr`` values (see ``app.core.security.AMR_CLAIM``).
AUTH_METHOD_BADGE = "badge"
AUTH_METHOD_PIN = "pin"
BADGE_ONLY_AUTH_METHODS: List[str] = [AUTH_METHOD_BADGE]
BADGE_AND_PIN_AUTH_METHODS: List[str] = [AUTH_METHOD_BADGE, AUTH_METHOD_PIN]

# ``fullmatch``, never ``match`` with ``$``: ``$`` also matches before a trailing newline, so
# "2580\n" would pass as digits and then reach ``int()`` in the run check (a 500).
_DIGITS_ONLY = re.compile(r"[0-9]+")

# Refusal texts. Each names the rule and never the value -- and quotes no example PIN either,
# so a refused PIN can never appear in the answer even when it happens to be the example.
PIN_RULE_DIGITS = (
    f"A floor PIN must be {FLOOR_PIN_MIN_LENGTH} to {FLOOR_PIN_MAX_LENGTH} digits, with no letters, spaces or symbols."
)
PIN_RULE_REPEATED = "A floor PIN cannot be the same digit repeated."
PIN_RULE_SEQUENCE = "A floor PIN cannot be a straight run of digits counting up or down."
PIN_RULE_BADGE = "A floor PIN cannot be the user's own employee ID or the last digits of their badge."


def coerce_pin_input(pin: Any) -> Any:
    """Turn any non-null JSON value into a string before Pydantic types it.

    The app's 422 handler echoes each error's ``input``, so a PIN typed as a JSON number
    (``1234``) or any other non-string would otherwise come back in the response body.
    Coercing means the request schema itself never refuses a PIN value: the sign-on step
    reads a malformed one as a wrong PIN, and the admin routes refuse it with
    :func:`validate_floor_pin`'s plain text. (The parameter is named ``pin`` so Sentry's
    scrubber, which filters frame variables by name, filters it.)
    """
    if pin is None or isinstance(pin, str):
        return pin
    return str(pin)


# The request-body type of a PIN. No length / pattern constraint on purpose (see
# ``coerce_pin_input``): a constraint failure would be a 422 echoing the value. Every model
# field of this type is also declared ``Field(..., repr=False)``, so the request model's
# repr -- which Sentry records for a frame's local variables -- never carries the PIN.
FloorPinInput = Annotated[
    str,
    BeforeValidator(coerce_pin_input),
    Field(
        description=(
            f"Floor PIN: {FLOOR_PIN_MIN_LENGTH} to {FLOOR_PIN_MAX_LENGTH} digits. Never echoed; stored only as a hash."
        )
    ),
]


class FloorPinRuleError(ValueError):
    """A proposed PIN breaks a rule. ``str(error)`` is the rule's text -- never the PIN."""


def badge_last_four(value: Optional[str]) -> Optional[str]:
    """A badge / employee ID read as its last four digits, zero-padded (``None`` if it has no digits).

    THE implementation behind ``app.api.endpoints.auth._normalize_employee_id`` (which
    delegates here), so the badge resolver and the PIN rule below read a badge identically.
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if len(digits) < 4:
        return digits.zfill(4)
    return digits[-4:]


def _is_straight_run(pin: str) -> bool:
    """True for 0123 / 1234 / 123456 / 4321 / 654321 -- each digit one step from the last."""
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    return steps == {1} or steps == {-1}


def _badge_forms(employee_id: Optional[str]) -> set[str]:
    """Every digit string a PIN must not equal for this badge.

    The badge's full digit string, its last-four normalization (``badge_last_four``), and
    its trailing 5 and 6 digits -- the "last digits of the badge" at every PIN length.
    """
    forms: set[str] = set()
    if not employee_id:
        return forms
    digits = re.sub(r"\D", "", employee_id)
    if digits:
        forms.add(digits)
        for length in range(FLOOR_PIN_MIN_LENGTH, FLOOR_PIN_MAX_LENGTH + 1):
            if len(digits) >= length:
                forms.add(digits[-length:])
    last_four = badge_last_four(employee_id)
    if last_four:
        forms.add(last_four)
    return forms


def validate_floor_pin(pin: object, *, employee_id: Optional[str]) -> str:
    """Return ``pin`` when it is an acceptable floor PIN for this user, else raise :class:`FloorPinRuleError`.

    Rules: digits only, 4 to 6 long; not one digit repeated; not a straight ascending or
    descending run; not the user's own employee-ID digits or their badge's last digits.
    """
    if not isinstance(pin, str) or not _DIGITS_ONLY.fullmatch(pin):
        raise FloorPinRuleError(PIN_RULE_DIGITS)
    if not FLOOR_PIN_MIN_LENGTH <= len(pin) <= FLOOR_PIN_MAX_LENGTH:
        raise FloorPinRuleError(PIN_RULE_DIGITS)
    if len(set(pin)) == 1:
        raise FloorPinRuleError(PIN_RULE_REPEATED)
    if _is_straight_run(pin):
        raise FloorPinRuleError(PIN_RULE_SEQUENCE)
    if pin in _badge_forms(employee_id):
        raise FloorPinRuleError(PIN_RULE_BADGE)
    return pin


def hash_floor_pin(pin: str) -> str:
    """bcrypt hash of an already-validated PIN (the password context, at its cost factor)."""
    return get_password_hash(pin)


def verify_floor_pin(pin: Optional[str], hashed: Optional[str]) -> bool:
    """True only when ``pin`` matches ``hashed``.

    A value that is not 4 to 6 digits cannot be any stored PIN, so it is refused without
    running bcrypt (which also keeps an oversized value away from the KDF's 72-byte limit);
    an unreadable stored hash reads as a mismatch rather than raising.
    """
    if not hashed or not isinstance(pin, str) or not _DIGITS_ONLY.fullmatch(pin):
        return False
    if not FLOOR_PIN_MIN_LENGTH <= len(pin) <= FLOOR_PIN_MAX_LENGTH:
        return False
    try:
        return verify_password(pin, hashed)
    except (ValueError, TypeError):
        return False
