"""``planned_runs``: reading an AI-extracted laser-nest run count.

``planned_runs`` is the run (sheet repeat) count for one laser nest. It comes
back from an AI extraction pass, so the value can be an int, a whole-number
float, a numeric string, or free text -- and the coercion has to turn all of
that into a non-optional ``int >= 1`` without ever raising, because a single bad
value would otherwise 400 an entire preview batch.

Three properties are load-bearing and are what most of these cases defend:

* THE FLOOR IS NOT A READ. Everything unreadable becomes 1, and 1 is also a
  perfectly ordinary real answer, so the returned int cannot distinguish "the
  sheet says one run" from "no run count was found". Only
  ``field_confidence["planned_runs"]`` separates them, which is why the import
  wizard counts the low-confidence rows out loud. ``read_planned_runs`` returns
  ``was_read`` beside the count so the row builder can force that ``low`` itself:
  the two-pass merge scores AGREEMENT, so two passes that both answered "N/A"
  used to come out "high" on a floored 1.

* NOTHING ABOVE ``MAX_PLANNED_RUNS`` (9,999) IS A READ. An 11-digit misread used
  to pass through "high" into the import and overflow the int4 column as a 500.
  It now reads as 1, ``low``, with a warning asking for the count by hand.

* WIDENING MUST NOT BECOME GUESSING. The rule used to be int-or-digit-string
  only, which threw away every near-miss a model actually emits for this field.
  It now reads a LEADING integer out of free text -- ``"3 sheets"``, ``"x3"``,
  ``"3 of 5"`` are all an unambiguous 3 -- but a number that is merely present
  is not a count: ``"sheet 4"`` stays 1, and neither is a digit glued to a word
  (``"3abc"``). That boundary is the point of the negative cases below, and so
  is the number/string PARITY: ``2.5`` and ``"2.5"`` are the same value in two
  encodings and must coerce to the same answer, or the result depends on
  whether the model quoted its JSON number.

``TestExtractionSeam`` runs each scenario through the REAL path a preview row
takes -- ``extract_nest_fields_from_pdf`` (both AI passes, through the real
``run_llm_task`` against a scripted ``ReplayClient``) -> the two-pass merge ->
``build_parsed_nest_from_extraction`` -> ``LaserNestPreviewRow``. That seam, where
pass merging and coercion meet, is where both bugs lived; a test of the coercion
alone is the gap that hid them.

The functions added with the bound (``read_planned_runs``,
``planned_runs_bound_warning``, ``read_extracted_planned_runs``,
``MAX_PLANNED_RUNS``) are reached through their modules rather than imported by
name, so each case fails on its own against code that predates them instead of
the whole module failing to collect. ``_count`` is the floored count alone, the
number a preview row shows.

The ``llm_replay`` fixture comes from ``tests/llm_replay.py``, which
``tests/conftest.py`` registers as a plugin; only ``text_message`` is imported.
"""

import base64
import io
import json

import pytest

import app.models.laser_nest as laser_nest_model
import app.services.laser_nest_service as nest_service
from app.schemas.work_order import LaserNestPreviewRow
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf
from app.services.laser_nest_service import build_parsed_nest_from_extraction
from app.services.prompts import LASER_NEST_EXTRACTION_PROMPT, LASER_NEST_VERIFICATION_PROMPT
from tests.llm_replay import text_message

pytestmark = pytest.mark.unit


def _count(value: object) -> int:
    """The floored count a preview row shows: ``read_planned_runs(value)[0]``, an int >= 1."""
    return nest_service.read_planned_runs(value)[0]


class TestIntegers:
    """The clean path: the schema asks for an integer and usually gets one."""

    @pytest.mark.parametrize("value,expected", [(1, 1), (3, 3), (42, 42)])
    def test_positive_int_passes_through(self, value, expected):
        assert _count(value) == expected

    @pytest.mark.parametrize("value", [0, -1, -400])
    def test_non_positive_int_floors_at_one(self, value):
        # A nest is cut at least once; a 0 or a negative is a model artifact,
        # not a statement that nothing runs.
        assert _count(value) == 1

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_a_count(self, value):
        # bool is an int subclass in Python, so this has to be rejected FIRST or
        # `True` silently becomes one run and `False` becomes a floored one --
        # both dressed up as a read value.
        assert _count(value) == 1


