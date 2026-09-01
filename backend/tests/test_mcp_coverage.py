"""The MCP coverage guard: a router that ships tomorrow must be a tool tomorrow.

Two guard functions in ``app/mcp/catalog.py`` are the CI trip-wire for "a new router
shipped and MCP is blind", and this file proves both against the live application AND
against synthetic documents -- so an empty result in the live case is a finding, not a
no-op:

1. ``uncovered_tags`` -- every tag except the explicit ``EXCLUDED_TAGS`` is reached by
   at least one tool (generated, or a convenience tool that CLAIMS the tag of a
   shadowed route). Tag-granular: a new router under an EXISTING tag slips past it,
   which is why there is a second guard.
2. ``unaccounted_operations`` -- every ``(METHOD, path)`` in the document is a tool, a
   shadowed route, a named excluded loader, a SECURED route under an excluded tag, or
   a route named in ``PUBLIC_OPERATIONS``. An unsecured route is never excused by its
   tag, so forgetting the ``security`` dependency on a new route fails CI by name.

The router wiring is checked against the LIVE route objects (``app.routes``), not by
parsing ``router.py``: every module under ``app/api/endpoints/`` owns at least one wired
route and vice versa, every module's routes carry a covered (or excluded) tag, and no
``/api/v1`` route hides from the OpenAPI document (``include_in_schema=False`` would make
it invisible to the catalog).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest
from fastapi.routing import APIRoute

from app.main import app
from app.mcp.catalog import (
    EXCLUDED_OPERATIONS,
    EXCLUDED_TAGS,
    HTTP_METHODS,
    PUBLIC_OPERATIONS,
    GeneratedTool,
    build_catalog,
    catalog_tags,
    iter_operations,
    unaccounted_operations,
    uncovered_tags,
)
from app.mcp.convenience import CONVENIENCE_TAGS, CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS

pytestmark = pytest.mark.unit

ENDPOINTS_DIR = Path(__file__).resolve().parents[1] / "app" / "api" / "endpoints"
API_PREFIX = "/api/v1"
_METHODS = {method.upper() for method in HTTP_METHODS}


@pytest.fixture(scope="module")
def spec() -> Dict[str, Any]:
    return app.openapi()


@pytest.fixture(scope="module")
def catalog(spec) -> List[GeneratedTool]:
    return build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _live_routes() -> List[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute) and route.path.startswith(API_PREFIX)]


def _route_keys(routes: List[APIRoute]) -> Set[Tuple[str, str]]:
    # ``path_format`` is the path as the document prints it -- a Starlette convertor
    # suffix (``{path:path}`` on the PO-upload PDF route) never reaches OpenAPI.
    return {(method, route.path_format) for route in routes for method in route.methods if method in _METHODS}


class TestLiveDocument:
    def test_every_tag_except_the_exclusions_has_a_tool(self, spec, catalog):
        missing = uncovered_tags(
            spec, catalog, shadowed=SHADOWED_OPERATIONS, excluded_tags=EXCLUDED_TAGS, convenience_tags=CONVENIENCE_TAGS
        )
        assert (
            missing == set()
        ), f"tags with no MCP tool: {sorted(missing)} -- add `security` to their routes or name them in EXCLUDED_TAGS"
        # Sanity on the subject: the document really carries the whole API.
        assert len(catalog_tags(spec)) >= 60

    def test_every_shadowed_routes_tag_is_claimed_by_a_convenience_tool(self, spec, catalog):
        """The shadow credit inside ``uncovered_tags`` is granted only for a claimed tag.

        Every shadowed route's tag must be one a convenience description ends with;
        and because today every shadowed route shares its tag with an unshadowed one,
        the live guard agrees with and without the credit -- the credit itself is
        proven on synthetic documents below.
        """
        shadowed_tags = {
            str(tag)
            for method, path, operation in iter_operations(spec)
            if (method, path) in SHADOWED_OPERATIONS
            for tag in operation["tags"]
        }
        assert shadowed_tags and shadowed_tags <= CONVENIENCE_TAGS, shadowed_tags - CONVENIENCE_TAGS
        assert uncovered_tags(spec, catalog, excluded_tags=EXCLUDED_TAGS) == uncovered_tags(
            spec, catalog, shadowed=SHADOWED_OPERATIONS, excluded_tags=EXCLUDED_TAGS, convenience_tags=CONVENIENCE_TAGS
        )

    def test_every_route_is_accounted_for_by_name(self, spec, catalog):
        """Route-granular: a new route under an existing tag cannot hide behind the tag."""
        assert unaccounted_operations(spec, catalog, shadowed=SHADOWED_OPERATIONS) == set()
        unsecured = {(m, p) for m, p, operation in iter_operations(spec) if not operation.get("security")}
        assert unsecured == set(PUBLIC_OPERATIONS), "PUBLIC_OPERATIONS must name exactly the unsecured routes"
        documented = {(m, p) for m, p, _operation in iter_operations(spec)}
        assert set(EXCLUDED_OPERATIONS) <= documented and set(SHADOWED_OPERATIONS) <= documented

    def test_no_api_route_hides_from_the_document(self, spec):
        routes = _live_routes()
        assert len(routes) >= 600
        assert all(route.include_in_schema for route in routes), "a hidden route can never become a tool"
        documented = {(m, p) for m, p, _operation in iter_operations(spec) if p.startswith(API_PREFIX)}
        assert _route_keys(routes) == documented

    def test_every_endpoint_module_is_wired_and_reachable(self, catalog):
        on_disk = {path.stem for path in ENDPOINTS_DIR.glob("*.py") if path.stem != "__init__"}
        by_module: Dict[str, List[APIRoute]] = {}
        for route in _live_routes():
            by_module.setdefault(route.endpoint.__module__.rsplit(".", 1)[-1], []).append(route)
        assert on_disk == set(by_module), "every endpoint module owns at least one wired route, and vice versa"
        assert len(on_disk) >= 60

        covered = {tool.tag for tool in catalog} | CONVENIENCE_TAGS
        for module, routes in by_module.items():
            tags = {str(tag) for route in routes for tag in route.tags}
            assert tags, f"router module {module!r} is wired without a tag"
            assert tags & (covered | EXCLUDED_TAGS), f"router module {module!r} (tags {sorted(tags)}) has no MCP tool"
        # The exclusions in use are the ones the brief names, so a new router cannot hide there quietly.
        in_use = {str(tag) for route in _live_routes() for tag in route.tags if tag in EXCLUDED_TAGS}
        assert in_use == {"Authentication", "Carrier Webhooks", "Error Logging", "errors"}


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


class TestGuardFunctions:
    def test_reports_a_tag_whose_operations_yield_no_tool(self):
        doc = _doc(_op("Covered"), _op("Blind", secured=False))
        tools = build_catalog(doc)
        assert {tool.tag for tool in tools} == {"Covered"}
        assert uncovered_tags(doc, tools, excluded_tags=EXCLUDED_TAGS) == {"Blind"}

    def test_reports_nothing_when_every_tag_is_covered(self):
        doc = _doc(_op("A"), _op("B", index=1))
        assert uncovered_tags(doc, build_catalog(doc), excluded_tags=EXCLUDED_TAGS) == set()

    def test_shadowed_operation_credits_its_tag_only_when_a_convenience_tool_claims_it(self):
        doc = _doc(_op("Shadowed"))
        shadow = {("GET", "/api/v1/synthetic/0")}
        tools = build_catalog(doc, shadowed=shadow)
        assert tools == []
        assert uncovered_tags(doc, tools, excluded_tags=EXCLUDED_TAGS) == {"Shadowed"}
        # Without a claim list every shadowed tag is credited (the pre-existing rule)...
        assert uncovered_tags(doc, tools, shadowed=shadow, excluded_tags=EXCLUDED_TAGS) == set()
        # ...with one, a shadow that no convenience tool stands behind is reported.
        assert uncovered_tags(doc, tools, shadowed=shadow, excluded_tags=EXCLUDED_TAGS, convenience_tags=()) == {
            "Shadowed"
        }
        assert (
            uncovered_tags(doc, tools, shadowed=shadow, excluded_tags=EXCLUDED_TAGS, convenience_tags={"Shadowed"})
            == set()
        )

    def test_excluded_tag_is_not_reported(self):
        doc = _doc(_op("Authentication", secured=False), _op("Plain", secured=False, index=1))
        assert uncovered_tags(doc, [], excluded_tags=EXCLUDED_TAGS) == {"Plain"}
        assert uncovered_tags(doc, [], excluded_tags=()) == {"Authentication", "Plain"}

    def test_unaccounted_reports_an_unsecured_route_under_a_covered_tag(self):
        """The shape the tag guard is blind to: sibling router, existing tag, no ``security``."""
        doc = _doc(_op("Work Orders"), _op("Work Orders", secured=False, index=1))
        tools = build_catalog(doc)
        assert uncovered_tags(doc, tools, excluded_tags=EXCLUDED_TAGS) == set(), "the tag guard passes"
        assert unaccounted_operations(doc, tools, public_operations=()) == {("GET", "/api/v1/synthetic/1")}
        assert unaccounted_operations(doc, tools, public_operations={("GET", "/api/v1/synthetic/1")}) == set()

    def test_unaccounted_excuses_shadowed_and_excluded_routes_but_only_secured_ones_by_tag(self):
        doc = _doc(_op("Authentication", secured=False), _op("Authentication", index=1), _op("Loader", index=2))
        shadow = {("GET", "/api/v1/synthetic/2")}
        assert unaccounted_operations(doc, [], shadowed=shadow, excluded_operations=(), public_operations=()) == {
            ("GET", "/api/v1/synthetic/0")
        }
        excluded = {("GET", "/api/v1/synthetic/2")}
        assert unaccounted_operations(doc, [], excluded_operations=excluded, public_operations=()) == {
            ("GET", "/api/v1/synthetic/0")
        }
