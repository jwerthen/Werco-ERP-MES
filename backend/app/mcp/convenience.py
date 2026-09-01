"""Hand-written tools with fixed names, plus the raw operations they shadow.

WHY these exist next to ~650 generated tools: the generated layer is a faithful
mirror of the API, and a faithful mirror repeats the API's sharp edges. The owner's
rules for agent use (brief rules 5-8) need a place to live that is NOT a route change:

- ``create_work_order`` / ``duplicate_work_order`` always land DRAFT and
  ``release_work_order`` is the separate, explicit step -- an agent must never put
  work on the floor as a side effect of creating it. ``update_work_order`` fronts the
  generic ``PUT`` for the same reason: the route's blind ``setattr`` would accept
  ``status: released`` with no ``released_at`` / ``released_by`` and no operation
  promotion, so the tool refuses ``released`` / ``in_progress`` and points at the
  real verbs.
- ``import_laser_nest_package``: the app's import is born RELEASED (that is what the
  planners' import button means) with its nest operations READY; the tool
  immediately PUTs the child back to DRAFT -- the header AND each READY operation
  back to PENDING, in that order, because a DRAFT header alone leaves the nests on
  the dispatch board and startable -- unless ``release=true``, and refuses to mix a
  package import onto a job that already carries manually entered nests (import
  REPLACES every nest on the job). The manual-nest check reads the LASER work order:
  a production parent's nests live on its laser child, never on its own operations.
- ``add_laser_nest`` fronts the manual-nest route, which force-sets the laser work
  order RELEASED: a new or previously-DRAFT laser work order is put back to DRAFT the
  same way, a job that was already on the floor is left there, and a manual nest is
  refused on a job whose nests came from a package import (rule 6, the other way).
- ``add_operation`` refuses operation names that are really file names or DXF export
  labels, and resolves a work center by NAME so an agent need not know ids.

Each convenience tool SHADOWS the generated tool for the same route (the pairs in
``SHADOWED_OPERATIONS`` are dropped from the catalog) so that there is exactly one
door to those routes and the rules cannot be bypassed by calling the raw twin.

Every handler reaches data the same way a generated tool does -- an HTTP request
through the executor, as the caller's user. No service imports, no DB.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import mcp.types as types

from app.mcp.auth import AuthContext
from app.mcp.catalog import ToolAnnotationHints, file_argument_schema
from app.mcp.executor import Executor, FilesArg, UploadError, UploadFile, decode_upload
from app.mcp.results import ExecResult, build_tool_result, error_result, extract_detail

API = "/api/v1"

SHADOWED_OPERATIONS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("POST", f"{API}/work-orders/"),
        ("PUT", f"{API}/work-orders/{{work_order_id}}"),
        ("POST", f"{API}/work-orders/{{work_order_id}}/duplicate"),
        ("POST", f"{API}/work-orders/{{work_order_id}}/release"),
        ("POST", f"{API}/work-orders/{{work_order_id}}/operations"),
        ("POST", f"{API}/work-orders/laser-nest-packages/standalone/import"),
        ("POST", f"{API}/work-orders/{{work_order_id}}/laser-nest-packages/import"),
        ("POST", f"{API}/work-orders/{{work_order_id}}/laser-nests/manual"),
        ("GET", f"{API}/search/"),
        ("GET", f"{API}/work-centers/"),
        ("GET", f"{API}/work-orders/{{work_order_id}}"),
        ("GET", f"{API}/shop-floor/dashboard"),
        ("GET", f"{API}/inventory/"),
        ("GET", f"{API}/purchasing/purchase-orders"),
        ("GET", f"{API}/quality/ncr"),
        ("GET", f"{API}/parts/"),
    }
)

# Rule 7: an operation is a short shop-floor step, never a file. These are the tells.
_FILE_NAME_SUFFIXES = (".dxf", ".dwg", ".nc", ".pdf")
_DXF_EXPORT_LABEL = "part detail"

# The API's own vocabulary (WorkOrderType / WorkOrderStatus / OperationStatus), as wire strings.
LASER_CUTTING = "laser_cutting"
WO_DRAFT = "draft"
OP_READY = "ready"
OP_PENDING = "pending"
# Status writes that have a real verb; ``update_work_order`` refuses them by name.
STATUS_WRITES_WITH_A_REAL_VERB: Mapping[str, str] = {
    "released": "release_work_order",
    "in_progress": "work_orders_start_work_order",
}
RELEASE_STAMP_NOTE = (
    "released_at / released_by still carry the stamp the route wrote when it created the laser work order "
    "RELEASED; release_work_order re-stamps them when the planner releases it."
)
# Belt to the list-query brace in ``_resolve_laser_work_order``: the work-order list is windowed.
LASER_CHILD_LOOKUP_LIMIT = 5000

# The API's own wording for the laser_cutting refusal (WorkOrderCreate.validate_work_order_type).
LASER_CUTTING_CREATE_REFUSAL = (
    "work_order_type 'laser_cutting' cannot be set on create: laser nest-dispatch work orders "
    "are created only by the nest package import (POST /work-orders/laser-nest-packages/...)"
)


class ToolFailure(Exception):
    """Carries a finished error ``CallToolResult`` out of a handler."""

    def __init__(self, result: types.CallToolResult) -> None:
        super().__init__("tool failed")
        self.result = result


class ToolContext:
    """What a convenience handler gets: route calls AS THE CALLER, and result shaping."""

    def __init__(
        self,
        executor: Executor,
        auth: AuthContext,
        *,
        max_result_chars: int,
        max_blob_bytes: int,
        max_upload_bytes: int,
    ) -> None:
        self._executor = executor
        self._auth = auth
        self.max_result_chars = max_result_chars
        self.max_blob_bytes = max_blob_bytes
        self.max_upload_bytes = max_upload_bytes

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        files: Optional[FilesArg] = None,
        form: Optional[Mapping[str, Any]] = None,
    ) -> ExecResult:
        return await self._executor.request(
            method=method, path=path, query=query, json=json, files=files, form=form, auth=self._auth
        )

    async def call_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call and return the parsed JSON body; any >= 400 becomes a ``ToolFailure`` carrying the server's detail."""
        result = await self.call(method, path, **kwargs)
        if result.status >= 400:
            raise ToolFailure(
                error_result(status=result.status, detail=extract_detail(result), method=method, path=path)
            )
        if not result.content:
            return None
        try:
            return result.json()
        except ValueError:
            raise ToolFailure(
                error_result(
                    status=result.status,
                    detail=f"Expected JSON from {method} {path}, got {result.media_type or 'no content type'}",
                    method=method,
                    path=path,
                )
            ) from None

    def finish(self, result: ExecResult, *, tool: str, method: str, path: str) -> types.CallToolResult:
        return build_tool_result(
            tool_name=tool,
            method=method,
            path=path,
            result=result,
            max_chars=self.max_result_chars,
            max_blob_bytes=self.max_blob_bytes,
        )

    def synthesize(self, payload: Any, *, tool: str, method: str, path: str) -> types.CallToolResult:
        """A tool-composed JSON payload, shaped (and capped) exactly like a route's response."""
        body = json.dumps(payload, default=str).encode("utf-8")
        return self.finish(
            ExecResult(status=200, content=body, content_type="application/json"), tool=tool, method=method, path=path
        )

    def fail(
        self,
        *,
        status: int,
        detail: Any,
        method: Optional[str] = None,
        path: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> ToolFailure:
        return ToolFailure(error_result(status=status, detail=detail, method=method, path=path, extra=extra))

    def decode_file(self, value: Any) -> UploadFile:
        try:
            return decode_upload(value, max_bytes=self.max_upload_bytes)
        except UploadError as exc:
            raise ToolFailure(error_result(status=exc.status, detail=str(exc))) from exc


Handler = Callable[[ToolContext, Dict[str, Any]], Awaitable[types.CallToolResult]]


@dataclass(frozen=True)
class ConvenienceTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    hints: ToolAnnotationHints
    handler: Handler


def _present(args: Mapping[str, Any], *names: str) -> Dict[str, Any]:
    return {name: args[name] for name in names if name in args and args[name] is not None}


def _wo_label(work_order: Mapping[str, Any], fallback: Any) -> str:
    return str(work_order.get("work_order_number") or fallback)


# --------------------------------------------------------------------------- search


_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "Search text: part numbers, work order numbers, customers, vendors, POs, NCRs, ...",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "types": {
            "type": "string",
            "description": "Comma-separated record types to restrict the search to (e.g. 'parts,work_orders'). Omit for all.",
        },
    },
    "required": ["q"],
}


