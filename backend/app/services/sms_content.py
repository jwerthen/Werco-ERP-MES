"""Terse SMS body construction — the ONE place an SMS body is built.

``NOTIFICATIONS_PLAN.md`` §3.4 / §11.1 make this a standing content rule, not a
per-call judgement call. The rule was **narrowly relaxed on 2026-07-29** (boundary
decision of record: ``docs/NOTIFICATIONS.md`` §11.1) — and relaxed far less than the
email rule, for a reason unrelated to CMMC: an SMS renders on a **locked phone
screen**, readable by anyone holding the phone without authenticating to anything,
and Twilio bills per segment. An SMS may carry only

* the record TYPE + IDENTIFIER (e.g. ``WO-1042``, ``NCR-2026-014``),
* the catalog EVENT label (e.g. "Work order blocked / on hold"),
* ONE short closed-vocabulary classifier (an enum value, e.g. ``machine_down``), and
* a "log in to view" pointer.

It must NEVER carry customer names, part numbers/descriptions, quantities, prices,
operator names, free-text reasons, or any other field detail. The detail lives
behind the login.

Because of that rule this module deliberately does NOT accept the caller-built
notification ``title``/``body`` (which crons and direct dispatchers compose freely
and which legitimately contain equipment names, quote numbers, day counts, ...).
That refusal is UNCHANGED by the 2026-07-29 relaxation. The body is built from the
catalog label plus a *sanitized* identifier and a *sanitized* classifier only, so a
future payload key carrying free text can never reach an SMS.

Everything here is pure and side-effect free so the content rule is unit-testable
and auditable in isolation.
"""

from __future__ import annotations

import re
from typing import Optional

# Brand prefix so a shop-floor phone recognizes the sender at a glance.
SMS_PREFIX = "Werco"

# Closing pointer — the whole point of the terse body is to drive the recipient to
# the app, where the CUI detail is protected by authentication.
SMS_CALL_TO_ACTION = "Log in to view."

# One GSM-7 SMS segment is 160 chars; keep the whole body inside a single segment so
# a storm cannot silently multiply into per-segment billing (and so shop-floor phones
# render it as one message). Bodies longer than this are truncated at the label.
SMS_MAX_LENGTH = 160

# An identifier is a record number, not free text. Anything outside this charset (or
# longer than the cap) is dropped rather than sent -- defense-in-depth against a
# payload key that someday carries a description instead of a number.
_IDENTIFIER_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#\- ]{0,39}$")

# A detail classifier is an enum VALUE -- a single identifier-shaped token. Letters,
# underscores and hyphens only: no digits (excludes part numbers, quantities, dates)
# and no whitespace (excludes every human-entered name and phrase).
_DETAIL_SAFE_RE = re.compile(r"^[A-Za-z][A-Za-z_\-]*$")
_DETAIL_MAX_LENGTH = 24
_DETAIL_MAX_WORDS = 3


