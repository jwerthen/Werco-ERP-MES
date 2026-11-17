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

The one narrowing it applies itself is a READ-ONLY API token's (migration ``119``), and
only so the agent is never shown or handed what the API layer would refuse anyway:
``tools/list`` omits every tool that writes and every tool under a tag in
``WERCO_MCP_READ_ONLY_DENY_TAGS``, and calling one by name is a 403 BEFORE argument
validation or dispatch (every tool stays in the registry, so it is never a 404).
``get_current_user`` enforces the same rule on every dispatched request regardless.

An AI-EXTERNAL API token (always read-only; Claude-plugin brief P0.3) is narrowed further,
to the tools whose every route is on ``app.core.export_control.AI_EXTERNAL_ALLOWED_ROUTES``:
its ``tools/list`` shows only those, and any other tool called by name is a 403 with the
API layer's own text before dispatch. What those tools return is redacted by the API's
export-control middleware on the in-process dispatch, not here.

And one narrowing applies to EVERY caller, a person's own access JWT included, because it is
about the agent rather than the credential: a Hank task kind that posts a person's own words
(``credentials.PEOPLE_ONLY_HANK_TASK_KINDS``: ``add_note``, a work-order comment) is never run
through an agent. ``prepare_task`` for such a kind, and ``execute_task`` on such a task (read
first, as the caller, through ``GET /hank/tasks/{task_id}``; a read that cannot complete is a 503,
never a blind execute), answer 403 before dispatch -- the reason the comment write routes are not
tools at all. One code path:
``app.mcp.people_only.people_only_refusal``. Both tools stay listed; every other kind is
unaffected, and the routes themselves still serve a person in the app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote

import jsonschema
import mcp.types as types
from mcp.server.lowlevel import Server

from app.core.config import settings
from app.core.credentials import READ_ONLY_TOOL_DENIED_DETAIL, READ_ONLY_TOOL_WRITES_DETAIL
from app.core.export_control import AI_EXTERNAL_ROUTE_DENIED_DETAIL, ai_external_operations
from app.mcp.auth import (
    FULL_SCOPE,
    AuthContext,
    CallerScope,
    TokenSource,
    api_token_claims,
    api_token_scope_hint,
    bearer_token_from_header,
    scope_from_row_claims,
    token_is_acceptable,
)
from app.mcp.catalog import FORM_CONTENT_TYPE, JSON_CONTENT_TYPE, MULTIPART_CONTENT_TYPE, GeneratedTool
from app.mcp.convenience import (
    CONVENIENCE_TOOL_ROUTES,
    CONVENIENCE_TOOLS,
    ConvenienceTool,
    ToolContext,
    ToolFailure,
    convenience_tool_reads,
    convenience_tool_tag,
)
from app.mcp.executor import Executor, FilesArg, UploadError, decode_upload, decode_upload_list
from app.mcp.people_only import people_only_refusal
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
    "quantity, notes, sequencing) but refuses status 'released' / 'in_progress': releasing is release_work_order, "
    "starting is work_orders_start_work_order (released jobs only), and lifting a work-order hold is "
    "work_orders_resume_work_order.\n"
    "- import_laser_nest_package and add_laser_nest: the API creates (or force-sets) the laser work order RELEASED "
    "with its nest operations READY and records the release (an on-hold job stays on hold); the tools put a new or "
    "previously-DRAFT laser work order back to DRAFT with "
    "the operations PENDING unless release=true (add_laser_nest never takes a job that was already on the floor "
    "off it). Manually entered nests and a package import are never mixed on one job, in either direction.\n"
    "- Operation names are short shop-floor steps ('Laser', 'Brake', 'Weld'); file names, paths and 'Part Detail' "
    "are refused, not rewritten.\n"
    "- Shop-floor clock-in / start / complete tools are the real routes and no quantities are invented. A "
    "clock_out whose good count reaches every unit that can reach the operation (the order, less any short "
    "declared upstream) COMPLETES the operation: its result's operation_status is then 'complete' -- check it "
    "before calling a complete tool. clock_in, shop_floor_start_operation, work_orders_start_operation and "
    "completing a READY operation refuse 409 on a draft or on-hold work order: release it (release_work_order) or "
    "resume it (work_orders_resume_work_order) first.\n"
    "- Short counts: no operation or work order is ever closed short at the counted quantity. A 409 saying units "
    "are 'not yet accounted for' or 'unaccounted' means the pieces must be recorded, or reported as scrap with a "
    "reason by the person who knows where they went: leave the work open and tell the user. Never invent scrap, "
    "a reason or a quantity to make a close succeed. An API token may not report scrap anywhere (clock-out, "
    "production reports, closes, force-complete or a PUT that raises quantity_scrapped -- 403; a person reports "
    "scrap, since a scrap count is quality evidence someone has to have seen) or change a scrap reason; nor may it "
    "close work short, record more good than the floor counted, edit an operation's or a work order header's "
    "counts, correct a count with either reduce-production tool, lower an order (or a laser nest's planned "
    "runs) once work was recorded, or re-import a nest package onto a laser job with recorded work (403). "
    "Good-only reports, zero-scrap clock-outs and full-count closes still work.\n"
    "- A READ-ONLY API token is shown only tools that read, outside the areas denied to read-only tokens "
    "(documents, exports, users, the audit log, admin and platform settings, API tokens, intake uploads). A tool "
    "that writes, or one in a denied area, answers 403 when called by name; so does the API itself.\n"
    "- Hank notes are a person's own words: prepare_task with kind 'add_note', and execute_task on an add_note "
    "task, answer 403 here on any credential -- ask the person to post the note from the app. Other Hank task "
    "kinds (change_due_date included) are prepared and executed as usual.\n"
    "- An AI-EXTERNAL token sees a narrower read set still. Records of export-controlled customers, and records "
    "the ERP cannot tie to a customer, come back with their ids, statuses, dates and quantities but every other "
    "text replaced by '[export-controlled]' (and \"export_controlled\": true): report them as withheld, never "
    "guess what they contain.\n"
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
    # A READ dispatches only GETs (a generated tool's method; every route a convenience
    # tool declares). ``tags`` are the OpenAPI tags it reaches, for the read-only deny list.
    reads: bool = False
    tags: FrozenSet[str] = field(default_factory=frozenset)
    # Every route it may dispatch is on the AI-external allowlist (P0.3).
    ai_external_allowed: bool = False

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


