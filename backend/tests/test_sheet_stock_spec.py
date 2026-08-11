"""Sheet-spec parsing — the grammar the matcher and the picker both stand on.

Two things are tested here, and they fail for different reasons.

1. PARITY with ``frontend/src/utils/sheetPart.ts``. Both suites read one file,
   ``tests/fixtures/sheet_part_cases.json`` — the same file
   ``frontend/src/utils/sheetPart.parity.test.ts`` imports. There is no second
   copy to drift. A change to either port that moves an answer fails CI on both
   sides at once, which is the point: the picker's default filter runs on the TS
   grammar and the matcher's catalog parse runs on this one, so a divergence
   means the wizard hides a sheet the matcher pre-fills — and a planner confirms
   it without the row ever looking wrong.

2. The SCALAR helpers this port adds on top (``thickness_inches``,
   ``dims_inches``, ``canonical_alloy``, ``spec_key`` …). They have no TS
   counterpart because the client never compares two machine reads of the same
   physical property; the matcher does nothing else.

The scalar tests lean hard on the FAIL-CLOSED cases, because every one of them
is a number the matcher would otherwise put into a comparison: ``None`` means
unreadable and a caller that coerces it to ``0.0`` gets a value that compares
equal to nothing while looking like a real measurement.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.services.sheet_stock_spec import (
    MAX_PLAUSIBLE_DIM_IN,
    MAX_PLAUSIBLE_THICKNESS_IN,
    MIN_PLAUSIBLE_DIM_IN,
    MIN_PLAUSIBLE_THICKNESS_IN,
    SheetSpec,
    alloy_family,
    canonical_alloy,
    derive_sheet_spec,
    dims_inches,
    is_sheet_like,
    match_triple,
    normalize_part_text,
    single_dim_inches,
    spec_key,
    thickness_bucket,
    thickness_inches,
)

pytestmark = pytest.mark.unit

# THE canonical fixture. `frontend/src/utils/sheetPart.parity.test.ts` imports
# this exact path (`../../../backend/tests/fixtures/sheet_part_cases.json`).
# Do not copy it into the frontend tree — one file is the whole mechanism.
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sheet_part_cases.json"


def _load_cases() -> List[Dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


CASES = _load_cases()
CASE_IDS = [case["id"] for case in CASES]


# ---------------------------------------------------------------------------
# The shared fixture, and the parity contract it carries
# ---------------------------------------------------------------------------


class TestSharedFixture:
    def test_fixture_is_populated(self):
        """A fixture that silently empties passes both suites vacuously."""
        assert len(CASES) >= 50

    def test_every_case_has_a_unique_id(self):
        assert len(set(CASE_IDS)) == len(CASE_IDS)

    def test_every_case_explains_itself(self):
        # `why` is what tells a future reader whether a failing row is a
        # regression or an intended change.
        for case in CASES:
            assert isinstance(case["why"], str) and case["why"].strip()

    def test_every_case_carries_all_three_expectations(self):
        for case in CASES:
            assert set(case) >= {
                "part_number",
                "name",
                "description",
                "expect_sheet_like",
                "expect_thickness",
                "expect_sheet_size",
            }
            assert isinstance(case["expect_sheet_like"], bool)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_derive_sheet_spec_matches_the_shared_fixture(case):
    """``derive_sheet_spec`` answers exactly what ``deriveSheetSpec`` answers."""
    spec = derive_sheet_spec(case["part_number"], case["name"])
    assert spec == SheetSpec(
        thickness=case["expect_thickness"],
        sheet_size=case["expect_sheet_size"],
    ), f"{case['id']}: {case['why']}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_is_sheet_like_matches_the_shared_fixture(case):
    """``is_sheet_like`` answers exactly what ``isSheetLikePart`` answers."""
    assert (
        is_sheet_like(case["part_number"], case["name"], case["description"]) is case["expect_sheet_like"]
    ), f"{case['id']}: {case['why']}"


class TestThePairAsTheWizardComposesThem:
    """The safety property belongs to the PAIR, not to ``derive_sheet_spec``."""

    @staticmethod
    def _misreads() -> List[Dict[str, Any]]:
        return [c for c in CASES if not c["expect_sheet_like"] and c["expect_thickness"] is not None]

    def test_the_fixture_still_contains_a_live_misread(self):
        # If this list ever empties, the guard below stops proving anything: it
        # would be asserting that nothing happens to rows that do not exist.
        assert self._misreads(), "fixture no longer covers a spec the grammar alone mis-reads"

    def test_a_non_sheet_part_stamps_nothing_even_when_the_grammar_matches(self):
        for case in self._misreads():
            # The grammar alone DOES read a spec off these...
            assert derive_sheet_spec(case["part_number"], case["name"]).thickness == case["expect_thickness"]
            # ...and the sheet-likeness gate is the only thing keeping angle-iron
            # (or a hex-screw callout) dimensions off a nest row.
            assert is_sheet_like(case["part_number"], case["name"], case["description"]) is False


# ---------------------------------------------------------------------------
# normalize / match_triple — the shared primitives
# ---------------------------------------------------------------------------


class TestNormalizePartText:
    def test_uppercases_and_collapses_whitespace(self):
        assert normalize_part_text("  0.188   thk  x 60 ") == "0.188 X 60"

    def test_drops_the_thk_and_inches_noise_words(self):
        assert normalize_part_text('0.188" THK x 60 x 120') == "0.188 X 60 X 120"
        assert normalize_part_text("0.125 inches x 48 x 96") == "0.125 X 48 X 96"

    def test_handles_none_without_throwing(self):
        assert normalize_part_text(None) == ""


class TestMatchTriple:
    def test_gauge_form_is_tried_before_the_decimal_form(self):
        # `10GA-72X120` would otherwise read as decimal thickness "10".
        assert match_triple("10GA-72X120-CS") == SheetSpec(thickness="10 ga", sheet_size="72x120")

    def test_the_anchor_is_the_entire_safeguard(self):
        # Un-anchored, every angle and tube part number in the catalog matches.
        assert match_triple("ANG-A36-1.5X1.5X.25") is None
        # The same angle, described by its NAME rather than its part number, so
        # the triple lands at the front. The grammar happily reads it — which is
        # why `is_sheet_like` exists and why the wizard consults both.
        assert match_triple("1.50 X 1.50 X 0.250 A36 ANGLE") == SheetSpec(thickness="1.50", sheet_size="1.50x0.250")


# ---------------------------------------------------------------------------
# thickness_inches — the hard gate's input
# ---------------------------------------------------------------------------


class TestThicknessInches:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("0.250 in", 0.25),
            ("10 ga", 0.1345),
            ("10ga", 0.1345),
            ("1/4", 0.25),
            (".25", 0.25),
            ("0.1345", 0.1345),
        ],
    )
    def test_reads_every_form_the_shop_writes(self, value, expected):
        assert thickness_inches(value) == pytest.approx(expected)

    def test_reads_millimetres(self):
        assert thickness_inches("4mm") == pytest.approx(4 / 25.4)

    def test_a_bare_number_is_never_promoted_to_a_gauge(self):
        """ "16" is 16 inches arithmetically and 16 ga to a human. It is neither.

        This is the single most important fail-closed case in the module: a bare
        number reads as 16.0 INCHES, which is not plate anyone lasers, and the
        plausibility bound is what turns that into a clean "unreadable" the
        matcher already refuses on. Never make this infer gauge.
        """
        assert thickness_inches("16") is None

    def test_a_gauge_outside_the_table_is_unreadable_not_guessed(self):
        # 9ga and 13ga are absent from GAUGE_TO_INCHES. Interpolating them would
        # be inventing a thickness.
        assert thickness_inches("9ga") is None
        assert thickness_inches("13 ga") is None

    @pytest.mark.parametrize("value", ["", "   ", None, "unknown", "as noted"])
    def test_empty_and_prose_are_unreadable(self, value):
        assert thickness_inches(value) is None

    def test_bounds_are_inclusive_at_the_edges_and_closed_outside(self):
        assert thickness_inches(str(MIN_PLAUSIBLE_THICKNESS_IN)) == pytest.approx(MIN_PLAUSIBLE_THICKNESS_IN)
        assert thickness_inches(str(MAX_PLAUSIBLE_THICKNESS_IN)) == pytest.approx(MAX_PLAUSIBLE_THICKNESS_IN)
        assert thickness_inches("0.0049") is None
        assert thickness_inches("4.1") is None

    def test_none_is_never_a_zero(self):
        # A caller that coerces this to 0.0 gets a number that compares equal to
        # nothing while looking like a measurement.
        assert thickness_inches("16") is not 0.0  # noqa: F632 - identity is the point
        assert thickness_inches("16") is None


# ---------------------------------------------------------------------------
# dims_inches / single_dim_inches
# ---------------------------------------------------------------------------


class TestDimsInches:
    def test_sorts_ascending_so_the_two_orders_normalize(self):
        """A nest reading 60 x 120 and a part numbered ...-120X60 are one sheet.

        Neither order is wrong and neither side is authoritative, so the pair is
        normalized rather than one being preferred.
        """
        assert dims_inches("60 x 120") == (60.0, 120.0)
        assert dims_inches("144x60") == (60.0, 144.0)
        assert dims_inches("60 x 120") == dims_inches("120X60")

    @pytest.mark.parametrize("value", ["60x120", "60 x 120", "60X120", "60 X 120", "60×120", "60*120"])
    def test_accepts_every_separator_in_the_data(self, value):
        assert dims_inches(value) == (60.0, 120.0)

    @pytest.mark.parametrize("value", ["", "   ", None, "72", "one sheet"])
    def test_returns_none_when_there_is_no_pair(self, value):
        assert dims_inches(value) is None

    def test_rejects_physically_absurd_dimensions(self):
        assert dims_inches("0.5x120") is None  # below MIN_PLAUSIBLE_DIM_IN
        assert dims_inches("1000x2000") is None  # above MAX_PLAUSIBLE_DIM_IN
        assert dims_inches(f"{MIN_PLAUSIBLE_DIM_IN:g}x{MAX_PLAUSIBLE_DIM_IN:g}") == (
            MIN_PLAUSIBLE_DIM_IN,
            MAX_PLAUSIBLE_DIM_IN,
        )


class TestSingleDimInches:
    def test_reads_a_lone_number(self):
        assert single_dim_inches("72") == 72.0
        assert single_dim_inches("72.5") == 72.5

    def test_refuses_a_pair_so_the_two_helpers_never_both_answer(self):
        # A pair is stronger evidence and has its own reader; answering here too
        # would let one sheet size feed both the exact and the one-dim score.
        assert single_dim_inches("60x120") is None
        assert single_dim_inches("144 X 60") is None

    @pytest.mark.parametrize("value", ["", "   ", None, "as noted"])
    def test_returns_none_when_there_is_no_number(self, value):
        assert single_dim_inches(value) is None

    def test_rejects_physically_absurd_dimensions(self):
        assert single_dim_inches("0.5") is None
        assert single_dim_inches("1000") is None


# ---------------------------------------------------------------------------
# canonical_alloy / alloy_family — how under-specification is RECOGNIZED
# ---------------------------------------------------------------------------


class TestCanonicalAlloy:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0.06X60X144-304SS", "304"),
            ("304SS", "304"),
            ("SS304", "304"),
            ("6061-T6", "6061"),
            ("5052-H32", "5052"),
            ("0.188-72X144-A36", "A36"),
            ("10GA-72X120-CS", "CS"),
            ("316L", "316L"),
            ("A572-50", "A572"),
            ("Cold Rolled sheet", "CRS"),
            ("Hot Rolled", "HRS"),
            ("mild steel", "A36"),
            ("carbon steel", "CS"),
        ],
    )
    def test_canonicalizes_the_grades_this_shop_writes(self, text, expected):
        assert canonical_alloy(text) == expected

    def test_longest_token_wins_so_a_grade_is_never_shortened(self):
        # `304L` must not come back as `304`, and `A572` must not read as `A5`.
        assert canonical_alloy("0.250-60X120-304L") == "304L"
        assert canonical_alloy("0.1875-60X120-A572-50-RYERSON") == "A572"

    @pytest.mark.parametrize("text", ["SPECS PLATE", "Discs and plates"])
    def test_does_not_find_cs_inside_an_unrelated_word(self, text):
        """The exact false positive a bare substring test for ``CS`` produced.

        `CS` is two letters and the catalog is full of prose. A false grade is
        worse than no grade: an absent one can never earn a pre-fill, while a
        wrong one silently drops every correct candidate at the alloy gate.
        """
        assert canonical_alloy(text) is None

    @pytest.mark.parametrize("text", ["", "   ", None, "SS", "Stainless", "Aluminum"])
    def test_returns_none_when_no_grade_is_stated(self, text):
        assert canonical_alloy(text) is None

    def test_never_reads_a_grade_out_of_the_dimensions(self):
        # The leading triple is stripped before grade matching precisely because
        # these digit runs collide with the short numeric tokens.
        assert canonical_alloy("0.316-60X120") is None
        assert canonical_alloy("6061-60X120") is None


class TestAlloyFamily:
    @pytest.mark.parametrize("text,expected", [("SS", "SS"), ("Stainless Steel", "SS"), ("Aluminum", "AL")])
    def test_recognizes_a_family_stated_without_a_grade(self, text, expected):
        assert alloy_family(text) == expected

    @pytest.mark.parametrize("text", ["304SS", "SS304", "6061-T6", "A36"])
    def test_is_none_once_a_grade_is_present(self, text):
        """A family is a statement that the grade was NOT stated.

        `304SS` names both; the grade is the answer and there is nothing
        under-specified about it, so the matcher must not be told to refuse.
        """
        assert alloy_family(text) is None

    @pytest.mark.parametrize("text", ["", "   ", None, "hot rolled coil"])
    def test_returns_none_when_no_family_is_named(self, text):
        assert alloy_family(text) is None


# ---------------------------------------------------------------------------
# thickness_bucket / spec_key — the read-time grouping key
# ---------------------------------------------------------------------------


class TestThicknessBucket:
    def test_snaps_to_a_gauge_when_within_tolerance(self):
        assert thickness_bucket("10ga") == "10ga"
        assert thickness_bucket("0.1345") == "10ga"
        assert thickness_bucket("0.105") == "12ga"  # 12ga is 0.1046

    def test_folds_a_trailing_zero(self):
        assert thickness_bucket("0.25") == thickness_bucket("0.250") == "0.2500"

    def test_unreadable_buckets_to_a_question_mark_not_to_zero(self):
        assert thickness_bucket("16") == "?"
        assert thickness_bucket(None) == "?"


class TestSpecKey:
    def test_collapses_trailing_zeros(self):
        assert spec_key("A36", "0.25", "60x120") == spec_key("A36", "0.250", "60x120")

    def test_collapses_dimension_order(self):
        assert spec_key("A36", "0.250", "144x60") == spec_key("A36", "0.250", "60x144")

    def test_collapses_gauge_against_its_decimal(self):
        assert spec_key("CS", "10ga", "60x120") == spec_key("CS", "0.1345", "120x60")

    def test_all_three_collapses_at_once(self):
        assert spec_key("A36", "0.25", "144x60") == spec_key("A36", "0.250", "60x144") == "A36|0.2500|60x144"

    def test_keeps_different_specs_apart(self):
        assert spec_key("A36", "0.250", "60x120") != spec_key("304", "0.250", "60x120")
        assert spec_key("A36", "0.250", "60x120") != spec_key("A36", "0.1875", "60x120")
        assert spec_key("A36", "0.250", "60x120") != spec_key("A36", "0.250", "48x96")

    def test_an_unreadable_triple_is_a_recognizable_sentinel(self):
        # `_load_history` skips this key rather than letting every unparseable
        # nest in the tenant share one history bucket.
        assert spec_key(None, None, None) == "?|?|?"

    def test_a_family_stands_in_when_no_grade_is_stated(self):
        assert spec_key("SS", "0.125", "60x120").startswith("SS|")
        assert spec_key("304SS", "0.125", "60x120").startswith("304|")


class TestAlloyGradeIsNeverReadOutOfADimension:
    """A digit run inside a thickness or a dimension is not a grade.

    Found in review: ``PL-0.304X60X120`` -- a 0.304" CARBON plate -- came back as
    grade ``304``, i.e. stainless. That is a wrong-GRADE match, the single failure
    mode the matcher's alloy gate exists to prevent, and it would have let a nest
    calling for 304 pre-fill a carbon plate.

    Two separate holes produced it and both are pinned here: ``.`` counts as a
    word boundary (so the whole-token pass read the thickness), and the
    compound-spelling pass was an unanchored digit search over a string that is
    mostly digits.
    """

    @pytest.mark.parametrize(
        "part_number",
        [
            "PL-0.304X60X120",  # 0.304" plate, not 304 stainless
            "SHT-0.316-60X120",  # 0.316" sheet, not 316
            "PL .304 THICK STEEL",
            "0.316 PLATE",
        ],
    )
    def test_a_decimal_thickness_is_not_a_grade(self, part_number):
        assert canonical_alloy(part_number) is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            # The compound spellings the second pass genuinely exists for: no word
            # boundary separates the grade from the family.
            ("0.06X60X144-304SS", "304"),
            ("0.125-48X96-304SS", "304"),
            ("SS304", "304"),
            # ...and the ordinary forms, which must keep working.
            ("304 SS", "304"),
            ("SHT-304-125", "304"),
            ("304 Stainless Sheet", "304"),
            ("0.250-60X120-A36", "A36"),
            ("10GA-72X120-CS", "CS"),
            ("0.1875-60X120-A572-50-RYERSON", "A572"),
            ("PLATE 6061 T6", "6061"),
            ("5052-H32", "5052"),
            ("304L", "304L"),
            ("17-4PH", "17-4PH"),
        ],
    )
    def test_real_grades_still_resolve(self, text, expected):
        assert canonical_alloy(text) == expected
