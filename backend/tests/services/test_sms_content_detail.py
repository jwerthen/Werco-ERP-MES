"""SMS classifier-detail coverage (``sms_content.safe_detail`` + ``build_sms_body``).

This module is the privacy fence for the SMS channel, so the tests here pin BOTH
directions: exactly what a body may carry, and what must never appear in one.

Headline assertions:

* ``safe_detail`` accepts closed-vocabulary, enum-shaped tokens only. The
  **load-bearing discriminator is whitespace in the raw value** -- a pure shape
  check cannot separate the enum value ``machine_down`` from the customer name
  ``Acme Aerospace Corp``, because after normalization both are just lowercase
  words. Every human-entered name or phrase has a space; a ``str``-backed enum
  value in this codebase does not.
* the detail is the FIRST thing dropped when the 160-char budget is tight, and it
  is never truncated mid-word into something misleading;
* **every** body is pure GSM-7 (all ``ord(c) < 128``) and fits one segment. This is
  the regression test for the U+2026 billing bug: a single non-GSM-7 character
  forces the whole message into UCS-2, cutting the per-segment budget from 160 to
  70, so one ellipsis silently turned a capped body into ~3 BILLED segments --
  defeating the cap it was helping to enforce.

Pure functions, no DB, no Redis, no network.
"""

import pytest

from app.services.sms_content import (
    SMS_MAX_LENGTH,
    build_overflow_sms_body,
    build_sms_body,
    build_test_sms_body,
    safe_detail,
    safe_identifier,
)

pytestmark = [pytest.mark.unit]

_LABEL = "Work order blocked / on hold"


# ---------------------------------------------------------------------------
# 1. safe_detail ACCEPTS enum-shaped tokens (and normalizes them to words)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("machine_down", "machine down"),  # the canonical str-backed enum value
        ("MACHINE_DOWN", "machine down"),  # enum NAME casing normalizes the same way
        ("material", "material"),  # single word
        ("quality-hold", "quality hold"),  # hyphen is an accepted word separator
        ("tooling", "tooling"),
        ("  material  ", "material"),  # surrounding whitespace is stripped, not a phrase
    ],
)
def test_safe_detail_accepts_enum_shaped_tokens(raw, expected):
    assert safe_detail(raw) == expected


def test_safe_detail_accepts_the_word_limit_exactly_and_refuses_one_more():
    """Boundary: <= 3 underscore-separated words pass, a 4th word does not."""
    assert safe_detail("late_start_hold") == "late start hold"
    assert safe_detail("late_start_hold_again") is None


def test_safe_detail_accepts_the_length_limit_exactly_and_refuses_one_more():
    """Boundary: <= 24 characters pass, 25 does not."""
    assert safe_detail("a" * 24) == "a" * 24
    assert safe_detail("a" * 25) is None


# ---------------------------------------------------------------------------
# 2. safe_detail REJECTS everything that is not a closed-vocabulary token.
#    One case per reason, so a regression names the rule it broke.
# ---------------------------------------------------------------------------


def test_safe_detail_refuses_a_customer_name():
    """A customer name is CUI-adjacent free text and has spaces -- refused."""
    assert safe_detail("Acme Aerospace Corp") is None


def test_safe_detail_refuses_a_record_number():
    """A record number carries digits (and belongs in the identifier slot) -- refused."""
    assert safe_detail("WO-1042") is None


def test_safe_detail_refuses_a_quantity():
    """Quantities are CUI-ish detail: digits AND a space -- refused."""
    assert safe_detail("5 pieces") is None


def test_safe_detail_refuses_a_part_number_phrase():
    """A part number is exactly what must not reach a lock screen -- refused."""
    assert safe_detail("part 7842-B") is None


def test_safe_detail_refuses_a_phrase_that_is_already_spaced():
    """Same letters as ``machine_down``, but a space means human-entered text."""
    assert safe_detail("machine down") is None


def test_safe_detail_refuses_a_sentence():
    """Operator-typed prose -- refused (spaces, punctuation and length all fail)."""
    assert safe_detail("Operator held the job because the fixture was damaged") is None


def test_safe_detail_refuses_an_over_length_token():
    """A long compound token is not a classifier, whatever its shape."""
    assert safe_detail("extraordinarily_long_classifier_token") is None


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", None])
def test_safe_detail_refuses_empty_and_none(raw):
    assert safe_detail(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "machine_down!",  # punctuation outside [A-Za-z_-]
        "_machine_down",  # must start with a letter
        "<script>",
        "machine.down",
        "machine/down",
    ],
)
def test_safe_detail_refuses_out_of_charset_tokens(raw):
    assert safe_detail(raw) is None


# ---------------------------------------------------------------------------
# 3. The whitespace rule specifically -- the load-bearing discriminator
# ---------------------------------------------------------------------------


def test_whitespace_is_what_separates_a_human_name_from_an_enum_value():
    """A two-word human name is refused even though its LETTERS would pass.

    Delete the whitespace check and ``safe_detail("Dana Host")`` starts returning
    ``"dana host"``: the letters, the length and the word count are all inside the
    limits. Only the "single raw token" rule refuses it -- which is why the same
    letters with the space removed DO pass. That asymmetry is the fence.
    """
    assert safe_detail("Dana Host") is None
    # Same characters, no space -> accepted. Shape alone cannot tell these apart.
    assert safe_detail("DanaHost") == "danahost"


