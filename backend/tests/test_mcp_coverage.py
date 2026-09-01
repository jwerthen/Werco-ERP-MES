"""The MCP coverage guard: a router that ships tomorrow must be a tool tomorrow.

``app/mcp/catalog.py::uncovered_tags`` is the CI guard for "a new router shipped and
MCP is blind". Two things are proven here, and the second is what keeps the first
honest:

1. Against the live ``app.openapi()`` document, every tag except the explicit
   ``EXCLUDED_TAGS`` is reached by at least one tool (generated, or a convenience
   tool standing in for a shadowed route) -- and every router module wired in
   ``app/api/router.py`` maps to a tag the catalog carries.
2. The guard FUNCTION reports a tag with no tools when handed a synthetic document
   containing one -- so the empty result in (1) is a finding, not a no-op.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from app.main import app
from app.mcp.catalog import EXCLUDED_TAGS, GeneratedTool, build_catalog, catalog_tags, uncovered_tags
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS

pytestmark = pytest.mark.unit

ROUTER_MODULE = Path(__file__).resolve().parents[1] / "app" / "api" / "router.py"
_INCLUDE_RE = re.compile(r"include_router\(\s*(\w+)\.router\s*,(?:\s*prefix=\"[^\"]*\"\s*,)?\s*tags=\[\"([^\"]+)\"\]")
_IMPORT_BLOCK_RE = re.compile(r"from app\.api\.endpoints import \((.*?)\)", re.DOTALL)


@pytest.fixture(scope="module")
def spec() -> Dict[str, Any]:
    return app.openapi()


@pytest.fixture(scope="module")
def catalog(spec) -> List[GeneratedTool]:
    return build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _convenience_tags() -> Set[str]:
    """The ``[Tag]`` each convenience description ends with."""
    tags = set()
    for tool in CONVENIENCE_TOOLS:
        match = re.search(r"\[([^\]]+)\]\s*$", tool.description)
        assert match, f"{tool.name} must name its tag in the description"
        tags.add(match.group(1))
    return tags


class TestLiveDocument:
    def test_every_tag_except_the_exclusions_has_a_tool(self, spec, catalog):
        missing = uncovered_tags(spec, catalog, shadowed=SHADOWED_OPERATIONS, excluded_tags=EXCLUDED_TAGS)
        assert (
            missing == set()
        ), f"tags with no MCP tool: {sorted(missing)} -- add `security` to their routes or name them in EXCLUDED_TAGS"
        # Sanity on the subject: the document really carries the whole API.
        assert len(catalog_tags(spec)) >= 60

    def test_every_shadowed_routes_tag_is_still_reachable(self, spec, catalog):
        """A convenience tool replaces each shadowed route, so its tag must stay covered.

        The shadow credit inside ``uncovered_tags`` is what makes a tag whose ONLY
        route is shadowed count as covered; on today's document no tag is in that
        position (every shadowed route shares its tag with an unshadowed one), so the
        credit is proven on a synthetic document below and only checked for
        consistency here: with and without the credit the live guard agrees.
        """
        generated_tags = {tool.tag for tool in catalog}
        convenience_tags = _convenience_tags()
        for path, item in spec["paths"].items():
            for method in ("get", "post", "put", "patch", "delete"):
                operation = item.get(method)
                if isinstance(operation, dict) and (method.upper(), path) in SHADOWED_OPERATIONS:
                    for tag in operation["tags"]:
                        assert (
                            tag in convenience_tags
                        ), f"{method.upper()} {path} is shadowed but no convenience tool claims {tag!r}"
                        assert tag in generated_tags or tag in convenience_tags
        assert uncovered_tags(spec, catalog, excluded_tags=EXCLUDED_TAGS) == uncovered_tags(
            spec, catalog, shadowed=SHADOWED_OPERATIONS, excluded_tags=EXCLUDED_TAGS
        )

    def test_every_router_module_maps_to_a_covered_tag(self, spec, catalog):
        source = ROUTER_MODULE.read_text()
        imported = {name.strip() for name in _IMPORT_BLOCK_RE.search(source).group(1).replace("\n", "").split(",")}
        imported.discard("")
        included = _INCLUDE_RE.findall(source)
        assert len(included) >= 60, "the include_router regex must match the real wiring, not a handful of lines"
        modules_included = {module for module, _tag in included}
        assert imported == modules_included, "every imported endpoint module is wired, and vice versa"

        covered = {tool.tag for tool in catalog} | _convenience_tags()
        for module, tag in included:
            assert tag in covered or tag in EXCLUDED_TAGS, f"router module {module!r} (tag {tag!r}) has no MCP tool"
        # The exclusions are the ones the brief names, so a new router cannot hide there quietly.
        assert {tag for _m, tag in included if tag in EXCLUDED_TAGS} == {
            "Authentication",
            "Carrier Webhooks",
            "Error Logging",
        }


def _doc(*operations: Dict[str, Any]) -> Dict[str, Any]:
    paths: Dict[str, Any] = {}
    for index, operation in enumerate(operations):
        paths[f"/api/v1/synthetic/{index}"] = {"get": operation}
    return {"paths": paths, "components": {"schemas": {}}}


def _op(tag: str, *, secured: bool = True, index: int = 0) -> Dict[str, Any]:
    op: Dict[str, Any] = {"operationId": f"list_{tag.lower()}_{index}_api_v1_x_get", "tags": [tag], "summary": tag}
    if secured:
        op["security"] = [{"OAuth2PasswordBearer": []}]
    return op


class TestGuardFunction:
    def test_reports_a_tag_whose_operations_yield_no_tool(self):
        doc = _doc(_op("Covered"), _op("Blind", secured=False))
        tools = build_catalog(doc)
        assert {tool.tag for tool in tools} == {"Covered"}
        assert uncovered_tags(doc, tools, excluded_tags=EXCLUDED_TAGS) == {"Blind"}

    def test_reports_nothing_when_every_tag_is_covered(self):
        doc = _doc(_op("A"), _op("B", index=1))
        assert uncovered_tags(doc, build_catalog(doc), excluded_tags=EXCLUDED_TAGS) == set()

    def test_shadowed_operation_credits_its_tag(self):
        doc = _doc(_op("Shadowed"))
        shadow = {("GET", "/api/v1/synthetic/0")}
        tools = build_catalog(doc, shadowed=shadow)
        assert tools == []
        assert uncovered_tags(doc, tools, excluded_tags=EXCLUDED_TAGS) == {"Shadowed"}
        assert uncovered_tags(doc, tools, shadowed=shadow, excluded_tags=EXCLUDED_TAGS) == set()

    def test_excluded_tag_is_not_reported(self):
        doc = _doc(_op("Authentication", secured=False), _op("Plain", secured=False, index=1))
        assert uncovered_tags(doc, [], excluded_tags=EXCLUDED_TAGS) == {"Plain"}
        assert uncovered_tags(doc, [], excluded_tags=()) == {"Authentication", "Plain"}
