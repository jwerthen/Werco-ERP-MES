"""Versioned prompt registry for all Anthropic-powered features.

Every prompt that reaches the API has an entry here so that:
- AIUsageEvent rows record which prompt version produced a given call, and
- AIInteractionEvent / AIRecommendation rows can attribute learning signals
  to the prompt revision that generated the suggestion.

Bump the ``version`` (semver) whenever prompt text changes and record the
change in ``CHANGELOG.md`` in this package. Prompt text should be stable,
deterministic content (no timestamps, no per-request values) — it forms the
cacheable prefix for prompt caching.
"""

from typing import Dict

from app.services.prompts.base import Prompt
from app.services.prompts.copilot import COPILOT_CHAT_PROMPT, NL_SEARCH_INTENT_PROMPT
from app.services.prompts.extraction import (
    BOM_EXTRACTION_PROMPT,
    BOM_EXTRACTION_SCHEMA,
    PO_EXTRACTION_PROMPT,
    PO_EXTRACTION_SCHEMA,
)
from app.services.prompts.qms import QMS_CLAUSE_EXTRACTION_PROMPT
from app.services.prompts.routing import ROUTING_GENERATION_PROMPT

PROMPT_REGISTRY: Dict[str, Prompt] = {
    prompt.id: prompt
    for prompt in (
        PO_EXTRACTION_PROMPT,
        BOM_EXTRACTION_PROMPT,
        ROUTING_GENERATION_PROMPT,
        QMS_CLAUSE_EXTRACTION_PROMPT,
        COPILOT_CHAT_PROMPT,
        NL_SEARCH_INTENT_PROMPT,
    )
}

__all__ = [
    "Prompt",
    "PROMPT_REGISTRY",
    "PO_EXTRACTION_PROMPT",
    "PO_EXTRACTION_SCHEMA",
    "BOM_EXTRACTION_PROMPT",
    "BOM_EXTRACTION_SCHEMA",
    "ROUTING_GENERATION_PROMPT",
    "QMS_CLAUSE_EXTRACTION_PROMPT",
    "COPILOT_CHAT_PROMPT",
    "NL_SEARCH_INTENT_PROMPT",
]
