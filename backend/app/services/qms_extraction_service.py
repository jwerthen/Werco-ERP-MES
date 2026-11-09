"""QMS clause extraction: render the prompt, make the model call, parse the clause list.

Moved out of ``POST /qms-standards/{id}/upload-pdf``
(``app/api/endpoints/qms_standards.py``) by roadmap #9-0 with **no behavior
change**: the request that reaches Anthropic (model routing context, the single
user message, ``max_tokens``, no ``system``, and no per-call timeout or retry
override) is byte-identical to what the inline code sent. The prompt text is
``QMS_CLAUSE_EXTRACTION_TEMPLATE`` in ``app/services/prompts/qms.py``.

The split of responsibilities:

- **This module** renders the prompt, calls :func:`run_llm_task`, strips any
  markdown fence, parses the JSON and checks that it is an array.
- **The handler** still owns everything around it: the PDF read, the mapping of
  each exception below to an HTTP status, writing the clause rows, the
  ``AuditService`` row and the commit. Tenant scope comes in as ``company_id``,
  which the handler takes from ``get_current_company_id``; it drives the
  ``allow_ai_egress`` check and the tenant-scoped ``ai_usage_events`` row inside
  :func:`run_llm_task`.

Exceptions propagate unchanged so the handler's ``except`` chain maps them
exactly as before:

- :class:`~app.services.llm_client.LLMNotConfiguredError` (SDK missing or no key);
- :class:`~app.services.llm_client.LLMEgressDisabledError` (company kill switch);
- :class:`json.JSONDecodeError` (the reply was not JSON);
- :class:`ValueError` ``"Expected a JSON array of clauses"`` (JSON, but not an array);
- anything the SDK raises.

The array's elements are returned exactly as the model produced them. The
handler coerces each one and skips any without a clause number or title, as it
always has; this module does not validate them.
"""

import json
from dataclasses import dataclass
from typing import Any, List, Optional

from app.services.llm_client import run_llm_task
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts import QMS_CLAUSE_EXTRACTION_PROMPT, QMS_CLAUSE_EXTRACTION_TEMPLATE, QMS_CLAUSE_SCHEMA

# Response token cap. It also feeds model routing: ``select_anthropic_model``
# escalates qms_clause_extraction to the reasoning tier at >= 50,000 input
# characters when the output cap is >= 16,000.
QMS_MAX_OUTPUT_TOKENS = 16000

# Only this many characters of the extracted PDF text are sent. Model routing
# still sees the full length (``input_chars=len(pdf_text)``), as it always has.
QMS_DOCUMENT_TEXT_MAX_CHARS = 100000

# Telemetry labels recorded on every ``ai_usage_events`` row.
QMS_TASK = "qms_clause_extraction"
QMS_FEATURE = "qms_standards"


@dataclass(frozen=True)
class QMSClauseExtractionResult:
    """The parsed clause array plus the model facts the handler logs."""

    clauses: List[Any]
    model: str
    model_selection_reason: str


def build_qms_clause_extraction_prompt(pdf_text: str, name: str, version: Optional[str]) -> str:
    """Render the user message for one standard.

    ``name`` and ``version`` are the standard's columns. A ``None`` or empty
    version leaves the label as the name followed by a single trailing space,
    which is exactly what the old inline f-string produced.
    """
    standard_label = f"{name} {version or ''}"
    document_text = pdf_text[:QMS_DOCUMENT_TEXT_MAX_CHARS]
    return QMS_CLAUSE_EXTRACTION_TEMPLATE.format(
        standard_label=standard_label,
        clause_schema=QMS_CLAUSE_SCHEMA,
        document_text=document_text,
    )


def _strip_markdown_fence(response_text: str) -> str:
    """Remove one leading ```json / ``` fence and one trailing ``` fence, then trim."""
    if response_text.startswith("```json"):
        response_text = response_text[7:]
    if response_text.startswith("```"):
        response_text = response_text[3:]
    if response_text.endswith("```"):
        response_text = response_text[:-3]
    return response_text.strip()


def extract_clauses_with_llm(
    pdf_text: str,
    name: str,
    version: Optional[str],
    company_id: int,
) -> QMSClauseExtractionResult:
    """Ask the model for every clause in ``pdf_text`` and return the parsed array.

    Synchronous, and it blocks for the length of the model call (minutes on a
    large standard). The caller decides where that runs.
    """
    prompt = build_qms_clause_extraction_prompt(pdf_text, name, version)

    llm_result = run_llm_task(
        LLMTaskContext(
            task=QMS_TASK,
            input_chars=len(pdf_text),
            max_output_tokens=QMS_MAX_OUTPUT_TOKENS,
        ),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=QMS_MAX_OUTPUT_TOKENS,
        company_id=company_id,
        feature=QMS_FEATURE,
        prompt_version=QMS_CLAUSE_EXTRACTION_PROMPT.version,
    )

    extracted = json.loads(_strip_markdown_fence(llm_result.text.strip()))

    if not isinstance(extracted, list):
        raise ValueError("Expected a JSON array of clauses")

    return QMSClauseExtractionResult(
        clauses=extracted,
        model=llm_result.model,
        model_selection_reason=llm_result.model_selection_reason,
    )
