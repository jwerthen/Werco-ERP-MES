"""CUI-safe SMS body construction — the ONE place an SMS body is built.

``NOTIFICATIONS_PLAN.md`` §3.4 / §11.1 make this a standing content rule, not a
per-call judgement call: Twilio sits **outside** the CUI boundary, so an SMS may
carry only

* the record TYPE + IDENTIFIER (e.g. ``WO-1042``, ``NCR-2026-014``),
* the catalog EVENT label (e.g. "Work order blocked / on hold"), and
* a "log in to view" pointer.

It must NEVER carry customer names, part numbers/descriptions, quantities, prices,
operator names, free-text reasons, or any other field detail. The detail lives
behind the login.

Because of that rule this module deliberately does NOT accept the caller-built
notification ``title``/``body`` (which crons and direct dispatchers compose freely
and which legitimately contain equipment names, quote numbers, day counts, ...).
It builds the body from the catalog label plus a *sanitized* identifier only, so a
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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def build_sms_body(*, label: str, identifier: Optional[object] = None) -> str:
    """Build the CUI-safe SMS body for one notification.

    Format (§3.4)::

        Werco: WO-1042 - Work order blocked / on hold. Log in to view.
        Werco: Incoming inspection failed. Log in to view.        # no identifier

    Args:
        label: the catalog entry's ``label`` (a static, CUI-free event name).
        identifier: the record number from the event payload. Sanitized via
            :func:`safe_identifier`; dropped when it is not record-number-shaped.

    Returns:
        A single-segment (<=160 char) body. The label is truncated before the
        call-to-action is dropped, so every message keeps its "log in" pointer.
    """
    safe = safe_identifier(identifier)
    clean_label = " ".join((label or "").split()).rstrip(".")
    head = f"{SMS_PREFIX}: {safe} - " if safe else f"{SMS_PREFIX}: "
    tail = f". {SMS_CALL_TO_ACTION}"

    room = SMS_MAX_LENGTH - len(head) - len(tail)
    if room < 1:  # pragma: no cover - only reachable if the constants are edited
        return _truncate(f"{head}{clean_label}", SMS_MAX_LENGTH)
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
