"""Canonical spelling for laser-nest sheet descriptors (``services/laser_nest_text``).

These fields have no ``Part`` FK behind them, so the STRING is the only grouping
key anything has. A 2026-08-06 production reconciliation found the same physical
sheet split across two rows on whitespace alone (``144x60`` vs ``144 x 60``) --
25 output rows for 19 real sheet specs, which makes every group under-report and
look like a smaller number rather than like an error.

The tests are organized around the module's one hard rule: **normalize spelling,
never meaning.** The "preserved" cases are as load-bearing as the "normalized"
ones -- each marks a transform that would be convenient and is wrong.
"""

import pytest

from app.services.laser_nest_text import (
    normalize_material,
    normalize_nest_descriptors,
    normalize_sheet_size,
    normalize_thickness,
)


class TestNormalizeSheetSize:
    """The transform the module exists for: separator spelling, not the numbers."""

    @pytest.mark.parametrize(
        "raw",
        [
            "144x60",
            "144 x 60",
            "144X60",
            "144 X 60",
            "144×60",  # U+00D7, as a PDF extraction emits it
            "144*60",  # what survives an ASCII-stripping program
            "  144   x   60  ",
        ],
    )
    def test_every_spelling_collapses_onto_one_key(self, raw):
        """THE regression. All seven spell the same sheet; all must group together."""
        assert normalize_sheet_size(raw) == "144 x 60"

    def test_decimal_dimensions_are_preserved_exactly(self):
        # 72.5 must not become 72, and 144.125 must not be rounded -- these are
        # real sheet dimensions off the nest program.
        assert normalize_sheet_size("144.125x72.5") == "144.125 x 72.5"

    def test_trailing_zero_on_a_dimension_is_preserved(self):
        # Meaning-preserving normalization only: 72.50 states precision.
        assert normalize_sheet_size("120x72.50") == "120 x 72.50"

    def test_leading_zero_is_added_to_a_bare_decimal(self):
        assert normalize_sheet_size(".5x.25") == "0.5 x 0.25"

    def test_trailing_unit_mark_survives(self):
        assert normalize_sheet_size('48 x 96"') == '48 x 96"'
        assert normalize_sheet_size("48x96 IN") == "48 x 96in"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Not two dimensions -- must pass through with whitespace collapsed
            # and NOTHING else touched. Mangling an unrecognized descriptor into
            # something tidy and wrong is worse than leaving it alone.
            ("drop   only", "drop only"),
            ("48 x 96 x 12", "48 x 96 x 12"),
            ("remnant", "remnant"),
        ],
    )
    def test_unrecognized_values_pass_through_unmangled(self, raw, expected):
        assert normalize_sheet_size(raw) == expected


class TestNormalizeThickness:
    def test_gauge_unit_lowercases_and_closes_up(self):
        assert normalize_thickness("16 GA") == "16ga"
        assert normalize_thickness("16ga") == "16ga"
        assert normalize_thickness("16 Gauge") == "16gauge"

    def test_leading_zero_is_added(self):
        assert normalize_thickness(".25") == "0.25"

    def test_trailing_zeros_are_PRESERVED(self):
        """Deliberate: on a manufacturing thickness the trailing digits state
        precision, and an as-built record is entitled to the figure as the
        program stated it. The accepted cost is that 0.25 and 0.250 still group
        apart -- do not "fix" this by stripping zeros."""
        assert normalize_thickness("0.250") == "0.250"
        assert normalize_thickness("0.2500") == "0.2500"

    def test_bare_number_gains_no_unit(self):
        # No unit inference: 16 is not promoted to 16ga.
        assert normalize_thickness("16") == "16"

    def test_inch_mark_keeps_its_glyph(self):
        assert normalize_thickness('0.25"') == '0.25"'

    def test_unrecognized_value_passes_through_collapsed(self):
        assert normalize_thickness("heavy   plate") == "heavy plate"


class TestNormalizeMaterial:
    def test_uppercases_and_collapses(self):
        assert normalize_material("a36") == "A36"
        assert normalize_material("  304  ss  ") == "304 SS"

    def test_abbreviations_are_NOT_expanded(self):
        """SS could be 304 or 316 and the nest does not say. Expanding it would
        put a fact in the record that no one asserted."""
        assert normalize_material("SS") == "SS"


class TestEmptyAndNone:
    @pytest.mark.parametrize("fn", [normalize_material, normalize_thickness, normalize_sheet_size])
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_and_blank_both_become_none(self, fn, raw):
        """A blank descriptor and an absent one are the same fact; storing ''
        would make them group apart, and the nest write paths already use the
        ``or None`` idiom."""
        assert fn(raw) is None


class TestIdempotence:
    """Applied twice must equal applied once -- the seams deliberately overlap
    (the extraction mapper normalizes, then the package build re-normalizes the
    planner-edited row), so a non-idempotent transform would corrupt on the
    second pass."""

    @pytest.mark.parametrize(
        "material,thickness,sheet_size",
        [
            ("a36", "0.250", "144x60"),
            ("SS", "16 GA", "120 × 48"),
            ("  crs ", ".125", '48 x 96"'),
            (None, None, None),
            ("weird", "heavy plate", "remnant"),
        ],
    )
    def test_second_pass_is_a_no_op(self, material, thickness, sheet_size):
        once = normalize_nest_descriptors(material, thickness, sheet_size)
        twice = normalize_nest_descriptors(*once)
        assert once == twice


class TestNormalizeNestDescriptors:
    def test_returns_all_three_in_order(self):
        assert normalize_nest_descriptors("a36", "16 GA", "144x60") == ("A36", "16ga", "144 x 60")

    def test_the_production_fragmentation_case(self):
        """The two rows that split in the 2026-08-06 reconciliation now agree."""
        assert normalize_nest_descriptors("A36", "0.25", "144x60") == normalize_nest_descriptors(
            "A36", "0.25", "144 x 60"
        )
