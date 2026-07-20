"""Unit tests for the laser-nest PDF field extraction service.

Offline by contract: ``_read_pdf_bytes`` and ``run_llm_task`` are stubbed at the
service's import site, so no real PDF reading or Anthropic API call ever happens
(evals are opt-in via RUN_LIVE_EVALS and out of scope here).

The service now has TWO input paths:

* NATIVE path (default): the PDF bytes are sent to Claude as a base64
  ``document`` content block (layout-aware vision). This runs whenever the raw
  bytes are <= ``_MAX_NATIVE_PDF_BYTES``. ``extract_text_from_document`` is NOT
  called on this path.
* TEXT fallback: only when the bytes exceed the native cap is the PDF flattened
  via ``extract_text_from_document`` and the OCR-aware user prompt used.

The behaviors pinned here are the service contract: native-document request
shape, clean-JSON parse, ```json fence stripping, the filename fallback for an
empty ``cnc_number``, the text-fallback path past the size cap, and the
never-raises degrade paths (fatal read, LLM not configured, invalid JSON,
Anthropic API error, catch-all).
"""

from types import SimpleNamespace

import httpx
import pytest

import app.services.laser_nest_extraction_service as svc
from app.services.laser_nest_extraction_service import _MAX_NATIVE_PDF_BYTES, extract_nest_fields_from_pdf
from app.services.llm_client import LLMEgressDisabledError, LLMNotConfiguredError, is_anthropic_api_error

pytestmark = pytest.mark.unit

# A tiny, valid-looking PDF byte string for the native path. Content is never
# parsed (the model would do that live); it just has to be non-empty bytes under
# the native size cap.
_FAKE_PDF_BYTES = b"%PDF-1.4 fake nest report bytes"


def _stub_pdf_bytes(monkeypatch, data: bytes = _FAKE_PDF_BYTES) -> None:
    """Make ``_read_pdf_bytes`` return fixed bytes (exercises the native path)."""
    monkeypatch.setattr(svc, "_read_pdf_bytes", lambda path: data)


def _stub_pdf_bytes_raises(monkeypatch, exc: BaseException) -> None:
    """Make ``_read_pdf_bytes`` raise (missing file, s3 hiccup) -- fatal to both paths."""

    def _raise(path):
        raise exc

    monkeypatch.setattr(svc, "_read_pdf_bytes", _raise)


def _stub_extraction(monkeypatch, *, text: str = "CNC 05749 ... A36 ... T 0.25", is_ocr: bool = False) -> None:
    """Make ``extract_text_from_document`` return a fixed DocumentExtractionResult-like.

    Only relevant on the TEXT fallback path (bytes > native cap).
    """
    monkeypatch.setattr(
        svc,
        "extract_text_from_document",
        lambda path: SimpleNamespace(text=text, is_ocr=is_ocr),
    )


class _LLMRecorder:
    """Captures the LLMTaskContext (first positional arg) and kwargs run_llm_task got."""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = 0
        self.ctx = None
        self.kwargs = None

    def __call__(self, *args, **kwargs):
        self.calls += 1
        # The service calls run_llm_task(ctx, messages=..., system=..., ...).
        self.ctx = args[0] if args else kwargs.get("context")
        self.kwargs = kwargs
        return SimpleNamespace(
            text=self.response_text,
            model="claude-stub",
            tier="stub",
            model_selection_reason="test",
            prompt_version="1.0.0",
        )


def _stub_llm_text(monkeypatch, response_text: str) -> _LLMRecorder:
    """Make ``run_llm_task`` return a fixed ``.text`` and record what it received."""
    recorder = _LLMRecorder(response_text)
    monkeypatch.setattr(svc, "run_llm_task", recorder)
    return recorder


def _stub_llm_raises(monkeypatch, exc: BaseException) -> None:
    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(svc, "run_llm_task", _raise)


def _stub_extraction_must_not_run(monkeypatch) -> None:
    def _boom(path):
        raise AssertionError("extract_text_from_document must NOT run on the native path")

    monkeypatch.setattr(svc, "extract_text_from_document", _boom)