def default_ai_external_operations() -> FrozenSet[Tuple[str, str]]:
    """The AI-external allowlist as full ``(METHOD, /api/v1/...)`` pairs -- the catalog's own path form."""
    return ai_external_operations(settings.API_V1_PREFIX)


def build_registry(
    catalog: Sequence[GeneratedTool],
    convenience: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS,
    *,
    ai_external_ops: Optional[Iterable[Tuple[str, str]]] = None,
) -> Dict[str, _Entry]:
    """Convenience tools first, then the catalog by name; a duplicate name is a build error.

    A convenience tool whose route is not in ``SHADOWED_OPERATIONS`` would collide
    with its generated twin here rather than silently win -- that is the point.
    ``ai_external_ops`` defaults to the AI-external allowlist under the mounted prefix.
    """
    allowed_ops = frozenset(default_ai_external_operations() if ai_external_ops is None else ai_external_ops)
    registry: Dict[str, _Entry] = {}
    route_tags = {tool.key: frozenset(tool.tags or (tool.tag,)) for tool in catalog}
    for tool in convenience:
        declared = CONVENIENCE_TOOL_ROUTES.get(tool.name, frozenset())
        registry[tool.name] = _Entry(
            tool=_mcp_tool(tool.name, tool.description, tool.input_schema, tool.hints),
            validator=jsonschema.Draft202012Validator(tool.input_schema),
            convenience=tool,
            reads=convenience_tool_reads(tool),
            tags=_convenience_tags(tool, route_tags),
            # A tool with no declared routes cannot be placed: never AI-external (fails closed).
            ai_external_allowed=bool(declared) and all(route in allowed_ops for route in declared),
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
            reads=generated.method == "GET",
            tags=frozenset(generated.tags or (generated.tag,)),
            ai_external_allowed=generated.key in allowed_ops,
        )
    return registry