async def _search(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/search/"
    result = await ctx.call("GET", path, query=_present(args, "q", "limit", "types"))
    return ctx.finish(result, tool="search", method="GET", path=path)


# --------------------------------------------------------------------------- list_work_centers


_LIST_WORK_CENTERS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Case-insensitive substring matched against the work center's name and code.",
        },
        "active_only": {"type": "boolean", "default": True},
    },
}


async def _list_work_centers(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/work-centers/"
    active_only = bool(args.get("active_only", True))
    centers = await ctx.call_json("GET", path, query={"active_only": active_only, "limit": 5000})
    needle = str(args.get("name") or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for center in centers or []:
        name = str(center.get("name") or "")
        code = str(center.get("code") or "")
        if needle and needle not in name.lower() and needle not in code.lower():
            continue
        rows.append(
            {
                "id": center.get("id"),
                "code": code,
                "name": name,
                "type": center.get("work_center_type"),
                "is_active": center.get("is_active"),
            }
        )
    return ctx.synthesize(rows, tool="list_work_centers", method="GET", path=path)


# --------------------------------------------------------------------------- create_work_order


_CREATE_WORK_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "part_id": {"type": "integer", "minimum": 1, "description": "The part to build (see list_parts / search)."},
        "quantity_ordered": {"type": "number", "exclusiveMinimum": 0},
        "work_order_type": {
            "type": "string",
            "default": "production",
            "description": "'production'. 'laser_cutting' is refused: laser jobs come only from import_laser_nest_package.",
        },
        "parent_work_order_id": {"type": "integer", "minimum": 1},
        "priority": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "1 = highest."},
        "due_date": {"type": "string", "format": "date", "description": "ISO date, not in the past."},
        "customer_name": {"type": "string", "maxLength": 255},
        "customer_po": {"type": "string", "maxLength": 50},
        "unit_number": {"type": "string", "maxLength": 50, "description": "Unit # for a one-unit-per-work-order job."},
        "notes": {"type": "string", "maxLength": 2000},
        "special_instructions": {"type": "string", "maxLength": 2000},
        "sequential_operations": {
            "type": "boolean",
            "default": True,
            "description": "True = sequenced routing (each op waits for every lower-sequence op); "
            "False = dispatch pool (same-work-center ops are mutually startable).",
        },
        "serial_numbers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-unit serials for a serialized job: unique, exactly quantity_ordered of them.",
        },
        "auto_routing": {
            "type": "boolean",
            "default": True,
            "description": "Generate operations from the part's released routing (query parameter of the route).",
        },
        # No ``status`` argument on purpose: a created work order is always DRAFT and
        # every convenience schema refuses unknown properties, so passing one is a 422
        # that names it -- never an argument that looks honoured and is not.
    },
    "required": ["part_id", "quantity_ordered"],
}

_CREATE_BODY_FIELDS = (
    "part_id",
    "quantity_ordered",
    "work_order_type",
    "parent_work_order_id",
    "priority",
    "due_date",
    "customer_name",
    "customer_po",
    "unit_number",
    "notes",
    "special_instructions",
    "sequential_operations",
    "serial_numbers",
)


