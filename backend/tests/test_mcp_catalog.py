"""The generated MCP catalog (``app/mcp/catalog.py`` + ``naming.py``) -- no database.

Brief rule 3: OpenAPI IS the catalog. These tests pin what that means operationally:
every secured operation the SPA can reach is a tool unless a convenience tool
shadows it; the excluded tags and the unsecured routes are absent; names are unique,
wire-valid, and prefixed by tag whenever two routers share a function name; every
``$ref`` is inlined so a client gets a self-contained ``inputSchema``; multipart
uploads become ``{filename, content_base64}`` objects; and the annotations say what
the HTTP method says.

The live ``app.openapi()`` document is the subject for the whole-catalog properties;
small synthetic documents pin the individual mapping rules (body/path name clash,
non-object bodies, header params, recursive components) so a regression names the
rule rather than "something in 663 tools changed".
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterator, List, Set

import pytest

from app.main import app
from app.mcp.catalog import (
    EXCLUDED_TAGS,
    JSON_CONTENT_TYPE,
    MAX_DESCRIPTION_CHARS,
    MULTIPART_CONTENT_TYPE,
    GeneratedTool,
    build_catalog,
    build_tool,
    catalog_summary,
    catalog_tags,
    iter_secured_operations,
    resolve_schema,
)
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.mcp.naming import (
    MAX_TOOL_NAME_LENGTH,
    TOOL_NAME_PATTERN,
    assign_tool_names,
    function_name_from_operation_id,
    prefixed_tool_name,
    tag_slug,
)

pytestmark = pytest.mark.unit

# ~703 operations, 686 secured, minus 4 excluded-tag groups and 14 shadowed routes.
# A drop below this means a whole router (or its ``security`` declarations) vanished.
MIN_GENERATED_TOOLS = 600


@pytest.fixture(scope="module")
def spec() -> Dict[str, Any]:
    return app.openapi()


@pytest.fixture(scope="module")
def catalog(spec) -> List[GeneratedTool]:
    return build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _walk(value: Any) -> Iterator[Any]:
    """Every dict and list nested anywhere inside ``value``."""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


# --------------------------------------------------------------------------- membership


class TestMembership:
    def test_every_secured_unshadowed_operation_is_exactly_one_tool(self, spec, catalog):
        expected: Set[tuple] = {(method, path) for method, path, _op, _params in iter_secured_operations(spec)} - set(
            SHADOWED_OPERATIONS
        )
        actual = [tool.key for tool in catalog]
        assert len(actual) == len(set(actual)), "one tool per (method, path)"
        assert set(actual) == expected
        assert len(catalog) > MIN_GENERATED_TOOLS

    def test_shadowed_routes_have_no_generated_twin(self, catalog):
        assert {tool.key for tool in catalog}.isdisjoint(SHADOWED_OPERATIONS)
        # ...and every shadowed pair really is a secured route in the document, so the
        # shadow list cannot silently rot into naming routes that no longer exist.
        secured = {(m, p) for m, p, _o, _pp in iter_secured_operations(app.openapi())}
        assert set(SHADOWED_OPERATIONS) <= secured

    def test_excluded_tags_and_unsecured_routes_are_absent(self, spec, catalog):
        assert not [tool for tool in catalog if tool.tag in EXCLUDED_TAGS]
        paths = {tool.path for tool in catalog}
        for absent in ("/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/errors/log", "/health"):
            assert absent not in paths, absent
        # The exclusions name real tags in the document (only the on-paper WebSocket one
        # is absent) -- a typo'd exclusion would exclude nothing and fail here.
        assert EXCLUDED_TAGS - catalog_tags(spec) == {"WebSocket"}
        # Every unsecured route (station logins, public claims, health) is out.
        for path, item in spec["paths"].items():
            for method in ("get", "post", "put", "patch", "delete"):
                operation = item.get(method)
                if isinstance(operation, dict) and not operation.get("security"):
                    assert (method.upper(), path) not in {tool.key for tool in catalog}

    def test_privileged_surfaces_stay_in_because_their_routes_are_the_gate(self, catalog):
        tags = {tool.tag for tool in catalog}
        assert {"Data Export", "Users", "Audit", "Platform Administration", "Admin Settings"} <= tags


# --------------------------------------------------------------------------- names


class TestNames:
    def test_names_are_unique_wire_valid_and_capped(self, catalog):
        names = [tool.name for tool in catalog]
        assert len(set(names)) == len(names)
        assert all(TOOL_NAME_PATTERN.match(name) for name in names)
        assert max(len(name) for name in names) <= MAX_TOOL_NAME_LENGTH

    def test_colliding_function_names_all_get_a_tag_prefix_and_unique_ones_stay_bare(self, spec, catalog):
        members: Dict[str, int] = {}
        for method, _path, operation, _params in iter_secured_operations(spec):
            fn = function_name_from_operation_id(operation["operationId"], method)
            members[fn] = members.get(fn, 0) + 1
        collided = {fn for fn, count in members.items() if count > 1}
        assert len(collided) >= 15, "the API is known to carry ~20 colliding function names"
        for tool in catalog:
            if tool.function_name in collided or tool.function_name in CONVENIENCE_TOOL_NAMES:
                assert tool.name == prefixed_tool_name(tag_slug(tool.tag), tool.function_name), tool.key
                assert tool.name != tool.function_name
            else:
                assert tool.name == tool.function_name, tool.key

    def test_known_collisions(self, catalog):
        names = {tool.name for tool in catalog}
        assert "start_operation" not in names
        assert {"work_orders_start_operation", "shop_floor_start_operation"} <= names
        # Shadowing Work Orders' add_operation/get_work_order/create_work_order must not
        # hand the bare names to their Routing / Preventive Maintenance twins.
        assert {"routing_add_operation", "preventive_maintenance_get_work_order"} <= names
        assert names.isdisjoint(CONVENIENCE_TOOL_NAMES)

    def test_function_name_derivation(self):
        assert function_name_from_operation_id("create_work_order_api_v1_work_orders__post", "POST") == (
            "create_work_order"
        )
        assert function_name_from_operation_id("create_manual_laser_nest_endpoint_api_v1_x_post", "POST") == (
            "create_manual_laser_nest"
        )
        # No marker: strip the trailing method instead.
        assert function_name_from_operation_id("health_check_get", "GET") == "health_check"
        assert function_name_from_operation_id("", "GET") == "operation"

    def test_tag_slug(self):
        assert tag_slug("Shop Floor") == "shop_floor"
        assert tag_slug("Customer Complaints & RMA") == "customer_complaints_rma"
        assert tag_slug("") == "untagged"

    def test_prefixed_name_fits_64_chars_slug_first(self):
        long_fn = "x" * 60
        name = prefixed_tool_name("a_very_long_tag_slug", long_fn)
        assert len(name) <= MAX_TOOL_NAME_LENGTH
        assert name.endswith(long_fn) and name.startswith("a_v")
        assert prefixed_tool_name("tag", "y" * 70) == "y" * 64

    def test_assign_tool_names_refuses_ambiguity(self):
        with pytest.raises(ValueError):
            assign_tool_names({"a": ("list_items", "Parts"), "b": ("list_items", "Parts")})
        # A reserved bare name pushes a lone function onto the prefixed form.
        assert assign_tool_names({"a": ("search", "Global Search")}, reserved={"search"}) == {
            "a": "global_search_search"
        }


# --------------------------------------------------------------------------- schemas


class TestInputSchemas:
    def test_no_ref_survives_anywhere(self, catalog):
        for tool in catalog:
            for node in _walk(tool.input_schema):
                if isinstance(node, dict):
                    assert "$ref" not in node, f"{tool.name}: unresolved $ref {node['$ref']}"
            assert tool.input_schema["type"] == "object"
            assert isinstance(tool.input_schema["properties"], dict)

    def test_path_params_are_required_and_body_fields_are_merged(self, catalog):
        update = next(tool for tool in catalog if tool.key == ("PUT", "/api/v1/work-orders/{work_order_id}"))
        props = update.input_schema["properties"]
        assert "work_order_id" in props and "version" in props and "status" in props
        assert "work_order_id" in update.input_schema["required"]
        assert "version" in update.input_schema["required"]
        assert update.body_content_type == JSON_CONTENT_TYPE
        assert "version" in update.body_properties and "work_order_id" not in update.body_properties
        assert update.path_params == ("work_order_id",)

    def test_query_params_are_properties(self, catalog):
        listing = next(tool for tool in catalog if tool.key == ("GET", "/api/v1/work-orders/"))
        assert listing.name == "work_orders_list_work_orders"
        assert {"skip", "limit", "status", "search"} <= set(listing.query_params)
        assert "skip" in listing.input_schema["properties"]
        assert listing.body_content_type is None

    def test_multipart_operations_expose_file_objects(self, catalog):
        multipart = [tool for tool in catalog if tool.body_content_type == MULTIPART_CONTENT_TYPE]
        assert len(multipart) >= 5, "the API has many CSV/PDF upload routes"
        with_files = [tool for tool in multipart if tool.file_fields or tool.file_list_fields]
        assert with_files, "at least one multipart route takes an UploadFile"
        for tool in with_files:
            for name in tool.file_fields:
                schema = tool.input_schema["properties"][name]
                assert schema["type"] == "object"
                assert set(schema["required"]) == {"filename", "content_base64"}
                assert schema["properties"]["content_base64"]["type"] == "string"
            for name in tool.file_list_fields:
                schema = tool.input_schema["properties"][name]
                assert schema["type"] == "array"
                assert "content_base64" in schema["items"]["properties"]


# --------------------------------------------------------------------------- descriptions / hints


class TestDescriptionsAndAnnotations:
    def test_descriptions_carry_method_path_and_tag_within_budget(self, catalog):
        for tool in catalog:
            assert tool.description.startswith(f"{tool.method} {tool.path}"), tool.name
            assert tool.description.endswith(f"[{tool.tag}]"), tool.name
            assert len(tool.description) <= MAX_DESCRIPTION_CHARS

    def test_annotations_follow_the_method(self, catalog):
        for tool in catalog:
            hints = tool.hints
            assert hints.open_world is False
            assert hints.read_only is (tool.method == "GET"), tool.name
            assert hints.idempotent is (tool.method in ("GET", "PUT", "DELETE")), tool.name
            if tool.method == "DELETE":
                assert hints.destructive, tool.name
            if any(word in tool.function_name for word in ("delete", "void", "cancel")):
                assert hints.destructive, tool.name
        by_name = {tool.name: tool for tool in catalog}
        assert by_name["delete_work_order"].hints.destructive
        assert (
            by_name["work_orders_list_work_orders"].hints.read_only
            and not by_name["work_orders_list_work_orders"].hints.destructive
        )

    def test_summary_rows_carry_what_print_catalog_prints(self, catalog):
        rows = catalog_summary(catalog[:3])
        assert [row["name"] for row in rows] == [tool.name for tool in catalog[:3]]
        assert set(rows[0]) == {"name", "method", "path", "tag", "function", "annotations", "deprecated"}
        assert set(rows[0]["annotations"]) == {
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
        }


# --------------------------------------------------------------------------- synthetic mapping rules


def _operation(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "operationId": "do_thing_api_v1_things__post",
        "tags": ["Things"],
        "security": [{"OAuth2PasswordBearer": []}],
        "summary": "Do a thing",
    }
    base.update(overrides)
    return base


class TestSyntheticMappingRules:
    def test_body_field_sharing_a_path_param_name_is_renamed_and_mapped_back(self):
        tool = build_tool(
            "put",
            "/api/v1/things/{thing_id}",
            _operation(
                parameters=[{"name": "thing_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                requestBody={
                    "required": True,
                    "content": {
                        JSON_CONTENT_TYPE: {
                            "schema": {
                                "type": "object",
                                "properties": {"thing_id": {"type": "integer"}, "name": {"type": "string"}},
                                "required": ["thing_id"],
                            }
                        }
                    },
                },
            ),
            name="do_thing",
        )
        props = tool.input_schema["properties"]
        assert set(props) == {"thing_id", "body_thing_id", "name"}
        assert tool.body_property_map == {"body_thing_id": "thing_id"}
        assert set(tool.body_properties) == {"body_thing_id", "name"}
        assert set(tool.input_schema["required"]) == {"thing_id", "body_thing_id"}

    def test_non_object_body_is_wrapped_as_a_single_body_property(self):
        tool = build_tool(
            "post",
            "/api/v1/things/bulk",
            _operation(
                requestBody={
                    "required": True,
                    "content": {JSON_CONTENT_TYPE: {"schema": {"type": "array", "items": {"type": "integer"}}}},
                }
            ),
            name="do_thing",
        )
        assert tool.body_wrapped and tool.body_required
        assert tool.body_properties == ("body",)
        assert tool.input_schema["properties"]["body"]["type"] == "array"
        assert tool.input_schema["required"] == ["body"]

    def test_header_and_cookie_params_are_dropped(self):
        tool = build_tool(
            "get",
            "/api/v1/things",
            _operation(
                operationId="list_things_api_v1_things_get",
                parameters=[
                    {"name": "if-none-match", "in": "header", "schema": {"type": "string"}},
                    {"name": "session", "in": "cookie", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
            ),
            name="list_things",
        )
        assert set(tool.input_schema["properties"]) == {"limit"}
        assert tool.query_params == ("limit",)

    def test_refs_are_inlined_including_through_components_and_cycles(self):
        components = {
            "Node": {
                "type": "object",
                "properties": {"children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}}},
            },
            "Wrapper": {"type": "object", "properties": {"node": {"$ref": "#/components/schemas/Node"}}},
        }
        resolved = resolve_schema({"$ref": "#/components/schemas/Wrapper"}, components)
        assert "$ref" not in str(resolved)
        node = resolved["properties"]["node"]
        assert node["type"] == "object"
        # The cycle degrades to a described stub rather than recursing or raising.
        assert "recursive" in node["properties"]["children"]["items"]["description"]
        assert "unavailable" in resolve_schema({"$ref": "#/components/schemas/Missing"}, components)["description"]

    def test_unsecured_and_excluded_operations_are_not_candidates(self):
        doc = {
            "paths": {
                "/api/v1/a": {"get": _operation(operationId="a_api_v1_a_get")},
                "/api/v1/b": {"get": _operation(operationId="b_api_v1_b_get", security=[])},
                "/api/v1/c": {"get": _operation(operationId="c_api_v1_c_get", tags=["Authentication"])},
            }
        }
        assert [path for _m, path, _o, _p in iter_secured_operations(doc)] == ["/api/v1/a"]

    def test_build_catalog_names_before_shadowing(self):
        op_a = _operation(operationId="get_item_api_v1_a__get", tags=["Alpha"])
        op_b = _operation(operationId="get_item_api_v1_b__get", tags=["Beta"])
        doc = {"paths": {"/api/v1/a/{id}": {"get": op_a}, "/api/v1/b/{id}": {"get": op_b}}}
        names = {tool.name for tool in build_catalog(doc, shadowed={("GET", "/api/v1/a/{id}")})}
        # Beta's twin keeps its prefixed name even though Alpha's was dropped.
        assert names == {"beta_get_item"}
        untouched = copy.deepcopy(doc)
        build_catalog(doc)
        assert doc == untouched, "the catalog builder must not mutate the document"
