"""Checklist item normalization parity: Python's ``normalize_checklist_item`` / ``checklist_item_key``
(``app/services/maintenance_checklist.py``) against the SHARED fixture the TypeScript twin
(``frontend/src/utils/maintenance.ts``) is held to by Jest.

The completion dialog asks for a reason exactly when IT sees an unticked item, so the page and the
server must normalize and match every item identically. The fixture is the contract: add a case
there, never in only one language. This file only reads it.
"""

import json
from pathlib import Path

import pytest

from app.services.maintenance_checklist import checklist_item_key, normalize_checklist_item

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    REPO_ROOT / "frontend" / "src" / "utils" / "__tests__" / "fixtures" / "maintenance-checklist-normalization.json"
)


def _cases():
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


CASES = _cases()


def test_the_fixture_is_there_and_well_formed():
    assert FIXTURE.is_file(), FIXTURE
    assert len(CASES) >= 40
    for case in CASES:
        assert set(case) == {"input", "normalized", "key"}, case
        assert all(isinstance(case[field], str) for field in case), case
    inputs = [case["input"] for case in CASES]
    assert len(inputs) == len(set(inputs)), "a duplicate input proves nothing twice"


@pytest.mark.parametrize("case", CASES, ids=[repr(case["input"]) for case in CASES])
def test_normalized_and_key_match_the_fixture(case):
    assert normalize_checklist_item(case["input"]) == case["normalized"]
    assert checklist_item_key(case["input"]) == case["key"]


@pytest.mark.parametrize("case", CASES, ids=[repr(case["input"]) for case in CASES])
def test_normalization_is_idempotent(case):
    """Every seam may normalize text another seam already normalized."""
    once = normalize_checklist_item(case["input"])
    assert normalize_checklist_item(once) == once
    assert checklist_item_key(once) == checklist_item_key(case["input"])
    assert checklist_item_key(case["key"]) == case["key"]


@pytest.mark.parametrize("case", CASES, ids=[repr(case["input"]) for case in CASES])
def test_the_key_is_the_lowercased_normalized_text(case):
    assert case["key"] == case["normalized"].lower()
