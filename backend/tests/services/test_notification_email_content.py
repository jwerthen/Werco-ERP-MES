"""Email notification CONTENT coverage (``notification_dispatch`` composition layer).

Notification content is a privacy surface, so these tests pin BOTH directions:
what an email body may carry, and what must never appear in one.

Headline assertions:

* ``_payload_detail_line`` renders the ``_DETAIL_KEYS`` ALLOWLIST, in catalog order,
  skipping absent/empty values and truncating a long one -- so a future emit site
  cannot silently widen what gets mailed just by adding a payload key;
* ``_content_for_event`` appends that line to the catalog description and returns the
  bare description when nothing is allowlisted (title is unchanged either way);
* the composition reads the PAYLOAD ONLY -- the dispatcher never queries the DB to
  resolve ``part_id`` into a part number, so part numbers and customer names stay out
  of the body (a scope/N+1 decision recorded in docs/NOTIFICATIONS.md section 11.1);
* ``CatalogEntry.description`` is NOT mutated by dispatch -- the same string is served
  to the preferences matrix by ``GET /notifications/catalog``, and it must not drift;
* the SMS field-allowlist (``_SMS_DETAIL_KEYS``) is a SEPARATE, much narrower fence
  than the email one -- ``reason`` is mailable but must never be text-messaged.

Pure functions, no DB, no Redis, no network.
"""

from types import SimpleNamespace

import pytest

from app.services.notification_catalog import get_entry
from app.services.notification_dispatch import (
    _DETAIL_KEYS,
    _EMAIL_DETAIL_VALUE_MAX,
    _SMS_DETAIL_KEYS,
    _content_for_event,
    _payload_detail_line,
    _payload_sms_detail,
)
from app.services.sms_content import safe_detail

pytestmark = [pytest.mark.unit]


def _event(payload):
    """A stand-in for the committed OperationalEvent the dispatcher reads."""
    return SimpleNamespace(event_payload=payload)


# ---------------------------------------------------------------------------
# 1. _payload_detail_line -- the email allowlist
# ---------------------------------------------------------------------------


def test_detail_line_composes_allowlisted_keys_in_catalog_order():
    """Order follows ``_DETAIL_KEYS``, NOT the payload's own key order.

    The payload below deliberately lists the keys backwards, so a regression that
    iterates ``payload`` instead of the allowlist flips the output.
    """
    line = _payload_detail_line(
        {
            "reason": "material shortage",
            "quantity_complete": 40,
            "status": "completed",
        }
    )
    assert line == "Status: completed | Qty complete: 40 | Reason: material shortage"


def test_detail_line_renders_the_full_transition_and_quantity_family():
    line = _payload_detail_line(
        {
            "old_status": "in_progress",
            "new_status": "completed",
            "quantity_scrapped": 2,
            "old_priority": "normal",
            "new_priority": "urgent",
            "days_late": 3,
            "disposition": "rework",
        }
    )
    assert line == (
        "From: in_progress | To: completed | Qty scrapped: 2 | "
        "Priority was: normal | Priority now: urgent | Days late: 3 | Disposition: rework"
    )


@pytest.mark.parametrize("empty", [None, ""])
def test_detail_line_skips_absent_and_empty_values(empty):
    line = _payload_detail_line({"status": "completed", "reason": empty, "disposition": empty})
    assert line == "Status: completed"


def test_detail_line_keeps_a_zero_quantity():
    """0 is information ("nothing scrapped"), not an empty value -- it must render."""
    assert _payload_detail_line({"quantity_scrapped": 0}) == "Qty scrapped: 0"


def test_detail_line_collapses_whitespace_in_a_value():
    """A multi-line operator reason must not turn the body into ragged blocks."""
    assert _payload_detail_line({"reason": "material\n  shortage\tagain"}) == "Reason: material shortage again"


def test_detail_line_truncates_a_long_value():
    """Email is far more permissive than SMS, but not unbounded."""
    line = _payload_detail_line({"reason": "x" * 300})
    value = line.split("Reason: ", 1)[1]
    assert len(value) == _EMAIL_DETAIL_VALUE_MAX
    assert value.endswith("...")
    assert "x" * 300 not in line


def test_detail_line_leaves_a_value_at_the_limit_untouched():
    """Boundary guard: exactly ``_EMAIL_DETAIL_VALUE_MAX`` chars is NOT truncated."""
    value = "y" * _EMAIL_DETAIL_VALUE_MAX
    assert _payload_detail_line({"reason": value}) == f"Reason: {value}"


@pytest.mark.parametrize("payload", [{}, {"part_id": 99, "customer_name": "Acme Aerospace Corp"}])
def test_detail_line_is_none_when_nothing_is_allowlisted(payload):
    assert _payload_detail_line(payload) is None


# ---------------------------------------------------------------------------
# 2. _content_for_event -- description + detail
# ---------------------------------------------------------------------------


def test_content_appends_the_detail_to_the_catalog_description():
    entry = get_entry("ncr.created")
    title, body = _content_for_event(
        entry,
        _event({"ncr_number": "NCR-14", "status": "open", "disposition": "rework"}),
    )
    assert title == f"{entry.label}: NCR-14"
    assert body == f"{entry.description}\n\nStatus: open | Disposition: rework"


