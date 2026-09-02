"""The docs-name guard: a tool name the docs (or the package itself) quote must exist in the live catalog.

Generated names are DERIVED (``app/mcp/naming.py``) and can shift when a router adds a
twin -- or a sibling's twin: ``delete_work_order`` became ``work_orders_delete_work_order``
when the family rule shipped, with no change to its own route. A doc that quotes a stale
name sends an agent (or a person) to a tool that answers 404. This file makes that a CI
failure rather than a stale runbook.

Subjects and rules -- one rule per surface, stated here so a red run says what to fix:

- ``docs/MCP.md`` section 7 (the convenience tools): the first column of its table names
  EXACTLY ``CONVENIENCE_TOOL_NAMES``, no more and no fewer.
- ``docs/MCP.md`` section 5 (the program map): every backticked identifier in the third
  column ("Representative tools") of its table is a live tool. The trailing ``★`` that
  marks a convenience tool sits outside the backticks.
- ``docs/MCP.md`` section 12 (example sessions): inside a fenced ``text`` block, the first
  token of every line that starts at column 0 is a tool call and must be a live tool;
  continuation lines are indented.
- Everywhere else in ``docs/MCP.md`` (every section except 6.2, which discusses function
  names and former names on purpose), in ``CLAUDE.md``'s ``mcp/`` bullet and in
  ``docs/API.md``'s "MCP door" section: a backticked identifier -- one match of
  ``[a-z][a-z0-9_]{2,63}``, which already leaves out env-var names (upper-case), file
  paths and routes (``/``, ``.``) and anything containing a space -- is a TOOL-NAME CLAIM
  when it is a convenience name, the function name of a live secured route (what the
  naming policy sees, shadowed or not), or carries a live tag slug as its prefix.
  Argument names, field names and status values are none of those, so they are not
  claims. A ``tool {"arg": ...}`` span claims its first token. Every claim must be a
  live tool. This is exactly what catches the stale case: the function name
  ``delete_work_order`` is live, its bare tool is not.
- The names the PACKAGE ITSELF quotes in prose -- the server instructions, every
  convenience description and argument description, the verbs in
  ``STATUS_WRITES_WITH_A_REAL_VERB`` -- are held to the same claim rule.
- ``.cursor/mcp.json.example`` is exactly Cursor's ``mcpServers`` map: valid JSON, that
  one top-level key, no ``_comment`` keys anywhere (Cursor's schema does not define them
  and some clients reject unknown keys; the guidance lives in MCP.md section 3.3).

The catalog is built in-process from ``app.main.app.openapi()`` exactly as the server
builds it; no database, no token.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Set, Tuple

import pytest

from app.main import app
from app.mcp.catalog import build_catalog, catalog_tags, iter_secured_operations
from app.mcp.convenience import (
    CONVENIENCE_TOOL_NAMES,
    CONVENIENCE_TOOLS,
    SHADOWED_OPERATIONS,
    STATUS_WRITES_WITH_A_REAL_VERB,
)
from app.mcp.naming import function_name_from_operation_id, tag_slug
from app.mcp.server import DEFAULT_INSTRUCTIONS

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MCP_DOC = REPO / "docs" / "MCP.md"
API_DOC = REPO / "docs" / "API.md"
CLAUDE_MD = REPO / "CLAUDE.md"
CURSOR_EXAMPLE = REPO / ".cursor" / "mcp.json.example"

# Sections of MCP.md exempt from the claim rule: 6.2 explains the naming policy and must
# be able to say "``list_work_orders`` is ``work_orders_list_work_orders`` because ...".
EXEMPT_SECTIONS: FrozenSet[str] = frozenset({"6.2"})

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")
_CALL_SPAN = re.compile(r"^([a-z][a-z0-9_]{2,63})\s+\{")
_PROSE_WORD = re.compile(r"\b[a-z][a-z0-9_]{2,63}\b")
_FENCE = re.compile(r"```[A-Za-z]*\n(.*?)```", re.S)
_SECTION = re.compile(r"^## (\d+)\. [^\n]*\n", re.M)
_SUBSECTION = re.compile(r"^### (\d+\.\d+) [^\n]*\n", re.M)
_TABLE_SEPARATOR = re.compile(r"^:?-+:?$")


# --------------------------------------------------------------------------- live catalog


class LiveNames:
    """Everything the claim rule needs, built once from the live document."""

    def __init__(self) -> None:
        spec = app.openapi()
        catalog = build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)
        self.tools: FrozenSet[str] = frozenset(tool.name for tool in catalog) | CONVENIENCE_TOOL_NAMES
        self.function_names: FrozenSet[str] = frozenset(
            function_name_from_operation_id(str(operation.get("operationId") or ""), method)
            for method, _path, operation, _params in iter_secured_operations(spec)
        )
        self.slugs: FrozenSet[str] = frozenset(tag_slug(tag) for tag in catalog_tags(spec))

    def is_claim(self, token: str) -> bool:
        """Is ``token`` presented as a tool name, by the rule in the module docstring?"""
        if token in CONVENIENCE_TOOL_NAMES or token in self.function_names:
            return True
        return any(token.startswith(slug + "_") and len(token) > len(slug) + 1 for slug in self.slugs)

    def stale(self, tokens: Iterable[str]) -> List[str]:
        """The tokens that claim to be tools and are not."""
        return sorted({token for token in tokens if self.is_claim(token) and token not in self.tools})


@pytest.fixture(scope="module")
def live() -> LiveNames:
    return LiveNames()


@pytest.fixture(scope="module")
def mcp_sections() -> Dict[str, str]:
    return split_sections(MCP_DOC.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- markdown helpers


def split_sections(text: str) -> Dict[str, str]:
    """``{"5": ..., "6": <preamble>, "6.1": ..., "6.2": ...}`` from the ``## N.`` / ``### N.M`` headings."""
    sections: Dict[str, str] = {}
    parts = _SECTION.split(text)
    for index in range(1, len(parts), 2):
        number, body = parts[index], parts[index + 1]
        sub_parts = _SUBSECTION.split(body)
        sections[number] = sub_parts[0]
        for sub_index in range(1, len(sub_parts), 2):
            sections[sub_parts[sub_index]] = sub_parts[sub_index + 1]
    return sections


