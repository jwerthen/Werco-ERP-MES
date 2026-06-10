"""Version registration for the QMS clause-extraction prompt.

The prompt text lives inline in ``app/api/endpoints/qms_standards.py`` (it is
a single f-string interpolating the standard name and document text; moving it
here was judged more churn than value). The registry entry exists so usage
telemetry records the version — bump it here whenever the inline text changes.
"""

from app.services.prompts.base import Prompt

QMS_CLAUSE_EXTRACTION_PROMPT = Prompt(
    id="qms_clause_extraction",
    version="1.0.0",
    text=None,  # maintained at the call site — see module docstring
)