def _document_block(messages):
    """Pull the single ``document`` content block out of a native-path message list."""
    content = messages[0]["content"]
    assert isinstance(content, list), "native path must send a list-of-blocks content"
    docs = [block for block in content if block.get("type") == "document"]
    assert len(docs) == 1, f"expected exactly one document block, got {len(docs)}"
    return docs[0]


# --------------------------------------------------------------------------- #
# Native path: the PDF rides in a base64 document block
# --------------------------------------------------------------------------- #
class TestNativeDocumentRequest:
    def test_native_path_sends_base64_pdf_document_block(self, monkeypatch):
        recorder = _stub_llm_text(monkeypatch, '{"cnc_number": "05749", "extraction_confidence": "high"}')
        _stub_pdf_bytes(monkeypatch)
        # If the native path is taken, the text extractor must NOT be touched.
        _stub_extraction_must_not_run(monkeypatch)

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        # Two LLM calls on the native path: the extraction read plus the
        # independent verification read (both carry the same document block).
        assert recorder.calls == 2

        # Context flags lift model selection off the FAST tier without any prompt text.
        assert recorder.ctx.task == "laser_nest_extraction"
        assert recorder.ctx.has_pdf_document is True
        assert recorder.ctx.input_chars == 0
        assert recorder.ctx.is_ocr is False

        # The messages carry a single base64 application/pdf document block.
        block = _document_block(recorder.kwargs["messages"])
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "application/pdf"
        data = block["source"]["data"]
        assert isinstance(data, str) and data, "document data must be a non-empty base64 string"

        # And it decodes back to the bytes we fed in.
        import base64

        assert base64.standard_b64decode(data) == _FAKE_PDF_BYTES

        # Metadata records the native input mode.
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"
        assert result["_extraction_metadata"]["source_was_ocr"] is False