class TestFloats:
    """JSON has one number type, so ``3`` can arrive as ``3.0``."""

    @pytest.mark.parametrize("value,expected", [(3.0, 3), (1.0, 1), (12.000, 12)])
    def test_whole_number_float_is_read(self, value, expected):
        assert _count(value) == expected

    @pytest.mark.parametrize("value", [2.5, 0.5, 3.9, -2.0])
    def test_fractional_or_non_positive_float_falls_back(self, value):
        # Half a run is not a sheet count, and rounding one would invent a
        # quantity nobody wrote. -2.0 is whole but still floors to 1.
        assert _count(value) == 1

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_falls_back_without_raising(self, value):
        # ``json.loads`` accepts NaN/Infinity by default, so these can genuinely
        # reach here; ``int(inf)`` would raise and 400 the whole preview batch.
        assert _count(value) == 1


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
        assert _count(value) == expected

    @pytest.mark.parametrize("value", ["0", "  0  ", "0.0"])
    def test_zero_string_floors_at_one(self, value):
        assert _count(value) == 1


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
        assert _count(value) == expected

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
        assert _count(value) == 1

    @pytest.mark.parametrize("value", ["3abc", "3x", "5x", "3ea"])
    def test_a_digit_glued_to_a_word_is_not_a_count(self, value):
        # The trailing word boundary. "3 sheets" is three runs; "3abc" is an
        # identifier fragment the model failed to parse.
        assert _count(value) == 1

    @pytest.mark.parametrize("value", ["2.5", "x2.5", "0.5", "3.9"])
    def test_a_fractional_string_is_refused_like_a_fractional_float(self, value):
        # Half a run is not a sheet count in EITHER encoding. This parity is the
        # property, not the individual values: reading "2.5" as 2 while 2.5
        # falls back to 1 would make the answer depend on whether the model
        # happened to quote its JSON number, which no caller can see or control.
        assert _count(value) == 1

    @pytest.mark.parametrize("number,text", [(3.0, "3.0"), (2.5, "2.5"), (12.0, "12.000"), (0.5, "0.5")])
    def test_the_number_and_string_encodings_of_one_value_agree(self, number, text):
        assert _count(number) == _count(text)


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
        assert _count(value) == 1

    def test_result_is_always_a_plain_int(self):
        for value in (3, 3.0, "3", "3 sheets", None, True, 2.5, "2.5", object()):
            result = _count(value)
            assert type(result) is int
            assert result >= 1


class TestTheBound:
    """``MAX_PLANNED_RUNS``: the owner-accepted design default, and both sides of it."""

    def test_the_bound_is_9999(self):
        # Owner decision 2026-11-09 (a design default the owner can still change).
        # Changing it is deliberate: it moves the import / manual-create / PATCH
        # refusals and the MCP ``add_laser_nest`` maximum together.
        assert laser_nest_model.MAX_PLANNED_RUNS == 9_999

    @pytest.mark.parametrize("value", [9_999, 9_999.0, "9999", "9999 sheets", "x9999"])
    def test_the_bound_itself_is_a_read(self, value):
        assert nest_service.read_planned_runs(value) == (9_999, True)
        assert nest_service.planned_runs_bound_warning(value) is None

    @pytest.mark.parametrize(
        "value",
        [10_000, 10_000.0, "10000", 99_999_999_999, "99999999999", "99999999999 sheets", 10**80, 1e300],
    )
    def test_above_the_bound_is_not_a_read_and_says_so(self, value):
        assert nest_service.read_planned_runs(value) == (1, False)
        warning = nest_service.planned_runs_bound_warning(value)
        assert warning is not None
        assert "looked wrong" in warning and "enter it by hand" in warning
        # Never clamped: 9,999 is no more a reading of "99999999999" than 1 is.
        assert _count(value) == 1

    def test_the_warning_quotes_the_value_but_clips_a_long_one(self):
        assert "'99999999999'" in nest_service.planned_runs_bound_warning(99_999_999_999)
        warning = nest_service.planned_runs_bound_warning("9" * 300)
        assert "9" * 40 + "…'" in warning
        assert "9" * 41 not in warning

    def test_a_digit_string_past_pythons_int_limit_does_not_raise(self):
        # int() refuses a digit string over 4,300 digits; the reader must not.
        assert nest_service.read_planned_runs("9" * 5_000) == (1, False)
        assert nest_service.planned_runs_bound_warning("9" * 5_000) is not None


class TestWasRead:
    """``was_read`` is True only for a clean 1..9,999 count. The floor never is."""

    @pytest.mark.parametrize(
        "value,expected",
        [(1, 1), (3, 3), (3.0, 3), ("3", 3), ("  3  ", 3), ("007", 7), ("3 sheets", 3), ("x3", 3), ("3 of 5", 3)],
    )
    def test_a_clean_count_is_read(self, value, expected):
        assert nest_service.read_planned_runs(value) == (expected, True)

    @pytest.mark.parametrize(
        "value",
        [None, "", "N/A", "unknown", "sheet 4", "3abc", 2.5, "2.5", 0, "0", -2, True, False, float("nan"), [3]],
    )
    def test_everything_else_is_the_floor_and_not_read(self, value):
        assert nest_service.read_planned_runs(value) == (1, False)
        # Only an over-the-bound count earns words; the rest are flagged by confidence.
        assert nest_service.planned_runs_bound_warning(value) is None


