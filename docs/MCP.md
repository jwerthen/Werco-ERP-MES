# Werco ERP over MCP (Model Context Protocol)

`backend/app/mcp/` turns the FastAPI application's own OpenAPI document into an MCP tool catalog
and dispatches every tool call back through the real routers, as the calling user. An agent
(Cursor, Claude Code, a chat bot) gets **677 tools** — 15 hand-written *convenience* tools plus
**662 generated** ones — and every one of them is exactly as powerful, and exactly as restricted,
as the same request from the web app.

This is the runbook: how to run it, how to authenticate, what the tools are called, which rules
the convenience tools enforce, what a result looks like, and what to do when a call fails.

**Counts and names in this file come from `python -m app.mcp --print-catalog` on the commit that
shipped API tokens** (15 + 662 tools, 16 shadowed raw operations, 2 excluded cutover loaders,
66 tags in the OpenAPI document, 62 of them in the catalog). Generated names are derived, not
curated — re-run the command after adding a router before quoting a name in a prompt.
`backend/tests/test_mcp_docs_names.py` checks every tool name this file, `CLAUDE.md` and `API.md`
quote against the live catalog, so a name that shifts fails CI instead of going stale here.

---

## 1. What it is, and what it is not

**It is:**

- **The same backend as the UI.** A tool call becomes an HTTP request to `app.main:app` — in
  process over `httpx.ASGITransport`, or over HTTPS to a deployed API — carrying the caller's own
  bearer token. The request passes the full middleware stack and the route's own dependencies
  (`get_current_user`, `get_current_company_id`, `require_role`, `get_audit_service`). **The router
  is the RBAC, tenancy and audit boundary**; nothing in `app/mcp/` imports a service or builds a
  user, and its one database read is the door's API-token row check (§4), which decides whether
  a credential is still live and nothing else.
- **OpenAPI as the catalog.** Generated tools are built from `app.openapi()` at startup — the same
  dict Swagger renders in non-production. A router that ships tomorrow is a tool tomorrow, with no
  list to update. (`app.openapi()` builds the document in process even in production, where the
  served `/api/openapi.json` route is deliberately disabled; the catalog does not re-enable it.)
- **Reads broad, writes real.** 62 of the API's 66 tags are in. Data Export, Users, Audit,
  Admin Settings, Platform Administration and Company Management are included: their own
  `require_role` gates and audit rows are the control, not the catalog.

**It is not:**