@pytest.mark.parametrize("gap", [" ", "\t", "\n", " "])
def test_any_internal_whitespace_refuses_the_value(gap):
    """Not just the ASCII space -- any whitespace character means a phrase."""
    assert safe_detail(f"machine{gap}down") is None


def test_safe_detail_is_stricter_than_safe_identifier():
    """The two sanitizers are deliberately different; SMS detail is the tighter one."""
    assert safe_identifier("WO-1042") == "WO-1042"  # a fine identifier ...
    assert safe_detail("WO-1042") is None  # ... but never a classifier.


# ---------------------------------------------------------------------------
# 4. build_sms_body with / without a detail, and the drop-before-truncate rule
# ---------------------------------------------------------------------------


def test_body_carries_a_safe_detail_in_parentheses():
    body = build_sms_body(label=_LABEL, identifier="WO-1042", detail="machine_down")
    assert body == "Werco: WO-1042 - Work order blocked / on hold (machine down). Log in to view."


def test_body_without_a_detail_is_unchanged_from_before():
    """No detail supplied -> byte-identical to the pre-feature body."""
    body = build_sms_body(label=_LABEL, identifier="WO-1042")
    assert body == "Werco: WO-1042 - Work order blocked / on hold. Log in to view."


def test_an_unsafe_detail_degrades_to_the_bare_label():
    """A refused VALUE drops the detail; it never leaks and never half-leaks."""
    body = build_sms_body(label=_LABEL, identifier="WO-1042", detail="Acme Aerospace Corp")
    assert body == "Werco: WO-1042 - Work order blocked / on hold. Log in to view."
    for leaked in ("Acme", "Aerospace", "Corp", "("):
        assert leaked not in body


def test_detail_is_dropped_not_truncated_when_the_budget_is_tight():
    """The detail is the least valuable element, so it goes FIRST -- whole, or not at all.

    The label here fits the budget on its own but not with the suffix appended, so a
    correct implementation drops the detail and leaves the label INTACT. A truncating
    implementation would instead emit a mangled "(machine dow..." tail.
    """
    head_len = len("Werco: WO-1 - ")
    tail_len = len(". Log in to view.")
    room = SMS_MAX_LENGTH - head_len - tail_len
    label = "y" * (room - 5)  # fits alone; label + " (machine down)" does not

    body = build_sms_body(label=label, identifier="WO-1", detail="machine_down")

    assert label in body, "the label must survive whole"
    assert "machine" not in body and "(" not in body
    assert "..." not in body, "nothing needed truncating -- only the detail was dropped"
    assert body == f"Werco: WO-1 - {label}. Log in to view."


def test_identifier_and_call_to_action_survive_a_truncating_label():
    """Content drops in increasing order of value: detail, then the label is cut."""
    body = build_sms_body(label="x" * 400, identifier="WO-1042", detail="machine_down")
    assert body.startswith("Werco: WO-1042 - ")
    assert body.endswith("Log in to view.")
    assert "machine" not in body  # dropped, not squeezed in
    assert len(body) == SMS_MAX_LENGTH


def test_a_detail_survives_when_there_is_room():
    """Mutation guard on the ordering: with room to spare the detail IS included."""
    body = build_sms_body(label="Short label", identifier="WO-1042", detail="material")
    assert "(material)" in body
    assert len(body) <= SMS_MAX_LENGTH


def test_body_without_an_identifier_still_points_at_the_app():
    body = build_sms_body(label="Incoming inspection failed", detail="material")
    assert body == "Werco: Incoming inspection failed (material). Log in to view."


# ---------------------------------------------------------------------------
# 5. Every body is GSM-7 and one segment (the U+2026 billing regression)
# ---------------------------------------------------------------------------


_GSM7_MATRIX = [
    build_sms_body(label=_LABEL, identifier="WO-1042", detail="machine_down"),
    build_sms_body(label=_LABEL, identifier="WO-1042"),
    build_sms_body(label=_LABEL),
    # The truncation path -- where the ellipsis used to be emitted.
    build_sms_body(label="x" * 400, identifier="WO-1"),
    build_sms_body(label="x" * 400, identifier="WO-1", detail="machine_down"),
    build_sms_body(label="x " * 200, identifier="WO-1042", detail="quality-hold"),
    build_overflow_sms_body(7),
    build_overflow_sms_body(1),
    build_test_sms_body(),
]


@pytest.mark.parametrize("body", _GSM7_MATRIX)
def test_every_body_fits_one_segment(body):
    assert len(body) <= SMS_MAX_LENGTH


@pytest.mark.parametrize("body", _GSM7_MATRIX)
def test_machine_composed_bodies_are_pure_gsm7(body):
    """Regression for the U+2026 truncation marker (fixed 2026-07-29).

    One non-GSM-7 character forces UCS-2 encoding, which drops the per-segment
    budget from 160 chars to 70 -- so a 160-char "capped" body became roughly
    three BILLED segments. Every character this module composes must be ASCII.
    """
    assert all(ord(ch) < 128 for ch in body), repr([ch for ch in body if ord(ch) >= 128])


def test_the_truncation_marker_is_ascii_not_an_ellipsis():
    body = build_sms_body(label="x" * 400, identifier="WO-1")
    assert "…" not in body
    assert body.count("...") == 1
    assert len(body) == SMS_MAX_LENGTH
    assert all(ord(ch) < 128 for ch in body)