async def _create_work_order(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/work-orders/"
    if args.get("work_order_type") == "laser_cutting":
        raise ctx.fail(status=422, detail=LASER_CUTTING_CREATE_REFUSAL, method="POST", path=path)
    # Only the listed fields are forwarded: the route has no status input (the model
    # defaults DRAFT) and operations are added one at a time through add_operation so
    # each name passes the rule-7 guard. The schema refuses unknown arguments, so this
    # allowlist is a second fence, not the first.
    body = _present(args, *_CREATE_BODY_FIELDS)
    query = {"auto_routing": args.get("auto_routing", True)}
    result = await ctx.call("POST", path, query=query, json=body)
    if result.status >= 400 or not result.is_json:
        return ctx.finish(result, tool="create_work_order", method="POST", path=path)
    created = result.json()
    if isinstance(created, dict) and created.get("status") not in (None, "draft"):
        # Unreachable against today's route (it cannot create anything but DRAFT); kept
        # so that a future route change cannot silently break rule 5 -- say so loudly
        # rather than pretend.
        raise ctx.fail(
            status=500,
            detail=f"Work order {_wo_label(created, created.get('id'))} was created with status "
            f"{created.get('status')!r}, not DRAFT. Review it before releasing.",
            method="POST",
            path=path,
            extra={"work_order": created},
        )
    return ctx.finish(result, tool="create_work_order", method="POST", path=path)


# --------------------------------------------------------------------------- add_operation


_ADD_OPERATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {"type": "integer", "minimum": 1},
        "name": {
            "type": "string",
            "minLength": 2,
            "maxLength": 255,
            "description": "A short shop-floor step name ('Laser', 'Brake', 'Weld', 'Deburr'). "
            "File names, paths and 'Part Detail' are refused.",
        },
        "work_center": {
            "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 1}],
            "description": "Work center id, or its name/code (exact match first, then unique substring).",
        },
        "sequence": {
            "type": "integer",
            "minimum": 10,
            "maximum": 990,
            "multipleOf": 10,
            "description": "Omit to append after the work order's current highest sequence.",
        },
        "operation_number": {
            "type": "string",
            "maxLength": 20,
            "description": "Bare identifier such as '10' (no 'Op ' prefix).",
        },
        "description": {"type": "string"},
        "setup_instructions": {"type": "string", "maxLength": 5000},
        "run_instructions": {"type": "string", "maxLength": 5000},
        "setup_time_hours": {"type": "number", "minimum": 0},
        "run_time_hours": {"type": "number", "minimum": 0},
        "run_time_per_piece": {"type": "number", "minimum": 0},
        "requires_inspection": {"type": "boolean", "default": False},
        "inspection_type": {"type": "string", "maxLength": 100},
        "component_part_id": {"type": "integer", "minimum": 1},
        "component_quantity": {
            "type": "number",
            "minimum": 0,
            "description": "BOM component demand for this step (the per-operation piece target on a batch job) -- "
            "NOT an operation quantity; an operation runs the work order's quantity_ordered.",
        },
        "operation_group": {"type": "string", "maxLength": 50},
        "quantity": {
            "type": "number",
            "description": "NOT ACCEPTED (refused with an explanation): an operation has no quantity of its own.",
        },
    },
    "required": ["work_order_id", "name", "work_center"],
}

OPERATION_HAS_NO_QUANTITY = (
    "add_operation takes no `quantity`: an operation has no quantity of its own -- it runs the work order's "
    "quantity_ordered. `component_quantity` is the BOM component demand for the step (the per-operation piece "
    "target on a batch job), not an operation quantity; pass it only when you mean that."
)

_ADD_OPERATION_PASSTHROUGH = (
    "operation_number",
    "description",
    "setup_instructions",
    "run_instructions",
    "setup_time_hours",
    "run_time_hours",
    "run_time_per_piece",
    "requires_inspection",
    "inspection_type",
    "component_part_id",
    "component_quantity",
    "operation_group",
)


def operation_name_rejection(name: str) -> Optional[str]:
    """Why ``name`` is not an operation name (rule 7), or None when it is fine."""
    candidate = name.strip()
    lowered = candidate.lower()
    if "/" in candidate or "\\" in candidate:
        return "it looks like a file path"
    if lowered.endswith(_FILE_NAME_SUFFIXES):
        return "it looks like a CNC/drawing file export"
    if lowered == _DXF_EXPORT_LABEL:
        return "'Part Detail' is a DXF export label, not a shop-floor step"
    return None


async def _resolve_work_center(ctx: ToolContext, spec: Any) -> int:
    if isinstance(spec, bool):
        raise ctx.fail(status=422, detail="work_center must be an id or a name, not a boolean.")
    if isinstance(spec, int):
        return spec
    needle = str(spec).strip().lower()
    if not needle:
        raise ctx.fail(status=422, detail="work_center must be an id or a non-empty name.")
    centers = await ctx.call_json("GET", f"{API}/work-centers/", query={"active_only": False, "limit": 5000}) or []

    def label(center: Mapping[str, Any]) -> str:
        return f"{center.get('name')} [{center.get('code')}] (id {center.get('id')})"

    exact = [
        c for c in centers if str(c.get("name") or "").lower() == needle or str(c.get("code") or "").lower() == needle
    ]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if len(exact) > 1:
        raise ctx.fail(
            status=409,
            detail=f"{len(exact)} work centers are named {spec!r}: {', '.join(label(c) for c in exact)}. Pass the id.",
        )
    partial = [
        c for c in centers if needle in str(c.get("name") or "").lower() or needle in str(c.get("code") or "").lower()
    ]
    if len(partial) == 1:
        return int(partial[0]["id"])
    if not partial:
        raise ctx.fail(
            status=404,
            detail=f"No work center matches {spec!r}. Use list_work_centers to see the shop's machines.",
        )
    raise ctx.fail(
        status=409,
        detail=f"{len(partial)} work centers match {spec!r}: {', '.join(label(c) for c in partial)}. "
        "Be more specific or pass the id.",
    )


async def _next_sequence(ctx: ToolContext, work_order_id: int) -> int:
    work_order = await ctx.call_json("GET", f"{API}/work-orders/{work_order_id}") or {}
    sequences = [int(op.get("sequence") or 0) for op in work_order.get("operations") or []]
    if not sequences:
        return 10
    return (max(sequences) // 10 + 1) * 10


async def _add_operation(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    work_order_id = int(args["work_order_id"])
    path = f"{API}/work-orders/{work_order_id}/operations"
    name = str(args["name"])
    reason = operation_name_rejection(name)
    if reason:
        raise ctx.fail(
            status=422,
            detail=f"Refusing operation name {name!r}: {reason}. "
            "Use a short shop-floor step such as 'Laser', 'Brake', 'Weld' or 'Deburr'.",
            method="POST",
            path=path,
        )
    if "quantity" in args:
        raise ctx.fail(status=422, detail=OPERATION_HAS_NO_QUANTITY, method="POST", path=path)
    work_center_id = await _resolve_work_center(ctx, args["work_center"])
    sequence = args.get("sequence")
    if sequence is None:
        sequence = await _next_sequence(ctx, work_order_id)
    body: Dict[str, Any] = {"name": name.strip(), "work_center_id": work_center_id, "sequence": sequence}
    body.update(_present(args, *_ADD_OPERATION_PASSTHROUGH))
    result = await ctx.call("POST", path, json=body)
    return ctx.finish(result, tool="add_operation", method="POST", path=path)


# --------------------------------------------------------------------------- get_work_order


_GET_WORK_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {"type": "integer", "minimum": 1},
        "work_order_number": {"type": "string", "minLength": 1, "description": "e.g. 'WO-20260821-001'."},
    },
    "description": "Pass work_order_id OR work_order_number.",
}