- **A god token or a new role.** There is no MCP identity. Every call carries a real ERP user's
  bearer — an access JWT, or an API token bound to one user (§4); a 401 or 403 from the route is returned verbatim (`is_error=true`, body carries
  `status` and the server's `detail`). Nothing here widens exports, bypasses `require_role` or adds
  a permission — see [RBAC_PERMISSIONS.md → MCP / agent access](RBAC_PERMISSIONS.md#mcp--agent-access).
- **A service-call shortcut.** Calling `services/` directly with a hand-built user would skip the
  tenant scoping, the audit rows and the optimistic-locking 409s the routers provide. Every one of
  those is a compliance invariant (`CLAUDE.md` → Compliance-critical invariants), so the package
  refuses the shortcut structurally: it has no way to reach a service.
- **HTTP-to-self.** The in-process executor dispatches into the ASGI app object without a socket.
  It is not a loopback HTTP call, so it needs no extra port, no self-signed TLS and no allowance in
  `ALLOWED_HOSTS`; the caller's `Host` is forwarded when there is one, and the first non-`*`
  `ALLOWED_HOSTS` entry (else `localhost`) is used when there is not.
- **A shop-floor automaton.** Clock-in / start / complete tools are the real routes (`clock_in`,
  `shop_floor_start_operation`, `shop_floor_complete_operation`, …). Nothing is auto-completed and
  no quantity is invented; the same gates (labor-record requirement, predecessor gating, 409 on a
  stale version) apply.

---

## 2. Architecture

```
backend/app/mcp/
  naming.py       pure: operationId -> function name, tag slugs, collision policy, 64-char fit
  catalog.py      pure: app.openapi() -> GeneratedTool list ($ref inlining, param/body mapping,
                  descriptions, annotations, EXCLUDED_TAGS / EXCLUDED_OPERATIONS / PUBLIC_OPERATIONS,
                  the two coverage guards uncovered_tags + unaccounted_operations, catalog_summary)
  results.py      pure: HTTP outcome -> CallToolResult (JSON/text/blob, caps, error shape)
  auth.py         AuthContext; TokenSource (static -> refresh -> password login); ErpTokenVerifier
                  (an access JWT by signature, an API token by its api_tokens row)
  executor.py     InProcessExecutor (httpx.ASGITransport into app) / RemoteExecutor (HTTPS)
  convenience.py  the 15 hand-written tools + SHADOWED_OPERATIONS (16 raw routes they replace)
  server.py       build_server(): registry, per-call auth, jsonschema validation, dispatch
  http.py         McpDoor + mount_mcp(app): the Streamable HTTP door at WERCO_MCP_HTTP_PATH
  __main__.py     python -m app.mcp: stdio bridge, dev HTTP server, --print-catalog
```

Two executors, one contract (`Executor.request(method, path, query, json, files, form, auth)`):

- **`InProcessExecutor(app)`** — `httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(caller_ip, 0)))`.
  A fresh transport per call on purpose: the `client` tuple is what the app's per-IP rate limiter
  keys on, so it must be *this* caller's address (the MCP HTTP caller's IP on the door,
  `127.0.0.1` on stdio) rather than one process-wide bucket every agent would share.
- **`RemoteExecutor(base_url)`** — real HTTPS to `WERCO_ERP_URL` (TLS verified, no redirects),
  for the stdio bridge running on a developer's or agent host's machine.

Both send what the SPA's Axios client sends — `Authorization: Bearer …`, `Accept: application/json, */*`,
`X-Requested-With: XMLHttpRequest`, `User-Agent: werco-mcp/<app version>` — and **neither ever sets
`Origin` or `Referer`**: the CSRF middleware enforces its browser rules only when one of those is
present, and an in-process dispatch is not a browser. Timeout is 120 s (a nest import can run long).

A **401 is retried once after a token refresh, and only for a bridge-side token** (`AuthContext.token_source`
set). A door caller's 401 passes straight through — their token is theirs to renew — and the
refresh/login exchanges go out with no auth at all, which is what keeps the retry from recursing.

---

## 3. Running it

### 3.1 The Streamable HTTP door (production shape)

The door is served **on the API host** at `WERCO_MCP_HTTP_PATH` (default `/mcp`), and it is **off by
default**. With `WERCO_MCP_HTTP_ENABLED=false` `mount_mcp(app)` returns `None` and touches nothing —
the API is byte-identical to a build without the package.

```bash
WERCO_MCP_HTTP_ENABLED=true          # the only switch needed
WERCO_MCP_HTTP_PATH=/mcp             # absolute path; "/", /api/*, /health/* and /docs/* refused
WERCO_MCP_HTTP_RATE_LIMIT=300/minute # per-IP ceiling on the door path itself (see the table)
```

What you get when it is on (all in `app/mcp/http.py` and `app/main.py`):

| Property | Detail |
|---|---|
| Route | A Starlette **`Route` at the exact path**, not a `Mount` (`Mount` answers a bare `POST /mcp` with a 307 to `/mcp/`, which MCP clients do not follow). Hidden from the OpenAPI schema. **`GET` → 405 `Allow: POST`**: a stateless door has no server-push stream to open, and answering it up front keeps the app's `GZipMiddleware` from holding an SSE response's headers for the transport's first 15 s keepalive. The path is refused at boot under `/api`, `/health` and `/docs`, and `mount_mcp` fails the boot if any existing route already serves it — a door on an occupied path would never be reached while `main.py` waived the JSON body cap for that route by path string. |
| Session model | **Stateless** (`stateless=True`, one transport per POST, no session id) with plain JSON responses (`json_response=True`). Railway runs `uvicorn --workers ${WEB_CONCURRENCY:-2}`; a session pinned to one worker's memory would break on the next request. Clients send `Accept: application/json, text/event-stream` and may call `tools/list` / `tools/call` without a prior `initialize`. |
| Auth at the door | `AuthenticationMiddleware(BearerAuthBackend(ErpTokenVerifier))` → `AuthContextMiddleware` → `RequireAuthMiddleware`. No bearer, an invalid/expired token, a non-`access` token or a **kiosk-scoped** token → one clean **401 + `WWW-Authenticate`** before any JSON-RPC is parsed. (The chain is assembled by hand: in mcp 2.1.1 `Server.streamable_http_app(token_verifier=…)` only installs the backend that consults the verifier when a full OAuth `AuthSettings` is also passed.) |
| Auth per call | `server.resolve_auth` re-reads the `Authorization` header on every `tools/call`, re-verifies it, and forwards the request's `client.host` and `Host` to the executor. Auth is resolved **before** argument validation on purpose. |
| Body cap | The app's `MAX_JSON_BODY_BYTES` (256 KB) is **waived for exactly this path** (exact match, only while the door is enabled). A nest PDF arrives base64-encoded *inside* the JSON-RPC envelope, so the SDK's `RequestBodyLimitMiddleware` bounds it at **`WERCO_MCP_MAX_UPLOAD_BYTES`** (default 25 MB) instead — **413** over it, before parsing. |
| Rate limit | **Two tiers.** The door is registered with the app limiter's `exempt` mechanism, so the **default** limit (100/60 s per IP) is waived on `/mcp` and a tool call is charged **once**, on the **inner** route it dispatches — keyed on the MCP caller's IP, exactly like the SPA rather than at half rate; per-path limits on the inner routes (e.g. the standalone laser preview/import 10/min) still apply. The door **path** then carries its own ceiling, `WERCO_MCP_HTTP_RATE_LIMIT` (default 300/minute per IP, enforced by the app's per-path limiter, kept above the default so it never binds a tool call): it bounds the requests that dispatch nothing inward and would otherwise be unmetered — `tools/list` (~0.5 MB of JSON per call), `initialize`, schema-rejected calls and unauthenticated 401 probes. |
| DNS-rebinding guard | The SDK's own guard is off (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`); Host/Origin pinning is the app's job (`TrustedHostMiddleware`, CSRF). |
| Lifespan | The SDK's session manager runs in a task group that must outlive every request, and Starlette never runs a sub-app's lifespan, so `main.py`'s own `lifespan` enters `app.state.mcp_door.lifespan()`. Each entry builds a fresh session manager (the SDK allows one `run()` per instance — that is what lets the test suite re-enter it per `TestClient`). A request to the door before the lifespan is entered gets **503** `MCP door is not running`. |

Deployment notes: [DEPLOYMENT_RUNBOOK.md → Enabling the MCP door](DEPLOYMENT_RUNBOOK.md#enabling-the-mcp-door-optional).

### 3.2 The stdio bridge: `python -m app.mcp`

The bridge speaks MCP on **stdin/stdout** and is what an agent spawns locally. Its mode is decided
by the environment:

| `WERCO_ERP_URL` | Mode | What it needs |
|---|---|---|
| set (`https://<api-host>`) | **REMOTE** — tools go over HTTPS to that deployment | Credentials in `WERCO_ERP_*` (§4). The catalog is still built locally from `app.openapi()`, so `app.main` must import: any *missing* `DATABASE_URL` / `SECRET_KEY` / `REFRESH_TOKEN_SECRET_KEY` / `ENVIRONMENT` / `RATE_LIMIT_ENABLED` is defaulted to a placeholder that is never used for anything (no connection is opened; the remote server signs the tokens). A value already set is never overridden. |
| unset | **IN-PROCESS** — tools dispatch into the local app object | The real environment: a `DATABASE_URL`, the real `SECRET_KEY`. Dev only. |

```bash
cd backend
# Remote bridge against a deployed API (the normal shape)
WERCO_ERP_URL=https://<api-host> WERCO_ERP_EMAIL=<assistant-user@yourshop.com> WERCO_ERP_PASSWORD=<password> \
  .venv311/bin/python -m app.mcp

# Same, with a long-lived API token (the standing credential: never expires unless issued with a
# lifetime, never 401s until an Admin revokes it -- no refresh, no login; §4)
WERCO_ERP_URL=https://<api-host> WERCO_ERP_TOKEN=<api token> .venv311/bin/python -m app.mcp

# Same, with a 15-minute access + refresh token pair (the bridge rotates it on 401)
WERCO_ERP_URL=https://<api-host> WERCO_ERP_TOKEN=<access token> WERCO_ERP_REFRESH_TOKEN=<refresh token> \
  .venv311/bin/python -m app.mcp

# The catalog as JSON — no token, no database, no WERCO_ERP_URL needed
.venv311/bin/python -m app.mcp --print-catalog > catalog.json
```

CLI flags: `--transport stdio|http` (default `stdio`, or `WERCO_MCP_TRANSPORT`), `--host` / `--port`
(HTTP only; defaults `WERCO_MCP_HOST` / `WERCO_MCP_PORT`, else `127.0.0.1:8765`), `--print-catalog`.

**stdout is the wire.** The app's own logging handler writes to `sys.stdout`
(`app/core/logging.py`), which would corrupt the protocol on import; the CLI captures the real
stdout first and re-points `sys.stdout` at stderr *before* importing `app.main`, so everything the
process prints — the app's startup log line included — lands on stderr, and the SDK is handed the
captured wire explicitly. `LOG_LEVEL` controls the stderr logger.

`--transport http` is a **dev-only** local Streamable HTTP server (a bare Starlette app with the
same `McpDoor`). In REMOTE mode its door verifier is a pass-through that only insists a bearer token
is *present* — this process cannot verify tokens a different deployment signed, so the remote
routes authenticate every call; in IN-PROCESS mode it uses the real `ErpTokenVerifier`.

`--print-catalog` prints:

```json
{
 "server": "werco-erp",
 "version": "1.0.0",
 "convenience_tools": [{"name": "...", "description": "...", "annotations": {...}}],
 "generated_tools":   [{"name": "...", "method": "GET", "path": "/api/v1/...", "tag": "...",
                        "function": "...", "annotations": {...}, "deprecated": false}],
 "shadowed_operations": [["GET", "/api/v1/inventory/"], ...],
 "counts": {"convenience": 15, "generated": 662, "shadowed": 16}
}
```

### 3.3 Client configuration

The committed example is `.cursor/mcp.json.example`: copy it to the git-ignored `.cursor/mcp.json`,
fill in the placeholders, and **keep ONE of its two entries** — they expose the same catalog; the
HTTP entry needs the door enabled on the server, the stdio entry does not. The file is exactly
Cursor's `mcpServers` map and nothing else: no comment keys, because Cursor's schema does not define
them and some clients reject unknown keys, so the guidance lives here instead (a test holds the
file to that shape). Placeholders only — never commit a token or a password.

**Cursor / any Streamable-HTTP client (Grok Bot included)** — one URL plus a bearer header. The
server side needs `WERCO_MCP_HTTP_ENABLED=true` (§3.1) and nothing else; where the URL and header
go is the client's business:

```json
{
  "mcpServers": {
    "werco-erp": {
      "url": "https://<your-api-host>/mcp",
      "headers": { "Authorization": "Bearer <ERP_API_TOKEN>" }
    }
  }
}
```

The token is an **API token** — a long-lived, revocable bearer bound to one real user, minted by an
Admin with `POST /api/v1/api-tokens/` ([API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients));
a dedicated user such as *Werco Assistant* is recommended (§4). It never expires unless issued with
a lifetime and never answers 401 until an Admin revokes it, so an HTTP client needs no refresh and
no login. A normal **15-minute** access token works in the same header, but then the client must
renew it itself (`POST /api/v1/auth/refresh`, or log in again) — the stdio bridge below does that
for you; the door does not.

**Claude Code** — either the door, registered from the CLI:

```bash
claude mcp add --transport http werco-erp https://<your-api-host>/mcp \
  --header "Authorization: Bearer <ERP_API_TOKEN>"
```

or the stdio bridge — with the same API token, or with a password login the bridge manages itself
(§4):

```bash
claude mcp add werco-erp \
  -e PYTHONPATH=/absolute/path/to/Werco-ERP-MES/backend \
  -e WERCO_ERP_URL=https://<your-api-host> \
  -e WERCO_ERP_TOKEN=<ERP_API_TOKEN> \
  -- /absolute/path/to/Werco-ERP-MES/backend/.venv311/bin/python -m app.mcp
```

**stdio bridge (Cursor spawns the process; works for any client that launches a command):** the
client runs `python -m app.mcp`, which speaks MCP on stdio and calls the deployed API over HTTPS.

```json
{
  "mcpServers": {
    "werco-erp-stdio": {
      "command": "/absolute/path/to/Werco-ERP-MES/backend/.venv311/bin/python",
      "args": ["-m", "app.mcp"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/Werco-ERP-MES/backend",
        "WERCO_ERP_URL": "https://<your-api-host>",
        "WERCO_ERP_TOKEN": "<ERP_API_TOKEN>"
      }
    }
  }
}
```

Credentials: set `WERCO_ERP_TOKEN` to an **API token** (as above — the standing credential, no
refresh needed), **or** `WERCO_ERP_EMAIL` + `WERCO_ERP_PASSWORD`, **or** a 15-minute access +
refresh token pair `WERCO_ERP_TOKEN` + `WERCO_ERP_REFRESH_TOKEN`; with the last two the bridge
refreshes or re-logs-in on 401 by itself (§4). Leave `WERCO_ERP_URL` unset **only** to dispatch into a local database (dev,
§3.2). `PYTHONPATH` is what lets `python -m app.mcp` import the `app` package from wherever the
client starts the process, so no working directory is assumed: Cursor's documented schema is
`command` / `args` / `env` only, and in remote mode the bridge reads no `.env` file (it sets its own
placeholders, §3.2), so a `cwd` key would add nothing.

The HTTP door and the bridge expose the **same catalog**; pick the HTTP entry when the door is
enabled on the server, the stdio entry when it is not (or when you want to log in with a password
and let the bridge manage the session — §4).

---

## 4. Auth model

**Every call is a real ERP user.** The bearer is one of two user credentials, and nothing else: a
normal access JWT (`type=access`, **15 minutes**, verified by `app.core.security.verify_token` — the
same function the routes use), or a long-lived **API token** (`type=api`, minted by an Admin with
`POST /api/v1/api-tokens/`, bound to one user and anchored to an `api_tokens` row that
`api_token_service.check_api_token` re-reads on every request — not revoked, not past the row's
`expires_at`, claims equal to the row, user active). Either way the route authenticates it exactly
as it would the SPA's request: tenancy comes from the token's active company (for an API token, the
company pinned on its row), `require_role` decides, and every write is audited as that user. The
API token is the credential to give a standing agent: it never expires unless issued with a
lifetime, never 401s until revoked, and is fenced (**403** `"API token cannot access this
resource"`) from every `/auth` and `/api-tokens` route — so it can never refresh, switch company,
or mint, list or revoke tokens, including through the three tools that front those routes
(`create_api_token`, `list_api_tokens`, `revoke_api_token`: Admin only, with an interactive JWT).
Full reference: [API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients).

| Surface | Where the token comes from | On 401 |
|---|---|---|
| HTTP door | The MCP request's `Authorization: Bearer …` header, checked at the door (`ErpTokenVerifier`: an access token by signature, an API token by its row) and again per call. | Passed through as-is. With an **API token**, a 401 means an Admin revoked it (or it reached its issued lifetime, or its user was deactivated) — ask for a new one; nothing renews it. With an access token, **the caller must send a fresh one itself** — refresh with `POST /api/v1/auth/refresh` (`{"refresh_token": …}` → `{access_token, refresh_token, expires_in}`, rotating) or log in again with `POST /api/v1/auth/login` (form-encoded `username` + `password`). |
| stdio bridge | `TokenSource.from_env` — `WERCO_ERP_TOKEN` (a static token: an **API token**, or an access token), `WERCO_ERP_REFRESH_TOKEN`, `WERCO_ERP_EMAIL` + `WERCO_ERP_PASSWORD`. | **Precedence:** the static token is used first; when the ERP answers 401 the bridge tries the refresh token, then an email/password login, and surfaces the 401 when neither is configured or when both fail. An API token never 401s until revoked, so with it alone the rotation path is simply never taken. Rotation is serialised with a lock (`/auth/refresh` rotates the refresh token; two concurrent refreshes would invalidate each other). A refresh token the server rejects (400/401/403) is forgotten so the next attempt goes straight to login. |

Kiosk-scoped badge tokens (`scope == "kiosk"`) are refused at the door and in `resolve_auth`: they
are path-fenced to the shop-floor routes and would 403 on almost every tool. Wallboard display
tokens and station tokens are not user credentials and are refused for the same reason. An API
token is accepted: the door runs its row check (`live_api_token_principal` — a short-lived session
opened for that one read, the only place the package touches the database) and `resolve_auth`
then accepts it by signature, because the dispatched route re-runs the same row check.

**Recommended:** a dedicated user for agent work — e.g. *Werco Assistant*, at the role the agent
needs (the owner's assistant is an **Admin**; a Manager, Supervisor or Viewer narrows what the
agent can do) — holding an **API token** rather than a person's login, so the audit trail says which
writes an agent made, and so the credential can be revoked with a reason, or the role changed,
without touching a person's account. No credentials, tokens or passwords are ever logged; the
bridge logs only *which kinds* of credential are configured (`access-token` — an API token counts
as one, `refresh-token`, `password-login`).

`POST /auth/login` is rate-limited at 5/min and `/auth/refresh` at 30/min per IP
([ENVIRONMENT_VARIABLES.md → Rate Limiting](ENVIRONMENT_VARIABLES.md#rate-limiting)); a bridge that
re-logs-in on every call would hit the login limit, which is one reason the refresh path exists.

---

## 5. Program map — every SPA page → OpenAPI tag(s) → tools

`★` marks a **convenience** tool (fixed name, §7); everything else is **generated** (§6). Tools are
representative, not exhaustive: run `--print-catalog` and filter by `tag` for the full set. Source
pages are `frontend/src/pages/*.tsx`; routes are from `frontend/src/App.tsx`.

| SPA page (route) | OpenAPI tag(s) | Representative tools |
|---|---|---|
| `Dashboard` (`/`) | Shop Floor, Scheduling, Calibration, Inventory, Quality Management | `get_shop_floor_dashboard`★, `get_scheduled_jobs`, `get_equipment_due_soon`, `get_low_stock_alerts`, `get_quality_summary` |
| `ActionInbox` (`/action-inbox`) | AI Learning, Setup & Readiness | `list_ai_recommendations`, `accept_ai_recommendation`, `dismiss_ai_recommendation`, `snooze_ai_recommendation`, `get_setup_health` |
| `Notifications` (`/notifications`) | Notifications | `list_notifications`, `get_unread_count`, `mark_notification_read`, `mark_all_read` |
| `MySettings` (`/settings`) | Users (self-service) | `get_current_user_info`, `get_my_notification_preferences`, `update_my_notification_preferences`, `update_my_phone`, `send_test_sms`, `change_own_password` |
| `SetupWizard` (`/setup`) | Setup & Readiness | `get_setup_health`, `get_part_readiness` |
| `WorkOrders` (`/work-orders`) | Work Orders, Work Order Templates | `work_orders_list_work_orders`, `get_work_order_by_number`, `update_work_order_priority`, `work_orders_delete_work_order`, `work_orders_restore_work_order`, `list_work_order_templates`, `use_work_order_template` |
| `WorkOrderNew` (`/work-orders/new`) | Work Orders, Parts, Bill of Materials, Routing, Customers, Setup & Readiness, Work Centers | `create_work_order`★, `preview_work_order_operations`, `get_routing_by_part`, `list_customer_names`, `get_part_readiness`, `list_work_centers`★ |
| `WorkOrderDetail` (`/work-orders/:id`) | Work Orders, Work Order Materials, Laser Nests, Work Order Blockers, Documents, Shop Floor | `get_work_order`★, `add_operation`★, `update_work_order`★, `release_work_order`★, `duplicate_work_order`★, `add_laser_nest`★, `work_orders_start_work_order`, `work_orders_complete_work_order`, `work_orders_complete_operation`, `work_orders_update_operation`, `update_laser_nest`, `delete_laser_nest`, `list_material_allocations`, `create_work_order_blocker`, `attach_document_to_work_order` |
| `ShopFloor` (`/shop-floor`) | Shop Floor, Work Order Blockers, Work Centers, Work Orders | `get_shop_floor_dashboard`★, `get_all_operations`, `get_active_shop_users`, `list_work_order_blockers`, `resolve_work_order_blocker` |
| `ShopFloorSimple` (`/shop-floor/operations`) | Shop Floor, Scanner, Work Centers, Work Orders | `get_all_operations`, `shop_floor_start_operation`, `shop_floor_complete_operation`, `put_operation_on_hold`, `shop_floor_resume_operation`, `lookup_barcode`, `resolve_action` |
| `OperatorKiosk` (`/kiosk`) | Shop Floor | `clock_in`, `clock_out`, `get_my_active_job`, `get_work_center_queue`, `get_operation_steps`, `record_operation_step`, `report_operation_production`, `shop_floor_complete_operation` — under the caller's own user token (kiosk badge tokens are refused) |
| `CrewStationKiosk` (`/kiosk?station=`) | Shop Floor | Same routes as above; station administration is `create_kiosk_station`, `list_kiosk_stations`, `revoke_kiosk_station`, `reset_kiosk_station_pin`. The station PIN login itself is unauthenticated plumbing and is not a tool |
| `DispatchBoard` (`/dispatch`) | Shop Floor, Work Orders | `get_dispatch_board`, `get_work_center_queue`, `set_work_center_run_order`, `update_work_order_priority` |
| `Wallboard` (`/wallboard`) | Shop Floor | `shop_floor_wallboard` (the TV reads it with a display token; through MCP it runs under the caller's user token) |
| `TvPair` (`/tv`) | Authentication (display tokens) | *No tool* — the `Authentication` tag is excluded on purpose; issue/revoke display tokens from the UI (`WALLBOARD.md`) |
| `WorkCenters` (`/work-centers`) | Work Centers | `list_work_centers`★, `create_work_center`, `update_work_center`, `update_work_center_status`, `delete_work_center`, `list_work_center_types` |
| `Scheduling` (`/scheduling`) | Scheduling, Work Centers, Work Orders | `get_scheduled_jobs`, `schedule_work_order`, `schedule_work_order_earliest`, `run_scheduling`, `get_capacity_heatmap`, `get_scheduling_conflicts`, `unschedule_work_order` |
| `Maintenance` (`/maintenance`) | Preventive Maintenance | `list_schedules`, `create_schedule`, `preventive_maintenance_list_work_orders`, `preventive_maintenance_create_work_order`, `preventive_maintenance_complete_work_order`, `get_calendar` |
| `DowntimeTracking` (`/downtime`) | Downtime Tracking, Work Centers | `list_downtime_events`, `create_downtime_event`, `resolve_downtime_event`, `get_downtime_summary`, `list_reason_codes` |
| `OEE` (`/oee`) | OEE Tracking, Work Centers | `get_oee_dashboard`, `list_oee_records`, `get_oee_trends`, `auto_calculate_oee`, `get_six_big_losses` |
| `JobCosting` (`/job-costing`) | Job Costing, Work Orders | `list_job_costs`, `get_job_cost`, `get_summary`, `calculate_costs`, `variance_report` |
| `PartsNew` (`/parts`) | Parts, Bill of Materials, Customers, Documents | `list_parts`★, `create_part`, `generate_part_number`, `create_new_revision`, `import_parts_csv` |
| `PartDetail` (`/parts/:id`) | Parts, Bill of Materials, Routing, Setup & Readiness | `get_part`, `get_part_by_number`, `get_bom_by_part`, `get_routing_by_part`, `get_part_readiness`, `get_part_backflush_readiness` |
| `PartEdit` (`/parts/:id/edit`) | Parts | `get_part`, `update_part`, `renumber_part`, `deactivate_part`, `activate_part` |
| `BOM` (`/bom`) | Bill of Materials, Parts, Materials & Supplies | `list_boms`, `get_bom`, `create_bom`, `add_bom_item`, `release_bom`, `explode_bom`, `where_used` |
| `BOMUomMismatches` (`/bom/uom-mismatches`) | Bill of Materials | `list_bom_uom_mismatches` |
| `Routing` (`/routing`) | Routing, Parts, Process Sheets, Work Centers | `list_routings`, `create_routing`, `routing_add_operation`, `routing_reorder_operations`, `release_routing`, `generate_routing_from_drawing` |
| `ProcessSheets` (`/process-sheets`) | Process Sheets | `list_process_sheets`, `create_process_sheet`, `add_process_sheet_step`, `release_process_sheet`, `new_process_sheet_revision`, `obsolete_process_sheet` |
| `EngineeringChanges` (`/engineering-changes`) | Engineering Change Orders | `list_ecos`, `create_eco`, `submit_eco`, `approve_eco`, `get_eco_dashboard`, `get_affected_items` |
| `Documents` (`/documents`) | Documents, Parts | `list_documents`, `upload_document`, `download_document`, `list_document_types`, `delete_document` |
| `CustomFields` (`/custom-fields`) | Custom Fields | `list_field_definitions`, `create_field_definition`, `set_custom_field_value`, `get_entity_custom_fields` |
| `Warehouse` (`/warehouse`) | — hub that mounts the Inventory, Receiving and Shipping pages | see those three rows |
| `Inventory` (`/inventory`) | Inventory, Parts | `list_inventory`★, `receive_inventory`, `adjust_inventory`, `transfer_inventory`, `list_transactions`, `preview_inventory_combine`, `combine_inventory`, `list_cycle_counts` |
| `Materials` (`/materials`) | Materials & Supplies | `materials_supplies_list_materials`, `materials_supplies_get_material`, `materials_supplies_create_material`, `materials_supplies_update_material`, `import_materials_csv` |
| `Receiving` (`/receiving`) | Receiving & Inspection, Purchasing | `get_open_purchase_orders`, `receive_material`, `get_inspection_queue`, `inspect_receipt`, `correct_receipt`, `void_receipt`, `print_receiving_label` |
| `Shipping` (`/shipping`) | Shipping | `get_ready_to_ship`, `create_shipment`, `mark_shipped`, `rate_shop`, `buy_label`, `get_tracking`, `issue_certificate_of_conformance` |
| `MRP` (`/mrp`) | Material Requirements Planning | `create_mrp_run`, `get_latest_mrp_run`, `get_mrp_actions`, `get_current_shortages`, `process_mrp_action` |
| `Purchasing` (`/purchasing`) | Purchasing, Parts, Documents | `list_purchase_orders`★, `get_purchase_order`, `create_purchase_order`, `add_po_line`, `send_purchase_order`, `list_vendors`, `create_vendor`, `restore_vendor` |
| `POUpload` (`/po-upload`) | PO Upload | `upload_and_extract_po`, `upload_and_extract_quote`, `upload_and_extract_invoice`, `create_po_from_upload`, `search_vendors` |
| `SupplierScorecards` (`/supplier-scorecards`) | Supplier Scorecards | `list_scorecards`, `scorecard_dashboard`, `auto_calculate_scorecard`, `create_audit`, `list_approved_suppliers` |
| `Traceability` (`/traceability`) | Traceability | `search_lots`, `trace_lot`, `trace_serial` |
| `Quotes` (`/quotes`) | Quotes, Parts | `list_quotes`, `get_quote`, `create_quote`, `add_quote_line`, `send_quote`, `generate_quote_pdf`, `convert_to_work_order` |
| `RFQQuoting` (`/rfq-packages/new`) | AI RFQ Quotes, Quotes, Customers | `create_rfq_package`, `get_rfq_package`, `generate_estimate`, `approve_estimate`, `export_internal_estimate` |
| `QuoteCalculator` (`/quote-calculator`) | Quote Calculator, DXF Parser | `calculate_sheet_metal_quote`, `calculate_cnc_quote`, `quote_calculator_list_materials`, `analyze_dxf`, `preview_dxf` |
| `EstimateWorkbench` (`/estimate-workbench`) | Estimate Workbench | `create_workbench`, `get_workbench`, `extract_from_rfq`, `recalc_estimate`, `finalize_workbench`, `export_customer_pdf` |
| `ShopData` (`/shop-data`) | Estimate Workbench | `get_shop_data`, `post_shop_data_row`, `patch_shop_data_row`, `get_shop_data_history` |
| `Customers` (`/customers`) | Customers | `list_customers`, `create_customer`, `update_customer`, `get_customer_stats`, `import_customers_csv` |
| `Quality` (`/quality`) | Quality Management, Parts | `list_quality_ncrs`★, `get_ncr`, `create_ncr`, `update_ncr`, `void_ncr`, `list_cars`, `create_fai`, `get_quality_summary`, `list_scrap_reason_codes` |
| `SPC` (`/spc`) | Statistical Process Control, Parts | `list_characteristics`, `add_measurements`, `get_chart_data`, `calculate_control_limits`, `get_capability`, `statistical_process_control_get_dashboard` |
| `CustomerComplaints` (`/customer-complaints`) | Customer Complaints & RMA | `list_complaints`, `create_complaint`, `create_rma`, `get_8d_report`, `customer_complaints_rma_get_dashboard` |
| `Calibration` (`/calibration`) | Calibration | `list_equipment`, `get_equipment_due_soon`, `record_calibration`, `get_calibration_history` |
| `OperatorCertifications` (`/certifications`) | Operator Certifications | `list_certifications`, `certification_dashboard`, `get_expiring_certifications`, `list_skill_matrix`, `check_operator_qualification` |
| `ToolManagement` (`/tool-management`) | Tool & Fixture Management | `list_tools`, `checkout_tool`, `checkin_tool`, `list_inspection_due`, `tool_fixture_management_get_dashboard` |
| `QMSStandards` (`/qms-standards`) | QMS Standards & Audit Readiness | `list_standards`, `get_audit_readiness`, `list_clauses`, `add_evidence`, `upload_pdf_and_extract_clauses` |
| `Analytics` (`/analytics/*`) | Analytics & BI | `get_kpi_dashboard`, `get_production_trends`, `analytics_bi_get_quality_metrics`, `get_flow_metrics`, `get_fpy`, `get_wip_aging`, `run_custom_report` |
| `Reports` (`/reports`) | Reports | `get_production_summary`, `get_work_center_utilization`, `get_inventory_value`, `get_ship_otd_report`, `reports_get_quality_metrics`, `get_employee_time_report` |
| `PrintTraveler` (`/print/traveler/:id`) | Work Orders, Print Reports, Parts | `get_work_order`★, `get_work_order_print_data`, `get_part` |
| `PrintPurchaseOrder` (`/print/purchase-order/:id`) | Print Reports | `get_purchase_order_print_data` |
| `PrintPackingSlip` (`/print/packing-slip/:id`) | Shipping | `get_shipment` |
| `PrintShippingLabel` (`/print/shipping-label/:id`) | Shipping, Documents | `get_shipment`, `download_document` |
| `PrintBadges` (`/print/badges`) | Users | `list_users` |
| `ImportCenter` (`/import-center`) | Import Kit, Purchasing, Users, Work Orders | `get_import_templates`, `download_import_template`, `import_users_csv` — the two Excel-cutover loaders (`POST /work-orders/import`, `POST /purchasing/purchase-orders/import`) are deliberately **not** tools (§6.1) |
| `Users` (`/users`) | Users | `list_users`, `create_user`, `update_user`, `deactivate_user`, `reset_user_password`, `approve_user`, `unlock_user` |
| `AuditLog` (`/audit-log`) | Audit | `list_audit_logs`, `get_audit_summary`, `verify_audit_integrity`, `get_integrity_status` |
| `VisitorLog` (`/visitor-log`) | Visitor Logs | `list_visitors`, `manual_entry`, `sign_out`, `export_visitors_csv`, `create_station`, `revoke_station` |
| `VisitorSignIn` (`/visitor-signin`) | Visitor Logs | `sign_in`, `sign_out` (under the caller's own role; the tablet's shared-PIN station login is unauthenticated plumbing and is not a tool) |
| `AdminSettings` (`/admin/settings`) | Admin Settings, Users, Quote Calculator | `list_labor_rates`, `update_overhead_setting`, `get_role_permissions`, `update_role_permissions`, `admin_settings_list_machines`, `admin_settings_update_machine`, `get_audit_log`, `list_users` |
| `PlatformOverview` (`/platform`) | Platform Administration | `platform_overview`, `list_companies`, `company_dashboard`, `browse_company_users` (platform admins only — the route decides) |
| `CompanyRegister` (`/register-company`) | Company Management | `get_my_company`, `update_my_company`, `update_my_company_ai_egress`, `update_my_company_sms_egress` — the public registration form itself is unauthenticated and has no tool |
| *(no page — Admin tooling, `curl` or these tools)* | API Tokens | `create_api_token`, `list_api_tokens`, `revoke_api_token` (Admin only; a call authenticated **by** an API token gets the route's 403 fence — §4) |
| `Login` (`/login`), `Register` (`/register`) | Authentication | *No tool* — excluded on purpose; identity comes from the JWT (§4) |
| `NotFound`, `Unauthorized` | — | No API calls |

Coverage: 73 page files; 68 map to at least one catalog tool. The five that do not are the three
`Authentication`-only pages (excluded by design) and the two chrome pages with no API.

---

## 6. The generated layer

### 6.1 Which operations become tools

Every operation in `app.openapi()` that **declares `security`** and carries none of the
`EXCLUDED_TAGS` — `Authentication`, `Carrier Webhooks`, `Error Logging`, its router-level twin
`errors`, and `WebSocket` (listed for completeness; WebSocket routes never appear in OpenAPI) —
minus two further, named sets:

- **`PUBLIC_OPERATIONS`** — the 17 routes with no `security` block (station logins,
  `register-public`, `reset-database`, the credential exchanges, the carrier webhook, the browser
  error beacon, the four untagged `/health*` probes). None is a user action and none is a tool.
  They are listed **by name** rather than skipped silently, so a new route that forgets its
  `security` dependency — even under a tag that already has tools — fails the coverage guard
  (`catalog.unaccounted_operations`, §14) until it is either secured or named there.
- **`EXCLUDED_OPERATIONS`** — the two Excel-cutover loaders, `POST /work-orders/import` and
  `POST /purchasing/purchase-orders/import` (`migration_import_service`). They record a *past*
  system's state rather than perform a user action: the work-order loader mints work orders
  RELEASED / IN_PROGRESS with operations READY / COMPLETE on no labor evidence, the PO loader
  mints purchase orders SENT — exactly the side door around "create lands DRAFT, release is
  explicit" (§9) and "never fake shop-floor time" that the convenience tools exist to close.
  They stay an Import Center action for an admin at cutover (`EXCEL_MIGRATION_RUNBOOK.md`).

On the commit that shipped API tokens: 706 operations, 689 secured, 680 candidates, 16 shadowed
(§8), 2 excluded → **662 generated tools**.

### 6.2 Naming and the collision policy (`app/mcp/naming.py`)

1. **Function name** = the operationId prefix before `_api_v1_`
   (`create_work_order_api_v1_work_orders__post` → `create_work_order`); a trailing `_endpoint`
   is dropped (`create_manual_laser_nest_endpoint` → `create_manual_laser_nest`).
2. **Unique across the catalog → used bare.**
3. **Collision → every member gets `<tag_slug>_<function_name>`**; none keeps the bare name, so a
   bare name never silently points at whichever router sorted first. Tag slug = lowercase,
   non-alphanumerics → `_`, collapsed (`Shop Floor` → `shop_floor`, `Customer Complaints & RMA` →
   `customer_complaints_rma`). Real examples: `work_orders_start_operation` / `shop_floor_start_operation`;
   `preventive_maintenance_get_dashboard` / `customer_complaints_rma_get_dashboard` /
   `statistical_process_control_get_dashboard` / `tool_fixture_management_get_dashboard`;
   `materials_supplies_list_materials` / `admin_settings_list_materials` / `quote_calculator_list_materials`;
   `reports_get_quality_metrics` / `analytics_bi_get_quality_metrics`.
4. **The prefix propagates to the family.** Every function name has a *noun phrase*: the tokens
   after the first (verb) token, with the last token singularized — `ies` → `y`; `es` dropped when
   the stem ends in `sh` / `ch` / `x` / `s` / `z`, so `list_finishes` → `finish`; otherwise a
   trailing `s` dropped, so `list_machines` → `machine` and `delete_work_order` → `work_order`; a
   token ending in `ss` is left alone. Within one tag, the noun phrases of the collision-prefixed
   functions form a set, and **every other function in that tag whose noun phrase is in the set is
   prefixed with the tag slug too**, so a resource family reads as one family rather than two
   prefixed and two bare members. Real examples: Admin Settings' `create_machine` / `list_machines`
   collide with Quote Calculator's, so `admin_settings_update_machine` and
   `admin_settings_delete_machine` are prefixed with them (and `admin_settings_update_finish` /
   `admin_settings_delete_finish` beside `admin_settings_list_finishes`);
   `materials_supplies_get_material` beside `materials_supplies_list_materials`;
   `routing_delete_operation` / `routing_reorder_operations` beside `routing_add_operation`;
   `shop_floor_resume_operation` beside `shop_floor_start_operation`;
   `work_orders_delete_work_order` / `work_orders_restore_work_order` beside
   `work_orders_start_work_order`. A **reserved** name (rule 5) does not seed propagation: every
   generated function that carries a convenience name is shadowed by that tool, so there is no
   visible split to repair, and seeding from it would prefix every `*_part`, `*_inventory` and
   `*_work_center` tool. Propagation is a single pass — a reached member's noun phrase is already
   in the set, so it can never widen it. Singularization is deliberately dumb and only has to be
   consistent within a family; its one known miss is `clauses` → `claus` against `clause`, which
   can only ever cost a propagation, never invent one.
5. **Convenience names are reserved.** A lone generated function named `search` would surface as
   `<tag>_search` rather than fight the convenience tool for the name.
6. **Names are assigned over the full secured set *before* shadowing.** Shadowing the Work Orders
   `add_operation` / `get_work_order` / `create_work_order` does not hand the bare name to their
   twins: they stay `routing_add_operation`, `preventive_maintenance_get_work_order`,
   `preventive_maintenance_create_work_order`. A generated name is therefore the same on every
   transport and regardless of what is shadowed.
7. **≤ 64 characters**, matching `^[a-zA-Z0-9_-]{1,64}$` (SDK-enforced on the wire): the tag slug
   is trimmed first, then the name. Longest name today: `preventive_maintenance_complete_work_order`
   (42). The policy is **total**: two members colliding under one tag, or a truncation that folds two
   names together, get a deterministic numeric suffix (`_2`, `_3`, … in sorted-key order) rather
   than raising — the catalog is built at import of `app.main`, and a naming nicety must never keep
   the API from booting. No live name needs the suffix today; `tests/test_mcp_catalog.py` pins that,
   so a suffixed name is noticed in CI rather than in a prompt.

**The consequence to remember:** a generated name can **shift** the day a collision appears — and
not only its own. `list_work_orders` is `work_orders_list_work_orders` today only because the
maintenance router also defines a `list_work_orders`; and `delete_work_order` is
`work_orders_delete_work_order` although nobody else defines a `delete_work_order` — its *siblings*
collide, and the family rule carries the prefix across. Delete the maintenance twins and both would
surface bare again. Convenience tools (§7) have fixed names for exactly that reason; anchor prompts
on them, and re-run `--print-catalog` after adding a router. `backend/tests/test_mcp_docs_names.py`
holds this file, `CLAUDE.md` and `API.md` to the live catalog, so a shifted name turns CI red rather
than leaving a stale runbook.

### 6.3 Input schema

One JSON object per tool, `$ref`s fully inlined (MCP clients receive a self-contained
`inputSchema`; cycles and depth > 12 degrade to a described stub rather than failing the catalog).
Pydantic's auto-`title` **keyword** is stripped; a body **field** named `title` (NCRs, CARs, ECOs,
complaints, process sheets, QMS clauses and evidence, maintenance work orders, AI recommendations,
document uploads — 17 routes, 10 of which require it) is kept, and `default` / `example` / `enum` /
`const` values are copied as opaque data. `description`, `enum`, `default`, `format`, `minimum`
etc. are kept. Every schema is **`additionalProperties: false`**: a misspelled argument
(`serach`) is a 422 that names it, never a silently dropped filter that returns the *unfiltered*
result with `isError: false`.

| OpenAPI | Tool argument |
|---|---|
| Path parameter | Required top-level property |
| Query parameter | Top-level property (required per spec) |
| JSON body that is an object | Its properties **merged at the top level**; the executor splits them back out. If a body field shares a name with a path/query parameter it is renamed `body_<x>` (no route needs this today) |
| JSON body that is not an object (a bare array, a free-form dict, an optional model) | A single `body` property sent as-is, **described with the body schema's own title** so a free-form dict says what it is: `The request body (Cutting Speeds), sent as-is.` on `quote_calculator_create_machine`, `Sheet Pricing` on `quote_calculator_create_material`, `Operation Order` on `routing_reorder_operations`, `Permissions` on `update_role_permissions` |
| Multipart / form | Form fields as properties; each file field is an object `{"filename": str, "content_base64": str, "content_type"?: str}` (a list of them for multi-file fields). 23 tools take files (`upload_document`, `import_parts_csv`, `create_rfq_package`, `analyze_dxf`, …). FastAPI 0.136 emits `{"type": "string", "contentMediaType": "application/octet-stream"}` for an `UploadFile`; the older `format: binary` is recognised too |
| Header / cookie parameter | Dropped — transport concerns the executor never forwards (the API has exactly one: `if-none-match` on `GET /shop-floor/dashboard`) |

Arguments are validated against that schema with `jsonschema` before dispatch; a failure is a
422-shaped `is_error` result listing up to 10 problems as `"<tool>: <location>: <message>"` plus a hint.

### 6.4 Description and annotations

Description: `"<METHOD> <path> — <summary>. <first docstring paragraph> [<Tag>]"`, capped at ~600
characters, `(DEPRECATED)` appended when the route is deprecated (one today: `issue_inventory`).
Because the docstring feeds the tool description, **route docstrings are now agent-facing text** —
keep their first paragraph accurate.

Annotations (`ToolAnnotations`): `read_only_hint` for `GET`; `destructive_hint` for `DELETE` or a
function name containing `delete`/`void`/`cancel`/`purge`/`reset`/`hard`; `idempotent_hint` for
`GET`/`PUT`/`DELETE`; `open_world_hint` always `false`.

---

## 7. The convenience tools (fixed names)

Fifteen hand-written tools, listed **first** in `tools/list` (then the generated catalog sorted by
name). Each one reaches data exactly like a generated tool — an HTTP request through the executor
as the caller — and each **shadows** the raw route(s) it fronts (§8), so there is exactly one door
to those routes and the rules below cannot be bypassed by calling the raw twin. Every convenience
schema is `additionalProperties: false` (§6.3): an argument the table does not list is refused
with a 422 that names it, never accepted-and-ignored.

| Tool | Route(s) | Arguments | Behaviour |
|---|---|---|---|
| `search` | `GET /search/` | `q` (required, 1–100 chars), `limit` (1–50, default 20), `types` (comma-separated) | Global search |
| `list_work_centers` | `GET /work-centers/` | `name` (case-insensitive substring over name **and** code), `active_only` (default `true`) | Fetches with `limit=5000` and filters client-side; returns `id`, `code`, `name`, `type`, `is_active` |
| `create_work_order` | `POST /work-orders/` | `part_id`, `quantity_ordered` (required); `work_order_type`, `parent_work_order_id`, `priority`, `due_date`, `customer_name`, `customer_po`, `unit_number`, `notes`, `special_instructions`, `sequential_operations`, `serial_numbers`, `auto_routing` (query, default `true`). **No `status` argument** — passing one is a 422 | See [DRAFT guarantees](#9-draft-guarantees). Refuses `work_order_type: "laser_cutting"` **before** calling, with the API's own wording |
| `add_operation` | `POST /work-orders/{id}/operations` | `work_order_id`, `name`, `work_center` (required; **int id or name/code string**); `sequence` (10–990, multiple of 10); passthrough `operation_number`, `description`, `setup_instructions`, `run_instructions`, `setup_time_hours`, `run_time_hours`, `run_time_per_piece`, `requires_inspection`, `inspection_type`, `component_part_id`, `component_quantity`, `operation_group`. `quantity` is accepted by the schema only to be **refused 422 with an explanation** | **Name guard** (below). Work center by name: exact match on name or code → substring → refuses **404** on none, **409** naming the candidates on more than one ("Pass the id"). `sequence` omitted → `GET /work-orders/{id}` and use the next multiple of 10 after the current maximum (10 on an empty plan). **An operation has no quantity of its own** — it runs the work order's `quantity_ordered`; `component_quantity` is the BOM component demand for the step (the per-operation piece target on a batch job), not an operation quantity, so the spec's `quantity` is not aliased to it. Not for laser WOs (the route refuses 400) |
| `get_work_order` | `GET /work-orders/{id}` or `/work-orders/by-number/{n}` | `work_order_id` **or** `work_order_number` (percent-encoded into the path, so `../` or `?` cannot steer the call onto another route) | Full `WorkOrderResponse`: operations, nests (`operations[].laser_nest`), status, `version` |
| `update_work_order` | `PUT /work-orders/{id}` | `work_order_id`, `version` (required); `quantity_ordered`, `priority`, `status`, `sequential_operations`, `due_date`, `customer_name`, `customer_po`, `unit_number`, `notes`, `special_instructions`, `quantity_complete`, `quantity_scrapped`, `scrap_reason` — every `WorkOrderUpdate` field, pinned to the route's own schema by a catalog test | The header edit verb, version-gated (409 when stale). **`status` accepts `draft` / `on_hold` / `cancelled` only**: `released` and `in_progress` are refused **422** naming the real verbs (`release_work_order`, `work_orders_start_work_order`) — the route's blind `setattr` would accept them with no `released_at` / `released_by` and no operation promotion; `complete` / `closed` are refused by the route itself. `status: "draft"` is how a released-but-unstarted job is put back (the nest tools use the same PUT) |
| `duplicate_work_order` | `POST /work-orders/{id}/duplicate` | `work_order_id`, `quantity_ordered` (both required — the route requires the quantity; a laser job ignores it and derives its own from the copied nests), `due_date` | Copies the *plan* onto a new DRAFT (`WORK_ORDER_TEMPLATES.md` / duplicate service); post-checks DRAFT |
| `release_work_order` | `POST /work-orders/{id}/release` | `work_order_id` | Explicit only; the route refuses anything not DRAFT. The **only** tool that releases work |
| `import_laser_nest_package` | `POST /work-orders/laser-nest-packages/standalone/import` or `POST /work-orders/{id}/laser-nest-packages/import` | `work_order_id` (omit for standalone), `file` `{filename, content_base64, content_type?}` **or** `source_path`, `rows` (list → sent as the JSON string the route expects, or a string), `work_center_id`, `due_date` (standalone only), `sheet_match_provenance`, `release` (default `false`) | See [Nest import demotion](#10-nest-import-demotion). Returns `{import, work_order, demoted_to_draft, operations_returned_to_pending, note}` |
| `add_laser_nest` | `POST /work-orders/{id}/laser-nests/manual` | `work_order_id`, `cnc_number`, `planned_runs` (required); `nest_name`, `material`, `thickness`, `sheet_size`, `work_center_id`, `material_part_id`, `qty_per_run` (the route's `LaserNestManualCreate`, pinned by a catalog test); `release` (default `false`) | Keys one nest by hand onto an assembly parent (its laser child is found-or-created by the route) or a laser WO. The route **force-sets the laser WO RELEASED** with the new nest READY; the tool applies §10 to it — a **new** laser WO, or one that was **DRAFT before the call**, comes back DRAFT with PENDING operations; a job already on the floor (released / in progress / on hold before the call) is **never taken off it** (`demoted_to_draft: false` + a `note`). Refuses **409** a job whose nests came from a package import (§10 rule 1, the other direction). Returns `{nest, work_order, demoted_to_draft, ...}` |
| `get_shop_floor_dashboard` | `GET /shop-floor/dashboard` (+ `GET /shop-floor/dispatch-board`) | `include_dispatch_board` (default `false`) | With the flag: `{"dashboard": …, "dispatch_board": …}` |
| `list_inventory` | `GET /inventory/` or `GET /inventory/summary` | `summary` (default `false`), `part_id`, `warehouse`, `location_code`, `has_quantity` (default `true`), `limit` (default 200, max 10000), `offset` | `summary=true` → per-part totals |
| `list_purchase_orders` | `GET /purchasing/purchase-orders` | `status`, `vendor_id`, `deleted_only`, `limit` (default 200), `offset` | |
| `list_quality_ncrs` | `GET /quality/ncr` | `status`, `part_id`, `skip`, `limit` (default 100) | |
| `list_parts` | `GET /parts/` | `search`, `part_type`, `item_group` (default `engineering`), `active_only`, `include_bom_components`, `include_deleted` (admin), `skip`, `limit` (default 100, max 500) | |

**Operation-name guard (`add_operation`).** An operation is a short shop-floor step (`Laser`,
`Brake`, `Weld`, `Deburr`), never a file. A name is refused — **422, not rewritten** — when it
contains `/` or `\`, ends with `.dxf` / `.dwg` / `.nc` / `.pdf` (case-insensitive), or equals
`Part Detail` case-insensitively (a DXF export label). `operation_number` is the bare identifier
(`"10"`, never `Op 10` — `CLAUDE.md` → Conventions).

---

## 8. Shadowed raw operations

These 16 `(METHOD, path)` pairs are **dropped from the generated catalog**; the convenience tool is
the only door to them (`convenience.SHADOWED_OPERATIONS`):

| Raw operation | Replaced by |
|---|---|
| `POST /api/v1/work-orders/` | `create_work_order` |
| `PUT /api/v1/work-orders/{work_order_id}` | `update_work_order` |
| `POST /api/v1/work-orders/{work_order_id}/duplicate` | `duplicate_work_order` |
| `POST /api/v1/work-orders/{work_order_id}/release` | `release_work_order` |
| `POST /api/v1/work-orders/{work_order_id}/operations` | `add_operation` |
| `POST /api/v1/work-orders/laser-nest-packages/standalone/import` | `import_laser_nest_package` |
| `POST /api/v1/work-orders/{work_order_id}/laser-nest-packages/import` | `import_laser_nest_package` |
| `POST /api/v1/work-orders/{work_order_id}/laser-nests/manual` | `add_laser_nest` |
| `GET /api/v1/search/` | `search` |
| `GET /api/v1/work-centers/` | `list_work_centers` |
| `GET /api/v1/work-orders/{work_order_id}` | `get_work_order` |
| `GET /api/v1/shop-floor/dashboard` | `get_shop_floor_dashboard` |
| `GET /api/v1/inventory/` | `list_inventory` |
| `GET /api/v1/purchasing/purchase-orders` | `list_purchase_orders` |
| `GET /api/v1/quality/ncr` | `list_quality_ncrs` |
| `GET /api/v1/parts/` | `list_parts` |

Every other operation stays generated — including the neighbours of the shadowed ones
(`work_orders_start_work_order`, `work_orders_update_operation`, `preview_laser_nest_package_import`,
`get_work_order_by_number`, `update_laser_nest`, `get_inventory_summary`, `get_purchase_order`, …) —
except the two cutover loaders in `EXCLUDED_OPERATIONS` (§6.1), which have no tool and no
replacement. Adding a convenience tool whose route is *not* in the shadow set is a build error
(`build_registry` refuses a duplicate name) rather than a silent win.

---

## 9. DRAFT guarantees

- **`create_work_order` always lands DRAFT.** The route has no `status` input — neither
  `WorkOrderBase` nor `WorkOrderCreate` declares one; the model column defaults `DRAFT` and pydantic
  drops an extra `status` key — so the tool cannot "force" anything. It **forwards only the
  listed fields** (a `status` or `operations` argument is refused 422 by the strict schema before
  the handler runs; the allowlist is a second fence — operations are added one at a time through
  `add_operation` so each name passes the guard), and **post-checks** the response: a work order that came back in
  any status but `draft` is reported as a loud `is_error` (status 500) carrying the created work
  order in `work_order`. Unreachable against today's route; kept so a future route change cannot
  silently break the rule.
- **`duplicate_work_order` lands DRAFT** (the duplicate service builds a DRAFT; the tool post-checks
  `work_order.status` the same way and reports the payload under `duplicate` if not).
- **`release_work_order` is the only release.** Nothing else in the package moves a work order to
  RELEASED — not creation, not duplication, not template use (`use_work_order_template` is a
  generated tool over a route that itself lands DRAFT), and not the header update: the generic
  `PUT /work-orders/{id}` is shadowed by `update_work_order`, which refuses `status: released` and
  `status: in_progress` (422 naming `release_work_order` / `work_orders_start_work_order`) because
  the route's blind `setattr` would accept them with no `released_at` / `released_by` and no
  operation promotion — a release the audit trail could not attribute. The two Excel-cutover
  loaders that mint work orders RELEASED / IN_PROGRESS outright are not tools (§6.1). The nest
  routes are the ones *born* RELEASED, and §10 is how the tools handle them.

---

## 10. Nest import demotion

The application's nest-package import creates the laser work order **RELEASED, with its nest
operations READY** — that is what the planners' import button means — and the manual-nest route
force-sets the laser work order it lands on the same way. `import_laser_nest_package` and
`add_laser_nest` keep those routes and add four rules:

1. **Manual nests and a package import are never mixed on one job — in either direction, and the
   check reads the LASER work order.** A production parent carries no nests of its own: they live
   on its laser child, and `WorkOrderResponse` has no pointer to that child, so with a
   `work_order_id` the tool `GET`s the target and, for a production parent, finds the live
   `laser_cutting` child through the work-order list (`parent_work_order_id` = the parent, narrowed
   by the parent's part number) before looking at `operations[].laser_nest`. A nest with
   `cnc_file_name` **null** was keyed by hand. `import_laser_nest_package` refuses **409** naming
   the manual nests when any exist ("A package import REPLACES every nest on the job … Import into
   a fresh standalone laser work order instead"); `add_laser_nest` refuses **409** when the job's
   nests came from a package import (`cnc_file_name` set), because the next import would replace
   the hand-keyed nest silently. Both refusals run before anything is uploaded or written.
2. **Demote to DRAFT after a successful import, unless `release=true` — header AND operations.**
   The route's response carries `child_work_order` (a full `WorkOrderResponse`, RELEASED,
   `work_order_type='laser_cutting'`, operations READY). The tool issues
   `PUT /work-orders/{child.id}` `{"version": child.version, "status": "draft"}` — the ordinary,
   audited, version-gated update verb — and **then** `PUT /work-orders/operations/{id}`
   `{"version", "status": "pending"}` for every READY operation, in that order: the app's
   reconcile-on-read promotes the PENDING operations of a RELEASED work order but never those of a
   DRAFT one, so a read between the two steps cannot race the demote back. The second step is not
   optional: a DRAFT header alone is **not** a draft — the dispatch board and the kiosk queue
   filter on terminal statuses only, so READY nests would stay on the board and be startable by
   badge, an operator start would flip the job to IN_PROGRESS with no release ever recorded, and
   the planner's later `release_work_order` would be refused. The result is
   `{"import": <raw import response>, "work_order": <re-read WO>, "demoted_to_draft": true,
   "operations_returned_to_pending": N, "note": …}`; the `note` discloses what the demote cannot
   undo — `released_at` / `released_by` still carry the stamp the route wrote, and
   `release_work_order` re-stamps them when the planner releases. With `release=true` the tool
   returns `{"import": …, "work_order": child, "demoted_to_draft": false}` and leaves the job on
   the floor's board.
3. **`add_laser_nest` applies the same demote only to a job that was not on the floor.** The route
   force-sets the laser work order RELEASED whether it minted it or not, so the tool records the
   laser work order's status *before* the call: a **new** child, or one that was **DRAFT**, comes
   back DRAFT with PENDING operations exactly as in rule 2; a job that was released / in progress /
   on hold before the nest was added is left as the route set it (`demoted_to_draft: false` plus a
   `note` saying why) — adding a nest to a running job must never take the job off the floor.
4. **A failed demote is loud.** If the header `PUT` fails (409 on a concurrent edit, 403, …) the
   result is `is_error` with the PUT's status and a message beginning **`IMPORT SUCCEEDED, BUT work
   order … is still RELEASED`** (`NEST ADDED, BUT …` from `add_laser_nest`), with the `import` /
   `nest` and `work_order` payloads (and `demoted_to_draft: false`) as top-level keys of the result
   beside `status` and `detail` — the work order exists and is released; put it
   back to DRAFT from the UI or with `update_work_order` (`work_order_id`, `version`,
   `status: "draft"`). If the header landed but an operation `PUT` failed, the message says exactly
   that (**`… is DRAFT, BUT N of its operations are still READY`**, naming them with the route's
   own detail) and `operations_returned_to_pending` counts the ones that did land.

Either `file` or `source_path` is required (422 otherwise). `rows` given as a list is serialised to
the JSON string the multipart route expects; `due_date` is sent only on the standalone path.
The upload decodes on the bridge side under `WERCO_MCP_MAX_UPLOAD_BYTES` (413-shaped tool error
over it) and is then subject to the route's own `LASER_UPLOAD_MAX_BYTES` (50 MB).

**What this is not.** The demote is the owner's rule for the *MCP* layer; it does not close the
application's own gap that `PUT /shop-floor/operations/{id}/start` accepts an operation whose work
order is DRAFT and flips the job to IN_PROGRESS with no work-order status-change audit row. With
the operations at PENDING the kiosk and the dispatch board no longer show them, which is what makes
the DRAFT real from the floor's side; a 409 on a DRAFT parent in that shop-floor route would close
the hole for every path, and is an owner decision rather than something made here.

---

## 11. Result shapes and caps

`app/mcp/results.py` — pure, so the rules are testable without a database.

| HTTP outcome | `CallToolResult` |
|---|---|
| **≥ 400** | `is_error: true`; `structured_content` = `{"status": <code>, "detail": <server detail, verbatim>, "method": …, "path": …}` and the same JSON as text. A 422 `detail` is FastAPI's list of pydantic errors; a 409 is the domain's own sentence. **Never rewritten.** Error bodies are bounded at 8000 chars: non-JSON text is cut with an ellipsis, and a pathological JSON `detail` (hundreds of pydantic errors) becomes `{"truncated": true, "chars": M, "preview": …}` — under the cap, which is every error an agent will actually meet, nothing is touched |
| **3xx** | `is_error: true`, `{"status": 307, "detail": "Redirect to <Location> (redirects are never followed; call the exact route path).", …}`. Neither executor follows redirects, so a trailing-slash 307 with an empty body must never read as a completed action |
| **204 / empty 2xx** | `{"ok": true, "status": <code>}` |
| **2xx JSON** | `structured_content` = the parsed JSON (a non-object — list, number — is wrapped as `{"result": …}`); `content` = the same, pretty-printed, **capped at `WERCO_MCP_MAX_RESULT_CHARS`** (default 200 000) with a trailing `[truncated: N of M chars — narrow with limit/skip or filters]`. When truncated, `structured_content` is replaced by `{"truncated": true, "chars": M, "status": …}` so the client is never handed the payload twice |
| **2xx `text/*`** | The text, same cap; `structured_content` = `{"status", "text"}` (or the truncation marker) |
| **2xx anything else** (PDF, XLSX, CSV attachment, image) | An `EmbeddedResource` blob: base64 body, the response's media type, `uri` = `werco://<tool>/<sha256[:12]>`; `structured_content` = `{"status", "content_type", "bytes", "uri", "filename"}` (filename from `Content-Disposition`). **Over `WERCO_MCP_MAX_BLOB_BYTES`** (default 5 MB) → `is_error` "Download it from the Werco ERP UI instead" with `bytes` / `content_type` / `filename` |
| **No HTTP status at all** (in-process exception, connection refused, timeout) | `is_error`, `{"status": 0, "detail": "<ExceptionClass>: <message>", …}` |
| **Bad arguments** (schema validation) | `is_error`, `{"status": 422, "detail": ["<tool>: <location>: <message>", …], "hint": …}` |
| **Unknown tool name** | `is_error`, `{"status": 404, "detail": "Unknown tool '…'. Call tools/list for the catalog."}` |
| **No credentials** | `is_error`, `{"status": 401, "detail": …}` — before any request is made (§4) |

Convenience tools that compose a payload (`list_work_centers`, the `include_dispatch_board`
dashboard, the nest import) run it through the same shaping and the same cap.

---

## 12. Example sessions

Argument names below are the real ones from the catalog. Every step is one `tools/call`; the ERP
answers exactly as it would the web app, so a Supervisor sees the same 403s a Supervisor sees in
the UI.

### 12.1 Plan a press-brake + plate-roll job and put it on the floor (Manager)

```text
list_work_centers        {"name": "brake"}
  → [{"id": 7, "code": "PB-01", "name": "Press Brake 1", "type": "press_brake", "is_active": true}]
list_work_centers        {"name": "roll"}
  → [{"id": 9, "code": "PR-01", "name": "Plate Roll", "type": "rolling", "is_active": true}]
list_work_centers        {"name": "weld"}
  → [{"id": 11, "code": "WLD-01", "name": "Weld Bay 1", "type": "welding", "is_active": true}]

create_work_order        {"part_id": 412, "quantity_ordered": 1, "due_date": "2026-09-19",
                          "customer_po": "PO-88121"}
  → {... "id": 913, "work_order_number": "WO-20260901-004", "status": "draft", "version": 0, ...}
     (there is no status argument — passing one is a 422; the job is DRAFT until released, §9)

add_operation            {"work_order_id": 913, "name": "Brake", "work_center": "Press Brake 1"}
  → operation 10 (sequence defaulted to the first multiple of 10)
add_operation            {"work_order_id": 913, "name": "Roll", "work_center": 9}
  → operation 20
add_operation            {"work_order_id": 913, "name": "Weld", "work_center": "weld"}
  → operation 30 (name resolved by unique substring)
add_operation            {"work_order_id": 913, "name": "Deburr", "work_center": 7}
  → operation 40
add_operation            {"work_order_id": 913, "name": "Bracket_Rev_C.dxf", "work_center": 7}
  → is_error 422: "Refusing operation name 'Bracket_Rev_C.dxf': it looks like a CNC/drawing
     file export. Use a short shop-floor step such as 'Laser', 'Brake', 'Weld' or 'Deburr'."
add_operation            {"work_order_id": 913, "name": "Weld", "work_center": 11, "quantity": 25}
  → is_error 422: "add_operation takes no `quantity`: an operation has no quantity of its own …"

get_work_order           {"work_order_number": "WO-20260901-004"}
  → status "draft", operations [10 Brake, 20 Roll, 30 Weld, 40 Deburr], version 0

duplicate_work_order     {"work_order_id": 913, "quantity_ordered": 1}
  → {"work_order": {"id": 914, "status": "draft", ...}}     # a second unit is its own DRAFT job

release_work_order       {"work_order_id": 913}
  → status "released"; operation 10 is READY on the dispatch board
```

### 12.2 Re-run a proven job (Manager)

```text
search                   {"q": "housing", "types": "work_orders", "limit": 5}
duplicate_work_order     {"work_order_id": 640, "quantity_ordered": 10, "due_date": "2026-09-30"}
  → {"work_order": {... "status": "draft" ...}, skipped material ties listed in the response}
update_work_order        {"work_order_id": 921, "version": 0, "priority": 2, "customer_po": "PO-88140"}
  → version 1 (send the current version or the route answers 409)
update_work_order        {"work_order_id": 921, "version": 1, "status": "released"}
  → is_error 422: "status 'released' is not written through update_work_order: use release_work_order …"
release_work_order       {"work_order_id": 921}
```

### 12.3 Import a nest package without putting it on the floor (Manager / Supervisor)

```text
import_laser_nest_package
  {"file": {"filename": "nest-2026-09-01.pdf", "content_base64": "<base64>", "content_type": "application/pdf"},
   "work_center_id": 3, "due_date": "2026-09-12"}
  → {"import": {... "child_work_order": {"id": 930, "status": "released",
                                          "operations": [{"sequence": 10, "status": "ready", ...}, ...]}},
     "work_order": {"id": 930, "status": "draft", "version": 1, "work_order_type": "laser_cutting",
                    "operations": [{"sequence": 10, "status": "pending", ...}, ...]},
     "demoted_to_draft": true, "operations_returned_to_pending": 12,
     "note": "released_at / released_by still carry the stamp the route wrote …"}

get_work_order           {"work_order_id": 930}
  → nests under operations[].laser_nest (cnc_file_name set = imported); nothing on the dispatch board

release_work_order       {"work_order_id": 930}        # when the planner is happy with it

add_laser_nest           {"work_order_id": 640, "cnc_number": "P-4471", "planned_runs": 3}
  → on a DRAFT assembly parent: its laser child is created, and handed back DRAFT with the nest PENDING
     {"nest": {"id": 77, "cnc_number": "P-4471", ...}, "work_order": {"id": 931, "status": "draft", ...},
      "demoted_to_draft": true, "operations_returned_to_pending": 1, "note": "released_at …"}
```

Pointing the import at a job that already carries a hand-keyed nest — or at the assembly parent
whose laser child does (`{"work_order_id": 905, ...}`) — is refused with a 409 **before** the
upload, and `add_laser_nest` on a job whose nests came from a package import is refused the same
way: a nest package and manual nests are never mixed on one job.

### 12.4 Morning read-only sweep (any role — reads are tenant-scoped, not role-gated)

```text
get_shop_floor_dashboard {"include_dispatch_board": true}
list_quality_ncrs        {"status": "open", "limit": 20}
list_purchase_orders     {"status": "sent", "limit": 50}
list_inventory           {"summary": true, "limit": 200}
get_low_stock_alerts     {"limit": 25}
get_current_shortages    {}
work_orders_list_work_orders {"status": "on_hold", "limit": 50}
export_work_orders       {"format": "xlsx", "start_date": "2026-08-01"}
  → ADMIN/MANAGER only (403 otherwise, audited as an EXPORT when allowed); the workbook comes back
    as an EmbeddedResource blob — or an is_error telling you to download it from the UI if it is
    over WERCO_MCP_MAX_BLOB_BYTES
```

### 12.5 Raise a purchase order without receiving it (Manager)

```text
list_vendors             {"active_only": true}
  → [{"id": 18, "code": "VND-018", "name": "Metals Supply Co", "is_approved": true, ...}, ...]
search                   {"q": "A36 plate", "types": "parts", "limit": 5}
  → part 2207 "PLATE-A36-0.250"

create_purchase_order    {"vendor_id": 18, "required_date": "2026-09-15", "ship_to": "Dock 2",
                          "lines": [{"part_id": 2207, "quantity_ordered": 40, "unit_price": 86.50}]}
  → {"id": 512, "po_number": "PO-2026-0512", "status": "draft", "total": 3460.0, "version": 0, ...}
     (one audit CREATE row for the PO, as the route documents)
add_po_line              {"po_id": 512, "part_id": 2211, "quantity_ordered": 10, "unit_price": 12.00}
  → line 2 added and the PO totals rolled (a CREATE row for the line, an UPDATE row for the PO)
get_purchase_order       {"po_id": 512}
  → status "draft", 2 lines, every line's quantity_received 0
send_purchase_order      {"po_id": 512}
  → status "sent": issued to the vendor, still nothing received
```

No receiving verb is called. Booking material against the PO is `receive_material` (Receiving &
Inspection), a separate deliberate step that creates the receipt, the lot and the stock movement —
the PO stays open, and `list_purchase_orders {"status": "sent"}` shows it waiting on the dock.
A Viewer or Operator running the same session gets `403 Insufficient permissions` on
`create_purchase_order`, exactly as in the Purchasing page.

Shop-floor time is never faked: `clock_in {"work_order_id", "operation_id", "work_center_id"}`,
`report_operation_production {"operation_id", "quantity_complete_delta"}`,
`clock_out {"time_entry_id", "quantity_produced"}` and
`shop_floor_complete_operation {"operation_id", "quantity_complete"}` are the real routes with the
real gates (a complete with zero labor recorded is refused 400, exactly as at the kiosk).

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| HTTP **401** with `WWW-Authenticate` from `/mcp` itself, before any tool | No bearer; an expired access token; a revoked / expired API token (or one whose user was deactivated); a display / station / kiosk-scoped token | Give the agent an **API token** (`POST /api/v1/api-tokens/`, Admin) — it never 401s until revoked, so a 401 on one means an Admin revoked it: mint a new one. With a 15-minute access token, send a fresh one (`POST /auth/refresh` or `POST /auth/login`); HTTP callers renew their own, only the stdio bridge refreshes for you |
| `{"status": 403, "detail": "API token cannot access this resource"}` | The call is authenticated by an API token and the tool fronts a `/auth` or `/api-tokens` route (`create_api_token`, `list_api_tokens`, `revoke_api_token`) | Token administration is an Admin's interactive act: use an Admin's interactive JWT (through the door or with `curl`). Nothing an API token holds changes this (§4) |
| Tool result `{"status": 401, "detail": "No ERP credentials configured for this bridge…"}` | stdio bridge started with none of `WERCO_ERP_TOKEN` / `WERCO_ERP_REFRESH_TOKEN` / `WERCO_ERP_EMAIL`+`WERCO_ERP_PASSWORD` | Set one; the bridge logs `Credentials configured: …` on stderr at startup |
| Tool result `{"status": 401, "detail": "Could not obtain an ERP access token…"}` | Refresh and login both failed (wrong password, locked account, login rate limit 5/min) | Check the account in the UI; wait out the limit |
| `{"status": 403, "detail": "Insufficient permissions"}` | The user's role does not allow that route (`require_role`) — the SPA would refuse too | Use a user with the right role; nothing in MCP can widen it |
| `{"status": 409, ...}` on `update_work_order` | Stale `version` (optimistic locking) | `get_work_order` → resend with the current `version` |
| `{"status": 422, ...}` from `update_work_order` naming `release_work_order` / `work_orders_start_work_order` | `status: released` or `in_progress` was written through the header update | Call the named verb; the header update never releases or starts work (§9) |
| `{"status": 422, ...}` `"Additional properties are not allowed ('serach' was unexpected)"` | A misspelled argument — every schema is strict (§6.3) | Fix the argument name; nothing was dispatched |
| `{"status": 409, ...}` from `add_operation` naming several work centers | Ambiguous `work_center` string | Pass the `id` (from `list_work_centers`) or a more specific name |
| `{"status": 409, ...}` from `import_laser_nest_package` naming nests | Target already carries manually entered nests | Import into a fresh standalone WO (omit `work_order_id`) |
| `{"status": 409, ...}` from `release_work_order` | The WO is not DRAFT | Nothing to do — it is already released or is terminal |
| HTTP **413** from `/mcp` | The JSON-RPC envelope (a base64 file inside it) is over `WERCO_MCP_MAX_UPLOAD_BYTES` (25 MB) | Smaller file, or raise the setting on the server |
| Tool result `{"status": 413, "detail": "File … exceeds the …-byte MCP upload cap."}` | Same cap, decoded on the bridge side | As above |
| `{"status": 413, ...}` with the route's own message | The inner route's cap (50 MB `LASER_UPLOAD_MAX_BYTES`, 20 MB QMS PDFs) or, for a plain JSON tool, `MAX_JSON_BODY_BYTES` (256 KB) on the inner request | Split the payload — [API.md → Request Size Limits](API.md#request-size-limits) |
| `{"status": 429, "detail": "Rate limit exceeded: …"}` (a tool result) | The inner route's per-IP limit (100/60 s default, stricter per-path limits on auth, scanner and standalone nest preview/import) — one charge per tool call, exactly like the SPA | Slow down; on the door the caller's IP is the key, on a remote bridge the bridge host's IP is, on an in-process bridge it is `127.0.0.1` |
| HTTP **429** from `/mcp` itself | The door path's own ceiling, `WERCO_MCP_HTTP_RATE_LIMIT` (300/minute per IP by default): `tools/list`, `initialize`, schema-rejected calls and unauthenticated probes never reach an inner route, so this is the only limit that counts them (§3.1) | Stop re-listing the catalog on every turn (cache it); raise the setting if a shop legitimately runs many agents behind one NAT address |
| HTTP **405** from `GET /mcp` | The door is stateless and has no server-push stream to open | Nothing to do — clients POST; the TypeScript SDK treats 405 as "no push stream" |
| HTTP **503** `MCP door is not running` | The app served the route before/without entering its lifespan | Run the app through its lifespan (uvicorn does); in tests open a `TestClient` context |
| `{"status": 0, "detail": "ConnectError: …"}` / `ReadTimeout` | Remote bridge cannot reach `WERCO_ERP_URL`, or a long import exceeded 120 s | Check the URL / TLS; retry |
| `{"status": 404, "detail": "Unknown tool …"}` | A generated name shifted after a router was added (§6.2), or a typo | `tools/list` / `--print-catalog`; anchor prompts on the convenience names |
| Result ends with `[truncated: N of M chars …]` and `structured_content` is a marker | Over `WERCO_MCP_MAX_RESULT_CHARS` | Narrow with `limit` / `skip` / filters, or raise the cap |
| `is_error` "… over the …-byte MCP blob cap. Download it from the Werco ERP UI instead." | A binary response over `WERCO_MCP_MAX_BLOB_BYTES` | Fetch it from the UI, or raise the cap |
| Bridge prints JSON garbage / client reports a protocol error | Something wrote to the real stdout | The CLI already redirects; make sure nothing you added prints before `main()` captures the wire |

---

## 14. Settings and files

- Server settings (`app/core/config.py`): `WERCO_MCP_HTTP_ENABLED`, `WERCO_MCP_HTTP_PATH`,
  `WERCO_MCP_HTTP_RATE_LIMIT`, `WERCO_MCP_MAX_RESULT_CHARS`, `WERCO_MCP_MAX_BLOB_BYTES`,
  `WERCO_MCP_MAX_UPLOAD_BYTES`. Client-side bridge variables (read by `app/mcp/__main__.py` /
  `auth.py`, **not** `Settings`): `WERCO_ERP_URL`, `WERCO_ERP_TOKEN`, `WERCO_ERP_REFRESH_TOKEN`,
  `WERCO_ERP_EMAIL`, `WERCO_ERP_PASSWORD`, `WERCO_MCP_TRANSPORT`, `WERCO_MCP_HOST`, `WERCO_MCP_PORT`.
  Tables in [ENVIRONMENT_VARIABLES.md → MCP](ENVIRONMENT_VARIABLES.md#mcp-model-context-protocol-door-and-bridge).
- Dependency: `mcp==2.1.1` (`backend/requirements.txt`; transitives `mcp-types`, `httpx2`,
  `sse-starlette`, `jsonschema`, `anyio`).
- Tests (all in the default `pytest tests/` run, so in CI's required "Backend Tests" job):
  - `backend/tests/test_mcp_catalog.py` (no DB) — membership against the live `app.openapi()` (every
    secured, unshadowed, unexcluded operation is exactly one tool; `PUBLIC_OPERATIONS` names exactly
    the unsecured routes; `EXCLUDED_OPERATIONS` are the two loaders), names (unique, wire-valid,
    ≤ 64, collisions prefixed, the numeric-suffix fallback proven on synthetic input and unused on
    the live catalog), schemas (`$ref`-free, `additionalProperties: false`, every JSON body's
    property set equal to the document's — the `title` field included), and the two convenience
    schemas that front a route (`update_work_order`, `add_laser_nest`) pinned to that route's own
    property set.
  - `backend/tests/test_mcp_coverage.py` — the CI trip-wire for "a new router shipped and MCP is
    blind": `catalog.uncovered_tags` per tag (with the shadow credit granted only for a tag a
    convenience tool claims) **and** `catalog.unaccounted_operations` per route, plus the live
    route table (every `app/api/endpoints/` module owns a wired route, no `/api/v1` route hides from
    the document), each guard also proven on synthetic documents so an empty result is a finding.
  - `backend/tests/test_mcp_server.py` — the owner's rules on database rows over the SDK's
    in-memory transport: DRAFT create with `status` refused, the name guard and the `quantity`
    refusal, `update_work_order` refusing `released` / `in_progress`, the nest import demoting header
    and operations (and the parent-addressed manual-nest check reading the laser child), the real
    `add_laser_nest` route on a DRAFT parent and on a running job, tenant scoping, the result shapes
    (truncation, blob, 3xx, bounded error detail).
  - `backend/tests/test_mcp_http_door.py` — the door at the wire: 401 + `WWW-Authenticate` for no /
    garbage / kiosk / refresh / display / signin / station tokens, `GET` → 405, the operator 403 and
    manager DRAFT through the door, both body caps, the default-limit exemption with the inner hit
    kept and the door's own path ceiling, the reserved-path and occupied-path refusals.
  - `backend/tests/test_mcp_remote_executor.py` — the stdio bridge's HTTPS side over a
    `MockTransport`: refresh-on-401, password login, wire hygiene.
  - `backend/tests/test_mcp_docs_names.py` — the docs-name guard: §7's table names exactly the
    convenience tools; every representative tool in §5, every call in the §12 sessions, every
    tool-name claim elsewhere in this file (§6.2 excepted — it discusses function names on
    purpose), in `CLAUDE.md`'s `mcp/` bullet and in `API.md`'s MCP section exists in the live
    catalog; the names the package's own instructions and descriptions quote exist too; and
    `.cursor/mcp.json.example` is a bare `mcpServers` map with no comment keys.
  - `backend/tests/test_mcp_cli.py` — the stdio bridge's process boundary: argument parsing,
    the remote-mode placeholder environment (fills only what is missing, boots `Settings` without a
    database), credential kinds logged without values, `--print-catalog` as a clean-environment
    subprocess whose stdout is exactly one JSON document, and a real `stdio_client` round trip
    against a dead ERP (full catalog listed, a status-0 transport error rather than a hang, and the
    "no credentials" 401 result).
  - `backend/tests/test_api_tokens_smoke.py` — API tokens end to end: an Admin issues one (201,
    shown once, audit rows without the plaintext), the holder acts on a normal route with the
    `last_used_at` touch written once, the `/auth` + `/api-tokens` fence (403, and the 401s on
    `/auth/refresh` and the badge mint), the user's role carried (a Manager's token 403 on an Admin
    route), revoke → 401 on the next call and 409 on a second revoke, listing without secrets; then
    the **HTTP door** with an API token — `tools/list`, a read as the holder, `create_api_token`
    through the door answering the route's 403 fence, and a 401 at the door once revoked.
  - `backend/tests/test_migration_088_api_tokens.py` — the `api_tokens` table: single head,
    model/migration lock-step (columns, indexes, defaults), RLS Postgres-only, and the SQLite
    upgrade / downgrade round trip.
- API tokens (server side, no new setting): `app/api/endpoints/api_tokens.py`,
  `app/services/api_token_service.py` (`check_api_token` is the one row check the door and
  `get_current_user` share), `app/models/api_token.py`, migration `088_api_tokens`; the door half is
  `app/mcp/auth.py` (`live_api_token_principal`, `ErpTokenVerifier`).
- Related: [API.md → MCP door](API.md#mcp-door-mcp), [API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients),
  [RBAC_PERMISSIONS.md → MCP / agent access](RBAC_PERMISSIONS.md#mcp--agent-access),
  [DEPLOYMENT_RUNBOOK.md → Enabling the MCP door](DEPLOYMENT_RUNBOOK.md#enabling-the-mcp-door-optional),
  `.cursor/mcp.json.example`, `backend/.env.example`.
