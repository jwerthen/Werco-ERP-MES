"""Native-PDF laser-nest extraction eval.

This eval measures per-field accuracy (cnc_number, material, thickness,
sheet_size) of the NATIVE-PDF extraction path -- the one that hands Claude the
rendered PDF as a base64 ``document`` block so it reads each field from its own
labeled position on the 2-D sheet. That layout-aware path is what fixes the
glued-digits / material-grade-on-the-wrong-line failures of the old text-flatten
baseline; this case is built precisely to exercise that win (material grade on a
separate machine line, numeric fields in distinct blocks -- see nest_fixtures).

Fixtures are SYNTHESIZED with reportlab (no real customer content): a digital
text-layer nest PDF and an image-only ("scanned") variant with the same known
values. Both are well under ``_MAX_NATIVE_PDF_BYTES``, so both ride the native
document path (Claude vision reads the image-only PDF directly).

Offline mode (default, no API key): builds the synthetic fixtures and exercises
the scorer plumbing -- it proves the fixtures construct and that the scorer
actually fails wrong output, without any network call. Live mode
(RUN_LIVE_EVALS=1 + ANTHROPIC_API_KEY) writes each fixture to disk, runs the
real native-PDF extraction, and scores the fresh output against ground truth.
"""

import tempfile
from pathlib import Path

import pytest

from .harness import requires_live_evals
from .nest_fixtures import build_digital_nest_pdf, build_scanned_nest_pdf
from .scoring import assert_thresholds, score_laser_nest_extraction

pytestmark = pytest.mark.evals


def _strip_metadata(output: dict) -> dict:
    return {key: value for key, value in (output or {}).items() if not key.startswith("_")}


# Per-field thresholds for the live native-PDF run. All four primary fields are
# expected to land exactly on a clean synthetic sheet; field_accuracy is the mean.
_LIVE_THRESHOLDS = {
    "cnc_number_match": 1.0,
    "material_match": 1.0,
    "thickness_match": 1.0,
    "sheet_size_match": 1.0,
    "field_accuracy": 1.0,
}

# The image-only ("scanned") variant is held to a slightly looser bar: vision on a
# rasterized sheet is reliable but a stray glyph (e.g. a degree/inch mark) can cost
# one field. Require the CNC number exactly and >=75% of fields overall.
_LIVE_SCANNED_THRESHOLDS = {
    "cnc_number_match": 1.0,
    "field_accuracy": 0.75,
}


class TestNestFixtureSynthesis:
    """Offline: the synthetic fixtures construct and look like PDFs."""

    def test_digital_fixture_builds(self):
        fixture = build_digital_nest_pdf()
        assert fixture.pdf_bytes.startswith(b"%PDF"), "digital fixture must be a PDF"
        assert len(fixture.pdf_bytes) > 200
        assert set(fixture.expected) == {"cnc_number", "material", "thickness", "sheet_size"}

    def test_scanned_fixture_builds(self):
        fixture = build_scanned_nest_pdf()
        assert fixture.pdf_bytes.startswith(b"%PDF"), "scanned fixture must be a PDF"
        # Image-only PDFs are much larger than the text-layer version.
        assert len(fixture.pdf_bytes) > len(build_digital_nest_pdf().pdf_bytes)


class TestNestScoringPlumbing:
    """Offline: the per-field scorer actually fails wrong extractions."""

    def test_scorer_perfect_on_exact_match(self):
        fixture = build_digital_nest_pdf()
        scores = score_laser_nest_extraction(fixture.expected, dict(fixture.expected))
        assert scores["field_accuracy"] == 1.0
        assert_thresholds(scores, _LIVE_THRESHOLDS, case_id="self-check")

    def test_scorer_catches_wrong_material(self):
        fixture = build_digital_nest_pdf()
        wrong = dict(fixture.expected)
        wrong["material"] = "304SS"  # not A36
        scores = score_laser_nest_extraction(fixture.expected, wrong)
        assert scores["material_match"] == 0.0
        assert scores["field_accuracy"] < 1.0
        with pytest.raises(AssertionError):
            assert_thresholds(scores, _LIVE_THRESHOLDS, case_id="self-check")

    def test_scorer_is_case_and_space_insensitive(self):
        # The scorer normalizes case/whitespace, so these still match.
        expected = {"cnc_number": "05749", "material": "A36", "thickness": "0.250 in", "sheet_size": "60 x 120"}
        actual = {"cnc_number": "05749", "material": " a36 ", "thickness": "0.250 in", "sheet_size": "60 x 120"}
        scores = score_laser_nest_extraction(expected, actual)
        assert scores["field_accuracy"] == 1.0


@requires_live_evals
class TestNativePdfExtractionLive:
    """Live: run the real native-PDF extraction and score the four fields."""

    def _run(self, fixture):
        from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / fixture.file_name
            pdf_path.write_bytes(fixture.pdf_bytes)
            return extract_nest_fields_from_pdf(str(pdf_path), fixture.file_name, company_id=None)

    def test_digital_native_pdf_extracts_fields(self):
        fixture = build_digital_nest_pdf()
        actual = self._run(fixture)
        # The service never raises; on a live failure it degrades with a warning.
        assert not actual.get("warning"), f"Live extraction degraded: {actual.get('warning')}"
        # Confirm we actually took the native-PDF path, not the text fallback.
        assert actual["_extraction_metadata"]["input_mode"] == "native_pdf"
        scores = score_laser_nest_extraction(fixture.expected, _strip_metadata(actual))
        assert_thresholds(scores, _LIVE_THRESHOLDS, case_id="laser_nest_digital_native")

    def test_scanned_image_only_pdf_extracts_fields(self):
        fixture = build_scanned_nest_pdf()
        actual = self._run(fixture)
        assert not actual.get("warning"), f"Live extraction degraded: {actual.get('warning')}"
        # An image-only PDF under the size cap still rides the native document path
        # (Claude vision reads the raster directly).
        assert actual["_extraction_metadata"]["input_mode"] == "native_pdf"
        scores = score_laser_nest_extraction(fixture.expected, _strip_metadata(actual))
        assert_thresholds(scores, _LIVE_SCANNED_THRESHOLDS, case_id="laser_nest_scanned_native")