async def _get_work_order(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    if args.get("work_order_id") is not None:
        path = f"{API}/work-orders/{int(args['work_order_id'])}"
    elif args.get("work_order_number"):
        # Percent-encoded exactly like a generated path parameter: a number carrying
        # ``/``, ``?`` or ``..`` must look the number up (404), never steer the call
        # onto a different GET route.
        path = f"{API}/work-orders/by-number/{quote(str(args['work_order_number']).strip(), safe='')}"
    else:
        raise ctx.fail(status=422, detail="Pass work_order_id or work_order_number.")
    result = await ctx.call("GET", path)
    return ctx.finish(result, tool="get_work_order", method="GET", path=path)


# --------------------------------------------------------------------------- update_work_order


_UPDATE_WORK_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {"type": "integer", "minimum": 1},
        "version": {
            "type": "integer",
            "minimum": 0,
            "description": "Optimistic-lock version from the last read (get_work_order); a stale value is refused 409.",
        },
        "quantity_ordered": {"type": "number", "exclusiveMinimum": 0},
        "priority": {"type": "integer", "minimum": 1, "maximum": 10, "description": "1 = highest."},
        "status": {
            "type": "string",
            "description": "Accepted: 'draft' (put an unreleased job back to draft), 'on_hold', 'cancelled'. "
            "REFUSED here: 'released' and 'in_progress' -- releasing is release_work_order and starting is "
            "work_orders_start_work_order; both record who authorised it and promote the first operation, which a "
            "status write would skip. 'complete' / 'closed' are refused by the route itself (use "
            "work_orders_complete_work_order).",
        },
        "sequential_operations": {
            "type": "boolean",
            "description": "True = sequenced routing; False = dispatch pool. Turning it ON is refused 409 on a laser "
            "job and on a job already worked out of sequence (the route names the operations).",
        },
        "due_date": {"type": "string", "format": "date"},
        "customer_name": {"type": "string", "maxLength": 255},
        "customer_po": {"type": "string", "maxLength": 50},
        "unit_number": {"type": "string", "maxLength": 50, "description": "Unit #; an empty string clears it."},
        "notes": {"type": "string", "maxLength": 2000},
        "special_instructions": {"type": "string", "maxLength": 2000},
        "quantity_complete": {"type": "number", "minimum": 0},
        "quantity_scrapped": {"type": "number", "minimum": 0},
        "scrap_reason": {"type": "string", "maxLength": 255, "description": "Required when quantity_scrapped > 0."},
    },
    "required": ["work_order_id", "version"],
}

# Every WorkOrderUpdate field (pinned against the route's own schema by a catalog test).
_UPDATE_BODY_FIELDS = tuple(name for name in _UPDATE_WORK_ORDER_SCHEMA["properties"] if name != "work_order_id")


async def _update_work_order(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    work_order_id = int(args["work_order_id"])
    path = f"{API}/work-orders/{work_order_id}"
    requested = str(args.get("status") or "").strip().lower()
    verb = STATUS_WRITES_WITH_A_REAL_VERB.get(requested)
    if verb:
        raise ctx.fail(
            status=422,
            detail=f"status {requested!r} is not written through update_work_order: use {verb}, which records who "
            "authorised it (released_at / released_by) and promotes the first operation. A status write here would "
            "skip both.",
            method="PUT",
            path=path,
        )
    result = await ctx.call("PUT", path, json=_present(args, *_UPDATE_BODY_FIELDS))
    return ctx.finish(result, tool="update_work_order", method="PUT", path=path)


# --------------------------------------------------------------------------- duplicate / release


_DUPLICATE_WORK_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {"type": "integer", "minimum": 1, "description": "The work order whose PLAN is copied."},
        "quantity_ordered": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Quantity on the new work order (required by the route). A laser job ignores it and "
            "derives its quantity from the copied nests' planned runs -- read it back from the response.",
        },
        "due_date": {
            "type": "string",
            "format": "date",
            "description": "Due date for the new work order; omit to leave unset.",
        },
    },
    "required": ["work_order_id", "quantity_ordered"],
}


async def _duplicate_work_order(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/work-orders/{int(args['work_order_id'])}/duplicate"
    body = _present(args, "quantity_ordered", "due_date")
    result = await ctx.call("POST", path, json=body)
    if result.status < 400 and result.is_json:
        payload = result.json()
        copied = payload.get("work_order") if isinstance(payload, dict) else None
        if isinstance(copied, dict) and copied.get("status") not in (None, "draft"):
            raise ctx.fail(
                status=500,
                detail=f"Duplicate {_wo_label(copied, copied.get('id'))} landed with status {copied.get('status')!r}, "
                "not DRAFT. Review it before releasing.",
                method="POST",
                path=path,
                extra={"duplicate": payload},
            )
    return ctx.finish(result, tool="duplicate_work_order", method="POST", path=path)


_RELEASE_WORK_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"work_order_id": {"type": "integer", "minimum": 1}},
    "required": ["work_order_id"],
}