def safe_identifier(raw: Optional[object]) -> Optional[str]:
    """Return ``raw`` as a record identifier safe to put in an SMS, or ``None``.

    Accepts only short, record-number-shaped strings. Free text, anything with
    punctuation beyond ``. _ / # -``, and anything over 40 characters is refused
    (returns ``None``) so the body degrades to the label rather than leaking detail.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return value if _IDENTIFIER_SAFE_RE.match(value) else None


def safe_detail(raw: Optional[object]) -> Optional[str]:
    """Return ``raw`` as a short classifier safe to put in an SMS, or ``None``.

    Deliberately STRICTER than :func:`safe_identifier`, and not a free-text channel.
    An SMS renders on a locked phone screen, so the only detail permitted here is a
    closed-vocabulary token — an enum value like ``machine_down`` or ``material`` —
    rendered as words. Anything that looks like prose, a record number, a quantity,
    or a name is refused and the body degrades to the label alone.

    The guard is shape-based rather than a fixed word list, so a new enum member does
    not silently lose its SMS detail while operator-typed text still cannot pass. The
    load-bearing discriminator is **whitespace in the raw value**:

    * the raw value must be a SINGLE TOKEN -- an identifier-shaped enum value such as
      ``machine_down``, never a phrase. This is what separates ``machine_down`` from
      ``Acme Aerospace Corp``; a shape check alone cannot, because after normalization
      both are just lowercase words. Any human-entered name or sentence has spaces;
      a ``str``-backed enum value in this codebase does not.
    * letters, underscores and hyphens only -- **no digits**, which excludes part
      numbers, quantities, dates and most identifiers;
    * at most ``_DETAIL_MAX_WORDS`` underscore-separated words and
      ``_DETAIL_MAX_LENGTH`` characters, which excludes long compound tokens.

    Callers must additionally source the value from a fixed payload allowlist
    (``_SMS_DETAIL_KEYS`` in ``notification_dispatch``) -- that allowlist is the first
    fence and this function is the second. Neither alone is sufficient: the allowlist
    stops the wrong FIELD, this stops the wrong VALUE in a right-looking field.
    """
    if raw is None:
        return None
    token = str(raw).strip()
    # Single token only. A phrase means human-entered text, which never goes to SMS.
    if not token or any(ch.isspace() for ch in token):
        return None
    if len(token) > _DETAIL_MAX_LENGTH:
        return None
    if not _DETAIL_SAFE_RE.match(token):
        return None
    words = [w for w in token.replace("-", "_").split("_") if w]
    if not words or len(words) > _DETAIL_MAX_WORDS:
        return None
    return " ".join(words).lower()


def _truncate(text: str, limit: int) -> str:
    # ASCII "..." rather than U+2026. A single non-GSM-7 character forces the whole
    # message into UCS-2 encoding, which drops the per-segment budget from 160 chars
    # to 70 -- so one ellipsis silently turned a capped single-segment body into
    # roughly three billed segments, defeating the cap it was helping to enforce.
    # Fixed 2026-07-29. Keep every character in this module GSM-7.
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def build_sms_body(*, label: str, identifier: Optional[object] = None, detail: Optional[object] = None) -> str:
    """Build the SMS body for one notification.

    Format (§3.4, relaxed 2026-07-29)::

        Werco: WO-1042 - Work order blocked / on hold (machine down). Log in to view.
        Werco: WO-1042 - Work order blocked / on hold. Log in to view.   # no safe detail
        Werco: Incoming inspection failed. Log in to view.               # no identifier

    Still deliberately does NOT accept the caller-composed ``title``/``body``. Those are
    written freely by crons and direct dispatchers and legitimately contain equipment
    names, customer names and day counts; an SMS lands on a locked screen, so the body
    stays machine-composed from vetted inputs only.

    Args:
        label: the catalog entry's ``label`` (a static event name).
        identifier: the record number from the event payload. Sanitized via
            :func:`safe_identifier`; dropped when it is not record-number-shaped.
        detail: an optional closed-vocabulary classifier (an enum value such as
            ``machine_down``). Sanitized via :func:`safe_detail`, which refuses
            digits, prose and anything longer than a few words; dropped when unsafe.
            Callers must source it from the ``_SMS_DETAIL_KEYS`` allowlist.

    Returns:
        A single-segment (<=160 char) body, all GSM-7. Content is dropped in
        increasing order of value -- the detail goes first, then the label is
        truncated -- so every message keeps its identifier and "log in" pointer.
    """
    safe = safe_identifier(identifier)
    safe_det = safe_detail(detail)
    clean_label = " ".join((label or "").split()).rstrip(".")
    head = f"{SMS_PREFIX}: {safe} - " if safe else f"{SMS_PREFIX}: "
    tail = f". {SMS_CALL_TO_ACTION}"

    room = SMS_MAX_LENGTH - len(head) - len(tail)
    if room < 1:  # pragma: no cover - only reachable if the constants are edited
        return _truncate(f"{head}{clean_label}", SMS_MAX_LENGTH)

    # The detail is the least valuable element, so it is the first thing dropped when
    # the budget is tight -- never truncated mid-word into something misleading.
    if safe_det:
        suffix = f" ({safe_det})"
        if len(clean_label) + len(suffix) <= room:
            return f"{head}{clean_label}{suffix}{tail}"
    return f"{head}{_truncate(clean_label, room)}{tail}"


def build_overflow_sms_body(count: int) -> str:
    """Body for the storm-control collapse message (§3.4).

    One message stands in for ``count`` suppressed alerts::

        Werco: 7 more alerts - check the app. Log in to view.

    Carries no identifiers at all, so it is CUI-safe by construction.
    """
    n = max(int(count), 1)
    noun = "alert" if n == 1 else "alerts"
    return f"{SMS_PREFIX}: {n} more {noun} - check the app. {SMS_CALL_TO_ACTION}"


def build_test_sms_body() -> str:
    """Body for the "Send test SMS" button (My Settings)."""
    return f"{SMS_PREFIX}: test message - SMS alerts are configured for your account."