# --------------------------------------------------------------------------- #
# Happy path: clean JSON parses into the expected fields (native path)
# --------------------------------------------------------------------------- #
class TestCleanParse:
    def test_clean_json_maps_to_fields(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        recorder = _stub_llm_text(
            monkeypatch,
            """{
                "cnc_number": "05749",
                "material": "A36",
                "thickness": "0.25in",
                "sheet_size": "72.5x120",
                "planned_runs": 3,
                "confidence": {"cnc_number": "high", "material": "high"},
                "extraction_confidence": "high"
            }""",
        )

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        assert result["cnc_number"] == "05749"
        assert result["material"] == "A36"
        assert result["thickness"] == "0.25in"
        assert result["sheet_size"] == "72.5x120"
        assert result["planned_runs"] == 3
        assert result["extraction_confidence"] == "high"
        # AI pinned the CNC number, so the source is "ai" and there is no warning.
        assert result["source"] == "ai"
        assert result["warning"] is None
        assert result["_extraction_metadata"]["model"] == "claude-stub"
        assert result["_extraction_metadata"]["source_was_ocr"] is False
        # Native path is the default.
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"
        assert recorder.ctx.has_pdf_document is True


# --------------------------------------------------------------------------- #
# Fence stripping (native path)
# --------------------------------------------------------------------------- #
class TestFenceStripping:
    def test_strips_json_code_fence(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(
            monkeypatch,
            '```json\n{"cnc_number": "05751", "material": "304SS", "extraction_confidence": "high"}\n```',
        )

        result = extract_nest_fields_from_pdf("/tmp/05751.pdf", "05751.pdf")

        assert result["cnc_number"] == "05751"
        assert result["material"] == "304SS"
        assert result["source"] == "ai"
        assert result["warning"] is None
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"

    def test_strips_bare_triple_backtick_fence(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, '```\n{"cnc_number": "05752", "extraction_confidence": "low"}\n```')

        result = extract_nest_fields_from_pdf("/tmp/05752.pdf", "05752.pdf")

        assert result["cnc_number"] == "05752"
        assert result["source"] == "ai"


# --------------------------------------------------------------------------- #
# Filename fallback when the model can't pin the CNC number (native path)
# --------------------------------------------------------------------------- #
class TestFilenameFallback:
    @pytest.mark.parametrize("cnc_value", ["", None])
    def test_empty_or_missing_cnc_falls_back_to_filename_stem(self, monkeypatch, cnc_value):
        _stub_pdf_bytes(monkeypatch)
        if cnc_value is None:
            payload = '{"material": "A36", "extraction_confidence": "low"}'
        else:
            payload = '{"cnc_number": "", "material": "A36", "extraction_confidence": "low"}'
        _stub_llm_text(monkeypatch, payload)

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf")

        # Filename stem becomes the CNC number; source flips to "filename".
        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        # Material the model DID return is preserved; this is not the empty path.
        assert result["material"] == "A36"
        assert result["warning"] is None

    def test_fallback_strips_directory_and_extension(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, '{"cnc_number": null, "extraction_confidence": "low"}')

        result = extract_nest_fields_from_pdf("/tmp/whatever.pdf", "NEST-12345.PDF")

        assert result["cnc_number"] == "NEST-12345"
        assert result["source"] == "filename"


# --------------------------------------------------------------------------- #
# Text fallback: only past the native size cap
# --------------------------------------------------------------------------- #
class TestTextFallbackPastSizeCap:
    def test_oversized_pdf_uses_text_path(self, monkeypatch):
        """A PDF larger than the native cap flattens to text and uses the OCR-aware
        prompt; the document block is NOT used and input_mode is 'text'."""
        _stub_pdf_bytes(monkeypatch, b"x" * (_MAX_NATIVE_PDF_BYTES + 1))
        _stub_extraction(monkeypatch, text="CNC 05749 A36 0.25", is_ocr=False)
        recorder = _stub_llm_text(
            monkeypatch, '{"cnc_number": "05749", "material": "A36", "extraction_confidence": "high"}'
        )

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        assert result["cnc_number"] == "05749"
        assert result["material"] == "A36"
        assert result["_extraction_metadata"]["input_mode"] == "text"
        # On the text path the context is built from the flattened text, not a document.
        assert recorder.ctx.has_pdf_document is False
        assert recorder.ctx.input_chars > 0
        # The user prompt is a plain string, not a list of blocks.
        assert isinstance(recorder.kwargs["messages"][0]["content"], str)

    def test_passes_ocr_flag_into_metadata_on_text_path(self, monkeypatch):
        """``source_was_ocr`` is only ever True on the TEXT fallback, where OCR can run."""
        _stub_pdf_bytes(monkeypatch, b"x" * (_MAX_NATIVE_PDF_BYTES + 1))
        _stub_extraction(monkeypatch, is_ocr=True)
        recorder = _stub_llm_text(monkeypatch, '{"cnc_number": "05750", "extraction_confidence": "medium"}')

        result = extract_nest_fields_from_pdf("/tmp/05750.pdf", "05750.pdf")

        assert result["cnc_number"] == "05750"
        assert result["_extraction_metadata"]["source_was_ocr"] is True
        assert result["_extraction_metadata"]["input_mode"] == "text"
        assert recorder.ctx.is_ocr is True

    def test_text_extraction_failure_returns_filename_only_result(self, monkeypatch):
        """On the TEXT path, an ``extract_text_from_document`` failure (corrupt/odd
        PDF, OCR backend hiccup) must degrade to a filename-only result with a
        warning -- NOT propagate. The LLM must never be reached when text
        extraction has already failed.
        """
        _stub_pdf_bytes(monkeypatch, b"x" * (_MAX_NATIVE_PDF_BYTES + 1))

        def _raise(path):
            raise RuntimeError("pdf parse exploded")

        monkeypatch.setattr(svc, "extract_text_from_document", _raise)
        # If text extraction failed but run_llm_task still ran, that is a bug.
        _stub_llm_raises(monkeypatch, AssertionError("run_llm_task must not run when text extraction fails"))

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        # Filename-only degrade: stem becomes the CNC number, fields are nulled.
        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert result["material"] is None
        assert result["thickness"] is None
        assert result["sheet_size"] is None
        assert result["extraction_confidence"] == "low"
        # The text-extraction failure surfaces via the catch-all "Extraction failed".
        assert "Extraction failed" in result["warning"]
        assert "pdf parse exploded" in result["warning"]
        # The failure happened on the text path, so input_mode is "text".
        assert result["_extraction_metadata"]["input_mode"] == "text"
        # is_ocr defaults to False before the failing call, so the metadata is clean.
        assert result["_extraction_metadata"]["source_was_ocr"] is False


# --------------------------------------------------------------------------- #
# Fatal read: degrade before the LLM is ever reached
# --------------------------------------------------------------------------- #
class TestFatalRead:
    def test_unreadable_pdf_degrades_without_calling_llm(self, monkeypatch):
        """A read failure (missing file, s3 hiccup) is fatal to BOTH paths: the
        function degrades to a filename-only result and never calls run_llm_task."""
        _stub_pdf_bytes_raises(monkeypatch, FileNotFoundError("no such file: /tmp/missing.pdf"))
        _stub_llm_raises(monkeypatch, AssertionError("run_llm_task must not run when the PDF cannot be read"))
        # Text extraction must not run either -- the read failed before any path branched.
        _stub_extraction_must_not_run(monkeypatch)

        result = extract_nest_fields_from_pdf("/tmp/missing.pdf", "05749.pdf", company_id=1)

        assert result["cnc_number"] == "05749"  # filename stem
        assert result["source"] == "filename"
        assert result["material"] is None
        assert result["thickness"] is None
        assert result["sheet_size"] is None
        assert result["extraction_confidence"] == "low"
        assert "Extraction failed" in result["warning"]


# --------------------------------------------------------------------------- #
# s3 ref: native path runs via the storage backend, not open()
# --------------------------------------------------------------------------- #
class TestS3Ref:
    def test_s3_ref_reads_via_storage_backend_not_open(self, monkeypatch):
        """An ``s3://`` ref must materialize through ``read_ref_bytes`` (the storage
        backend), never through a local ``open()``. We assert the native path runs
        and that a local open is never attempted."""
        monkeypatch.setattr(svc, "is_s3_ref", lambda ref: True)
        monkeypatch.setattr(svc, "read_ref_bytes", lambda ref: _FAKE_PDF_BYTES)

        # Guard: if anything tries to open a local file, fail loudly.
        import builtins

        real_open = builtins.open

        def _guard_open(*args, **kwargs):
            raise AssertionError(f"open() must not be called for an s3 ref (args={args!r})")

        monkeypatch.setattr(builtins, "open", _guard_open)
        try:
            recorder = _stub_llm_text(monkeypatch, '{"cnc_number": "05749", "extraction_confidence": "high"}')

            result = extract_nest_fields_from_pdf("s3://bucket/1/nests/05749.pdf", "05749.pdf", company_id=1)
        finally:
            monkeypatch.setattr(builtins, "open", real_open)

        # Extraction + verification reads; the s3 bytes were read exactly once
        # (pass 2 reuses the already-encoded document, never re-opens the ref).
        assert recorder.calls == 2
        assert result["cnc_number"] == "05749"
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"
        block = _document_block(recorder.kwargs["messages"])
        assert block["source"]["media_type"] == "application/pdf"


# --------------------------------------------------------------------------- #
# Never raises -- degrade-gracefully paths (native path unless noted)
# --------------------------------------------------------------------------- #
class TestNeverRaises:
    @pytest.mark.parametrize("reason", ["library", "api_key"])
    def test_llm_not_configured_returns_filename_only_result(self, monkeypatch, reason):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMNotConfiguredError(reason))

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf")

        assert result["cnc_number"] == "05749"  # filename stem
        assert result["source"] == "filename"
        assert result["material"] is None
        assert result["thickness"] is None
        assert result["sheet_size"] is None
        assert result["extraction_confidence"] == "low"
        assert result["warning"]  # non-empty warning string
        if reason == "library":
            assert "library" in result["warning"]
        else:
            assert "API key" in result["warning"]
        # The failure happened on the native path -- metadata records it.
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"

    def test_invalid_json_returns_filename_only_result(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, "this is not json at all {oops")

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf")

        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert result["material"] is None
        assert "Invalid JSON" in result["warning"]

    def test_anthropic_api_error_returns_filename_only_result(self, monkeypatch):
        """An ``anthropic.APIError`` (the type the service checks via
        ``is_anthropic_api_error``) must degrade gracefully, not propagate."""
        _stub_pdf_bytes(monkeypatch)
        anthropic = pytest.importorskip("anthropic")
        api_error = anthropic.APIConnectionError(
            message="connection reset",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        # Sanity: this really is the error type the service branches on.
        assert is_anthropic_api_error(api_error)
        _stub_llm_raises(monkeypatch, api_error)

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf")

        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert "API error" in result["warning"]

    def test_unexpected_error_returns_filename_only_result(self, monkeypatch):
        """A non-Anthropic, non-JSON error still degrades gracefully (catch-all)."""
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, RuntimeError("kaboom"))

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf")

        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert "Extraction failed" in result["warning"]

    def test_egress_disabled_returns_filename_only_result(self, monkeypatch):
        """AI egress OFF for the company (``LLMEgressDisabledError`` from
        run_llm_task) degrades to a filename-only result with an egress-disabled
        warning -- it NEVER raises, so a 50-nest batch still completes."""
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMEgressDisabledError(company_id=1))

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        # _create_empty_nest_result shape: filename stem, nulled fields, low confidence.
        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
        assert result["material"] is None
        assert result["thickness"] is None
        assert result["sheet_size"] is None
        assert result["extraction_confidence"] == "low"
        # The warning explicitly names the disabled state (not a generic failure).
        assert "disabled" in result["warning"].lower()
        assert "allow_ai_egress" in result["warning"]
        # It is the egress branch, not the catch-all "Extraction failed".
        assert "Extraction failed" not in result["warning"]
        assert result["_extraction_metadata"]["input_mode"] == "native_pdf"


