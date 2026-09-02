"""The generated MCP catalog (``app/mcp/catalog.py`` + ``naming.py``) -- no database.

Brief rule 3: OpenAPI IS the catalog. These tests pin what that means operationally:
every secured operation the SPA can reach is a tool unless a convenience tool
shadows it; the excluded tags and the unsecured routes are absent; names are unique,
wire-valid, and prefixed by tag whenever two routers share a function name -- and the
prefix reaches the family (every sibling in the tag sharing the collision's noun
phrase), so a resource family is prefixed as a whole; a non-object body is one ``body``
property described with the body schema's own title; every
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
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set

import pytest

from app.main import app
from app.mcp.catalog import (
    EXCLUDED_OPERATIONS,
    EXCLUDED_TAGS,
    JSON_CONTENT_TYPE,
    MAX_DESCRIPTION_CHARS,
    MULTIPART_CONTENT_TYPE,
    PUBLIC_OPERATIONS,
    GeneratedTool,
    build_catalog,
    build_tool,
    catalog_summary,
    catalog_tags,
    iter_operations,
    iter_secured_operations,
    resolve_schema,
    unaccounted_operations,
)
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS, ConvenienceTool
from app.mcp.naming import (
    MAX_TOOL_NAME_LENGTH,
    TOOL_NAME_PATTERN,
    assign_tool_names,
    family_prefixed_keys,
    function_name_from_operation_id,
    noun_phrase,
    prefixed_tool_name,
    singularize_token,
    tag_slug,
)

pytestmark = pytest.mark.unit

# ~703 operations, 686 secured, minus 4 excluded-tag groups, 16 shadowed routes and the 2
# excluded cutover loaders. A drop below this means a whole router (or its ``security``
# declarations) vanished.
MIN_GENERATED_TOOLS = 600


def _convenience(name: str) -> ConvenienceTool:
    return next(tool for tool in CONVENIENCE_TOOLS if tool.name == name)


def _raw_body_properties(spec: Mapping[str, Any], operation: Mapping[str, Any]) -> Optional[Set[str]]:
    """The property names of an operation's JSON body straight from the document (one level of $ref)."""
    content = ((operation.get("requestBody") or {}).get("content") or {}).get(JSON_CONTENT_TYPE)
    if not content:
        return None
    components = spec["components"]["schemas"]

    def deref(schema: Any) -> Any:
        while isinstance(schema, dict) and "$ref" in schema:
            schema = components[schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    schema = deref(content.get("schema") or {})
    if isinstance(schema.get("properties"), dict):
        return set(schema["properties"])
    for combinator in ("anyOf", "oneOf"):
        branches = [
            deref(b) for b in schema.get(combinator) or [] if not (isinstance(b, dict) and b.get("type") == "null")
        ]
        if len(branches) == 1 and isinstance(branches[0].get("properties"), dict):
            return set(branches[0]["properties"])
    return None


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
        expected: Set[tuple] = (
            {(method, path) for method, path, _op, _params in iter_secured_operations(spec)}
            - set(SHADOWED_OPERATIONS)
            - set(EXCLUDED_OPERATIONS)
        )
        actual = [tool.key for tool in catalog]
        assert len(actual) == len(set(actual)), "one tool per (method, path)"
        assert set(actual) == expected
        assert len(catalog) > MIN_GENERATED_TOOLS

    def test_shadowed_and_excluded_routes_have_no_generated_twin(self, catalog):
        keys = {tool.key for tool in catalog}
        assert keys.isdisjoint(SHADOWED_OPERATIONS)
        assert keys.isdisjoint(EXCLUDED_OPERATIONS)
        assert set(SHADOWED_OPERATIONS).isdisjoint(EXCLUDED_OPERATIONS)
        # ...and every shadowed / excluded pair really is a secured route in the document,
        # so neither list can silently rot into naming routes that no longer exist.
        secured = {(m, p) for m, p, _o, _pp in iter_secured_operations(app.openapi())}
        assert set(SHADOWED_OPERATIONS) <= secured
        assert set(EXCLUDED_OPERATIONS) <= secured
        # The excluded loaders are the two Excel-cutover routes and nothing else.
        assert set(EXCLUDED_OPERATIONS) == {
            ("POST", "/api/v1/work-orders/import"),
            ("POST", "/api/v1/purchasing/purchase-orders/import"),
        }

    def test_public_operations_name_exactly_the_unsecured_routes(self, spec):
        unsecured = {
            (method, path) for method, path, operation in iter_operations(spec) if not operation.get("security")
        }
        assert unsecured == set(PUBLIC_OPERATIONS), "a new unsecured route must be named in PUBLIC_OPERATIONS"

    def test_every_operation_in_the_document_is_accounted_for(self, spec, catalog):
        assert unaccounted_operations(spec, catalog, shadowed=SHADOWED_OPERATIONS) == set()

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

    def test_collisions_and_their_families_get_a_tag_prefix_and_the_rest_stay_bare(self, spec, catalog):
        entries: Dict[tuple, tuple] = {}
        members: Dict[str, int] = {}
        for method, path, operation, _params in iter_secured_operations(spec):
            fn = function_name_from_operation_id(operation["operationId"], method)
            entries[(method, path)] = (fn, str(operation["tags"][0]))
            members[fn] = members.get(fn, 0) + 1
        collided = {fn for fn, count in members.items() if count > 1}
        assert len(collided) >= 15, "the API is known to carry ~20 colliding function names"
        prefixed = family_prefixed_keys(entries)
        collision_keys = {key for key, (fn, _tag) in entries.items() if fn in collided}
        assert collision_keys < prefixed, "the family rule must reach beyond the collisions themselves"
        for tool in catalog:
            if tool.key in prefixed or tool.function_name in CONVENIENCE_TOOL_NAMES:
                assert tool.name == prefixed_tool_name(tag_slug(tool.tag), tool.function_name), tool.key
                assert tool.name != tool.function_name
            else:
                assert tool.name == tool.function_name, tool.key
        # The family members the rule reaches today, by name: every one shares a noun phrase
        # with a collision in its own tag and would otherwise have been the bare odd-one-out.
        propagated = {entries[key][0] for key in prefixed - collision_keys}
        assert propagated == {
            "delete_finish",
            "delete_machine",
            "update_finish",
            "update_machine",
            "get_material",
            "delete_operation",
            "reorder_operations",
            "resume_operation",
            "delete_work_order",
            "restore_work_order",
            # Shadowed Work Orders twins are named too (names are assigned before shadowing).
            "duplicate_work_order",
            "release_work_order",
        }

    def test_known_collisions(self, catalog):
        names = {tool.name for tool in catalog}
        assert "start_operation" not in names
        assert {"work_orders_start_operation", "shop_floor_start_operation"} <= names
        # Shadowing Work Orders' add_operation/get_work_order/create_work_order must not
        # hand the bare names to their Routing / Preventive Maintenance twins.
        assert {"routing_add_operation", "preventive_maintenance_get_work_order"} <= names
        assert names.isdisjoint(CONVENIENCE_TOOL_NAMES)

    def test_known_families(self, catalog):
        """The family rule, on the live catalog: the ten names it moved, and the neighbours it left alone."""
        names = {tool.name for tool in catalog}
        family = {
            "admin_settings_delete_finish",
            "admin_settings_delete_machine",
            "admin_settings_update_finish",
            "admin_settings_update_machine",
            "materials_supplies_get_material",
            "routing_delete_operation",
            "routing_reorder_operations",
            "shop_floor_resume_operation",
            "work_orders_delete_work_order",
            "work_orders_restore_work_order",
        }
        assert family <= names
        assert names.isdisjoint(
            {
                "delete_finish",
                "delete_machine",
                "update_finish",
                "update_machine",
                "get_material",
                "delete_operation",
                "reorder_operations",
                "resume_operation",
                "delete_work_order",
                "restore_work_order",
            }
        )
        # Same tags, different noun phrase: untouched. (A noun phrase is the whole tail, so
        # ``work_order_by_number`` and ``operation_on_hold`` are not ``work_order`` / ``operation``.)
        assert {
            "create_labor_rate",
            "list_work_center_types_admin",
            "import_materials_csv",
            "copy_routing",
            "put_operation_on_hold",
            "get_all_operations",
            "get_work_order_by_number",
            "update_work_order_priority",
            "preview_work_order_operations",
        } <= names
        # A reserved-name prefix never seeds a family: the Parts / Inventory / Work Centers
        # resource families stay bare although a convenience tool owns their list verb.
        assert {"get_part", "update_part", "receive_inventory", "create_work_center", "get_purchase_order"} <= names

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

    def test_noun_phrase_derivation(self):
        # ies -> y; es dropped after sh/ch/x/s/z; otherwise a trailing s dropped; ss left alone.
        assert singularize_token("entries") == "entry"
        assert singularize_token("finishes") == "finish"
        assert singularize_token("losses") == "loss"
        assert singularize_token("boxes") == "box"
        assert singularize_token("machines") == "machine"
        assert singularize_token("orders") == "order"
        assert singularize_token("process") == "process"
        assert singularize_token("material") == "material"
        assert singularize_token("s") == "s" and singularize_token("es") == "e"
        # The noun phrase is the tail after the verb, last token singularized.
        assert noun_phrase("list_work_orders") == "work_order"
        assert noun_phrase("delete_work_order") == "work_order"
        assert noun_phrase("list_finishes") == "finish"
        assert noun_phrase("get_material") == "material"
        assert noun_phrase("reorder_operations") == "operation"
        assert noun_phrase("get_all_operations") == "all_operation"
        assert noun_phrase("get_work_order_by_number") == "work_order_by_number"
        assert noun_phrase("put_operation_on_hold") == "operation_on_hold"
        assert noun_phrase("search") == "" and noun_phrase("") == ""
        # The documented false negative: ``clauses`` -> ``claus`` never meets ``clause``.
        assert noun_phrase("list_clauses") != noun_phrase("create_clause")

    def test_family_prefix_propagation_on_synthetic_entries(self):
        entries = {
            # Admin Settings' list_machines collides with Quote Calculator's ...
            "a-list": ("list_machines", "Admin Settings"),
            "q-list": ("list_machines", "Quote Calculator"),
            # ... so the Admin Settings machine family is prefixed as a whole ...
            "a-update": ("update_machine", "Admin Settings"),
            "a-delete": ("delete_machine", "Admin Settings"),
            # ... but a different noun phrase in the same tag stays bare ...
            "a-rate": ("update_labor_rate", "Admin Settings"),
            "a-types": ("list_machine_types", "Admin Settings"),
            # ... and the SAME noun phrase in an uninvolved tag stays bare too.
            "s-update": ("update_machine_setting", "Scheduling"),
            "x-get": ("get_machine", "Maintenance"),
            # A one-token function has no noun phrase and can neither seed nor be reached.
            "g-search": ("search", "Global Search"),
        }
        names = assign_tool_names(entries)
        assert names == {
            "a-list": "admin_settings_list_machines",
            "q-list": "quote_calculator_list_machines",
            "a-update": "admin_settings_update_machine",
            "a-delete": "admin_settings_delete_machine",
            "a-rate": "update_labor_rate",
            "a-types": "list_machine_types",
            "s-update": "update_machine_setting",
            "x-get": "get_machine",
            "g-search": "search",
        }
        assert family_prefixed_keys(entries) == {"a-list", "q-list", "a-update", "a-delete"}
        # Propagation is one pass: a reached member widens nothing (its noun phrase is the seed).
        assert family_prefixed_keys({**entries, "a-more": ("purge_machines", "Admin Settings")}) == {
            "a-list",
            "q-list",
            "a-update",
            "a-delete",
            "a-more",
        }
        # A reserved name prefixes the lone function but seeds no family.
        reserved = assign_tool_names(
            {"p-list": ("list_parts", "Parts"), "p-get": ("get_part", "Parts")}, reserved={"list_parts"}
        )
        assert reserved == {"p-list": "parts_list_parts", "p-get": "get_part"}
        # Independent of insertion order.
        assert assign_tool_names(dict(reversed(list(entries.items())))) == names

    def test_prefixed_name_fits_64_chars_slug_first(self):
        long_fn = "x" * 60
        name = prefixed_tool_name("a_very_long_tag_slug", long_fn)
        assert len(name) <= MAX_TOOL_NAME_LENGTH
        assert name.endswith(long_fn) and name.startswith("a_v")
        assert prefixed_tool_name("tag", "y" * 70) == "y" * 64

    def test_assign_tool_names_is_total_and_deterministic(self):
        # Two entries under one tag with one function name: the policy suffixes the second
        # (in sorted-key order) rather than raising -- build_door runs at import of
        # app.main, and a naming nicety must not keep the API from booting.
        expected = {"a": "parts_list_items", "b": "parts_list_items_2"}
        assert assign_tool_names({"a": ("list_items", "Parts"), "b": ("list_items", "Parts")}) == expected
        assert assign_tool_names({"b": ("list_items", "Parts"), "a": ("list_items", "Parts")}) == expected
        # A 64-char fit that folds two names together is disambiguated inside the cap.
        folded = assign_tool_names({"x": ("y" * 70 + "a", "T"), "z": ("y" * 70 + "b", "T")})
        assert folded == {"x": "y" * 64, "z": "y" * 62 + "_2"}
        assert all(TOOL_NAME_PATTERN.match(name) for name in folded.values())
        # A suffix never lands on a name another entry already holds.
        three = assign_tool_names({"a": ("x", "T"), "b": ("x", "T"), "c": ("x_2", "T")})
        assert three == {"a": "t_x", "b": "t_x_2", "c": "x_2"}
        # A reserved bare name pushes a lone function onto the prefixed form.
        assert assign_tool_names({"a": ("search", "Global Search")}, reserved={"search"}) == {
            "a": "global_search_search"
        }

    def test_no_live_name_needed_the_numeric_suffix(self, catalog):
        assert not [tool.name for tool in catalog if re.search(r"_\d+$", tool.name)]


# --------------------------------------------------------------------------- schemas


class TestInputSchemas:
    def test_no_ref_survives_anywhere_and_every_schema_is_strict(self, catalog):
        for tool in catalog:
            for node in _walk(tool.input_schema):
                if isinstance(node, dict):
                    assert "$ref" not in node, f"{tool.name}: unresolved $ref {node['$ref']}"
            assert tool.input_schema["type"] == "object"
            assert isinstance(tool.input_schema["properties"], dict)
            assert tool.input_schema["additionalProperties"] is False, tool.name

    def test_json_body_fields_are_all_present_including_one_named_title(self, spec, catalog):
        """The tool's body property set equals the document's -- ``title`` included.

        ``resolve_schema`` strips pydantic's auto-``title`` KEYWORD; 17 routes also have
        a body FIELD called ``title`` (NCR, CAR, ECO, complaint, process sheet, QMS
        clause / evidence, maintenance work order, AI recommendation, document upload)
        and 10 of them require it, so a tool that lost the field could never succeed.
        """
        by_key = {tool.key: tool for tool in catalog}
        checked = 0
        for method, path, operation, _params in iter_secured_operations(spec):
            tool = by_key.get((method, path))
            if tool is None or tool.body_content_type != JSON_CONTENT_TYPE or tool.body_wrapped:
                continue
            raw = _raw_body_properties(spec, operation)
            if raw is None:
                continue
            sent = {tool.body_property_map.get(name, name) for name in tool.body_properties}
            assert sent == raw, tool.name
            checked += 1
        assert checked > 150, "the comparison must cover the bulk of the JSON-body tools"
        ncr = by_key[("POST", "/api/v1/quality/ncr")]
        assert "title" in ncr.body_properties and "title" in ncr.input_schema["properties"]
        assert "title" in ncr.input_schema["required"]
        assert "title" not in ncr.input_schema["properties"]["title"], "the keyword is stripped, the field is kept"
        titled = [tool.name for tool in catalog if "title" in tool.body_properties]
        assert len(titled) >= 16, titled

    def test_path_params_are_required_and_body_fields_are_merged(self, catalog):
        update = next(tool for tool in catalog if tool.key == ("PUT", "/api/v1/work-orders/operations/{operation_id}"))
        assert update.name == "work_orders_update_operation"
        props = update.input_schema["properties"]
        assert "operation_id" in props and "version" in props and "status" in props
        assert "operation_id" in update.input_schema["required"]
        assert "version" in update.input_schema["required"]
        assert update.body_content_type == JSON_CONTENT_TYPE
        assert "version" in update.body_properties and "operation_id" not in update.body_properties
        assert update.path_params == ("operation_id",)

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
        assert by_name["work_orders_delete_work_order"].hints.destructive
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
        assert tool.input_schema["properties"]["body"]["description"] == "The request body, sent as-is."

    def test_wrapped_body_description_carries_the_schemas_title(self):
        """A wrapped body is an opaque ``body`` argument; the schema's title is what says what it IS."""

        def wrapped(schema: Dict[str, Any], components: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            tool = build_tool(
                "post",
                "/api/v1/things",
                _operation(requestBody={"required": False, "content": {JSON_CONTENT_TYPE: {"schema": schema}}}),
                name="do_thing",
                components=components,
            )
            assert tool.body_wrapped
            return tool.input_schema["properties"]["body"]

        # The shape FastAPI emits for ``Optional[Dict[str, Any]] = Body(None, title=...)``.
        optional_dict = wrapped(
            {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}], "title": "Cutting Speeds"}
        )
        assert optional_dict["description"] == "The request body (Cutting Speeds), sent as-is."
        assert "title" not in optional_dict, "the keyword itself is still stripped"
        # A bare list with the title on the array.
        assert wrapped({"type": "array", "items": {"type": "string"}, "title": "Permissions"})["description"] == (
            "The request body (Permissions), sent as-is."
        )
        # A ``$ref`` to a non-object component: the component's title, its name as the fallback.
        components = {
            "IdList": {"type": "array", "items": {"type": "integer"}, "title": "Id List"},
            "Untitled": {"type": "array", "items": {"type": "integer"}, "description": "Ids in order."},
        }
        assert wrapped({"$ref": "#/components/schemas/IdList"}, components)["description"] == (
            "The request body (Id List), sent as-is."
        )
        untitled = wrapped({"$ref": "#/components/schemas/Untitled"}, components)
        assert untitled["description"] == "The request body (Untitled), sent as-is. Ids in order."
        # The optional branch is followed when the wrapper itself carries no title.
        assert wrapped({"anyOf": [{"$ref": "#/components/schemas/IdList"}, {"type": "null"}]}, components)[
            "description"
        ] == ("The request body (Id List), sent as-is.")

    def test_live_wrapped_bodies_say_what_they_are(self, catalog):
        by_name = {tool.name: tool for tool in catalog}
        for name, title in (
            ("quote_calculator_create_machine", "Cutting Speeds"),
            ("quote_calculator_create_material", "Sheet Pricing"),
            ("routing_reorder_operations", "Operation Order"),
            ("update_role_permissions", "Permissions"),
        ):
            tool = by_name[name]
            assert tool.body_wrapped, name
            assert f"({title})" in tool.input_schema["properties"]["body"]["description"], name

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

    def test_a_body_property_named_title_survives_and_only_the_keyword_is_stripped(self):
        components = {
            "Ncr": {
                "title": "Ncr",
                "type": "object",
                "properties": {
                    "title": {"title": "Title", "type": "string", "default": {"title": "kept-as-data"}},
                    "severity": {"title": "Severity", "type": "string", "enum": ["title", "minor"]},
                },
                "required": ["title"],
            }
        }
        tool = build_tool(
            "post",
            "/api/v1/ncr",
            _operation(
                requestBody={
                    "required": True,
                    "content": {JSON_CONTENT_TYPE: {"schema": {"$ref": "#/components/schemas/Ncr"}}},
                }
            ),
            name="create_ncr",
            components=components,
        )
        props = tool.input_schema["properties"]
        assert set(props) == {"title", "severity"}
        assert "title" not in props["title"] and "title" not in props["severity"], "the keyword is gone"
        assert props["title"]["default"] == {"title": "kept-as-data"}, "opaque data is never walked"
        assert props["severity"]["enum"] == ["title", "minor"]
        assert tool.body_properties == ("title", "severity")
        assert tool.input_schema["required"] == ["title"]
        assert tool.input_schema["additionalProperties"] is False

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

    def test_excluded_operations_are_dropped_without_renaming_neighbours(self):
        op_a = _operation(operationId="load_api_v1_a__post", tags=["Alpha"])
        op_b = _operation(operationId="load_api_v1_b__post", tags=["Beta"])
        doc = {"paths": {"/api/v1/a": {"post": op_a}, "/api/v1/b": {"post": op_b}}}
        names = {tool.name for tool in build_catalog(doc, excluded_operations={("POST", "/api/v1/a")})}
        assert names == {"beta_load"}


