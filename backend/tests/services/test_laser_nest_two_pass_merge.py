"""Unit tests for the TWO-PASS merge policy in ``extract_nest_fields_from_pdf``.

The extraction runs an independent verification read (pass 2) after a
successful pass-1 parse and merges the two per field:

  * agree (case-insensitive / stripped; ``planned_runs`` numerically, so ``3``
    agrees with ``"3"``)      -> PASS 1's value, confidence "high"
  * exactly one side null/blank -> the non-null value, "medium"
  * both non-null, different    -> the VERIFIER's value, "low"
  * both null                   -> null, "low"

``confidence`` becomes the merged per-field dict, ``extraction_confidence`` the
MINIMUM across fields, ``passes`` = 2. A pass-2 failure of ANY kind keeps the
pass-1 result byte-for-byte untouched with ``passes = 1`` and a skip warning.

Offline like ``test_laser_nest_extraction.py``: ``_read_pdf_bytes`` and
``run_llm_task`` are stubbed at the service import site; the stub returns a
DIFFERENT payload for call 1 (extraction) vs call 2 (verification).
"""

import json
from types import SimpleNamespace

import pytest

import app.services.laser_nest_extraction_service as svc
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf

pytestmark = pytest.mark.unit

_FAKE_PDF_BYTES = b"%PDF-1.4 fake nest report bytes"

_MERGE_FIELDS = ("cnc_number", "material", "thickness", "sheet_size", "planned_runs")


def _stub_pdf_bytes(monkeypatch) -> None:
    monkeypatch.setattr(svc, "_read_pdf_bytes", lambda path: _FAKE_PDF_BYTES)


class _SequencedLLM:
    """run_llm_task stub returning a different response per call, in order.

    An entry may be a string (returned as ``.text``) or an exception instance
    (raised). Records every call's kwargs so tests can assert the verification
    pass's telemetry/prompt wiring.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"ctx": args[0] if args else None, "kwargs": kwargs})
        index = len(self.calls) - 1
        entry = self.responses[index]
        if isinstance(entry, BaseException):
            raise entry
        return SimpleNamespace(
            text=entry,
            model=f"claude-stub-pass{index + 1}",
            tier="stub",
            model_selection_reason="test",
            prompt_version=f"{index + 1}.0.0",
        )


def _stub_llm_sequence(monkeypatch, *responses) -> _SequencedLLM:
    stub = _SequencedLLM(responses)
    monkeypatch.setattr(svc, "run_llm_task", stub)
    return stub


def _payload(**overrides) -> str:
    """A full extraction/verification JSON payload with per-field overrides."""
    base = {
        "cnc_number": "05749",
        "material": "A36",
        "thickness": "0.25in",
        "sheet_size": "72.5x120",
        "planned_runs": 3,
        "confidence": {field: "high" for field in _MERGE_FIELDS},
        "extraction_confidence": "high",
    }
    base.update(overrides)
    return json.dumps(base)


def _extract(monkeypatch, pass1: str, pass2) -> tuple[dict, _SequencedLLM]:
    _stub_pdf_bytes(monkeypatch)
    stub = _stub_llm_sequence(monkeypatch, pass1, pass2)
    result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)
    return result, stub


# --------------------------------------------------------------------------- #
# Field-level merge matrix
# --------------------------------------------------------------------------- #
class TestMergePolicyMatrix:
    def test_exact_agreement_is_high_and_keeps_pass1_values(self, monkeypatch):
        result, stub = _extract(monkeypatch, _payload(), _payload())

        assert stub.calls and len(stub.calls) == 2
        for field in _MERGE_FIELDS:
            assert result["confidence"][field] == "high"
        assert result["extraction_confidence"] == "high"
        assert result["passes"] == 2
        assert result["warning"] is None
        assert result["cnc_number"] == "05749"
        assert result["planned_runs"] == 3

    def test_case_and_whitespace_insensitive_agreement_keeps_pass1_verbatim(self, monkeypatch):
        """' a36 ' from the verifier AGREES with pass 1's 'A36' — and the merged
        value is pass 1's exact string, never the normalized compare form."""
        result, _ = _extract(monkeypatch, _payload(material="A36"), _payload(material="  a36  "))

        assert result["material"] == "A36"
        assert result["confidence"]["material"] == "high"

    def test_planned_runs_string_vs_int_agree_numerically(self, monkeypatch):
        """Verifier says '3' (string), pass 1 said 3 (int): numeric agreement,
        high confidence, pass-1's int kept."""
        result, _ = _extract(monkeypatch, _payload(planned_runs=3), _payload(planned_runs="3"))

        assert result["planned_runs"] == 3
        assert result["confidence"]["planned_runs"] == "high"
        assert result["extraction_confidence"] == "high"

    def test_planned_runs_int_vs_string_pass1_string_kept(self, monkeypatch):
        """The symmetric case: pass 1 said '3' (string) — agreement keeps the
        pass-1 value as-is (the string), it does not adopt the verifier's int."""
        result, _ = _extract(monkeypatch, _payload(planned_runs="3"), _payload(planned_runs=3))

        assert result["planned_runs"] == "3"
        assert result["confidence"]["planned_runs"] == "high"

    def test_pass1_null_takes_verifier_value_at_medium(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(material=None), _payload(material="304SS"))

        assert result["material"] == "304SS"
        assert result["confidence"]["material"] == "medium"
        assert result["extraction_confidence"] == "medium"

    def test_verifier_null_keeps_pass1_value_at_medium(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(sheet_size="72.5x120"), _payload(sheet_size=None))

        assert result["sheet_size"] == "72.5x120"
        assert result["confidence"]["sheet_size"] == "medium"

    def test_verifier_blank_string_counts_as_null(self, monkeypatch):
        """A whitespace-only verifier value is 'not pinned', not a conflict."""
        result, _ = _extract(monkeypatch, _payload(thickness="0.25in"), _payload(thickness="   "))

        assert result["thickness"] == "0.25in"
        assert result["confidence"]["thickness"] == "medium"

    def test_conflict_takes_verifier_value_at_low(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(material="A36"), _payload(material="A572"))

        assert result["material"] == "A572"
        assert result["confidence"]["material"] == "low"
        assert result["extraction_confidence"] == "low"

    def test_both_null_stays_null_at_low(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(sheet_size=None), _payload(sheet_size=None))

        assert result["sheet_size"] is None
        assert result["confidence"]["sheet_size"] == "low"
        assert result["extraction_confidence"] == "low"

    def test_extraction_confidence_is_min_across_fields(self, monkeypatch):
        """One medium field among agreements pulls the overall to medium; adding
        a conflict pulls it to low."""
        result, _ = _extract(monkeypatch, _payload(material=None), _payload(material="304SS"))
        assert result["extraction_confidence"] == "medium"

        result, _ = _extract(
            monkeypatch,
            _payload(material=None, thickness="0.25in"),
            _payload(material="304SS", thickness="0.375in"),
        )
        # material: one-sided (medium); thickness: conflict (low) -> min is low.
        assert result["confidence"]["material"] == "medium"
        assert result["confidence"]["thickness"] == "low"
        assert result["extraction_confidence"] == "low"

    def test_merged_confidence_replaces_pass1_self_reported_confidence(self, monkeypatch):
        """The per-field dict comes from the AGREEMENT PATTERN, not either pass's
        self-reported confidence — a pass-1 'low' self-rating on an agreed field
        still merges to 'high'."""
        pass1 = _payload(confidence={field: "low" for field in _MERGE_FIELDS})
        result, _ = _extract(monkeypatch, pass1, _payload())

        assert result["confidence"] == {field: "high" for field in _MERGE_FIELDS}

    def test_cnc_number_agreement_keeps_source_ai(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(), _payload())
        assert result["source"] == "ai"

    def test_both_passes_null_cnc_falls_back_to_filename(self, monkeypatch):
        """When NEITHER pass pins the CNC number the filename stem takes over
        (after the merge), flipping source to 'filename'."""
        result, _ = _extract(monkeypatch, _payload(cnc_number=None), _payload(cnc_number=None))

        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert result["confidence"]["cnc_number"] == "low"


