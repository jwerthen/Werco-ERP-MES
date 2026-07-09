"""Tests for Estimate Workbench RFQ → draft extraction (deterministic path)."""

from unittest.mock import MagicMock, patch

from app.services.estimate_extraction_vote import MAJORITY, REVIEW
from app.services.estimate_workbench_extraction_service import (
    build_workbench_draft_from_votes,
    deterministic_pass_from_parsed,
    drafts_from_deterministic_only,
    extract_workbench_draft_from_rfq,
)


def test_deterministic_pass_from_parsed_splits_fab_and_buy():
    parsed = {
        "parts": [
            {
                "part_id": "P-1",
                "part_name": "Panel",
                "line_type": "manufactured",
                "item_type": "make",
                "material": "A36 Mild Steel",
                "thickness_in": 0.075,
                "qty": 2,
                "bend_count": 4,
                "cut_length": 40,
                "hole_count": 2,
                "bbox": {"width": 10, "length": 10},
            },
            {
                "part_id": "HW-1",
                "part_name": "PEM nut",
                "line_type": "hardware",
                "item_type": "buy",
                "qty": 10,
            },
        ],
        "hardware_items": [
            {"part_number": "CLS-032", "description": "Clinchnut", "qty": 8},
        ],
    }
    fab, buy = deterministic_pass_from_parsed(parsed)
    assert len(fab) == 1
    assert fab[0]["bend_count"] == 4
    assert fab[0]["thickness_in"] == 0.075
    assert len(buy) >= 2  # hardware_items + purchased part


def test_drafts_from_deterministic_never_confirmed():
    parsed = {
        "parts": [
            {
                "part_id": "P-1",
                "part_name": "Panel",
                "line_type": "manufactured",
                "material": "A36",
                "thickness_in": 0.075,
                "qty": 1,
                "bend_count": 2,
                "confidence": {"material": 0.9, "thickness": 0.9, "geometry": 0.9},
                "sources": {"bom": ["x"], "drawing_pdf": ["y"], "flat_pattern_dxf": ["z"]},
            }
        ],
        "hardware_items": [],
        "warnings": [],
        "assumptions": [],
    }
    det = drafts_from_deterministic_only(parsed)
    assert det["mode"] == "deterministic"
    assert len(det["fab_lines"]) == 1
    # Two+ sources + high scores → Majority, never Confirmed without triple LLM
    assert det["fab_lines"][0]["confidence"] == MAJORITY


def test_build_workbench_draft_from_votes_triple_pass():
    line = {
        "detail_name": "Bottom",
        "part_number": "DET-1",
        "material": "A36 Mild Steel",
        "qty": 1,
        "thickness_in": 0.075,
        "bend_count": 4,
        "pierce_count": 0,
    }
    draft = build_workbench_draft_from_votes(
        [[line], [line], [dict(line, bend_count=3)]],
        [[], [], []],
        assembly_name="RFQ-1",
        mode="triple_pass",
    )
    assert draft["summary"]["fab_count"] == 1
    fab = draft["assemblies"][0]["fab_lines"][0]
    assert fab["bend_count"] == 4
    assert fab["confidence"] == MAJORITY


def test_extract_workbench_draft_deterministic_flag():
    pkg = MagicMock()
    pkg.id = 7
    pkg.rfq_number = "RFQ-7"
    pkg.files = [MagicMock()]

    parsed = {
        "parts": [
            {
                "part_id": "A",
                "part_name": "Bracket",
                "line_type": "manufactured",
                "material": "304 Stainless",
                "thickness_in": 0.075,
                "qty": 1,
                "bend_count": 1,
                "confidence": {"material": 0.5, "thickness": 0.5, "geometry": 0.2},
                "sources": {"bom": ["bom.xlsx"]},
            }
        ],
        "hardware_items": [],
        "warnings": ["low text"],
        "assumptions": [],
        "source_attribution": {"bom": True},
    }

    db = MagicMock()
    query = db.query.return_value
    query.options.return_value.filter.return_value.first.return_value = pkg

    with patch(
        "app.services.estimate_workbench_extraction_service.parse_rfq_package_files",
        return_value=parsed,
    ):
        draft = extract_workbench_draft_from_rfq(db, rfq_package_id=7, company_id=1, use_llm=False)

    assert draft["mode"] == "deterministic"
    assert draft["assemblies"][0]["name"] == "RFQ-7"
    assert draft["assemblies"][0]["fab_lines"][0]["confidence"] == REVIEW
    assert "low text" in draft["warnings"]
