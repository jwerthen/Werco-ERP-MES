"""Text a ``str`` can hold but UTF-8 cannot encode: unpaired UTF-16 surrogates.

JSON allows a lone surrogate escape (``"\\ud800"``) and ``json.loads`` keeps it as the
code point U+D800 -- which a Python ``str`` can carry but no UTF-8 encoder accepts. So a
request body can deliver one into any ``Any``-typed or ``str`` field, it can travel all the
way to a JSON column (``json.dumps`` escapes it back to ``\\ud800``, and Postgres ``json``
stores that), and then every response that renders it raises ``UnicodeEncodeError`` --
after the route has already run, as a 500 nothing upstream can catch. A stored one 500s
every later read of that row.

The surrogate range U+D800-U+DFFF is the ONLY set of code points a ``str`` can hold that
strict UTF-8 refuses, so the checks below are exact, not a heuristic.
"""

import re
from typing import Any

_UNENCODABLE = re.compile("[\ud800-\udfff]")

# What an unencodable character is shown as once it has to be rendered anyway.
REPLACEMENT_CHARACTER = "�"


def is_utf8_encodable(text: str) -> bool:
    """Whether ``text`` can be encoded as UTF-8 (holds no unpaired surrogate)."""
    return _UNENCODABLE.search(text) is None


def replace_unencodable(text: str) -> str:
    """``text`` with each unpaired surrogate replaced by U+FFFD (unchanged when there is none)."""
    return _UNENCODABLE.sub(REPLACEMENT_CHARACTER, text)


def contains_unencodable_text(value: Any) -> bool:
    """Whether any string in ``value`` -- a JSON-shaped structure, dict keys included -- cannot
    be encoded as UTF-8. Anything that is not a ``str``, ``dict``, ``list`` or ``tuple`` is
    not looked into."""
    if isinstance(value, str):
        return not is_utf8_encodable(value)
    if isinstance(value, dict):
        return any(contains_unencodable_text(k) or contains_unencodable_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_unencodable_text(item) for item in value)
    return False


def scrub_unencodable_text(value: Any) -> Any:
    """A copy of ``value`` with every unencodable string (dict keys included) passed through
    :func:`replace_unencodable`. Containers keep their type; anything else is returned as is,
    so a structure with nothing to replace comes back equal to the original."""
    if isinstance(value, str):
        return replace_unencodable(value)
    if isinstance(value, dict):
        return {scrub_unencodable_text(k): scrub_unencodable_text(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(scrub_unencodable_text(item) for item in value)
    if isinstance(value, list):
        return [scrub_unencodable_text(item) for item in value]
    return value
