# Role-Based Access Control (RBAC) Documentation

## Overview

Werco ERP has seven tenant roles and a separate Platform Admin role. Backend API checks enforce
authorization; frontend controls reflect those policies for usability.

## Roles

| Role | Description | Use Case |
|------|-------------|----------|
| **Admin** | Tenant administration and domain access | Company administrators, IT staff |
| **Manager** | Department-wide access with approval capabilities | Department managers, production managers |
| **Supervisor** | Team-level access with create/edit permissions | Shift supervisors, team leads |
| **Operator** | View and update assigned work | Machine operators, production workers |
| **Quality** | Quality-specific actions | Quality inspectors, QC staff |
| **Shipping** | Shipping operations | Shipping clerks, warehouse staff |
| **Viewer** | Read-only access | Auditors, executives, guests |
| **Platform Admin** | Cross-company administration and context switching | Platform operators; not assignable through tenant account APIs |

## Access enforcement model

Permissions are enforced at two layers, and the two layers **intentionally differ for reads**:

- **Writes / state changes** (Create, Edit, Delete, Approve, Release, Send, Adjust, Transfer, Complete, Inspect, …) are enforced **server-side** via the `require_role` dependency on the endpoint. These are the authoritative access controls and match the matrix below.
- **Operational/domain reads** — the **View** rows for the operational modules below (e.g. Work Orders, Parts, BOMs, Routings, Inventory, Purchasing, Receiving, Customers, Quotes) — are **tenant-scoped** (every query is filtered to the caller's active company via `get_current_company_id`) and are available to **any authenticated user within that tenant**. The list/detail GET endpoints depend on `get_current_user` only and do **not** restrict reads by role. The **View** columns therefore describe the *intended in-app navigation* (which the frontend gates for usability), not a server-enforced read restriction. This is the current intended design: **read-broad / write-restricted**.
- **Administrative / governance reads are the exception and _are_ enforced server-side:** **Users** (`require_role([ADMIN, MANAGER])`), **Admin Settings** (`ADMIN`), and **Audit Logs** (`require_role([ADMIN, MANAGER])`).
- **Controlled documents are read-gated server-side by their access level** (migration `131`, AS9100D 7.5.3.1): a `restricted` document opens only for Admin / Manager / Quality / Supervisor, and an `export_controlled` one only for a user export-authorized in their own company, whatever the role. Every path that returns document metadata or bytes answers **404** for a document the caller may not open, and the audit log redacts the rows that name that document on read. `standard` documents (the default) stay read-broad. See [Documents](#documents).
- **Advisory stock-piece observation evidence is also server-gated:** its source/history routes require effective `inventory:view`, including company role overrides. This separate register does not change the read-broad gates on existing operational inventory endpoints. Recording requires the existing stock-mutator roles as detailed under Inventory below.
- **Bulk data export is its own access category, not a domain read, and _is_ enforced server-side** — `require_role([ADMIN, MANAGER])` plus an `EXPORT` audit row on every one of them. See [Bulk data export is not a domain read](#bulk-data-export-is-not-a-domain-read) immediately below, and [Bulk Data Export](#bulk-data-export) in the matrix for the route list.

> If the business requires least-privilege on domain reads (e.g. hiding vendor pricing / PO financials from Operator/Quality/Shipping at the API), enforce it **uniformly** by adding `require_role` to the read endpoints across modules, with authorization tests — not per-router. Until then, treat the **View** columns for operational modules as UI-visibility, not as a server-enforced control.

Authenticated HTTP requests resolve the current stored user and active company. Disabled users
and unavailable companies are refused; an ordinary user's token company must match the account's
current company. Platform principals may use a switched context. An inactive viewed company
still permits logout/switch, subject to those routes' own checks. Only **stored** Platform Admin/
superuser principals retain `/platform/` control-plane access for reactivation of an inactive home
or sole company; ordinary tenant-data access remains blocked. Read-only contexts still refuse
writes, with the existing logout/switch exceptions. API tokens receive no inactive-company escape.

WebSockets accept only **unscoped access JWTs**, not kiosk/operator-scoped, API, display, station
or refresh credentials. All three routes validate the live account/company; work-center and
work-order routes also resolve an owned parent (work orders must be undeleted). These checks
run at admission, before event delivery, after incoming messages and after 30 seconds idle;
failure closes with **1008**. HTTP inactive-company recovery exceptions do not apply. Logout
does not revoke an unexpired access JWT; these checks introduce no access/refresh revocation list.
See [API authentication](API.md#using-the-token) and [WebSockets](API.md#real-time-updates-websocket).

### Bulk data export is not a domain read

**Handing over a whole dataset as a file is server-gated to Admin / Manager and audited, and is
deliberately outside the read-broad rule above.** The read-broad paragraph still stands exactly as
written — it is, and remains, the rule for domain reads. This is a separate category sitting beside
it, not an amendment to it.

**Why the two are different exposures.** A domain read returns **one record** through the UI, to
someone who had to navigate to it and who can carry away only what is on the screen in front of
them. A bulk export returns the **entire dataset as a file, in a single request** — the parts master
with `standard_cost`, the full inventory valuation, every PO line with `unit_price` and vendor,
every quote with its customer contacts. That is a disclosure event rather than navigation: the file
leaves the system, keeps its value after the account is disabled, and is the shape a departing
employee or a compromised low-privilege session actually uses. Auditing follows from the same fact —
if a dataset can leave in one request, there has to be a record of it having left.

**Why this is not the per-router least-privilege the paragraph above warns against.** The objection
there is to tiering *reads* one router at a time, which produces a system where the same record is
visible on one screen and refused on another. This carve-out does the opposite: it changes nothing
about who can open a record on screen (a Supervisor, Operator, Quality, Shipping or Viewer user
still reads every one of these datasets in the UI), and the gate is applied **uniformly to every
bulk-export surface in the system at once** — which is precisely the "uniformly, not per-router"
discipline being asked for. The trigger is the shape of the response, not the identity of the
router.

**The boundary: one record is not a dataset.** Single-record document routes are on the other side
of the line and are deliberately untouched — a CoC PDF, one quote PDF, one nest drawing, one
work-order traveler, one estimate breakdown, a kiosk document view. Two of those are load-bearing on
the shop floor (an Operator must be able to pull the traveler and open the controlled drawing at the
point of use), so gating them would break production, not tighten it. The test for whether a new
route falls under this rule is: *does one request return a whole table's worth of rows as a file?*
If the path carries a record id, the answer is no. Where a surface already sits at a **stricter**
tier, that tier stands — this rule is a floor, never a loosening.

### MCP / agent access

Agents reach the API through the MCP server in `backend/app/mcp/` ([docs/MCP.md](MCP.md)) —
either the Streamable HTTP door at `/mcp` (off by default, `WERCO_MCP_HTTP_ENABLED`) or the stdio
bridge `python -m app.mcp`. **Nothing in this document changes for them** (the credential rules on
top of the role — the writes an API token is refused whatever its user's role, such as declaring
scrap or closing work short, and the judgment writes deliberately left open to one — are listed once,
in [Credential policy for judgment writes](#credential-policy-for-judgment-writes), and apply to an
API token however it is presented). Every tool call is a
real request dispatched through the same routers with a real user's bearer — a 15-minute access JWT
or a long-lived, Admin-issued **API token** bound to one user (see the API tokens callout under
[Admin](#admin)): `get_current_user` resolves the user, tenancy comes from the token's active
company (for an API token, the company pinned on its row), `require_role` decides, and every
write is audited **as that user**. The matrix below is the matrix an agent gets — a Supervisor's
agent sees exactly the 403s a Supervisor sees in the UI, bulk export stays Admin / Manager and
audited, Users / Admin Settings / Audit reads stay server-gated, and the read-broad rule for domain
reads applies unchanged.

**A token can be minted narrower than its user — never wider** (migration `119_api_token_scope`,
Claude-plugin brief P0, 2026-11-13). The narrowing is fixed at mint, audited, and read from the
`api_tokens` row on every request (`get_current_user`); the MCP door applies the same rule to its
listing. It applies through the door and on direct `/api/v1` calls alike, and a full token is
unchanged.

- **Read-only** (`scope = "read_only"`):
  - only `GET` / `HEAD` / `OPTIONS`, whatever the user's role; anything else is **403**;
  - nothing under a tag in `WERCO_MCP_READ_ONLY_DENY_TAGS` (Documents, Document Deliveries, Data
    Export, Users, Audit, Admin Settings, Carrier Integrations, Platform Administration, Company
    Management, API Tokens, Hank Intake) — **403**; API Tokens, Users, Audit, Documents and Data
    Export are a floor the setting can never drop;
  - no file bytes (a non-JSON response becomes **403**), and a GET exporter that writes an
    `EXPORT` audit row refuses it **403** before writing that row;
  - global search drops the `user` result type.
- **AI-external** (`ai_external = true`; always read-only): additionally refused unless the
  company's `allow_ai_egress` is on, limited to a fixed read allowlist, and served export-controlled
  and unlinkable records redacted. See [docs/EXPORT_CONTROL_AI_EGRESS.md](EXPORT_CONTROL_AI_EGRESS.md).

So a Viewer-bound read-only token sees **less** than the Viewer does in the UI, never more. The
predicates are in [Credential policy for judgment writes](#credential-policy-for-judgment-writes).

There is **no MCP identity, no MCP role and no MCP-only permission**: the package reaches no
service. Its database reads are the door's API-token row check, plus the company's AI-egress
switch for an AI-external token. They decide whether the credential is still live and how far a
narrowed token is narrowed — never anything wider than the user's role. It cannot bypass `require_role`, and a 401/403
from the route is returned verbatim rather than worked around. Kiosk-scoped badge tokens, wallboard
display tokens and station tokens are refused at the door because they are not user credentials;
an API token is accepted, as the one user it is bound to. What
the MCP layer *adds* is stricter, not looser. A narrowed token's listing and by-name refusals
mirror the API's own gates (above). One refusal is the door's own and applies to every caller, a
person's own JWT included: a Hank note (`prepare_task` with kind `add_note`, or `execute_task` on
an `add_note` task) is **403** before dispatch, because a note is a person's own words (owner
decision 2026-11-16; [MCP.md §4.3](MCP.md#43-people-only-hank-tasks-every-credential)). The
layer's hand-written tools force new and duplicated work
orders to land DRAFT, keep release an explicit separate call (the header-update tool refuses
`status: released` / `in_progress`, which the raw `PUT` would have accepted with no release stamp),
set an imported or hand-keyed nest package back to DRAFT with its operations pending (a job on hold
is left on hold), never mix manual nests with a package import, and refuse operation names that are
file names — see
[MCP.md → DRAFT guarantees](MCP.md#9-draft-guarantees). The two Excel-cutover loaders are not
tools at all, and neither is AI **Accept & apply** (`POST /ai/recommendations/{id}/apply`): under
owner ruling 3 only a person signed in to the app runs an AI recommendation's action, so it is in
`catalog.EXCLUDED_OPERATIONS`, and the route refuses API tokens besides. The three Approved Supplier
List writes (recording a supplier-approval decision and the ASL import preview / commit) and the two
legacy `approved_supplier_list` writes are excluded the same way, and refuse API tokens too — see
[Purchasing](#purchasing). So are the five comment and watcher writes (`POST` / `PATCH` / `DELETE
/comments`, `POST` / `DELETE /watchers`; owner decision 2026-11-16, a comment is a person's own
words): the reads — the thread, the watch state and mention search — stay tools; see
[Comments and watchers](#comments-and-watchers). So are the four floor-PIN writes (setting,
clearing or unlocking a user's floor PIN, `PUT` / `DELETE /users/{user_id}/floor-pin` and
`POST /users/{user_id}/floor-pin/unlock`, and the company's badge sign-in PIN policy,
`PUT /companies/me/floor-pin-policy`; AS9100D 8.5.2): credential administration is a person's act,
the routes refuse API tokens too, and reading a user's PIN state or the policy stays a tool; see
[Users](#users). So are the two document-access writes
(`POST /documents/{id}/access-level` and `PUT /users/{id}/export-authorization`, migration `131`):
an agent on a person's JWT must not lower an export-controlled drawing or grant export access. The
access review stays a tool; see [Documents](#documents). Bulk release (`POST /work-orders/bulk-release`) is excluded as well, so an
agent releases one job per call; unlike those, the route itself has no API-token refusal — see
[Work Orders](#work-orders). An agent on an API token
also gets a 403 the same person would not get in the UI when it tries to mark an AI action card
handled — see [Credential policy for judgment writes](#credential-policy-for-judgment-writes)
and [AI recommendations (Action Inbox)](#ai-recommendations-action-inbox).

Operationally: give agents a **dedicated user** (e.g. *Werco Assistant*, at the role the agent
needs — the owner's assistant is an Admin; a Manager or a narrower role tightens what it can do) so
the audit trail attributes agent writes and the role can be changed without touching a person's
account — and hand it an **API token** (`POST /api-tokens/` or Admin → API Tokens, Admin only)
rather than a 15-minute access token: same role, same audit attribution, revocable with a reason,
and fenced from `/auth` and `/api-tokens` so it can never mint another credential. An agent that
only needs to read gets a **read-only** token. An **outside AI service** gets a read-only,
AI-external token bound to a **Viewer** user.

### Credential policy for judgment writes

A few writes record a **person's judgment** — not just a permitted action — and some of them are
restricted to certain *credentials*, on top of the role gate. The restrictions have eleven sources,
and each row below names its own: owner ruling 3 of 2026-11-05 (the two AI rows), the owner's
decision of 2026-11-07 (a standing API token may not declare scrap or close work short), this
PR's extensions of that decision (scrap reasons, the re-stamp short close and the other count
judgments), the owner's decision of 2026-11-13 (only an Admin, signed in, marks a customer
export-controlled), the lead's design decision of 2026-11-13 for the two quality-approval verbs (CAR
Quality Manager approval, FAI customer approval) and its review follow-ups (the FAI final
disposition and the CAR form's vendor / customer sign-off boxes), and the lead's decisions of
2026-11-15 (an egress kill switch is a person's judgment; a narrowed token's read writes nothing),
the Approved Supplier List change of 2026-11-13 (the supplier-approval rows), and the owner's
decisions of 2026-11-16 for work-order comments (a comment is a person's own words) and of 2026-11-17
(a standing API token may not complete a maintenance work order with checklist items unticked), and
the NCR / CAR records change of 2026-11-19 (migration `127`: an NCR's disposition and concession,
its re-verification, and a CAR's effectiveness verification), the floor-PIN change of
2026-11-19 (migration `130`: setting, clearing or unlocking a badge sign-on PIN, and the company's
PIN policy, is credential administration, a person's act), and the restricted document viewing
change of 2026-11-19 (migration `131`: a document's access level and a user's export authorization),
and the owner's decision of 2026-11-20 that a standing API token may file a maintenance log entry
(an MCP agent on a person's own JWT too, a half not separately put to the owner), and the in-process
inspection sign-off change of 2026-11-23 (signing that an inspection was performed is a person's
judgment, as an FAI final disposition is; a default taken from the improvement plan, not an owner
ruling).
This section is the **one list** of those credential rules — what refuses a credential,
what is deliberately left open (and who decided each), and what is deliberately not refused —
across the AI governance (ruling 3), floor-gates (ruling 2) and short-count (ruling 1) changes;
other documents point here. The predicates live once, in
`backend/app/core/credentials.py`, and read the credential `get_current_user` stamped on the
request:

| Predicate | Refuses | Allows |
|---|---|---|
| `is_interactive_person` | A long-lived **API token**, and any station- or badge-scoped (`kiosk`) token | A person signed in with their own access JWT |
| `is_api_token_credential` (the write is refused when true) | A long-lived **API token** | An interactive session, and a crew-station badge token (a person standing at the kiosk) |
| `is_read_only_api_token` (migration `119`) | An API token minted `scope = "read_only"` — and, failing closed, any API token whose scope is not exactly `full`. `get_current_user` refuses it every non-`GET` / `HEAD` / `OPTIONS` request and every route under a tag in `WERCO_MCP_READ_ONLY_DENY_TAGS` (**403**); global search drops its `user` results | A full API token, an interactive session, a badge token |
| `is_ai_external_credential` (migration `119`) | An API token minted `ai_external = true` (failing closed: any value but exactly `false`). Refused while the company's AI egress is off and outside the fixed read allowlist (**403**), and served export-controlled records redacted ([EXPORT_CONTROL_AI_EGRESS.md](EXPORT_CONTROL_AI_EGRESS.md)) | Every other credential |
| `is_narrowed_api_token` | The union of the two above (an AI-external row is read-only whatever its scope says). Its reconcile-on-read GETs (`GET /work-orders/`, `/work-orders/{id}`, `/shop-floor/dashboard`, `/shop-floor/operations`, `/shop-floor/operations/{id}`) skip the reconcile and serve the stored state — no status flip, audit row, event, notification, inventory movement or rollup (lead decision 2026-11-15, "read-only means read-only"); a GET exporter refuses it **403** before writing its `EXPORT` row | A full API token, an interactive session, a badge or display token — their reads reconcile as before |

The three narrowing predicates are access rules, not judgment-write rules: they decide what a token
may read or call at all, on every route, and whether its reads may write. They are listed here so that this table stays the one
list of credential predicates.

Writes that apply them — each refused before any write and before any audit row (the 403 texts
live in `ai_action_applier.py` and `operation_action_gates.py`, for the quality judgments in
`quality_record_service.py` and `fai_service.py`, for export control
`app/api/endpoints/customers.py`, for the egress switches, the comment writes, the floor-PIN writes, the document-access writes and the inspection sign-off `app/core/credentials.py`, and for the supplier-approval rows
`app/services/supplier_approval_service.py` and `app/api/endpoints/purchasing.py`, for the maintenance
checklist `app/services/maintenance_checklist.py`; where each sits in its route's
check order is in `docs/API.md` → Work Orders → "Short count at completion" and → AI
Recommendations):

| Write | Predicate | Refusal | Decided by |
|---|---|---|---|
| AI **Accept & apply** — `POST /ai/recommendations/{id}/apply` | `is_interactive_person` | **403**; also not an MCP tool | Owner ruling 3 |
| **Mark handled** — record-only `POST /ai/recommendations/{id}/accept` on an AI action card | `is_interactive_person` | **403** | Owner ruling 3 |
| **Declaring scrap** — any request that would raise a recorded `quantity_scrapped` (an operation's, a time entry's or a work-order header's), by any positive amount: `POST /shop-floor/clock-out/{id}`, `POST /shop-floor/operations/{id}/production` (with or without `open_ncr`), the Hank `report_production` task (`POST /hank/tasks` and `…/execute`), both operation-complete verbs, force-complete (`POST /work-orders/{id}/complete`), `PUT /work-orders/operations/{id}` and `PUT /work-orders/{id}` | `is_api_token_credential`, through `operation_action_gates.api_token_declares_scrap` (a non-finite delta fails closed) | **403** `API_TOKEN_SCRAP` (`Scrap must be reported by a person …`) | Owner decision 2026-11-07: only a person — kiosk, crew badge, desk — declares scrap, because a scrap count is quality evidence someone has to have seen |
| **Writing or changing a scrap reason or code** (`scrap_reason` / `scrap_reason_code_id`), even with no new scrap: clock-out (onto the caller's own time entry), `PUT /work-orders/{id}`, force-complete re-sending the recorded scrap with a different reason or code (or with none over a recorded one), `PUT /work-orders/operations/{id}`. Sending no reason, or the stored one, is no change | `is_api_token_credential`, through `api_token_changes_scrap_reason` | **403** `API_TOKEN_SCRAP` | **This PR's extension** of the 2026-11-07 decision: re-categorizing a person's scrap is the same evidence (the scrap Pareto groups by the code) |
| **Closing work short** — a short close by either operation-complete verb whose scrap is already on record (one declaring new scrap gets `API_TOKEN_SCRAP` first), a force-complete below the order, and an office reduce-production re-stamp that would set a declared short | `is_api_token_credential` (reduce-production through `production_reduction_service.api_token_reduction_refusal`) | **403** `API_TOKEN_SHORT_CLOSE` (`Closing work short must be done by a person …`) | Owner decision 2026-11-07 (a token keeps only the full-count close); the re-stamp case is **this PR's extension** |
| **The other count judgments** — good above the floor's labor record (office operation complete, override reason or not; force-complete above `deliverable`); `PUT /work-orders/operations/{id}` changing `quantity_complete` or a `quantity_scrapped` that is not a rise; `PUT /work-orders/{id}` changing the header's `quantity_complete` / `quantity_scrapped` in either direction; **every** correction through `POST /work-orders/operations/{id}/reduce-production` or `POST /shop-floor/operations/{id}/reduce-production`; lowering `quantity_ordered` once any operation has recorded good, scrap, a start or any labor; lowering a laser nest's `planned_runs` through `PATCH /laser-nests/{id}` once that nest's operation has (its runs are the operation's target, and the laser work order's order is derived from them). Both lowerings read one rule, `operation_action_gates.operation_has_recorded_work` (good, scrap, a start — IN_PROGRESS / COMPLETE status or a stamped `actual_start` — or any `TimeEntry`) | `is_api_token_credential` (reduce-production through `api_token_reduction_refusal`, re-checked under the row locks) | **403** `API_TOKEN_COUNT_JUDGMENT` (`Recording more good than the floor counted, …`) | **This PR's extension** of the same principle — the owner was asked about scrap and short closes, not these |
| **Marking a customer export-controlled, or un-marking it** — `POST /customers/{id}/contracts` and `PUT /customers/{id}/contracts/{contract_id}` (ITAR / export controlled / CUI contract flags), and reading the standing behind them, `GET /customers/{id}/export-control` | `is_api_token_credential`, inline in `app/api/endpoints/customers.py` (after the Admin role gate) | **403** `"An API token cannot read or change a customer's export control: an Admin does it, signed in to the app."` — any token, whatever its role or scope | Owner decision 2026-11-13 (Claude-plugin brief P0.3): only an Admin, signed in, marks or un-marks a customer |
| **Turning a company egress kill switch on or off** — `PUT /companies/me/ai-egress` (`allow_ai_egress`, which also gates every AI-external connector), `PUT /companies/me/sms-egress` (`allow_sms_egress`), `PUT /admin/settings/shipping-profile` (`allow_carrier_egress`) and `PUT /receiving/print-profile` (`allow_print_egress`). The two profile routes are refused **whole**, since each is its switch's only writer. None of the four is an MCP tool (`catalog.EXCLUDED_OPERATIONS`), so a person's own JWT cannot reach them through an agent either. The AI flip's UPDATE and STATUS_CHANGE rows are **required** evidence (`required_audit`; **503**, nothing changed, when either cannot be written) | `is_api_token_credential`, inline at the top of each route (after the Admin role gate) | **403** `API_TOKEN_EGRESS_SWITCH` (`"A company egress kill switch (AI, SMS, carrier or label-print egress) is turned on or off by an Admin signed in to the app, not an API token: opening or closing data egress is a person's judgment."`) — any token, whatever its role or scope (a read-only token meets the read-only gate's 403 first) | Lead decision 2026-11-15 (Claude-plugin brief build, round-2 owner question): opening or closing data egress is a person's judgment |
| **Re-importing a nest package onto a laser job with recorded work** — `POST /work-orders/{id}/laser-nest-packages/import` (either import shape; the MCP tool `import_laser_nest_package` with a `work_order_id`) when any operation the import would delete has recorded work (the same `operation_has_recorded_work` rule), or the laser work order's header already carries good or scrap: the import replaces every nest operation and zeroes the header counts, erasing a person's good, scrap and any declared short. Refused before the upload is unpacked, and again under the import's lock reading the database rather than the rows the first check loaded; a token's import onto un-worked nests with a zero header, and the standalone import, which wipes nothing, are unchanged | `is_api_token_credential` (`laser_nest_service.import_would_wipe_recorded_work`) | **403** `API_TOKEN_NEST_REIMPORT` (`Re-importing a nest package replaces nests that already have recorded work …`) — its own text, so the caller is told which action was refused | **This PR's extension** of the same principle — the owner was asked about scrap and short closes, not this |
| **Recording a CAR's Quality Manager approval** — `POST /quality/car/{id}/quality-approval` (the MCP tool `approve_car_quality`). The stamp names the approving person | `is_api_token_credential` | **403** `API_TOKEN_QUALITY_APPROVAL` (`A standing API token cannot record a CAR's quality approval. A person signed in to the app must record it.`) | Lead's design decision for the CAR form (2026-11-13), applying the 2026-11-07 principle; not an owner ruling |
| **Recording an FAI's customer approval** — `POST /quality/fai/{id}/customer-approval` (the MCP tool `record_fai_customer_approval`). It asserts that a person saw the customer's acceptance | `is_api_token_credential` | **403** `API_TOKEN_CUSTOMER_APPROVAL` (`A standing API token cannot record an FAI's customer approval. A person signed in to the app must record it.`) | Lead's design decision for the FAI log of record (2026-11-13), applying the 2026-11-07 principle; not an owner ruling |
| **Recording an FAI's final disposition** — `PUT /quality/fai/{id}` setting `status` to `passed`, `failed` or `conditional`. It is the inspection's quality judgment: it stamps the approver and freezes the evidence for good. Refused before the row lock; a token's non-final edits (notes, `in_progress`, inspector, inspection date) are unchanged | `is_api_token_credential` | **403** `API_TOKEN_FAI_DISPOSITION` (`A standing API token cannot record an FAI's final disposition. A person signed in to the app must record it.`) | Review follow-up to the lead's 2026-11-13 decision (the lesser customer approval already refused tokens, so the disposition before it could not stay open); not an owner ruling |
| **Filling or changing a CAR's vendor or customer approval boxes** — `vendor_approval_name` / `_title` / `_date` and `customer_approval_name` / `_date` on `POST /quality/car` (any non-blank value) or `PUT /quality/car/{id}` (any change, clearing included; re-sending the stored value is no change). The printed controlled form shows them as the vendor's and customer's sign-offs | `is_api_token_credential` | **403** `API_TOKEN_CAR_SIGNOFF` (`A standing API token cannot record or change a CAR's vendor or customer approval. A person signed in to the app must record it.`) | Review follow-up to the lead's 2026-11-13 decision, for the same reason as the FAI customer approval; not an owner ruling |
| **Changing an NCR's disposition or customer concession** — `PUT /quality/ncr/{id}` (the MCP tool `update_ncr`) changing `disposition` or any of `concession_reference` / `concession_authorized_by` / `concession_date` (clearing included; re-sending the stored values beside another edit is no change). The disposition change stamps `dispositioned_by` with the deciding person (AS9100D 8.7.1 / 8.7.2). A token's other NCR edits, `action_taken` included, are unchanged | `is_api_token_credential` | **403** `API_TOKEN_NCR_JUDGMENT` (`A standing API token cannot record an NCR's disposition or customer concession. A person signed in to the app must record it.`) | Design decision of the NCR / CAR records change (2026-11-19, migration `127`), applying the 2026-11-07 principle as the lead's 2026-11-13 quality-approval decision did; not an owner ruling |
| **Recording an NCR's re-verification** — `POST /quality/ncr/{id}/reverification` (the MCP tool `record_ncr_reverification`). The stamp names the person who re-verified the reworked or repaired product, and a `conforming` result is what lets a rework / repair NCR close. Refused before the NCR is read | `is_api_token_credential` | **403** `API_TOKEN_NCR_REVERIFICATION` (`A standing API token cannot record an NCR's re-verification. A person signed in to the app must record it.`) | The same 2026-11-19 design decision; not an owner ruling |
| **Recording a CAR's effectiveness verification** — `POST /quality/car/{id}/effectiveness` (the MCP tool `record_car_effectiveness`). The stamp names the person who judged the corrective action effective or not, an `effective` result is what lets a corrective CAR close, and `not_effective` returns the CAR to corrective action. Refused before the CAR is read | `is_api_token_credential` | **403** `API_TOKEN_CAR_EFFECTIVENESS` (`A standing API token cannot record a CAR's effectiveness verification. A person signed in to the app must record it.`) | The same 2026-11-19 design decision; not an owner ruling |
| **Recording a supplier-approval decision or loading the Approved Supplier List** — `PUT /purchasing/vendors/{id}/approval`, `POST /purchasing/vendors/asl-import/preview` and `POST /purchasing/vendors/asl-import/commit` (the preview too: it exists only to feed a commit) | `is_api_token_credential` | **403** (`Supplier approval is a person's decision: an API token cannot change the Approved Supplier List. Sign in to the app to do this.`); also not MCP tools (`catalog.EXCLUDED_OPERATIONS`) | Design decision of the Approved Supplier List change (2026-11-13): a supplier approval on the AS9100D 8.4 list is a named person's judgment. Not an owner ruling |
| **Writing the legacy `approved_supplier_list` table** — `POST /supplier-scorecards/approved-suppliers/` and `PUT /supplier-scorecards/approved-suppliers/{asl_id}` (read-only history since migration 123) | `is_api_token_credential` | **403**, the same text; also not MCP tools (`catalog.EXCLUDED_OPERATIONS`) | Review follow-up of the same change: an agent's "approved" entry there would contradict the vendor's own ASL status. People's use of the two routes is unchanged |
| **Granting vendor approval by the checkbox** — `POST /purchasing/vendors` with `is_approved: true`, `PUT /purchasing/vendors/{id}` raising `is_approved` from `false` to `true`, and `POST /purchasing/vendors/import-csv` when any row asks for `is_approved` (refused before any row is written, dry run included). A token may still create and edit vendors, re-send `true` on an approved one, and withdraw approval | `is_api_token_credential` | **403** (`Approving a supplier is a person's decision: an API token cannot mark a vendor approved. Sign in to the app to do this.`) | Review follow-up of the same change: the checkbox is the approval that puts a vendor not on the ASL into the Create PO picker. Not an owner ruling |
| **Completing a maintenance work order with checklist items unticked** — `POST /maintenance/work-orders/{id}/complete` when any checklist item the work order carries (or the body sends) is left unticked — or, on a PM work order, any step of its PM schedule's checklist as snapshotted when the work order was opened (migration `132`; an open work order opened before `132` is judged on the schedule's current checklist) — whatever `incomplete_checklist_reason` it sends. Refused before the reason check and before any write. A token's completion with every item ticked (or no checklist) is unchanged, and so is a person's completion with a reason | `is_api_token_credential`, inline in `complete_work_order` | **403** `API_TOKEN_INCOMPLETE_CHECKLIST` (`Completing a maintenance work order with checklist items unticked must be done by a person signed in to the app, not an API token.`; text in `app/services/maintenance_checklist.py`) | Owner decision 2026-11-17, under the 2026-11-07 principle: why a PM step was skipped is a person's judgment, and a completion resets the machine's PM clock for a full interval |
| **Taking items off a maintenance work order's checklist** — `PUT /maintenance/work-orders/{id}` whose `checklist_results` drops, renames or clears an item an **open** work order carries (matched on normalized text, case-insensitively, so a change of spacing, leading bullet or case is not a removal; ticking, unticking or adding is unchanged; on a completed or cancelled work order every change is already **409**, for every credential, before this check). Refused before the first write | `is_api_token_credential`, through `maintenance_checklist.removed_checklist_items` | **403** `API_TOKEN_CHECKLIST_REMOVAL` (`Removing items from a maintenance work order's checklist must be done by a person signed in to the app, not an API token.`) | **This PR's extension** of the owner's 2026-11-17 decision: without it a token reaches the refused completion in two steps (PUT the unticked items away, then complete) |
| **Taking items off a PM schedule's checklist while it has an open work order with no checklist snapshot** — `PUT /maintenance/schedules/{id}` whose `checklist` drops, renames or clears a step (matched as on the work order: a change of spacing, leading bullet or case is not a removal) while a scheduled / overdue / in-progress / on-hold work order opened **before migration `132`** carries the schedule's id (it has no snapshot, so completion judges it on the schedule's current steps). The PM import applies the same rule: on `POST /maintenance/schedules/import/preview` / `/commit`, a sheet that takes steps away from such a schedule gets a row error on every row of that schedule, so the commit is **409** with nothing written. Adding steps, editing a schedule with no open work order, every other field of one that has an open work order (`next_due_date` included: see the PM due-date row below), and (since 2026-11-20) removing steps while the open work order carries a snapshot — the removal binds the next work order — are unchanged | `is_api_token_credential`, inline in `update_schedule` (`_open_work_order_for_schedule(..., judging_live_checklist=True)`) and in the import's `_flag_open_work_orders` | **403** `API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL` (`Removing items from the checklist of a PM schedule whose open work order is still judged on the schedule's current checklist (it was opened before checklist snapshots) must be done by a person signed in to the app, not an API token.`); on the import, that text as the row error, plus the work order's number | **This PR's extension** (2026-11-17), for the same reason: the third route to the same skip. **Narrowed 2026-11-20** with the checklist snapshot (owner decision): a snapshotted work order is judged on its own copy, so a schedule edit cannot skip its steps; only an open pre-`132` work order still follows the schedule's steps |
| **Posting, editing or deleting a comment, or watching / unwatching a record** — `POST /comments`, `PATCH /comments/{comment_id}`, `DELETE /comments/{comment_id}`, `POST /watchers`, `DELETE /watchers`. Refused in the route's first dependency, before the body is validated or anything is read or written. None of the five is an MCP tool (`catalog.COMMENT_WRITE_OPERATIONS`, part of `EXCLUDED_OPERATIONS`), so a person's own JWT cannot reach them through an agent either. The reads (`GET /comments`, `GET /watchers`, `GET /users/mention-search`) stay open to a token the read gates admit | `is_api_token_credential`, in the `comment_writer` dependency (`app/api/endpoints/comments.py`) | **403** `API_TOKEN_COMMENT_WRITE` (`A comment is a person's own words: an API token cannot post, edit or delete comments or change who is watching a record. Sign in to the app.`) — any token, whatever its role or scope (a read-only or AI-external token meets its own gate's 403 first) | Owner decision 2026-11-16 (work-order comments): a comment is a person's own words, and a watch a person's own preference |
| **Setting, clearing or unlocking a user's floor PIN, or turning the company's badge sign-in PIN policy on or off** — `PUT /users/{user_id}/floor-pin`, `DELETE /users/{user_id}/floor-pin`, `POST /users/{user_id}/floor-pin/unlock` (Admin, or a Manager for Operator / Supervisor / Shipping / Viewer accounts; see [Users](#users)) and `PUT /companies/me/floor-pin-policy` (Admin). Refused at the top of each route (after the role gate), before the target user or the company is read. None of the four is an MCP tool (`catalog.FLOOR_PIN_OPERATIONS`, part of `EXCLUDED_OPERATIONS`), so a person's own JWT cannot reach them through an agent either. Reading a user's PIN state (`has_floor_pin` on the user record) and the policy (`require_floor_pin` on `GET /companies/me`) is unchanged. The PIN writes' `FLOOR_PIN_SET` / `FLOOR_PIN_CLEARED` / `FLOOR_PIN_UNLOCKED` rows and the policy flip's UPDATE and STATUS_CHANGE rows are **required** evidence (**503**, nothing saved, when one cannot be written) | `is_api_token_credential`, inline at the top of each route | **403** `API_TOKEN_FLOOR_PIN_ADMIN` (`"A floor PIN, and the company's badge-sign-in PIN policy, are set by a person signed in to the app, not an API token: credential administration is a person's act."`) — any token, whatever its role or scope (a read-only or AI-external token meets its own gate's 403 first). A crew-station badge token never reaches these routes (its path fence keeps it on the shop floor) | The floor-PIN change's design (AS9100D 8.5.2, migration `130`, 2026-11-19); not an owner ruling |
| **Posting a note through Hank** — the `add_note` task, a work-order comment, at `POST /hank/tasks` (prepare) and `POST /hank/tasks/{id}/execute`. Refused before the generic role check and before anything is written, and `GET /hank/capabilities` never offers the kind to a token. **At the MCP door the rule looks past the credential**: `prepare_task` with kind `add_note`, and `execute_task` on an `add_note` task (read first as the caller), answer **403** `"A note is a person's own words: post it from the app, not through an agent."` to an agent on **any** credential, a person's own JWT included (a pre-read that cannot complete is **503**, nothing executed) — `app/mcp/people_only.py` | `is_api_token_credential`, in `hank_task_service._refuse_api_token_for_people_only` over `credentials.PEOPLE_ONLY_HANK_TASK_KINDS` (the MCP package reads the same set) | **403** `API_TOKEN_COMMENT_WRITE` (`A comment is a person's own words: an API token cannot post, edit or delete comments or change who is watching a record. Sign in to the app.`) — any token, whatever its role or scope | Owner decision 2026-11-16 (work-order comments): a comment is a person's own words, so Hank's note takes the comment routes' rule |
| **Changing a document's access level** — `POST /documents/{id}/access-level` (`standard` / `restricted` / `export_controlled`, applied to the document's whole revision history). Refused before anything is read. Not an MCP tool (`catalog.DOCUMENT_ACCESS_OPERATIONS`, part of `EXCLUDED_OPERATIONS`), so a person's own JWT cannot reach it through an agent either: an agent must not lower an export-controlled drawing to `standard`. The access review (`GET /documents/access-review`, the MCP tool `get_document_access_review`) and every document read stay open to a token the read gates admit | `is_api_token_credential`, inline at the top of the route (after the Admin / Quality role gate) | **403** `API_TOKEN_DOCUMENT_ACCESS_LEVEL` (`A document's access level is changed by an Admin or Quality user signed in to the app, not an API token: deciding who may open a controlled document is a person's judgment.`) — any token, whatever its role or scope (a read-only or AI-external token meets its own gate's 403 first) | Design decision of the restricted document viewing change (2026-11-19, migration `131`), applying the egress-switch reasoning of 2026-11-15; not an owner ruling |
| **Granting or revoking a user's export authorization** — `PUT /users/{id}/export-authorization`, the only thing that opens an `export_controlled` document. Refused before anything is read. Not an MCP tool (`catalog.DOCUMENT_ACCESS_OPERATIONS`): an agent on a person's JWT must not grant export access, its own user's included. Reading a user's authorization (the user record) stays open | `is_api_token_credential`, inline at the top of the route (after the Admin role gate) | **403** `API_TOKEN_EXPORT_AUTHORIZATION` (`Export authorization is granted or revoked by an Admin signed in to the app, not an API token: deciding who may open export-controlled documents is a person's judgment.`) — any token, whatever its role or scope | The same 2026-11-19 design decision; not an owner ruling |
| **Changing your own password** — `POST /users/change-password` (any signed-in person, My Settings → Password). Refused before the body's passwords are read. Not an MCP tool (`catalog.PASSWORD_OPERATIONS`, part of `EXCLUDED_OPERATIONS`): an agent on a person's own JWT must not change that person's password. The change, its `password_changed_at` stamp and the `PASSWORD_CHANGE` audit row are one transaction — the audit row is **required** evidence (**503**, nothing saved). It does not end the person's other signed-in sessions (refresh does not read `password_changed_at`; they last until sign-out or the 7-day idle window) | `is_interactive_person`, inline at the top of the route | **403** `PASSWORD_CHANGE_PEOPLE_ONLY` (`Your password is changed by you, signed in to the app: an API token or a shop-floor badge or station token cannot change a password.`) — every API token whatever its role or scope, and every crew-station badge or station token | The self-service password change's design (2026-11-23); not an owner ruling |
| **Signing off an in-process inspection** — `POST /shop-floor/operations/{operation_id}/inspection`, the only writer of `WorkOrderOperation.inspection_complete` (the generic `PUT /work-orders/operations/{id}` refuses the field **422**). It records that an inspection was performed, under the signer's name, as a **required** `MARK_OPERATION_INSPECTED` audit row (**503**, nothing signed, when it cannot be written). Refused after the role gate and before the operation is read, so a token cannot probe whether an id exists. Not an MCP tool (`catalog.INSPECTION_SIGN_OFF_OPERATIONS`, part of `EXCLUDED_OPERATIONS`), so a person's own JWT cannot reach it through an agent either. A crew-station badge token, which passes `is_api_token_credential`, is refused **403** by the kiosk path fence instead (`deps._is_kiosk_scope_allowed_path`), as labor approval is, whatever badge was scanned. The operation reads carrying `requires_inspection` / `inspection_complete` / `inspection_type` stay open | `is_api_token_credential`, inline at the top of the route (after the role gate) | **403** `API_TOKEN_INSPECTION_SIGN_OFF` (`An inspection is signed off by a person signed in to the app, not an API token: recording that an inspection was performed is a person's judgment.`) — any token, whatever its role or scope (a read-only or AI-external token meets its own gate's 403 first) | The inspection sign-off change (2026-11-23): the improvement plan's recommended default, matching the FAI final disposition; not an owner ruling. Fencing the badge token off is the same change's review follow-up |

Deliberately **not** refused:

- A **crew-station badge token** is a person standing at the kiosk with their own badge, so it
  passes every `is_api_token_credential` rule above. For declaring scrap and closing work short that
  is the owner's decision (it names the crew badge: "kiosk, crew badge, desk"); for the count-judgment
  and scrap-reason rows it is this PR's reading. (`is_interactive_person` refuses it, and its path
  fence keeps it on `/api/v1/shop-floor` anyway.) The in-process inspection sign-off is the one
  shop-floor row above it cannot reach: the path fence denies that route to every badge token.
- An agent presenting a **person's own interactive access JWT** (an MCP client signed in as that
  person) is that person's session to both predicates and is not refused — current behavior, not an
  owner decision. The 2026-11-07 decision
  covered standing API tokens only; whether an agent on a person's JWT may make these writes was
  not part of it. The exception is the Hank note row above: the MCP door refuses `add_note` to an
  agent on any credential (owner decision 2026-11-16), and writes that are not MCP tools at all
  (`catalog.EXCLUDED_OPERATIONS`) are out of an agent's reach whatever it carries.
- **Not yet decided (owner question):** a CAR status move through `PUT /quality/car/{id}` to
  `verification` or `closed` stamps `verified_by` / `closed_by` with the token's user, and is not
  refused for a standing API token today. It is the same kind of write as the rows above; whether
  it gets the same rule is open. Since migration `127` a token can close a corrective CAR only once
  a person has recorded an `effective` effectiveness verification, and a `rework` / `repair` NCR
  only once a person has recorded a `conforming` re-verification; closing either is still not
  refused as such.
- Scrap fields that are not production scrap records: `POST /fabrication-quotes/{quote_id}/actuals`
  (the MCP tool `record_actuals`) takes a `scrap_quantity` but stores it as a quoting-calibration
  observation (`FabricationQuoteActual.observation_json`), and a BOM line's `scrap_factor` is a
  planning allowance. Neither is refused. `tests/api/test_api_token_scrap_refusal.py` lists both as
  exempt with their reasons, and fails CI on any new scrap writer that is neither guarded nor
  listed.

The role gate still applies after the credential check: the person must hold a role that could
apply the action. `hank_task_service.py` and its siblings carry older inline copies of the stricter
predicate.

Personal presentation writes are not judgment writes and are deliberately absent from the tables
above: saving or resetting your own Hank preferences (`PUT /hank/preferences`,
`POST /hank/preferences/reset`) or dashboard layout (`PUT /dashboard/layout`,
`POST /dashboard/layout/reset`) requires an interactive person — `is_interactive_person` for the
dashboard layout, the equivalent clauses inline in `HankPreferenceService.can_edit` — plus the
read-only company fence, while any credential that reaches the route may read them (see
[Dashboard](#dashboard) and [Hank](#hank-ai-chat-and-pdf-filing)).

Judgment-shaped writes deliberately left open, so no predicate applies (the role gate and the
route's own fences still do):

| Write | Open to | Decision |
|---|---|---|
| **Resume Work Order** — `POST /work-orders/{id}/resume` (lifts a work-order hold) | Any credential of a user holding Admin / Manager / Supervisor, a standing **API token** included (the MCP tool `work_orders_resume_work_order`); kiosk badge and station tokens are refused by their path fence, as on every `/work-orders` route. Each call's required audit rows are written as the token's user with `extra_data.credential` | Owner decision 2026-11-07: an agent may lift a work-order hold |
| **Release** — `POST /work-orders/{id}/release` and `POST /work-orders/bulk-release` (puts DRAFT work on the floor) | Any credential of a user holding Admin / Manager / Supervisor, a standing **API token** included; read-only and AI-external tokens are refused by the global gates, kiosk badge and station tokens by their path fence. Each release's audit row carries `extra_data.credential` | Existing behavior, not an owner ruling: the MCP tool `release_work_order` is an agent's release door, one job per call; bulk release is kept off the MCP catalog only. Whether a standing token should bulk-release is an open owner question |
| **Dismiss**, snooze and feedback on a recommendation, action cards included | Every authenticated user and every credential | A design decision of the AI governance change; for Dismiss on an action card, confirmed by the owner 2026-11-07 (it does not take the Mark handled gate) — see [AI recommendations (Action Inbox)](#ai-recommendations-action-inbox) |
| **Moving a work order's due date through Hank** — the `change_due_date` task (`POST /hank/tasks`, `POST /hank/tasks/{id}/execute`; the MCP tools `prepare_task` / `execute_task`) | Any credential of a user holding Admin / Manager / Supervisor with `work_orders:view` + `work_orders:edit`, a **full** API token included; read-only and AI-external tokens are refused by their own gates, as on every write. Each execution's required audit row carries the reason and `via: hank` (and `extra_data.credential` under a token) | Owner decision 2026-11-16: parity with `update_work_order` / `PUT /work-orders/{id}`, which already moves `due_date` for a token. **Open owner follow-up:** may agents move due dates at all? |
| **Editing the supplier quality flow-down settings or clause catalog** — `PUT /purchasing/flowdown-profile`, `POST /purchasing/quality-clauses`, `PUT /purchasing/quality-clauses/{id}` (the MCP tools `update_po_flowdown_profile`, `create_po_quality_clause`, `update_po_quality_clause`) | Any credential of a user holding Admin / Manager / Quality, a **full** API token included; read-only and AI-external tokens are refused by their own gates. Each write's required audit row carries `extra_data.credential` under a token | Owner decision 2026-11-20: a full API token may edit the supplier quality flow-down settings and clause catalog; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate); the write is attributed to the token's bound user with `extra_data.credential`. (Until then it was an implementation decision of the PO flow-down change, migration `128`, 2026-11-19.) For comparison, the Approved Supplier List writes refuse API tokens. A PO that has left draft keeps the block frozen onto it, so an edit reaches draft POs only. See [Purchasing](#purchasing) |
| **Recording or changing who owns material** (AS9100D 8.5.3) — `POST /inventory/items/{item_id}/ownership` (the MCP tool `change_inventory_item_ownership`) re-owns one stock row as company, customer or supplier property, which decides whether automatic consumption may draw it and whether valuation counts it; `POST /inventory/receive`, `POST /receiving/receive` and the lines of `POST /receiving/deliveries` record the owner at receipt (the receipt carries it to stock through inspection) | Any credential of a user holding Admin / Manager / Supervisor (plus receiving view / create permission on deliveries), a **full** API token included; read-only and AI-external tokens are refused by their own gates. Hank's `receive_delivery` task has no ownership fields, so Hank receives company stock only. The ownership change's required audit row carries the reason, and every audit row written under a token carries `extra_data.credential` | Owner decision 2026-11-20: a full API token may record or change stock ownership, through the ownership verb and at receipt; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate); the write is attributed to the token's bound user with `extra_data.credential`. (Until then it was an implementation decision of the owned-property change, migration `129`, 2026-11-19.) Hank's company-only receipt narrows Hank's task input, not the credential policy. See [Inventory](#inventory) |
| **Verifying QMS clause evidence** — `PUT /qms-standards/evidence/{evidence_id}` with `is_verified` (the MCP tool `update_evidence`). A Verify stamps `verified_by` with the caller's user id and `verified_date`; a verified link is never retired by auto-link and counts as verified evidence in audit readiness | Any credential of a user holding Admin / Manager / Quality, a **full** API token included; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate). The route's `UPDATE` audit row is required evidence (**503**, nothing saved, when it cannot be written), lists the `is_verified` change, and carries `extra_data.credential` under a token | Owner decision 2026-11-20: a full API token may verify QMS clause evidence; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate); the write is attributed to the token's bound user with `extra_data.credential`. `verified_by` names that user too, so the audit row's `extra_data.credential` is what tells a token's Verify from the person's own |
| **Moving a PM schedule's next due date** — `PUT /maintenance/schedules/{schedule_id}` with `next_due_date` (the MCP tool `update_schedule`), including while the schedule has an open work order. The date also moves through the PM import's commit (`POST /maintenance/schedules/import/commit`, the MCP tool `commit_pm_schedule_import`, already open to tokens: see [Maintenance](#maintenance-pm-schedules-maintenance-work-orders-event-log)) and through the roll-forward when `POST /maintenance/work-orders/{wo_id}/complete` (the MCP tool `preventive_maintenance_complete_work_order`) completes a work order carrying a `schedule_id` | Any credential of a user holding Admin / Manager / Supervisor (completion also Operator), a **full** API token included; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate). A token's only refusals on these routes are the checklist rows above: taking steps off a schedule whose open work order has no checklist snapshot (on the import, a row error), and completing with checklist items unticked. The direct edit's `maintenance_schedule` UPDATE row is best-effort (as is the completion roll-forward's; the import's is required) and carries `extra_data.credential` under a token | Owner decision 2026-11-20: a full API token may move a PM schedule's `next_due_date`; read-only and AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate); the write is attributed to the token's bound user with `extra_data.credential` |
| **Filing a maintenance log entry** — `POST /maintenance/log` (the MCP tool `create_log`): an `inspection`, `repair`, `part_replacement`, `observation` or `note`, on a machine or asset or against a maintenance work order (open or finished) | Any credential of a user holding Admin / Manager / Supervisor / Operator, a **full** API token included, and an MCP agent on a person's own JWT (`create_log` stays a tool); read-only and AI-external tokens are refused by their own gates. The entry's `performed_by` is the credential's user. **A token's entry says so on the record itself** (migration `133`): `maintenance_logs.api_token_id` is set, every log read serves `filed_via_api_token: true`, and the Maintenance timeline tags it *via API token*; its audit row also carries `extra_data.credential` (the only token record for an entry filed before `133`). An agent's entry on a person's JWT carries no marker and is attributed to that person. The `maintenance_log` CREATE audit row is **required** evidence (**503**, nothing saved). What any caller cannot do through it is fixed by the route: `started` and `completed` entries are the work order's Start / Complete verbs' own (**422**), and an entry on a work order is filed under that work order's machine or asset (**409** otherwise) | Owner decision 2026-11-20 for standing API tokens. **The MCP-agent-on-a-person's-JWT half was not separately put to the owner** (to be confirmed with them): it follows from `create_log` remaining a tool. Note the contrast with work-order comments, which every API token is refused (`API_TOKEN_COMMENT_WRITE`), and with comments and Hank's `add_note`, which the MCP door refuses to an agent on any credential (owner decision 2026-11-16: a comment is a person's own words); maintenance log entries are decided the other way on both counts. See [Maintenance](#maintenance-pm-schedules-maintenance-work-orders-event-log) |

## Permission Matrix

### Continuous Improvement

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View suggestions and activity | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Submit, edit, assign, change status, and comment | ✓ | ✓ | | | | | |

The `/continuous-improvement` page and API are company-scoped. Platform Admin and
superuser privileges follow the shared role gate; a switched read-only company
context still refuses writes. Owners must be active managers or above in the same
company. Shared kiosk credentials cannot access this section. Every submission,
edit, status change, and comment records its actor and a server-generated timestamp.
See [Continuous Improvement](CONTINUOUS_IMPROVEMENT.md) for the workflow.

### Work Orders

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| View deleted work orders (the restore view) | ✓ | ✓ | | | | | |
| Restore a deleted work order | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | ✓ | | | | |
| Release several drafts at once (`POST /work-orders/bulk-release`, 1–50 per call) | ✓ | ✓ | ✓ | | | | |
| Start work order (released only) | ✓ | ✓ | ✓ | | | | |
| Hold work order (`PUT` `status: on_hold`; released / in-progress only) | ✓ | ✓ | ✓ | | | | |
| Resume work order (lift a work-order hold) | ✓ | ✓ | ✓ | | | | |
| Start operation (office verb) | ✓ | ✓ | ✓ | | ✓ | | |
| Start operation (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| Complete (office verb) | ✓ | ✓ | ✓ | | ✓ | | |
| Complete (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| Hold / resume operation (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Resolve / dismiss the blocker behind a hold | ✓ | ✓ | ✓ | | | | |
| Approve labor (TimeEntry) | ✓ | ✓ | ✓ | | ✓ | | |
| View material ties | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Tie / edit / untie material | ✓ | ✓ | ✓ | | | | |
| View work order templates | ✓ | ✓ | ✓ | | | | |
| Save / rename / delete a work order template | ✓ | ✓ | ✓ | | | | |
| Use a work order template (creates DRAFT WOs — up to 20 per call) | ✓ | ✓ | ✓ | | | | |

> **Delete — code now matches the matrix (Admin + Manager).** `DELETE /api/v1/work-orders/{id}`
> (`app/api/endpoints/work_orders.py`) was previously gated **stricter than this matrix** —
> `require_role([ADMIN])` — while the **Delete** row above already listed Admin **and** Manager. The
> gate is now `require_role([ADMIN, MANAGER])`, so a Manager can soft-delete a work order as documented.
> Soft/hard-delete and restore behavior is otherwise unchanged (default soft delete; `hard_delete=true`
> only for draft/cancelled WOs). See `docs/API.md` → Work Orders.

> **Seeing a deleted work order — a deliberate widening for Managers.** `GET /api/v1/work-orders/`
> gained `deleted_only=true`, the restore view behind Work Orders → **Deleted**, gated **Admin +
> Manager** by an in-handler check mirroring `require_role([ADMIN, MANAGER])` clause for clause
> (`is_superuser` and `PLATFORM_ADMIN` admitted the same way it admits them). **Before this, no
> Manager could see a deleted work order at all** — the pre-existing `include_deleted=true` on the same
> endpoint is **Admin-only** and is unchanged. That was incoherent: the **Delete** and **Restore** rows
> above are both Admin + Manager, so a Manager could delete a work order and then had no screen that
> would list it back. The new gate matches those two rows exactly, so a hidden tab and a refused call
> agree.
>
> It discloses no new *class* of data: the live work-order list is role-open to **every** authenticated
> user (the **View** row above), Managers could read the same rows before they were deleted, and the
> `audit_log` DELETE row — already Admin + Manager via `GET /audit/` — carries the same actor and
> timestamp the view now surfaces as `deleted_by_name` / `deleted_at`.
>
> **This diverges from the Vendors and Purchase Orders restore views on purpose.** Those leave
> `deleted_only=true` on `get_current_user` (see → Purchasing), reasoning that the archive returns rows
> the same reader could already see. The reasoning transfers; the situation does not. Work orders
> already ship an Admin-only `include_deleted`, and an ungated `deleted_only` would let any
> authenticated reader fetch the deleted set and merge it client-side — silently repealing that gate
> instead of deciding to change it. If that Admin-only gate is ever revisited, revisit this one with it
> rather than letting the two drift.

> **Approve labor — endpoint mapping (Batch 11B / G5-A).** The shop-floor labor sign-off
> `POST /api/v1/shop-floor/time-entries/{id}/approve` and `…/unapprove` (which set / clear
> `TimeEntry.approved` + `approved_by`, the field the opt-in `REQUIRE_APPROVED_LABOR_FOR_COST` flag
> keys labor-cost rollups on) are enforced **in code** to this row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`). In
> addition to the role gate, **self-approval is
> forbidden**: a user cannot approve or unapprove their **own** TimeEntry (segregation of duties for
> the labor-cost gate) — that returns **403** even for an approver-role user. A cross-tenant id returns
> **404**. Both actions are audited (`time_entry_approve` / `time_entry_unapprove`).

> **Start operation — both start verbs are role-gated** (the office verb since `feat/work-center-op-pool`,
> the shop-floor verb since owner decision P0.4, 2026-11-13).
> The **Start operation** row above is split across two endpoints exactly like the **Complete** row is:
> - `POST /api/v1/work-orders/operations/{id}/start` — the **office** verb — is enforced in code to
>   `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`. It previously had **no role gate at all**
>   (bare `get_current_user`), so any authenticated tenant user — **Viewer and Shipping included** —
>   could stamp `actual_start` / `started_by`, move the work order to IN_PROGRESS, and write rows onto
>   the tamper-evident chain from a page they were only meant to read. That gap predates this work; it
>   was masked while the office path ran a **stricter** predecessor gate than the shop floor and so
>   refused nearly every operation such a user could reach. With same-work-center operations promoting
>   to READY together on a pooled work order (`docs/API.md` → Work Orders → "READY promotion: a sequenced ROUTING or a DISPATCH POOL"), the
>   operations a Viewer can reach are exactly the ones the gate no longer refuses, so the hole became
>   reachable. The gate **matches its office twin** `…/operations/{id}/complete` —
>   being able to start an operation but not complete it, or the reverse, is incoherent — which is why
>   QUALITY is included here for the same reason it is there.
> - `PUT /api/v1/shop-floor/operations/{id}/start` — the **operator** verb — is
>   `require_role([ADMIN, MANAGER, SUPERVISOR, OPERATOR, QUALITY])` (`_FLOOR_OPERATION_ROLES` in
>   `app/api/endpoints/shop_floor.py`), per the note directly below. Operators start work there and
>   on the kiosk, never through the office page, so **no operator lost a capability**; the Operator ✓
>   on the Start operation row is that verb.
>
> **The two shop-floor operation verbs are role-gated; the hole they left is narrowed, not closed
> (owner decision P0.4, 2026-11-13).** `PUT /shop-floor/operations/{id}/start` and
> `POST /shop-floor/operations/{id}/complete` used to take a bare `Depends(get_current_user)`, so a
> **Viewer** or **Shipping** user could start and complete an operation — closing it at the good
> count recorded on the floor, receiving finished goods, consuming tied material and writing chain
> rows. Both now take `require_role([ADMIN, MANAGER, SUPERVISOR, OPERATOR, QUALITY])`: the
> `work_orders:complete` set, and Hank's `report_production` set. Viewer and Shipping get **403**
> `Insufficient permissions` before the operation is read or the body is validated, so no
> existence oracle is left either. Superuser and Platform Admin pass as everywhere. Both kiosk
> credentials carry the badge holder's **live** role, so operators are unchanged:
> - the crew station's badge-minted `scope="kiosk"` token;
> - the single-operator `/kiosk` employee-login token.
>
> The badge mint (`POST /auth/kiosk-badge-token`) stays role-blind, so a Viewer or Shipping badge
> at the crew station still mints and is then refused at complete. If that badge's production
> report went first, the report is saved and the close is refused.
>
> **This narrows the hole; it does not close it.** These routes still take a bare
> `get_current_user`, so a Viewer can still start and complete work through them:
> - `POST /shop-floor/clock-in` starts a PENDING / READY operation (IN_PROGRESS, `actual_start`,
>   `started_by`, the work-order lift) for any authenticated user;
> - `POST /shop-floor/clock-out/{id}` completes the operation on a full good count, running the
>   same finalize and inventory seam;
> - `POST /shop-floor/operations/{id}/production`, `…/reduce-production`, `PUT …/hold` and
>   `PUT …/resume`.
>
> The labor-evidence gate on floor completion is a record-quality check, not an authorization one.
> Gating clock-in and the full-count auto-complete is a further owner decision, not taken. A
> **read-only API token** is refused every one of these writes whatever its user's role (see
> [MCP / agent access](#mcp--agent-access)), so an agent's read-only token cannot use this path.
>
> No role gained anything. The office operation-start verb has **no UI caller today** — the floor's
> operation Start / Check In controls (`ShopFloorSimple`, the Time Clock, both kiosks) call
> `POST /shop-floor/clock-in` or the shop-floor start verb, and Work Order detail's **Start** button is
> the *work-order* start (`POST /work-orders/{id}/start`, below), not this verb — so the gate closes an
> API-reachable hole rather than hiding a button; wire any future office operation-Start control to
> `work_orders:edit` so the hidden control and the refused call agree. See `docs/API.md` → Work Orders.

> **Start / Hold / Resume work order — Admin / Manager / Supervisor (owner ruling 2026-11-05).**
> - `POST /api/v1/work-orders/{id}/start` is now enforced in code to
>   `require_role([ADMIN, MANAGER, SUPERVISOR])`. It previously took a bare `get_current_user`, so
>   **any** authenticated tenant user — Operator, Quality, Shipping and Viewer included — could move a
>   released work order to IN_PROGRESS **and lift a work-order hold** with it (the verb accepted
>   ON_HOLD). Those four roles **lose** that capability on purpose: the verb now accepts RELEASED only
>   and refuses 409 on a draft or held job.
> - **Hold work order** is the existing `PUT /api/v1/work-orders/{id}` (already Admin / Manager /
>   Supervisor) with `status: on_hold`; it now refuses 409 on a **draft**, so a hold is only ever
>   placed on released work.
> - **Resume work order** is the new `POST /api/v1/work-orders/{id}/resume`,
>   `require_role([ADMIN, MANAGER, SUPERVISOR])`, with required (fail-closed) audit rows. It never
>   releases a job: with no release on record the job returns to DRAFT and goes back through
>   **Release**.
>   It is also the MCP tool `work_orders_resume_work_order`, and a standing per-user API token
>   holding one of those roles may call it — the same posture as `release_work_order` (**owner
>   decision 2026-11-07**: an agent may lift a work-order hold; each call is audited as the token's
>   user with `extra_data.credential`).
>
> Work Order detail shows **Start** (released), **Hold Work Order** (released / in progress) and
> **Resume Work Order** (on hold) only to users holding `work_orders:edit` (or a superuser) — the same
> Admin / Manager / Supervisor set — so a hidden control and a refused call agree; `customPermissions`
> can still diverge at runtime, and the server stays authoritative (a mismatch surfaces as the
> verbatim 403). Superuser and Platform Admin pass through `require_role` as everywhere else. See
> `docs/API.md` → Work Orders → "Draft and on-hold work orders refuse every start".

> **Release — one work order or several, and the Work Orders list (endpoint mapping).**
> `POST /api/v1/work-orders/{id}/release` and `POST /api/v1/work-orders/bulk-release` are both
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`require_role` itself admits superuser and Platform
> Admin), so the **Release** row and the bulk row above are one gate. Bulk release grants nothing the
> single route did not: each id is judged and released exactly as the single route would (DRAFT and at
> least one operation — there is no material or blocker gate), scoped to the active company (another
> company's id is the same `Work order not found` result as a deleted or absent one, with no number),
> and each released work order gets its own audit row attributed to the caller. No role gained or lost
> a capability: the bulk route does nothing a run of single releases by the same user could not.
>
> **The Work Orders list and the Work Order detail page now hide Release from roles that cannot
> release.** The list's row **Release** button (desktop table and mobile cards), the bulk-release
> checkboxes and the detail page's **Release** button render only for a user whose role holds
> `work_orders:release` (Admin, Manager, Supervisor, Platform Admin) or a superuser — the set the
> endpoints admit — through one shared check, `userCanReleaseWorkOrders` in
> `frontend/src/utils/permissions.ts`. Before this, both buttons rendered for **every** role, and an
> Operator, Quality, Shipping or Viewer user who clicked one got the 403. Checkboxes appear on
> **draft** rows only, on the desktop table only (none on the mobile card list), and select-all covers
> the drafts loaded on the current page. As above, `customPermissions` can still diverge at runtime
> and the server stays authoritative.
>
> **Not an MCP tool.** Bulk release is in `catalog.EXCLUDED_OPERATIONS`, so an agent releases one job
> per call through `release_work_order`. That is an exclusion from the agent catalog, not a credential
> refusal: the route has no API-token refusal of its own, so a full API token whose user holds one of the three roles
> can call it directly on `/api/v1`, as it can the single release (a read-only token is refused as for
> every write). See `docs/API.md` → Work Orders → "Releasing several work orders at once".

> **The shop-floor verbs stay open to operators (clock-in to any authenticated user; the start verb
> to the floor roles since P0.4), but now refuse draft and on-hold work orders.**
> `POST /shop-floor/clock-in`, `PUT /shop-floor/operations/{id}/start` and the READY auto-start inside
> both operation-complete verbs refuse **409** for **every** role — Admin included — when the
> operation's work order is DRAFT ("release first") or ON_HOLD ("Resume Work Order"). This is a
> state gate, not a role gate: no role cell above changed, and a supervisor's only way past it is the
> audited door (**Release** or **Resume Work Order**). The office verbs carry the same state gate:
> `POST /work-orders/operations/{id}/start`, and the generic `PUT /work-orders/operations/{id}`
> (Admin / Manager / Supervisor) when it hand-sets `status: in_progress` — or `status: ready` on a
> draft, or raises `quantity_complete` on a not-started operation — so the generic edit cannot stand
> in for the start verb it sits beside (answered before its open-operation count 409s, so the refusal
> names Release / Resume rather than a door the job keeps shut). Paper back-entry has no exemption. Labor already running when a job is held stays closable (clock-out, production
> report, completing an IN_PROGRESS operation).

> **Short count at completion (owner ruling 1, 2026-11-05) — state and credential rules; no role
> cell changed.** A short count at operation completion is reported as scrap with a reason, or the
> operation stays open; it is never closed at the counted quantity. The role gates are exactly the
> rows above — what changed is what each verb will close, and for whom:
> - **Complete (shop-floor verb)** — Admin / Manager / Supervisor / Operator / Quality (P0.4; see
>   the note above). It is now a **close**:
>   it books the good already recorded on the floor and refuses **409**, writing nothing, while any
>   received unit is unaccounted. It credits nothing to anyone's time entry, and a short close
>   with someone else still clocked in needs `acknowledge_open_entries` (their unreported pieces will
>   not be counted).
> - **Complete (office verb)** — still Admin / Manager / Supervisor / Quality. It closes only when
>   good + scrap cover every unit that reached the operation (otherwise it saves progress), recording
>   more good than the floor's labor evidence needs a `quantity_override_reason` written as required
>   audit evidence, and a short close with anyone clocked in needs `acknowledge_open_entries`. An
>   operation **nobody ever clocked into** still closes from the desk with no reason — the owner's
>   office-exempt asymmetry with the shop-floor labor gate is unchanged.
> - **Force-complete** (`POST /work-orders/{id}/complete`, Admin / Manager / Supervisor / Quality) —
>   bounded by what the floor recorded (an override reason to exceed it, required audit) and refused
>   when the shortfall is not covered by scrap with a reason.
> - **Correcting a completed operation** — the office reduce-production verb stays **Admin / Manager
>   / Supervisor** (the existing gate), and is now the only way to correct a COMPLETE operation's
>   count: a correction that would leave it short must send `reopen_operation` (it returns to in
>   progress, with required audit rows). On an operation nobody ever clocked in to it lowers the
>   desk-entered good count directly (there is no labor to walk). The operator's twin refuses
>   `reopen_operation` (**422**), and
>   `PUT /work-orders/operations/{id}` refuses good / status edits and scrap LOWERING on a COMPLETE
>   operation (**409**) — the same Admin / Manager / Supervisor gate may still ADD scrap found after
>   the close, with a reason, as required audit evidence, but only into units still unaccounted
>   (zero on an accounted close; a rise past that is **409** — on a live work order, pieces counted
>   good and later found bad go through reduce-production with `reopen_operation`, then a re-close
>   with the scrap; on a complete / closed / cancelled one no operation-level correction exists, and
>   the 409 says to record them as a nonconformance (NCR) instead).
> - **An API token may not declare scrap, change a scrap reason, close work short or make the
>   related count judgments** — a credential rule, not a role rule; no role cell changed. A standing
>   per-user API token (`app.core.credentials.is_api_token_credential`) is refused **403** on those
>   writes whatever its user's role, and a crew-station badge token is a person and passes. Which
>   routes, which 403 text, and which parts the owner decided on 2026-11-07 (scrap, short closes)
>   versus which this PR extends (scrap reasons, the other count judgments, every reduce-production
>   correction) are listed once, in
>   [Credential policy for judgment writes](#credential-policy-for-judgment-writes).
>
> See `docs/API.md` → Work Orders → "Short count at completion".

> **Hold / resume are operator-facing; resolving the blocker behind a hold is not.** Both rows above
> are new to this matrix; **neither gate changed**. `PUT /api/v1/shop-floor/operations/{id}/hold` and
> `PUT /api/v1/shop-floor/operations/{id}/resume` (`app/api/endpoints/shop_floor.py`) take a bare
> `Depends(get_current_user)` — no role gate, matching the other operator write verbs (clock-in,
> production, reduce-production; the shop-floor complete verb is role-gated since P0.4) — and are tenant-scoped (a cross-tenant id → **404**) and
> audited (resume writes a **`STATUS_CHANGE`** row carrying old→new status,
> `extra_data.transition = "resume_operation"`, and the ids of any blocker still open at resume). The
> **blocker** verbs are a tier up, and on a different router: `PUT /api/v1/work-order-blockers/{id}`
> (acknowledge / assign / dismiss) and `POST /api/v1/work-order-blockers/{id}/resolve`
> (`app/api/endpoints/work_order_blockers.py`) are `require_role([ADMIN, MANAGER, SUPERVISOR])` — the
> Work Orders **Edit** row. Being mounted outside `/api/v1/shop-floor` also puts them outside the
> kiosk path fence, so a badge-minted crew-station token is **403** there whatever badge was scanned.
>
> **That asymmetry is the design, and it is why resume is safe to leave role-open.** Resuming moves
> an *operation status*; resolving a blocker closes the *quality/material finding* that stopped the
> job, and only the second is a supervisory judgement. So resume deliberately does **not** resolve
> the blocker — it returns the still-open ones on the response (BLK-4, warn-and-record) so operation
> status and blocker status can be seen to diverge rather than diverging silently.
>
> **The other direction discloses too, and it had to be taught to.** Closing a blocker *does* resume
> its operation as a side effect — but only when no other OPEN/ACKNOWLEDGED blocker still names it,
> and only as a restore, so it can land at `PENDING` and stay off the board. Both write verbs on the
> blocker router (`POST …/resolve` and `PUT …/{id}`, which resumes identically on a **dismiss**) now
> return `operation_outcome` saying which way it went; the Work Order page raises a `warning` rather
> than a green toast when the operation is still held or came off hold into `PENDING`. Same
> principle as BLK-4 — the person who can close the finding is told whether the job actually moved —
> and **no gate moved to do it**: see `docs/API.md` → "Closing a blocker now SAYS what it did to the
> operation".
>
> **What changed is reach, not authorization.** The crew-station queue read now surfaces the work
> center's `ON_HOLD` operations (`docs/API.md` → Shop Floor → "Held work"), and resume sits inside
> the kiosk path fence on none of its deny lists, so a **badge-scanned Operator can take a job off
> hold from the shared terminal** instead of walking to a desk. The kiosk can already *place* a hold;
> a control with no inverse on the same terminal was the defect. Any authenticated user could always
> call resume — no role gained anything here either. See [docs/KIOSK.md](KIOSK.md) → Held work and
> resume.
>
> **Reach is bounded by two write-side refusals, not by a role.** Because resume is role-open, the
> limits on what it can do have to live in the endpoint: it **409**s on a cancelled (soft-deleted)
> laser nest's tombstone operation (resuming one would undo a soft delete — invariant 3), and it
> **restores rather than promotes**, flooring at `PENDING` and delegating any lift to `READY` to the
> shared promotion rule. So a role-open verb still cannot perform the **release** that
> `POST /work-orders/{id}/release` owns — a hold placed on a `PENDING` operation, or on one whose work
> order is still `DRAFT`, resumes to `PENDING` and stays off the board.
>
> **Reach widened again on the office side, and again no gate moved.** The Work Order page now
> carries a **Clear hold** action on every non-cancelled `on_hold` operation row (it previously had *no* control
> that lifted a hold — `resumeOperation` had zero call sites there, so an owner who held a nest had
> to walk to a kiosk or a shop-floor page). It calls the same role-open
> `PUT /api/v1/shop-floor/operations/{id}/resume`, so it is deliberately **not** role-gated in the
> UI: gating it would hide a control the server allows, and the matrix row above already reads ✓ for
> all seven roles. The consequence worth stating plainly is that **Viewer and Shipping now have a
> hold-lifting control on a page they can read** — not new authorization (either role could always
> call the endpoint, and `ShopFloorSimple`'s old "Check In" button already did on the same
> `work_orders:view` route), but it is now a prominent office-page button for two roles with no
> other write on that page. Narrowing it is an **endpoint** change, not a UI one; do it there or not
> at all, so the hidden control and the refused call keep agreeing.
>
> Live nest rows and their Laser Nest Package cards also expose **Hold nest**, through the
> existing authenticated `PUT /api/v1/shop-floor/operations/{id}/hold`. The card exposes
> **Clear hold** while held, using the same role-open resume verb as the operation row.
> **Cancel nest / Restore nest** remain restricted to Admin / Manager / Supervisor; temporary
> hold never deletes a nest or removes its planned quantity. Hold now rejects a cancelled
> nest with 409 so a stale screen cannot report an unrecoverable pause as successful.
>
> **The Blockers panel's Resolve button is now gated to the tier the endpoint always required.** It
> used to render for every role, so an Operator, Quality user, Shipping user or Viewer looking at a
> stuck job was offered the one control guaranteed to **403** on them — and, before Clear hold
> existed, nothing on that page could get the job moving. The button is now gated on
> `work_orders:edit`, which resolves to exactly the set `require_role([ADMIN, MANAGER, SUPERVISOR])`
> admits (plus superuser / platform admin) — i.e. the **"Resolve / dismiss the blocker behind a
> hold"** row above, unchanged. Where it is hidden the copy names who closes a blocker and points at
> Clear hold, which the reader *can* use. **No gate in
> `app/api/endpoints/work_order_blockers.py` was touched** — then or since. That file has changed
> since (both write verbs now return `operation_outcome`), but `require_role([ADMIN, MANAGER,
> SUPERVISOR])` on each of them is byte-identical to what it always was; the response grew, the
> authorization did not.
>
> **A shared crew station is not an identified caller, and the held payload reflects that.** The
> station token is a 24-hour shared-PIN credential on an unattended tablet with no idle logout, so
> the blocker's free text (`note`, `title`) is **not sent** to it — only `category`, `severity`, the
> attribution and two booleans. The same read over a **user session** returns the full block. This
> mirrors the wallboard's standing rule for unattended shop screens (no NCR titles/descriptions);
> see `docs/API.md` → Shop Floor → "Disclosure (`held`)".
>
> That withholding is scoped to the **blocker's** free text. The same queue read **does** send the
> job's five office-authored guidance fields (work-order notes / special instructions, and the
> operation's description / setup / run text) to a station principal — a recorded exception (owner
> decision, 2026-08-14), on the reasoning that planning text exists to reach the person doing the
> work, and explicitly **not** a relaxation of the rule above. No role or permission changed. See
> `docs/KIOSK.md` → "Disclosure: this free text does reach a crew station".

> **Release stays the authorization boundary even though a read can now promote operations.** READY
> promotion runs from a reconcile-on-read seam (one of four) — so a work order released before the
> pooling rule shipped repairs itself when anyone loads it (`docs/API.md` → Work Orders → "A read
> heals a stranded work order"). That read is performed under the **reader's** own token, and the
> **View** row above is ticked for every role, so a Viewer's `GET /work-orders/{id}` can flip
> operations PENDING → READY. Two things keep this off the **Release** row: the promotion **never
> touches a DRAFT work order**, so no read by any role can put unreleased work on the floor's board —
> `POST /work-orders/{id}/release` (Admin / Manager / Supervisor) remains the only way work reaches
> the board the first time, and it remains the record of *who* authorized production — and promotion
> grants no capability the predecessor gate did not already allow, so the reachable-operation
> observation in the note above is unchanged in kind by it. Reconcile-on-read already performed
> reader-triggered writes (completion rollups, audited to the requesting user); this adds no new role,
> no new endpoint, and no new gate.

> **Operator-qualification gate is record-only (Batch 11C / G5-B).** `POST /api/v1/shop-floor/clock-in`
> and `PUT /api/v1/shop-floor/operations/{id}/start` stay **operator-facing**: clock-in is open to
> **any authenticated user** (`get_current_user`), and the start verb is the floor roles (Admin /
> Manager / Supervisor / Operator / Quality, since P0.4); the qualification gate added no role gate. The G5-B qualification gate (no active
> `SkillMatrix` entry at level ≥ 2 for the work center, or a missing/expired required
> `OperatorCertification`) **only records** a tamper-evident `audit_log` row
> (`OPERATOR_QUALIFICATION_EXCEPTION`) + a warning event and surfaces a `qualification_exceptions`
> array on the response; it does **not** gate the operator's role or block the clock-in / start. The
> gate's lookups are tenant-scoped (every skill/cert/work-center query filters the active company).

> **Over-count correction — operator self-service (no new role) + a role-gated office twin.**
> `POST /api/v1/shop-floor/operations/{id}/reduce-production` (walk back good-count an operator
> OVER-reported on their **own unapproved** labor — open clock-in first, then their own earlier
> unapproved sessions — before completion; a miscount fix, not scrap) is
> **operator-facing**, open to **any authenticated user** (`get_current_user`), matching the other
> operator write verbs (clock-in, production, hold; the shop-floor complete verb is role-gated since
> P0.4). It adds **no new role or permission**.
> Authorization is by **evidence, not role**: the walk-back is bounded to the caller's **own
> unapproved** entries on the operation (crew-safe — never another operator's count; **approved**
> labor is excluded — approval is the immutability boundary, G5-A) and is refused **409** once
> the operation is COMPLETE or the WO is terminal (post-completion corrections stay an
> office/supervisor task — and that referral is now honest, since the office twin below **accepts** a
> COMPLETE operation where it used to hit the identical refusal). It is
> tenant-scoped (a cross-tenant id → **404** before any mutation) and writes a tamper-evident
> `audit_log` row (action `reduce_operation_production`, old→new quantity + the operator-supplied
> reason). See `docs/API.md` → Shop Floor → "Over-count correction".
>
> The **office twin** `POST /api/v1/work-orders/operations/{id}/reduce-production` is enforced
> **in code** to `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above
> (an Operator gets **403**): it corrects recorded production on **any operator's unapproved**
> labor record, which is a supervisory power, not self-service. No clock-in is required. Approved
> entries stay excluded on this path too — the front door for signed-off labor is
> `POST /shop-floor/time-entries/{id}/unapprove` (the audited Approve-labor row, which forbids
> self-unapproval), then reduce. Same tenant-scoped **404** and tamper-evident audit row as the
> shop-floor verb.
>
> **Neither verb takes a standing API token** (**403** before any lock or write, whatever the bound
> user's role) — a count correction is a person's judgment; see "An API token may not declare scrap
> or make count judgments" above. A crew-station badge is a person and still corrects its own count
> on the shop-floor verb.
>
> **The office/operator split on a COMPLETE operation is a role decision, not a mechanism detail.**
> The office verb passes `allow_completed_operation=True` and the operator verb does not, so a
> completed operation is correctable **only** by Admin / Manager / Supervisor; a **terminal work
> order** is refused on both, with its own distinct message so neither verb makes a referral the
> other cannot honor. The refusal being relaxed dated from a production incident and was justified
> on "downstream inventory / cost / FG effects have fired and cannot be walked back" — the reasoned
> **material return** above is that walk-back, which is what makes the relaxation a supervised
> correction rather than a loosened control. Note that the two powers travel together by design:
> the same tier that can lower a completed operation's count is the tier that can hand the material
> back, so neither half can be exercised without the authority to do the other. The supervisor's
> optional note is recorded on
> the **audit row only**, never written onto another operator's labor record. In the UI this is
> the **Correct count** action on the work-order detail page, gated on `work_orders:edit`. Since owner
> ruling 1 a correction that would leave a COMPLETE operation short also needs `reopen_operation`
> (the dialog's explicit **Reopen Op N and correct** step) and returns it to in progress — the same
> Admin / Manager / Supervisor tier, no new permission; the operator verb refuses `reopen_operation`
> with **422**. See `docs/API.md` → Work Orders → "Over-count correction … (supervisor/office)" and
> "Short count at completion".

> **Renumber a part — endpoint mapping.** `POST /api/v1/parts/{id}/renumber` (change a part's
> number in place, retiring the old one) is enforced in code to `require_role([ADMIN, MANAGER])` —
> **deliberately narrower than the Parts Edit row above**, which reaches Supervisor. Renumbering is a
> controlled change to an article's identity under AS9100D 8.5.2, so it sits with `POST
> /parts/{id}/revision` (ADMIN/MANAGER) and `DELETE /parts/{id}` (ADMIN) — its sibling identity verbs
> on the same router — not with the tier that edits a description. **A Supervisor gets 403.**
>
> The client gates the control on the new `parts:renumber` permission, held by `platform_admin`,
> `admin` and `manager` only, so the hidden control and the refused call agree. Note the companion
> read, `GET /parts/{id}/renumber-impact`, is open to any authenticated tenant user (like the
> backflush readiness read): it discloses facts about this company's own catalog, and a screen that
> could not show them would be asking for a decision nobody could make. It is a **pure read** — no
> audit row, no event, structurally (the service takes no `AuditService`).
>
> Two things are RBAC-relevant beyond the gate. First, the Item Number input on the Materials screen
> and the Part Number input on Part Edit **stay disabled for every role, including admin** — enabling
> either would route a rename through the blind-`setattr` `PUT`, where it would carry no reason, no
> audit identity, no collision check and no repair of the operation links the number stands in for.
> The dedicated verb is the only door. Second, the write is on the tamper-evident chain as one
> `resource_type='part'` UPDATE row filed under the **OLD** number, carrying the required reason —
> so "who renumbered this, from what, and why" is answerable by searching the number someone actually
> holds on paper. See `docs/API.md` → Parts → "Renumbering a part".

> **Inline due-date edit on the work-order LIST — endpoint mapping.** Rescheduling a job from the
> Work Orders list (the pencil on the Due Date column) is not a new verb: it is `PUT
> /api/v1/work-orders/{id}` carrying `due_date` plus the row's optimistic-lock `version`, so it is the
> Work Orders **Edit** row above — `require_role([ADMIN, MANAGER, SUPERVISOR])`, which `require_role`
> itself also admits superuser and platform-admin to. The client gates the pencil on
> `work_orders:edit`, whose holders are exactly that set, so the hidden control and the refused call
> agree; **an Operator, Quality, Shipping or Viewer user gets 403**, and hiding the control is not the
> enforcement.
>
> Two things about it are RBAC-relevant beyond the gate. First, unlike Duplicate above, there **is** a
> status gate: changing a due date on a COMPLETE/CLOSED/CANCELLED work order is refused **409** by the
> endpoint, because that date is the promise date the job's delivery performance was scored against —
> no role can do it, the trio included. Both UI surfaces (the list and the work-order detail page) hide
> the pencil on a terminal WO to match. Second, the write is on the tamper-evident chain like any other
> header update: one `work_order` `UPDATE` row whose `extra_data.changes` carries the `due_date` old
> and new values, attributed to the acting user — editing from a list rather than the detail page
> changes nothing about what is recorded. See `docs/API.md` → Work Orders → "Due date on a finished
> job".

> **Duplicate a work order — endpoint mapping.** `POST /api/v1/work-orders/{id}/duplicate` (copy a
> job's *plan* — operations, laser nests, open material ties, re-snapshotted process-sheet steps —
> onto a new **DRAFT** work order) is enforced **in code** to `require_role([ADMIN, MANAGER,
> SUPERVISOR])` — the Work Orders **Create** row above. It reaches no wider than the trio already
> holds: it is a create verb (Create), it mints operations (`POST /work-orders/{id}/operations`, same
> trio), it mints nests (the laser-nest note below, same trio) and it mints material ties (the Material
> ties note below, same trio). **An Operator gets 403**, matching every other planning act.
>
> Three things about it are RBAC-relevant beyond the gate itself. First, there is **no status gate** —
> duplicating a COMPLETE job is the headline case — so read-broad/write-restricted is the only control
> in front of it; the tenant scope is what does the rest (a source outside the active company or
> soft-deleted → **404**, never 403, and never a "exists elsewhere" leak). Second, it **refuses** two
> conditions the create path would also have refused (**409** on a soft-deleted produced part, **409
> `PROCESS_SHEET_UNAVAILABLE`** on a sheet family with no released revision) — one button is not a
> licence to route around a gate a planner would have hit by hand. Third, every write it performs is on
> the tamper-evident chain: one `work_order` `log_create` carrying `source_work_order_id` /
> `source_work_order_number` (the duplicate holds **no FK** back to its source, so that row is the only
> record of the lineage) plus the deliberate omissions, and one row per copied nest and per copied tie,
> byte-parallel to the import and tie-creation paths. In the UI this is the **Duplicate** action on the
> work-order detail header and the Work Orders row/mobile-card actions, gated on `work_orders:edit` so
> the hidden control and the refused call agree. See `docs/API.md` → Work Orders → "Duplicating a work
> order".

> **Laser-nest manual entry + reference PDF — endpoint mapping.** Manually keying a laser nest and
> all per-nest mutations follow the Work Orders **Create / Edit / Delete** rows above —
> `require_role([ADMIN, MANAGER, SUPERVISOR])`: `POST /api/v1/work-orders/{id}/laser-nests/manual`
> (create), `PATCH /api/v1/laser-nests/{id}` (edit), `POST /api/v1/laser-nests/{id}/attach-document`
> and `DELETE /api/v1/laser-nests/{id}/document` (attach/detach the reference PDF), and
> `DELETE /api/v1/laser-nests/{id}` (soft-delete; the operation goes `ON_HOLD`) and
> `POST /api/v1/laser-nests/{id}/restore` (restore the original nest and planned quantity).
> Cancelled operation rows show **Restore nest** only to this trio; the ordinary Clear hold
> action remains unavailable on cancelled nests. This matches the
> laser-nest **package import** endpoints — the per-WO pair (`…/{id}/laser-nest-packages/preview`
> and `…/import`) and the no-WO **standalone** pair
> (`…/laser-nest-packages/standalone/preview|import`, whose import creates a fresh part-less
> laser-cutting work order) — and the
> stateless PDF field-extraction endpoint `POST /api/v1/laser-nests/extract` (same ADMIN/MANAGER/
> SUPERVISOR gate; no DB write, no audit). The
> **exception** is the operator-readable inline PDF preview `GET /api/v1/laser-nests/{id}/document`,
> which is open to **any authenticated user** (`get_current_user`) so operators can view the shop
> drawing — read-only and still tenant-scoped (a cross-tenant or soft-deleted nest → **404**). All
> writes are audited; nests are soft-deleted, never hard-deleted. See `docs/API.md` → Laser Nests.

> **Dispatch run order — endpoint mapping.** Setting the order operators see work in is a planner
> act, gated to the Work Orders **Edit** row above —
> `require_role([ADMIN, MANAGER, SUPERVISOR])`, tenant-scoped:
> `GET /api/v1/shop-floor/dispatch-board` (the manager board — every active work center with its
> live queue, plus any deactivated work center still holding queued work as a flagged
> `is_active: false` read-only column; a **zero-write read**, no audit rows) and
> `PUT /api/v1/shop-floor/work-centers/{id}/run-order` (rewrite one work center's dense 1..N rank;
> a work center outside the active company or inactive → **404**, indistinguishable from missing).
> The rewrite writes **one** tamper-evident `audit_log` row per manager action — a `work_center`
> `log_update` carrying the old → new operation-id lists — not one row per operation. Operators
> **consume** the resulting order through the read-broad
> `GET /api/v1/shop-floor/work-center-queue/{id}` (unchanged gate: any authenticated user, or a
> crew-station kiosk token for its own work center) and can never set it. `run_order` is
> **advisory**: it orders and labels the queue and adds **no** gate — start eligibility stays
> entirely with the existing operation gates and predecessor rules. In the UI this is the
> **Dispatch Board** page (`/dispatch`), route-gated on **`work_orders:edit`** (admin / manager /
> supervisor) rather than the read-only `work_orders:view` that `/scheduling` uses. See
> `docs/API.md` → Shop Floor → "Dispatch run order" and [docs/KIOSK.md](KIOSK.md).

> **Work-order dispatch plan (Dispatch v2) — endpoint mapping.** Ordering the shop's **work
> orders** is the same planner act, on the same gate — `require_role([ADMIN, MANAGER,
> SUPERVISOR])` (`_DISPATCH_PLANNER_ROLES` in `shop_floor.py`; platform admins / superusers pass as
> they do on every `require_role`), tenant-scoped through `get_current_company_id`, work-order reads
> filtered `is_deleted == False`. Operator, Quality, Shipping and Viewer get **403**. Three routes:
>
> | Route | Effect | Audit |
> |---|---|---|
> | `GET /api/v1/shop-floor/dispatch-board/work-orders` | Every live work order as a lane (the planner's Work orders view) | none — a **zero-write read** |
> | `PUT /api/v1/shop-floor/dispatch-board/work-order-order` | Set the shop-wide work-order plan (whole-list authority, compare-and-swap) | **one** `dispatch_board` `log_update` per save (old → new plan, every position change listed in `extra_data.changed`) |
> | `PUT /api/v1/shop-floor/dispatch-board/work-orders/{work_order_id}/operation-order` | Order one planned pool/laser work order's queued operations at each machine | **one** `work_order` `log_update` ("Set dispatch operation order") |
>
> A save that changes nothing writes no audit row. The plan verbs bump no `version` and move no
> `updated_at` (Core `UPDATE`s on display metadata), so they never 409 an open edit form. Like
> `run_order`, the plan is **advisory** and adds **no** gate: it orders what operators see and
> never decides what may start, clock in or complete. Operators **consume** it — the reordered queue
> and the planner's floor notice — through reads they already hold
> (`GET /shop-floor/work-center-queue/{id}`, `GET /shop-floor/my-active-job`,
> `GET /shop-floor/operations`), and can never set it.
>
> **Kiosk fence.** All three routes sit under `/api/v1/shop-floor/dispatch-board`, a **prefix** in
> `KIOSK_TOKEN_DENIED_PREFIXES` (`app/api/deps.py`), so a badge-minted `scope="kiosk"` token is
> **403** on every one of them whatever role the scanned badge holds — pinned by a fence test per
> route. A crew-station token cannot authenticate on them at all (**401** — `require_role` runs
> through `get_current_user`, which accepts only access JWTs). The route gate in the UI is
> unchanged: `/dispatch` → `work_orders:edit`.
>
> **Every writer of a machine's queue order shares one per-company lock**: the two plan verbs, the
> run-order `PUT`, and the four operation move paths (`PUT /work-orders/operations/{id}` with a new
> `work_center_id`, `PUT /scheduling/operations/{id}/work-center`,
> `PUT /scheduling/work-orders/{id}/schedule` and `POST /scheduling/work-orders/{id}/schedule-earliest`
> when they change the work center) — all Admin / Manager / Supervisor, all unchanged. No role or
> permission changed on any existing route. See `docs/API.md` → Shop Floor → "Work-order dispatch
> plan" and "Floor dispatch notices".

> **Scanner resolve-action is read-only and open to any authenticated user (A0.4).**
> `POST /api/v1/scanner/resolve-action` (the QR traveler / badge scan resolver,
> `app/api/endpoints/scanner.py`) carries no role gate (`get_current_user` only) — it mirrors the
> read-broad shop-floor reads it sits in front of. It is **read-only** (no audit rows, no
> operational events, no auth side effects; a badge scan is a lookup only — badge **login** stays
> exclusively on the auth routes: passwordless on `POST /auth/employee-login`, and, since badge
> sign-in was added to the password form, badge **plus password** on `POST /auth/login`) and
> **tenant-scoped** (a cross-tenant code, or a
> soft-deleted work order, resolves to `kind: "unknown"`). URL-shaped traveler codes resolve too;
> the URL's host is deliberately **not** validated — a scanned URL carries no tenant authority, and
> tenancy always derives from the authenticated caller. The per-action gating it reports
> (`legal_actions` / `blockers`) reflects operation / time-entry **state**, not role. Clock-in,
> production, hold and resume are open to any authenticated user. The shop-floor complete verb is
> the floor roles (P0.4), so the resolver can list `complete` for a Viewer who will then get
> **403**. That is a display mismatch, not a bypass: the resolver performs no write and the verb
> refuses. See `docs/API.md` → Scanner.

> **Operation sequencing (pool vs routing) is an Edit-row capability — no new role, no new permission.**
> `WorkOrder.sequential_operations` (migration `081`) decides whether a work order's operations are a
> sequenced routing or a work-center dispatch pool. It is set on **create** (`POST /api/v1/work-orders/`,
> Work Orders **Create** row) and changed only through `PUT /api/v1/work-orders/{id}`, which already
> carries `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above. Nothing was
> added to the role matrix and no permission string was minted. Two properties matter for review rather
> than for gating: turning sequencing **on** is refused (**409**) while any operation the strict rule
> would block has already been worked, and an accepted flip writes one `AuditService.log_status_change`
> row per operation it demotes READY → PENDING (`extra_data.transition = "sequential_operations_enabled"`),
> so the rule change and every status it moved are attributable on the tamper-evident chain. Operators
> gain and lose nothing: the setting changes which operations reach READY, and the shop-floor start /
> complete verbs read the same resolved rule, so a hidden card and a refused badge scan agree. See
> `docs/API.md` → Work Orders → "READY promotion: a sequenced ROUTING or a DISPATCH POOL".

> **Unit # is an Edit-row field — no new role, no new permission, and it is deliberately ungated on
> the floor screens.** `WorkOrder.unit_number` (migration `083`) is the build identity of a
> one-unit-per-work-order job. It is set on **create** (`POST /api/v1/work-orders/`, Work Orders
> **Create** row) and changed or cleared only through `PUT /api/v1/work-orders/{id}`, which already
> carries `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above. Nothing
> was added to the role matrix, no permission string was minted, and the change is audited through
> the generic `log_update` field diff like any other header field.
>
> The disclosure side is the part worth reviewing. The field is returned to **both** un-badged
> principals: the crew station's shared-PIN **station** token (on the queue and `held` rows) and a
> **display** token on the public TV, where — unlike `customer_name` — it is **not** behind
> `show_customer_names`. That is a decision, not a default: a bounded (`String(50)`) build number is
> not customer data, and its boundedness is exactly what makes it showable on an unattended screen
> where the work order's unbounded free-text `notes` (which is where this number used to be typed)
> is not. It widens neither the five-key guidance exception recorded above nor the blocker free-text
> withholding. See `docs/API.md` → Work Orders → "Unit #" and `docs/WALLBOARD.md` →
> "Unit # — ungated".

> **Material ties — endpoint mapping.** Tying stock material to a work order (the tie that makes
> inventory deplete as work completes) is a **planning act**, gated to the Work Orders **Edit** row
> above. On `app/api/endpoints/work_order_materials.py`:
>
> | Verb | Endpoint | Gate |
> |------|----------|------|
> | List ties | `GET /api/v1/work-orders/{id}/material-allocations` | `get_current_active_user` — any authenticated tenant user |
> | Tie material | `POST /api/v1/work-orders/{id}/material-allocations` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Edit a tie | `PATCH /api/v1/work-orders/{id}/material-allocations/{allocation_id}` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Untie | `DELETE /api/v1/work-orders/{id}/material-allocations/{allocation_id}` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Read a tie's per-lot consumption | `GET /api/v1/work-orders/{id}/material-allocations/{allocation_id}/consumption` | `get_current_active_user` — any authenticated tenant user |
> | **Return consumed material** | `POST /api/v1/work-orders/{id}/material-allocations/{allocation_id}/return` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Tie a nest at creation | `POST /api/v1/work-orders/{id}/laser-nests/manual` and the four `…/laser-nest-packages/{preview,import}` routes, via the optional `material_part_id` | `require_role([ADMIN, MANAGER, SUPERVISOR])` — the endpoints' own pre-existing gate |
>
> **The return verb sits in the same tier as the tie verbs, and outside the kiosk path fence, for a
> stronger reason than the rest of them.** Every other verb on this router manages a **planning row**;
> the return is the one that **moves stock** and writes tamper-evident ledger rows. Reading it as
> "just another tie mutation" and later relaxing it to operator self-service would be the mistake this
> paragraph exists to prevent: **moving material back with a reason is a bigger power than tying it,
> not a smaller one.** It is a supervised reversal of a physical fact, it credits specific heat/cert
> lots, and it is the only self-service path that can lower a tie's `qty_consumed`. Keeping it under
> `/work-orders` rather than `/api/v1/shop-floor` means a crew-station or kiosk-scoped operator token
> is path-fenced away from it entirely — an operator who over-reported a count asks a supervisor,
> exactly as they do for the office reduce-production verb. The read half
> (`…/consumption`) is deliberately **broad**, like the tie list: it discloses ledger facts about
> material the company already owns, and a return dialog that could not show which lots the material
> goes back to would be asking for a confirmation nobody could give.
>
> **The matrix rows above hold now that a UI exists** (the work-order Materials panel, the nest
> wizard's sheet-part picker, the Dispatch Board chip, the kiosk deduction line). Nothing was
> re-gated: the nest-creation paths already required Admin / Manager / Supervisor, which is the same
> set the tie verbs require, so a nest tie created inside the import transaction cannot be a
> privilege escalation around `POST …/material-allocations`.
>
> The read is deliberately broad — a tie is shop-visible context ("this job burns *that* sheet"),
> and the same rows are already reachable through lot traceability. **Operator gets 403 on all four
> mutating verbs**; deciding what material a job consumes — or handing it back — is not operator
> self-service, unlike the
> shop-floor production verbs. The router is mounted under `/work-orders`, **not**
> `/api/v1/shop-floor`, so kiosk-scoped operator tokens are **path-fenced away from it** entirely —
> a crew-station token cannot tie, untie or return material even if it reached the route.
>
> **The floor reads ties without reaching that router, and the fence is unchanged.** The Dispatch
> Board's `material_tie` and the kiosk's `material_ties` ride
> `GET /shop-floor/dispatch-board` (Admin / Manager / Supervisor) and
> `GET /shop-floor/work-center-queue/{id}` + `GET /shop-floor/my-active-job` (any authenticated user,
> **or** a crew-station token for its own work center) — reads the fence already permits, carrying
> data rather than granting a new capability. The same precedent as `scrap_reason_codes`. Both are
> **pure reads**: they post no `ISSUE`, write no audit row and reconcile nothing. Note the small
> disclosure widening this implies for a **station** principal — an unattended, PIN-unlocked terminal
> with no operator identity can now see material part numbers and on-hand stock for the parts tied to
> its own work center's queued operations (not an inventory browser, but not nothing).
>
> Every lookup (work order, part, operation, pinned lot, allocation) is **tenant-scoped**: a
> cross-tenant id is **404**, never 403, so an id cannot be probed. Every create / edit / untie /
> return writes
> a tamper-evident `audit_log` row on resource type `work_order_material_allocation`. Untie is
> `status = cancelled` — the row is **never physically deleted**, because the ledger's
> `allocation_id` back-reference must keep resolving — and is refused **409** while the **ledger**
> still shows material issued against the tie (the signed ISSUE − RETURN net, not the `qty_consumed`
> cache, so a fully returned tie can be untied); the 409 names `POST …/return` with
> `intent: "return_and_untie"`, which credits the material back to its source lots **and** cancels the
> tie in one transaction.
> A return additionally requires a **non-blank reason** and writes it on the ledger row, in the audit
> description and in `extra_data.reason`; it appends compensating `RETURN` transactions and never
> edits a historical row.
>
> **The consumption itself has no endpoint and no separate gate.** It runs inside the existing
> completion paths — `apply_operation_completion_inventory_effects` when an **operation** completes and
> `apply_completion_inventory_effects` when the **work order** does — so whoever is authorized to
> complete the work is what authorizes the resulting stock movement, including the operator-facing
> kiosk and shop-floor completion verbs and the reconcile-on-read GET. **The reversal is the
> asymmetric case, deliberately**: consuming needs no gate beyond the authority to complete the work,
> while un-consuming has its own endpoint, its own Admin/Manager/Supervisor gate and a mandatory
> reason. That asymmetry is the point — production authorizes depletion because production happened;
> nothing on the floor happens that authorizes putting material back, so somebody has to say why. **The attributed actor is
> whoever completed the operation the material was consumed against** — with the trigger at operation
> completion that is the operator who finished nest 1 of 3, not whoever later closed the job. (Through
> PR 2 it was the other way round and this paragraph said so; the reconcile-on-read GET is still the
> one path with no meaningful actor, which is exactly why it is **not** a per-operation trigger — see
> `docs/MATERIAL_CONSUMPTION_PLAN.md` → Residual gaps.) **No role gained a capability — and one role LOST one.** An Operator
> could already drive consumption by completing a job's last operation, so what changed is when and how
> often, not who. In the other direction, `POST /api/v1/work-orders/operations/{id}/complete` — the
> **office** operation-complete verb — was gated to **Admin / Manager / Supervisor / Quality** in the same
> change, matching `complete-work-order` (its larger sibling, which completes every operation on the work
> order). Quality is included deliberately: refusing it a single operation while allowing it the whole work
> order would be incoherent. `reduce-production` stays stricter for a reason that does not apply here — it
> rewrites other operators' recorded labor.
> It had been open to any authenticated tenant user, **Viewer and Shipping included**, while its office
> siblings were already gated. That gap predates this work,
> but it became load-bearing the moment completing an operation began decrementing stock: a Viewer could
> move inventory and write hash-chain rows from a page they were only supposed to read. The UI button is
> gated to the same tier. **Operators are unaffected** — they complete work through
> `/api/v1/shop-floor/operations/{id}/complete` and the kiosk, which is the documented design above, not
> through the office page. This is deliberate: material depletes
> because production happened, not because someone was granted an inventory power. The stock
> decrement, the `ISSUE` ledger row, and any `ALLOCATION_SHORTAGE` are audited on the hash chain
> regardless of which path drove them. Lifecycle side effects follow the same rule — nest re-import
> and work-order delete cancel or refuse ties under **their own** existing gates (Admin / Manager /
> Supervisor for import; Admin / Manager for WO delete). See `docs/API.md` → Work Orders →
> "Material ties" and [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md).

> **Work order templates — the same trio as Duplicate, on *every* verb including the reads.**
> `/api/v1/work-order-templates` (`app/api/endpoints/work_order_templates.py`) gates all six verbs
> — list, get, create, update, delete, use — with `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> the same set `POST /work-orders/` and `POST /work-orders/{id}/duplicate` require. **No role gained
> a capability.** A template is a name plus a pointer at an existing work order; using one calls the
> duplicate service against that work order, so it can only ever mint a work order the caller could
> already have minted by hand — with the same refusals and landing in **DRAFT**, off every
> dispatch board until somebody with **Release** releases it. Since 2026-09-01 one call may mint up
> to **20** of them (`count`, with an optional `unit_numbers` list); that is a batch of the same act,
> **not a new capability** — same verb, same `require_role([ADMIN, MANAGER, SUPERVISOR])`, no new
> endpoint, no new permission, and every copy still lands DRAFT. The cap is a server-side module
> constant (422 above it), so the batch size is not something a role can raise. See `docs/API.md` →
> Work Orders → "Work order templates".
>
> Note the shape of the **View** row: unlike work-order **View** (every role), reading the catalog is
> restricted to the same three roles. That is deliberate rather than an oversight to "fix" — the
> only thing the catalog is for is creating work orders, so a role that cannot create one has nothing
> to do with the list, and the entries carry a live plan summary of the source job. Widening the read
> without widening the write would put a picker full of disabled buttons in front of an Operator.
>
> The frontend gates every template control (the Templates tab on `/work-orders`, "Save as template"
> on the list row actions and the detail action bar, and the "New from template" header button) on
> the existing **`work_orders:edit`** permission, which maps to exactly Admin / Manager / Supervisor
> — so a hidden control and a refused call agree, per the standing nav/route-gating rule. As
> everywhere, `require_role` also admits **PLATFORM_ADMIN** and superusers (see
> [Superuser Override](#superuser-override)); `work_orders:edit` is on the platform-admin permission
> set too, so the two stay consistent.
>
> **Related gap, not fixed here:** this matrix has no **Duplicate** row, so
> `POST /work-orders/{id}/duplicate` — Admin / Manager / Supervisor in code — is
> undocumented in the matrix even though the rows above are defined by reference to it. Worth adding
> when someone next edits this section. See `docs/API.md` → Work Orders → "Work order
> templates" and [docs/WORK_ORDER_TEMPLATES.md](WORK_ORDER_TEMPLATES.md).

### Comments and watchers

v1 (2026-11-16): comment threads, @mentions and watchers on **work orders only**, in the office app
(the Comments panel on the work-order detail page). Routes: `GET`/`POST /comments`,
`PATCH`/`DELETE /comments/{comment_id}`, `GET`/`POST`/`DELETE /watchers`, `GET /users/mention-search`.

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Read a work order's comments and watch state | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Post a comment (and @mention people) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Edit a comment | own | own | own | own | own | own | own |
| Delete a comment | any | any | own | own | own | own | own |
| Watch / unwatch a work order (self) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Mention search (`GET /users/mention-search`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Who may comment mirrors work-order READ access** — every signed-in member of the active company,
> all seven tenant roles, Viewer included — because `GET /work-orders/{id}` itself depends only on
> authentication and the active company. The record-type registry in
> `app/services/comment_service.py` (`COMMENTABLE_TYPES`) owns that rule per record type, so a
> future type (a PO, a quote) gets the read RBAC of its own domain rather than this one. The same
> check gates each @mention (an inactive, foreign or not-permitted id is **422**, naming ids only)
> and each notification recipient, re-checked when the worker dispatches.
>
> - **Edit is the author's alone.** An Admin or Manager can remove a comment but never rewrite
>   someone's words: anyone else's `PATCH` is **403**.
> - **Delete** is the author, an Admin or a Manager (moderation); a superuser or Platform Admin
>   passes, as with `require_role`. Deletion is a soft delete, audited with `extra_data.moderated`
>   when the deleter is not the author. There is no restore in v1.
> - **Watching** is the caller's own preference: `POST` / `DELETE /watchers` act only on the
>   caller, never on another user id. Posting a comment auto-watches its author and everyone it
>   mentions. Only **members** of the active company watch, because only members are notified: a
>   Platform Admin viewing another company is not auto-watched there, `POST /watchers` is **409**
>   for them, and `GET /watchers` reports `watching: false`.
> - **Mention search** returns `{id, display_name}` only — never an email, role or phone — and is
>   open to every member of the company, unlike the Admin / Manager `GET /users/` list.
> - **Tenancy.** The work order is resolved in the active company and must be live; another
>   company's, an absent or a soft-deleted work order is **404** on every route, never **403**.
>
> **Credentials (owner decision 2026-11-16).** Every write — posting, editing and deleting
> comments, watching and unwatching — refuses **any** API token **403**, and none of the five is an
> MCP tool; see [Credential policy for judgment writes](#credential-policy-for-judgment-writes).
> The reads stay open to a token the read gates admit: a full token reads all three; a read-only
> token reads the thread and the watch state but is **403** on mention search (`Users` tag, part of
> the read-only deny floor); an AI-external token is **403** on all three (none is on its
> allowlist). A crew-station badge token is refused **403** on every route here by its path fence,
> so the kiosk shows no comments. See
> [API.md → Comments and watchers](API.md#comments-and-watchers).

### Parts

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| **Arm automatic BOM backflush** (`backflush_components`) | ✓ | ✓ | ✓ | | | | |
| **Mark customer-supplied** (`customer_supplied`) | ✓ | ✓ | ✓ | | | | |
| View backflush readiness / dry-run preview | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Deactivate / reactivate** (`POST /parts/{id}/deactivate` · `/activate`) | ✓ | ✓ | | | | | |
| Delete | ✓ | | | | | | |
| Restore a deleted part | ✓ | ✓ | | | | | |

> **The backflush row is the ordinary Edit row, and that is a recorded decision, not an omission**
> (`feat/backflush-exposure`, PR 4.5). `Part.backflush_components` opts a part into **automatic
> consumption of its BOM/routing components out of stock** on every future work-order completion — a
> permanent, shop-wide policy change that writes the lots it drew onto the as-built record. It is set as
> an ordinary field on `PUT /parts/{id}` (and on `PUT /materials/{id}`, which writes the same `parts`
> rows through the same schema), so its authorization tier is whatever those endpoints already enforce:
> **`require_role([ADMIN, MANAGER, SUPERVISOR])` — the same permission as editing a description.** The
> owner chose this over a dedicated reasoned verb limited to Admin/Manager. Three consequences are
> **accepted residuals**, recorded in `docs/CMMC_LEVEL_2_COMPLIANCE.md`: a **supervisor** can arm it;
> **no reason is captured** (the audit row records who, when, false→true, and the readiness verdict in
> `extra_data` — not why); and **a concurrent flip does not 409**, because `Part` maps no `version`
> column, so optimistic locking on parts is cosmetic and last write wins.
>
> **The role gate is not the only gate.** Enabling is refused **409** while the part's readiness check
> reports a blocking diagnostic, through one shared function (`assert_backflush_change_allowed`) defined
> in `parts.py` and **imported** by `materials.py` — a gate in only one of the two files would not be a
> gate. **Disabling is never refused.** Neither door accepts the field on **create**: it is absent from
> `PartBase`/`PartCreate`, so `POST /parts/`, `POST /materials/` and both CSV importers cannot set it,
> and a part is always created **off**.
>
> **⚠️ But the gate protects the instant of the flip and nothing after it, which matters for how the
> permission should be read.** The readiness check runs the part's **BOM** explosion only — the routing
> half is not evaluated at part scope at all — it is evaluated **once**, and it is never re-run on a BOM
> edit, a routing change, a release or a completion. Every input it read stays editable afterwards by the
> **same ADMIN/MANAGER/SUPERVISOR tier** through `boms:edit` / `routings:edit`. A **BOM**-line write now
> at least *says so* — it returns a `backflush_armed_warning` and stamps `extra_data.backflush_armed_parts`
> on its own `bom_line` audit row — but it does not re-check and does not refuse, and the **routing** edit
> path is still entirely unaware. So the recorded verdict `backflush_readiness: "clean"` asserts
> less than a reader assumes: not "this part's demand resolves correctly", only "no blocking diagnostic
> in its BOM at that instant". What backs it afterwards is a completion-time refusal
> (`BACKFLUSH_DEMAND_REFUSED`), which is a net, not a second gate. **Practically: granting `boms:edit` or
> `routings:edit` is, for an armed part, granting the ability to change what automatically leaves stock.**
>
> **Auditing who armed it is ONE query, through either door.** `PUT /materials/{id}` writes the same
> `parts` row and logs the change as `resource_type="part"`, not `"material"`, so the trail is not split
> by which URL was used (`create_material` / `delete_material` still log `"material"`). The canonical
> query recipe lives in [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → the 2026-07-27
> (PR 4.5) changelog row, item (3) — written down once, on purpose.
>
> The two read companions — `GET /parts/{id}/backflush-readiness` and
> `GET /work-orders/{id}/backflush-preview` — follow the read-broad rule at the top of this document
> (any authenticated user in the tenant) and are **pure reads: they write nothing**, not even an audit
> row. See `docs/API.md` → Parts → Part Schema, and
> [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md) → "Exposing the flag (PR 4.5)".

> **The customer-supplied row is the ordinary Edit row too** (migration `134`). `Part.customer_supplied`
> marks a part the customer furnishes, and MRP then creates no demand for it and refuses to turn any
> older action for it into supply (owner decision 2026-11-22; see
> [API.md → MRP → Customer-supplied parts](API.md#customer-supplied-parts-mrp)). It is set or cleared
> as an ordinary field on `PUT /parts/{id}` / `PUT /materials/{id}`, so its tier is those routes'
> `require_role([ADMIN, MANAGER, SUPERVISOR])`: no gate, refusal or reason of its own, and the change
> is recorded on the handler's `log_update` row (`resource_type="part"` through either door). Like
> `backflush_components` it is absent from `PartBase` / `PartCreate` and from both CSV importers, so a
> part is always created unflagged. Neither route refuses an API token: a full token bound to one of
> those roles can set it, and a read-only or AI-external token is refused by the read-only gate.

> **Deactivate / reactivate a part — ADMIN / MANAGER, deliberately narrower than the Edit row.**
> `POST /parts/{id}/deactivate` and `POST /parts/{id}/activate` (shipped with the Combine/Merge SKUs
> feature, to retire the SKU a combine folded away) are `require_role([ADMIN, MANAGER])` — the same
> identity tier as `POST /parts/{id}/renumber` and `POST /parts/{id}/revision`, not the
> `PUT /parts/{id}` tier that reaches Supervisor. **A Supervisor gets 403.** Deactivation removes the
> part from every picker, search and purchasing signal in the app, so a required `reason` is captured
> on the deactivate side (the activate side's is optional — the permissive direction is visible the
> moment it happens); both write a `resource_type="part"` UPDATE row on the tamper-evident chain.
>
> **These two verbs are the ONLY non-delete writers of `parts.is_active`, and that is an
> authorization decision, not a plumbing one.** `is_active` doubles as the **soft-delete mask**
> (`delete_part` sets `is_deleted` AND `is_active=false` AND `status='obsolete'` together), so adding
> `is_active` or `status` to `PartUpdate` would hand every **Supervisor** a way to clear a delete mask
> through an ordinary blind-`setattr` form save on `PUT /parts/{id}` or `PUT /materials/{id}`, neither
> of which filters `is_deleted` on its lookup. That is the 2026-08-16 `Vendor` trap invariant 3
> records. Both verbs additionally **404 a soft-deleted part** (*"restore it first"*) for the same
> reason. **Until 2026-11-05 the schema did not match this paragraph:** `PartUpdate` still declared
> `is_active` and `status`, so a Supervisor could write both through either `PUT`. Both fields are
> now removed (`tests/test_part_update_activation_fields.py`); a `PUT` that still sends either key has
> it dropped and changes nothing.
>
> **A restore returns the RECORD, not the permission (behaviour change).**
> `POST /parts/{id}/restore` used to hard-code `is_active = True`. It now returns the part to the
> `is_active` it had before the delete (`COALESCE(is_active_before_delete, false)`, migration `086`),
> and an unknowable prior value — NULL, meaning the part was deleted before `086` — resolves
> **INACTIVE**. The reasoning is invariant 3's, and it is an RBAC argument: putting a retired part
> number back into use is an engineering decision that must be *made*, by someone holding the
> activate verb, with an audit row saying so — never inherited as a side effect of undoing a delete.
> Restoring too restrictively costs one explicit audited re-activation and is visible immediately;
> restoring too permissively is indistinguishable from a legitimate approval and is never detected.
> Same control migration `082` established for `Vendor`. Because a restrictive restore obliges shipping
> the screen that undoes it, **Materials** has an **In Use / Retired** view that reaches a retired part so
> the audited activate verb can switch it back on — the obligation the Vendors **Active / Inactive /
> Deleted** switch discharges on that side. ⚠️ The **Parts** page has no equivalent view yet, so an
> inactive-restored *engineering* part is still only reachable by calling the endpoint directly. See
> `docs/API.md` → Parts.

### BOMs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit — header (`PUT /bom/{id}`) and lines (`POST`/`PUT`/`DELETE` on items). **Status-dependent — see below** | ✓ | ✓ | ✓ | | | | |
| **View unit-of-measure mismatch report** (`GET /bom/uom-mismatches`) | ✓ | ✓ | ✓ | | | | |
| **BOM Unit Mismatches page** (`/bom/uom-mismatches`) — nav entry + route, `boms:edit` | ✓ | ✓ | ✓ | | | | |
| Delete (`DELETE /bom/{id}`) — **soft**, draft only | ✓ | ✓ | | | | | |
| **Restore** (`POST /bom/{id}/restore`) | ✓ | ✓ | | | | | |
| Release (`POST /bom/{id}/release`) | ✓ | ✓ | | | | | |
| **Unrelease** (`POST /bom/{id}/unrelease`) | ✓ | ✓ | | | | | |

> **Edit is status-dependent, and that is a second gate on top of the role.** A role tick above
> means "may attempt the verb"; whether it succeeds depends on the BOM's `status`, which is a
> controlled-document state:
>
> | BOM `status` | Header `PUT /bom/{id}` | Line writes | `release` | `unrelease` | `delete` |
> |---|---|---|---|---|---|
> | `draft` | all fields | allowed | ✓ | 400 *"BOM is not released"* | ✓ (soft) |
> | `released` | **`description` only** — anything else **400** | **400** | 400 *already released* | ✓ | 400 |
> | anything else (legacy/corrupt) | **400** | **400** | **400** | ✓ — **normalises back to `draft`** | 400 |
>
> The workflow for changing a released BOM is therefore **unrelease → edit → release**, three
> audited rows on the chain rather than one silent in-place mutation. That is also the answer for
> the unit-of-measure worklist above: a mismatched line on a *released* BOM cannot be corrected in
> place — unrelease it, fix the line, release it again.
>
> **`status` is not writable through `PUT /bom/{id}` at all.** The field was removed from
> `BOMUpdate`. Previously it was an unvalidated free string on a verb open to **Supervisor**, one
> tier wider than the Admin/Manager `release` verb it shadowed — so a Supervisor could
> `PUT {"status": "released"}` and bypass both the release role gate and its "cannot release a BOM
> with no items" precondition, producing an approved controlled document with `approved_by` /
> `approved_at` **NULL**: an approved document with no approver. Sending `"draft"` un-released a BOM
> without clearing the approver. Both transitions now belong exclusively to `release` / `unrelease`,
> which are Admin/Manager and which stamp or clear the approval evidence. A legacy client that still
> sends `status` gets a 200 with the field ignored and the true status echoed back.
>
> **`unrelease` is also the de-corruption door.** It refuses only a BOM that is *already* `draft`;
> anything else it withdraws to `draft`, audited with the actual prior status. That matters because
> the removed free-string field could have written junk (`"RELEASED"`, `"obsolete"`, anything), and
> every other verb requires a draft — without this, `BOM.part_id` being UNIQUE would leave such a
> part permanently unable to hold a working BOM.

> **The unit-of-measure mismatch report is gated to the Edit tier, not the View tier — deliberately**
> (`feat/backflush-exposure`, PR 4.5). It is a **remediation worklist**, not a browse view: every row on
> it is a BOM line someone has to go and correct before the part it belongs to can be armed for automatic
> backflush. ADMIN / MANAGER / SUPERVISOR is exactly the set that can act on a row — edit the line
> (`PUT /bom/items/{id}`) or arm the flag (`PUT /parts/{id}`) — so handing the list to a role that can do
> neither buys nothing. It is a **pure read: it writes nothing**, not even an audit row. Note this is
> *narrower* than the two backflush read companions above, which are open to any authenticated tenant
> user because they answer a question about one part a user is already looking at. See `docs/API.md` →
> BOM (Bill of Materials).
>
> **The screen carries the same gate, in both places it can be enforced.** PR 4.5 shipped the report
> API-only; the follow-up added the **BOM Unit Mismatches** page at **`/bom/uom-mismatches`** (sidebar:
> Engineering → *BOM Unit Mismatches*, directly after *Bill of Materials*). Both the nav entry and the
> route require **`boms:edit`**, whose role set — `{platform_admin, admin, manager, supervisor}` — is
> exactly the endpoint's `require_role([ADMIN, MANAGER, SUPERVISOR])`. So the link is not rendered for a
> role that cannot act on a row, **and** the route guard refuses a deep link from one; the API gate then
> refuses the fetch regardless, which is the enforcement that actually counts. The route entry is
> `{ prefix: '/bom/uom-mismatches', permission: 'boms:edit' }` and wins over the `/bom` → `boms:view`
> entry because `getRouteAccessRequirement` matches the **longest** prefix — a page gated *more* tightly
> than the parent it sits under, which is the reason that longest-prefix rule exists. Nav gating needed a
> small mechanism that did not exist before: `NavItem` gained an optional `permission`, and
> `visibleNavigation` filters items **and** collapsible-group children by it, dropping a group left with
> no visible children so no group opens onto nothing. Every pre-existing nav item carries no
> `permission`, so nothing else changed.
>
> **What the page can and cannot do is part of the gate's rationale.** It is **read-only**: rows
> deep-link to `/bom?id={bom_id}` and to the assembly part, and there is no inline BOM-line editor,
> because BOM-line create/update/delete wrote **no audit rows at all** when it was built — making this
> screen the primary remediation flow would have put a compliance-critical correction on an un-audited
> endpoint. **That blocker is now closed** (all three verbs audit as `bom_line`), so an inline editor is
> no longer refused on compliance grounds; it is simply not built. The
> corrections themselves therefore run through the ordinary, already-gated BOM edit path. See
> `docs/API.md` → BOM → *Where this is worked — the BOM Unit Mismatches screen*, and
> [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md) → "Exposing the flag (PR 4.5)" for
> the run-report → correct-lines → re-check-readiness → arm sequence.

### Routings

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Copy | ✓ | ✓ | | | | | |
| Generate from drawing (AI) | ✓ | ✓ | ✓ | | | | |
| Edit (draft routing) | ✓ | ✓ | ✓ | | | | |
| Edit time standards (released routing) | ✓ | ✓ | | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

> **Edit row splits by routing status — endpoint mapping (`feat/routing-editable-time-standards`).**
> `PUT /api/v1/routing/{routing_id}/operations/{operation_id}` (`update_operation`,
> `app/api/endpoints/routing.py`) carries the decorator-level
> `require_role([ADMIN, MANAGER, SUPERVISOR])` — the **Edit (draft routing)** row, where every
> operation field is editable. On a **released** routing the same endpoint allows in-place edits to
> **time standards only** (`setup_hours`, `run_hours_per_unit`, `move_hours`, `queue_hours`,
> `cycle_time_seconds`, `pieces_per_cycle`) and gates that path **in code** to **Admin / Manager**
> only — a **Supervisor** hitting the released-edit path receives **403**
> (*"Editing a released routing's time standards requires the Admin or Manager role."*). This mirrors
> **Release** (also Admin/Manager-only): editing live released content is release-adjacent authority,
> so it is held to the release role set rather than the broader draft-edit set. Changing any
> non-time-standard (process) field on a released routing returns **400** (create a new revision
> instead); an **obsolete** routing is fully locked (**400**). Adding/deleting/reordering operations
> on a released routing also returns **400** (process is frozen on release). Superuser / Platform
> Admin bypass role checks, as elsewhere. Every applied change is tamper-evidently audit-logged; see
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → CONFIGURATION MANAGEMENT (CM).

> **Copy & AI generation — endpoint mapping (`feat/process-sheets-library`).**
> `POST /api/v1/routing/{routing_id}/copy` (`copy_routing`, `app/api/endpoints/routing.py`) carries
> `require_role([ADMIN, MANAGER])` — deliberately **narrower than Create** (no Supervisor). The
> two-step AI generation flow — `POST /routing/generate-from-drawing` then
> `POST /routing/create-from-generation` — carries `require_role([ADMIN, MANAGER, SUPERVISOR])`, the
> Create role set. Both paths produce **draft** routings (Release stays Admin/Manager), and the copy
> endpoint writes a tamper-evident `audit_log` CREATE with `extra_data.copied_from` (the source
> routing id) — see `docs/API.md` → Routing and the
> [CMMC change log](CMMC_LEVEL_2_COMPLIANCE.md) entry dated 2026-07-06.
>
> **The AI accept step names who accepted (2026-11-05).** `create-from-generation` is the step where a
> named person at that role set accepts an AI proposal; nothing is created by `generate-from-drawing`
> alone. It now writes the routing's `audit_log` CREATE with `extra_data.approved_by`, the generation
> session, model and prompt version, and the count of human corrections. Before this the step wrote
> only AI-learning telemetry, so an AI-originated routing had no audit row attributing its creation.
> See `docs/API.md` → Routing → *AI routing generation*.

### Work Centers

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (`GET /work-centers/`, `/{id}`, `/types`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create (`POST /work-centers/`) | ✓ | ✓ | | | | | |
| Edit (`PUT /work-centers/{id}`) | ✓ | ✓ | | | | | |
| Set current status (`POST /work-centers/{id}/status`) | ✓ | ✓ | | | | | |
| Deactivate (`DELETE /work-centers/{id}`) | ✓ | | | | | | |
| Import CSV (`POST /work-centers/import-csv`) | ✓ | ✓ | | | | | |

> **Status is Admin / Manager, and that is a tightening.** `POST /work-centers/{id}/status`
> previously carried a bare `get_current_user` — **any** authenticated user in the tenant could
> flip a machine to `offline`/`maintenance`, and no audit row named who. It is the only writer
> of `WorkCenter.current_status` outside the CSV importer, and the flag drives what the dispatch
> board and the operator kiosk show. The tier matches **Edit**, not **Deactivate**: a status flip
> is reversible, and Edit already lets a Manager flip `is_active`. Superuser / Platform Admin
> bypass, as elsewhere.
>
> **The frontend gate is paired with it, in the same change.** The inline status `<select>` on
> each row is gated to the same role set and renders as a read-only `StatusBadge` for everyone
> else — otherwise the control would render, accept a change, fire the request, and surface the
> refusal only as a 403 toast afterwards.
>
> ⚠️ **The page and the endpoints disagree about who Manager is, and the page is the narrower
> one.** `/work-centers` *is* route-guarded — `App.tsx` → `routeAccessRequirements` maps the
> prefix to **`admin:settings`**, which only **Admin** and **Platform Admin** hold. So a
> **Manager holds every endpoint permission in this table except Deactivate, yet is routed to
> `/unauthorized` when opening the page**, and the nav entry is shown to them by a different
> rule. Nobody who can currently reach the page is refused by the client gate above; it is
> defense in depth today and becomes load-bearing when the route tier is aligned. Tracked
> separately — do not "fix" it by widening the status endpoint instead.
>
> **Deactivate stays Admin-only and is server-GATED**: refused **409** while any live operation
> still references the machine (see `docs/API.md` → Work Centers). Every state-changing endpoint
> in this table writes a tamper-evident `audit_log` row (`resource_type = "work_center"`);
> create/status were the last two that did not.

### Maintenance (PM schedules, maintenance work orders, event log)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (schedules, work orders, a work order's log entries (`GET /maintenance/work-orders/{id}/logs`), overdue, calendar, dashboard, history, equipment assets and asset history) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit PM schedule (`POST`/`PUT /maintenance/schedules`) | ✓ | ✓ | ✓ | | | | |
| Deactivate PM schedule (`DELETE /maintenance/schedules/{id}`) | ✓ | ✓ | ✓ | | | | |
| Import PM schedules (`POST /maintenance/schedules/import/preview`, `/import/commit`; may create equipment assets) | ✓ | ✓ | ✓ | | | | |
| Create / edit / retire equipment asset (`POST /maintenance/assets`, `PUT`/`DELETE /maintenance/assets/{id}`) | ✓ | ✓ | ✓ | | | | |
| Create / edit maintenance WO (`POST`/`PUT /maintenance/work-orders`) | ✓ | ✓ | ✓ | | | | |
| Start / complete maintenance WO (`POST .../{id}/start`, `.../complete`) | ✓ | ✓ | ✓ | ✓ | | | |
| Add event log entry (`POST /maintenance/log`; a standing API token of these roles may too — see [Credential policy for judgment writes](#credential-policy-for-judgment-writes)) | ✓ | ✓ | ✓ | ✓ | | | |

> ⚠️ **This table is new because the router had NO role gating at all.** All sixteen handlers were
> `Depends(get_current_user)`, so a **Viewer could create, start and complete maintenance work
> orders** on any machine in the tenant — while the sibling supplier-scorecard router already gated
> its writes to Admin / Manager.
>
> **The split follows the work-order permissions the frontend already uses.** Planning verbs
> (schedules, opening/editing a maintenance WO) match `work_orders:create` / `work_orders:edit`
> (Admin / Manager / Supervisor). Performing verbs (start / complete / log) additionally admit
> **Operator**, mirroring `work_orders:complete` — the maintenance tech doing the work signs in as
> one. Superuser / Platform Admin bypass, as elsewhere.
>
> **Reads stay open to every role.** `/maintenance` is route-guarded on `work_orders:view`
> (`App.tsx` → `routeAccessRequirements`), which every role holds, so refusing reads would break the
> page for its intended audience.
>
> **The client now matches the server gate.** `Maintenance.tsx` shows the planning controls (New
> Schedule, New Work Order, Import PM Schedules, a schedule's **Start PM** — which creates a
> maintenance WO — **Edit** and Deactivate, the Assets tab's add / edit / retire / reactivate) only
> to `canPlanMaintenance` (Admin / Manager / Supervisor / Platform Admin, or a superuser) and a
> work order's Start / Complete only to `canPerformMaintenance` (those plus Operator) —
> `frontend/src/utils/maintenance.ts`; every other role gets the read-only page. Completing a WO that
> carries a `schedule_id` advances that schedule's next due date under the performing gate (audited
> as a `maintenance_schedule` UPDATE) — the only way an Operator moves a schedule.
>
> **Assets and the PM import follow the planning tier.** Equipment-asset writes and both PM import
> routes are `_PLANNING_ROLES` (Admin / Manager / Supervisor), like schedules; asset reads are open to
> every role, like the rest of the router. The PM import is planning data, not a credential-restricted
> judgment write: it accepts an API token, and both of its routes are MCP tools. The one exception
> (2026-11-20) is the schedule-checklist rule: a token's sheet that takes steps away from a schedule
> whose open work order was opened before migration `132` (no checklist snapshot) gets a row error,
> so its commit is **409** — see `API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL` under
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes).
>
> **Starting, completing or cancelling a maintenance WO can write downtime.** A work-center WO with
> `requires_shutdown` opens an event on start only when the machine has none open (it adopts an
> operator's open event, or shares one another started shutdown WO holds), and on complete or a `PUT`
> to `cancelled` resolves it, hands it to a WO still holding the machine down, or — an adopted event on
> a cancel — hands it back to the machine still open (see [Downtime](#downtime) and `docs/API.md` →
> Maintenance). Those downtime writes ride on the maintenance verb's own role gate — an Operator who
> may start or complete the WO thereby writes the event — and are audited as `downtime_event` rows.
> Adopting, handing over or handing back sets only the event's `maintenance_work_order_id` /
> `maintenance_adopted` link (fields `PUT /downtime/{id}` cannot write). Holding and cancelling go
> through `PUT /maintenance/work-orders/{id}` (planning roles, so the Maintenance page shows Hold /
> Cancel to Admin / Manager / Supervisor / Platform Admin, on open rows only); that `PUT` refuses
> `in_progress` and `completed` (**409**, the Start / Complete verbs own them), un-starting a started
> WO, changing `requires_shutdown` once started, and any change at all to a completed or cancelled WO
> (below).
>
> **Three state refusals on the same gates (2026-11-17; no role changed).** Creating a maintenance WO
> with a `schedule_id` that already has an open one (scheduled, overdue, in progress or on hold) is
> **409** naming it, for every role — one open work order per PM schedule. Completing a WO with any
> checklist item unticked needs a written `incomplete_checklist_reason` (**409** without one, nothing
> saved; owner decision 2026-11-17); with one, the completion's `STATUS_CHANGE` audit row records the
> unticked items and the reason and is **required** evidence (**503**, nothing saved, if it cannot be
> written). It is a reason on the record, not an approval, so the performing gate is unchanged: an
> Operator who may complete the WO may record it. A standing API token may not: with any item
> unticked its completion is **403** `API_TOKEN_INCOMPLETE_CHECKLIST` whatever reason it sends (owner
> decision 2026-11-17) — see [Credential policy for judgment writes](#credential-policy-for-judgment-writes). And a
> completed or cancelled maintenance WO is a final record: a `PUT` that would change **any** of its
> fields is **409** before any write — its status as before, and now also `checklist_results`,
> `notes`, title, description, dates, priority, assignee and `requires_shutdown` (the detail names
> the fields) — for every role and every credential: a person's session, a Platform Admin and an API
> token alike. Before, only the status was held, so a planning-role `PUT` could re-tick a completed
> PM's checklist after the fact. Re-sending the record's own value is accepted and writes nothing
> (no audit row). A later note goes on the record as a `POST /maintenance/log` entry with
> `maintenance_wo_id` (performing roles, as in the table; dated, signed, audited, and filed under the
> work order's own machine or asset — a different explicit one is **409**), never as an edit of the
> WO; the Maintenance page's work-order detail view has an **Add log entry** form for it. This is a state rule, not a credential policy, so it has no row in the credential table.
> Details in `docs/API.md` → Maintenance → *A completed or cancelled work order is a final record*
> and *The PM loop*.
>
> **PM record integrity (2026-11-20, migration `132`; no role changed).** The one-open-work-order
> rule is now also a partial unique index in the database (same 409 for every role). A PM work order
> snapshots its schedule's checklist when it is opened, and completion judges those steps, so a
> schedule edit or PM import by any role binds the next work order, not an open or finished one (an
> open work order opened before `132` is judged on the schedule's current checklist until it
> completes). That narrowed the schedule-checklist token refusal to pre-`132` open work orders and
> extended it to the PM import (credential table above). `/complete` with `notes` omitted now keeps
> the work order's notes instead of wiping them; a change is recorded old and new on the completion's
> audit row, which becomes required evidence (503, nothing saved) when it replaces or clears notes the
> work order already had. See `docs/API.md` → Maintenance → *The PM loop*.

> **Tenancy: ten of the sixteen handlers took no company argument.** `start` and `complete`
> resolved the work order by bare id, and `dashboard` / `calendar` / `history` / `overdue`
> aggregated across every tenant. All are scoped now, and `work_center_id`, `schedule_id` and
> `maintenance_wo_id` are validated against the caller's company (flat **404**, never 403). See
> `docs/API.md` → Maintenance for the three production 500s this also fixed and the removal of the
> unaudited status write from two GETs.

> **Every state change writes a tamper-evident `audit_log` row** — `maintenance_schedule`,
> `maintenance_work_order` (with `STATUS_CHANGE` on start and complete) and `maintenance_log` (required
> evidence since 2026-11-20: a log entry whose audit row cannot be written is not saved, **503**). The
> router previously wrote none, so who started or closed a PM job was unrecoverable, and PM records
> are AS9100D-auditable quality records.

> **Before deploying, check for legacy cross-tenant rows.** The new write guards stop *new*
> mis-tenanted rows; they do nothing about rows written before them. Such a row is owned by the
> caller's company and so passes every scoping filter, while the serializer used to render the
> foreign machine's name. The serializers now null the related field, but the rows themselves are a
> data problem:
>
> ```sql
> SELECT 'maintenance_work_orders' AS t, m.id, m.company_id AS owner, w.company_id AS fk_owner
>   FROM maintenance_work_orders m JOIN work_centers w ON w.id = m.work_center_id
>  WHERE w.company_id <> m.company_id
> UNION ALL SELECT 'maintenance_work_orders.schedule', m.id, m.company_id, s.company_id
>   FROM maintenance_work_orders m JOIN maintenance_schedules s ON s.id = m.schedule_id
>  WHERE s.company_id <> m.company_id
> UNION ALL SELECT 'maintenance_schedules', s.id, s.company_id, w.company_id
>   FROM maintenance_schedules s JOIN work_centers w ON w.id = s.work_center_id
>  WHERE w.company_id <> s.company_id
> UNION ALL SELECT 'maintenance_logs', l.id, l.company_id, w.company_id
>   FROM maintenance_logs l JOIN work_centers w ON w.id = l.work_center_id
>  WHERE w.company_id <> l.company_id;
> ```
>
> Empty result → nothing to do. Non-empty → those rows need repointing at a machine the owning
> company actually has.

### Tool & Fixture Management (tool crib)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (tools, dashboard, checked-out, replacement-due, inspection-due, history) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit tool (`POST /tool-management/tools/`, `PUT .../tools/{id}`) | ✓ | ✓ | ✓ | | | | |
| Retire tool (`DELETE /tool-management/tools/{id}`) | ✓ | ✓ | ✓ | | | | |
| Check out / check in (`POST .../tools/{id}/checkout`, `.../checkin`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| Log usage (`POST .../tools/{id}/log-usage`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |

> ⚠️ **This table is new because the router had NO role gating at all.** Every handler was
> `Depends(get_current_user)`, so a **Viewer could create, edit and retire tools** and check them in
> and out, with no audit row naming who.
>
> **Managing a tool record follows the inventory stock mutators** (`inventory.STOCK_MUTATOR_ROLES` —
> the Inventory Adjust / Issue / Receive rows, Admin / Manager / Supervisor), as `TOOL_PLANNING_ROLES`
> in `app/api/endpoints/tool_management.py`: a tool is crib inventory. **Using a tool**
> (`TOOL_USE_ROLES`) is open to every tenant role except **Viewer**. `/tool-management` is
> route-gated on `inventory:view`, which every role holds, and the page offers checkout and checkin
> to all of them, so the read-only Viewer is the only role that loses anything. Kiosk operator tokens
> are path-fenced to the shop-floor routes, so there is no kiosk caller. Superuser / Platform Admin
> bypass, as elsewhere. Recording a tool as customer- or supplier-owned (AS9100D 8.5.3, migration
> `129`) is part of create / edit and takes that same gate; there is no separate ownership verb for
> tools.
>
> **Reads stay open to every role, and are now tenant-scoped.** The history read resolved a tool by
> bare id, and the dashboard and the checked-out / replacement-due / inspection-due lists aggregated
> across tenants. Every state change now writes an `audit_log` row (`tool`, `tool_usage_log`). See
> `docs/API.md` → Tool & Fixture Management.
>
> ⚠️ **The client has no matching gate yet.** `ToolManagement.tsx` renders its write controls
> unconditionally, so a Viewer sees controls the server now refuses and gets a 403 toast. Pairing the
> client gate with the server gate is a follow-up (Maintenance above has since been paired).

### Supplier Scorecards, Audits & Approved Supplier List

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard, ranking, history, lists, detail, due-soon) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit scorecard | ✓ | ✓ | | | | | |
| Auto-calculate scorecard (`POST .../calculate/{vendor_id}`) | ✓ | ✓ | | | | | |
| Create / edit supplier audit | ✓ | ✓ | | | | | |
| Create / edit **legacy** ASL entry (`/supplier-scorecards/approved-suppliers`) | ✓ | ✓ | | | | | |
| Record a supplier-approval decision / import the ASL **on the vendor record** — see [Purchasing](#purchasing) | ✓ | ✓ | | | ✓ | | |
| Open the **Supplier Scorecards** screen (route + nav: `purchasing:view` or `quality:approve`) | ✓ | ✓ | ✓ | | ✓ | | ✓ |

> **The Approved Supplier List moved to the vendor record (migration `123`).** The page's
> **Approved Supplier List** tab now reads vendors and writes through `PUT
> /purchasing/vendors/{id}/approval` and the ASL import, whose roles (Admin / Manager / **Quality**)
> are in the [Purchasing](#purchasing) table. The legacy `approved-suppliers` routes keep the Admin /
> Manager gate above and are unchanged, but the page no longer calls them.

> **The write tiers are unchanged** — this router already gated its writes to Admin / Manager. The
> table is recorded here because it never was, and because the reads are open to every role: a
> supplier's score, audit result and approval status are visible to anyone signed in.
>
> **What changed is tenancy, not roles.** Fifteen of sixteen handlers reached outside the caller's
> company, including three cross-tenant **writes** (`PUT` on scorecard / supplier audit / ASL entry
> each resolved its row by bare id — the scorecard handler took a `company_id` dependency and never
> used it). Every state change now also writes a tamper-evident `audit_log` row
> (`supplier_scorecard`, `supplier_audit`, `approved_supplier`); the router previously wrote none,
> so who downgraded a supplier to `Disqualified` or flipped an ASL entry to `removed` was
> unrecoverable. See `docs/API.md` → Supplier Scorecards.

> **Before deploying, check for legacy cross-tenant rows** (same reasoning as Maintenance above):
>
> ```sql
> SELECT 'supplier_scorecards' AS t, x.id, x.company_id AS owner, v.company_id AS fk_owner
>   FROM supplier_scorecards x JOIN vendors v ON v.id = x.vendor_id WHERE v.company_id <> x.company_id
> UNION ALL SELECT 'supplier_audits', a.id, a.company_id, v.company_id
>   FROM supplier_audits a JOIN vendors v ON v.id = a.vendor_id WHERE v.company_id <> a.company_id
> UNION ALL SELECT 'approved_supplier_list', p.id, p.company_id, v.company_id
>   FROM approved_supplier_list p JOIN vendors v ON v.id = p.vendor_id WHERE v.company_id <> p.company_id;
> ```
>
> A legacy ASL row is the one that also needs manual repair rather than just repointing: it
> permanently consumes the vendor's single **global** ASL slot, and the owning company can neither
> see nor edit it (both endpoints are now scoped), so it gets a 400 "Vendor already has an ASL
> entry" with no self-service recovery.

### Process Sheets

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | ✓ | | |
| Edit (draft sheet + steps) | ✓ | ✓ | ✓ | | ✓ | | |
| Delete (draft only) | ✓ | ✓ | ✓ | | ✓ | | |
| New revision | ✓ | ✓ | ✓ | | ✓ | | |
| Release | ✓ | ✓ | | | ✓ | | |
| Obsolete | ✓ | ✓ | | | ✓ | | |

> **Role split — endpoint mapping (`feat/process-sheets-library`).** All `/api/v1/process-sheets`
> writes are gated by decorator-level `require_role` in `app/api/endpoints/process_sheets.py`:
> **authoring** (create, header edit, step CRUD, soft-delete, new-revision) carries
> `AUTHOR_ROLES = [ADMIN, MANAGER, SUPERVISOR, QUALITY]`; **release** and **obsolete** carry
> `RELEASE_ROLES = [ADMIN, MANAGER, QUALITY]`. Unlike Routings, **Quality** participates in both
> sets — process sheets are inspection documents, and quality owns released inspection content
> (release-adjacent authority), while release stays narrower than authoring, mirroring the
> Routings draft-edit vs release split. Mutability is status-gated in the service: only **draft**
> sheets are editable — header edits, step CRUD, and delete on a released/obsolete sheet return
> **409** (create a new revision instead). GET endpoints depend on `get_current_user` only
> (tenant-scoped, read-broad — see the access enforcement model above). Superuser / Platform Admin
> bypass role checks, as elsewhere.

### Consumables

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View stock, employee picker and checkout history | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Record checkout to an employee | ✓ | ✓ | ✓ | | | | |
| Export filtered checkout history (CSV) | ✓ | ✓ | | | | | |
| Edit / delete / void a posted checkout | | | | | | | |

`GET /consumables/metadata`, `/stock`, and `/checkouts` use the tenant-scoped
operational read policy. The in-app `/consumables` route uses `inventory:view`.
The metadata exposes only numeric user ID, name, department and active
state; it does not grant access to administrative `/users/` data. Inactive past
recipients can be used as history filters but cannot receive a new issue.

`POST /consumables/checkouts` requires `ADMIN`, `MANAGER` or `SUPERVISOR`, matching
existing inventory stock mutations. The server sets the issuer and timestamp;
the selected employee is the recipient, not the authenticated actor. Stock,
ledger, immutable employee attribution and required audits commit atomically.
The existing Platform Admin/superuser bypass remains subject to company scope
and the read-only context fence. Metadata `can_manage` and `can_export` reflect
the available actions. There is no separate consumables-attendant role.

`GET /consumables/checkouts/export.csv` follows the existing **Admin/Manager bulk
export** gate and writes an `EXPORT` audit record. See [Consumables](CONSUMABLES.md)
for setup, retry handling and the initial correction limitations.

### Inventory

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Adjust | ✓ | ✓ | ✓ | | | | |
| Issue | ✓ | ✓ | ✓ | | | | |
| Receive | ✓ | ✓ | ✓ | | | | |
| Transfer | ✓ | ✓ | ✓ | | | | |
| Change a stock row's ownership (`POST /inventory/items/{item_id}/ownership`) | ✓ | ✓ | ✓ | | | | |
| **Preview a SKU combine** | ✓ | ✓ | ✓ | | | | |
| **Combine two SKUs** (`inventory:combine`) | ✓ | ✓ | | | | | |
| Create location | ✓ | ✓ | | | | | |
| Create / complete cycle count | ✓ | ✓ | ✓ | | | | |
| Assign counter / review / post reviewed count adjustments | ✓ | ✓ | ✓ | | | | |
| Start (open) cycle count | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| Record count on an item | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |

**Advisory physical-piece observations — separate evidence register.** The following
gates apply to `/api/v1/inventory/stock-piece-sources` and
`/api/v1/inventory/stock-pieces`, including observation history. They do not grant
availability, reservation, consumption, remnant valuation or certification powers.

| Action | Authoritative server gate |
|---|---|
| Read source candidates, latest observations or immutable history | Active authenticated company context plus effective `inventory:view`. Company role overrides replace the default permission list; a missing/revoked permission returns 403. |
| Register a label, append corrected measurements or withdraw an observation | Effective `inventory:view` **and** existing `ADMIN`, `MANAGER` or `SUPERVISOR` stock-mutator role. A read-only company context refuses writes. No `purchasing:*`, quote approval, spacing-policy approval or cycle-count grant substitutes for this role gate. |
| Reserve, consume, approve eligibility or assign credit | Not provided by this register. |

The existing Superuser/Platform Admin role bypass remains subject to the active
company and read-only write fence. Kiosk credentials cannot use these routes.
API tokens use their bound tenant/user permissions and are checked for revocation
and expiry; each recorded command retains token attribution. Source/history page
envelopes contain the active `company_id` and server-computed `can_record`, allowing
the UI to hide write controls without inventing a second permission policy.

Every command requires `expected_company_id`; corrections/withdrawals additionally
require the expected observation version. UUID replay is scoped to the same
company, actor, credential and command content. A stale version or conflicting
request returns 409; foreign/unknown history/source IDs return 404. Required audit
failure rolls back the entire command. Withdrawal preserves prior evidence and
does not write off inventory. See [the API contract](API.md#advisory-physical-piece-observations)
and [the observation architecture](STOCK_OBSERVATION_ARCHITECTURE.md).

> **Issue — now enforced in code (Admin / Manager / Supervisor).** `POST /api/v1/inventory/issue`
> (`app/api/endpoints/inventory.py`) was previously gated only by `get_current_user` — **any**
> authenticated user in the tenant could issue stock off a lot, with no role restriction. It is now
> `require_role([ADMIN, MANAGER, SUPERVISOR])`, the same set as the sibling stock-mutating
> `/inventory/adjust`, which is what the **Issue** row above records. The endpoint is additionally
> marked **deprecated** in favor of a planned work-order-scoped `POST /work-orders/{id}/issue-material`
> (that replacement is not implemented yet — `/inventory/issue` is still the supported path). See
> `docs/API.md` → Inventory.

> **Receive / Transfer — now enforced in code (Admin / Manager / Supervisor).** `POST
> /api/v1/inventory/receive` and `POST /api/v1/inventory/transfer` previously depended on
> `get_current_user` only, so **any** authenticated user in the tenant — Viewer included — could
> create stock and write a ledger row. Both now carry `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> matching the sibling stock mutators `/inventory/issue` and `/inventory/adjust` and the PO-receipt
> path `POST /receiving/receive`, which writes the same `inventory_items` /
> `inventory_transactions` tables. The **Transfer** row above was previously intended policy the
> server did not enforce; it is now an enforced control, and the new **Receive** row records the
> gate that closed the same gap on the receive path. (An earlier revision of this section described
> both as un-gated drift — that is no longer true.)
>
> The UI layer now agrees with the server on this one: `frontend/src/pages/Inventory.tsx` hides the
> **Receive Inventory** button and drops the per-row **Transfer** action (column and mobile-card
> affordance) for roles without `inventory:adjust` / `inventory:transfer`, which resolve to exactly
> Admin / Manager / Supervisor (+ Platform Admin, who bypasses `require_role` server-side). Per the
> [Access enforcement model](#access-enforcement-model) the UI gate remains cosmetic — the server
> gate is the control.

> **Change a stock row's ownership (AS9100D 8.5.3, migration `129`).** `POST
> /api/v1/inventory/items/{item_id}/ownership` re-owns one stock row as company, customer or supplier
> property. Its gate is `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]`, the
> same tier as Adjust / Receive / Transfer. The Inventory page shows the **Change ownership**
> row action to the roles holding `inventory:adjust`, which resolve to the same three (plus Platform
> Admin). Every call needs a `reason` and writes a required audit row (**503**, nothing changed, if it
> cannot). Receiving owned material in the first place uses the existing receive gates
> (`/inventory/receive`, `POST /receiving/receive`, `/receiving/deliveries`); no role changed.
>
> **API tokens are not refused.** Owner decision 2026-11-20: a full API token bound to an Admin,
> Manager or Supervisor may change a row's ownership (the route has no credential check, and it is
> the MCP tool `change_inventory_item_ownership`) and may record ownership at receipt: `POST
> /inventory/receive`, `POST /receiving/receive` and each line of `POST /receiving/deliveries` take
> the ownership fields from any caller their role gate admits. Read-only and AI-external tokens are
> refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate). The write is attributed to the token's bound user with
> `extra_data.credential`. See
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes). Hank's
> `receive_delivery` task, by contrast, can only receive company stock: its line schema has no
> ownership fields.

> **Combine two SKUs — endpoint mapping, and why the write is NARROWER than Adjust.**
>
> | Step | Endpoint | Gate |
> |------|----------|------|
> | Preview (pure read — writes nothing) | `POST /inventory/combine/preview` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
> | Perform the combine | `POST /inventory/combine` | `require_role([ADMIN, MANAGER])` |
>
> **A Supervisor can look, and gets 403 on the write.** That split is the whole point. Adjusting one
> lot corrects a **count**; folding two part numbers together changes which **article** the material
> *is* — a controlled change to article identity under AS9100D 8.5.2. So the write sits with
> `POST /parts/{id}/renumber` and `POST /parts/{id}/revision` (both ADMIN/MANAGER), **not** with the
> stock-mutator tier that `inventory:adjust` / `inventory:transfer` occupy. The preview stays wider
> because a supervisor investigating *"why do we have this sheet on two numbers?"* needs to be able to
> look, and it is structurally incapable of writing anything (`build_combine_preview` takes no
> `AuditService` and no actor id — the same posture as `GET /parts/{id}/renumber-impact` and the
> backflush-readiness reads).
>
> **The client permission key is `inventory:combine`**, held by `platform_admin`, `admin` and
> `manager` only (`frontend/src/utils/permissions.ts`), so the hidden control and the refused call
> agree. It is deliberately a **narrower** key than `inventory:adjust`, which reaches Supervisor —
> a separate key rather than a reuse, because the two authorize genuinely different acts.
>
> **It is FRONTEND-ONLY, and that is the `parts:renumber` pattern, not an omission.** The key is
> deliberately absent from the backend's `role_permission.py` `ALL_PERMISSIONS` /
> `PERMISSION_CATEGORIES`, exactly as `parts:renumber` is. Per the
> [Access enforcement model](#access-enforcement-model): **the key gates the button; the backend
> `require_role` is the enforcement.** Adding it to the backend catalog would imply a second
> authorization surface that nothing consults.
>
> The paired part-activation verbs shipped with this feature — `POST /parts/{id}/deactivate` and
> `POST /parts/{id}/activate`, used to retire the folded-away SKU — carry the **same**
> `require_role([ADMIN, MANAGER])` gate, for the same reason. See the Parts section below and
> `docs/API.md` → Inventory → "Combining two SKUs".

> **Cycle counts — endpoint mapping.** On `app/api/endpoints/inventory.py`:
>
> | Step | Endpoint | Gate |
> |------|----------|------|
> | Create + enroll stock rows | `POST /inventory/cycle-counts` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
> | Open for counting | `POST /inventory/cycle-counts/{id}/start` | `require_role(COUNT_WRITE_ROLES)` = everyone except Viewer |
> | Record a counted quantity | `POST /inventory/cycle-counts/{id}/items/{item_id}/count` | `require_role(COUNT_WRITE_ROLES)` = everyone except Viewer |
> | Complete + post adjustments | `POST /inventory/cycle-counts/{id}/complete` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
>
> **`start` and `record_count` now exclude Viewer — the only cycle-count policy change.** Both were
> bare `get_current_user`, so **Viewer** — the read-only role, which
> `frontend/src/utils/permissions.ts` grants `inventory:view` and nothing else — could open a count
> and write the counted quantities a manager's ledger-posting `complete` derives its adjustment
> from. That is a write, and a quality record at that, so it does not belong to a read-only role.
>
> The gate is deliberately defined by **exclusion**
> (`COUNT_WRITE_ROLES = [ADMIN, MANAGER, SUPERVISOR, OPERATOR, QUALITY, SHIPPING]`, in
> `app/api/endpoints/inventory.py`): every *working* role keeps both verbs, so the entire shop-floor
> counting path is preserved and only the read-only role loses access. Counting is the operator
> task; the privileged steps stay *creating* the count (which enrolls the stock rows) and
> *completing* it (which posts the variance to the ledger), both unchanged at
> `[ADMIN, MANAGER, SUPERVISOR]`.
>
> **Do not narrow `start` further.** An earlier revision of this pass gated it to Admin / Manager /
> Supervisor; that was reverted before merge, because combined with the `record_count` IN_PROGRESS
> requirement below it would leave an operator unable to work a `SCHEDULED` count at all (unable to
> open one, and **409** on any attempt to count into it) — a shop-floor capability regression rather
> than a hardening. The operator path — `start` a scheduled count, then `record_count` into it — is
> pinned by
> `backend/tests/api/test_inventory_hardening.py::test_operator_can_start_a_scheduled_count_and_record_into_it`,
> and every non-Viewer role is pinned by `test_start_allowed_for_every_working_role` /
> `test_record_count_allowed_for_every_working_role`.
>
> Beyond authorization, the hardening pass added **integrity guards and audit coverage** to these
> two steps:
>
> - `start` returns **409** on a terminal count (`COMPLETED` / `CANCELLED`) — re-opening a completed
>   count would let a second `complete` double-post the same physical variance to the ledger.
> - `start` writes a `cycle_count` STATUS_CHANGE audit row on the real `SCHEDULED → IN_PROGRESS`
>   transition, and preserves the original `started_at` when an already-started count is re-assigned
>   (audited as an UPDATE of `assigned_to`, not a fabricated second transition).
> - `record_count` returns **409** unless the parent count is `IN_PROGRESS`. A counted quantity is
>   the quality record the variance adjustment derives from: once the count is closed that record is
>   evidence and must not be overwritten, and before it is opened there is nothing to count against.
> - `record_count` writes a `cycle_count_item` **UPDATE** audit row on every counted quantity. A
>   re-POST while the count is still `IN_PROGRESS` is legal (a genuine re-count) but silently
>   replaces `counted_quantity` / `variance` / `counted_by` — the audit row is the only surviving
>   record of what it overwrote.
> - `POST /inventory/cycle-counts` writes a `cycle_count` **CREATE** audit row carrying the declared
>   scope (`warehouse`, `location_code`, `part_id`) and the number of stock rows enrolled, so "who
>   scoped this count, and which rows did `complete` later adjust" is answerable from the hash chain.
>
> Also changed by the same hardening pass:
> `record_count` is now **tenant-scoped** — it resolves the parent count *and* the count item
> against the active company (**404** otherwise), where it previously matched on ids alone. That was
> a real authorization defect in the code, though not an exploitable one in the field: the only
> writer of `cycle_count_items` is `POST /inventory/cycle-counts`, which omitted the NOT NULL
> `company_id` stamp and therefore always failed with `IntegrityError` whenever it enrolled a row,
> so no cross-tenant `cycle_count_items` row could exist to be written onto (rows predating
> migration `026_add_multi_tenancy` were backfilled to the single seeded company). `POST
> /inventory/cycle-counts` now enrolls only the active company's inventory rows and stamps
> `company_id` on each `CycleCountItem`, and `.../complete` adjusts only this company's stock.
> See `docs/API.md` → Inventory for the lifecycle guards (**409** on terminal counts, **404** on an
> unresolvable `location_code`) and the audit rows these steps now write.

### Purchasing

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Approve | ✓ | ✓ | | | | | |
| Record supplier confirmation / assign follow-up | ✓ | ✓ | ✓ | | | | |
| Delete / restore vendor (soft) | ✓ | ✓ | | | | | |
| Delete / restore purchase order (soft) | ✓ | ✓ | | | | | |
| Record a supplier-approval decision (`PUT /purchasing/vendors/{id}/approval`) | ✓ | ✓ | | | ✓ | | |
| Import the Approved Supplier List (`POST /purchasing/vendors/asl-import/preview`, `/commit`) | ✓ | ✓ | | | ✓ | | |
| Edit the supplier quality flow-down settings and clause catalog (`PUT /purchasing/flowdown-profile`, `POST /purchasing/quality-clauses`, `PUT /purchasing/quality-clauses/{id}`) | ✓ | ✓ | | | ✓ | | |
| Choose a draft PO's quality clauses (`quality_clause_codes` on PO create / `PUT`) | ✓ | ✓ | ✓ | | | | |

Supplier confirmation also rechecks the company's `purchasing:view` and
`purchasing:create` permissions at save time and refuses read-only company contexts.
Follow-up owners must be active users in the same company with purchasing access.

> **Read enforcement:** Per the [Access enforcement model](#access-enforcement-model),
> Purchasing list/detail reads (`list_vendors`, `list_purchase_orders`, and the
> single-record GETs in `app/api/endpoints/purchasing.py`) are tenant-scoped but **not**
> role-restricted — any authenticated user in the tenant can read vendor and PO data, so
> the **View** row above reflects intended UI visibility rather than a server-enforced
> restriction. Only the write/approve actions (Create, Approve, send, line edits) are
> role-gated. Receiving (below) follows the same read-broad / write-restricted pattern.

> **Approved Supplier List writes are Admin / Manager / Quality, and a person's only (migration
> `123`, 2026-11-13).** `ASL_WRITE_ROLES` in `app/api/endpoints/purchasing.py` gates `PUT
> /purchasing/vendors/{id}/approval` and both ASL import routes. **Quality** sits beside Admin /
> Manager here, although the general vendor edit stays Admin / Manager, because the QL 7.4 form's
> owner is "Purchasing / Quality". Superuser / Platform Admin bypass, as elsewhere. All three routes
> refuse an **API token** (403) before any write and are not MCP tools — see
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes). The two ASL
> reads, `GET /purchasing/vendors/reevaluation-due` and `POST /purchasing/supplier-approval-check`,
> are open to any authenticated user in the tenant, like the other purchasing reads.
>
> - **The Admin / Manager vendor edit cannot go around it.** On a vendor with an ASL status,
>   `is_approved` follows the status, and `PUT /purchasing/vendors/{id}` refuses **409** an
>   `is_approved` that contradicts it — so the vendor-edit tier cannot mark a Disqualified or
>   Conditional supplier approved. The Purchasing → Vendors **Edit** form locks its Approved checkbox
>   for such a vendor and leaves `is_approved` out of the save.
> - **The Disqualified refusal is a state rule, not a role rule.** A purchase order to a vendor
>   Disqualified on the ASL is refused **409** for every role at every PO-create door, and an
>   existing one cannot be approved, submitted for approval, given lines, sent or emailed (`docs/API.md`
>   → Purchasing → *Approved Supplier List on the vendor record*).
> - **Granting approval by the vendor checkbox is a person's too.** An API token is refused (403)
>   creating a vendor with `is_approved: true`, raising it through the vendor `PUT`, or importing a
>   vendor CSV that asks for it — see
>   [Credential policy for judgment writes](#credential-policy-for-judgment-writes). The vendor `PUT`
>   row-locks the vendor and refuses an explicit `is_approved: null` (422).
> - **UI.** The **Update approval** and **Import ASL** controls on Supplier Scorecards → Approved
>   Supplier List render only for `canWriteAsl` (Admin / Manager / Quality, or a superuser;
>   `frontend/src/utils/supplierApproval.ts`). It omits Platform Admin, matching the other purchasing
>   write controls. **The Quality role opens that page** (lead decision L2, 2026-11-15: the ASL's
>   owner is Purchasing / Quality). `/supplier-scorecards` is route- and nav-gated on
>   `anyOf: ['purchasing:view', 'quality:approve']` (`frontend/src/utils/routeAccess.ts`; the sidebar
>   reads the same requirement through `canAccessPath`, so the two cannot disagree). The shipped
>   Quality set has no `purchasing:view`, and `quality:approve` is held by exactly Platform Admin /
>   Admin / Manager / Quality — the ASL approval roles — so the change adds Quality and nobody else
>   (`quality:view` would also have admitted Operator). No server gate moved: every read the page
>   makes (`GET /supplier-scorecards/supplier-scorecards/dashboard`, `/supplier-scorecards/`,
>   `/supplier-audits/`, `GET /purchasing/vendors`, `/purchasing/vendors/reevaluation-due`) was
>   already open to any signed-in user of the tenant, and no purchasing write was widened. Because
>   scorecard and supplier-audit writes stay Admin / Manager, the page hides **New Scorecard** /
>   **New Audit** from every other role (`canManageSupplierScorecards`,
>   `frontend/src/utils/recordWriteAccess.ts`) rather than offer a guaranteed 403.
> - **No same-name twin of a Disqualified supplier** (lead decision L1, 2026-11-15). A state rule
>   on every door that creates a vendor, for every role: a name that normalizes (the ASL import's
>   own rule) to a live vendor Disqualified on the ASL is refused **409** by `POST
>   /purchasing/vendors` and the PO upload's create-new-vendor path, and is a row error in the vendor
>   CSV import and the Import Center vendor batch (`docs/API.md` → Purchasing → *Approved Supplier
>   List on the vendor record*).

> **Supplier quality flow-down writes are Admin / Manager / Quality (migration `128`, AS9100D
> 8.4.3, 2026-11-19).** `FLOWDOWN_WRITE_ROLES` in `app/api/endpoints/purchasing.py` gates `PUT
> /purchasing/flowdown-profile`, `POST /purchasing/quality-clauses` and `PUT
> /purchasing/quality-clauses/{id}`: the requirements are Quality's document, so Quality sits beside
> Admin / Manager as on the ASL writes. Superuser / Platform Admin bypass, as elsewhere. The two
> reads, `GET /purchasing/flowdown-profile` and `GET /purchasing/quality-clauses`, and the PO
> print data that carries the block are open to any authenticated user in the tenant, like the other
> purchasing reads. Each write is required-audited (**503**, nothing saved, when the row cannot be
> written).
>
> - **API tokens are not refused** on these writes. Owner decision 2026-11-20: a full API token of an
>   Admin / Manager / Quality user may edit the flow-down settings and clause catalog; read-only and
>   AI-external tokens are refused by their own gates (the read-only gate, and for an AI-external token also the AI-egress gate); the write is attributed to the token's
>   bound user with `extra_data.credential`. All five routes are MCP tools. For comparison, the ASL
>   writes above refuse API tokens. See
>   [Credential policy for judgment writes](#credential-policy-for-judgment-writes).
> - **A PO's clause choice rides the PO's own gate.** `quality_clause_codes` on `POST
>   /purchasing/purchase-orders` and `PUT /purchasing/purchase-orders/{id}` takes the PO create /
>   edit tier (Admin / Manager / Supervisor), so a Supervisor can pick clauses on a draft but cannot
>   edit the catalog. Once the PO has left `draft`, changing it is **409** for every role: a state
>   rule, not a role rule (`docs/API.md` → Purchasing → *Quality flow-down on the purchase order*).
> - **UI: one editor, two tabs.** The same settings component renders as **Purchasing → Quality
>   flow-down** (`frontend/src/pages/Purchasing.tsx`) and as **Quality → PO Flow-down**
>   (`frontend/src/pages/Quality.tsx`, deep link `/quality?tab=flowdown`). Both are gated on
>   `canManageFlowdown` (Admin / Manager / Quality, or a superuser), and both omit Platform Admin,
>   matching the other purchasing write controls. `?tab=flowdown` is honored only behind that gate;
>   any other Quality-page reader stays on the tab they are on.
> - **Why the Quality page carries it.** `/purchasing` is route- and nav-gated on `purchasing:view`
>   (`frontend/src/utils/routeAccess.ts`), and the shipped Quality permission set has no
>   `purchasing:view` (neither `frontend/src/utils/permissions.ts` nor the backend defaults in
>   `app/models/role_permission.py`). `/quality` is gated on `quality:view`, which Quality holds, so a
>   Quality user edits the flow-down from Quality → PO Flow-down with no permission change. No route
>   gate moved and no server gate changed.
> - **What a Quality user still cannot open by default:** the Purchasing page itself and the PO
>   print page (`/print/purchase-order`, also `purchasing:view`), so they cannot open a purchase
>   order to see the block printed on it. An Admin can grant `purchasing:view` to the Quality role
>   (`PUT /admin/settings/role-permissions/quality`, audited); that also opens every other screen
>   gated on the permission, such as Purchase Orders, Quotes and Customers.

> **PO status edits: a state rule landed, and the Approve row is still not enforced on `PUT`
> (2026-11-05).** `PUT /purchasing/purchase-orders/{id}` is `require_role([ADMIN, MANAGER,
> SUPERVISOR])` and used to write any status, so a Supervisor could mark a PO `sent` without the
> Admin / Manager `POST .../send`, declare it `received`, or reopen a closed one. A transition
> whitelist now refuses those moves (**409** naming the verb that owns each one). That makes the send
> gate real: `sent` is reachable only through the Admin / Manager send verb. The whitelist is a
> **state** rule, not a role rule, and it deliberately leaves one gap open. `pending_approval` and
> `approved` are still settable through that `PUT`, so **a Supervisor can still move a PO to
> `approved`**, and nothing stamps `approved_by` / `approved_at`. The **Approve** row above (Admin /
> Manager) is therefore **not server-enforced** on this path. A dedicated Admin / Manager approve verb
> is an open follow-up. Until it ships, read an `approved` status as "set by someone at the PO edit
> tier", and find who in the PO's `purchase_order` UPDATE audit row. See `docs/API.md` → Purchasing →
> *PO status edits*.

> **Vendor / PO soft-delete + restore — endpoint mapping.** `DELETE /purchasing/vendors/{id}`,
> `POST /purchasing/vendors/{id}/restore`, `DELETE /purchasing/purchase-orders/{id}`, and
> `POST /purchasing/purchase-orders/{id}/restore` (`app/api/endpoints/purchasing.py`) are enforced **in
> code** to the two **Delete / restore** rows above — `require_role([ADMIN, MANAGER])`. `Vendor` and
> `PurchaseOrder` gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`), so these are
> **soft** deletes (never physical — invariant #3): the row is flagged `is_deleted`, drops out of all
> reads, and is restorable. Both the delete and the restore write a tamper-evident `audit_log` row.
> Guardrails are server-enforced: a **vendor** delete also deactivates it (`is_active=false`, after
> recording the prior value so the restore can put it back — see below) and is refused (**400**)
> while it has an active PO; a **PO** delete is refused (**400**) when any line has
> received material (void the receipts first); and opening a PO against a soft-deleted/inactive vendor
> is refused (**404**). See `docs/API.md` → Purchasing.
>
> **The PO delete is reachable from two pages** — the Purchasing PO list and the **open-PO list on
> Warehouse → Receiving** — but it is one endpoint under one gate. The Receiving surface does **not**
> inherit the Receiving rows below: a Supervisor who may receive, correct a receipt and clear an
> inspection hold still sees **no** delete control, because this stays `require_role([ADMIN,
> MANAGER])`.

> **PO restore — the read and the verb are gated differently, on purpose.** Restoring a PO needs two
> things: a way to *see* deleted POs, and permission to bring one back. They sit at different tiers,
> and the split is deliberate rather than an oversight:
>
> | Half | Where | Gate |
> |---|---|---|
> | **See** deleted POs | `GET /purchasing/purchase-orders?deleted_only=true` — the only read in the API that returns a soft-deleted PO | **Any authenticated user in the tenant** (`get_current_user`) |
> | **Restore** one | `POST /purchasing/purchase-orders/{id}/restore` | **Admin / Manager** (`require_role([ADMIN, MANAGER])`) — the *Delete / restore purchase order (soft)* row above |
>
> **Why the read carries no new gate:** `deleted_only=true` returns rows the same reader could
> already see *before* they were deleted — PO list/detail reads are tenant-scoped but not
> role-restricted (see *Read enforcement* above), so a PO that was readable on Monday does not become
> sensitive by being deleted on Tuesday. Gating the view would protect nothing while making the
> feature unusable: a manager cannot restore what a screen refuses to list. The privileged act is
> changing state, and that gate lives where the state changes — on the verb.
>
> **The UI is STRICTER than the server, and knowing which way round matters.** The Restore *button*
> matches the verb exactly: Purchasing → Purchase Orders → **Deleted** (an Active / Deleted segmented
> control) renders it only for Admin / Manager (`canRestorePO` in
> `frontend/src/pages/Purchasing.tsx`, a constant kept separate from the delete gate so a future
> change to one cannot silently move the other), so a hidden control and a refused call agree. The
> *view*, though, is narrower in the UI than on the server: `/purchasing` is gated on
> `purchasing:view` (`routeAccessRequirements` in `App.tsx`), which per
> `frontend/src/utils/permissions.ts` belongs to platform_admin / admin / manager / supervisor /
> viewer — so an **operator, quality or shipping** user cannot open the page at all, while their
> token *can* read the same rows straight from `GET /purchasing/purchase-orders`. Do not read the
> table above as "the UI reflects the endpoint": it does not, and the gap is the endpoint's, not the
> screen's.
>
> That looseness is **not specific to `deleted_only`** — it is how every read on this endpoint has
> always behaved, deleted or live, and gating the flag alone would be theatre (the same rows are
> readable pre-delete and via `?status=closed`) while breaking the feature, since a supervisor could
> no longer locate a PO for a manager to restore. The right fix is to bring `GET
> /purchasing/purchase-orders` and `GET /purchasing/purchase-orders/{id}` as a whole under
> `require_role([PLATFORM_ADMIN, ADMIN, MANAGER, SUPERVISOR, VIEWER])` so the server matches
> `purchasing:view` — vendor names, unit pricing and order totals are not an operator's or shipping
> clerk's to read. **That is an open owner decision and a separate change.** It applies verbatim to
> `GET /purchasing/vendors` — including its `deleted_only` view — for the same reasons and with the
> same non-fix; the vendor half of this note is below.
>
> Both directions stay audited (`log_delete` with `soft_delete=true`; `log_update` with
> `action="restore"`), so who deleted and who restored are both on the chain.
>
> **Vendor restore is gated the same way, and it now has a screen.** It was for a long time the
> asymmetric case — the verb existed and was audited while nothing could reach it — and that is
> closed: `GET /purchasing/vendors?deleted_only=true` lists the tenant's soft-deleted vendors, and
> Purchasing → **Vendors** → **Deleted** renders them with a **Restore** control. The split is
> identical to the PO one above, deliberately, so there is one posture to learn rather than two:
>
> | Half | Where | Gate |
> |---|---|---|
> | **See** deleted vendors | `GET /purchasing/vendors?deleted_only=true` — the only read in the API that returns a soft-deleted vendor | **Any authenticated user in the tenant** (`get_current_user`) |
> | **Restore** one | `POST /purchasing/vendors/{id}/restore` | **Admin / Manager** (`require_role([ADMIN, MANAGER])`) — the *Delete / restore vendor (soft)* row above |
>
> **Why the read carries no new gate** — same argument, and it is not a copy for tidiness: a vendor
> that was readable on Monday does not become sensitive by being deleted on Tuesday, vendor
> list/detail reads are tenant-scoped but not role-restricted (see *Read enforcement* above), and
> gating the view would protect nothing while making the feature unusable — a manager cannot restore
> what a screen refuses to list. The privileged act is changing state, and that gate lives on the
> verb. The **UI** matches the verb exactly (`canRestoreVendor` in
> `frontend/src/pages/Purchasing.tsx`, a constant kept separate from `canRestorePO` and from the
> delete gate so a change to one cannot silently move the others), so a hidden control and a refused
> call agree. The same UI-is-stricter-than-the-server caveat applies as for POs: `/purchasing` is
> gated on `purchasing:view`, so operator / quality / shipping users cannot open the page while their
> token can still read the rows straight from the endpoint. That is the endpoint's gap, not the
> screen's, and it is the open owner decision noted above.
>
> **One thing the role gate does *not* decide: what a restore reactivates.** Restoring a vendor puts
> back the `is_active` it had when it was deleted — it is **not** an unconditional re-activate, which
> is what the verb used to do. A supplier the shop deliberately switched off before deleting comes
> back switched off, and a manager who restores it has not thereby re-approved it. A vendor deleted
> **before** migration `082` has no recorded prior state and, by owner decision, restores **inactive**
> as well — the safe reading of an unknown approval flag is *off*, so the restore never hands back an
> active-looking supplier on a guess. That is an
> AS9100D 8.4 property of the approved-supplier list rather than an RBAC one, but it is the reason a
> Manager-level restore is safe to offer at all: the verb can undo a records mistake without being
> able to hand back supplier approval as a side effect. Re-activating stays a separate, separately
> audited `PUT /purchasing/vendors/{id}` under the same Admin / Manager gate, performed from
> Purchasing → **Vendors** → **Inactive** → **Edit** (the third view of the same segmented control;
> a restored-but-switched-off vendor is on no other screen, so without it that "separate, deliberate
> step" would have no way to be taken). The UI gate there is `canCreateVendor`
> (`purchasing:approve` — the same Admin / Manager set the `PUT` enforces), not `canRestoreVendor`:
> the button calls the update endpoint, so it must match the update endpoint. See `docs/API.md` →
> Purchasing → *Restoring a vendor returns the record, not the approval*.

### Quotes

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (list, detail, conversion plan, quote PDF) | ✓ | ✓ | ✓ | | | | ✓ |
| Edit terms / accept / reject / expire (`PUT /quotes/{id}`) | ✓ | ✓ | ✓ | | | | |
| Mark sent (`POST /quotes/{id}/send`) | ✓ | ✓ | ✓ | | | | |
| Convert to work orders (`POST /quotes/{id}/convert`) | ✓ | ✓ | ✓ | | | | |

> **Endpoint mapping (2026-11-05).** The three write verbs share one constant, `QUOTE_WRITE_ROLES =
> [ADMIN, MANAGER, SUPERVISOR]` in `app/api/endpoints/quotes.py`, enforced with `require_role`
> (superuser and Platform Admin pass, as elsewhere). It is a fixed role gate: company role-permission
> overrides neither grant nor remove it. The set is the one `convert` was already gated to, and the
> roles that hold `purchasing:create` by default, which Fabrication Quoting requires to approve the
> estimate that becomes the ERP quote. Until this change `PUT` and `/send` used `get_current_user`, so
> any authenticated user, a Viewer included, could accept, reject, expire, re-word or mark sent a
> customer quote. Each write is now also audited on the tamper-evident chain (see `docs/API.md` →
> Quotes).
>
> **Line drawing revisions (migration `122`).** Entering each line's drawing revision is the `PUT`
> verb's `line_revisions`, so it sits on the **Edit terms** row with no new role. The revision gate
> that refuses sending, the quote PDF and quote email while a line lacks one (**409**) is a record-state
> rule, not a permission: it applies to every role, superuser and Platform Admin included. See
> `docs/API.md` → Quotes → "Drawing revision on every quote line".
>
> **Reads stay read-broad.** Per the [Access enforcement model](#access-enforcement-model), the list,
> detail, conversion-plan and PDF routes are tenant-scoped but not role-restricted; the **View** row is
> the intended UI. In the UI, `/quotes` requires `purchasing:view` (`frontend/src/utils/routeAccess.ts`),
> so Operator / Quality / Shipping cannot open the page, and a Viewer who can open it no longer sees
> **Edit draft**, **Send** or **Convert** (`canWriteQuotes` in
> `frontend/src/utils/recordWriteAccess.ts`, which mirrors `QUOTE_WRITE_ROLES`). The **Email** action
> uses the same helper: quote email delivery (`DocumentDeliveryService.authorize`) admits the same
> Admin / Manager / Supervisor set plus superuser and Platform Admin, which the page's old inline role
> list left out.

### Material Nesting (retired)

The Material Nesting estimator (`/nest`) and every `/api/v1/quote-nesting/*` route it used —
buyer PDF, catalog material resolution, team drafts, saved calculations, spacing-policy
governance and original-DXF attachments — were removed in commit `3e17456e` (2026-09-16).
Their permission rules went with them: `/nest` now only redirects to `/fabrication-quotes`,
and the API answers **404** for every one of those paths. No permission key was added or
removed by the retirement; `purchasing:view` / `purchasing:create` keep their other uses.
For the estimator that replaced it, see [FABRICATION_QUOTING.md](FABRICATION_QUOTING.md).
The tables those routes wrote are kept as historical records with no API reader or writer.

### Receiving

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Post multi-line delivery / upload linked certificate | ✓ | ✓ | ✓ | | | | |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Correct receipt (in place) | ✓ | ✓ | ✓ | | | | |
| Void receipt (soft-delete) | ✓ | ✓ | | | | | |
| Clear inspection hold (waive) | ✓ | ✓ | ✓ | | ✓ | | |
| Print / reprint receiving label | ✓ | ✓ | ✓ | | | | |
| Configure print profile | ✓ | | | | | | |

The new multi-line delivery and certificate writes also recheck the company's
`receiving:view` and `receiving:create` permissions at submission time and refuse
read-only company contexts. Platform/superuser exemptions follow the existing role
contract; they do not bypass tenant scoping or the read-only context fence.

> **Write enforcement:** The Create and Inspect rows above are now enforced **in code** on
> the canonical `/api/v1/receiving` endpoints (`app/api/endpoints/receiving.py`):
> `POST /receiving/receive` → `require_role([ADMIN, MANAGER, SUPERVISOR])` and
> `POST /receiving/inspect/{receipt_id}` → `require_role([ADMIN, MANAGER, QUALITY, SUPERVISOR])`
> — the receive-capable roles may also complete incoming inspection, plus Quality; adding
> Supervisor is an owner-approved spec change (2026-07-16, previously excluded)
> (superuser / Platform Admin bypass role checks, as elsewhere). This replaces a prior state
> where the receive endpoint was not role-restricted and a duplicate receiving/inspection
> path existed under `/api/v1/purchasing`; that duplicate has been removed, so `/api/v1/receiving`
> is the single source of truth. Receiving reads follow the same read-broad / write-restricted
> pattern noted for Purchasing above.

> **Correct / void receipt — endpoint mapping.** Fixing a mis-keyed receipt is enforced **in code** on
> `app/api/endpoints/receiving.py`:
> - **`PATCH /receiving/receipt/{receipt_id}`** (correct in place — new total quantity + optional
>   traceability fields, required reason) → `require_role([ADMIN, MANAGER, SUPERVISOR])`, the **Correct
>   receipt** row — the same receive-tier set that may `POST /receiving/receive` and post inventory
>   adjustments.
> - **`POST /receiving/receipt/{receipt_id}/void`** (soft-delete + full reversal, required reason) →
>   `require_role([ADMIN, MANAGER])`, the **Void receipt** row — deliberately tighter (void is delete
>   authority; Supervisor can correct but not void).
>
> `POReceipt` gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`); void is a soft
> delete (invariant #3) and is **terminal — there is no restore** (re-receive to redo). Both actions
> require a non-blank `reason`, are fully tamper-evidently audited, and reconcile the PO line, PO
> status, and (dock-to-stock) inventory — the historical `RECEIVE` transaction is never mutated;
> reversal is a signed compensating `ADJUST`. Corrections/voids are refused after the receipt is
> inspected, or once its stock has been allocated/consumed. See `docs/API.md` → Receiving & Inspection.

> **Clear inspection hold — endpoint mapping, and why it matches Inspect exactly.**
> **`POST /receiving/receipt/{receipt_id}/clear-inspection`** (waive an inspection hold placed by
> mistake, required reason) → `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`, the **Clear
> inspection hold** row. This is the non-destructive third post-receipt verb: the receipt and its
> lot/heat/cert are kept exactly as keyed, it is re-classified dock-to-stock
> (`inspection_status = not_required`), the material posts into inventory, and it drops off the
> inspection queue.
>
> **The role list is deliberately identical to Inspect, and that identity is the control.** An earlier
> draft excluded Supervisor on a segregation-of-duties argument: Supervisor is the receiving-clerk tier
> (it holds **Create**, so it is the tier that ticks the "requires inspection" box in the first place),
> and letting it waive the hold would let the same person quietly undo their own click. **The owner
> overruled that, and the reason is a records-integrity one, not convenience.**
>
> Excluding Supervisor closed the **honest** exit and left the **dishonest** one open. A supervisor
> facing a mis-ticked receipt with no manager on site did not leave it sitting on the queue — they put
> it through **Inspect → Visual → Pass**, because Supervisor holds Inspect and always has. That writes
> a named inspector and a timestamp onto an inspection that never happened: a **fabricated quality
> record**, and precisely the AS9100D records-integrity defect PR #127 fixed in code — reintroduced by
> a human instead of by the code. Whoever is trusted to record that a lot **passed** is already trusted
> to record that it never needed inspecting, and of the two records that tier can produce, the
> reasoned, attributed, hash-chained waiver is the strictly more truthful one. So the gate now grants
> the truthful record rather than forcing the false one.
>
> **Keep the two lists in lockstep.** If Inspect and Clear-hold ever diverge again, the tier holding
> Inspect and not Clear-hold gets the fabricated pass back as its only exit. **Void** is the one that
> stays tighter (Admin / Manager) — that is *delete* authority, a different question from whether an
> inspection was owed.
>
> Lockstep includes **`PLATFORM_ADMIN`**, which is why `Receiving.tsx`'s `canClearInspection` lists it
> and its neighbours (`canCorrectReceipt` / `canVoidReceipt` / `canDeletePO`) do not. Platform admin
> sits outside the matrix below — `api/deps.py :: require_role` admits it before the list is read — so
> the frontend convention elsewhere is to omit it and simply be conservative. Here that would be a
> *behavioural* divergence, not a cosmetic one: `frontend/src/utils/permissions.ts` grants
> `platform_admin` the `receiving:inspect` permission that drives the Inspect button, so omitting it
> from Clear-hold would put a context-switched platform admin on the queue holding Inspect and **not**
> Clear-hold — the exact one-way door this list closes, made worse by the fact that the old
> "ask a manager or Quality" hint has been removed. A parametrized frontend test asserts the general
> rule (*no role sees Inspect without Clear Hold*) rather than the four names, so the pair cannot
> drift apart again silently.
>
> | Action | Roles | Rationale |
> |---|---|---|
> | Receive (sets the flag) | Admin / Manager / **Supervisor** | Receive-tier clerical work. |
> | Correct receipt | Admin / Manager / **Supervisor** | Same receive-tier — fixing your own keying. |
> | Inspect (resolves the hold with a real inspection) | Admin / Manager / **Quality** / **Supervisor** | Completing a *real* inspection is receive-tier work, plus Quality. |
> | **Clear inspection hold (waives the hold)** | Admin / Manager / **Quality** / **Supervisor** | **Same list as Inspect, on purpose** — the tier that can record a pass must also be able to record the truthful waiver, or it records the false pass instead. |
> | Void receipt | Admin / Manager | Delete authority, tighter still. |
>
> **What this gate is NOT, stated plainly because this document is what an auditor is shown:** it is
> **not** a two-person rule and never was. Admin, Manager and Supervisor all hold `receiving:create`,
> so every role that can clear a hold can also have placed one — anyone here can waive their own
> mis-click. The control this endpoint provides is **attribution and visibility**, not separation of
> duties: see the paragraph below for what it actually enforces.
>
> The gate is not the only control: the endpoint **requires a non-blank `reason`** (1–500 chars,
> trimmed), writes a tamper-evident `audit_log` **status change** (`pending_inspection` → `accepted`)
> carrying that reason, and emits a `receipt_inspection_cleared` operational event — so a waiver is
> always attributable to a named user with a stated justification (invariant #2). It refuses **409**
> for any receipt not currently `pending_inspection`, which is also the replay guard. Records
> integrity is preserved: `inspection_status` becomes `not_required`, **never `passed`**, and
> `inspection_method` / `inspected_by` / `inspected_at` stay NULL — no inspection happened, so the
> record must not claim one (AS9100D, the PR #127 rule). The waiver is also stamped outside the
> Admin/Manager-only audit log, which matters precisely because **Quality and Supervisor can perform
> this waiver but cannot read `GET /audit/`** — but the two stamps are not equally durable, and the
> difference is worth stating:
>
> - The **`RECEIVE` ledger row** carries `reason_code="INSPECTION_HOLD_CLEARED"` plus the reason in
>   its notes. This is the **durable in-app trace for non-audit-readers**: `inventory_transactions`
>   is append-only in practice (corrections are compensating rows, never edits — invariant 3/6) and
>   it is rendered in-app on Warehouse → Inventory → **Stock Movements**, which Supervisor and
>   Quality can both reach with `inventory:view`.
> - The line appended to **`receipt.notes`** is a **convenience copy on the record itself, not a
>   protected one.** Clearing a hold leaves `inspection_status = not_required`, which is *not* in
>   `_INSPECTED_STATUSES`, so the receipt stays correctable — and `PATCH /receiving/receipt/{id}`
>   (Admin / Manager / **Supervisor**) assigns `receipt.notes` wholesale from the payload, which the
>   Correct dialog prefills into a plain textarea. A supervisor correcting a quantity the next day
>   can retype that box and drop the waiver line without noticing it was one. Nothing is *lost* when
>   that happens — the immutable `audit_log` status-change row and the correction's own `log_update`
>   (carrying old → new `notes`) both survive, as does the ledger row above — but do not describe the
>   on-receipt stamp as tamper-evident. It is durable against every tier except the one that both
>   writes and edits it.
>
> The endpoint additionally fires the `receipt.inspection_cleared` notification to **Manager +
> Quality**.
>
> **The previously recorded "known gap" is closed by this role list**, and closed in the direction the
> owner chose: Supervisor was added here rather than removed from Inspect. The gap was that a
> Supervisor blocked from the waiver still held Inspect and could stamp a fabricated Visual pass. It is
> no longer mitigated by UI wording — the honest verb is simply available to the same tier. **Owner
> decision — see `docs/API.md` → Receiving & Inspection.**

> **Not every control on the Receiving page is gated by the rows above.** The open-PO list on the
> Receive tab carries a **Delete PO** control wired to `DELETE /purchasing/purchase-orders/{po_id}` —
> a *Purchasing* permission (**Admin / Manager**, the *Delete / restore purchase order* row in the
> Purchasing matrix), not a Receiving one, and it is the tighter of the two lists. Gate any new
> control on the page against the endpoint it calls, not against the page it lives on. See
> `docs/API.md` → Receiving & Inspection → *Deleting an open PO from the Receiving page*.

> **Thermal receiving-label printing (ProxyBox / WHTP203e).** Manually (re)printing the
> 4×6 receiving label — `POST /receiving/receipt/{receipt_id}/print-label` — is enforced
> to **Admin / Manager / Supervisor** via `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> the same gate as `POST /receiving/receive`. Configuring the per-company print profile —
> `GET` / `PUT /receiving/print-profile` (ProxyBox base URL / target / API key, copies,
> paper size, and the `auto_print_on_receipt` + `allow_print_egress` toggles) — is
> **admin-only** via `get_admin_user`, so only an admin can enter the printer credential
> or flip the outbound-egress kill switch (default OFF, audited as a status change). The `PUT`
> is the switch's only writer, so it refuses **any API token 403** whatever its role and is not
> an MCP tool (lead decision 2026-11-15 — [Credential policy for judgment writes](#credential-policy-for-judgment-writes)). See
> [docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md).

### Shipping

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | ✓ | |
| Complete | ✓ | ✓ | ✓ | | | ✓ | |
| Rate-shop / validate address (`shipping:rate`) | ✓ | ✓ | ✓ | | | ✓ | |
| Buy label / BOL / schedule pickup (`shipping:label`) | ✓ | ✓ | ✓ | | | ✓ | |
| Void / refund label (`shipping:void`) | ✓ | ✓ | ✓ | | | ✓ | |
| Issue Certificate of Conformance | ✓ | ✓ | | | ✓ | | |

> **Carrier-integration write actions — endpoint mapping (multi-carrier shipping integration).**
> The carrier actions on `app/api/endpoints/shipping.py` —
> `POST /shipping/validate-address`, `POST /shipping/{id}/rate-shop`,
> `POST /shipping/{id}/buy-label`, `POST /shipping/{id}/buy-bol`,
> `POST /shipping/{id}/schedule-pickup`, `POST /shipping/{id}/void-label`, and
> `POST /shipping/{id}/refund` — are enforced **in code** to
> `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])` (`CARRIER_WRITE_ROLES`). They transmit
> customer data to a carrier (gated by the per-company `allow_carrier_egress` kill switch in the
> service) and move money (label/BOL/void/refund are audited), so they carry the same role set that
> may complete a shipment. The new permission strings `shipping:rate`, `shipping:label`, and
> `shipping:void` (in `app/models/role_permission.py`, granted to Admin / Manager / Supervisor /
> Shipping) drive the **frontend** `PermissionGate` / `usePermissions` visibility; the
> `require_role` lists above are the authoritative server-side control. The read endpoints
> (`GET /shipping/{id}/rates`, `GET /shipping/{id}/tracking`) stay open to any authenticated tenant
> user (read-broad / write-restricted). See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md) and `docs/API.md` →
> Shipping. The inbound carrier tracking webhook (`POST /webhooks/carriers/{provider}`) is
> **unauthenticated by design** — see the Admin → Integrations note below.

> **Complete (mark shipped) — endpoint mapping (2026-06-09).** The Shipping **Complete** action
> `POST /api/v1/shipping/{shipment_id}/ship` (`mark_shipped`) is now enforced **in code** to the
> Complete row via `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])`
> (`app/api/endpoints/shipping.py`). **This is a permission change:** the endpoint was previously open
> to **any authenticated user**, who could close a work order by shipping it; a non-privileged user now
> receives **403**. Marking shipped is the terminal shipping action that transitions the work order to
> `CLOSED`, so it carries the **Complete** permission (not the broader View/Create reads).
>
> **Certificate of Conformance — endpoint mapping (Batch 11C / G6-B).** Issuing a CoC
> `POST /api/v1/shipping/{shipment_id}/coc` (mint or return the existing frozen-snapshot CoC) is
> enforced **in code** to `require_role([ADMIN, MANAGER, QUALITY])`
> (`app/api/endpoints/shipping.py`) — a quality artifact, so the write is restricted (this is why the
> matrix row above does **not** include the **Shipping** role, which otherwise holds Shipping
> Create/Complete). Reading the CoC — `GET /shipping/{shipment_id}/coc` (metadata) and
> `GET /shipping/{shipment_id}/coc/pdf` (rendered PDF) — is open to **any authenticated user** in the
> active company (read-broad / write-restricted, like the other shipping reads). All three are
> tenant-scoped (cross-tenant `shipment_id` → **404**). A CoC is also **auto-issued on ship** when
> required; the auto-issue runs in the ship handler's context and is not separately role-gated beyond
> the existing ship permission.

### Quality

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Approve | ✓ | ✓ | | | ✓ | | |
| Author unfinished FAI / characteristics / process-step prefill | ✓ | ✓ | ✓ | | ✓ | | |
| Final FAI disposition (`passed` / `failed` / `conditional`) | ✓ | ✓ | | | ✓ | | |
| Record FAI customer approval (`POST /quality/fai/{id}/customer-approval`) | ✓ | ✓ | | | ✓ | | |
| Record CAR Quality Manager approval (`POST /quality/car/{id}/quality-approval`) | ✓ | ✓ | | | ✓ | | |
| Record NCR re-verification (`POST /quality/ncr/{id}/reverification`) | ✓ | ✓ | | | ✓ | | |
| Record CAR effectiveness verification (`POST /quality/car/{id}/effectiveness`) | ✓ | ✓ | | | ✓ | | |
| Calibration | ✓ | ✓ | | | ✓ | | |
| Manage scrap reason codes | ✓ | ✓ | | | ✓ | | |
| Void / restore NCR | ✓ | ✓ | | | ✓ | | |
| Void / restore CAR | ✓ | ✓ | | | ✓ | | |

> **Inspect — endpoint mapping.** The shop-floor inspection sign-off
> `POST /api/v1/shop-floor/operations/{operation_id}/inspection` (which records
> `WorkOrderOperation.inspection_complete = True` and clears the completion inspection quality gate)
> is enforced **in code** to this Inspect row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`,
> `mark_operation_inspected`). The role set matches the matrix exactly — this repo has no separate
> `INSPECTOR` role, so operation inspection is performed by Admin / Manager / Supervisor / Quality.
> It is the flag's **only** writer: `PUT /work-orders/operations/{id}` refuses `inspection_complete`
> **422**. On top of the role gate, only a person may sign: any standing API token is **403**
> (`API_TOKEN_INSPECTION_SIGN_OFF`, see
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes)), a crew-station
> badge token is **403** by the kiosk path fence whatever its role, and the route is not an MCP
> tool. The `MARK_OPERATION_INSPECTED` audit row is required evidence (**503**, nothing signed). The
> app offers **Sign off** on Work Order Detail operation rows and Shop Floor operation cards to the
> same four roles (plus Platform Admin and superusers, whom `require_role` admits); every other role
> sees the "Inspection required" / "Inspected" chip only.
>
> **FAI evidence has separate authoring and final-disposition gates.** Every FAI/characteristic
> write, including prefill, requires Admin / Manager / Supervisor / Quality. Setting a final status
> requires Admin / Manager / Quality and records the actual approver and completion date. A final
> status or existing completion date blocks every subsequent header/characteristic/prefill write
> with **409**, including platform callers; corrections require a new inspection. Parent/child
> lookups are tenant-scoped (**404** for foreign/missing ids), and manual characteristics derive
> their company from the parent context. Required audit rows, counts and evidence commit together
> (**503** with rollback on audit loss). Unfinished-characteristic deletion retains its full prior
> contents in the audit trail. Reads remain broad within the tenant; prefill never overwrites an
> existing measurement or sets conformance. See [API: FAI](API.md#first-article-inspections-fai).
>
> **FAI customer approval — endpoint mapping.** `POST /quality/fai/{fai_id}/customer-approval` is
> enforced **in code** to the **Record FAI customer approval** row via
> `require_role(fai_service.APPROVE_ROLES)` = **Admin / Manager / Quality**, the same set as final
> disposition. It is the one write allowed on an FAI after its final disposition, and it writes only
> `customer_approved` and `customer_approval_date`: on a PASSED or CONDITIONAL inspection, once
> (**409** otherwise), with a required `APPROVE` audit row (**503** with rollback on audit loss). A
> standing API token is refused **403** whatever its user's role (see
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes)), and so is a token
> setting the final disposition itself through `PUT /quality/fai/{id}`. The UI shows the
> control to the same roles (`canApproveFAIs` in `frontend/src/utils/recordWriteAccess.ts`).
>
> **FAI log of record — read-broad, export gated.** `GET /quality/fai/log` depends on
> `get_current_user` only, like every FAI read: any authenticated user of the company can read the
> log. Downloading it as a file, `GET /exports/fai/export`, is a bulk export: **Admin / Manager**
> and audited (see [Bulk Data Export](#bulk-data-export)). Quality is deliberately not in that set,
> and the Log view's **Export log** button is hidden from Quality, Supervisor and the other roles
> (`canExportRecords`).
>
> **Scrap reason codes (Lean Phase 1) — read-broad / write-restricted.** Managing the tenant's
> structured scrap vocabulary (`POST /api/v1/quality/scrap-reason-codes`,
> `PUT /quality/scrap-reason-codes/{id}`) is a quality-system configuration task, enforced **in
> code** via `require_role([ADMIN, MANAGER, QUALITY])` (`SCRAP_REASON_WRITE_ROLES` in
> `app/api/endpoints/scrap_reasons.py`) — the same write set that owns the NCR/CAR vocabulary. The
> **read** (`GET /quality/scrap-reason-codes`) depends on `get_current_user` only — any
> authenticated user in the tenant, including Operators via the kiosk/desktop scrap pickers — so
> the matrix row above reflects the server-enforced **write** control. There is no delete endpoint:
> retirement is `is_active: false` (historical scrap rows reference these ids — traceability).
>
> **Void / restore NCR — endpoint mapping.** `DELETE /quality/ncr/{ncr_id}` (void) and
> `POST /quality/ncr/{ncr_id}/restore` (`app/api/endpoints/quality.py`) are enforced **in code** to the
> **Void / restore NCR** row above — `require_role(quality_record_service.WRITE_ROLES)`, i.e.
> `[ADMIN, MANAGER, QUALITY]`. `NonConformanceReport`
> gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`); a void is a soft delete
> (invariant #3) that also moves the NCR to the existing `VOID` status, requires a **non-blank
> `reason`**, and is **refused (400)** while the NCR still actively gates a work order (an
> `OPEN`/`ACKNOWLEDGED` `WorkOrderBlocker` references it). Restore reopens it to `OPEN` (clearing any
> closure fields an NCR voided while CLOSED still carries). Both are
> required-audited in the same transaction (**503** with rollback on audit loss): the void writes a
> status-change **and** a delete row, restore a `RESTORE` row. See `docs/API.md` → Quality.
>
> **Void / restore CAR — endpoint mapping.** `POST /quality/car/{car_id}/void` and
> `POST /quality/car/{car_id}/restore` are enforced **in code** to the **Void / restore CAR** row above
> via the same `quality_record_service.WRITE_ROLES`; Operator, Supervisor and Viewer get **403**. Void
> requires a **non-blank `reason`** (**422** otherwise), refuses a CLOSED or already-void CAR
> (**409**), and records the reason on a required `STATUS_CHANGE` audit row. The CLOSED refusal stops
> a one-step void only: the same roles can reopen a CLOSED CAR with a status-only `PUT` (no reason
> captured yet) and then void it, with both steps on the audit trail. `VOID` is the CAR's tombstone:
> the CAR stays readable. Restore reopens a void CAR to `open` (**409** when it is not void) on a
> required `RESTORE` audit row.
>
> **NCR / CAR edits — endpoint mapping (the Approve row).** `PUT /quality/ncr/{ncr_id}` (edit,
> disposition, action taken, customer concession, close, reopen, assign, CAR link), `PUT /quality/car/{car_id}` (edit, verification,
> close, reopen, assign) and `POST /quality/ncr/{ncr_id}/create-car` are enforced **in code** via
> `require_role(quality_record_service.WRITE_ROLES)` = **Admin / Manager / Quality**, the **Approve**
> row (the frontend detail screen gates on `quality:approve`, which the same three roles hold).
> Filing an NCR (`POST /quality/ncr`), opening a CAR (`POST /quality/car`, CAR form fields included)
> and all NCR / CAR reads, the printable CAR (`GET /quality/car/{car_id}/pdf`) among them, depend on
> `get_current_user` only, so any authenticated user of the company can file one (Operators
> included). Every one of these writes commits with a required audit row (**503** with rollback on
> audit loss) and validates `part_id` / `work_order_id` / `assigned_to` / `car_id`, and a CAR's
> `customer_id` / `vendor_id`, against the active company before writing (**404**; a vendor may be
> inactive or removed, since a supplier CAR can record that removal). A CLOSED NCR or CAR refuses
> every change except a status-only reopen (**409**), for every role. See `docs/API.md` → Quality →
> "NCR / CAR writes".
>
> **CAR Quality Manager approval — endpoint mapping.** `POST /quality/car/{car_id}/quality-approval`
> is enforced **in code** to the **Record CAR Quality Manager approval** row via the same
> `quality_record_service.WRITE_ROLES` = **Admin / Manager / Quality**; Supervisor, Operator,
> Shipping and Viewer get **403**. The server stamps the approver and time (neither is accepted in a
> request body), once per CAR (**409** when already approved or VOID), with a required `APPROVE` audit
> row. It does not gate closure. A standing API token is refused **403** whatever its user's role
> (see [Credential policy for judgment writes](#credential-policy-for-judgment-writes)), and so is a
> token filling or changing the form's vendor or customer approval boxes on `POST` / `PUT`. The CAR
> detail screen shows **Record quality approval** only to `quality:approve` holders, the same three
> roles by default.
>
> **NCR re-verification and CAR effectiveness verification — endpoint mapping.**
> `POST /quality/ncr/{ncr_id}/reverification` and `POST /quality/car/{car_id}/effectiveness`
> (migration `127_ncr_car_records`) are enforced **in code** to the **Record NCR re-verification**
> and **Record CAR effectiveness verification** rows via the same
> `quality_record_service.WRITE_ROLES` = **Admin / Manager / Quality**; Supervisor, Operator,
> Shipping and Viewer get **403**. Each server-stamps who recorded it and when (neither is accepted
> in a request body; no `PUT` carries them), is re-recordable, and commits with a required audit
> row (**503** with rollback on audit loss). The re-verification is **409** on a CLOSED NCR and
> unless it is dispositioned `rework` or `repair`; the effectiveness verification is **409** unless the CAR
> is in `verification`, and a `not_effective` result returns the CAR to `corrective_action`. A
> standing API token is refused **403** on both whatever its user's role, and so is a token
> changing an NCR's `disposition` or customer concession through `PUT /quality/ncr/{ncr_id}` (see
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes)). These records
> also gate closure (`PUT`, the Approve row): a `rework` / `repair` NCR closes only with a stored
> `conforming` re-verification, and a corrective CAR only with a stored `effective` result. The
> detail screen shows **Record re-verification** / **Record effectiveness** only to
> `quality:approve` holders. See `docs/API.md` → Quality → "NCR disposition evidence" and "CAR
> corrective-action evidence".

> **Calibration — endpoint mapping (now enforced in code).** `POST /api/v1/calibration/equipment`,
> `PUT /calibration/equipment/{id}` and `POST /calibration/equipment/{id}/calibrate` are enforced to
> the **Calibration** row above via `require_role([ADMIN, MANAGER, QUALITY])`
> (`CALIBRATION_WRITE_ROLES` in `app/api/endpoints/calibration.py`) — the same set that holds the
> frontend's `quality:calibration` permission, which route-gates `/calibration`. **This is a
> permission change:** until now the row described the UI only. Every calibration write was a bare
> `get_current_user`, so any authenticated user in the tenant — a Viewer included — could set a gauge
> `active` and put it past the process-sheet gauge check. Reads (`GET /calibration/equipment`,
> `/due-soon`, `/{id}`, `/{id}/history`) stay open to every role in the tenant, tenant-scoped; the
> dashboard's calibration-due alert reads `/due-soon`. Superuser / Platform Admin bypass, as
> elsewhere. The role is necessary, not sufficient: `PUT` may only take a gauge **out** of service
> (**409** otherwise), and only a passing calibration record puts one back — see `docs/API.md` →
> Calibration.

### Operator Certifications & Training

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | | | ✓ | | |
| Edit | ✓ | ✓ | | | ✓ | | |
| Delete | ✓ | ✓ | | | ✓ | | |

### Skill Matrix

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |

> **Write enforcement — these role sets are new (defaults chosen for this fix, 2026-06-09).** The
> seven write endpoints on the operator-certifications router
> (`app/api/endpoints/operator_certifications.py`, mounted at `/api/v1/operator-certifications`) are now
> enforced **in code**; the RBAC matrix previously had **no rows** for these record types and the writes
> were open to any authenticated user.
> - **Certifications + training writes** —
>   `POST/PUT/DELETE …/certifications/{…}` (`create_certification` / `update_certification` /
>   `delete_certification`) and `POST/PUT …/training/{…}` (`create_training` / `update_training`) —
>   require `require_role([ADMIN, MANAGER, QUALITY])` (`CERT_TRAINING_WRITE_ROLES`). These are
>   operator-qualification / conformance records that Quality owns alongside Admin/Manager.
> - **Skill-matrix writes** — `POST …/skill-matrix/` (`create_skill_entry`, which upserts) and
>   `PUT …/skill-matrix/{entry_id}` (`update_skill_entry`) — require
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`SKILL_MATRIX_WRITE_ROLES`), because skill-matrix
>   entries are competency assessments performed by Supervisors (and above).
>
> Any other authenticated role now receives **403**. **Read** endpoints (the certifications dashboard /
> list / by-user / by-id, training list / by-user, and the skill-matrix check / by-user /
> by-work-center / list) stay open to **any authenticated user**, tenant-scoped — the read-broad /
> write-restricted model. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Writes are audited + FK-validated.** Each write now records a tamper-evident `audit_log` row
> (resource types `operator_certification` / `training_record` / `skill_matrix`; create / update /
> delete). The create endpoints (and `update_training`'s re-pointed `work_center_id`) reject a
> `user_id` / `work_center_id` that does not belong to the active company with **422** before insert
> (cross-tenant FK-injection guard). See `docs/API.md` and `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`.

### Customer Complaints & RMA

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard, lists, detail, 8D report) | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Record / edit a complaint, record an investigation | ✓ | ✓ | ✓ | | ✓ | | |
| Open / edit an RMA, receive and inspect returned material | ✓ | ✓ | ✓ | | ✓ | | |
| Resolve / close a complaint | ✓ | ✓ | | | ✓ | | |
| Raise an NCR / CAR from a complaint | ✓ | ✓ | | | ✓ | | |
| Approve / deny / dispose / complete an RMA | ✓ | ✓ | | | ✓ | | |

> **Endpoint mapping (enforced in code, 2026-11-05).** The router
> (`app/api/endpoints/customer_complaints.py`, mounted at `/api/v1/complaints`) uses two role sets
> defined in `app/services/customer_complaint_service.py`. They mirror the **Inspect** and
> **Approve** rows of the [Quality](#quality) matrix:
> - **`AUTHOR_ROLES` = Admin / Manager / Supervisor / Quality** (the `quality:inspect` tier):
>   `POST /complaints/complaints/`, `PUT /complaints/complaints/{id}`, `POST …/{id}/investigate`,
>   `POST /complaints/rma/`, `PUT /complaints/rma/{id}`, `POST /complaints/rma/{id}/receive` and
>   `POST /complaints/rma/{id}/inspect`.
> - **`APPROVER_ROLES` = Admin / Manager / Quality** (the `quality:approve` tier):
>   `POST …/{id}/resolve`, `…/close`, `…/create-ncr`, `…/create-car`, and
>   `POST /complaints/rma/{id}/approve`, `…/deny`, `…/dispose` and `…/complete`.
>
> Any other authenticated role receives **403** with nothing written. Superuser and Platform Admin
> pass `require_role` as everywhere else, but stay bound to the active company.
>
> **What changed.** Before this fix only `create-ncr`, `create-car` and the RMA `approve` / `deny`
> / `dispose` / `complete` verbs were gated, all to Admin / Manager / Quality, and those keep the
> same set. Every other write, `resolve` and `close` included, was open to **any** authenticated
> user. As a result:
> - Operator, Viewer and Shipping lose every write they had: complaint create, edit, investigate,
>   resolve and close, and RMA create, edit, receive and inspect.
> - Supervisor loses `resolve` and `close` but keeps every author-tier verb.
>
> The page hides **New Complaint** (header and empty state) and **Create RMA** for roles without
> `quality:inspect`, so Operator and Viewer no longer see controls the server would refuse.
>
> **Reads stay read-broad, scoped to the tenant.** The dashboard, both lists, complaint and RMA
> detail and the 8D report depend on `get_current_user` only. The **View** row above is the UI's
> `quality:view` gate on `/customer-complaints`, the same as the Quality View row. On the server,
> every route (read or write) resolves through the active company: another tenant's id returns
> **404** before anything is read or changed, the dashboard aggregates only the active company,
> and part / customer / RMA expansions never cross tenants.
>
> **An Author-tier edit cannot make an Approver's decision.** `PUT` is Author-tier, so the update
> schemas carry no field an Approver verb decides: no `status`, no resolution fields
> (`resolution_description`, `corrective_action`, `preventive_action`, `actual_cost`) on a
> complaint, and no `disposition` or `credit_amount` on an RMA (all **422**). The fields an
> approval rests on are locked once it is made: RMA `quantity` / `reason` change only while
> `requested`, and a `closed` / `void` complaint or a `completed` / `denied` RMA refuses every edit
> (**409**). Investigation and inspection findings are likewise written only by their own verbs.
>
> **Writes are audited.** Resource types are `customer_complaint`, `rma`, `ncr` and `car`. `resolve`,
> `close`, RMA `approve` / `deny` / `dispose` / `complete`, and the NCR or CAR a complaint raises
> (with the complaint's link) write their audit rows with `log_required`: if a row cannot be saved
> the request returns **503** and nothing commits. The other writes use the best-effort helpers.
> Status moves only through the workflow verbs, which return **400** from the wrong state.
> See [API: Customer Complaints & RMA](API.md#customer-complaints--rma).

### Engineering Change Orders (ECO)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit | ✓ | ✓ | | | | | |
| Submit / Approve / Reject | ✓ | ✓ | | | | | |
| Implement / Complete | ✓ | ✓ | | | | | |
| Add / Edit task, Add approval | ✓ | ✓ | | | | | |

> **ECO mutations are Admin / Manager (enforced in code).** Every state-changing ECO endpoint
> (`POST /eco/eco/`, `PUT /eco/eco/{id}`, and the `submit` / `approve` / `reject` / `implement` /
> `complete` transitions, plus `tasks` create/update and `approvals` create) is gated with
> `require_role([ADMIN, MANAGER])` (`app/api/endpoints/engineering_changes.py`). Any other authenticated
> role receives **403**. The read endpoints (list, get, dashboard, list approvals, affected items) remain
> open to all authenticated users. Previously these mutations were available to **any** authenticated
> user — this row records the tightened authorization landed in WO-completion remediation Batch 11A
> (G4-Fix1), alongside the ECO router's tenant scoping and audit logging.

### Job costing

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Read (API) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit estimates and status | ✓ | ✓ | | | | | |
| Add / delete manual entries | ✓ | ✓ | | | | | |
| Recalculate actual costs | ✓ | ✓ | | | | | |

Every `/job-costs` mutation enforces Admin / Manager (**403** otherwise). Both reads and writes
resolve a tenant-owned job cost through an owned work order; writes require an undeleted parent,
while reads preserve costs after work-order soft deletion. Entries and operation references
must belong to that parent/company (**404** for explicitly requested foreign/missing ids).
Legacy foreign entries are omitted on reads and invalid operation/user references render `null`.
Recalculation regenerates TimeEntry labor entries, preserves other entries and recomputes totals. Required audits include
entry snapshots and total changes; audit loss returns **503** and rolls back the whole mutation.
Deletion and recalculation retain removed entry evidence in the audit log. Same-company historical
part labels remain readable. No new financial approval workflow is introduced, and automatic
work-order completion retains its existing business path. See [API: Job costing](API.md#job-costing).

**Job-cost figures on the work-order read (2026-11-13).** `GET /work-orders/{id}` and
`GET /work-orders/by-number/{wo_number}` now carry `job_cost_actuals` (the JobCost's material /
labor / burden / subcontract / total) and `issued_material_cost`. No gate changed: job-cost reads were
already open to every role, and the work-order reads keep their own gate (any authenticated user in
the active company). The JobCost is read with its own `company_id` scope, never through the work
order's relationship, so a foreign-tenant row is never shown.

### Documents

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Read / download (API) — `standard` documents; see the access-level table below | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Manual upload / revision and immediate release | ✓ | ✓ | | | ✓ | | |
| Attach an unlinked PDF to a work order | ✓ | ✓ | | | ✓ | | |
| Delete an unretained document | ✓ | ✓ | | | | | |
| Change a document's access level (`POST /documents/{id}/access-level`; reason required; no API token) | ✓ | | | | ✓ | | |
| Access review (`GET /documents/access-review`) | ✓ | | | | ✓ | | |

**Who may open a document, by access level** (migration `131`, AS9100D 7.5.3.1;
`app/services/document_access.py`):

| `access_level` | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|---|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| `standard` (default) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `restricted` | ✓ | ✓ | ✓ | | ✓ | | |
| `export_controlled` | export-authorized only | export-authorized only | export-authorized only | export-authorized only | export-authorized only | export-authorized only | export-authorized only |

- A superuser and a Platform Admin open `restricted` documents (the `require_role` pass-through),
  but **not** `export_controlled` ones unless export-authorized. No role bypasses export
  authorization.
- Export authorization counts only in the user's **own** company. A platform account authorized in
  its home company that switches into another tenant opens none of that tenant's
  `export_controlled` documents, and `authorized_user_count` counts only the company's own users.
- The levels are independent: an export-authorized Operator opens `standard` and
  `export_controlled` documents but not `restricted` ones.
- An API token and a crew-station badge token act as their bound user (role and flag). A
  crew-station tablet's own station token and an **AI-external** API token see `standard` only,
  everywhere.
- Reads whose results go to an AI model (copilot document search; Hank's job documents for
  readiness, knowledge and the shipping packet; Hank chat's attach preview, saved-work reads,
  handoff reads and intake-file tools) treat `export_controlled` documents as missing for
  everyone, export-authorized users included, as well as any document the caller may not open. A
  person's own `POST /hank/tasks` keeps the ordinary rule.
- A document the caller may not open is absent from every list and **404** on every by-id path,
  before any other check: the document list, detail, revisions, download, attach, delete, the kiosk drawing viewer, laser-nest PDFs,
  QMS links, ECO affected documents, process-sheet step attachments, and Hank / copilot document
  reads (an ECO's `affected_documents` and a Hank handoff answer the same **422** they give a
  missing id). The audit log (`GET /audit/`, Admin / Manager) returns such a document's rows, and
  the `hank_intake_file` / `hank_task` rows that name it, redacted: values, description and
  identifier hidden, a deleted document judged by its last recorded level, the stored row
  untouched. A `search` matches a redacted row only on what the reader is shown, and is applied
  before pagination. Every path is listed in
  [API: Restricted and export-controlled documents](API.md#restricted-and-export-controlled-documents-as9100d-7531).
- Export authorization is granted and revoked only by an Admin, with a reason, through
  `PUT /users/{id}/export-authorization` (see [Users](#users)); deactivating a user revokes it.
  A level applies to the document's whole revision history. Changing it needs the caller to open
  the addressed document, and lowering it needs the caller to open every revision (**409**
  otherwise), so lowering an `export_controlled` document takes an export-authorized Admin or
  Quality user. A writer that changes a chain's level locks the chain and re-walks it until
  stable (**409**, retry, after 5 walks). Both writes refuse every API token and are not MCP tools
  ([Credential policy](#credential-policy-for-judgment-writes)).
- Manual upload takes an `access_level` and the server only raises it: to the predecessor
  revision's level, and to `export_controlled` when the linked part's or work order's customer is
  export-controlled; a raised revision lifts its earlier revisions too. Hank intake filing applies
  the same raise rule, and attaching a document to an export-controlled customer's work order
  (`POST /documents/{id}/attach-work-order`, or the Hank attach task on execute) raises it and its
  chain to `export_controlled`, one audited `ACCESS_LEVEL_CHANGE` row per revision. Certificates,
  labels, bills of lading, laser-nest drawings and step evidence are stored `standard`.

Manual uploads publish as released and record the actual release actor and UTC instant, so
upload/revision and attachment require Admin / Manager / Quality. Delete retains Admin / Manager.
Generated artifacts use their existing receipt/shipping business-route gates. Foreign document
or linked-record ids return **404**; role refusals return **403**. Revision histories cannot be
rebound or deleted, and receipt certificates remain protected even after a receipt is voided
(**409**). A document that a same-company QMS standard or QMS clause evidence link names is
refused too (**409**), including through a removed standard, clause or evidence link; clearing
the link's `document_id` (Admin / Manager / Quality, audited) releases it. So is a document that
a same-company shipment carrier label or bill of lading, receipt receiving label, process-sheet
step record attachment or governed file record names (**409**), voided, removed and superseded
records included: none has a verb that releases it. A laser nest's PDF is refused too. A removed
nest, or a live nest whose operation has recorded work, keeps it. Otherwise Detach
(`DELETE /laser-nests/{id}/document`, Admin / Manager / Supervisor, audited) releases it once it is
cleared from every nest. A document an engineering change order lists in `affected_documents` is
refused too: while the ECO is draft or submitted, taking it out of the list (`PUT /eco/eco/{id}`,
Admin / Manager, audited) releases it; from under review on, and for a rejected or cancelled ECO,
it is kept. A refusal reports every permanent reason before any unlink, Detach or ECO-edit
instruction. New revisions retain prior files, type and links. Required audit loss returns **503**
and rolls back metadata changes; bytes are removed only by the corresponding failure cleanup
or after an audited deletion commits. Same-company historical part labels remain available.
The Documents, Quality and Job Costing UI write controls reflect the fixed role gates and FAI
final-state rules; the server also enforces read-only context, parent validity and retention.
UI controls do not grant API authority. See [API: Documents](API.md#documents).

### Users

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | | | | | |
| Create | ✓ | | | | | | |
| Edit | ✓ | | | | | | |
| Delete | ✓ | | | | | | |
| Roles | ✓ | | | | | | |
| Department pick list + users whose stored Department is off it (`GET /users/departments`; read-only) | ✓ | | | | | | |
| Floor PIN — set / reset / clear / unlock (`PUT` / `DELETE /users/{user_id}/floor-pin`, `POST …/floor-pin/unlock`; signed in — any API token 403). Manager: Operator / Supervisor / Shipping / Viewer accounts only | ✓ | ✓ | | | | | |
| Grant / revoke export authorization (`PUT /users/{id}/export-authorization`; reason required; no API token) | ✓ | | | | | | |
| Own profile + notification settings (`/users/me/*`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Mention search (`GET /users/mention-search`, `{id, display_name}` only — see [Comments and watchers](#comments-and-watchers)) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Self-scoped `/users/me/*` routes are open to every authenticated role — by construction, not by
> grant.** `GET /users/me`, `PUT /users/me/phone`, `GET`/`PUT /users/me/notification-preferences`,
> and `POST /users/me/test-sms` (`app/api/endpoints/users.py`) carry **no `require_role`**: they
> read and write only `current_user` and **never accept a user id**, so there is no id to authorize
> and no path to another user's record. The backing UI is **My Settings** (`/settings`), routed for
> all authenticated roles. Notes:
> - **Phone changes are audited** on both the self-service path (`extra_data.source =
>   "self_service"`) and the Admin `POST /users/` / `PUT /users/{id}` paths — the phone is the
>   destination of every SMS alert. Numbers are normalized to E.164; an invalid number is **400**.
> - **`phone` is field-minimized**: it serializes only in the self-profile / Admin-Manager
>   user-management `UserResponse`, never in general user serialization. See
>   [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#phone-is-field-minimized).
> - **`POST /users/me/test-sms` targets the caller's own number only** (never a caller-supplied
>   destination), is gated by the company `allow_sms_egress` kill switch, and is rate-limited
>   **3/minute** per IP.
> - Saving preferences cannot bypass a **mandatory** channel: the dispatcher re-applies the
>   catalog's `mandatory_channel` at send time regardless of the stored row, so a mandatory-critical
>   event can never be fully muted.

> **Tenant account administration is Admin-only (`require_role([ADMIN])`)** — both creation
> paths (`POST /auth/register`, `POST /users/`), CSV/XLSX import, approval, password reset,
> activation, `PUT /users/{id}` (edit, including role assignment), `DELETE /users/{id}` (deactivate), and
> `POST /users/{id}/unlock` (clear the 5-failed-logins/30-minute lockout: resets
> `failed_login_attempts`, clears `locked_until`) all gate to
> **Admin** (`app/api/endpoints/users.py`). The **View** rows are the governance-read exception noted
> above: `GET /users/` (list) and `GET /users/{id}` are `require_role([ADMIN, MANAGER])`, so a
> **Supervisor** gets a **failed load** (403), not a read — user records are *not* on the read-broad
> domain default. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **The one write on another user's account a Manager holds: the floor PIN (AS9100D 8.5.2,
> migration `130`), for floor accounts only.** `PUT /users/{user_id}/floor-pin` (set or reset),
> `DELETE /users/{user_id}/floor-pin` (clear) and `POST /users/{user_id}/floor-pin/unlock` (lift a
> lock, keeping the PIN) are `require_role([ADMIN, MANAGER])`, so a Manager can issue and recover
> the floor's PINs without holding account administration. Three rules sit on top of the role, in
> this order:
> - **Any API token is 403** before anything is read (credential administration is a person's
>   act; see [Credential policy for judgment writes](#credential-policy-for-judgment-writes)), and
>   none of the three routes is an MCP tool.
> - The target must be in the active company (**404**), and a stored Platform Admin / superuser
>   target needs a stored platform principal (**403**), as for password reset and unlock — a tenant
>   Admin cannot manage a platform account's PIN either.
> - **A Manager may manage PINs only for Operator, Supervisor, Shipping and Viewer accounts**
>   (**403** `"A Manager can manage floor PINs only for Operator, Supervisor, Shipping and Viewer
>   accounts. Ask an Admin to manage this account's PIN."`). An Admin's, a Manager's (the
>   Manager's own included) and a Quality user's PIN need an Admin (or a Superuser / Platform
>   Admin).
>
> **Why the scope.** Whoever chooses a person's PIN can sign in as that person with their badge, and
> every record that session writes carries `extra_data.auth_methods` `["badge", "pin"]`, which reads
> as that person's own act. Admin, Manager and Quality are the roles that carry judgment —
> approvals, dispositions, quality sign-off — so a Manager able to set one of those PINs could
> record that judgment under a peer's or a superior's identity. Three rules together mean nobody
> below Admin can take over a judgment-role identity: the scope (a Manager cannot write such a PIN),
> the promotion rule below (a PIN a Manager wrote for a floor account does not survive a promotion),
> and the **session binding** — a `["badge", "pin"]` session carries `pst`, the set time of the PIN
> it verified, and is refused **401** `floor_pin_reauth_required` on every request, refresh and
> company switch once that PIN is reset, cleared or cleared by a promotion, so a session a Manager
> opened with a floor account's PIN cannot outlive that account's promotion. An Admin already
> administers every account, so letting an Admin set any tenant PIN grants nothing new.
>
> **A promotion into a judgment role clears the PIN.** The scope is checked when a PIN is written,
> so on its own it would let a PIN a Manager chose for an Operator survive that person's promotion.
> Instead, a role change **into** Admin, Manager or Quality **from any other role**, through
> `PUT /users/{id}` or `POST /users/{id}/approve`, clears the floor PIN (hash, set date, wrong-PIN
> count and any lock) in the same transaction (`user_provisioning.clear_floor_pin_on_promotion`),
> and the role change's required audit row carries `extra_data.floor_pin_cleared_on_promotion: true`.
> If the person **had** a PIN, the promotion also sets `floor_pin_reset_required` (marked
> `floor_pin_reset_required: true` on the same row): both badge sign-on routes then answer **403**
> `floor_pin_reset_required` ("Your role changed. Ask an Admin to set a new floor PIN."),
> **whatever the company policy**, until an Admin sets a new PIN — or clears it, an explicit
> decision to allow badge-only sign-on. The promotion never quietly downgrades the badge to
> badge-only, and every existing badge + PIN session of that person ends at its next request. The
> Users page shows *New PIN needed (role changed)*. A move between judgment roles (Manager to
> Admin), a demotion and a move between floor roles keep the PIN.
>
> All three writes record required audit evidence in the same transaction (`FLOOR_PIN_SET` /
> `FLOOR_PIN_CLEARED` / `FLOOR_PIN_UNLOCKED`; **503** and rollback on audit loss), never the PIN or
> its hash. The Users page's **Floor PIN** column and its dialog are shown to Admin and Manager
> (Platform Admin / superuser too); on a row the caller may not manage, the Manage action reads
> *Admin only* (or *Platform admin only*), and the server enforces the same rule. Unlocking a
> password lockout (`POST /users/{id}/unlock`, Admin) does not touch a PIN lock; the floor-PIN
> unlock, a new PIN or a clear lifts it early and resets the wrong-PIN count. Every PIN lock also
> expires on its own (15 minutes up to a 7-day cap); a lock of 24 hours or more alerts the company's
> **Admins** in the app (`floor_pin.lockout`, mandatory in-app), and the Users page lists every
> currently locked floor PIN above the table, with its end time, for Admin and Manager. A Manager
> can unlock only the accounts in the scope above. Setting or clearing a PIN also
> ends that person's badge + PIN sessions at their next request; the unlock keeps the PIN and the
> sessions. The company policy that makes a PIN mandatory is Admin-only
> ([Admin](#admin)). See `docs/API.md` → Users (Admin) → "Floor PIN".
>
> **Tenant provisioning uses one role policy (`services/user_provisioning.py`).**
> - Both creation routes, update and approval reject `role = platform_admin` with **400**;
>   CSV/XLSX import returns a per-row error. This applies even to platform callers. Tenant Admin
>   remains assignable. Creation forces `is_superuser=false` and derives company context
>   server-side; body/import fields cannot grant platform authority or redirect the account.
> - Existing stored Platform Admin/superuser targets are protected on update, approval, password
>   reset, activation, deactivation and unlock: only a stored platform principal may manage them
>   (**403** for a tenant Admin). Foreign-company targets remain **404**.
> - **Self role-escalation guard:** on `PUT /users/{id}`, an Admin editing **their own** record cannot
>   change **their own** role (**400**, `"You cannot change your own role"`); editing their own
>   name/email/other fields stays allowed. This mirrors the delete endpoint's "cannot deactivate
>   yourself" self-guard, so an Admin cannot self-elevate and a role change to one's own account must
>   be made by a different Admin.
>
> Every user mutation — create, update (including any role change), approve, password-reset,
> deactivate, activate, and unlock — is recorded in the tamper-evident audit log (unlock as a
> `STATUS_CHANGE` `locked` → `unlocked` when the lock was still in force, as an `UPDATE` of the two
> fields when it only cleared residual state — an expired lock or attempts short of 5 — and the
> endpoint is idempotent: a no-op unlock writes no row); the self-service
> `POST /users/change-password` likewise records a `PASSWORD_CHANGE` audit event (mirroring
> `reset-password`; the password/hash is never included) as **required** evidence in the same
> transaction as the change (**503**, nothing saved, on audit loss), and is refused **403** to every
> API token and badge / station token (see [Credential policy for judgment writes](#credential-policy-for-judgment-writes)).
>
> Creation, update, approval, password reset, activation, deactivation and unlock commit their
> required evidence atomically (**503** and rollback on audit loss). Deactivation includes every
> companion API-token revocation audit and, for an export-authorized user, the companion
> `EXPORT_AUTHORIZATION_REVOKE` row. Import keeps its per-row transaction/error behavior;
> previously accepted rows remain committed, while an audit failure creates no account for that
> row. `/auth/register` attributes the event to the initiating Admin and targets the new user.
> Unchanged account updates and unlocks with no lock state do not add audit rows. Other self-service/auth logging retains its existing
> behavior; the required-audit guarantee is specific to these paths.
>
> **Export authorization (migration `131`)** is the only thing that opens an `export_controlled`
> document (see [Documents](#documents)). `PUT /users/{id}/export-authorization` is Admin-only, takes
> a required reason, refuses every API token **403** and is not an MCP tool. It follows the same
> target rules as `PUT /users/{id}` (foreign-company **404**; a stored Platform Admin / superuser
> target needs a platform principal, **403**), answers **409** on a no-op, stamps who granted it and
> when (cleared on revoke), and writes a required `EXPORT_AUTHORIZATION_GRANT` /
> `EXPORT_AUTHORIZATION_REVOKE` audit row (**503** and rollback on loss). There is no self-guard:
> an Admin may grant their own authorization, and the grant is audited like any other. The flag is
> not on the create, update or import bodies. The authorization counts only in the user's own
> company, never in a tenant a platform account switches into. **Deactivation revokes it**: both
> deactivation verbs clear it with a companion required `EXPORT_AUTHORIZATION_REVOKE` row (reason
> `user deactivated`), like the API-token sweep, and reactivation restores nothing.
>
> Public `/auth/register-public` chooses roles server-side: the empty-install bootstrap creates
> an active Platform Admin/superuser; later signups create inactive Viewers without superuser
> authority. Accepted creation requires an atomic registration audit (**503** on loss); ordinary
> duplicate/refusal responses remain uniform. **No route creates a company**: both
> company-creation routes — public `POST /companies/register` and platform-admin
> `POST /platform/companies` — were removed on 2026-11-23, so a Platform Admin can no longer
> onboard a tenant. The only `Company` constructor is that empty-install bootstrap in
> `POST /auth/register-public`. Platform company updates, including activation, still require a
> target-company audit in the same transaction (**503** and rollback on loss). See [API: onboarding](API.md#company-onboarding-and-platform-administration).
>
> **Password-strength policy.** A password set on any of these paths — `POST /users/` (create),
> `POST /users/{id}/reset-password`, and self-service `POST /users/change-password` — must satisfy
> the server-side strength policy (≥ 12 chars; and no common weak substring from the ~37-entry
> blocklist in `app/schemas/user.py`), the **same policy** as `POST /auth/register`. There are **no
> character-class requirements**: the uppercase / lowercase / number / special-char rules were removed
> on 2026-07-29 in favor of length + blocklist per NIST SP 800-63B §5.1.1.2, and the blocklist was
> expanded in the same change. There is no first-admin `admin_password` path any more: both
> company-creation routes (`POST /companies/register` and `POST /platform/companies`) were removed
> on 2026-11-23.
> The user CSV import applies it per row to user-supplied passwords; operator auto-generated (badge)
> passwords are exempt. See `docs/API.md` → Users.
>
> **Badge printing (A0.4).** The badge print sheet `/print/badges` (opened from the Users page via
> multi-select → "Print Badges") is **frontend-gated by `canManageUsers`** (=
> `users:create` OR `users:edit`) — both the Users-page button
> (`frontend/src/utils/permissions.ts`) and the `/print/badges` route (route map in
> `frontend/src/App.tsx`) require it. After user management was aligned to Admin-only, only **Admin**
> holds those permission strings (plus Platform Admin / superuser), so badge printing is now
> **effectively Admin-only**: a **Manager** (who holds `users:view` — the read-only list) and a
> **Supervisor** (who holds no `users:*` permission) never see the Print Badges control or reach the
> route. No new endpoint or permission string was added: badges are client-rendered QR codes of
> `users.employee_id`, and the page loads its data from the existing `GET /api/v1/users/`, which is
> server-enforced to `require_role([ADMIN, MANAGER])` (see the access-enforcement note above). The
> badge gate is therefore **narrower than** that read split — a Manager can open the Users page and
> read the list but cannot print badges — consistent with the Admin-only user-write posture in the
> note directly above.

### Dashboard

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Own dashboard layout — read / save / reset (`GET`/`PUT /dashboard/layout`, `POST /dashboard/layout/reset`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Self-scoped by construction, like `/users/me/*`.** The three routes
> (`app/api/endpoints/dashboard_layouts.py`) carry **no `require_role`**: they read and write only the
> caller's own row for the active company (`get_current_user` + `get_current_company_id`) and never
> accept a user id. A layout is presentation only and grants nothing — every endpoint a widget reads
> enforces its own authorization. **Reads** are open to any credential that reaches the route, an API
> token included (it gets `can_edit: false`); a kiosk badge token is refused **403** by its path fence,
> as everywhere outside `/api/v1/shop-floor`. **Saves and resets** additionally need an interactive
> person (`is_interactive_person`) in an active company they belong to (or a platform principal in a
> switched context), outside a read-only company view — otherwise **403** — and write a required
> audit row (resource type `dashboard_layout`; **503** and nothing saved when it cannot be written).
> Operators hold the row too, but `/` redirects them to the kiosk, so they never see the page.

**Which widgets a viewer is offered.** The dashboard's widget catalog (see
[`docs/DASHBOARD.md` → Widget catalog](DASHBOARD.md#widget-catalog)) shows each widget only to
viewers whose effective permissions (role defaults plus company overrides, evaluated by the SPA's
`usePermissions().can`) satisfy its eligibility rule. Eligibility is a UX proxy: a widget whose
endpoint refuses the viewer still renders "Not available for your role" in place. With the default
role permissions:

| Widgets | Rule | Default roles offered them |
|---|---|---|
| Every default-layout widget except Setup progress, plus the Action Inbox and Notifications panels | none | All (Operators are redirected away from `/`) |
| Setup progress banner | Admin (`isAdmin`: the `admin` role or a superuser) | Admin |
| Open CARs and Pending FAIs tiles, Quality Pulse panel | `quality:view` | Admin, Manager, Supervisor, Quality, Viewer |
| Overdue / Due Today work-order lists, Late Jobs, Blocked Jobs, Today's Output, Active Downtime, Maintenance Due | `work_orders:view` | All |
| Inspection Queue | `receiving:view` | Admin, Manager, Supervisor, Quality, Viewer |
| Shipping Outlook | `shipping:view` | Admin, Manager, Supervisor, Shipping, Viewer |
| Delivery & Yield KPIs, Visitors On Site | Admin, Manager or Supervisor **role** (mirrors the endpoints' `require_role`, so a permission override cannot widen it) | Admin, Manager, Supervisor |

Superusers pass every rule; Platform Admins pass every rule except Setup progress, which checks for
the `admin` role itself. **Links inside a visible widget are gated separately**, by the same route
check `PrivateRoute` enforces (`canAccessPath`): a link the viewer cannot open renders as plain text. With the default permissions that removes, for example, the
Calibration Due links for Supervisor, Shipping and Viewer (`/calibration` needs `quality:calibration`)
and Work Center Status **View All** for everyone but Admin (`/work-centers` needs `admin:settings`).
See [API → Personal dashboard layout](API.md#personal-dashboard-layout).

### Customers

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (list, names, detail, stats) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | | | | | |
| Delete (soft or hard) / restore | ✓ | ✓ | | | | | |
| Import (`POST /customers/import-csv`) | ✓ | ✓ | | | | | |
| Export control: read, add or change contracts (`GET /customers/{id}/export-control`, `POST /customers/{id}/contracts`, `PUT /customers/{id}/contracts/{contract_id}`) — never through an API token | ✓ | | | | | | |

> **Export control is Admin-only, and a person's act (owner decision 2026-11-13, Claude-plugin
> brief P0.3).** All three routes take `require_role([ADMIN])` (`EXPORT_CONTROL_ROLES`). On top of
> that they refuse **any** API token, whatever its role or scope, with **403** `"An API token cannot
> read or change a customer's export control: an Admin does it, signed in to the app."`. They are
> the only writer of `customer_contracts`. A contract flagged ITAR / export controlled / CUI (or
> classified `itar_export_controlled` / `cui`) makes the customer export-controlled, whatever the
> contract's `active` flag or dates. Its records are then redacted for AI-external API tokens.
> Every write carries a required `reason` and REQUIRED audit rows (503, nothing saved, when they
> cannot be written). There is no delete: un-marking is a `PUT` that clears the flags, also with
> a reason.
>
> The UI is the **Export control** panel on the customer detail in Customers. It is shown to
> Admin, Platform Admin and superuser, the role set `require_role([ADMIN])` admits, so a hidden
> panel and a refused call agree. Manager keeps Edit on the customer record itself but not this.
> See [docs/EXPORT_CONTROL_AI_EGRESS.md](EXPORT_CONTROL_AI_EGRESS.md).

> **Create and Edit were open to every authenticated user until this change**
> (`POST /customers/`, `PUT /customers/{id}` depended on `get_current_user` only). Both are now
> role-gated in `app/api/endpoints/customers.py`, with two different role sets:
>
> - **Edit** is `require_role([ADMIN, MANAGER])` (`CUSTOMER_UPDATE_ROLES`), matching the customer
>   import, delete and restore routes and the vendor master's create / update. Changing an
>   existing customer changes the CoC / FAI requirements and payment terms that quotes and
>   shipments read, so it is treated as an office-controlled master.
> - **Create** is `require_role([ADMIN, MANAGER, SUPERVISOR])` (`CUSTOMER_CREATE_ROLES`): exactly
>   the roles `POST /work-orders/` admits. New Work Order (`WorkOrderNew.tsx`) creates the customer
>   inline when the typed name is not in the master yet, and abandons the work-order submit if that
>   create is refused. A create gate narrower than the work-order gate would stop a Supervisor from
>   opening a work order for a new customer. A test holds the two role sets equal
>   (`test_customer_create_gate_admits_exactly_the_work_order_create_roles`). A Supervisor create
>   accepts the full create body (requirements and terms included), as it did before this change.
>
> Platform Admin and superuser pass both gates as everywhere else. Reads stay read-broad.
>
> **Known UI gap:** the Customers page shows Add and Edit to every role and the nav entry is not
> gated. An Operator, Quality, Shipping or Viewer user who saves a new customer, and anyone below
> Manager (Supervisor included) who saves an edit, sees the server's **403** as an error toast.
> There is no `customers:*` permission in `frontend/src/utils/permissions.ts` yet to hide the
> buttons with. New Work Order is not affected: only roles that may create work orders reach its
> inline create, and all of them may create a customer.
>
> **Tenancy.** Every customer route is scoped to the active company. `GET /customers/{id}/stats`
> and the hard-delete dependency check now also scope the parts and work orders they read by
> `customer_name` (previously unscoped, so a name shared by two companies leaked the other
> company's part numbers, work-order numbers and customer POs). Name and code are unique per
> company (migration `115`), not globally. See `docs/API.md` → Customers.

### Custom Fields

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View definitions and values | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit definitions | ✓ | ✓ | | | | | |
| Deactivate definitions | ✓ | | | | | | |
| Set / delete values (**ungated, known gap; see note**) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Known gap, not intended policy: value writes carry no role gate.** Any authenticated user of
> the active company, Viewer included, can set or delete a custom-field value on any record type.
> That contradicts Viewer's read-only definition at the top of this document. It predates this
> change and is tracked as a follow-up: gate value writes, at minimum excluding Viewer, preferably
> mirroring the edit roles of each target record type. No page in the SPA calls the value-write
> routes today; they are reachable through the API and the MCP catalog.
>
> What changed is tenancy and audit. The `/custom-fields/values/*` routes now resolve the
> definition, the target record and the value row in the active company only (**404** otherwise;
> writes require a live record), and every value create, overwrite and delete writes an audit row
> (`custom_field_value`). Definition create, update and deactivate also write audit rows now
> (`custom_field_definition`: `CREATE`, `UPDATE`, and a `STATUS_CHANGE` `active` → `inactive`).
> See `docs/API.md` → Custom Fields.

### Bulk Imports (Import Center / Excel Migration Kit)

Recoverable `/import/batches*` history, review, correction exports and resume calls
enforce the same entity-specific import roles below. A batch ID does not grant access
to an entity the current user cannot import. Successful input rows cannot be changed
through correction uploads. These multipart batch routes are excluded from MCP's
generated catalog, matching the existing import boundary.

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Download templates (`GET /import/templates*`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Import users (`POST /users/import-csv`) | ✓ | | | | | | |
| Import parts / materials | ✓ | ✓ | ✓ | | | | |
| Import customers / vendors / work centers | ✓ | ✓ | | | | | |
| Import routings (`POST /routing/import/preview`, `/import/commit`) | ✓ | ✓ | ✓ | | | | |
| Import open work orders (`POST /work-orders/import`) | ✓ | ✓ | ✓ | | | | |
| Import open purchase orders (`POST /purchasing/purchase-orders/import`) | ✓ | ✓ | | | | | |
| Import the Approved Supplier List (`POST /purchasing/vendors/asl-import/preview`, `/commit`; update-only) | ✓ | ✓ | | | ✓ | | |
| Import PM schedules (`POST /maintenance/schedules/import/preview`, `/import/commit`) | ✓ | ✓ | ✓ | | | | |

> **Endpoint mapping (A0.2 Excel migration kit, enforced in code).** All rows above apply
> identically to dry-run (`?dry_run=true`) and commit calls.
> - **Templates are open to any authenticated user** (`get_current_user`): the XLSX templates are
>   static workbooks containing **no tenant data**, so listing/downloading them carries no read risk.
> - **Open-WO import mirrors Work Orders → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/work_orders.py`) — importing an
>   open work order creates+releases a WO through the same generation path as `POST /work-orders/`,
>   so it carries exactly the WO Create/Release role set.
> - **Open-PO import is Admin / Manager only — deliberately narrower than WO import**:
>   `require_role([ADMIN, MANAGER])` (`app/api/endpoints/purchasing.py`). Imported POs land directly
>   in **`sent` (issued)** status, and the interactive PO `/send` transition is Admin/Manager-only —
>   allowing Supervisor here would let a spreadsheet issue POs its holder cannot issue in the UI
>   (privilege escalation via import).
> - **User import is Admin-only and cannot mint `platform_admin`**: a row with
>   `role = platform_admin` is rejected per-row (`"role 'platform_admin' cannot be assigned via
>   import"`), and `platform_admin` is excluded from the advertised valid-roles list. The
>   platform-admin role is the cross-company Werco oversight role and must never be assignable from
>   a tenant spreadsheet, even by a company Admin.
> - **Routing import mirrors Routings → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/routing.py`) on both the
>   `/routing/import/preview` (dry-run) and `/routing/import/commit` endpoints — it creates **draft**
>   routings through the same path as `POST /routing/`, so it carries exactly the Routings Create role
>   set (Release stays Admin/Manager — imported routings land as draft and must be released
>   separately). The **frontend** gates the Routing page **Import Routings** button (which opens the
>   `RoutingImportWizard` dry-run/commit modal) on the `routings:create` permission via
>   `hasPermission` (`frontend/src/pages/Routing.tsx`), matching this server-side role set —
>   operator / quality / shipping / viewer never see the button. The Import Center's **Routings** tab
>   (`mode: 'linked'`) only surfaces the template download + column hints and links to the Routing
>   page; the upload/preview/commit lives in the wizard, not in the Import Center.
> - The entity-import role sets (parts/materials → A/M/S; customers/vendors/work centers → A/M) are
>   unchanged from the pre-existing CSV imports and match each module's Create row above.
> - **ASL import mirrors recording a supplier approval** (`ASL_WRITE_ROLES`, Admin / Manager /
>   Quality — see [Purchasing](#purchasing)): it writes the same vendor fields as `PUT
>   /purchasing/vendors/{id}/approval`, never creates a vendor, refuses an API token (403) on both
>   routes, and neither route is an MCP tool. **PM schedule import mirrors creating a PM schedule**
>   (`_PLANNING_ROLES`, Admin / Manager / Supervisor — see [Maintenance](#maintenance-pm-schedules-maintenance-work-orders-event-log)),
>   including the equipment assets it creates; it accepts an API token (except a sheet that takes
>   checklist steps away from a schedule whose open work order predates migration `132`, a row error —
>   see the Maintenance note) and both routes are MCP tools.
>   Both are their own preview / commit pairs, not `/import/batches`; the Import Center shows them as
>   linked cards that open Supplier Scorecards and Maintenance respectively, and both commits write
>   required audit rows (not `source = "import"`: `asl_import` and `pm_import`).
> - **Audit:** every committed import row writes a tamper-evident `audit_log` entry tagged
>   `source = "import"`; dry runs write nothing (savepoint rollback). See `docs/API.md` →
>   Bulk Imports & Templates and `docs/EXCEL_MIGRATION_RUNBOOK.md`.

### Bulk Data Export

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Export any bulk dataset (CSV / XLSX) | ✓ | ✓ | | | | | |

**This row is server-enforced**, unlike the **View** rows for the same modules — see
[Access enforcement model](#access-enforcement-model). Supervisor, Operator, Quality, Shipping and
Viewer can still *read* every one of these datasets in the UI; what they cannot do is download the
whole set as a file.

Every route below is `require_role([ADMIN, MANAGER])`, tenant-scoped via `get_current_company_id`,
and writes an `EXPORT` audit row through `AuditService`:

| Endpoint | Dataset (`resource_type`) | Notably discloses |
|---|---|---|
| `GET /api/v1/exports/work-orders/export` | `work_order` | customer name / customer PO, scrap quantities |
| `GET /api/v1/exports/parts/export` | `part` | **`standard_cost`**, reorder points, customer part numbers |
| `GET /api/v1/exports/inventory/export` | `inventory_item` | **`unit_cost` + computed `total_value`**, lot / serial, locations |
| `GET /api/v1/exports/purchase-orders/export` | `purchase_order` | vendor identity, subtotal / tax / shipping / total |
| `GET /api/v1/exports/purchase-orders/lines/export` | `purchase_order_line` | **`unit_price`** per line — the open purchase commitments |
| `GET /api/v1/exports/quotes/export` | `quote` | **customer contact + email (PII)**, subtotal / total |
| `GET /api/v1/exports/inventory/transactions/export` | `inventory_transaction` | the whole valuation ledger incl. free-text `notes` |
| `GET /api/v1/exports/fai/export` | `fai` | the FAI log of record: every first article with its customer, customer PO, job, result and customer approval |
| `GET /api/v1/analytics/custom-report/export` | `custom_report` | a tenant-authored query over a whole data source — every matching row, capped at 10,000 (refused **400** over it, never truncated); only a template the caller owns, a shared one, or any for an Admin ([Analytics](#analytics)) |
| `GET /api/v1/consumables/checkouts/export.csv` | `consumable_checkout` | employee names/departments, quantities, issuer and free-text notes |

> **Two surfaces sit at a stricter tier and stay there.** `GET /visitor-logs/export.csv` is
> `require_role([ADMIN, MANAGER])` because **visitor PII is a documented exception to read-broad**
> (its list endpoint is gated too) — a PII decision, not this one, and it is the audit-shape
> precedent the eight `/exports/*` routes now follow.
> `GET /estimate-workbench/{id}/export/audit.xlsx|.json|customer.pdf` is
> `require_role([ADMIN, MANAGER, SUPERVISOR])` and is **out of scope**: the path carries an estimate
> id, so it is one record, not a dataset. The gate above is a **floor** — an existing stricter tier
> is never loosened to meet it.

> **The audit row records the request, never the payload** (`app/services/export_audit.py`):
> `action="EXPORT"`, `resource_type` = the dataset, the row count in the description, and
> `new_values` carrying the format, the columns actually disclosed and the filters that selected the
> rows. It is committed **before** the file streams, so an abandoned download is still on record. A
> **403 leaves no row** — a refusal disclosed nothing.
>
> **Caller-supplied text is fenced on the way in, because on the way out there is no remedy.** An
> `audit_log` row is un-`UPDATE`-able and un-`DELETE`-able (the `008`/`060` triggers) and is covered
> by the integrity hash, so anything the request puts in `new_values` is permanent. Two fences,
> because the two inputs have different shapes: `columns` is fenced by **allowlist** (only names the
> endpoint recognizes are recorded — there is a known set to intersect against), and filter *values*
> are fenced by **length** (free text by definition, so `max_length` on each `Query` — a value past
> the bound is a **422** and writes nothing, and the bound is set at the width of the column the
> filter compares against so nothing that could match a row is ever refused). `export_audit._cap`
> re-applies the length bound inside the shared seam as a backstop for a future exporter that forgets
> to declare one. Coverage: `backend/tests/api/test_export_gate_and_audit.py`.
>
> **"Audited" here is best-effort, as it is everywhere else in the app.** `AuditService.log` swallows
> an audit failure that its savepoints contain (a `sequence_number` collision that outlasts the
> retries) and a Python-side failure in its own work, such as a value its JSON columns cannot
> serialize, which it rolls back to the audit INSERT's savepoint (`app/services/audit_service.py`). If
> the chain write fails either way, the export still streams and goes unrecorded. A database error
> that aborted the transaction (for example a deadlock on the chain lock) is re-raised, and the export
> fails with no file streamed. That is the repo-wide contract, not something specific to exports —
> the same is true of the visitor-log export and every audited write path — but read the row above
> as "every export writes an audit row on the success path", not as an absolute guarantee that no
> byte can leave without one.

> **Not bulk exports, deliberately ungated:** single-record documents — `GET /shipping/{id}/coc/pdf`,
> `GET /quality/car/{id}/pdf`, `POST /quotes/{id}/generate-pdf`, `GET /laser-nests/{id}/document`,
> `GET /shop-floor/documents/{id}/inline` (gating it would break the kiosk document viewer),
> `GET /documents/{id}/download`, `GET /po-upload/pdf/{path}`,
> `GET /rfq-packages/{id}/internal-estimate-export` — and `GET /import/templates/{entity}`, a static
> workbook containing no tenant data. The frontend `<DataTable>` CSV button is also untouched: it
> serializes rows the client already fetched through ordinary reads, so gating it would require
> gating the reads.

### Analytics

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Flow / WIP-aging / adoption (Lean Phase 1) | ✓ | ✓ | ✓ | | | | |
| FPY / scrap Pareto (Lean Phase 1) | ✓ | ✓ | ✓ | | ✓ | | |
| Predictive forecasts (delivery / capacity / inventory demand) | ✓ | ✓ | ✓ | | | | |
| Custom reports — data-source catalog, ad-hoc run, save a template (`GET /data-sources`, `POST /custom-report`, `POST /custom-report/templates`) | ✓ | ✓ | | | | | |
| Custom reports — list saved templates, own + shared (`GET /custom-report/templates`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Custom reports — open / run a saved template: own or shared, or any for an Admin (`GET /custom-report/templates/{id}`, `POST …/{id}/run`) | ✓ | ✓ | | | | | |
| Custom reports — change / delete a saved template, author or Admin only (`PATCH` / `DELETE /custom-report/templates/{id}`) | ✓ | ✓ | | | | | |
| Export (`GET /custom-report/export`, own or shared template or any for an Admin, audits an `EXPORT` action) | ✓ | ✓ | | | | | |

> **Lean Phase 1 analytics reads are role-gated in code.** `GET /api/v1/analytics/flow`,
> `GET /analytics/wip-aging`, and `GET /analytics/adoption` require
> `require_role([ADMIN, MANAGER, SUPERVISOR])`; `GET /analytics/fpy` and
> `GET /analytics/scrap-pareto` additionally admit **Quality**
> (`require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`) — yield and scrap categorization are
> quality-system reads. All five are read-only and tenant-scoped (`app/api/endpoints/analytics.py`).
> The pre-existing View row (overview / KPIs / trends / quality metrics) remains any-authenticated.
>
> **Predictive analytics are role-gated AND now tenant-scoped.**
> `GET /api/v1/analytics/predict/delivery/{work_order_id}`, `GET /analytics/predict/capacity` and
> `GET /analytics/predict/inventory-demand` require `require_role([ADMIN, MANAGER, SUPERVISOR])`.
> All three are read-only and write nothing (no ledger row, no audit row, no event).
>
> **The role gate was already there; tenancy was not.** `PredictionService` was constructed with a
> session and **no `company_id`**, so every read underneath these three routes ran unscoped — an
> invariant-1 defect, and the role gate did nothing to contain it: the ADMIN / MANAGER / SUPERVISOR
> check confirms the caller holds a privileged role *in their own company*, then the service read
> every company's data. `/predict/delivery/{work_order_id}` was the worst of the three — it resolved
> a caller-supplied sequential primary key with **no ownership check at all**, returning any tenant's
> work-order header plus its sequenced routing (machine and hours per step), which for a job shop is
> the process plan itself.
>
> Each route now takes `company_id: int = Depends(get_current_company_id)` — the **active** company,
> so platform-admin context switching is honoured — and passes it to the constructor, matching the
> sibling `AnalyticsService(db, company_id)` in the same router. `company_id` is a constructor
> argument rather than a per-call one so an unscoped construction is a `TypeError` rather than a
> silent platform-wide read. A foreign `work_order_id` now 404s **byte-identically to an absent one**
> (`{"detail": "Work order not found"}`), so the refusal is not an existence oracle (#189
> convention). Tenancy is pinned by `backend/tests/api/test_prediction_tenancy.py`.
>
> **Operational note for multi-company installs:** because these figures previously summed across
> every tenant, they will **drop** after this change — see
> [Predictive analytics behavior change](API.md#predictive-analytics-behavior-change) in `docs/API.md`.
> The old numbers were wrong; the smaller ones are correct.
>
> **`GET /reports/ship-otd` is any-authenticated (pre-existing reports posture).** The Lean Phase 1
> ship-based OTD/OTIF detail report follows `reports.py`'s convention — `get_current_user` only, no
> role gate, tenant-scoped. **Observation (compliance review, 2026-07-10):** this report exposes
> customer-name delivery rollups (per-customer OTD %, late counts) to every role in the tenant,
> including Operator/Viewer, under that pre-existing posture. If reports are later role-tiered,
> this endpoint should be revisited with the rest of `/reports/*`.

> **Custom reports: Admin / Manager on every verb but the list, plus an ownership rule on saved
> templates (2026-11-16).** Every route under `/api/v1/analytics/custom-report*` and
> `GET /analytics/data-sources` is `require_role([ADMIN, MANAGER])` and tenant-scoped, except
> `GET /analytics/custom-report/templates`, which stays any-authenticated as it always has. On top
> of the role gate, `app/services/report_template_service.py` applies one ownership rule:
> - **Read, run or export a template** (`require_readable`). Allowed when it is the caller's own,
>   `is_shared`, or the caller is an Admin; otherwise **403** `Access denied to this report`.
> - **Change or delete a template** (`require_modifiable`). Allowed for the template's author or an
>   Admin; otherwise **403** `Only the template's author or an admin can change or delete it`.
> - In both rules a superuser or Platform Admin counts as an Admin, as `require_role` already treats
>   them everywhere else. So a Manager can run a colleague's shared template but cannot open a
>   private one, edit or delete it, while an Admin can do all of these. The two rules used to
>   disagree: an Admin was refused a colleague's private template on read yet could `PATCH` it and
>   get the whole definition back, so the read refusal protected nothing (2026-11-16).
> - **A template in another company** is **404** `Report template not found` on every verb,
>   byte-identical to an id that does not exist, and is never listed.
>
> The list returns the caller's own templates plus every shared one, for every role (an Admin
> included, who opens a colleague's private template by id). A Supervisor, Operator, Quality,
> Shipping or Viewer user can therefore list shared templates but not open, run or export them.
> `GET` and `PATCH /analytics/custom-report/templates/{id}` and
> `POST /analytics/custom-report/templates/{id}/run` are new in this change. Template create, change
> and delete write `report_template` audit rows (the delete's is required: no row, no delete — 503);
> running a report writes nothing, and the export writes its `EXPORT` row only on success. API
> tokens are not refused on any of these routes (none is a judgment write), so an Admin- or
> Manager-bound token can build and run reports. A read-only token can read the catalog and the
> templates, but cannot run a report (both run verbs are `POST`) or download the CSV: the export
> refuses a read-only or AI-external token **403** before it reads the template or runs the query
> (`refuse_file_export_to_narrowed_token`), so neither the row-cap nor the empty-result 400 can
> answer it. See [API.md → Custom report builder](API.md#custom-report-builder).
>
> **The Custom Reports screen is gated to match (2026-11-16).** The Analytics → **Custom Reports**
> tab (`frontend/src/pages/Analytics.tsx`, `can('analytics:export')`) and the `/analytics/reports`
> route (`frontend/src/utils/routeAccess.ts`, `{ prefix: '/analytics/reports', permission:
> 'analytics:export' }`, which wins over `/analytics` → `analytics:view` by longest prefix) both
> require `analytics:export`. In the shipped role sets that permission is held by Platform Admin,
> Admin and Manager, the same set as the endpoints. Before this change every role saw the tab and
> then got a screen of 403s. A deep link from any other role is now refused at the route instead.
> The frontend gate reads the *effective* permission, so a company role override that grants
> `analytics:export` to another role shows that role the tab, but the server role gate still answers
> 403. As everywhere in this document, the server gate is the enforcement.

### OEE

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard / trends / six-big-losses / list records & targets) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Write (auto-calculate / create-edit-delete records & targets) | ✓ | ✓ | ✓ | | | | |

> **Write enforcement (read-broad / write-restricted).** The OEE **write/mutation** endpoints —
> `POST /api/v1/oee/calculate/{work_center_id}`, `POST`/`PUT`/`DELETE /oee/records`, and
> `POST`/`PUT`/`DELETE /oee/targets` — are now enforced **in code** to the Write row via
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`OEE_WRITE_ROLES` in `app/api/endpoints/oee.py`),
> matching the sibling Analytics-write posture. **This is a permission change:** these endpoints were
> previously open to any authenticated user. OEE **read** endpoints (`/oee/dashboard`, `/oee/trends`,
> `/oee/six-big-losses/{wc}`, and the list/get GETs for records and targets) depend on
> `get_current_user` only — they are tenant-scoped but not role-restricted, so operators/viewers can
> still load OEE dashboards. The **View** row therefore reflects intended UI visibility; the **Write**
> row is a server-enforced control. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Audit coverage (2026-06-09).** The OEE write endpoints now also write a tamper-evident `audit_log`
> row on every record/target create/update/delete (and the auto-calc upsert), so OEE mutations are on
> the hash chain alongside the role gate. No role change — audit-trail coverage only.

### Production calendar

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View the calendar / resolve an instant (`GET /production-calendar`, `GET /production-calendar/resolve`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Edit the calendar (`PUT /production-calendar`) | ✓ | | | | | | |
| Sync working calendars from the shifts (`POST /production-calendar/working-calendars/sync`) | ✓ | ✓ | ✓ | | | | |

> **Edit is Admin only** (`CALENDAR_EDIT_ROLES` in `app/api/endpoints/production_calendar.py`): the
> production-day start and the shifts decide how every OEE, wallboard, report, downtime and job-cost
> figure is bucketed, so changing them is a configuration decision, not a floor one. Every save writes a
> **required** `production_calendar` audit row (503, nothing saved, on failure). The **sync** carries the
> roles of `PUT /scheduling/work-centers/{id}/calendar` (`WORKING_CALENDAR_SYNC_ROLES`), whose rows it
> writes — one required `working_calendar` audit row per work center; its dry run (the default) writes
> nothing. Reads are open to every authenticated role in the tenant (the top-bar shift badge reads
> them). Tenant-scoped through `get_current_company_id`; a work center or shift of another company is
> 404 / 422. Superuser / Platform Admin bypass role checks, as elsewhere. See `docs/API.md` →
> Production calendar & shifts.
>
> **In the app** the editor and the sync both live on Admin Settings → **Production Calendar**
> (`/admin/settings?tab=calendar`), and `/admin/settings` is `AdminRoute`-gated. So a Manager or
> Supervisor, who may run the sync, reaches it through the API or the MCP tool
> `sync_working_calendars_from_shifts`, not the UI. The page's own gates mirror the server's
> (`canEditProductionCalendar` / `canSyncWorkingCalendars` in `frontend/src/utils/recordWriteAccess.ts`).
>
> **OEE shift references.** OEE writes (`OEE_WRITE_ROLES`) name a defined shift by `shift_id`; the
> role gate is unchanged.

### Downtime

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (events, active, summary, by-work-center, reason codes) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Report / resolve an event (`POST /downtime/`, `POST /downtime/{id}/resolve`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| Correct an event (`PUT /downtime/{id}`) | ✓ | ✓ | ✓ | | | | |
| Manage reason codes (`POST /downtime/reason-codes`, `PUT /downtime/reason-codes/{id}`) | ✓ | ✓ | ✓ | | | | |

> ⚠️ **This table is new because the router had NO role gating and no audit.** Every handler was
> `Depends(get_current_user)`, so a **Viewer could start, correct and close downtime events** — and
> UNPLANNED event minutes are subtracted from productive run in OEE availability (see OEE above).
>
> **Reporting stays on the floor; correcting matches OEE Write.** `DOWNTIME_REPORT_ROLES` (create /
> resolve, `app/api/endpoints/downtime.py`) is every tenant role except **Viewer**: `/downtime` is
> route-gated on `work_orders:view`, which every role holds, and `DowntimeTracking.tsx` calls only
> create and resolve, so operators keep logging stoppages. `DOWNTIME_MANAGE_ROLES` (`PUT` and the
> reason-code writes) is Admin / Manager / Supervisor — the `OEE_WRITE_ROLES` set — because both
> rewrite OEE inputs after the fact; no page calls them today. Reads stay open to every role,
> tenant-scoped. There is no delete endpoint. Superuser / Platform Admin bypass, as elsewhere.
>
> **Every state change writes a tamper-evident `audit_log` row** (`downtime_event`,
> `downtime_reason_code`), and `work_center_id` / `work_order_id` are validated against the active
> company (flat **404**). See `docs/API.md` → Downtime Tracking.
>
> ⚠️ **The client has no matching gate yet.** `DowntimeTracking.tsx` shows its create and resolve
> controls to a Viewer, who now gets a 403 toast.
>
> **Maintenance work orders also open and resolve events (migration `124`)**, under the maintenance
> verbs' own gates rather than the table above: starting a work-center maintenance WO that requires
> shutdown (Admin / Manager / Supervisor / Operator) opens or adopts an event, and completing it (the
> same roles) or a `PUT` to `cancelled` (Admin / Manager / Supervisor) resolves it; `completed` is only
> reachable through `POST .../complete`, and a completed or cancelled work order cannot be reopened
> (`PUT` 409). An operator's `POST /downtime/` on a machine a maintenance work order holds down is 409. See
> [Maintenance](#maintenance-pm-schedules-maintenance-work-orders-event-log).

> **Before deploying, check for legacy cross-tenant rows.** The write guards stop *new* mis-tenanted
> events; they do nothing about rows written before them. Event reads now render such a row's foreign
> work center / work order as `null`, but the rows themselves are a data problem:
>
> ```sql
> SELECT 'downtime_events.work_center' AS t, d.id, d.company_id AS owner, w.company_id AS fk_owner
>   FROM downtime_events d JOIN work_centers w ON w.id = d.work_center_id
>  WHERE w.company_id <> d.company_id
> UNION ALL SELECT 'downtime_events.work_order', d.id, d.company_id, wo.company_id
>   FROM downtime_events d JOIN work_orders wo ON wo.id = d.work_order_id
>  WHERE wo.company_id <> d.company_id;
> ```
>
> Empty result → nothing to do. Non-empty → repoint each row with `PUT /downtime/{id}` (Admin /
> Manager / Supervisor) at a machine or job the owning company actually has.

### AI recommendations (Action Inbox)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| List recommendations; dismiss, snooze, send feedback (`/ai/recommendations`, `…/dismiss`, `…/snooze`, `…/feedback`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Accept a **suggest-only** recommendation (record-only `…/accept`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Accept & apply** (`…/apply`), or **Mark handled** (record-only `…/accept`), a priority / blocker / draft-PO action card | ✓ | ✓ | ✓ | | | | |
| **Accept & apply**, or **Mark handled**, a draft-NCR action card | ✓ | ✓ | ✓ | | ✓ | | |
| Create a recommendation (`POST /ai/recommendations`); run the aggregation (`POST /ai/aggregate`) | ✓ | ✓ | ✓ | | | | |
| View governance (`GET /ai/governance`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Owner ruling 3 (2026-11-05): nothing AI-driven executes without a named person clicking
> Accept.** `POST /ai/recommendations/{id}/apply` is the only path that runs an AI recommendation's
> action, and it runs it **as the person who clicked** — their role, their audit attribution, the
> recommendation recorded in each audit row's `extra_data`. There is no system actor and no role
> bypass; the nightly job and `POST /ai/aggregate` mint and sweep cards and never apply one.
> Besides the role in the matrix, Accept & apply requires, in order: a pending card with an
> allowlisted `apply_on_accept` action (409); a person in an interactive session — **API tokens
> and kiosk/station tokens get 403** ([credential policy](#credential-policy-for-judgment-writes));
> a non-read-only company context (403); and **company AI egress on** (`allow_ai_egress`, 403
> while off). It is not an MCP tool. Superuser / Platform Admin pass the role check as everywhere.
>
> **Action cards are closed only by someone who could act on them.** Record-only Accept on an
> action card ("Mark handled") needs the same role as Accept & apply and an interactive session,
> but **not** AI egress, because nothing executes. A lower role or an API token gets 403 and the
> card stays open for the people who can apply it; anyone can still snooze it or send feedback.
> This exists because a blocker-triage card is minted once per blocker: an Operator or an agent
> who accepted it used to retire it for good. Suggest-only cards remain acceptable by anyone.
>
> **Dismiss, snooze and feedback deliberately stay open to every authenticated user and every
> credential**, including on action cards (a design decision for this release). They run
> nothing, and gating them would leave an Operator unable to clear their own inbox. The gate
> above is therefore partial: Dismiss retires a card as permanently as Mark handled does (and
> also records a `rejected` learning signal). **The owner decided on 2026-11-07 that Dismiss on an
> action card stays open to every role** — it does not take the `mark_handled_refusal` gate. That is a
> decision, not an oversight.
>
> **The AI-egress gate on Accept & apply is a deliberate reading of ruling 3, confirmed by the
> owner on 2026-11-07.** Accept & apply makes no LLM call — it is a deterministic, tenant-local write — so
> the coupling is to the company's "AI actions" switch, not to data egress. While egress is off
> (the default) no tenant gets Accept & apply; cards show the plan and anyone whose role could
> apply it can Mark handled. Dropping the gate is one check in `apply_refusal` plus copy, and is an owner decision.
>
> **The AI door is never broader than the record's own route.** Draft PO and blocker writes match
> their routes (Admin / Manager / Supervisor). Filing an NCR (`POST /quality/ncr`) is open to any
> authenticated user — `quality_record_service.WRITE_ROLES` (Admin / Manager / Quality) governs
> NCR *edits*, disposition, void and restore, not filing — so the Quality+ (incl. Supervisor) set
> for the draft-NCR card is narrower than the direct route, not wider.
>
> **What the person reviews is what changes.** An action card whose action names a different work
> order, part or blocker than the record its "Open record" link points at is refused (409, plan
> blocked) rather than applied, and a blocker card on a missing, deleted or terminal job is a
> closed target that is refused the same way.
>
> The role sets come from `ACTION_MIN_ROLE` in `backend/app/services/ai_action_applier.py`
> (Supervisor and up for priority, blocker and PO actions; Quality, Supervisor, Manager and Admin
> for NCR). The frontend reflects them through the server-computed `apply_action.can_apply` /
> `can_mark_handled` flags rather than its own role map. See
> [API.md → AI Recommendations](API.md#ai-recommendations-action-inbox) and
> [AI_ALWAYS_ON.md](AI_ALWAYS_ON.md).

<a id="werco-copilot-read-only-ai-chat"></a>

### Hank (AI chat and PDF filing)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Chat (`POST /copilot/chat`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Shift briefing (`GET /hank/briefing`; source permissions apply) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Repeat-job task (`work_orders:view` + `work_orders:create`) | ✓ | ✓ | ✓ | | | | |
| Draft-PO task (`purchasing:view` + `purchasing:create`) | ✓ | ✓ | ✓ | | | | |
| Attach-existing-PDF task (`work_orders:view`) | ✓ | ✓ | | | ✓ | | |
| Personal work-order follow-up (`work_orders:view`; interactive session) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Personal Hank preferences (interactive save/reset) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Upload and release PDF (`POST /documents/upload`) | ✓ | ✓ | | | ✓ | | |
| Smart PDF intake and reviewed filing (interactive document role) | ✓ | ✓ | | | ✓ | | |
| Receive-delivery task (`receiving:view` + `receiving:create`) | ✓ | ✓ | ✓ | | | | |
| Report-production task (`work_orders:view` + `work_orders:complete`; own active clock required) | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| Pending-shipment task (`shipping:view` + `shipping:create` + `work_orders:view`) | ✓ | ✓ | ✓ | | | ✓ | |
| Change-due-date task (`work_orders:view` + `work_orders:edit`; moves `due_date` only, never `must_ship_by`) | ✓ | ✓ | ✓ | | | | |
| Add-note task (`work_orders:view`; posts a work-order comment; a person only — never an API token, never through MCP) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Handoffs and own routine runs (interactive `work_orders:view`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Draft/edit/archive a routine (interactive `work_orders:view`) | ✓ | ✓ | ✓ | | | | |
| Approve a routine (interactive `work_orders:view`) | ✓ | ✓ | | | | | |

> **Task authority applies to every stage.** Preparing, executing and cancelling a task
> recheck its action-specific role and effective permissions. Reading/listing retains the
> role fence and effective view permissions, without requiring create permission or a
> writable company context, so an employee can still recover a receipt after write access ends.
> Table checkmarks assume default permissions; a company override can remove access but
> cannot bypass the action's role fence. Saved tasks belong to the active company, actor
> and credential (interactive user session or originating API token). Other actors/tokens
> receive 404. Commands carry expected company and task version; stale source fingerprints
> require a fresh reviewed proposal. Read-only company contexts cannot perform task actions.
> Existing Superuser/Platform Admin bypass and token fences remain. No new role is added.
> Task inbox status filters and pagination preserve the same company/actor/credential and
> current view-access predicates on every page. A filter cannot reveal another employee's
> task or recover records whose source access was removed. Shared handoffs are a separate
> participant-scoped resource; they do not expose another employee's private tasks.
>
> **Due-date and note tasks (2026-11-16).** `change_due_date` takes the role set of
> `PUT /work-orders/{id}`, the other door that moves a due date — Admin / Manager / Supervisor,
> the roles that hold `work_orders:edit` by default — and that route's terminal-status 409 through
> one shared helper. `add_note` follows the comment rule: commenting mirrors work-order read access,
> so all seven tenant roles hold it, and a **Viewer**'s capabilities now report `can_write: true`
> with `allowed_kinds: ["add_note"]` — the first reviewed task open to a Viewer (follow-ups,
> preferences and handoffs were already open; they are not task kinds). Credentials (owner
> decision 2026-11-16): `add_note` is **people-only** — every API token is refused **403** at
> preparation and execution and is never offered the kind, and the MCP door refuses it to an agent
> even on a person's own JWT; `change_due_date` stays open to a **full** API token, in parity with
> the `update_work_order` agent door, while read-only and AI-external tokens are refused by their
> own gates. Both are in
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes), the second as an
> open owner follow-up. See [API → Hank](API.md#hank-ai-shop-teammate).
>
> **Intake has document-publication authority.** Upload/analysis/review/file/retry/cancel
> are restricted to interactive Admin/Manager/Quality (or existing elevated authority).
> Read-only sessions can inspect their own saved files, sources and receipts but cannot
> change them; API-token/kiosk credentials are excluded. Part, job, supplier, PO and receipt
> matches/links independently require their module's effective view permission. Explicit
> certificate release and receipt binding additionally require `receiving:view` and
> `receiving:create`, matching the exact receipt part/supplier and refusing replacement of
> an existing certificate. Required document/receipt/intake audits commit atomically.
> No extraction constitutes material acceptance, drawing approval or production release.
> The worker checks current user/company authority before analysis and before persisting
> results; company AI egress independently controls the external extraction call.
>
> **Operational reads retain source access.** Readiness/knowledge require work-order view;
> readiness quality details require quality view and material coverage requires inventory
> plus purchasing view. Shipping packets require shipping and work-order view. Purchasing
> impact requires purchasing view; stock requires inventory view, and direct downstream
> jobs require work-order, BOM and inventory view. Genealogy requires inventory, quality,
> work-order and purchasing view. These reports do not mutate or reconcile source records.
>
> **Handoffs and routines use interactive authority.** Same-company work-order access,
> active membership/elevated access and writable context are required for changes.
> Handoffs are readable only by their sender/recipient; only the recipient acknowledges/
> completes and only the sender cancels. Either participant may attach verified photos
> while the handoff and job are active. In-app participant notices accompany explicit
> handoff changes, separately from the optional personal watch-alert preference.
> Routine management permits Admin/Manager/Supervisor; approval permits Admin/Manager.
> Other employees see approved routines and run them under their own current authority.
> Approved routine snapshots do not confer permissions: completed action/intake/handoff
> evidence is checked before advancing the corresponding step, and checklist notes do
> not execute business actions. Editing a procedure returns it to draft; run history stays.
> The combined work queue retains each source's private/participant and current access
> predicates and bounds its results; it is not a company-wide employee surveillance view.
>
> **Personal follow-ups have a separate gate.** `can_watch` requires effective
> `work_orders:view`, an active company and user, and company membership or existing
> Superuser/Platform Admin access. It has no action-role fence and adds no permission.
> Only interactive credentials can create/read/control watches; API tokens and kiosk
> credentials are excluded. Read-only company contexts can read but cannot start, check,
> snooze, resume or stop a watch. Every command retains company/owner/version checks.
> The worker reloads current owner, company and permissions on every check; it stops with
> generic `needs_attention` and no alert if access or the live work order is unavailable.
> Completion writes only the personal task receipt, required audit and owner-only in-app
> notification atomically; it cannot clear blockers, approve documents or change a job.
>
> **Preferences are personal presentation settings.** Reads scope to the current
> company/user and return defaults without inserting rows. API credentials and read-only
> sessions may read their own choices; save/reset require an active interactive user and
> company, writable context, and membership or existing elevated company access. The
> kiosk path fence remains. No module permission is granted by a focus-area choice:
> briefings reorder only authorized sections. Saves/reset use expected company/version,
> tenant/user locking and required audit in the same transaction. The alert preference
> may suppress a new personal completion notification, never the completed receipt or
> its audit evidence. Preferences do not alter business records or automation policy.
>
> **Shift briefing source permissions.** This deterministic read is separate from chat and
> uses the active company's effective role overrides: `work_orders:view` for shared
> blockers/late work and the caller's open-clock jobs, `quality:view` for NCRs,
> `inventory:view` for stock/MRP exceptions, and `purchasing:view` for PO exceptions.
> Shipping due-date review requires both `shipping:view` and `work_orders:view`. Sections
> without access are omitted; roles choose display order, not additional authority. Existing
> Superuser/Platform Admin bypass and token fences remain. No new permission or write is added.
>
> **Endpoint vs. tool-level access.** The endpoint (`app/api/endpoints/copilot.py`) requires only
> an authenticated user (`get_current_user`), so the chat itself carries no role gate.
> Its thirteen read tools retain source access; `my_shift_briefing` additionally checks effective
> permissions for each source. `prepare_hank_task` can save an audited review proposal only
> under the action authority above. It cannot execute business changes. `search_documents`
> returns bounded, tenant-scoped document
> metadata and links, including revision/status; it does not read PDF contents or expose storage
> paths. The `search_erp` tool **excludes employee (`user`-type)
> results entirely** — data minimization, so employee names/emails never enter model prompts
> regardless of the caller's role; the **Admin/Manager-only** gate on user results inside global
> search now applies to `GET /search` only. The tool registry
> (`CopilotToolSpec.allowed_roles` in `app/services/copilot_service.py`) supports fully
> role-restricted tools; the task preparation handler independently rechecks current
> role/permission/company authority, even if the model attempts an unavailable action.
>
> **Tenant scope is never model-controlled.** `company_id` is injected server-side from the
> active company (`get_current_company_id`) into every tool call; tool input schemas carry no
> tenant identifier, and undeclared input keys supplied by the model (including a `company_id`)
> are dropped before dispatch. Per-user rate limit: 20 requests/minute default
> (`COPILOT_RATE_LIMIT_PER_MINUTE`). See [API → Hank](API.md#hank-ai-shop-teammate).
>
> **PDF filing is a separate user action.** Hank's Upload PDF form calls the existing document
> upload route, retaining its Admin/Manager/Quality release gate, active-company checks, and
> required audit. The UI hides uploading for read-only sessions; the server remains authoritative.
> Existing Superuser/Platform Admin role bypass behavior is unchanged. Submission creates a
> released document, optionally linked to the current verified job when the user checks the
> attachment box. Chat cannot invoke this write. No new role or permission is granted.

### Visitor Logs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View / search log (`visitor_logs:view`) | ✓ | ✓ | ✓ | | | | |
| Sign in / sign out a visitor | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Export log (CSV) | ✓ | ✓ | | | | | |
| Back-enter an offline visit (`POST /manual`) | ✓ | ✓ | | | | | |
| Delete (soft) a visitor record | ✓ | ✓ | | | | | |
| Manage sign-in stations (create / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/visitor-logs`, `app/api/endpoints/visitor_logs.py`).** The two
> **visitor write** endpoints — `POST /sign-in` and `POST /sign-out` — are gated by the dedicated
> `get_signin_principal` dependency, which accepts **either** a PIN-minted station signin token
> (`type="signin"`, the lobby tablet) **or any authenticated staff user**. So the "Sign in / sign
> out" row is open to every authenticated user (and to an unattended station tablet), not a role —
> it is **not** the `require_role` model the rows below use. The **list** endpoint `GET /` is
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (this is the server-enforced read gate the
> `visitor_logs:view` permission and the `/visitor-log` route mirror — visitor PII is *not* on the
> read-broad domain default). **Export** (`GET /export.csv`, audits an `EXPORT` action), **soft-delete**
> (`DELETE /{id}`), **staff back-entry** (`POST /manual` — records an offline visit with its actual
> past times; a staff access token only, the PIN-minted station token is **rejected**), and **all
> station administration** (`POST /stations`, `GET /stations`, `POST /stations/{id}/revoke`,
> `POST /stations/{id}/reset-pin`) are `require_role([ADMIN, MANAGER])`.
> Every query is tenant-scoped (staff via `get_current_company_id`; the tablet via the authoritative
> `signin_stations` row, never the client `cid`); visitor records are soft-deleted, never
> hard-deleted; and every state change is tamper-evidently audited (station writes record the station
> label as the actor). See [docs/API.md](API.md) → Visitor Logs and
> [docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md).
>
> **`station-login` is the only new public write surface.** `POST /visitor-logs/station-login` is
> unauthenticated by design — a tablet cannot present a JWT — but it is **PIN-gated**: it verifies the
> shared station PIN against the bcrypt `pin_hash` and a bad/revoked station or wrong PIN returns
> **401** (indistinguishable; the failed attempt is audited). It is therefore not on this permission
> matrix. Like the inbound carrier webhook, trust is established without a user role.

### Crew-Station Kiosk

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Manage kiosk stations (create / list / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/shop-floor/kiosk-stations` + `POST /auth/kiosk-badge-token`).**
> All four station-administration endpoints (`POST /kiosk-stations`, `GET /kiosk-stations`,
> `POST /kiosk-stations/{id}/revoke`, `POST /kiosk-stations/{id}/reset-pin`, in
> `app/api/endpoints/shop_floor.py`) are `require_role([ADMIN, MANAGER])` — the same set as
> visitor sign-in stations. Everything the crew terminal itself does is **not** on the role
> matrix, because neither of its credentials is a role-bearing user session:
>
> - The **station token** (`type="kiosk"`, PIN-minted via the public rate-limited
>   `POST /shop-floor/kiosk-stations/station-login`) carries no user identity and is honored by
>   exactly four routes — the roster-enriched work-center-queue read (its own bound work center
>   only, via the dedicated `get_kiosk_or_user` dependency; since Dispatch v2 it also carries the
>   planner's `dispatch_notice` for that machine, see `docs/API.md` → "Floor dispatch notices"),
>   the badge-token mint, and the station's own workstation pair
>   (`GET /shop-floor/kiosk-stations/work-centers`, `PUT /shop-floor/kiosk-stations/work-center` —
>   list its company's active work centers and re-bind itself to one, audited as a `kiosk_station`
>   update), all three via `get_kiosk_station`. Every other endpoint rejects it with **401**; tenant
>   scope and revocation come from the authoritative `kiosk_stations` row, never the client `cid`.
> - The **badge-minted operator token** (`POST /auth/kiosk-badge-token`, station-token-gated) is
>   a 5-minute `scope="kiosk"` access token for the badge-identified user — on the allowed paths
>   the operator IS `current_user`, so the shop-floor endpoints' existing role/tenant/audit rules
>   apply unchanged and every labor mutation is attributed to the operator, never the station.
>   The mint itself checks no role, so a Viewer or Shipping badge mints. Since P0.4 (2026-11-13)
>   that badge is then refused **403** `Insufficient permissions` by the role-gated
>   `POST /shop-floor/operations/{id}/complete` (Admin / Manager / Supervisor / Operator / Quality),
>   while clock-in, production and clock-out still accept it — see
>   [Work Orders](#work-orders).
>   Outside `/api/v1/shop-floor/*` (+ `POST /auth/employee-logout`) the token is **403**
>   (path-fenced in `get_current_user`). No refresh token is ever minted. The mint runs the floor-PIN
>   step (migration `130`): a badge whose user has a PIN must be followed by it, and with the
>   company's PIN policy on a badge with no PIN is refused — an authentication rule, not a role
>   rule, the same for every role (see [docs/KIOSK.md](KIOSK.md) → Two-tier auth).
>
> Station lifecycle (create / reset-PIN / revoke), station-login failures, and badge-token
> issuance/failures all write tamper-evident audit rows. See [docs/API.md](API.md) →
> Authentication → Kiosk station tokens, and [docs/KIOSK.md](KIOSK.md) → Crew station mode.

### Admin

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Settings | ✓ | | | | | | |
| Integrations (`admin:integrations`) | ✓ | | | | | | |
| Audit Logs | ✓ | ✓ | | | | | |
| AI usage & cost summary (`/ai-usage/summary`) | ✓ | ✓ | | | | | |
| AI egress kill switch (`PUT /companies/me/ai-egress`; signed in — any API token 403) | ✓ | | | | | | |
| SMS egress kill switch (`PUT /companies/me/sms-egress`; signed in — any API token 403) | ✓ | | | | | | |
| Badge sign-in PIN policy (`PUT /companies/me/floor-pin-policy`, `require_floor_pin`; signed in — any API token 403) | ✓ | | | | | | |
| Wallboard display tokens (`/auth/display-token` issue/list/revoke + setup-code reissue) | ✓ | ✓ | | | | | |
| API tokens for bots / MCP clients (`/api-tokens` issue/list/revoke) | ✓ | | | | | | |
| Visitor sign-in stations (`/visitor-logs/stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| Crew kiosk stations (`/shop-floor/kiosk-stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| System | ✓ | | | | | | |

> **Badge sign-in PIN policy (AS9100D 8.5.2, migration `130`).** `PUT /companies/me/floor-pin-policy`
> is `require_role([ADMIN])`, refuses any API token **403** before anything is read, is not an MCP
> tool, and only ever changes the caller's own active company. A real flip writes two required audit
> rows (**503** and rollback on loss). The Admin Settings → **Badge Sign-in** tab is interactive for
> Admin and read-only for anyone else who can open Admin Settings. Issuing the PINs themselves is
> Admin, or a Manager for Operator / Supervisor / Shipping / Viewer accounts ([Users](#users)). See
> `docs/API.md` → Company (self-service).

> **Changing a role's permissions is an audited RBAC change that fails closed (2026-11-05).** The
> **Settings** row covers every `/admin/settings/*` route in `app/api/endpoints/admin_settings.py`
> (`admin_only = require_role([ADMIN])`). Of those, the company role-permission overrides — `PUT
> /admin/settings/role-permissions/{role}` and `POST /admin/settings/role-permissions/{role}/reset` —
> now require their evidence: a `role_permission` UPDATE row (old/new permissions, `granted`,
> `revoked`, `action`) written through `AuditService.log_required` inside
> `user_provisioning.atomic_security_write`. If that row cannot be written, the permission change is
> rolled back and the caller gets **503**, the same posture as account administration. The other
> admin-settings writes (work-center rates, email recipients, work-center types) now also write
> best-effort rows to the tamper-evident `audit_log`. Before this change all of them wrote only to
> `SettingsAuditLog`, the Admin page's mutable history table, which is outside the hash chain and
> still written for display. See `docs/API.md` → Admin Settings.

> **Integrations (carrier-account credentials + shipping profile) — endpoint mapping.** The
> carrier-integration admin console — `app/api/endpoints/integrations.py`, mounted under
> `/api/v1/admin/settings` — is enforced **in code** to `require_role([ADMIN])` on every route:
> the carrier-account CRUD (`GET`/`POST`/`PUT`/`DELETE …/carrier-accounts`), the credential-only
> `POST …/carrier-accounts/{id}/test-connection`, and the company shipping-profile
> `GET`/`PUT …/shipping-profile` (which holds the `allow_carrier_egress` kill switch). Carrier
> secrets are write-only (Fernet-encrypted, never returned — only `api_key_last4` /
> `has_webhook_secret`); deletes are soft deletes; create/update/delete and the egress toggle are
> audited. `PUT …/shipping-profile` is the `allow_carrier_egress` switch's only writer, so it refuses
> **any API token 403** whatever its role and is not an MCP tool (lead decision 2026-11-15 —
> [Credential policy for judgment writes](#credential-policy-for-judgment-writes)). The new `admin:integrations` permission string (granted to **Admin** in
> `app/models/role_permission.py`) drives the frontend Carrier Integrations tab's visibility. See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md).
>
> **Inbound carrier webhook is unauthenticated (by design).** `POST /api/v1/webhooks/carriers/{provider}`
> (`app/api/endpoints/carrier_webhooks.py`) has **no auth dependency** — a carrier cannot present a
> JWT. Trust is established by **HMAC signature** verification against the stored per-tenant webhook
> secret, and the owning tenant is resolved **only from stored shipment data**
> (`Shipment.aggregator_shipment_id`), never from caller input. A request that matches no secret or no
> shipment is dropped with **204** (no existence oracle). It is therefore not on this permission matrix.
>
> **Intentionally-unauthenticated endpoints (the full set).** Five write/verify surfaces establish trust
> *without* a user role — and so none appears on the role matrix: the **carrier webhook** above (HMAC
> signature), the visitor **`station-login`** (a shared station PIN mints a scoped `signin` token — see
> Visitor Logs above), the crew-kiosk **`station-login`** (`POST /shop-floor/kiosk-stations/station-login`,
> a shared station PIN mints a scoped `kiosk` token — see Crew-Station Kiosk above), the wallboard
> **display-token** verification (a scoped `display` JWT — see below), and the wallboard
> **setup-code claim** (`POST /auth/display-token/claim`, a single-use 15-minute pairing code —
> stored only as a SHA-256 hash — exchanged for the display JWT; rate-limited 10/minute per IP,
> uniform 404 for every failure — see below). Each binds the request to a
> tenant through stored server-side state, never caller-supplied identity.

> **Audit-log access (tenant-scoped).** The **Audit Logs** row above covers audit *retrieval*:
> `GET /api/v1/audit/`, `/audit/summary`, `/audit/actions`, `/audit/resource-types`
> (`require_role([ADMIN, MANAGER])`). These are **tenant-scoped** — each filters by the caller's
> active company (`get_current_company_id`), so Admin/Manager see only their own company's audit
> data.
>
> **Audit-integrity endpoints (`/api/v1/audit/integrity/*`).** These verify the tamper-evident
> hash chain and are authorized separately from retrieval:
>
> | Endpoint | Role | Scope |
> |----------|------|-------|
> | `GET /audit/integrity/status` | **Platform Admin only** (`require_platform_admin`) | Global chain |
> | `GET /audit/integrity/verify` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/verify-recent` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/record/{sequence_number}` | **Admin** (`require_role([ADMIN])`) | **Own active company only** |
>
> The three aggregate endpoints are Platform-Admin-only because the hash chain is a single global
> sequence interleaved across all tenants — its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to one company without leaking other tenants' data. The per-record
> endpoint serves a company Admin's "are *my* records intact?" need: a company-scoped Admin may
> verify only a record belonging to their active company, and a cross-tenant record returns
> **404** (not 403) so it can't be used to probe for another company's records. Platform Admins /
> superusers may verify any record (superuser bypasses role checks, as elsewhere).

> **AI usage & cost summary.** `GET /api/v1/ai-usage/summary` (`app/api/endpoints/ai_usage.py`)
> is enforced **in code** via `require_role([ADMIN, MANAGER])` and is **tenant-scoped** to the
> caller's active company. It returns read-only per-task / per-model aggregates over the
> `ai_usage_events` LLM telemetry ledger (operational telemetry, not audit data — see
> [docs/API.md](API.md) → AI Usage Telemetry). Note the **Manager allowance is currently dormant
> in the UI**: the only consuming surface is the Admin Settings → AI Usage & Cost tab, and
> `/admin/settings` is AdminRoute-gated (admin role / superuser), so Managers can exercise this
> permission only via direct API calls today.

> **AI egress kill switch (`PUT /api/v1/companies/me/ai-egress`).** Enforced **in code** via
> `require_role([ADMIN])` — **Admin-only**, matching the sibling CUI egress kill switches
> (`allow_carrier_egress` / `allow_print_egress`, also Admin-only): flipping the CUI boundary is a
> decision reserved to Admins. It only ever mutates the caller's **own active company**
> (`get_current_company_id`; the company is never taken from the request body). Flipping the
> `Company.allow_ai_egress` CUI control writes tamper-evident `audit_log` rows (a field update **and**
> an `ai_egress_enabled` / `ai_egress_disabled` status change). The toggle is surfaced in the UI at
> **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`); within that tab the control is
> interactive for ADMIN (enabling egress requires explicit confirmation) and read-only for
> other roles. Since the Claude-plugin brief P0.3 (2026-11-13) the same switch also gates every
> **AI-external API token**. While it is off such a token is refused **403** on every API route
> and at the MCP door, so turning it on also opens an outside AI connector
> ([docs/EXPORT_CONTROL_AI_EGRESS.md](EXPORT_CONTROL_AI_EGRESS.md)). **No API token flips it**
> (lead decision 2026-11-15 — [Credential policy for judgment writes](#credential-policy-for-judgment-writes)): any token, whatever its role or scope, is refused **403**
> before anything is read (a read-only one by the read-only gate first: the `PUT` is a write, and
> Company Management is a denied tag), and the route is not an MCP tool. A flip's two audit rows
> are **required** evidence — if either cannot be written the flip rolls back and the answer is
> **503**. See
> [docs/API.md](API.md) → Company (self-service) and
> [docs/AI_QUOTING_AGENT_RUNBOOK.md](AI_QUOTING_AGENT_RUNBOOK.md).

> **SMS egress kill switch (`PUT /api/v1/companies/me/sms-egress`).** Enforced **in code** via
> `require_role([ADMIN])` — **Admin-only**, for the same reason as the AI switch above: it gates all
> outbound notification SMS to **Twilio, which sits outside the CUI boundary**, so flipping it is a
> CUI-boundary decision reserved to Admins, and to an Admin **signed in to the app**: any API token
> is refused **403** and the route is not an MCP tool (lead decision 2026-11-15 — [Credential policy for judgment writes](#credential-policy-for-judgment-writes)). It only
> ever mutates the caller's **own active company**
> (`get_current_company_id`; never taken from the request body). Flipping `Company.allow_sms_egress`
> writes tamper-evident `audit_log` rows **twice** — a field update **and** an
> `sms_egress_enabled` / `sms_egress_disabled` status change. Every company is created **OFF**, and
> the switch is re-resolved **fail-closed before every send** (unknown tenant, missing company row,
> or a DB error all deny), so turning it off also stops messages already queued in ARQ. Surfaced in
> the UI at **Admin Settings → SMS Privacy** (`/admin/settings?tab=smsprivacy`). Note this is only
> the *company* half of the gate — SMS additionally requires a **per-user** opt-in with a saved
> phone number (self-service, see Users above), and only `sms_eligible` catalog events are
> offerable. See [docs/API.md](API.md) → Company (self-service) and
> [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#sms-channel-twilio).

> **Wallboard display tokens (`/auth/display-token`, A0.5).** Issue / list / revoke / setup-code
> reissue (`POST /auth/display-token/{id}/setup-code`) are enforced
> **in code** via `require_role([ADMIN, MANAGER])` and tenant-scoped to the active company;
> issuance, revocation, setup-code reissue, and each successful TV claim write tamper-evident
> `audit_log` rows. The TV-side **claim** (`POST /auth/display-token/claim`) is the one **public**
> display-token endpoint (see the intentionally-unauthenticated set above): a single-use,
> 15-minute setup code — hashed at rest — is exchanged for the display JWT, re-minted from the
> `display_tokens` row so revocation semantics are unchanged. **A display token is not a role
> and carries no user identity** — it is a single-endpoint credential for an unattended TV. What it
> **can** do: authenticate the read-only `GET /shop-floor/wallboard` (via the dedicated
> `get_display_or_user` dependency), scoped to the issuing company (taken from the `display_tokens`
> DB row, never from the client). A per-display **`show_customer_names`** flag (Boolean, default
> `false`; migration `072`) additionally gates whether the board reveals work-order **customer
> names**; for **signed-in** callers of the same endpoint that content is gated by **role** —
> only **Platform Admin / Admin / Manager** see customer names, and every other role (Supervisor /
> Operator / Quality / Shipping / Viewer) plus every un-flagged display token gets the redacted,
> public-safe board (`docs/WALLBOARD.md` → Customer names — gated). What it **cannot** do: reach any other endpoint (`verify_token`
> accepts only `type == "access"` JWTs, so a display token gets **401** everywhere else), write
> anything (the wallboard endpoint performs zero writes), or outlive revocation/expiry (the DB row
> is re-checked on every request; a revoked token dies on the TV's next ~30s poll). As with AI
> usage above, the **Manager allowance is currently UI-dormant**: the managing surface is Admin
> Settings → Wallboard Displays and `/admin/settings` is AdminRoute-gated, so Managers can exercise
> it only via direct API calls today. See [docs/API.md](API.md) → Authentication → Display tokens
> and [docs/WALLBOARD.md](WALLBOARD.md).

> **API tokens (`/api-tokens`).** Issue / list / revoke are enforced **in code** via
> `require_role([ADMIN])` — Admin only, not Manager — and tenant-scoped to the active company
> (`app/api/endpoints/api_tokens.py`); issuance and revocation each write tamper-evident `audit_log`
> rows (an `api_token` row plus an `API_TOKEN_ISSUED` / `API_TOKEN_REVOKED` authentication event),
> metadata only, never the token. **An API token is not a role**: it is bound to **one user** and
> carries exactly that user's role — `require_role` decides on every route as it does for the SPA, a
> Manager's token gets a Manager's 403s, and the active company is pinned to the `api_tokens` row
> (never a JWT claim, never a platform admin's switched context). What it **cannot** do, whatever
> the holder's role: reach any route under `/auth` or `/api-tokens` (**403** `"API token cannot
> access this resource"` — no refresh, logout, company switch, display-token or kiosk-station verbs,
> and never mint, list or revoke tokens, which is an Admin's interactive act even when the token
> belongs to an Admin), or outlive revocation / expiry / user deactivation (the row is re-read on
> every request: revoked or expired → **401** on the next call; deactivating the holder through
> `DELETE /users/{id}` or `PUT /users/{id}` `is_active: false` **revokes** every token it holds in
> the same transaction — reason `user deactivated`, audited `API_TOKEN_REVOKED` — so reactivation
> restores the account and no token; a holder disabled some other way → **403**). That fence is an
> intent boundary, not a privilege boundary: an **Admin**-held token is still an Admin on `/users`,
> so it can create an interactive Admin, reset a password or reactivate a user — the bound user's
> role is the real boundary, so give a bot the narrowest role that does its job. **Never a platform
> principal**: a superuser / `platform_admin` bypasses every `require_role` gate and reaches
> `/platform/*` by explicit company id, so `POST /api-tokens/` refuses such a target (**409**,
> whoever the issuer is — the same line the tenant user verbs hold by refusing to *assign*
> `platform_admin`) and the row check refuses such a holder at use, so a later promotion stops a
> standing token rather than widening it. **Every audit row written under an API token carries
> `extra_data.credential`** (`kind: api_token`, `api_token_id`, `jti_prefix`, `label`), folded in by
> `AuditService`, so a token's writes are attributed to the bound user yet tellable from that
> person's own interactive actions and from each other's. Only Admins mint one, the target must be
> an active, non-platform user of the same company (404 / 409 otherwise), and the default lifetime
> is *never* — so give each agent a **dedicated account** at the role it needs (see
> [MCP / agent access](#mcp--agent-access)) rather than a person's login, and revoke with a reason
> when it is retired. **A token can be minted narrower than its user** (migration `119`): `scope`
> `read_only` limits it to `GET` / `HEAD` / `OPTIONS` outside the tags in
> `WERCO_MCP_READ_ONLY_DENY_TAGS`. `ai_external` (always read-only; the default for a read-only
> token) also makes it subject to the company's AI egress switch, a fixed read allowlist and
> export-control redaction. Both are fixed at mint and audited — see
> [MCP / agent access](#mcp--agent-access). Tokens are issued, listed and revoked in the app at
> **Admin → API Tokens** (`/admin/api-tokens`). That page is behind `AdminRoute` and an
> `adminOnly` nav entry, the same Admin / Platform Admin / superuser set `require_role([ADMIN])`
> admits. They can also be managed with `curl`, or with the `create_api_token` /
> `list_api_tokens` / `revoke_api_token` MCP tools and an interactive Admin JWT. See
> [docs/API.md](API.md) → Authentication → API tokens.

## Backend Implementation

### Using `require_role` Dependency

```python
from app.api.deps import require_role
from app.models.user import UserRole

@router.post("/work-orders")
def create_work_order(
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    # Only admin, manager, and supervisor can create work orders
    ...
```

### Available Roles

```python
class UserRole(str, enum.Enum):
    PLATFORM_ADMIN = "platform_admin"  # Werco oversight role — cross-company read access
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"
```

## Frontend Implementation

### Using Permission Components

```tsx
import { PermissionGate, CanCreate, CanEdit, CanDelete, AdminOnly } from './components/PermissionGate';

// Single permission check
<PermissionGate permission="work_orders:create">
  <CreateButton />
</PermissionGate>

// Any of multiple permissions
<PermissionGate anyOf={['work_orders:edit', 'work_orders:delete']}>
  <ActionMenu />
</PermissionGate>

// Convenience components
<CanCreate resource="work_orders">
  <CreateButton />
</CanCreate>

<AdminOnly>
  <AdminPanel />
</AdminOnly>
```

### Using Permission Hook

```tsx
import { usePermissions } from './hooks/usePermissions';

function MyComponent() {
  const { can, canAny, isAdmin, role } = usePermissions();
  
  if (can('work_orders:create')) {
    // Show create button
  }
  
  if (isAdmin) {
    // Show admin features
  }
}
```

### Protected Routes

```tsx
import { ProtectedRoute, AdminRoute } from './components/ProtectedRoute';

<Route path="/admin" element={
  <ProtectedRoute requireAdmin>
    <AdminPage />
  </ProtectedRoute>
} />

<Route path="/users" element={
  <ProtectedRoute permission="users:view">
    <UsersPage />
  </ProtectedRoute>
} />
```

## Superuser Override

Stored `is_superuser=true` and Platform Admin principals bypass role gates. They still obey
tenant context, credential scope, read-only-context restrictions, active-company requirements
(except the narrow HTTP recovery paths above), record-retention/finalization guards and required
audits. Tenant provisioning never grants `platform_admin` or superuser authority, even when the
caller is a platform principal.

The SPA mirrors this for `platform_admin`: `hasPermission` (`frontend/src/utils/permissions.ts`)
answers true for every permission and never applies a company's custom role-permission override to
it. Before this, `GET /admin/settings/role-permissions` (loaded at login) answered
`platform_admin: []`, because the backend's default role-permission table has no entry for it, and
read as an override that made every `can()` false for a password-login Platform Admin until the
next page reload. Superusers are handled in `usePermissions`, which returns true before consulting
any role.

## Adding New Permissions

1. **Backend**: Add new endpoint with `require_role()` dependency
2. **Frontend**: 
   - Add permission to `Permission` type in `utils/permissions.ts`
   - Add to appropriate role arrays in `ROLE_PERMISSIONS`
   - Use `PermissionGate` or `usePermissions` in components

## Security Notes

- Permissions are checked on BOTH frontend (UI) and backend (API)
- Frontend checks are for UX only - they can be bypassed
- Backend checks are the authoritative security layer
- Always verify permissions server-side before performing actions


### Recorded-piece planning snapshot

The pure `POST /inventory/stock-pieces/{piece_id}/observations/{number}/planning-snapshot`
resolver requires effective `inventory:view` and normal tenant/token/kiosk access.
It does not require Admin/Manager/Supervisor inventory mutation authority and makes
no observation, audit or inventory change. Existing read-only-company contexts still
refuse the POST under the central method fence; no read-only exemption is added.
No purchasing permission grants inventory access. The saved-draft, saved-run and
original-DXF routes that once carried the selected piece onward (with their own
`inventory:view` gates) were retired with every `/quote-nesting/*` route in `3e17456e`
(2026-09-16); this resolver is the only remaining recorded-piece planning route.
