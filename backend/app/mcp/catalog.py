"""OpenAPI document -> generated MCP tool catalog.

Pure: no app imports beyond ``app.mcp.naming``, no I/O, no database -- it takes the
dict ``app.openapi()`` returns and nothing else, so it is unit-testable in isolation
and ``python -m app.mcp --print-catalog`` can run without a DATABASE_URL.

WHY OpenAPI is the catalog (brief rule 3): the same document the SPA's Swagger page
renders is the ground truth of what the API does, so a hand-maintained list of ~700
tool names could only ever be stale. The catalog records, per tool, everything the
executor needs to turn a flat MCP argument object back into a real HTTP request
(path/query locations, the body content type, which properties belong to the body and
under what field name, which multipart fields are files). It never decides who may
call what: the route it dispatches to is the RBAC/tenancy/audit boundary.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from app.mcp.naming import assign_tool_names, function_name_from_operation_id

# Tags whose operations are session plumbing or machine-to-machine surfaces, not user
# actions: identity comes from the JWT (Authentication), carriers speak HMAC (Carrier
# Webhooks), the browser error beacon is unauthenticated (Error Logging / its router-
# level "errors" tag), and WebSocket routes never appear in OpenAPI but are listed so
# the exclusion set is complete on paper. Everything else with a ``security`` block is
# in -- Data Export, Users, Audit, Platform Administration included: their own
# ``require_role`` gates and audit rows are the control, not the catalog.
EXCLUDED_TAGS: FrozenSet[str] = frozenset(
    {"Authentication", "Carrier Webhooks", "Error Logging", "errors", "WebSocket"}
)

# Secured operations that are deliberately NOT tools and have no convenience twin: the
# two Excel-cutover loaders (``migration_import_service``). They record a PAST system's
# state rather than perform a user action -- the work-order loader mints work orders
# RELEASED / IN_PROGRESS with operations READY / COMPLETE on no labor evidence, the PO
# loader mints purchase orders SENT -- which is exactly the side door around "create
# lands DRAFT, release is explicit" (brief rule 5) and "never fake shop-floor time"
# (rule 8) that the convenience tools exist to close. They stay an Import Center
# action for an admin at cutover (docs/EXCEL_MIGRATION_RUNBOOK.md).
EXCLUDED_OPERATIONS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/work-orders/import"),
        ("POST", "/api/v1/purchasing/purchase-orders/import"),
    }
)

# Every operation in the document that declares NO ``security`` block, BY NAME. None
# is a user action: health probes, credential exchanges, station PIN logins, the public
# claim/registration forms, the unauthenticated browser error beacon, the HMAC-verified
# carrier webhook. ``iter_secured_operations`` skips unsecured routes silently, and this
# list is what turns that silence into an explicit exclusion: ``unaccounted_operations``
# (the CI coverage guard) reports any unsecured route NOT named here, so a new router
# that forgets its ``security`` dependency -- even under an existing tag -- fails CI
# instead of quietly never becoming a tool. Adding a public route means naming it here.
PUBLIC_OPERATIONS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/health/detailed"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/employee-login"),
        ("POST", "/api/v1/auth/refresh"),
        ("GET", "/api/v1/auth/setup-status"),
        ("POST", "/api/v1/auth/register-public"),
        ("POST", "/api/v1/auth/display-token/claim"),
        ("POST", "/api/v1/auth/reset-database"),
        ("POST", "/api/v1/shop-floor/kiosk-stations/station-login"),
        ("POST", "/api/v1/webhooks/carriers/{provider}"),
        ("POST", "/api/v1/companies/register"),
        ("POST", "/api/v1/visitor-logs/station-login"),
        ("POST", "/api/v1/errors/log"),
        ("GET", "/api/v1/errors/health"),
    }
)

HTTP_METHODS: Tuple[str, ...] = ("get", "post", "put", "patch", "delete")

JSON_CONTENT_TYPE = "application/json"
MULTIPART_CONTENT_TYPE = "multipart/form-data"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_BODY_CONTENT_PREFERENCE = (JSON_CONTENT_TYPE, MULTIPART_CONTENT_TYPE, FORM_CONTENT_TYPE)

MAX_DESCRIPTION_CHARS = 600
# Component schemas can reference each other in cycles (BOM trees, nested WO
# responses); the resolver stops at this depth and leaves a described stub.
MAX_REF_DEPTH = 12
_DESTRUCTIVE_WORDS = ("delete", "void", "cancel", "purge", "reset", "hard")
_WRAPPED_BODY_PROPERTY = "body"
_REF_PREFIX = "#/components/schemas/"
_WHITESPACE = re.compile(r"\s+")
# Keywords whose value is a map of NAME -> schema. The names are data -- a body field
# may legitimately be called ``title`` (NCRs, CARs, ECOs, documents, process sheets,
# complaints, QMS clauses and evidence all have one) -- so only the schemas beneath
# them are walked and the keys are kept verbatim.
_SCHEMA_MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
# Keywords whose value is opaque data (an example object, an enum of literals), never a
# schema: copied as-is, never walked, so a literal ``{"title": ...}`` inside a default
# survives too.
_OPAQUE_KEYWORDS = ("default", "example", "examples", "const", "enum")


@dataclass(frozen=True)
class ToolAnnotationHints:
    """The MCP ``ToolAnnotations`` bits, derived from the HTTP method and the name."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool = False

    def as_dict(self) -> Dict[str, bool]:
        return {
            "read_only_hint": self.read_only,
            "destructive_hint": self.destructive,
            "idempotent_hint": self.idempotent,
            "open_world_hint": self.open_world,
        }


