"""Golden-fixture evals for the PO and BOM extraction pipelines.

Offline mode (default) scores the stored golden outputs — it validates the
scoring plumbing and pins the expected quality bar without any API key or
network access. Live mode (RUN_LIVE_EVALS=1 + ANTHROPIC_API_KEY) re-runs each
case against the real API and scores the fresh output against ground truth.
"""

import pytest

from .harness import case_ids, load_cases, requires_live_evals
from .scoring import assert_thresholds, score_bom_extraction, score_po_extraction

pytestmark = pytest.mark.evals

PO_CASES = load_cases("po_extraction")
BOM_CASES = load_cases("bom_extraction")


def _strip_metadata(output: dict) -> dict:
    return {key: value for key, value in (output or {}).items() if not key.startswith("_")}


class TestPOExtractionOffline:
    @pytest.mark.parametrize("case", PO_CASES, ids=case_ids(PO_CASES))
    def test_stored_output_meets_thresholds(self, case):
        scores = score_po_extraction(case["expected"], _strip_metadata(case["stored_output"]))
        assert_thresholds(scores, case["thresholds"], case_id=case["id"])


class TestBOMExtractionOffline:
    @pytest.mark.parametrize("case", BOM_CASES, ids=case_ids(BOM_CASES))
    def test_stored_output_meets_thresholds(self, case):
        scores = score_bom_extraction(case["expected"], _strip_metadata(case["stored_output"]))
        assert_thresholds(scores, case["thresholds"], case_id=case["id"])


class TestScoringPlumbing:
    """Sanity checks that the scorers actually fail bad outputs."""

    def test_po_scorer_catches_missing_line_items(self):
        case = PO_CASES[0]
        broken = _strip_metadata(dict(case["stored_output"]))
        broken["line_items"] = []
        scores = score_po_extraction(case["expected"], broken)
        assert scores["line_item_recall"] == 0.0
        with pytest.raises(AssertionError):
            assert_thresholds(scores, case["thresholds"], case_id=case["id"])

    def test_po_scorer_catches_wrong_total(self):
        case = PO_CASES[0]
        broken = _strip_metadata(dict(case["stored_output"]))
        broken["total_amount"] = 999999.99
        scores = score_po_extraction(case["expected"], broken)
        assert scores["header_accuracy"] < 1.0

    def test_bom_scorer_catches_wrong_quantity(self):
        case = BOM_CASES[0]
        broken = _strip_metadata(dict(case["stored_output"]))
        broken["items"] = [dict(item, quantity=777) for item in broken["items"]]
        scores = score_bom_extraction(case["expected"], broken)
        assert scores["item_field_accuracy"] < 1.0


@requires_live_evals
class TestPOExtractionLive:
    @pytest.mark.parametrize("case", PO_CASES, ids=case_ids(PO_CASES))
    def test_live_extraction_meets_thresholds(self, case):
        from app.services.llm_service import extract_po_data_with_llm

        actual = extract_po_data_with_llm(case["input_text"], is_ocr=case.get("is_ocr", False))
        assert not actual.get("_error"), f"Live extraction failed: {actual.get('_error')}"
        scores = score_po_extraction(case["expected"], _strip_metadata(actual))
        assert_thresholds(scores, case.get("live_thresholds", case["thresholds"]), case_id=case["id"])


@requires_live_evals
class TestBOMExtractionLive:
    @pytest.mark.parametrize("case", BOM_CASES, ids=case_ids(BOM_CASES))
    def test_live_extraction_meets_thresholds(self, case):
        from app.services.llm_service import extract_bom_data_with_llm

        actual = extract_bom_data_with_llm(case["input_text"], is_ocr=case.get("is_ocr", False))
        assert not actual.get("_error"), f"Live extraction failed: {actual.get('_error')}"
        scores = score_bom_extraction(case["expected"], _strip_metadata(actual))
        assert_thresholds(scores, case.get("live_thresholds", case["thresholds"]), case_id=case["id"])
