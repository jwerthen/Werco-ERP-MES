"""Unit tests for ``laser_nest_service._coerce_planned_runs``.

``planned_runs`` is the run (sheet repeat) count for one laser nest. It comes
back from an AI extraction pass, so the value can be an int, a whole-number
float, a numeric string, or free text -- and the coercion has to turn all of
that into a non-optional ``int >= 1`` without ever raising, because a single bad
value would otherwise 400 an entire preview batch.

Two properties are load-bearing and are what most of these cases defend:

* THE FLOOR IS NOT A READ. Everything unreadable becomes 1, and 1 is also a
  perfectly ordinary real answer, so the returned int cannot distinguish "the
  sheet says one run" from "no run count was found". Only
  ``field_confidence["planned_runs"]`` separates them, which is why the import
  wizard counts the low-confidence rows out loud. Nothing here should be
  "simplified" into inventing a confidence signal from the number.

* WIDENING MUST NOT BECOME GUESSING. The rule used to be int-or-digit-string
  only, which threw away every near-miss a model actually emits for this field.
  It now reads a LEADING integer out of free text -- ``"3 sheets"``, ``"x3"``,
  ``"3 of 5"`` are all an unambiguous 3 -- but a number that is merely present
  is not a count: ``"sheet 4"`` stays 1, and neither is a digit glued to a word
  (``"3abc"``). That boundary is the point of the negative cases below, and so
  is the number/string PARITY: ``2.5`` and ``"2.5"`` are the same value in two
  encodings and must coerce to the same answer, or the result depends on
  whether the model quoted its JSON number.
"""

import pytest

from app.services.laser_nest_service import _coerce_planned_runs

pytestmark = pytest.mark.unit


class TestIntegers:
    """The clean path: the schema asks for an integer and usually gets one."""

    @pytest.mark.parametrize("value,expected", [(1, 1), (3, 3), (42, 42)])
    def test_positive_int_passes_through(self, value, expected):
        assert _coerce_planned_runs(value) == expected

    @pytest.mark.parametrize("value", [0, -1, -400])
    def test_non_positive_int_floors_at_one(self, value):
        # A nest is cut at least once; a 0 or a negative is a model artifact,
        # not a statement that nothing runs.
        assert _coerce_planned_runs(value) == 1

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_a_count(self, value):
        # bool is an int subclass in Python, so this has to be rejected FIRST or
        # `True` silently becomes one run and `False` becomes a floored one --
        # both dressed up as a read value.
        assert _coerce_planned_runs(value) == 1


class TestFloats:
    """JSON has one number type, so ``3`` can arrive as ``3.0``."""

    @pytest.mark.parametrize("value,expected", [(3.0, 3), (1.0, 1), (12.000, 12)])
    def test_whole_number_float_is_read(self, value, expected):
        assert _coerce_planned_runs(value) == expected

    @pytest.mark.parametrize("value", [2.5, 0.5, 3.9, -2.0])
    def test_fractional_or_non_positive_float_falls_back(self, value):
        # Half a run is not a sheet count, and rounding one would invent a
        # quantity nobody wrote. -2.0 is whole but still floors to 1.
        assert _coerce_planned_runs(value) == 1

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_falls_back_without_raising(self, value):
        # ``json.loads`` accepts NaN/Infinity by default, so these can genuinely
        # reach here; ``int(inf)`` would raise and 400 the whole preview batch.
        assert _coerce_planned_runs(value) == 1


class TestNumericStrings:
    """The shapes a model emits when it answers in text rather than JSON number."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("3", 3),
            ("12", 12),
            ("007", 7),  # zero-padded, the way CAM prints program numbers
            ("  3  ", 3),  # stray whitespace
            ("3.0", 3),
            ("3.00", 3),
        ],
    )
    def test_digit_strings_are_read(self, value, expected):
        assert _coerce_planned_runs(value) == expected

    @pytest.mark.parametrize("value", ["0", "  0  ", "0.0"])
    def test_zero_string_floors_at_one(self, value):
        assert _coerce_planned_runs(value) == 1


class TestFreeText:
    """A LEADING integer is a count; a number appearing later is a label."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("3 sheets", 3),
            ("3 Sheets Required", 3),
            ("x3", 3),
            ("X3", 3),
            ("x 3", 3),
            ("  x3", 3),
            ("3 of 5", 3),  # "3 of 5" is three runs, not five
            ("x3.0", 3),
            ("3-5", 3),
        ],
    )
    def test_leading_integer_is_read(self, value, expected):
        assert _coerce_planned_runs(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "sheet 4",  # a label with a number, not a count
            "sheets 3",
            "run 2 of 7",
            "qty: 3",
            "Nest 12",
        ],
    )
    def test_a_number_that_is_not_leading_is_not_a_count(self, value):
        # This is the whole safeguard on the widened rule. "sheet 4" is nest
        # sheet number four; reading it as four runs would quadruple a job.
        assert _coerce_planned_runs(value) == 1

    @pytest.mark.parametrize("value", ["3abc", "3x", "5x", "3ea"])
    def test_a_digit_glued_to_a_word_is_not_a_count(self, value):
        # The trailing word boundary. "3 sheets" is three runs; "3abc" is an
        # identifier fragment the model failed to parse.
        assert _coerce_planned_runs(value) == 1

    @pytest.mark.parametrize("value", ["2.5", "x2.5", "0.5", "3.9"])
    def test_a_fractional_string_is_refused_like_a_fractional_float(self, value):
        # Half a run is not a sheet count in EITHER encoding. This parity is the
        # property, not the individual values: reading "2.5" as 2 while 2.5
        # falls back to 1 would make the answer depend on whether the model
        # happened to quote its JSON number, which no caller can see or control.
        assert _coerce_planned_runs(value) == 1

    @pytest.mark.parametrize("number,text", [(3.0, "3.0"), (2.5, "2.5"), (12.0, "12.000"), (0.5, "0.5")])
    def test_the_number_and_string_encodings_of_one_value_agree(self, number, text):
        assert _coerce_planned_runs(number) == _coerce_planned_runs(text)


class TestUnreadable:
    """Never raises. Anything it cannot read becomes the floor."""

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "abc",
            "unknown",
            "n/a",
            "-",
            {"planned_runs": 3},
            [3],
            (),
            object(),
        ],
    )
    def test_junk_falls_back_to_one(self, value):
        # A bad model value must not ValueError its way into a 400 that sinks
        # the whole preview batch -- one unreadable field costs one defaulted
        # row, flagged by its confidence, and nothing else.
        assert _coerce_planned_runs(value) == 1

    def test_result_is_always_a_plain_int(self):
        for value in (3, 3.0, "3", "3 sheets", None, True, 2.5, "2.5", object()):
            result = _coerce_planned_runs(value)
            assert type(result) is int
            assert result >= 1
