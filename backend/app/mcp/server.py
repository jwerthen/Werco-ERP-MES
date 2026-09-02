"""``build_server``: the lowlevel MCP ``Server`` over an executor and a catalog.

The server owns three things and nothing else:

1. The tool LIST -- convenience tools first (stable names), then the generated
   catalog sorted by name.
2. Per-call AUTH resolution -- the caller's ERP access token, from the HTTP request's
   ``Authorization`` header on the Streamable HTTP door or from the bridge-side
   ``TokenSource`` on stdio (``ServerRequestContext.request`` is the Starlette
   ``Request`` on HTTP and ``None`` on stdio / in-memory transports).
3. DISPATCH -- validate the arguments against the tool's own input schema, rebuild
   the HTTP request the catalog recorded, hand it to the executor, shape the result.

It never decides permissions. A 401/403/409 from the route is returned verbatim as an
``is_error`` result (brief rule 2), because the route is the boundary and the answer
it gives the SPA is the answer the agent gets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote

import jsonschema
import mcp.types as types
from mcp.server.lowlevel import Server

from app.core.config import settings
from app.mcp.auth import AuthContext, TokenSource, bearer_token_from_header, token_is_acceptable
from app.mcp.catalog import FORM_CONTENT_TYPE, JSON_CONTENT_TYPE, MULTIPART_CONTENT_TYPE, GeneratedTool
from app.mcp.convenience import CONVENIENCE_TOOLS, ConvenienceTool, ToolContext, ToolFailure
from app.mcp.executor import Executor, FilesArg, UploadError, decode_upload, decode_upload_list
from app.mcp.results import build_tool_result, error_result, transport_error_result, validation_error_result

logger = logging.getLogger(__name__)

SERVER_NAME = "werco-erp"
MAX_VALIDATION_MESSAGES = 10

DEFAULT_INSTRUCTIONS = (
    "Werco ERP-MES (an AS9100D / ISO 9001 precision-manufacturing ERP) over MCP.\n"
    "- Every tool dispatches a real API request AS THE CALLER. The same role-based permissions, tenant scoping and "
    "audit trail as the web app apply, and every write is recorded as that user. A 401 means the access token is "
    "missing or expired; a 403 means the user's role does not allow the action. Neither can be worked around here.\n"
    "- create_work_order and duplicate_work_order ALWAYS produce a DRAFT work order. release_work_order is the "
    "separate, explicit step that puts work on the floor. update_work_order edits a header (priority, due date, "
    "quantity, notes, sequencing) but refuses status 'released' / 'in_progress': releasing is release_work_order and "
    "starting is work_orders_start_work_order.\n"
    "- import_laser_nest_package and add_laser_nest: the API creates (or force-sets) the laser work order RELEASED "
    "with its nest operations READY; the tools put a new or previously-DRAFT laser work order back to DRAFT with "
    "the operations PENDING unless release=true (add_laser_nest never takes a job that was already on the floor "
    "off it). Manually entered nests and a package import are never mixed on one job, in either direction.\n"
    "- Operation names are short shop-floor steps ('Laser', 'Brake', 'Weld'); file names, paths and 'Part Detail' "
    "are refused, not rewritten.\n"
    "- Shop-floor clock-in / start / complete tools are the real routes: nothing is auto-completed and no "
    "quantities are invented.\n"
    "- Reads are broad: the generated tools mirror the whole API. Start with the convenience tools (search, "
    "list_*, get_work_order, get_shop_floor_dashboard). Large results are truncated with a note; narrow them with "
    "limit / skip / filters.\n"
    "- Convenience tool names are stable. Generated names come from the API's function names and gain a tag prefix "
    "when two routers share one (shop_floor_start_operation vs work_orders_start_operation)."
)


@dataclass
class ResultCaps:
    max_result_chars: int
    max_blob_bytes: int
    max_upload_bytes: int

    @classmethod
    def from_settings(cls) -> "ResultCaps":
        return cls(
            max_result_chars=settings.WERCO_MCP_MAX_RESULT_CHARS,
            max_blob_bytes=settings.WERCO_MCP_MAX_BLOB_BYTES,
            max_upload_bytes=settings.WERCO_MCP_MAX_UPLOAD_BYTES,
        )


@dataclass
class _Entry:
    tool: types.Tool
    validator: jsonschema.Draft202012Validator
    generated: Optional[GeneratedTool] = None
    convenience: Optional[ConvenienceTool] = None

    @property
    def method(self) -> Optional[str]:
        """The HTTP method a generated tool dispatches; a convenience tool makes several calls, so None."""
        return self.generated.method if self.generated else None

    @property
    def path(self) -> Optional[str]:
        return self.generated.path if self.generated else None


def _mcp_tool(
    name: str, description: str, input_schema: Mapping[str, Any], hints: Any, title: Optional[str] = None
) -> types.Tool:
    return types.Tool(
        name=name,
        title=title or None,
        description=description,
        input_schema=dict(input_schema),
        annotations=types.ToolAnnotations(**hints.as_dict()),
    )


def build_registry(
    catalog: Sequence[GeneratedTool],
    convenience: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS,
) -> Dict[str, _Entry]:
    """Convenience tools first, then the catalog by name; a duplicate name is a build error.

    A convenience tool whose route is not in ``SHADOWED_OPERATIONS`` would collide
    with its generated twin here rather than silently win -- that is the point.
    """
    registry: Dict[str, _Entry] = {}
    for tool in convenience:
        registry[tool.name] = _Entry(
            tool=_mcp_tool(tool.name, tool.description, tool.input_schema, tool.hints),
            validator=jsonschema.Draft202012Validator(tool.input_schema),
            convenience=tool,
        )
    for generated in sorted(catalog, key=lambda item: item.name):
        if generated.name in registry:
            raise ValueError(f"Tool name {generated.name!r} is both a convenience tool and a generated tool")
        registry[generated.name] = _Entry(
            tool=_mcp_tool(
                generated.name, generated.description, generated.input_schema, generated.hints, generated.summary
            ),
            validator=jsonschema.Draft202012Validator(generated.input_schema),
            generated=generated,
        )
    return registry


async def resolve_auth(
    ctx: Any,
    token_source: Optional[TokenSource],
    *,
    verify_bearer: bool = True,
) -> Union[AuthContext, types.CallToolResult]:
    """The caller's credentials for this call, or the 401 result explaining their absence.

    HTTP door: ``ctx.request`` is the transport's Starlette ``Request``; the bearer
    token it carries was already accepted by ``ErpTokenVerifier`` at the door and is
    re-checked here cheaply (``verify_bearer`` is False only for the dev HTTP bridge in
    front of a REMOTE ERP, whose tokens this process cannot verify -- the remote routes
    do). stdio / in-memory: ``ctx.request`` is ``None`` and the ``TokenSource`` speaks.
    """
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        token = bearer_token_from_header(headers.get("authorization"))
        if token is None:
            return error_result(
                status=401,
                detail="Missing bearer token: send 'Authorization: Bearer <ERP access token>' on the MCP request.",
            )
        if verify_bearer and not token_is_acceptable(token):
            return error_result(
                status=401,
                detail="Invalid, expired or kiosk-scoped access token. Obtain a fresh ERP access token and retry.",
            )
        client = getattr(request, "client", None)
        client_host = getattr(client, "host", None) or "127.0.0.1"
        return AuthContext(token=token, client_host=client_host, host_header=headers.get("host"))

    if token_source is None:
        return error_result(
            status=401,
            detail="No ERP credentials configured for this bridge: set WERCO_ERP_TOKEN, WERCO_ERP_REFRESH_TOKEN, "
            "or WERCO_ERP_EMAIL + WERCO_ERP_PASSWORD.",
        )
    token = await token_source.get_token()
    if not token:
        return error_result(
            status=401,
            detail="Could not obtain an ERP access token from the configured credentials (login or refresh failed).",
        )
    return AuthContext(token=token, client_host="127.0.0.1", host_header=None, token_source=token_source)


def validation_messages(validator: jsonschema.Draft202012Validator, arguments: Mapping[str, Any]) -> List[str]:
    errors = sorted(validator.iter_errors(arguments), key=lambda err: list(err.absolute_path))
    messages = []
    for err in errors[:MAX_VALIDATION_MESSAGES]:
        location = "/".join(str(part) for part in err.absolute_path) or "<arguments>"
        messages.append(f"{location}: {err.message}")
    if len(errors) > MAX_VALIDATION_MESSAGES:
        messages.append(f"... and {len(errors) - MAX_VALIDATION_MESSAGES} more")
    return messages


def build_generated_request(
    tool: GeneratedTool,
    arguments: Mapping[str, Any],
    *,
    max_upload_bytes: int,
) -> Tuple[str, Dict[str, Any], Any, Optional[FilesArg], Optional[Dict[str, Any]]]:
    """Split a flat MCP argument object back into ``(path, query, json, files, form)``."""
    path = tool.path
    for name in tool.path_params:
        path = path.replace("{" + name + "}", quote(str(arguments[name]), safe=""))
    query = {name: arguments[name] for name in tool.query_params if name in arguments}

    json_body: Any = None
    files: Optional[Dict[str, Any]] = None
    form: Optional[Dict[str, Any]] = None

    if tool.body_content_type == JSON_CONTENT_TYPE:
        if tool.body_wrapped:
            wrapped = tool.body_properties[0]
            if wrapped in arguments:
                json_body = arguments[wrapped]
            elif tool.body_required:
                json_body = {}
        else:
            supplied = {
                tool.body_property_map.get(name, name): arguments[name]
                for name in tool.body_properties
                if name in arguments
            }
            if supplied or tool.body_required:
                json_body = supplied
    elif tool.body_content_type in (MULTIPART_CONTENT_TYPE, FORM_CONTENT_TYPE):
        form = {}
        files = {}
        for name in tool.body_properties:
            if name not in arguments or arguments[name] is None:
                continue
            field_name = tool.body_property_map.get(name, name)
            if name in tool.file_fields:
                files[field_name] = decode_upload(arguments[name], max_bytes=max_upload_bytes)
            elif name in tool.file_list_fields:
                files[field_name] = decode_upload_list(arguments[name], max_bytes=max_upload_bytes)
            else:
                form[field_name] = arguments[name]
        if not files:
            files = None
    return path, query, json_body, files, form


def build_server(
    executor: Executor,
    *,
    catalog: Sequence[GeneratedTool],
    token_source: Optional[TokenSource] = None,
    instructions: str = DEFAULT_INSTRUCTIONS,
    version: str = "0",
    caps: Optional[ResultCaps] = None,
    verify_bearer: bool = True,
    convenience: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS,
) -> Server:
    """Assemble the ``werco-erp`` MCP server.

    ``executor`` is how tools reach the API (in-process ASGI or remote HTTPS);
    ``catalog`` is the generated tool list from ``build_catalog(app.openapi(), ...)``;
    ``token_source`` supplies credentials on stdio (the HTTP door reads the bearer
    header instead). ``caps`` default to the ``WERCO_MCP_*`` settings.
    """
    result_caps = caps or ResultCaps.from_settings()
    registry = build_registry(catalog, convenience)
    listing = types.ListToolsResult(tools=[entry.tool for entry in registry.values()])

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return listing

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = registry.get(params.name)
        if entry is None:
            return error_result(status=404, detail=f"Unknown tool {params.name!r}. Call tools/list for the catalog.")

        auth = await resolve_auth(ctx, token_source, verify_bearer=verify_bearer)
        if isinstance(auth, types.CallToolResult):
            return auth

        arguments: Dict[str, Any] = dict(params.arguments or {})
        problems = validation_messages(entry.validator, arguments)
        if problems:
            return validation_error_result(params.name, problems)

        try:
            if entry.convenience is not None:
                context = ToolContext(
                    executor,
                    auth,
                    max_result_chars=result_caps.max_result_chars,
                    max_blob_bytes=result_caps.max_blob_bytes,
                    max_upload_bytes=result_caps.max_upload_bytes,
                )
                return await entry.convenience.handler(context, arguments)

            generated = entry.generated
            assert generated is not None  # nosec B101 - registry invariant, not input validation
            path, query, json_body, files, form = build_generated_request(
                generated, arguments, max_upload_bytes=result_caps.max_upload_bytes
            )
            outcome = await executor.request(
                method=generated.method, path=path, query=query, json=json_body, files=files, form=form, auth=auth
            )
            return build_tool_result(
                tool_name=generated.name,
                method=generated.method,
                path=path,
                result=outcome,
                max_chars=result_caps.max_result_chars,
                max_blob_bytes=result_caps.max_blob_bytes,
            )
        except ToolFailure as failure:
            return failure.result
        except UploadError as exc:
            return error_result(status=exc.status, detail=str(exc), method=entry.method, path=entry.path)
        except Exception as exc:  # noqa: BLE001 - a tool call must answer, never crash the session
            logger.exception("MCP tool %s failed before producing an HTTP status", params.name)
            return transport_error_result(exc, method=entry.method, path=entry.path)

    return Server(
        SERVER_NAME,
        version=version,
        instructions=instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
