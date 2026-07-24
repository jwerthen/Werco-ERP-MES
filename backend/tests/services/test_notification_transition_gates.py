"""Transition-gate + CUI-content regression tests (PR-1 adversarial-review fixes).

The ``ncr_updated`` / ``work_order_blocker_updated`` / ``fai_updated`` OperationalEvents
fire on ANY edit and carry both ``status`` and ``previous_status``. The catalog gates must
fire only on the TRANSITION into the terminal state, not on every edit while the record is
already terminal -- otherwise re-editing a closed NCR / resolved blocker / completed FAI
(past the 5-min dedup window, a distinct event id so ``notified_at`` idempotency doesn't
help) re-sends a spurious notification.

Also asserts the expedite email is CUI-safe (no quantity in the rendered body, §11.1).
"""

from types import SimpleNamespace

import pytest

from app.services.email_service import EmailService
from app.services.notification_catalog import (
    gate_blocker_resolved,
    gate_fai_completed,
    gate_ncr_closed,
)

pytestmark = pytest.mark.unit


def _ev(payload):
    return SimpleNamespace(event_payload=payload)


def test_gate_blocker_resolved_fires_only_on_transition():
    # open -> resolved: the real resolve transition FIRES
    assert gate_blocker_resolved(_ev({"previous_status": "open", "status": "resolved"})) is True
    # editing an already-resolved blocker (e.g. reassign / note): SUPPRESSED
    assert gate_blocker_resolved(_ev({"previous_status": "resolved", "status": "resolved"})) is False
    # a non-resolve update never fires
    assert gate_blocker_resolved(_ev({"previous_status": "open", "status": "acknowledged"})) is False
    # case-insensitive (enum .value may be upper)
    assert gate_blocker_resolved(_ev({"previous_status": "OPEN", "status": "RESOLVED"})) is True


def test_gate_ncr_closed_fires_only_on_transition():
    assert gate_ncr_closed(_ev({"previous_status": "open", "status": "closed"})) is True
    # re-editing a still-CLOSED NCR (disposition/note fix): SUPPRESSED (the reported bug)
    assert gate_ncr_closed(_ev({"previous_status": "closed", "status": "closed"})) is False
    assert gate_ncr_closed(_ev({"previous_status": "open", "status": "in_review"})) is False
    assert gate_ncr_closed(_ev({"previous_status": "OPEN", "status": "CLOSED"})) is True


def test_gate_fai_completed_fires_only_on_completion_transition():
    # in_progress -> passed: the completion transition FIRES
    assert gate_fai_completed(_ev({"previous_status": "in_progress", "status": "passed"})) is True
    # editing an already-completed FAI: SUPPRESSED
    assert gate_fai_completed(_ev({"previous_status": "passed", "status": "passed"})) is False
    # terminal -> different terminal (correcting a completed result) is NOT a completion transition
    assert gate_fai_completed(_ev({"previous_status": "passed", "status": "failed"})) is False
    assert gate_fai_completed(_ev({"previous_status": "in_progress", "status": "in_progress"})) is False


def test_gates_default_to_fire_when_previous_status_absent():
    # Defensive: for a hypothetical emit that omits previous_status, fire (conservative,
    # never silently drop). Real ncr/blocker/fai emits always carry previous_status.
    assert gate_ncr_closed(_ev({"status": "closed"})) is True
    assert gate_blocker_resolved(_ev({"status": "resolved"})) is True
    assert gate_fai_completed(_ev({"status": "passed"})) is True


def test_expedite_email_is_cui_safe_no_quantity():
    """§11.1: the expedite email carries the part identifier + required date only -- the
    order quantity (CUI field detail) must not cross the external SMTP boundary."""
    html = EmailService()._render_template(
        "expedite_required",
        {"part_number": "ABC-123", "required_date": "2026-08-01", "base_url": "http://erp.test"},
    )
    assert "ABC-123" in html  # identifier is allowed
    assert "Quantity" not in html  # the CUI row is gone
    assert "500" not in html  # no stray quantity value ever renders