class TestRowBuilderFlagsTheFloor:
    """``build_parsed_nest_from_extraction`` carries ``was_read`` into the confidence."""

    def _row(self, **result):
        return build_parsed_nest_from_extraction(result, abs_path="/tmp/05749.pdf", rel_path="05749.pdf")

    def test_unread_count_is_low_even_when_the_merge_said_high(self):
        row = self._row(
            planned_runs="N/A",
            confidence={"cnc_number": "high", "material": "high", "planned_runs": "high"},
            extraction_confidence="high",
        )
        assert row.planned_runs == 1
        assert row.field_confidence == {"cnc_number": "high", "material": "high", "planned_runs": "low"}
        assert row.confidence == "low"
        assert row.warning is None

    def test_a_result_with_no_confidence_dict_still_flags_the_runs(self):
        # The preview's own degrade row (a failed extraction task) carries no
        # per-field dict at all; the floored 1 it gets must still be flagged.
        row = self._row(cnc_number=None, extraction_confidence="low")
        assert row.planned_runs == 1
        assert row.field_confidence == {"planned_runs": "low"}

    def test_the_bound_warning_joins_an_existing_one(self):
        row = self._row(
            planned_runs=99_999_999_999,
            confidence={"planned_runs": "medium"},
            extraction_confidence="medium",
            warning="Verification pass skipped: API error",
        )
        assert row.planned_runs == 1
        assert row.field_confidence == {"planned_runs": "low"}
        assert row.warning.startswith("Verification pass skipped: API error; Run count '99999999999' looked wrong")

    def test_a_clean_read_is_left_exactly_as_the_merge_scored_it(self):
        row = self._row(
            planned_runs=3,
            confidence={"planned_runs": "medium"},
            extraction_confidence="medium",
            warning=None,
        )
        assert (row.planned_runs, row.field_confidence, row.confidence, row.warning) == (
            3,
            {"planned_runs": "medium"},
            "medium",
            None,
        )


class TestTheSharedReading:
    """``read_extracted_planned_runs``: the one copy of the not-read rule.

    The preview row and ``POST /laser-nests/extract`` both take their run count,
    confidences and warning from it (the endpoint tests prove the handler delegates
    and that the two doors agree); these pin what it returns.
    """

    def test_a_clean_read_keeps_the_results_own_values(self):
        reading = nest_service.read_extracted_planned_runs(
            {
                "planned_runs": "3 sheets",
                "confidence": {"planned_runs": "medium", "material": "high"},
                "extraction_confidence": "medium",
                "warning": "Verification pass skipped: API error",
            }
        )
        assert reading == nest_service.PlannedRunsReading(
            count=3,
            was_read=True,
            confidence="medium",
            field_confidence={"planned_runs": "medium", "material": "high"},
            warning="Verification pass skipped: API error",
        )

    def test_not_read_is_low_everywhere_and_an_over_the_bound_count_says_why(self):
        reading = nest_service.read_extracted_planned_runs(
            {
                "planned_runs": 10_000,
                "confidence": {"planned_runs": "high", "material": "high"},
                "extraction_confidence": "high",
                "warning": "Verification pass skipped: API error",
            }
        )
        assert (reading.count, reading.was_read, reading.confidence) == (1, False, "low")
        assert reading.field_confidence == {"planned_runs": "low", "material": "high"}
        assert reading.warning.startswith("Verification pass skipped: API error; Run count '10000' looked wrong")

    def test_a_non_string_warning_is_dropped_not_joined(self):
        # AI-adjacent output: a junk warning shape must not reach a str field.
        reading = nest_service.read_extracted_planned_runs({"planned_runs": "N/A", "warning": {"oops": 1}})
        assert (reading.count, reading.was_read, reading.warning) == (1, False, None)
        assert reading.field_confidence == {"planned_runs": "low"}


# --------------------------------------------------------------------------- #
# The seam: extraction -> merge -> row builder -> preview schema
# --------------------------------------------------------------------------- #

