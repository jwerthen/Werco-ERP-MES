"""Unit tests for ``segment_nest_pdf`` — AI pass 0 for bare multi-page uploads.

Offline by contract, mirroring ``test_laser_nest_extraction.py``: the service's
``_read_pdf_bytes`` and ``run_llm_task`` are stubbed at the import site, so no
real PDF read or Anthropic call happens.

Pinned behaviors:
  * a VALID grouping response is accepted (normalized: nests sorted by first
    page, hints coerced to str-or-None, confidence coerced into the vocabulary);
  * every strict-validation failure — overlapping pages, missing pages,
    non-consecutive pages, zero nests, out-of-range/bool pages, duplicate
    skipped pages, non-dict payloads — DEGRADES to one-nest-per-page with a
    warning (never raises, never merges nests);
  * a single-page PDF short-circuits without touching the LLM (or the bytes);
  * LLM failures of every flavor (unconfigured, egress disabled, bad JSON, API
    error, catch-all) degrade with a flavor-specific warning;
  * an over-cap byte size degrades without an LLM call.
"""

import httpx
import pytest

import app.services.laser_nest_extraction_service as svc
from app.services.laser_nest_extraction_service import _MAX_NATIVE_PDF_BYTES, segment_nest_pdf
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError, is_anthropic_api_error

pytestmark = pytest.mark.unit

_FAKE_PDF_BYTES = b"%PDF-1.4 fake multi-page nest report"


def _stub_pdf_bytes(monkeypatch, data: bytes = _FAKE_PDF_BYTES) -> None:
    monkeypatch.setattr(svc, "_read_pdf_bytes", lambda path: data)


def _stub_llm_text(monkeypatch, response_text: str):
    """Fixed-response run_llm_task stub that records call count + kwargs."""
    from types import SimpleNamespace

    calls = {"n": 0, "kwargs": None}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            text=response_text,
            model="claude-stub",
            tier="stub",
            model_selection_reason="test",
            prompt_version="1.0.0",
        )

    monkeypatch.setattr(svc, "run_llm_task", _fake)
    return calls


def _stub_llm_raises(monkeypatch, exc: BaseException) -> None:
    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(svc, "run_llm_task", _raise)


def _stub_llm_must_not_run(monkeypatch) -> None:
    _stub_llm_raises(monkeypatch, AssertionError("run_llm_task must NOT be called on this path"))


def _default_shape(page_count: int) -> list[dict]:
    return [{"pages": [page], "cnc_number_hint": None} for page in range(1, page_count + 1)]