# --------------------------------------------------------------------------- #
# Pass-2 failure: pass 1 survives untouched
# --------------------------------------------------------------------------- #
class TestVerificationFailureKeepsPassOne:
    PASS1 = None  # set in _assert_pass1_untouched

    def _assert_pass1_untouched(self, result):
        assert result["cnc_number"] == "05749"
        assert result["material"] == "A36"
        assert result["thickness"] == "0.25in"
        assert result["sheet_size"] == "72.5x120"
        assert result["planned_runs"] == 3
        # Pass 1's self-reported per-field confidence is NOT replaced.
        assert result["confidence"] == {field: "high" for field in _MERGE_FIELDS}
        assert result["extraction_confidence"] == "high"
        assert result["passes"] == 1
        assert result["warning"] is not None
        assert "Verification pass skipped" in result["warning"]
        assert result["source"] == "ai"

    def test_pass2_exception_keeps_pass1(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(), RuntimeError("verifier exploded"))
        self._assert_pass1_untouched(result)
        assert "verifier exploded" in result["warning"]

    def test_pass2_invalid_json_keeps_pass1(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(), "not json at all {")
        self._assert_pass1_untouched(result)
        assert "invalid JSON" in result["warning"]

    def test_pass2_non_object_json_keeps_pass1(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(), '["not", "an", "object"]')
        self._assert_pass1_untouched(result)

    def test_pass2_success_records_two_passes_and_verification_metadata(self, monkeypatch):
        result, stub = _extract(monkeypatch, _payload(), _payload())

        assert result["passes"] == 2
        assert result["warning"] is None
        meta = result["_extraction_metadata"]
        # Pass-1 telemetry unchanged...
        assert meta["model"] == "claude-stub-pass1"
        # ...plus the verification pass's own model/prompt version.
        assert meta["verification_model"] == "claude-stub-pass2"
        assert meta["verification_prompt_version"] == "2.0.0"

        # The verification call is telemetry-tagged as its own feature and sees
        # pass 1's fields labeled comparison-only.
        verify_kwargs = stub.calls[1]["kwargs"]
        assert verify_kwargs["feature"] == "laser_nest_verification"
        instruction_blocks = verify_kwargs["messages"][0]["content"]
        text_block = next(block["text"] for block in instruction_blocks if block.get("type") == "text")
        assert "comparison ONLY" in text_block
        assert "05749" in text_block

    def test_pass2_failure_leaves_no_verification_metadata(self, monkeypatch):
        result, _ = _extract(monkeypatch, _payload(), RuntimeError("nope"))
        meta = result["_extraction_metadata"]
        assert "verification_model" not in meta
        assert "verification_prompt_version" not in meta
