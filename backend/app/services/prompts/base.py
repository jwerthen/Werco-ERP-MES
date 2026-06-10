"""Prompt dataclass shared by the versioned prompt registry."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Prompt:
    """A versioned prompt.

    ``text`` is None when the prompt text is maintained at the call site and
    the registry entry exists for version tracking only.
    """

    id: str
    version: str
    text: Optional[str] = None
