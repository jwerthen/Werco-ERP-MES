"""Unit tests for estimate extraction majority-vote (Phase 4)."""

from app.services.estimate_extraction_vote import (
    CONFIRMED,
    MAJORITY,
    REVIEW,
    align_and_vote_fab_passes,
    confidence_from_agreement,
    line_vote_to_buyout_draft,
    line_vote_to_fab_draft,
    material_is_known,
    thickness_is_nonstandard,
    vote_buyout_line,
    vote_fab_line,
    vote_field,
)


def test_confidence_from_agreement():
    assert confidence_from_agreement(3, 3) == CONFIRMED
    assert confidence_from_agreement(2, 3) == MAJORITY
    assert confidence_from_agreement(1, 3) == REVIEW
    assert confidence_from_agreement(0, 3) == REVIEW


def test_vote_field_unanimous():
    fv = vote_field("bend_count", [4, 4, 4])
    assert fv.value == 4
    assert fv.agreement == 3
    assert fv.confidence == CONFIRMED


def test_vote_field_majority():
    fv = vote_field("bend_count", [4, 4, 3])
    assert fv.value == 4
    assert fv.agreement == 2
    assert fv.confidence == MAJORITY


def test_vote_field_split_is_review():
    fv = vote_field("bend_count", [1, 2, 3])
    assert fv.agreement == 1
    assert fv.confidence == REVIEW


def test_vote_field_normalizes_material_case():
    fv = vote_field("material", ["A36 Mild Steel", "a36 mild steel", "A36 Mild Steel"])
    assert fv.agreement == 3
    assert fv.confidence == CONFIRMED
    assert fv.value == "a36 mild steel"


def test_unknown_material_forces_review():
    fv = vote_field("material", ["Unobtanium-X", "Unobtanium-X", "Unobtanium-X"])
    assert fv.agreement == 3
    assert fv.confidence == REVIEW
    assert any("unknown material" in r for r in fv.anomaly_reasons)


def test_nonstandard_thickness_forces_review():
    assert thickness_is_nonstandard(0.400) is True
    assert thickness_is_nonstandard(0.075) is False
    fv = vote_field("thickness_in", [0.400, 0.400, 0.400])
    assert fv.confidence == REVIEW


def test_material_is_known():
    assert material_is_known("304 Stainless")
    assert material_is_known("5052 Aluminum")
    assert not material_is_known("Mystery Alloy")


def test_vote_fab_line_confirmed():
    base = {
        "material": "A36 Mild Steel",
        "thickness_in": 0.075,
        "width_in": 10.0,
        "length_in": 12.0,
        "cut_length_in": 44.0,
        "pierce_count": 2,
        "bend_count": 4,
        "qty": 1,
        "detail_name": "Bottom",
        "part_number": "DET-001",
    }
    voted = vote_fab_line([base, dict(base), dict(base)])
    assert voted.confidence == CONFIRMED
    draft = line_vote_to_fab_draft(voted)
    assert draft["bend_count"] == 4
    assert draft["confidence"] == CONFIRMED
    assert draft["field_confidence"]["thickness_in"]["confidence"] == CONFIRMED


def test_vote_fab_line_majority_on_bends():
    a = {
        "material": "A36",
        "thickness_in": 0.075,
        "bend_count": 4,
        "qty": 1,
        "detail_name": "Side",
    }
    b = dict(a)
    c = dict(a, bend_count=3)
    voted = vote_fab_line([a, b, c])
    assert voted.values["bend_count"] == 4
    assert voted.confidence == MAJORITY
    assert voted.verification_note


def test_vote_buyout_placeholder_without_cost():
    voted = vote_buyout_line(
        [
            {"description": "PEM nut", "qty": 10, "part_number": "CLS-032"},
            {"description": "PEM nut", "qty": 10, "part_number": "CLS-032"},
            {"description": "PEM nut", "qty": 10, "part_number": "CLS-032"},
        ]
    )
    assert voted.confidence == REVIEW
    draft = line_vote_to_buyout_draft(voted)
    assert draft["unit_cost"] == 0
    assert "quote required" in (draft["verification_note"] or "").lower()


def test_align_and_vote_fab_passes_partial_presence():
    pass0 = [{"part_number": "A", "material": "A36", "thickness_in": 0.075, "bend_count": 2, "qty": 1}]
    pass1 = [{"part_number": "A", "material": "A36", "thickness_in": 0.075, "bend_count": 2, "qty": 1}]
    pass2 = []  # line missing from pass 3
    results = align_and_vote_fab_passes([pass0, pass1, pass2])
    assert len(results) == 1
    assert results[0].confidence == REVIEW
    assert "2/3" in (results[0].verification_note or "")