# Fixture A states its run count once ("No. of Sheets: 3") and ALSO carries a parts
# table with per-sheet and job-total columns -- the 14 and 42 a confused read picks
# up instead. Fixture B states no run count at all. Every value is invented.
_FIXTURE_A_LINES = [
    (72, 740, "WERCO NEST REPORT"),
    (72, 712, "CNC Program No: 05749"),
    (72, 672, "MACHINE: Ermaksan Fiber 6kW    Grade / Material: A36"),
    (72, 632, "Thickness: 0.250 in"),
    (360, 632, "Sheet Size: 60 x 120"),
    (72, 592, "No. of Sheets: 3"),
    (72, 552, "Part No.        Qty / Sheet     Total Qty"),
    (72, 532, "BRKT-001        14              42"),
]
_FIXTURE_B_LINES = _FIXTURE_A_LINES[:5]  # the header and sheet descriptors; no count, no parts table


def _nest_pdf(lines) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    pdf.setFont("Helvetica", 11)
    for x, y, text in lines:
        pdf.drawString(x, y, text)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _reading(planned_runs) -> str:
    """One AI pass's JSON answer: every other field read cleanly and identically."""
    return json.dumps(
        {
            "cnc_number": "05749",
            "material": "A36",
            "thickness": "0.250 in",
            "sheet_size": "60 x 120",
            "planned_runs": planned_runs,
            "confidence": {
                "cnc_number": "high",
                "material": "high",
                "thickness": "high",
                "sheet_size": "high",
                "planned_runs": "high",
            },
            "extraction_confidence": "high",
        }
    )


_SEAM_SCENARIOS = [
    # id, fixture lines, pass 1, pass 2, expected runs, expected runs confidence, bound warning?
    ("A-agree", _FIXTURE_A_LINES, 3, 3, 3, "high", False),
    ("A-pass1-confused", _FIXTURE_A_LINES, 42, 3, 3, "low", False),
    ("B-both-null", _FIXTURE_B_LINES, None, None, 1, "low", False),
    ("B-both-junk", _FIXTURE_B_LINES, "N/A", "N/A", 1, "low", False),
    ("B-both-zero", _FIXTURE_B_LINES, 0, 0, 1, "low", False),
    ("huge", _FIXTURE_A_LINES, 99_999_999_999, 99_999_999_999, 1, "low", True),
    ("at-the-bound", _FIXTURE_A_LINES, 9_999, 9_999, 9_999, "high", False),
    ("one-over-the-bound", _FIXTURE_A_LINES, 10_000, 10_000, 1, "low", True),
]


class TestExtractionSeam:
    @pytest.mark.parametrize(
        "lines,pass1,pass2,expected_runs,expected_confidence,bound_warning",
        [scenario[1:] for scenario in _SEAM_SCENARIOS],
        ids=[scenario[0] for scenario in _SEAM_SCENARIOS],
    )
    def test_scenario(
        self, tmp_path, llm_replay, lines, pass1, pass2, expected_runs, expected_confidence, bound_warning
    ):
        pdf_bytes = _nest_pdf(lines)
        pdf_path = tmp_path / "05749.pdf"
        pdf_path.write_bytes(pdf_bytes)
        replay = llm_replay([text_message(_reading(pass1)), text_message(_reading(pass2))])

        result = extract_nest_fields_from_pdf(str(pdf_path), "05749.pdf", 1)
        nest = build_parsed_nest_from_extraction(result, abs_path=str(pdf_path), rel_path="05749.pdf")
        row = LaserNestPreviewRow(**nest.as_dict())

        # Both passes really ran, on the native-PDF path, through the real wrapper.
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"
        assert result["passes"] == 2
        assert row.passes == 2
        assert len(replay.requests) == 2
        for request in replay.requests:
            document = request["messages"][0]["content"][0]
            assert document["type"] == "document"
            assert base64.standard_b64decode(document["source"]["data"]) == pdf_bytes
        assert [usage.feature for usage in replay.usage_rows] == ["laser_nest_extraction", "laser_nest_verification"]
        assert [usage.prompt_version for usage in replay.usage_rows] == [
            LASER_NEST_EXTRACTION_PROMPT.version,
            LASER_NEST_VERIFICATION_PROMPT.version,
        ]
        assert all(usage.company_id == 1 and usage.success for usage in replay.usage_rows)

        # The run count, as the planner will see it.
        assert row.planned_runs == expected_runs
        assert row.field_confidence["planned_runs"] == expected_confidence
        if expected_confidence == "low":
            # The row's confidence is the minimum across its fields.
            assert row.confidence == "low"
        else:
            assert row.confidence == "high"
        if bound_warning:
            assert row.warning is not None
            assert "looked wrong" in row.warning and "enter it by hand" in row.warning
            assert str(pass1) in row.warning
        else:
            assert row.warning is None