def section_with_subsections(sections: Dict[str, str], number: str) -> str:
    """Section ``number`` and every ``number.M`` subsection, in document order (§12 is 12.1 … 12.5)."""
    return "\n".join(body for key, body in sections.items() if key == number or key.startswith(number + "."))


def fenced_blocks(text: str) -> List[str]:
    return _FENCE.findall(text)


def without_fences(text: str) -> str:
    return _FENCE.sub("", text)


def backticked_identifiers(text: str) -> Iterator[str]:
    """Every backticked identifier, plus the tool of every backticked ``tool {…}`` span."""
    for span in _BACKTICK_SPAN.findall(text):
        span = span.strip()
        if _IDENTIFIER.match(span):
            yield span
            continue
        call = _CALL_SPAN.match(span)
        if call:
            yield call.group(1)


def table_rows(text: str) -> List[List[str]]:
    """The body rows of every markdown table in ``text`` as lists of cell strings (header and separator dropped)."""
    rows: List[List[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(_TABLE_SEPARATOR.match(cell) for cell in cells if cell):
            continue
        rows.append(cells)
    # Drop each table's header row: it is the row right before a separator, i.e. the
    # first row collected, and any row whose cells carry no backticks at all is header-like.
    return [row for row in rows if any("`" in cell for cell in row)]


def fence_calls(text: str) -> Iterator[str]:
    """The first token of every column-0 line in every fenced block."""
    for block in fenced_blocks(text):
        for line in block.splitlines():
            if not line or line[0].isspace():
                continue
            first = line.split()[0]
            if _IDENTIFIER.match(first):
                yield first


def claims_in(text: str, live: LiveNames) -> List[str]:
    return live.stale(backticked_identifiers(without_fences(text)))


# --------------------------------------------------------------------------- MCP.md


class TestMcpDoc:
    def test_convenience_section_names_exactly_the_convenience_tools(self, mcp_sections):
        section = section_with_subsections(mcp_sections, "7")
        listed = {
            span for row in table_rows(section) for span in _BACKTICK_SPAN.findall(row[0]) if _IDENTIFIER.match(span)
        }
        assert listed == set(CONVENIENCE_TOOL_NAMES), {
            "missing from section 7": sorted(set(CONVENIENCE_TOOL_NAMES) - listed),
            "in section 7 but not a convenience tool": sorted(listed - set(CONVENIENCE_TOOL_NAMES)),
        }

    def test_program_map_representative_tools_are_live(self, mcp_sections, live):
        rows = table_rows(section_with_subsections(mcp_sections, "5"))
        assert len(rows) >= 60, "the program map table is the subject; it must have been found"
        stale: Dict[str, List[str]] = {}
        seen = 0
        for row in rows:
            assert len(row) >= 3, row
            tools = [span for span in _BACKTICK_SPAN.findall(row[2]) if _IDENTIFIER.match(span)]
            seen += len(tools)
            missing = sorted(set(tools) - live.tools)
            if missing:
                stale[row[0]] = missing
        assert seen >= 250, "the third column is where the representative tools live"
        assert stale == {}, f"section 5 names tools that do not exist: {stale}"

    def test_example_session_calls_are_live(self, mcp_sections, live):
        calls = list(fence_calls(section_with_subsections(mcp_sections, "12")))
        assert len(calls) >= 25, "the example sessions must have been found"
        assert {"create_work_order", "release_work_order", "import_laser_nest_package"} <= set(calls)
        assert sorted(set(calls) - live.tools) == []

    def test_every_tool_name_claim_in_the_runbook_is_live(self, mcp_sections, live):
        assert {"5", "6.2", "7", "12", "12.1"} <= set(mcp_sections), sorted(mcp_sections)
        stale = {
            number: claims_in(body, live)
            for number, body in mcp_sections.items()
            if number not in EXEMPT_SECTIONS and claims_in(body, live)
        }
        assert stale == {}, (
            "docs/MCP.md quotes tool names that are not in the live catalog (a derived name shifted, or a "
            f"typo); re-run `python -m app.mcp --print-catalog` and fix the section: {stale}"
        )

    def test_the_claim_rule_is_not_vacuous(self, mcp_sections, live):
        # Live things the rule must treat as claims: a convenience name, a prefixed generated
        # name, and a bare FUNCTION name whose tool is prefixed (the stale case).
        assert live.is_claim("create_work_order") and "create_work_order" in live.tools
        assert live.is_claim("work_orders_delete_work_order") and "work_orders_delete_work_order" in live.tools
        assert live.is_claim("delete_work_order") and "delete_work_order" not in live.tools
        assert live.stale(["delete_work_order", "work_orders_delete_work_order"]) == ["delete_work_order"]
        # Things it must NOT treat as claims: arguments, field names, status values, prose.
        for token in ("part_id", "quantity_ordered", "status", "on_hold", "released_at", "version", "localhost"):
            assert not live.is_claim(token), token
        # ...and the exempt section really does carry former/function names, so the exemption is load-bearing.
        assert claims_in(mcp_sections["6.2"], live), "6.2 should mention bare function names by design"


# --------------------------------------------------------------------------- CLAUDE.md / API.md


class TestOtherDocs:
    def test_claude_md_mcp_bullet_names_only_live_tools(self, live):
        text = CLAUDE_MD.read_text(encoding="utf-8")
        bullets = [line for line in text.splitlines() if line.startswith("- `mcp/` —")]
        assert len(bullets) == 1, "CLAUDE.md carries exactly one `mcp/` bullet under Backend architecture"
        [bullet] = bullets
        assert "create_work_order" in bullet and "work_orders_delete_work_order" in bullet
        assert claims_in(bullet, live) == []

    def test_api_md_mcp_section_names_only_live_tools(self, live):
        text = API_DOC.read_text(encoding="utf-8")
        match = re.search(r"^## MCP door[^\n]*\n(.*?)(?=^## )", text, re.M | re.S)
        assert match, "docs/API.md carries an `## MCP door` section"
        assert claims_in(match.group(1), live) == []


# --------------------------------------------------------------------------- the package's own prose


def _descriptions(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        description = schema.get("description")
        if isinstance(description, str):
            yield description
        for value in schema.values():
            yield from _descriptions(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _descriptions(item)


class TestPackageProse:
    def test_status_write_verbs_are_live_tools(self, live):
        assert set(STATUS_WRITES_WITH_A_REAL_VERB.values()) <= live.tools

    def test_server_instructions_and_convenience_descriptions_name_only_live_tools(self, live):
        texts: List[Tuple[str, str]] = [("DEFAULT_INSTRUCTIONS", DEFAULT_INSTRUCTIONS)]
        for tool in CONVENIENCE_TOOLS:
            texts.append((f"{tool.name}.description", tool.description))
            for description in _descriptions(tool.input_schema):
                texts.append((f"{tool.name}.input_schema", description))
        stale = {label: live.stale(_PROSE_WORD.findall(text)) for label, text in texts}
        stale = {label: names for label, names in stale.items() if names}
        assert stale == {}, f"the package quotes tool names that do not exist: {stale}"
        # The instructions really do quote generated names, so this test is not vacuous.
        quoted = {word for word in _PROSE_WORD.findall(DEFAULT_INSTRUCTIONS) if live.is_claim(word)}
        assert {"work_orders_start_work_order", "shop_floor_start_operation"} <= quoted


# --------------------------------------------------------------------------- .cursor/mcp.json.example


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


class TestCursorExample:
    def test_example_is_exactly_an_mcp_servers_map_without_comment_keys(self):
        document = json.loads(CURSOR_EXAMPLE.read_text(encoding="utf-8"))
        assert set(document) == {"mcpServers"}
        servers = document["mcpServers"]
        assert servers and all(isinstance(entry, dict) for entry in servers.values())
        assert "_comment" not in set(_keys(document))
        for name, entry in servers.items():
            assert ("url" in entry) != ("command" in entry), f"{name}: an HTTP entry or a stdio entry, not both"
        secrets: Set[str] = set()
        for text in json.dumps(document).split('"'):
            if text.startswith(("eyJ", "sk-")):
                secrets.add(text)
        assert not secrets, "the committed example carries placeholders only"