# --------------------------------------------------------------------------- #
# Valid responses are accepted (and normalized)
# --------------------------------------------------------------------------- #
class TestValidGroupingAccepted:
    def test_valid_grouping_with_skipped_page(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        calls = _stub_llm_text(
            monkeypatch,
            '{"nests": [{"pages": [1, 2], "cnc_number_hint": "05749"}, {"pages": [3], "cnc_number_hint": null}],'
            ' "skipped_pages": [4], "confidence": "high"}',
        )

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 4, company_id=1)

        assert result["nests"] == [
            {"pages": [1, 2], "cnc_number_hint": "05749"},
            {"pages": [3], "cnc_number_hint": None},
        ]
        assert result["skipped_pages"] == [4]
        assert result["confidence"] == "high"
        assert result["warning"] is None
        assert calls["n"] == 1

    def test_nests_sorted_by_first_page_even_if_response_out_of_order(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(
            monkeypatch,
            '{"nests": [{"pages": [3]}, {"pages": [1, 2]}], "skipped_pages": [], "confidence": "medium"}',
        )

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 3)

        assert [nest["pages"] for nest in result["nests"]] == [[1, 2], [3]]

    def test_blank_hint_coerced_to_none_and_junk_confidence_coerced_to_low(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(
            monkeypatch,
            '{"nests": [{"pages": [1], "cnc_number_hint": "   "}, {"pages": [2], "cnc_number_hint": "  05750 "}],'
            ' "skipped_pages": [], "confidence": "very sure"}',
        )

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        assert result["nests"][0]["cnc_number_hint"] is None
        assert result["nests"][1]["cnc_number_hint"] == "05750"
        assert result["confidence"] == "low"
        assert result["warning"] is None

    def test_json_fence_is_stripped(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(
            monkeypatch,
            '```json\n{"nests": [{"pages": [1]}, {"pages": [2]}], "skipped_pages": [], "confidence": "high"}\n```',
        )

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        assert result["warning"] is None
        assert len(result["nests"]) == 2


# --------------------------------------------------------------------------- #
# Strict validation failures DEGRADE to one nest per page
# --------------------------------------------------------------------------- #
class TestValidationDegrades:
    @pytest.mark.parametrize(
        ("payload", "page_count"),
        [
            # A page claimed by two nests.
            ('{"nests": [{"pages": [1, 2]}, {"pages": [2, 3]}], "skipped_pages": [], "confidence": "high"}', 3),
            # A page in a nest AND in skipped_pages.
            ('{"nests": [{"pages": [1]}, {"pages": [2]}], "skipped_pages": [2, 3], "confidence": "high"}', 3),
            # A page covered nowhere.
            ('{"nests": [{"pages": [1]}, {"pages": [2]}], "skipped_pages": [], "confidence": "high"}', 3),
            # Non-consecutive pages inside a nest.
            ('{"nests": [{"pages": [1, 3]}, {"pages": [2]}], "skipped_pages": [], "confidence": "high"}', 3),
            # Descending pages inside a nest.
            ('{"nests": [{"pages": [2, 1]}, {"pages": [3]}], "skipped_pages": [], "confidence": "high"}', 3),
            # Zero nests.
            ('{"nests": [], "skipped_pages": [1, 2, 3], "confidence": "high"}', 3),
            # All pages skipped (zero preview rows would dead-end the wizard).
            ('{"nests": [], "skipped_pages": [1, 2], "confidence": "high"}', 2),
            # Out-of-range page.
            (
                '{"nests": [{"pages": [1]}, {"pages": [2]}, {"pages": [5]}], "skipped_pages": [3], "confidence": "high"}',
                3,
            ),
            # Bool masquerading as a page number.
            ('{"nests": [{"pages": [true]}, {"pages": [2]}], "skipped_pages": [], "confidence": "high"}', 2),
            # Duplicate skipped pages.
            ('{"nests": [{"pages": [1]}], "skipped_pages": [2, 2], "confidence": "high"}', 2),
            # Nest entry not a dict.
            ('{"nests": [[1, 2]], "skipped_pages": [], "confidence": "high"}', 2),
            # Payload not an object.
            ('[{"pages": [1]}]', 2),
            # nests key missing entirely.
            ('{"skipped_pages": [], "confidence": "high"}', 2),
        ],
        ids=[
            "overlapping_nests",
            "page_in_nest_and_skipped",
            "missing_page",
            "non_consecutive",
            "descending",
            "zero_nests",
            "all_pages_skipped",
            "out_of_range_page",
            "bool_page",
            "duplicate_skipped",
            "nest_not_a_dict",
            "payload_not_object",
            "missing_nests_key",
        ],
    )
    def test_invalid_response_degrades_to_one_nest_per_page(self, monkeypatch, payload, page_count):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, payload)

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", page_count, company_id=1)

        assert result["nests"] == _default_shape(page_count)
        assert result["skipped_pages"] == []
        assert result["confidence"] == "low"
        assert "failed validation" in result["warning"]
        assert "one nest per page" in result["warning"]


# --------------------------------------------------------------------------- #
# Single page: no LLM call at all
# --------------------------------------------------------------------------- #
class TestSinglePageShortCircuit:
    def test_single_page_skips_the_llm_entirely(self, monkeypatch):
        _stub_llm_must_not_run(monkeypatch)
        # Even the bytes must not be read — the short-circuit precedes the try.
        monkeypatch.setattr(
            svc, "_read_pdf_bytes", lambda path: pytest.fail("_read_pdf_bytes must not run for a single page")
        )

        result = segment_nest_pdf("/tmp/one.pdf", "one.pdf", 1, company_id=1)

        assert result == {
            "nests": [{"pages": [1], "cnc_number_hint": None}],
            "skipped_pages": [],
            "confidence": "high",
            "warning": None,
        }

    def test_zero_page_count_also_short_circuits(self, monkeypatch):
        # Defensive: callers 400 earlier on a 0-page PDF, but <=1 short-circuits.
        _stub_llm_must_not_run(monkeypatch)
        result = segment_nest_pdf("/tmp/zero.pdf", "zero.pdf", 0)
        assert result["nests"] == [{"pages": [1], "cnc_number_hint": None}]
        assert result["warning"] is None


# --------------------------------------------------------------------------- #
# LLM failures degrade with a flavor-specific warning
# --------------------------------------------------------------------------- #
class TestLlmFailureDegrades:
    def _assert_default(self, result, page_count):
        assert result["nests"] == _default_shape(page_count)
        assert result["skipped_pages"] == []
        assert result["confidence"] == "low"

    @pytest.mark.parametrize("reason", ["library", "api_key"])
    def test_not_configured_degrades(self, monkeypatch, reason):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMNotConfiguredError(reason))

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 3)

        self._assert_default(result, 3)
        if reason == "library":
            assert "library" in result["warning"]
        else:
            assert "API key" in result["warning"]

    def test_egress_disabled_degrades(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMEgressDisabledError(company_id=1))

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2, company_id=1)

        self._assert_default(result, 2)
        assert "allow_ai_egress" in result["warning"]

    def test_invalid_json_degrades(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, "not json {")

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        self._assert_default(result, 2)
        assert "Invalid JSON" in result["warning"]

    def test_anthropic_api_error_degrades(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        anthropic = pytest.importorskip("anthropic")
        api_error = anthropic.APIConnectionError(
            message="connection reset",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        assert is_anthropic_api_error(api_error)
        _stub_llm_raises(monkeypatch, api_error)

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        self._assert_default(result, 2)
        assert "API error" in result["warning"]

    def test_unexpected_error_degrades(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, RuntimeError("kaboom"))

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        self._assert_default(result, 2)
        assert "Segmentation failed" in result["warning"]

    def test_unreadable_bytes_degrade(self, monkeypatch):
        def _raise(path):
            raise FileNotFoundError("gone")

        monkeypatch.setattr(svc, "_read_pdf_bytes", _raise)
        _stub_llm_must_not_run(monkeypatch)

        result = segment_nest_pdf("/tmp/nests.pdf", "nests.pdf", 2)

        self._assert_default(result, 2)
        assert "Segmentation failed" in result["warning"]


# --------------------------------------------------------------------------- #
# Oversized PDF: degrade without an LLM call
# --------------------------------------------------------------------------- #
class TestOversizedPdf:
    def test_over_cap_bytes_degrade_without_llm_call(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch, b"x" * (_MAX_NATIVE_PDF_BYTES + 1))
        _stub_llm_must_not_run(monkeypatch)

        result = segment_nest_pdf("/tmp/huge.pdf", "huge.pdf", 3, company_id=1)

        assert result["nests"] == _default_shape(3)
        assert result["confidence"] == "low"
        assert "too large" in result["warning"]