@dataclass
class GeneratedTool:
    """One catalog entry: the MCP-facing tool plus the recipe to rebuild the HTTP call."""

    name: str
    function_name: str
    tag: str
    method: str  # upper-case HTTP method
    path: str  # path template, already prefixed with /api/v1
    summary: str
    description: str
    input_schema: Dict[str, Any]
    hints: ToolAnnotationHints
    path_params: Tuple[str, ...] = ()
    query_params: Tuple[str, ...] = ()
    body_content_type: Optional[str] = None
    # Input-schema property names that are sent in the body (JSON fields, form fields
    # or file fields). ``body_property_map`` translates an input name back to the wire
    # field name where the two differ (a body field renamed ``body_<x>`` because a path
    # or query parameter already owned ``<x>``).
    body_properties: Tuple[str, ...] = ()
    body_property_map: Dict[str, str] = field(default_factory=dict)
    # True when the whole body is one ``body`` property (non-object bodies: a bare
    # array, a free-form dict, an optional model the caller may omit entirely).
    body_wrapped: bool = False
    body_required: bool = False
    # Multipart file fields, by INPUT name. ``file_list_fields`` take a list of files.
    file_fields: Tuple[str, ...] = ()
    file_list_fields: Tuple[str, ...] = ()
    deprecated: bool = False
    operation_id: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.method, self.path)


# --------------------------------------------------------------------------- $ref resolution


def resolve_schema(
    schema: Any,
    components: Mapping[str, Any],
    *,
    depth: int = 0,
    stack: Tuple[str, ...] = (),
) -> Any:
    """Return ``schema`` with every local ``$ref`` inlined.

    MCP clients receive a self-contained ``inputSchema`` per tool; there is no
    ``components`` section to point at. Cycles and runaway depth degrade to a
    described stub rather than raising, because one pathological schema must not
    take the other ~650 tools down with it.
    """
    if isinstance(schema, list):
        return [resolve_schema(item, components, depth=depth, stack=stack) for item in schema]
    if not isinstance(schema, dict):
        return schema

    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref[len(_REF_PREFIX) :] if ref.startswith(_REF_PREFIX) else ref
        if name in stack or depth >= MAX_REF_DEPTH:
            return {"description": f"Schema for {name} omitted (recursive reference)."}
        target = components.get(name)
        if target is None:
            return {"description": f"Schema for {name} unavailable."}
        resolved = resolve_schema(target, components, depth=depth + 1, stack=stack + (name,))
        # Sibling keys next to a $ref (description, default) are kept, per JSON Schema 2019+.
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        if isinstance(resolved, dict):
            merged = dict(resolved)
            merged.update(resolve_schema(siblings, components, depth=depth, stack=stack))
            return merged
        return resolved

    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            # Pydantic's auto-titles ("Work Order Id") are noise next to the property
            # name. This strips the KEYWORD only: a property NAMED ``title`` lives inside
            # a ``properties`` map and is kept by the branch below.
            continue
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {
                str(name): resolve_schema(sub_schema, components, depth=depth, stack=stack)
                for name, sub_schema in value.items()
            }
        elif key in _OPAQUE_KEYWORDS:
            out[key] = copy.deepcopy(value)
        else:
            out[key] = resolve_schema(value, components, depth=depth, stack=stack)
    return out


