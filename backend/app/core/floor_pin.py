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
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional, Tuple

from pydantic import BeforeValidator, Field

from app.core.security import get_password_hash, verify_password

FLOOR_PIN_MIN_LENGTH = 4
FLOOR_PIN_MAX_LENGTH = 6

# The PIN lock tiers. ``users.floor_pin_failed_attempts`` is CUMULATIVE -- a lock does not reset
# it; only a correct PIN or an admin unlock / set / clear does -- and EVERY 5th wrong PIN starts a
# longer lock, all of them EXPIRING:
#     5 -> 15 minutes, 10 -> 2 hours, 15 -> 24 hours, 20 -> 48 hours, 25 -> 96 hours,
#     30 and every further 5 -> 7 days (the cap).
# No tier is permanent on purpose: a lock only an admin can lift would let anyone holding badge
# numbers lock every PIN holder out from the public route (PR #369 re-check). Instead a lock of
# 24 hours or longer notifies the company's Admins (``floor_pin_lockout``), who can unlock early.
# A tier lock is set by the SAME conditional UPDATE that reserves the attempt that reaches it, so
# a burst can never run past a tier (``app.api.endpoints.auth._reserve_floor_pin_attempt``).
# Separate counters from the password lockout (``users.failed_login_attempts`` / ``locked_until``),
# so a PIN lockout never locks the person's password login and a password lockout never touches
# the PIN.
FLOOR_PIN_LOCK_STEP = 5
FLOOR_PIN_MAX_FAILURES = FLOOR_PIN_LOCK_STEP  # the first tier
FLOOR_PIN_LOCKOUT_MINUTES = 15
FLOOR_PIN_LONG_LOCK_FAILURES = 10
FLOOR_PIN_LONG_LOCKOUT_MINUTES = 120
# (cumulative wrong PINs, lock minutes); every multiple of 5 past the last row takes the cap.
FLOOR_PIN_LOCK_TIERS: Tuple[Tuple[int, int], ...] = (
    (5, 15),
    (10, 2 * 60),
    (15, 24 * 60),
    (20, 48 * 60),
    (25, 96 * 60),
)
FLOOR_PIN_LOCK_CAP_MINUTES = 7 * 24 * 60
# A lock this long or longer notifies the company's Admins (in-app).
FLOOR_PIN_NOTIFY_LOCK_MINUTES = 24 * 60
FLOOR_PIN_LOCKOUT_EVENT_TYPE = "floor_pin_lockout"


def floor_pin_lock_minutes(attempts: int) -> Optional[int]:
    """The lock (minutes) that the ``attempts``-th cumulative wrong PIN starts, or None.

    Every multiple of ``FLOOR_PIN_LOCK_STEP`` is a tier boundary; the tiers escalate and cap at
    7 days. Pure, so the reservation's SQL ``CASE`` and every caller read the same table.
    """
    if attempts < FLOOR_PIN_LOCK_STEP or attempts % FLOOR_PIN_LOCK_STEP:
        return None
    for threshold, minutes in FLOOR_PIN_LOCK_TIERS:
        if attempts == threshold:
            return minutes
    return FLOOR_PIN_LOCK_CAP_MINUTES


def floor_pin_lock_tier(attempts: int) -> Optional[int]:
    """The tier number (1 = 15 minutes ... 6 = the 7-day cap) of a boundary count, or None."""
    if floor_pin_lock_minutes(attempts) is None:
        return None
    return min(attempts // FLOOR_PIN_LOCK_STEP, len(FLOOR_PIN_LOCK_TIERS) + 1)


# Sign-on refusals that are not a challenge or a wrong / locked PIN.
# A badge+PIN session whose PIN changed since it was verified (reset, cleared, cleared by a
# promotion): refused 401 on its next request, refresh or company switch. Also a badge-only
# session once the user has a PIN or the company requires one.
FLOOR_PIN_REAUTH_CODE = "floor_pin_reauth_required"
FLOOR_PIN_REAUTH_MESSAGE = "Sign in again with your badge and PIN."
# A promotion into Admin / Manager / Quality cleared the PIN: badge sign-on is refused (403)
# until an Admin sets a new PIN or clears it, whatever the company policy.
FLOOR_PIN_RESET_REQUIRED_CODE = "floor_pin_reset_required"
FLOOR_PIN_RESET_REQUIRED_MESSAGE = "Your role changed. Ask an Admin to set a new floor PIN."

_EPOCH = datetime(1970, 1, 1)


def floor_pin_stamp(set_at: Optional[datetime]) -> int:
    """``floor_pin_set_at`` as integer epoch-MICROSECONDS -- the token's ``pst`` claim.

    Integer arithmetic (never ``total_seconds()``, a float), so the round trip through a JWT is
    exact. A naive value is UTC (how the column is written); an aware one is converted. ``None``
    (a PIN with no recorded set time, which only a direct database write produces) is 0, so the
    claim still pins the session to that state.
    """
    if set_at is None:
        return 0
    if set_at.tzinfo is not None:
        set_at = set_at.astimezone(timezone.utc).replace(tzinfo=None)
    delta = set_at - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def pin_session_still_valid(floor_pin_hash: Optional[str], floor_pin_set_at: Optional[datetime], stamp: Any) -> bool:
    """Is a ``["badge", "pin"]`` session still backed by the PIN it verified?

    False when the PIN is gone (cleared, or cleared by a promotion), when it was set again
    since (``floor_pin_set_at`` no longer equals the session's ``pst``), or when the token
    carries no usable ``pst``. A LOCK does not end the session: a lock is about guessing, and
    this session already proved the PIN.
    """
    if not floor_pin_hash or type(stamp) is not int:
        return False
    return floor_pin_stamp(floor_pin_set_at) == stamp


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