async def _release_work_order(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/work-orders/{int(args['work_order_id'])}/release"
    result = await ctx.call("POST", path)
    return ctx.finish(result, tool="release_work_order", method="POST", path=path)


# --------------------------------------------------------------------------- import_laser_nest_package


_IMPORT_NEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Target: an assembly parent (its laser child is found-or-created) or a laser work order "
            "(rebuilt in place). Omit to create a fresh STANDALONE laser work order.",
        },
        "file": file_argument_schema("The nest package: a ZIP of per-nest files, or a bare (multi-page) nest PDF."),
        "source_path": {
            "type": "string",
            "description": "Server-side path of an already-staged package (instead of file).",
        },
        "rows": {
            "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "string"}],
            "description": "Confirmed nest rows from the preview step; a list is sent as the JSON string the route expects.",
        },
        "work_center_id": {"type": "integer", "minimum": 1, "description": "The laser the nests run on."},
        "due_date": {
            "type": "string",
            "format": "date",
            "description": "Standalone imports only: due date of the new work order.",
        },
        "sheet_match_provenance": {
            "anyOf": [{"type": "object"}, {"type": "string"}],
            "description": "Optional audit-only map of source_file -> how its sheet was chosen.",
        },
        "release": {
            "type": "boolean",
            "default": False,
            "description": "The route creates the laser work order RELEASED. By default this tool immediately sets it "
            "back to DRAFT; pass true to leave it RELEASED.",
        },
    },
}


def _nest_names(work_order: Mapping[str, Any], *, imported: bool) -> List[str]:
    """Names of the nests on ``operations[].laser_nest`` that came from a package import (``imported``)
    or were keyed by hand (``cnc_file_name`` null)."""
    names: List[str] = []
    for operation in work_order.get("operations") or []:
        nest = operation.get("laser_nest") if isinstance(operation, dict) else None
        if isinstance(nest, dict) and (nest.get("cnc_file_name") is not None) == imported:
            names.append(str(nest.get("nest_name") or operation.get("name") or nest.get("id")))
    return names


def _manual_nests(work_order: Mapping[str, Any]) -> List[str]:
    return _nest_names(work_order, imported=False)


async def _part_number(ctx: ToolContext, part_id: Any) -> Optional[str]:
    """Best effort: the part number narrows the work-order list search; any failure means no narrowing."""
    if not part_id:
        return None
    result = await ctx.call("GET", f"{API}/parts/{int(part_id)}")
    if result.status >= 400 or not result.is_json:
        return None
    try:
        payload = result.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("part_number") or "").strip() or None