class TestSyntheticSegmentNames:
    """The bare-PDF path passes ``filename_is_cnc_hint=False``: synthetic split
    names ('nest-p001.pdf') must never be stamped in as CNC numbers, on either
    the degrade path or the both-passes-null merge path."""

    def test_degrade_leaves_cnc_null_for_synthetic_names(self, monkeypatch):
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMEgressDisabledError(company_id=1))

        result = extract_nest_fields_from_pdf(
            "/tmp/nest-p001.pdf", "nest-p001.pdf", company_id=1, filename_is_cnc_hint=False
        )

        # NOT 'nest-p001' -- the row stays blank for the planner to fill.
        assert result["cnc_number"] is None
        assert result["source"] == "none"
        assert result["extraction_confidence"] == "low"
        assert "disabled" in result["warning"].lower()

    def test_both_passes_null_cnc_stays_null_for_synthetic_names(self, monkeypatch):
        # Both passes see the same fixed response: cnc null, material pinned.
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_text(monkeypatch, '{"cnc_number": null, "material": "A36", "confidence": {}}')

        result = extract_nest_fields_from_pdf("/tmp/nest-p003.pdf", "nest-p003.pdf", filename_is_cnc_hint=False)

        assert result["cnc_number"] is None
        assert result["source"] == "ai"
        assert result["confidence"]["cnc_number"] == "low"
        # The other fields still merge normally (agreement -> high).
        assert result["material"] == "A36"
        assert result["confidence"]["material"] == "high"

    def test_default_filename_fallback_unchanged(self, monkeypatch):
        """Per-file packages (real CNC filenames) keep the stem fallback."""
        _stub_pdf_bytes(monkeypatch)
        _stub_llm_raises(monkeypatch, LLMEgressDisabledError(company_id=1))

        result = extract_nest_fields_from_pdf("/tmp/05749.pdf", "05749.pdf", company_id=1)

        assert result["cnc_number"] == "05749"
        assert result["source"] == "filename"
