"""Versioned prompt for QMS clause extraction.

The prompt text lives here as ``QMS_CLAUSE_EXTRACTION_TEMPLATE`` (a
``str.format`` template) plus ``QMS_CLAUSE_SCHEMA``. It is rendered by
``app.services.qms_extraction_service.build_qms_clause_extraction_prompt`` and
sent as the single **user** message; the request carries no ``system`` prompt.

v1.0.0's text was moved verbatim from the inline f-string in
``app/api/endpoints/qms_standards.py`` (roadmap #9-0). The rendered bytes are
identical, so the version did not change. Because the template is per-request
user content rather than a stable system prompt, the registry entry keeps
``text=None``; the template constant is the text to edit.

Bump ``QMS_CLAUSE_EXTRACTION_PROMPT.version`` and add a ``CHANGELOG.md`` entry
whenever the template, the schema or the request layout changes.

Placeholders, all filled by the service: ``{standard_label}`` (the standard's
name, a space, then its version or nothing), ``{clause_schema}`` (always
``QMS_CLAUSE_SCHEMA``) and ``{document_text}`` (the first 100,000 characters of
the extracted PDF text). ``str.format`` does not re-parse substituted values, so
braces inside the document text or the standard name are sent as-is.
"""

from app.services.prompts.base import Prompt

QMS_CLAUSE_SCHEMA = """[
  {
    "clause_number": "string - e.g. '4.1', '8.5.2'",
    "title": "string - clause title",
    "description": "string - full clause text or summary of requirements"
  }
]"""

QMS_CLAUSE_EXTRACTION_TEMPLATE = """You are a QMS standards expert. Extract ALL clauses and sub-clauses from this quality management document.

The document is: {standard_label}

Rules:
1. Extract EVERY numbered clause and sub-clause (e.g., 4.1, 4.2, 5.1.1, 8.5.2)
2. Include the clause number, title, and the full requirement text as description
3. Maintain the hierarchical numbering exactly as shown in the document
4. Include ALL levels of sub-clauses
5. For the description, include the actual requirement text — not just a summary
6. Return a JSON array of objects, nothing else

Schema:
{clause_schema}

Document text:
---
{document_text}
---

Return ONLY a valid JSON array. No markdown, no explanations."""

QMS_CLAUSE_EXTRACTION_PROMPT = Prompt(
    id="qms_clause_extraction",
    version="1.0.0",
    text=None,  # per-request user-message template: see QMS_CLAUSE_EXTRACTION_TEMPLATE above
)
