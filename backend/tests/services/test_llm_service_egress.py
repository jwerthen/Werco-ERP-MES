"""AI-egress OFF-path coverage for the PO/BOM extraction service.

Offline by contract: ``run_llm_task`` is stubbed at the service's import site, so
no Anthropic call ever happens. These tests pin the degrade behavior when a
company has ``allow_ai_egress=False`` and ``run_llm_task`` raises
``LLMEgressDisabledError``: both extractors must return their well-formed EMPTY
result carrying the egress-disabled marker on ``_error`` -- never raise, and never
be confused with the generic "Extraction failed" catch-all.

The happy/JSON/catch-all paths for these extractors are exercised by the eval
harness (tests/evals); here we lock only the kill-switch branch.
"""

import pytest

import app.services.llm_service as svc
from app.services.llm_client import LLMEgressDisabledError
from app.services.llm_service import (
    _EGRESS_DISABLED_MESSAGE,
    extract_bom_data_with_llm,
    extract_po_data_with_llm,
)

pytestmark = pytest.mark.unit


def _stub_llm_egress_off(monkeypatch) -> None:
    """Make ``run_llm_task`` raise the egress kill-switch error."""

    def _raise(*args, **kwargs):
        raise LLMEgressDisabledError(company_id=kwargs.get("company_id"))

    monkeypatch.setattr(svc, "run_llm_task", _raise)


class TestPOExtractionEgressOff:
    def test_egress_off_returns_empty_po_result_with_marker(self, monkeypatch):
        _stub_llm_egress_off(monkeypatch)

        result = extract_po_data_with_llm("some po text", company_id=1)

        # Empty-result shape (from _create_empty_result), never raised.
        assert result["po_number"] is None
        assert result["line_items"] == []
        assert result["extraction_confidence"] == "low"
        assert result["vendor"] == {"name": None, "address": None}
        # The egress-disabled marker is surfaced verbatim on _error.
        assert result["_error"] == _EGRESS_DISABLED_MESSAGE
        assert "allow_ai_egress" in result["_error"]
        # Not the generic catch-all.
        assert "Extraction failed" not in result["_error"]

    def test_egress_off_quote_document_type_also_degrades(self, monkeypatch):
        _stub_llm_egress_off(monkeypatch)

        result = extract_po_data_with_llm("quote text", document_type="quote", company_id=1)

        assert result["line_items"] == []
        assert result["_error"] == _EGRESS_DISABLED_MESSAGE


class TestBOMExtractionEgressOff:
    def test_egress_off_returns_empty_bom_result_with_marker(self, monkeypatch):
        _stub_llm_egress_off(monkeypatch)

        result = extract_bom_data_with_llm("some bom text", company_id=1)

        # Empty-BOM shape (from _create_empty_bom_result), never raised.
        assert result["document_type"] is None
        assert result["items"] == []
        assert result["extraction_confidence"] == "low"
        assert result["assembly"]["part_number"] is None
        # The egress-disabled marker is surfaced verbatim on _error.
        assert result["_error"] == _EGRESS_DISABLED_MESSAGE
        assert "allow_ai_egress" in result["_error"]
        assert "Extraction failed" not in result["_error"]