# --------------------------------------------------------------------------- convenience schemas


class TestConvenienceSchemasMirrorTheirRoutes:
    """A hand-written schema that fronts a route must not drift from the route's own contract."""

    @pytest.fixture(scope="class")
    def unshadowed(self, spec) -> Dict[tuple, GeneratedTool]:
        return {tool.key: tool for tool in build_catalog(spec, shadowed=())}

    def test_update_work_order_forwards_every_work_order_update_field(self, unshadowed):
        generated = unshadowed[("PUT", "/api/v1/work-orders/{work_order_id}")]
        convenience = _convenience("update_work_order")
        assert set(convenience.input_schema["properties"]) == set(generated.input_schema["properties"])
        assert set(convenience.input_schema["required"]) == set(generated.input_schema["required"])

    def test_add_laser_nest_mirrors_the_manual_nest_route_plus_release(self, unshadowed):
        generated = unshadowed[("POST", "/api/v1/work-orders/{work_order_id}/laser-nests/manual")]
        convenience = _convenience("add_laser_nest")
        assert set(convenience.input_schema["properties"]) - {"release"} == set(generated.input_schema["properties"])
        assert set(convenience.input_schema["required"]) == set(generated.input_schema["required"])

    def test_every_convenience_schema_is_strict_and_claims_a_tag(self):
        for tool in CONVENIENCE_TOOLS:
            assert tool.input_schema["additionalProperties"] is False, tool.name
            assert re.search(r"\[[^\]]+\]\s*$", tool.description), tool.name
        assert "status" not in _convenience("create_work_order").input_schema["properties"]
        assert set(_convenience("duplicate_work_order").input_schema["required"]) == {
            "work_order_id",
            "quantity_ordered",
        }
        assert "quantity" in _convenience("add_operation").input_schema["properties"]
