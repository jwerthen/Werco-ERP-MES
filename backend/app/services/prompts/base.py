"""Prompt dataclass shared by the versioned prompt registry."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Prompt:
    """A versioned prompt.

    ``text`` is None when the prompt is a per-request template rendered by its
    service (e.g. ``QMS_CLAUSE_EXTRACTION_TEMPLATE`` in ``prompts/qms.py``,
    sent as the user message) rather than a stable, cacheable system prompt.
    The entry still versions that template for telemetry: bump ``version``
    when the template changes, exactly as for a prompt that carries ``text``.
    """

    id: str
    version: str
    text: Optional[str] = None
