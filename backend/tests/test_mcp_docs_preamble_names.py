"""The docs-name guard, extended to ``docs/MCP.md``'s preamble (the text before ``## 1.``).

``tests/test_mcp_docs_names.py`` holds every numbered section of ``docs/MCP.md`` to the
tool-name claim rule, but ``split_sections`` starts at the first ``## N.`` heading, so the
preamble -- where every "Re-checked again on ..." paragraph quotes the tools a change added
-- was read by nothing: a shifted derived name, or a bare function name, there stayed green.
This applies the same rule (``LiveNames`` / ``claims_in``, imported, never copied) to that
text. Like that rule it flags only tokens that look like tool names (a route function name, a
tag-slug prefix, or a ``_<function name>`` suffix), so it does not catch every typo.
"""

from __future__ import annotations

import re

import pytest

from tests.test_mcp_docs_names import MCP_DOC, LiveNames, claims_in

pytestmark = pytest.mark.unit

_FIRST_SECTION = re.compile(r"^## \d+\. ", re.M)


def mcp_preamble() -> str:
    """``docs/MCP.md`` up to its first ``## N.`` heading."""
    text = MCP_DOC.read_text(encoding="utf-8")
    match = _FIRST_SECTION.search(text)
    assert match is not None, "docs/MCP.md has no numbered section heading"
    return text[: match.start()]


@pytest.fixture(scope="module")
def live() -> LiveNames:
    return LiveNames()


def test_preamble_tool_name_claims_are_live(live: LiveNames) -> None:
    stale = claims_in(mcp_preamble(), live)
    assert stale == [], (
        "docs/MCP.md's preamble quotes tool names that are not in the live catalog (a derived name "
        f"shifted, or a bare function name); re-run `python -m app.mcp --print-catalog` and fix it: {stale}"
    )


def test_preamble_rule_is_not_vacuous(live: LiveNames) -> None:
    preamble = mcp_preamble()
    # The preamble really does quote live tools, so the check above reads something.
    assert "`work_orders_resume_work_order`" in preamble and live.is_claim("work_orders_resume_work_order")
    # A stale (bare function) name placed there is caught.
    assert claims_in(preamble + "\n`delete_work_order`\n", live) == ["delete_work_order"]