def _non_null_branch(schema: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """For ``anyOf/oneOf: [X, {type: null}]`` return X; otherwise None."""
    for combinator in ("anyOf", "oneOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list):
            non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(non_null) == 1 and len(non_null) != len(branches) and isinstance(non_null[0], dict):
                return dict(non_null[0])
    return None


def _object_with_properties(schema: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return ``(object_schema, optional)`` when ``schema`` is (or wraps) a property-bearing object."""
    if not isinstance(schema, dict):
        return None, False
    if isinstance(schema.get("properties"), dict):
        return schema, False
    branch = _non_null_branch(schema)
    if branch is not None and isinstance(branch.get("properties"), dict):
        return branch, True
    return None, False


def _file_kind(schema: Any) -> Optional[str]:
    """``"file"`` / ``"file_list"`` / None for a multipart property schema.

    FastAPI 0.136 + pydantic 2.12 emit ``{"type": "string", "contentMediaType":
    "application/octet-stream"}`` for an ``UploadFile``; older generators emitted
    ``format: binary``. Both are recognised.
    """
    if not isinstance(schema, dict):
        return None
    inner = _non_null_branch(schema) or schema
    if inner.get("type") == "string" and (inner.get("format") == "binary" or "contentMediaType" in inner):
        return "file"
    if inner.get("type") == "array" and _file_kind(inner.get("items")) == "file":
        return "file_list"
    return None


def file_argument_schema(description: Optional[str] = None) -> Dict[str, Any]:
    """The MCP-side shape of one uploaded file."""
    return {
        "type": "object",
        "description": description or "A file to upload.",
        "properties": {
            "filename": {"type": "string", "description": "File name as it should reach the server."},
            "content_base64": {"type": "string", "description": "The file bytes, base64-encoded."},
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (defaults to application/octet-stream).",
            },
        },
        "required": ["filename", "content_base64"],
    }


# --------------------------------------------------------------------------- description / hints


def _first_paragraph(text: Optional[str]) -> str:
    if not text:
        return ""
    paragraph = text.strip().split("\n\n", 1)[0]
    return _WHITESPACE.sub(" ", paragraph).strip()


def describe_operation(method: str, path: str, operation: Mapping[str, Any], tag: str) -> str:
    """``"GET /api/v1/x — Summary. First docstring paragraph. [Tag]"``, capped at ~600 chars."""
    summary = _WHITESPACE.sub(" ", (operation.get("summary") or "").strip())
    paragraph = _first_paragraph(operation.get("description"))
    text = f"{method} {path}"
    if summary:
        text += f" — {summary.rstrip('.')}."
    if paragraph and paragraph.lower() != summary.lower():
        text += f" {paragraph}"
    if operation.get("deprecated"):
        text += " (DEPRECATED)"
    suffix = f" [{tag}]"
    budget = MAX_DESCRIPTION_CHARS - len(suffix)
    if len(text) > budget:
        text = text[: budget - 1].rstrip() + "…"
    return text + suffix


def annotation_hints(method: str, function_name: str) -> ToolAnnotationHints:
    lowered = function_name.lower()
    return ToolAnnotationHints(
        read_only=method == "GET",
        destructive=method == "DELETE" or any(word in lowered for word in _DESTRUCTIVE_WORDS),
        idempotent=method in ("GET", "PUT", "DELETE"),
        open_world=False,
    )


# --------------------------------------------------------------------------- building


def _merge_parameters(path_level: Sequence[Any], op_level: Sequence[Any]) -> List[Dict[str, Any]]:
    """Operation-level parameters override path-level ones with the same (name, in)."""
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for param in list(path_level) + list(op_level):
        if isinstance(param, dict) and "name" in param and "in" in param:
            merged[(param["name"], param["in"])] = param
    return list(merged.values())


def _free_input_name(wanted: str, taken: Mapping[str, Any]) -> str:
    name = wanted
    while name in taken:
        name = f"body_{name}"
    return name


def build_tool(
    method: str,
    path: str,
    operation: Mapping[str, Any],
    *,
    name: str,
    path_level_parameters: Sequence[Any] = (),
    components: Optional[Mapping[str, Any]] = None,
) -> GeneratedTool:
    """Build one ``GeneratedTool`` from an OpenAPI operation object."""
    components = components or {}
    method = method.upper()
    tags = operation.get("tags") or ["untagged"]
    tag = str(tags[0])
    operation_id = str(operation.get("operationId") or "")
    function_name = function_name_from_operation_id(operation_id, method)

    properties: Dict[str, Any] = {}
    required: List[str] = []
    path_params: List[str] = []
    query_params: List[str] = []

    for param in _merge_parameters(path_level_parameters, operation.get("parameters") or []):
        location = param.get("in")
        if location not in ("path", "query"):
            # Header/cookie parameters (one ETag ``if-none-match`` in this API) are
            # transport concerns the executor never forwards.
            continue
        pname = str(param["name"])
        schema = resolve_schema(param.get("schema") or {}, components)
        if not isinstance(schema, dict):
            schema = {}
        if param.get("description") and "description" not in schema:
            schema["description"] = param["description"]
        properties[pname] = schema
        if location == "path":
            path_params.append(pname)
            required.append(pname)
        else:
            query_params.append(pname)
            if param.get("required"):
                required.append(pname)

    body_content_type: Optional[str] = None
    body_properties: List[str] = []
    body_property_map: Dict[str, str] = {}
    body_wrapped = False
    body_required = False
    file_fields: List[str] = []
    file_list_fields: List[str] = []

    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content") or {}
        chosen = next((ct for ct in _BODY_CONTENT_PREFERENCE if ct in content), None)
        if chosen is None and content:
            chosen = next(iter(content))
        if chosen is not None:
            body_content_type = chosen
            body_required = bool(request_body.get("required"))
            raw_schema = (content[chosen] or {}).get("schema") or {}
            body_schema = resolve_schema(raw_schema, components)
            object_schema, optional_body = _object_with_properties(body_schema)
            if object_schema is not None:
                object_required = set(object_schema.get("required") or []) if not optional_body else set()
                for field_name, field_schema in object_schema["properties"].items():
                    input_name = _free_input_name(str(field_name), properties)
                    if input_name != field_name:
                        body_property_map[input_name] = str(field_name)
                    if chosen in (MULTIPART_CONTENT_TYPE, FORM_CONTENT_TYPE):
                        kind = _file_kind(field_schema)
                        if kind == "file":
                            description = field_schema.get("description") if isinstance(field_schema, dict) else None
                            properties[input_name] = file_argument_schema(description)
                            file_fields.append(input_name)
                        elif kind == "file_list":
                            properties[input_name] = {"type": "array", "items": file_argument_schema()}
                            file_list_fields.append(input_name)
                        else:
                            properties[input_name] = field_schema
                    else:
                        properties[input_name] = field_schema
                    body_properties.append(input_name)
                    if field_name in object_required:
                        required.append(input_name)
            else:
                body_wrapped = True
                input_name = _free_input_name(_WRAPPED_BODY_PROPERTY, properties)
                wrapped = body_schema if isinstance(body_schema, dict) else {}
                wrapped = dict(wrapped)
                wrapped.setdefault("description", "The request body, sent as-is.")
                properties[input_name] = wrapped
                body_properties.append(input_name)
                if body_required and _non_null_branch(body_schema if isinstance(body_schema, dict) else {}) is None:
                    required.append(input_name)

    # ``additionalProperties: false`` so a misspelled argument (``serach``) is a 422 that
    # names it, not a silently dropped filter that hands the caller the UNFILTERED
    # result with ``isError: false``. The executor only ever forwards known
    # properties, so this changes what the caller is TOLD, never what is sent.
    input_schema: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        # Preserve declaration order, drop duplicates.
        input_schema["required"] = list(dict.fromkeys(required))

    return GeneratedTool(
        name=name,
        function_name=function_name,
        tag=tag,
        method=method,
        path=path,
        summary=str(operation.get("summary") or ""),
        description=describe_operation(method, path, operation, tag),
        input_schema=input_schema,
        hints=annotation_hints(method, function_name),
        path_params=tuple(path_params),
        query_params=tuple(query_params),
        body_content_type=body_content_type,
        body_properties=tuple(body_properties),
        body_property_map=body_property_map,
        body_wrapped=body_wrapped,
        body_required=body_required,
        file_fields=tuple(file_fields),
        file_list_fields=tuple(file_list_fields),
        deprecated=bool(operation.get("deprecated")),
        operation_id=operation_id,
    )


def iter_secured_operations(
    spec: Mapping[str, Any],
    *,
    excluded_tags: Iterable[str] = EXCLUDED_TAGS,
) -> Iterable[Tuple[str, str, Dict[str, Any], List[Any]]]:
    """Yield ``(METHOD, path, operation, path_level_parameters)`` for every catalog candidate.

    An operation is a candidate when it declares ``security`` (a route with no
    security block is a station login, a public claim or a health probe -- none are
    user actions) and none of its tags is excluded.
    """
    excluded = set(excluded_tags)
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_level = path_item.get("parameters") or []
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            if not operation.get("security"):
                continue
            if any(tag in excluded for tag in (operation.get("tags") or [])):
                continue
            yield method.upper(), str(path), operation, list(path_level)


def build_catalog(
    spec: Mapping[str, Any],
    *,
    shadowed: Iterable[Tuple[str, str]] = (),
    excluded_tags: Iterable[str] = EXCLUDED_TAGS,
    excluded_operations: Iterable[Tuple[str, str]] = EXCLUDED_OPERATIONS,
    reserved_names: Iterable[str] = (),
) -> List[GeneratedTool]:
    """Turn an OpenAPI document into the generated tool list, sorted by name.

    ``shadowed`` is the set of ``(METHOD, path)`` pairs a convenience tool replaces;
    their generated twins are dropped so the convenience tool is the only door to
    that route (it is where the DRAFT / demote / name-guard rules live).
    ``excluded_operations`` are dropped with NO replacement (the cutover loaders).

    Names are assigned over EVERY secured operation BEFORE the shadowed and excluded
    pairs are dropped, so neither ever renames a neighbour: the Routing router's
    ``add_operation`` is ``routing_add_operation`` whether or not the Work Orders twin
    is shadowed, and a generated name is the same on every transport. ``reserved_names``
    (the convenience tools' fixed names) additionally keep any lone generated
    function from taking a bare name a convenience tool owns.
    """
    dropped: Set[Tuple[str, str]] = set(shadowed) | set(excluded_operations)
    components = (spec.get("components") or {}).get("schemas") or {}

    secured: Dict[Tuple[str, str], Tuple[Dict[str, Any], List[Any]]] = {}
    for method, path, operation, path_level in iter_secured_operations(spec, excluded_tags=excluded_tags):
        secured[(method, path)] = (operation, path_level)

    names = assign_tool_names(
        {
            key: (
                function_name_from_operation_id(str(operation.get("operationId") or ""), key[0]),
                str((operation.get("tags") or ["untagged"])[0]),
            )
            for key, (operation, _path_level) in secured.items()
        },
        reserved=reserved_names,
    )

    tools = [
        build_tool(
            method,
            path,
            operation,
            name=names[(method, path)],
            path_level_parameters=path_level,
            components=components,
        )
        for (method, path), (operation, path_level) in secured.items()
        if (method, path) not in dropped
    ]
    tools.sort(key=lambda tool: tool.name)
    return tools


def catalog_tags(spec: Mapping[str, Any]) -> Set[str]:
    """Every tag that appears on any operation in the document (for the coverage guard)."""
    tags: Set[str] = set()
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                tags.update(str(tag) for tag in (operation.get("tags") or []))
    return tags


def iter_operations(spec: Mapping[str, Any]) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    """Yield ``(METHOD, path, operation)`` for EVERY operation in the document, secured or not."""
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                yield method.upper(), str(path), operation


def uncovered_tags(
    spec: Mapping[str, Any],
    tools: Sequence[GeneratedTool],
    *,
    shadowed: Iterable[Tuple[str, str]] = (),
    excluded_tags: Iterable[str] = EXCLUDED_TAGS,
    convenience_tags: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Tags in the document that NO tool reaches: the "a new router shipped and MCP is blind" guard.

    A tag is covered when a generated tool carries it or when one of its operations
    is in ``shadowed`` (a convenience tool stands in for that route -- ``Global
    Search`` has exactly one operation and it is shadowed). When ``convenience_tags``
    is given (the ``[Tag]`` each convenience description claims), the shadow credit
    is granted ONLY for tags a convenience tool actually claims -- a route added to
    ``SHADOWED_OPERATIONS`` with no convenience tool behind it is then reported, not
    credited. ``excluded_tags`` are the deliberate, named omissions. Anything left
    over is a router whose operations yielded no tool -- typically because none of
    them declares ``security`` -- and the fix is either a ``security`` block on the
    routes or an explicit entry in ``EXCLUDED_TAGS``, never silence.

    This is TAG-granular: a new router under an EXISTING tag cannot be caught here.
    ``unaccounted_operations`` is the route-granular companion that can.
    """
    covered: Set[str] = {tool.tag for tool in tools}
    shadowed_set = set(shadowed)
    claimable = None if convenience_tags is None else set(convenience_tags)
    for method, path, operation in iter_operations(spec):
        if (method, path) in shadowed_set:
            for tag in operation.get("tags") or []:
                if claimable is None or str(tag) in claimable:
                    covered.add(str(tag))
    excluded = set(excluded_tags)
    return {tag for tag in catalog_tags(spec) if tag not in covered and tag not in excluded}


def unaccounted_operations(
    spec: Mapping[str, Any],
    tools: Sequence[GeneratedTool],
    *,
    shadowed: Iterable[Tuple[str, str]] = (),
    excluded_operations: Iterable[Tuple[str, str]] = EXCLUDED_OPERATIONS,
    excluded_tags: Iterable[str] = EXCLUDED_TAGS,
    public_operations: Iterable[Tuple[str, str]] = PUBLIC_OPERATIONS,
) -> Set[Tuple[str, str]]:
    """Every ``(METHOD, path)`` in the document that nothing accounts for -- the route-granular guard.

    An operation is accounted for when it is a generated tool, a shadowed route (a
    convenience tool fronts it), a named excluded operation, a SECURED operation
    under an excluded tag, or a named public (unsecured) operation. An unsecured
    route is never excused by its tag: it must be named in ``public_operations``,
    which is what makes "forgot the ``security`` dependency" a CI failure even under
    a tag that already has tools (sibling routers sharing a tag is the repo pattern:
    ``scrap_reasons`` under Quality, ``work_order_materials`` under Work Orders).
    """
    tool_keys = {tool.key for tool in tools}
    shadowed_set = set(shadowed)
    excluded_ops = set(excluded_operations)
    excluded = set(excluded_tags)
    public = set(public_operations)
    unaccounted: Set[Tuple[str, str]] = set()
    for method, path, operation in iter_operations(spec):
        key = (method, path)
        if key in tool_keys or key in shadowed_set or key in excluded_ops or key in public:
            continue
        if operation.get("security") and any(str(tag) in excluded for tag in (operation.get("tags") or [])):
            continue
        unaccounted.add(key)
    return unaccounted


def catalog_summary(tools: Sequence[GeneratedTool]) -> List[Dict[str, Any]]:
    """The JSON ``--print-catalog`` prints: one compact row per generated tool."""
    return [
        {
            "name": tool.name,
            "method": tool.method,
            "path": tool.path,
            "tag": tool.tag,
            "function": tool.function_name,
            "annotations": tool.hints.as_dict(),
            "deprecated": tool.deprecated,
        }
        for tool in tools
    ]


def deep_copy_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """A defensive copy for callers that hand schemas to a validator that may cache them."""
    return copy.deepcopy(dict(schema))
