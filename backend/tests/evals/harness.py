"""Shared helpers for the AI eval harness (marker: evals)."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"

LIVE_EVALS_ENABLED = os.getenv("RUN_LIVE_EVALS") == "1" and bool(os.getenv("ANTHROPIC_API_KEY"))

requires_live_evals = pytest.mark.skipif(
    not LIVE_EVALS_ENABLED,
    reason="Live evals are opt-in: set RUN_LIVE_EVALS=1 and ANTHROPIC_API_KEY to hit the real API",
)


def load_cases(task: str) -> List[Dict[str, Any]]:
    cases = []
    for path in sorted(GOLDEN_DIR.glob(f"{task}_*.json")):
        with path.open() as handle:
            cases.append(json.load(handle))
    return cases


def case_ids(cases: List[Dict[str, Any]]) -> List[str]:
    return [case["id"] for case in cases]