async def _resolve_laser_work_order(ctx: ToolContext, target: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The laser work order a nest tool addressed through ``target``, as the API shows it.

    A ``laser_cutting`` target IS the laser work order. A production parent's nests
    live on its laser CHILD (``parent_work_order_id`` = the parent, the route's
    ``_resolve_laser_target`` finds-or-creates it), and ``WorkOrderResponse`` carries no
    pointer to that child, so it is found through the work-order list -- narrowed by
    the parent's part number when that can be read, since a child shares its parent's
    part -- and then read in full. ``None`` means the parent has no live laser child
    yet (the route will create one).
    """
    if target.get("work_order_type") == LASER_CUTTING:
        return dict(target)
    query: Dict[str, Any] = {"limit": LASER_CHILD_LOOKUP_LIMIT}
    part_number = await _part_number(ctx, target.get("part_id"))
    if part_number:
        query["search"] = part_number
    rows = await ctx.call_json("GET", f"{API}/work-orders/", query=query) or []
    children = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("parent_work_order_id") == target.get("id")
        and row.get("work_order_type") == LASER_CUTTING
    ]
    if not children:
        return None
    child = min(children, key=lambda row: int(row.get("id") or 0))
    return await ctx.call_json("GET", f"{API}/work-orders/{child['id']}") or {}


async def _demote_to_draft(
    ctx: ToolContext, work_order: Mapping[str, Any], *, headline: str, evidence: Mapping[str, Any]
) -> Dict[str, Any]:
    """Put a laser work order the route left RELEASED back to DRAFT, operations included.

    Header FIRST (``PUT /work-orders/{id}`` ``{version, status: "draft"}``), then every
    READY operation back to PENDING (``PUT /work-orders/operations/{id}``): the app's
    reconcile-on-read promotes PENDING operations of a RELEASED work order but never
    those of a DRAFT one, so this order cannot be raced back by a read in between. A
    DRAFT header alone is not enough -- the nest operations the route minted READY
    would stay on the dispatch board and the kiosk queue, startable, on a job that
    says DRAFT (the dispatch views filter on terminal statuses only). Every failure is
    loud and carries ``evidence`` (the import / nest payload) so the caller knows what
    exists and in what state.
    """
    work_order_id = int(work_order["id"])
    label = _wo_label(work_order, work_order_id)
    header_path = f"{API}/work-orders/{work_order_id}"
    current: Dict[str, Any] = dict(work_order)
    if current.get("status") != WO_DRAFT:
        demoted = await ctx.call("PUT", header_path, json={"version": current.get("version"), "status": WO_DRAFT})
        if demoted.status >= 400 or not demoted.is_json:
            raise ctx.fail(
                status=demoted.status,
                detail=f"{headline}, BUT work order {label} (id {work_order_id}) is still "
                f"{str(current.get('status') or 'released').upper()}: setting it back to DRAFT failed "
                f"({extract_detail(demoted)}). It is on the floor's board now; put it back to DRAFT from the UI or "
                "retry with update_work_order.",
                method="PUT",
                path=header_path,
                extra={**evidence, "work_order": current, "demoted_to_draft": False},
            )
        current = demoted.json()

    ready = [op for op in current.get("operations") or [] if isinstance(op, dict) and op.get("status") == OP_READY]
    failures: List[str] = []
    failure_status = 409
    for operation in ready:
        op_path = f"{API}/work-orders/operations/{int(operation['id'])}"
        outcome = await ctx.call("PUT", op_path, json={"version": operation.get("version"), "status": OP_PENDING})
        if outcome.status >= 400:
            if not failures:
                failure_status = outcome.status
            failures.append(
                f"OP{operation.get('sequence')} {operation.get('name')} (id {operation['id']}): "
                f"{extract_detail(outcome)}"
            )
    if failures:
        raise ctx.fail(
            status=failure_status,
            detail=f"{headline} and work order {label} (id {work_order_id}) is DRAFT, BUT {len(failures)} of its "
            f"operations are still READY -- on the dispatch board and startable: {'; '.join(failures)}. Return "
            "them to pending from the UI (or through the operation update tool) before relying on the DRAFT.",
            method="PUT",
            path=f"{API}/work-orders/operations/{{operation_id}}",
            extra={
                **evidence,
                "work_order": current,
                "demoted_to_draft": True,
                "operations_returned_to_pending": len(ready) - len(failures),
            },
        )
    # Re-read so the returned work order is what the database says, operations included
    # (a read never promotes a DRAFT work order, so this cannot undo the demote).
    final = await ctx.call_json("GET", header_path) or current
    return {
        "work_order": final,
        "demoted_to_draft": True,
        "operations_returned_to_pending": len(ready),
        "note": RELEASE_STAMP_NOTE,
    }


def _as_json_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


async def _import_laser_nest_package(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    tool = "import_laser_nest_package"
    target_id = args.get("work_order_id")
    if target_id is not None:
        target_id = int(target_id)
        target = await ctx.call_json("GET", f"{API}/work-orders/{target_id}") or {}
        # Rule 6 reads the LASER work order: a production parent's nests are on its child.
        laser = await _resolve_laser_work_order(ctx, target)
        manual = _manual_nests(laser) if laser else []
        if manual:
            raise ctx.fail(
                status=409,
                detail=f"Work order {_wo_label(laser or target, target_id)} already carries {len(manual)} manually "
                f"entered nest(s) ({', '.join(manual)}). A package import REPLACES every nest on the job, so manual "
                "nests and a package import are never mixed. Import into a fresh standalone laser work order "
                "instead (omit work_order_id).",
            )
        path = f"{API}/work-orders/{target_id}/laser-nest-packages/import"
    else:
        path = f"{API}/work-orders/laser-nest-packages/standalone/import"

    files: Optional[Dict[str, UploadFile]] = None
    if args.get("file") is not None:
        files = {"file": ctx.decode_file(args["file"])}
    if files is None and not args.get("source_path"):
        raise ctx.fail(status=422, detail="Provide the package as `file` or as a server-side `source_path`.", path=path)

    form: Dict[str, Any] = {
        "source_path": args.get("source_path"),
        "work_center_id": args.get("work_center_id"),
        "rows": _as_json_string(args.get("rows")),
        "sheet_match_provenance": _as_json_string(args.get("sheet_match_provenance")),
    }
    if target_id is None and args.get("due_date"):
        form["due_date"] = args["due_date"]

    imported = await ctx.call("POST", path, form=form, files=files)
    if imported.status >= 400 or not imported.is_json:
        return ctx.finish(imported, tool=tool, method="POST", path=path)
    payload = imported.json()
    child = payload.get("child_work_order") if isinstance(payload, dict) else None
    if not isinstance(child, dict) or child.get("id") is None:
        return ctx.finish(imported, tool=tool, method="POST", path=path)

    if args.get("release"):
        return ctx.synthesize(
            {"import": payload, "work_order": child, "demoted_to_draft": False}, tool=tool, method="POST", path=path
        )

    # Rule 6: the import is born RELEASED; hand it back as DRAFT unless told otherwise.
    outcome = await _demote_to_draft(ctx, child, headline="IMPORT SUCCEEDED", evidence={"import": payload})
    return ctx.synthesize({"import": payload, **outcome}, tool=tool, method="POST", path=path)


# --------------------------------------------------------------------------- add_laser_nest


_ADD_LASER_NEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "work_order_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Target: an assembly parent (its laser child is found-or-created) or a laser work order "
            "(the nest is appended to it).",
        },
        "cnc_number": {"type": "string", "minLength": 1, "maxLength": 100, "description": "CNC program number."},
        "planned_runs": {"type": "integer", "minimum": 1, "description": "Planned sheet runs."},
        "nest_name": {"type": "string", "maxLength": 255},
        "material": {"type": "string", "maxLength": 100},
        "thickness": {"type": "string", "maxLength": 50},
        "sheet_size": {"type": "string", "maxLength": 100},
        "work_center_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Which laser this nest runs on. Omitted, it inherits the machine the job's existing nests "
            "already run on (shop-wide laser auto-detect only when the job has none).",
        },
        "material_part_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional material tie: the stock sheet/plate part this nest consumes when its operation "
            "completes.",
        },
        "qty_per_run": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Material consumed per completed run; requires material_part_id (defaults to 1.0 with it).",
        },
        "release": {
            "type": "boolean",
            "default": False,
            "description": "The route force-sets the laser work order RELEASED with the new nest READY. By default "
            "this tool puts a NEW laser work order, or one that was DRAFT before the call, back to DRAFT with its "
            "operations PENDING; pass true to leave it RELEASED. A job already on the floor before the call "
            "(released / in progress / on hold) is never taken off it.",
        },
    },
    "required": ["work_order_id", "cnc_number", "planned_runs"],
}

_ADD_LASER_NEST_BODY_FIELDS = (
    "cnc_number",
    "planned_runs",
    "nest_name",
    "material",
    "thickness",
    "sheet_size",
    "work_center_id",
    "material_part_id",
    "qty_per_run",
)


async def _add_laser_nest(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    tool = "add_laser_nest"
    target_id = int(args["work_order_id"])
    path = f"{API}/work-orders/{target_id}/laser-nests/manual"
    target = await ctx.call_json("GET", f"{API}/work-orders/{target_id}") or {}
    laser_before = await _resolve_laser_work_order(ctx, target)
    # Rule 6, the other direction: a job whose nests came from a package import takes
    # no hand-keyed nest -- the next import would replace it silently.
    imported = _nest_names(laser_before, imported=True) if laser_before else []
    if imported:
        raise ctx.fail(
            status=409,
            detail=f"Work order {_wo_label(laser_before or {}, target_id)} already carries {len(imported)} nest(s) "
            f"from a package import ({', '.join(imported)}). Manually entered nests and a package import are never "
            "mixed on one job: re-import the corrected package with import_laser_nest_package instead.",
            method="POST",
            path=path,
        )
    status_before = laser_before.get("status") if laser_before else None

    created = await ctx.call("POST", path, json=_present(args, *_ADD_LASER_NEST_BODY_FIELDS))
    if created.status >= 400 or not created.is_json:
        return ctx.finish(created, tool=tool, method="POST", path=path)
    nest = created.json()

    if laser_before is not None:
        laser = await ctx.call_json("GET", f"{API}/work-orders/{int(laser_before['id'])}") or {}
    else:
        laser = await _resolve_laser_work_order(ctx, target)
    if not laser:
        return ctx.synthesize(
            {
                "nest": nest,
                "work_order": None,
                "demoted_to_draft": False,
                "note": "The nest was added but its laser work order could not be resolved through the work-order "
                "list; read it with get_work_order and put it back to DRAFT with update_work_order if it should "
                "not be on the floor.",
            },
            tool=tool,
            method="POST",
            path=path,
        )

    if args.get("release") or status_before not in (None, WO_DRAFT):
        note = (
            "Left RELEASED as requested."
            if args.get("release")
            else f"The laser work order was already {status_before!r} before the nest was added; it was left as the "
            f"route set it ({laser.get('status')!r}) rather than taken off the floor."
        )
        return ctx.synthesize(
            {"nest": nest, "work_order": laser, "demoted_to_draft": False, "note": note},
            tool=tool,
            method="POST",
            path=path,
        )

    outcome = await _demote_to_draft(ctx, laser, headline="NEST ADDED", evidence={"nest": nest})
    return ctx.synthesize({"nest": nest, **outcome}, tool=tool, method="POST", path=path)


# --------------------------------------------------------------------------- reads


_DASHBOARD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_dispatch_board": {
            "type": "boolean",
            "default": False,
            "description": "Also fetch GET /shop-floor/dispatch-board and return both.",
        }
    },
}


async def _get_shop_floor_dashboard(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/shop-floor/dashboard"
    if not args.get("include_dispatch_board"):
        return ctx.finish(await ctx.call("GET", path), tool="get_shop_floor_dashboard", method="GET", path=path)
    dashboard = await ctx.call_json("GET", path)
    board = await ctx.call_json("GET", f"{API}/shop-floor/dispatch-board")
    return ctx.synthesize(
        {"dashboard": dashboard, "dispatch_board": board}, tool="get_shop_floor_dashboard", method="GET", path=path
    )


_LIST_INVENTORY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "boolean",
            "default": False,
            "description": "Per-part totals (GET /inventory/summary) instead of rows.",
        },
        "part_id": {"type": "integer", "minimum": 1},
        "warehouse": {"type": "string"},
        "location_code": {"type": "string"},
        "has_quantity": {"type": "boolean", "default": True, "description": "Only rows with stock on hand."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
}


async def _list_inventory(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    if args.get("summary"):
        path = f"{API}/inventory/summary"
        query = _present(args, "limit", "offset")
    else:
        path = f"{API}/inventory/"
        query = _present(args, "part_id", "warehouse", "location_code", "has_quantity", "limit", "offset")
    query.setdefault("limit", 200)
    return ctx.finish(await ctx.call("GET", path, query=query), tool="list_inventory", method="GET", path=path)


_LIST_PURCHASE_ORDERS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "PO status filter (draft, submitted, approved, received, closed, cancelled).",
        },
        "vendor_id": {"type": "integer", "minimum": 1},
        "deleted_only": {
            "type": "boolean",
            "default": False,
            "description": "Only soft-deleted POs (the restore view).",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 200},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
}


async def _list_purchase_orders(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/purchasing/purchase-orders"
    query = _present(args, "status", "vendor_id", "deleted_only", "limit", "offset")
    query.setdefault("limit", 200)
    return ctx.finish(await ctx.call("GET", path, query=query), tool="list_purchase_orders", method="GET", path=path)


_LIST_NCRS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "NCR status filter (open, under_review, disposition_pending, closed, ...).",
        },
        "part_id": {"type": "integer", "minimum": 1},
        "skip": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 100},
    },
}


async def _list_quality_ncrs(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/quality/ncr"
    query = _present(args, "status", "part_id", "skip", "limit")
    return ctx.finish(await ctx.call("GET", path, query=query), tool="list_quality_ncrs", method="GET", path=path)


_LIST_PARTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "search": {"type": "string", "description": "Matches part number, name, description or customer part number."},
        "part_type": {"type": "string", "description": "manufactured | purchased | assembly | raw_material"},
        "item_group": {"type": "string", "default": "engineering", "description": "engineering | materials | all"},
        "active_only": {"type": "boolean", "default": True},
        "include_bom_components": {"type": "boolean", "default": True},
        "include_deleted": {"type": "boolean", "default": False, "description": "Admin only."},
        "skip": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    },
}


async def _list_parts(ctx: ToolContext, args: Dict[str, Any]) -> types.CallToolResult:
    path = f"{API}/parts/"
    query = _present(
        args,
        "search",
        "part_type",
        "item_group",
        "active_only",
        "include_bom_components",
        "include_deleted",
        "skip",
        "limit",
    )
    return ctx.finish(await ctx.call("GET", path, query=query), tool="list_parts", method="GET", path=path)


# --------------------------------------------------------------------------- registry


_READ = ToolAnnotationHints(read_only=True, destructive=False, idempotent=True)
_CREATE = ToolAnnotationHints(read_only=False, destructive=False, idempotent=False)
_MUTATE = ToolAnnotationHints(read_only=False, destructive=False, idempotent=False)
_UPDATE = ToolAnnotationHints(read_only=False, destructive=False, idempotent=True)


def _strict(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Every convenience schema refuses unknown arguments: a misspelled filter is a 422 that
    names it, never a silently dropped one that hands back the UNFILTERED result."""
    return {**dict(schema), "additionalProperties": False}


CONVENIENCE_TOOLS: Tuple[ConvenienceTool, ...] = (
    ConvenienceTool(
        "search",
        "GET /api/v1/search/ — Global search across parts, work orders, customers, vendors, POs and more. [Global Search]",
        _strict(_SEARCH_SCHEMA),
        _READ,
        _search,
    ),
    ConvenienceTool(
        "list_work_centers",
        "GET /api/v1/work-centers/ — The shop's work centers (id, code, name, type), optionally filtered by a name/code "
        "substring. Use it to find the id or exact name add_operation needs. [Work Centers]",
        _strict(_LIST_WORK_CENTERS_SCHEMA),
        _READ,
        _list_work_centers,
    ),
    ConvenienceTool(
        "create_work_order",
        "POST /api/v1/work-orders/ — Create a production work order. ALWAYS lands DRAFT (there is no status argument; "
        "passing one is refused); call release_work_order to put it on the floor. Refuses work_order_type "
        "'laser_cutting' — use import_laser_nest_package for laser jobs. Add steps with add_operation. [Work Orders]",
        _strict(_CREATE_WORK_ORDER_SCHEMA),
        _CREATE,
        _create_work_order,
    ),
    ConvenienceTool(
        "add_operation",
        "POST /api/v1/work-orders/{id}/operations — Add one routing step to a work order. Work center by id OR name; "
        "sequence defaults to the next multiple of 10. Operation names must be short shop-floor steps — file names, "
        "paths and 'Part Detail' are refused, not rewritten. An operation has no quantity of its own (it runs the "
        "work order's quantity_ordered); component_quantity is BOM component demand. Not for laser nest work "
        "orders. [Work Orders]",
        _strict(_ADD_OPERATION_SCHEMA),
        _MUTATE,
        _add_operation,
    ),
    ConvenienceTool(
        "get_work_order",
        "GET /api/v1/work-orders/{id} (or /by-number/{n}) — One work order with its operations, nests, status and "
        "optimistic-lock version. [Work Orders]",
        _strict(_GET_WORK_ORDER_SCHEMA),
        _READ,
        _get_work_order,
    ),
    ConvenienceTool(
        "update_work_order",
        "PUT /api/v1/work-orders/{id} — Edit a work order's header (priority, due date, quantity, customer, unit #, "
        "notes, sequencing, scrap) with its current version (409 when stale). status accepts draft / on_hold / "
        "cancelled only: 'released' and 'in_progress' are refused — releasing is release_work_order, starting is "
        "work_orders_start_work_order. [Work Orders]",
        _strict(_UPDATE_WORK_ORDER_SCHEMA),
        _UPDATE,
        _update_work_order,
    ),
    ConvenienceTool(
        "duplicate_work_order",
        "POST /api/v1/work-orders/{id}/duplicate — Copy a work order's PLAN (operations, instructions, nests, open "
        "material ties) onto a new DRAFT work order; the production record stays behind. [Work Orders]",
        _strict(_DUPLICATE_WORK_ORDER_SCHEMA),
        _CREATE,
        _duplicate_work_order,
    ),
    ConvenienceTool(
        "release_work_order",
        "POST /api/v1/work-orders/{id}/release — Release a DRAFT work order to production: the explicit authorization "
        "step that records who released it and promotes the first operation. The only tool that releases work "
        "(update_work_order refuses status 'released'). [Work Orders]",
        _strict(_RELEASE_WORK_ORDER_SCHEMA),
        _MUTATE,
        _release_work_order,
    ),
    ConvenienceTool(
        "import_laser_nest_package",
        "POST /api/v1/work-orders/laser-nest-packages/standalone/import or /{id}/laser-nest-packages/import — Import a "
        "nest package (ZIP or nest PDF) into a laser work order. The route creates it RELEASED with its nest "
        "operations READY; this tool sets it back to DRAFT and the operations to PENDING unless release=true, and "
        "refuses a target whose laser work order already carries manually entered nests. Returns {import, "
        "work_order, demoted_to_draft, operations_returned_to_pending, note}. [Work Orders]",
        _strict(_IMPORT_NEST_SCHEMA),
        _CREATE,
        _import_laser_nest_package,
    ),
    ConvenienceTool(
        "add_laser_nest",
        "POST /api/v1/work-orders/{id}/laser-nests/manual — Key one laser nest by hand onto an assembly parent (its "
        "laser child is found-or-created) or a laser work order. The route force-sets the laser work order RELEASED; "
        "this tool puts a new or previously-DRAFT one back to DRAFT with its operations PENDING unless release=true, "
        "never takes a job already on the floor off it, and refuses a job whose nests came from a package import. "
        "Returns {nest, work_order, demoted_to_draft, ...}. [Work Orders]",
        _strict(_ADD_LASER_NEST_SCHEMA),
        _CREATE,
        _add_laser_nest,
    ),
    ConvenienceTool(
        "get_shop_floor_dashboard",
        "GET /api/v1/shop-floor/dashboard — Live shop-floor status (active operations, operators, holds); optionally "
        "the dispatch board too. [Shop Floor]",
        _strict(_DASHBOARD_SCHEMA),
        _READ,
        _get_shop_floor_dashboard,
    ),
    ConvenienceTool(
        "list_inventory",
        "GET /api/v1/inventory/ (or /inventory/summary) — Stock rows by part/warehouse/location, or per-part totals "
        "with summary=true. [Inventory]",
        _strict(_LIST_INVENTORY_SCHEMA),
        _READ,
        _list_inventory,
    ),
    ConvenienceTool(
        "list_purchase_orders",
        "GET /api/v1/purchasing/purchase-orders — Purchase orders filtered by status / vendor. [Purchasing]",
        _strict(_LIST_PURCHASE_ORDERS_SCHEMA),
        _READ,
        _list_purchase_orders,
    ),
    ConvenienceTool(
        "list_quality_ncrs",
        "GET /api/v1/quality/ncr — Non-conformance reports filtered by status / part. [Quality Management]",
        _strict(_LIST_NCRS_SCHEMA),
        _READ,
        _list_quality_ncrs,
    ),
    ConvenienceTool(
        "list_parts",
        "GET /api/v1/parts/ — The part master, searchable by number/name/description. [Parts]",
        _strict(_LIST_PARTS_SCHEMA),
        _READ,
        _list_parts,
    ),
)

CONVENIENCE_TOOL_NAMES: FrozenSet[str] = frozenset(tool.name for tool in CONVENIENCE_TOOLS)

_TAG_SUFFIX = re.compile(r"\[([^\]]+)\]\s*$")


def convenience_tags(tools: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS) -> FrozenSet[str]:
    """The OpenAPI tag each convenience description claims with its trailing ``[Tag]``.

    ``catalog.uncovered_tags`` credits a shadowed route's tag as covered only when a
    convenience tool claims it, so the suffix is a contract, not decoration: a
    description without one is a build error here rather than a silent credit.
    """
    tags = set()
    for tool in tools:
        match = _TAG_SUFFIX.search(tool.description)
        if match is None:
            raise ValueError(f"convenience tool {tool.name!r} must end its description with the [Tag] it stands in for")
        tags.add(match.group(1))
    return frozenset(tags)


CONVENIENCE_TAGS: FrozenSet[str] = convenience_tags()