def _convenience_tags(tool: ConvenienceTool, route_tags: Mapping[Tuple[str, str], FrozenSet[str]]) -> FrozenSet[str]:
    """The tag the tool claims, plus the catalog tags of every route it declares.

    A shadowed route has no generated twin to read a tag from; its tool's claimed ``[Tag]``
    stands for it (``tests/test_mcp_read_only.py`` pins every read tool's routes to the tag
    it claims, against the live OpenAPI document).
    """
    tags = set()
    claimed = convenience_tool_tag(tool)
    if claimed:
        tags.add(claimed)
    for key in CONVENIENCE_TOOL_ROUTES.get(tool.name, ()):
        tags.update(route_tags.get(key, ()))
    return frozenset(tags)


def read_only_refusal(*, reads: bool, tags: Iterable[str], deny_tags: Iterable[str]) -> Optional[str]:
    """The refusal a READ-ONLY caller gets for a tool, or None when the tool is theirs to call.

    The write check comes first, so a writing tool under a denied tag is told it writes.
    """
    if not reads:
        return READ_ONLY_TOOL_WRITES_DETAIL
    if set(tags) & set(deny_tags):
        return READ_ONLY_TOOL_DENIED_DETAIL
    return None


def ai_external_refusal(*, reads: bool, tags: Iterable[str], deny_tags: Iterable[str], allowed: bool) -> Optional[str]:
    """The refusal an AI-EXTERNAL caller gets for a tool, or None.

    The read-only rule first (an AI-external token is always read-only), then the allowlist.
    """
    refusal = read_only_refusal(reads=reads, tags=tags, deny_tags=deny_tags)
    if refusal is not None:
        return refusal
    return None if allowed else AI_EXTERNAL_ROUTE_DENIED_DETAIL


def read_only_view(
    catalog: Sequence[GeneratedTool],
    convenience: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS,
    *,
    deny_tags: Iterable[str],
) -> Tuple[List[GeneratedTool], List[ConvenienceTool]]:
    """``(generated, convenience)`` a READ-ONLY API token is shown -- the same rule
    ``build_server``'s read-only listing applies (one classification, ``build_registry``),
    in the input order. ``python -m app.mcp --print-catalog --read-only`` prints it."""
    denied = frozenset(deny_tags)
    registry = build_registry(catalog, convenience)
    keep = {
        name
        for name, entry in registry.items()
        if read_only_refusal(reads=entry.reads, tags=entry.tags, deny_tags=denied) is None
    }
    return [tool for tool in catalog if tool.name in keep], [tool for tool in convenience if tool.name in keep]


def ai_external_view(
    catalog: Sequence[GeneratedTool],
    convenience: Sequence[ConvenienceTool] = CONVENIENCE_TOOLS,
    *,
    deny_tags: Iterable[str],
) -> Tuple[List[GeneratedTool], List[ConvenienceTool]]:
    """``(generated, convenience)`` an AI-EXTERNAL API token is shown -- ``build_server``'s
    AI-external listing rule, in the input order."""
    denied = frozenset(deny_tags)
    registry = build_registry(catalog, convenience)
    keep = {
        name
        for name, entry in registry.items()
        if ai_external_refusal(reads=entry.reads, tags=entry.tags, deny_tags=denied, allowed=entry.ai_external_allowed)
        is None
    }
    return [tool for tool in catalog if tool.name in keep], [tool for tool in convenience if tool.name in keep]