def test_content_is_the_bare_description_when_nothing_is_allowlisted():
    entry = get_entry("ncr.created")
    title, body = _content_for_event(entry, _event({"ncr_number": "NCR-14"}))
    assert title == f"{entry.label}: NCR-14"
    assert body == entry.description
    assert "\n" not in body, "no dangling blank line when there is no detail"


def test_content_handles_an_empty_payload():
    entry = get_entry("ncr.created")
    title, body = _content_for_event(entry, _event({}))
    assert title == entry.label  # no identifier -> label alone
    assert body == entry.description


def test_content_handles_a_null_payload():
    entry = get_entry("ncr.created")
    title, body = _content_for_event(entry, SimpleNamespace(event_payload=None))
    assert (title, body) == (entry.label, entry.description)


# ---------------------------------------------------------------------------
# 3. The allowlist is the point: non-allowlisted keys never reach the body
# ---------------------------------------------------------------------------


def test_a_non_allowlisted_payload_key_never_reaches_the_body():
    """``part_id`` / ``customer_name`` / ``part_number`` are plausible payload keys.

    None of them is in ``_DETAIL_KEYS``, so none may appear -- and the dispatcher
    deliberately does not query the DB to expand an id into a part number either.
    Adding a key to a payload must NOT widen the email.
    """
    entry = get_entry("ncr.created")
    title, body = _content_for_event(
        entry,
        _event(
            {
                "ncr_number": "NCR-14",
                "status": "open",
                "part_id": 7842,
                "part_number": "55-2210",
                "customer_name": "Acme Aerospace Corp",
                "operator_name": "J. Ruiz",
                "unit_price": "412.50",
            }
        ),
    )
    assert body == f"{entry.description}\n\nStatus: open"
    for leaked in ("7842", "55-2210", "Acme", "Aerospace", "Ruiz", "412.50", "part_id", "customer_name"):
        assert leaked not in body, f"{leaked!r} leaked into the email body"
        assert leaked not in title


def test_the_email_allowlist_is_a_closed_set():
    """Pins the allowlist itself: widening it is a deliberate, reviewed edit."""
    assert [key for key, _ in _DETAIL_KEYS] == [
        "status",
        "old_status",
        "new_status",
        "quantity_complete",
        "quantity_scrapped",
        "quantity_affected",
        "quantity_received",
        "quantity_accepted",
        "quantity_rejected",
        "old_priority",
        "new_priority",
        "days_late",
        "days_until_expiry",
        "disposition",
        "category",
        "source",
        "inspection_method",
        "reason",
    ]
    # Operator-typed free-text fields that are NOT mailable.
    for banned in ("title", "note", "scrap_reason", "defect_type", "step_label", "customer_name", "part_number"):
        assert banned not in {key for key, _ in _DETAIL_KEYS}


# ---------------------------------------------------------------------------
# 4. The catalog description must not drift (it feeds the preferences matrix)
# ---------------------------------------------------------------------------


def test_dispatch_does_not_mutate_the_catalog_description():
    """``entry.description`` is served verbatim by ``GET /notifications/catalog``.

    Composition happens in ``_content_for_event``; the catalog stays a static string.
    """
    entry = get_entry("ncr.created")
    before = entry.description

    for _ in range(3):
        _content_for_event(entry, _event({"status": "open", "reason": "material shortage"}))

    assert entry.description == before
    # ... and the module-level catalog object itself is untouched for the next reader.
    assert get_entry("ncr.created").description == before
    assert "Status: open" not in get_entry("ncr.created").description


# ---------------------------------------------------------------------------
# 5. The SMS field-fence is separate from (and much narrower than) the email one
# ---------------------------------------------------------------------------


def test_sms_detail_picks_the_first_allowlisted_field_in_order():
    assert _payload_sms_detail({"source": "kiosk", "category": "material"}) == "material"
    assert _payload_sms_detail({"source": "kiosk"}) == "kiosk"
    assert _payload_sms_detail({}) is None


def test_operator_typed_fields_are_not_sms_eligible_even_with_a_passing_value():
    """The two SMS fences are INDEPENDENT, and both are required.

    ``safe_detail`` would happily accept these values -- they are single enum-shaped
    tokens. They are refused anyway because the FIELD is not in ``_SMS_DETAIL_KEYS``:
    ``note`` / ``scrap_reason`` / ``reason`` are operator-typed and routinely carry
    customer and part detail, so they can never even be considered.
    """
    for field in ("note", "scrap_reason", "reason", "defect_type", "step_label", "title"):
        assert safe_detail("tooling") == "tooling", "the VALUE fence would have passed it"
        assert _payload_sms_detail({field: "tooling"}) is None, f"{field!r} must not be SMS-eligible"


def test_reason_is_mailable_but_never_text_messageable():
    """The one key that makes the two allowlists visibly different."""
    assert "reason" in {key for key, _ in _DETAIL_KEYS}
    assert "reason" not in _SMS_DETAIL_KEYS
    payload = {"reason": "tooling"}
    assert _payload_detail_line(payload) == "Reason: tooling"
    assert _payload_sms_detail(payload) is None


def test_the_sms_allowlist_is_a_closed_set_of_enum_fields():
    assert _SMS_DETAIL_KEYS == ("category", "planned_type", "source")