def caller_scope(
    ctx: Any,
    token_source: Optional[TokenSource],
    *,
    verify_bearer: bool = True,
    token: Optional[str] = None,
) -> CallerScope:
    """How this call's credential narrows the caller (``FULL_SCOPE`` for a person or a full token).

    HTTP door: the row's ``scope`` / ``ai_external`` that ``ErpTokenVerifier`` put in the
    authenticated ``AccessToken.claims`` -- no second read. An API JWT whose verified
    principal carries no such record fails CLOSED (read-only, AI-external); only the dev
    passthrough in front of a REMOTE ERP (``verify_bearer`` False), which cannot read the
    row, falls back to the token's own claims. stdio: the static ``WERCO_ERP_TOKEN``'s own
    claims (or ``token``'s, when the call already holds one) -- never a login just to list.
    The token-claim fallbacks only ever NARROW; the dispatched route re-decides from the row.
    """
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        bearer = bearer_token_from_header(headers.get("authorization"))
        if bearer is None:
            return FULL_SCOPE  # no credential: the call is a 401 and the door refused the list
        principal = getattr(getattr(request, "scope", {}).get("user"), "access_token", None)
        recorded = scope_from_row_claims(getattr(principal, "claims", None))
        if recorded is not None:
            return recorded
        if verify_bearer and api_token_claims(bearer) is not None:
            return CallerScope(read_only=True, ai_external=True)
        return api_token_scope_hint(bearer)

    if token is not None:
        return api_token_scope_hint(token)
    static = token_source.static_token if token_source is not None else None
    return api_token_scope_hint(static) if static else FULL_SCOPE


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
                detail="Invalid, expired, revoked or kiosk-scoped token. Present a live ERP access token or API token "
                "and retry.",
            )
        client = getattr(request, "client", None)
        client_host = getattr(client, "host", None) or "127.0.0.1"
        narrowed = caller_scope(ctx, token_source, verify_bearer=verify_bearer)
        return AuthContext(
            token=token,
            client_host=client_host,
            host_header=headers.get("host"),
            read_only=narrowed.read_only,
            ai_external=narrowed.ai_external,
        )

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
    narrowed = caller_scope(ctx, token_source, verify_bearer=verify_bearer, token=token)
    return AuthContext(
        token=token,
        client_host="127.0.0.1",
        host_header=None,
        token_source=token_source,
        read_only=narrowed.read_only,
        ai_external=narrowed.ai_external,
    )


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
    read_only_deny_tags: Optional[Iterable[str]] = None,
) -> Server:
    """Assemble the ``werco-erp`` MCP server.

    ``executor`` is how tools reach the API (in-process ASGI or remote HTTPS);
    ``catalog`` is the generated tool list from ``build_catalog(app.openapi(), ...)``;
    ``token_source`` supplies credentials on stdio (the HTTP door reads the bearer
    header instead). ``caps`` default to the ``WERCO_MCP_*`` settings, and
    ``read_only_deny_tags`` to ``settings.read_only_deny_tags``.

    Both listings are computed HERE, once: the full one, and the read-only one a
    read-only API token is shown (reads only, no denied tag).
    """
    result_caps = caps or ResultCaps.from_settings()
    deny_tags: FrozenSet[str] = frozenset(
        settings.read_only_deny_tags if read_only_deny_tags is None else read_only_deny_tags
    )
    registry = build_registry(catalog, convenience)
    listing = types.ListToolsResult(tools=[entry.tool for entry in registry.values()])
    read_only_listing = types.ListToolsResult(
        tools=[
            entry.tool
            for entry in registry.values()
            if read_only_refusal(reads=entry.reads, tags=entry.tags, deny_tags=deny_tags) is None
        ]
    )
    ai_external_listing = types.ListToolsResult(
        tools=[
            entry.tool
            for entry in registry.values()
            if ai_external_refusal(
                reads=entry.reads, tags=entry.tags, deny_tags=deny_tags, allowed=entry.ai_external_allowed
            )
            is None
        ]
    )

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        narrowed = caller_scope(ctx, token_source, verify_bearer=verify_bearer)
        if narrowed.ai_external:
            return ai_external_listing
        if narrowed.read_only:
            return read_only_listing
        return listing

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = registry.get(params.name)
        if entry is None:
            return error_result(status=404, detail=f"Unknown tool {params.name!r}. Call tools/list for the catalog.")

        auth = await resolve_auth(ctx, token_source, verify_bearer=verify_bearer)
        if isinstance(auth, types.CallToolResult):
            return auth

        if auth.read_only or auth.ai_external:
            # Before validation and dispatch: nothing a narrowed token may not do is sent.
            if auth.ai_external:
                refusal = ai_external_refusal(
                    reads=entry.reads, tags=entry.tags, deny_tags=deny_tags, allowed=entry.ai_external_allowed
                )
            else:
                refusal = read_only_refusal(reads=entry.reads, tags=entry.tags, deny_tags=deny_tags)
            if refusal is not None:
                return error_result(status=403, detail=refusal, method=entry.method, path=entry.path)

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
            # People-only Hank kinds (add_note): refused for an agent on ANY credential, before dispatch.
            # Judged on the body the route would receive; an execute reads the task first, as the caller.
            people_only = await people_only_refusal(
                method=generated.method,
                route=generated.path,
                path=path,
                json_body=json_body,
                executor=executor,
                auth=auth,
            )
            if people_only is not None:
                status, detail = people_only
                return error_result(status=status, detail=detail, method=generated.method, path=path)
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
