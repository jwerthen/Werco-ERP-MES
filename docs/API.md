# Werco ERP API Documentation

This is a high-level overview of the Werco ERP API. For interactive documentation, visit `/api/docs` when the backend is running.

## Base URL

- Development: `http://localhost:8000/api/v1`
- Production: `https://werco-erp.yourdomain.com/api/v1`

## Authentication

Most endpoints require authentication using JWT tokens.

### Login

```http
POST /auth/login
Content-Type: application/json

{
  "email": "user@werco.com",
  "password": "password"
}
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### Using the Token

Include the token in the Authorization header:
```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Display tokens (TV wallboards)

Scoped, revocable credentials for unattended shop-floor TVs (A0.5). A display token is a
long-lived JWT with `type="display"` that authenticates **only** `GET /shop-floor/wallboard`
(see Shop Floor below) — every other endpoint rejects it with **401**.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/auth/display-token` | Issue a display token. Body: `{"label", "expires_days"}` (label 1–100 chars; lifetime default **90** days, capped at **365**) | Admin / Manager |
| GET | `/auth/display-token` | List this company's display tokens (metadata only — the JWTs are never returned) | Admin / Manager |
| DELETE | `/auth/display-token/{id}` | Revoke a display token (status flip, idempotent; cross-tenant id → 404) | Admin / Manager |

> **One-time reveal.** The raw JWT is returned exactly **once** — the `token` field on the POST
> response. It is never stored server-side (only its `jti` lands in the `display_tokens` row) and
> never appears in the list response, so a lost token cannot be recovered — revoke it and issue a
> new one.
>
> **Revocation is DB-authoritative.** `DELETE` flips the row's `revoked` flag (the row is kept as
> the issuance record, not deleted). Issuance and revocation both write tamper-evident `audit_log`
> rows. The wallboard auth dependency re-checks the `display_tokens` row (exists / not revoked /
> not past its DB `expires_at`) on **every** request, so a revoked or expired token stops working
> on the TV's next poll (~30s) even though the JWT itself is still signature-valid.

## Core Endpoints

### Work Orders

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/work-orders/` | List all work orders | Yes |
| POST | `/work-orders/` | Create work order | Yes |
| GET | `/work-orders/{id}` | Get work order by ID | Yes |
| PUT | `/work-orders/{id}` | Update work order (409 if it moves a terminal WO back to a non-terminal status) | Yes |
| DELETE | `/work-orders/{id}` | Delete work order | Admin |
| POST | `/work-orders/{id}/release` | Release to production | Yes |
| POST | `/work-orders/{id}/start` | Start production | Yes |
| POST | `/work-orders/{id}/complete` | Complete work order (409 if the WO is CANCELLED) | Yes |
| POST | `/work-orders/{id}/operations` | Add an operation to a work order | Admin / Manager / Supervisor |
| PUT | `/work-orders/operations/{id}` | Update an operation | Yes |
| POST | `/work-orders/operations/{id}/start` | Start an operation | Yes |
| POST | `/work-orders/operations/{id}/complete` | Complete an operation (or record partial progress; 409 if the parent WO is terminal) | Yes |

> **Tenant isolation on operation/completion endpoints.** The operation- and completion-level
> endpoints above (`/start`, `/complete`, `/operations/{id}`, `/operations/{id}/start`,
> `/operations/{id}/complete`, `/operations`) and their shop-floor counterparts (see below) scope
> every work-order / operation lookup to the caller's **active company** (`get_current_company_id`).
> An id belonging to another tenant returns **404 before any mutation** (not 403, so a guessed id
> can't be used to drive another company's operation or work order). State transitions on these
> paths — operation/WO **start** and **complete**, manual `/work-orders/{id}/complete` (status +
> the quantities it sets), and shipment-close — are recorded in the tamper-evident audit trail
> (`GET /audit/`) in addition to the existing real-time operational events.
>
> **Concurrency on completion endpoints.** Operation/work-order **start** and **complete**
> (`/operations/{id}/start`, `/operations/{id}/complete`, `/operations/{id}` update, and
> `/work-orders/{id}/complete`) now enforce optimistic locking on the underlying operation / work
> order row. A concurrent stale update returns **409 Conflict**
> (`{"detail": "This … was modified concurrently. Refresh and retry…"}`) instead of silently losing
> the update; the client should re-fetch and retry. The server also takes a row lock
> (`SELECT … FOR UPDATE`) around the over-completion check so two simultaneous completions cannot
> double-count quantity.
>
> **Completion contract (shared finalizer).** Operation completion rolls up into the work order
> through one shared finalizer, so all completion paths behave identically. On the absolute
> completion verbs (`/operations/{id}/complete`, both here and on the shop floor) the stored
> `quantity_complete` is `clamp(max(existing, requested, recorded production evidence), 0, target)`:
> it never drops below the value already recorded or below durable production evidence, and never
> exceeds the operation target. The work order's `quantity_complete` only ever moves forward. Scrap
> is **opt-in on update**: `quantity_scrapped` is optional on both `/work-orders/{id}/complete` and
> `/work-orders/operations/{id}/complete` — omit it to leave previously-recorded scrap untouched;
> send an explicit value (including `0`) to overwrite it. Completing an **on-hold** operation is
> rejected with **409 Conflict** (`{"detail": "Operation is on hold and cannot be completed"}`);
> `/work-orders/{id}/complete` likewise returns **409** if any open operation is on hold
> (`"…is on hold; resolve the hold first"`) — resolve the hold before completing. A work order that
> reaches `complete` always carries both an `actual_start` and an `actual_end`. Successful completion
> responses carry a `quality_exceptions` array (default `[]`) listing any unsatisfied **quality gates**
> — see "Quality gates on completion are warn-and-record" under Shop Floor; these warn, they do **not**
> block the completion.
>
> **Completion signals.** When a work order reaches **COMPLETE** (operation/WO completion paths) or
> **CLOSED** (shipment close), the system fires a uniform signal set: an internal `WO_COMPLETED`
> notification to the tenant's recipients (supervisors, managers, and the WO creator) and an outbound
> `work_order.completed` / `work_order.closed` **webhook** to the company's registered endpoints — see
> [Webhooks](#webhooks). Both are dispatched asynchronously **after commit** and best-effort: a signal
> failure never fails the completion, and nothing fires for a rolled-back completion.
>
> **Parent/child laser-nest completion rollup (G1).** When the **last** laser-cutting child work order
> (`WorkOrderType.LASER_CUTTING`, linked by `parent_work_order_id`) of a parent reaches a terminal
> status, the system records a `child_work_orders_complete` operational event **and** a tamper-evident
> `audit_log` row (action **`CHILD_WORK_ORDERS_COMPLETE`**) attributed to the parent. This is a
> **signal only** — it does **not** auto-complete the parent or mutate its route (parent and child WOs
> are not operation-coupled); it surfaces "all children done, ready to advance" so a human completes
> the parent. It fires from every completion path including reconcile-on-read (tagged
> `source = "reconcile_on_read"` there) and is tenant-scoped and best-effort. **No API request/response
> shape change.**
>
> **Idempotent completion.** `/work-orders/{id}/complete` (and shipment `/{id}/ship`) are idempotent:
> re-invoking on an already-terminal work order/shipment returns the current state
> (`{"already_completed": true}` / `{"already_shipped": true}`) and fires no second audit row, event,
> notification, or webhook.
>
> **Terminal-state lock (a finished/cancelled WO can't be resurrected).** The terminal statuses are
> **COMPLETE**, **CLOSED**, and **CANCELLED**. The idempotent no-op above applies only to a WO that has
> already completed (COMPLETE/CLOSED); a **CANCELLED** WO was deliberately taken out of production and is
> not silently completed:
> - `POST /work-orders/{id}/complete` on a **CANCELLED** WO returns **409 Conflict**
>   (`{"detail": "cannot complete a cancelled work order"}`).
> - `POST /work-orders/operations/{id}/complete` (and the shop-floor equivalent) against an operation
>   whose parent WO is in **any** terminal status returns **409 Conflict**
>   (`{"detail": "cannot complete operation: work order is <status>"}`) before any mutation — so
>   finalizing the last operation of a cancelled/closed WO can't drive it to COMPLETE.
> - `PUT /work-orders/{id}` that moves a **terminal** WO back to a **non-terminal** status returns
>   **409 Conflict** (`{"detail": "cannot move work order out of terminal status '<current>' to '<target>'"}`).
>   (This is a targeted guard on the one dangerous transition, not a full state machine.)
> - **Reconcile-on-read leaves terminal WOs untouched** — operation evidence read on any GET will not
>   reopen a terminal WO to IN_PROGRESS or resurrect a CANCELLED WO to COMPLETE.
>
> Resurrecting a terminal WO would re-fire finished-goods receipt / backflush / cost rollup and write a
> spurious COMPLETE row onto the tamper-evident audit chain; the lock prevents that.
>
> **Completion writes finished goods to inventory.** When a work order reaches **COMPLETE** (any
> completion path, including reconcile-on-read), the system **always** performs a finished-goods
> RECEIVE: it assigns the work order a lot number if it has none (`LOT-<work_order_number>`),
> creates or increments an inventory item for the work order's part at warehouse **`MAIN`** /
> location **`FINISHED-GOODS`** for the completed quantity, and writes a positive `RECEIVE`
> `InventoryTransaction` (`reference_type='work_order'`) at the part's `standard_cost`. The receipt is
> **audited** (`GET /audit/`) and **idempotent** — at most one finished-goods receipt per work order
> (DB-enforced), so a re-completion or a reconcile re-read never double-receives. Receipts are lot-only
> (no serial is assigned; the system has no part-serialization flag yet). A fully-scrapped work order
> (zero completed quantity) receives nothing. The receipt's lot is reconstructable end-to-end via
> [Traceability](#traceability).
>
> **Component backflush is opt-in per part (default off).** If the finished part has
> `backflush_components = true` (see [Part Schema](#part-schema)), completion **auto-consumes** the
> part's BOM components: one negative `ISSUE` `InventoryTransaction` per component (quantity scaled by
> the produced quantity and each BOM item's `scrap_factor`), decrementing source stock and carrying the
> consumed lot for genealogy — each **audited** and **idempotent** per component. When the flag is
> **false** (the default) completion moves no components, so a shop that issues material manually is
> never double-consumed. A backflush shortage (insufficient stock) **does not fail the completion** —
> the source lot is driven negative and the shortfall is recorded as a tamper-evident
> `BACKFLUSH_SHORTAGE` audit row plus a `backflush_shortage` warning event.
>
> **Labor-hour + cost rollup on completion is opt-in (global flag `LABOR_COST_ROLLUP_ENABLED`,
> default OFF).** When the flag is **on**, a work order reaching **COMPLETE** (any path, including
> reconcile-on-read) rolls op/WO `actual_hours` monotonic-up from time-entry evidence, computes
> `actual_cost` = **labor + issued material + overhead** (labor at `WorkCenter.hourly_rate`, falling
> back to `DEFAULT_LABOR_RATE`; overhead at `DEFAULT_OVERHEAD_RATE` — see
> [Environment Variables](ENVIRONMENT_VARIABLES.md)), syncs any linked `JobCost` to status `COMPLETED`,
> and writes one **audited** rollup row — all atomic with the completion, best-effort (a cost-side
> error never fails the completion). Hours sum across **all operators'** time entries on an operation
> (multiple operators are summed, not deduped). When the flag is **off** (the default), completion does
> **not** auto-populate `actual_cost` / `actual_hours` and touches no `JobCost`; the on-demand
> `POST /job-costs/{id}/calculate` is then the only way to materialize cost actuals. The
> `no_labor_recorded` quality exception (above) fires regardless of this flag.

#### Work Order Schema

```json
{
  "id": 1,
  "number": "WO-10001",
  "customer_name": "Acme Corp",
  "part_id": 123,
  "quantity": 100,
  "status": "planned",
  "priority": 2,
  "due_date": "2024-01-31",
  "created_at": "2024-01-01T10:00:00",
  "updated_at": "2024-01-01T10:00:00"
}
```

### Parts

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/parts/` | List all parts | Yes |
| POST | `/parts/` | Create part | Yes |
| GET | `/parts/{id}` | Get part by ID | Yes |
| PUT | `/parts/{id}` | Update part | Yes |
| DELETE | `/parts/{id}` | Delete part | Admin |
| GET | `/parts/{id}/bom` | Get BOM for part | Yes |

#### Part Schema

```json
{
  "id": 123,
  "number": "P-10001",
  "name": "Shaft Assembly",
  "description": "Main drive shaft assembly",
  "type": "manufactured",
  "unit_of_measure": "EA",
  "material_type": "ST-304",
  "is_active": true,
  "backflush_components": false,
  "created_at": "2024-01-01T10:00:00"
}
```

> `backflush_components` (boolean, default `false`) opts this part into **component backflush on
> work-order completion**: when a work order for this part completes, its BOM components are
> auto-consumed via negative `ISSUE` inventory transactions. Leave it `false` (the default) when
> material is issued manually, to avoid double-consuming stock. See the completion-inventory notes
> under [Work Orders](#work-orders).

### BOM (Bill of Materials)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/bom/` | List all BOMs | Yes |
| POST | `/bom/` | Create BOM | Yes |
| GET | `/bom/{id}` | Get BOM by ID | Yes |
| PUT | `/bom/{id}` | Update BOM | Yes |
| DELETE | `/bom/{id}` | Delete BOM | Admin |

#### BOM Item Schema

```json
{
  "id": 1,
  "bom_id": 10,
  "part_id": 123,
  "quantity": 2.0,
  "position": 1,
  "is_optional": false
}
```

### Work Centers

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/work-centers/` | List all work centers | Yes |
| POST | `/work-centers/` | Create work center | Yes |
| GET | `/work-centers/{id}` | Get work center by ID | Yes |
| PUT | `/work-centers/{id}` | Update work center | Yes |
| DELETE | `/work-centers/{id}` | Delete work center | Admin |

#### Work Center Schema

```json
{
  "id": 1,
  "name": "CNC Mill 1",
  "code": "CNC-001",
  "type": "cnc",
  "description": "Haas VF-3 CNC Milling Machine",
  "hourly_rate": 120.00,
  "is_active": true
}
```

### Routing

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/routing/` | List all routings | Yes |
| POST | `/routing/` | Create routing | Yes |
| GET | `/routing/{id}` | Get routing by ID | Yes |
| PUT | `/routing/{id}` | Update routing | Yes |

#### Routing Operation Schema

```json
{
  "id": 1,
  "routing_id": 10,
  "sequence": 10,
  "operation_code": "MILL-100",
  "description": "Rough mill to blueprint",
  "work_center_id": 1,
  "setup_time": 0.5,
  "run_time": 2.5,
  "notes": "Use roughing tool"
}
```

### Shop Floor

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/shop-floor/dashboard` | Shop floor dashboard | Yes |
| GET | `/shop-floor/my-active-job` | Get current user's active job | Yes |
| POST | `/shop-floor/clock-in` | Clock in to operation | Yes |
| POST | `/shop-floor/clock-out/{id}` | Clock out with production data | Yes |
| POST | `/shop-floor/operations/{id}/start` | Start an operation | Yes |
| POST | `/shop-floor/operations/{id}/production` | Add produced/scrapped quantity while staying clocked in | Yes |
| POST | `/shop-floor/operations/{id}/complete` | Complete / report progress on an operation | Yes |
| PUT | `/shop-floor/operations/{id}/hold` | Put an operation on hold (closes open time entries; body optional — category/severity/note file a structured blocker) | Yes |
| POST | `/shop-floor/operations/{id}/inspection` | Record operation inspection complete (sets `inspection_complete`) | Admin / Manager / Supervisor / Quality |
| POST | `/shop-floor/time-entries/{id}/approve` | Approve a TimeEntry (sets `approved` / `approved_by`) | Admin / Manager / Supervisor / Quality |
| POST | `/shop-floor/time-entries/{id}/unapprove` | Clear approval on a TimeEntry | Admin / Manager / Supervisor / Quality |
| GET | `/shop-floor/work-center-queue/{id}` | Get work center queue | Yes |
| GET | `/shop-floor/wallboard` | Read-only TV wallboard snapshot (`?dept=` narrows to one work-center type, case-insensitive) | User **or** display token |

> **Wallboard display-token threat model (A0.5).** `GET /shop-floor/wallboard` is the **only**
> endpoint a display token can reach — it is guarded by `get_display_or_user`, the sole dependency
> that honors `type="display"` JWTs; every other endpoint authenticates through `verify_token`,
> whose `type == "access"` check rejects display (and refresh) tokens with **401**. On every
> request the dependency re-checks the `display_tokens` DB row — existence, `revoked` flag, DB
> `expires_at`, and that the JWT's `cid` claim matches the row's `company_id` — and tenant scope
> comes from the **DB row, never client input**, so revocation/expiry hold for already-minted JWTs
> and a forged claim cannot widen scope. The endpoint is a **zero-write read**: deliberately no
> reconcile-on-read, no audit rows, no events — an unattended TV polling every 30s must never
> mutate state, and a display token has no user identity to attribute writes to. Operator names in
> the payload are truncated to "First L." (`operator_name`) because the board renders on a public
> screen. Signed-in users can call it too (their active company scopes the data). Payload:
> `work_centers[]` (`{code, name, status, active_jobs[], queued_count, blocked_count, down}`, each
> active job `{wo_number, part_number, op_name, operator_name, elapsed_minutes, qty_done,
> qty_target}`), `late_wos[]`, `blocked_wos[]` (tickers capped at 25), `generated_at`. Token
> issuance/revocation: see Authentication → Display tokens. Operating a TV: see
> [docs/WALLBOARD.md](WALLBOARD.md).

> **Tenant isolation on clock/operation endpoints.** Clock-in, clock-out, and the shop-floor
> operation start/complete endpoints scope every operation, work-order, and `TimeEntry` lookup to
> the caller's **active company** (`get_current_company_id`). A `time_entry_id` / `operation_id`
> belonging to another tenant returns **404 before any mutation** — a guessed foreign id can no
> longer drive another company's operation or work order to IN_PROGRESS / COMPLETE. When a
> clock-out (or an operation/WO start or completion) flips an operation or work order to a terminal
> status, that transition is written to the tamper-evident audit trail (`GET /audit/`) as well as
> the existing real-time operational event.
>
> **Concurrency on clock/completion endpoints.** Clock-out, production, and operation start/complete
> (`/clock-out/{id}`, `/operations/{id}/production`, `/operations/{id}/start`,
> `/operations/{id}/complete`) take a row lock (`SELECT … FOR UPDATE`) around the over-completion
> read-modify-write and enforce optimistic locking on the operation / time-entry row. A concurrent
> stale update returns **409 Conflict** ("This … was modified concurrently. Refresh and retry…")
> rather than losing the update.
>
> **Duplicate open clock-in is DB-enforced.** `/clock-in` (and operation `/start`, which opens a
> time entry) is backed by a partial unique index
> (`uq_open_time_entry ON time_entries(user_id, operation_id) WHERE clock_out IS NULL`): at most one
> open time entry can exist per user + operation. A racing double clock-in is rejected with
> **400 Bad Request** (`"You are already clocked in to this operation."`) instead of creating a
> second open entry that would double-count production.
>
> **Adoption-telemetry `source` channel (A0.1).** `POST /shop-floor/clock-in`,
> `POST /shop-floor/clock-out/{id}`, `POST /shop-floor/operations/{id}/production`,
> `POST /shop-floor/operations/{id}/complete`, and `PUT /shop-floor/operations/{id}/hold` (as of
> A0.3) accept an **optional** `source` field naming the client
> channel that produced the write: `kiosk` | `desktop` | `scanner` | `import` | `backfill` (any other
> value is a **422**). It is persisted on the time entry (`time_entries.source`, nullable; migration
> `048_time_entry_source`; returned as `source` on `TimeEntryResponse`) for adoption analytics during
> the paper-to-digital transition (clock-in coverage, digital completion %, backfill rate). Semantics:
> **omitted → stored `NULL`** — the server never guesses a channel; `NULL` means unknown/legacy (all
> pre-A0.1 rows, and entries opened by `/operations/{id}/start`, which takes no `source`, until a later
> write reports one). A clock-out without `source` keeps the channel recorded at clock-in.
> `/operations/{id}/complete` only **fills** `source` on the open entries it auto-closes when an entry
> has none — it never overwrites another operator's recorded channel. `/operations/{id}/hold` follows
> the **same fill-only-if-NULL contract** as `/complete`: a hold auto-closes every open time entry on
> the operation (which may belong to other operators), and the hold's `source` only fills a missing
> channel on those entries — it is never used to overwrite a channel recorded at clock-in. The channel
> also rides on the corresponding real-time events: the `labor_clock_in`, `labor_clock_out`,
> `operation_completed`, and `work_order_completed` `OperationalEvent` payloads carry a `source` key
> (`null` when not reported — e.g. office-endpoint or reconcile-on-read completions, which take no
> `source` input), and so do the hold-path events: `operation_hold` (emitted when the hold carries no
> blocker data) and `work_order_blocker_created` (emitted when the hold files a structured blocker).
>
> **Structured scrap reason on in-shift production reports (A0.3).**
> `POST /shop-floor/operations/{id}/production` accepts an **optional** `scrap_reason` string — the
> same shape and destination as the existing clock-out field (the `TimeEntry.scrap_reason` column,
> 255 max), persisted onto the caller's **active** time entry. It is stored only when the report
> actually carries scrap (`quantity_scrapped_delta > 0`); an omitted/`null` reason never clobbers a
> reason recorded by an earlier in-shift report. When stored, the reason is also appended to the
> tamper-evident `REPORT_OPERATION_PRODUCTION` audit description.
>
> **Completion contract.** The shop-floor `/operations/{id}/complete` shares the same finalizer as
> the office endpoint (see "Completion contract" under Work Orders): the absolute verb stores
> `clamp(max(existing, requested, recorded production evidence), 0, target)`; the additive verbs
> (`/clock-out/{id}`, `/operations/{id}/production`) add a delta floored at the same evidence and
> capped at the target. Completing an **on-hold** operation is rejected with **409 Conflict**
> (`{"detail": "Operation is on hold and cannot be completed"}`).
>
> **Reconcile-on-read is audited.** When a read endpoint (e.g. `/shop-floor/dashboard`, the operation
> list, or a work-order detail) drives an operation or work order to `complete` from durable time-entry
> evidence, that status change is now written to the tamper-evident audit trail (`GET /audit/`),
> attributed to the requesting user and tagged `source = "reconcile_on_read"`. This reconcile is
> best-effort: if its write fails it is rolled back silently and the read still returns **200**.
>
> **`/shop-floor/dashboard` caching + bounded reconcile.** The dashboard supports conditional requests:
> send the previous response's `ETag` back as `If-None-Match` to get a **304 Not Modified** (and no
> body) when nothing changed. The `ETag` is a cheap state fingerprint computed **before** the reconcile,
> so an unchanged dashboard 304s without running the reconcile or building the payload. The dashboard's
> reconcile scan is bounded to the most-recently-touched `SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT` open
> work orders (default 250; see `docs/ENVIRONMENT_VARIABLES.md`) — any WO beyond the cap is still
> reconciled when opened in its own detail / operations-list view.

> **Quality gates on completion are warn-and-record, not blocking.** Completing an operation or work
> order while a quality gate is unsatisfied still **succeeds (200)** — the gates do not block. Instead,
> the completion response carries a `quality_exceptions` array describing each unsatisfied gate, and
> the system records a tamper-evident `audit_log` row (action `COMPLETED_WITH_QUALITY_EXCEPTION`) plus
> a warning operational event for each. The gates are: `inspection_incomplete` (operation requires
> inspection but `inspection_complete` is not set), `open_ncr` (an unresolved NCR on the work order),
> `fai_not_passed` (a First Article Inspection on the work order that is not `PASSED`), `open_blocker`
> (an open/acknowledged work-order blocker), and `no_labor_recorded` (severity `medium`: a work order
> completed with one or more operations that recorded **zero** labor — no time entry, or only
> zero-duration entries — so its cost/hour actuals may be understated; helps surface missed clock-ins),
> and `child_work_orders_incomplete` (severity `high`, **G1**: a parent work order completed while one
> or more of its **laser-cutting** child work orders — linked by `parent_work_order_id`,
> `WorkOrderType.LASER_CUTTING` — were still non-terminal; the parent **still completes**, it does not
> block. A CANCELLED child counts as resolved, not a blocker. The exception lists the offending child
> WO numbers). The `no_labor_recorded` signal fires **regardless of the `LABOR_COST_ROLLUP_ENABLED`
> flag** (it is a process/operator-accuracy signal, not a cost figure). This applies to both
> `/work-orders/operations/{id}/complete` and `/shop-floor/operations/{id}/complete`,
> `/work-orders/{id}/complete`, and `/shop-floor/clock-out/{id}` when it completes an operation or work
> order (the field rides on that endpoint's `TimeEntryResponse`). Each entry is
> `{ "code", "message", "reference_type", "reference_id", "severity" }`; the field defaults to `[]`, so
> an all-clear completion is shape-compatible with the pre-existing response.
>
> _Limitation:_ on the **reconcile-on-read** path only `inspection_incomplete` is recorded (the
> NCR/FAI/blocker gates are evaluated on the next live completion). And `fai_not_passed` only fires
> when an FAI **exists** and is not passed — a required-but-missing FAI is not detectable (no
> "FAI required" flag in the data model).

> **Operator-qualification gate is warn-and-record, not blocking (G5-B).** `POST /shop-floor/clock-in`
> and `POST /shop-floor/operations/{id}/start` evaluate the operator against the operation's work
> center and **record** (never block) any unsatisfied qualification gate — the clock-in / start still
> **succeeds** and is open to **any authenticated user** (these are operator-facing; the gate only
> records). Each unsatisfied gate writes a tamper-evident `audit_log` row (action
> **`OPERATOR_QUALIFICATION_EXCEPTION`**) plus a warning operational event, and is surfaced on a
> `qualification_exceptions` array on the response — on the clock-in `TimeEntryResponse` and on the
> start-operation response body. The gates are:
> - `operator_not_skill_qualified` (severity `medium`): no active `SkillMatrix` entry at
>   `skill_level >= 2` ("Basic", a module constant `MIN_SKILL_LEVEL`) for the operation's work center.
> - `operator_certification_missing_or_expired` (severity `high`): where the work center declares a
>   `required_certification_type`, the operator holds no current (active / expiring-soon)
>   `OperatorCertification` of that type. When the work center has no required cert type (the common
>   case) this leg is skipped.
>
> Each entry is `{ "code", "message", "reference_type", "reference_id", "severity" }`; the field
> defaults to `[]`, so an all-clear clock-in / start is shape-compatible with the pre-G5-B response.
> The gate is **tenant-scoped** — every skill/cert/work-center lookup filters the active company.
>
> **Operator-certifications router is fully tenant-scoped (as of 2026-06-09).** Beyond the gate above,
> the operator-certifications read/by-id endpoints now filter the active `company_id`:
> - **Skill matrix:** the read endpoints under `GET /operator-certifications/skill-matrix/…` —
>   `check/{user_id}/{work_center_id}`, `user/{user_id}`, `work-center/{work_center_id}`, and the list —
>   the `POST .../skill-matrix/` writer, and `PUT .../skill-matrix/{entry_id}` (`update_skill_entry`)
>   all filter `SkillMatrix.company_id`. The model's unique constraint is now tenant-qualified too —
>   `(company_id, user_id, work_center_id)` via migration `045_skillmatrix_company_unique`.
> - **Certifications / training:** `GET /operator-certifications/certifications/dashboard` (its cert
>   counts, compliance rate, operators-with/without-certs — `User` now `company_id`-scoped — and
>   training-hours-this-month aggregates), `GET .../certifications/expiring`,
>   `GET .../certifications/user/{user_id}`, `GET .../certifications/{cert_id}`,
>   `GET .../training/user/{user_id}`, and `PUT .../training/{training_id}` (`update_training`) all
>   filter the active company; a cross-tenant id now returns **404** before any read/mutation.
>
> These remain open to **any authenticated user** — the tenant-scoping fix added company scoping, not an RBAC change.
>
> **Operator-certifications WRITE endpoints are now role-gated, audited, and FK-validated (2026-06-09).**
> The seven write endpoints on this router are no longer open to any authenticated user (they had no
> RBAC rows before):
> - **Certifications + training:** `POST/PUT/DELETE /operator-certifications/certifications/{…}` and
>   `POST/PUT /operator-certifications/training/{…}` → `require_role([ADMIN, MANAGER, QUALITY])`.
> - **Skill matrix:** `POST /operator-certifications/skill-matrix/` and
>   `PUT /operator-certifications/skill-matrix/{entry_id}` → `require_role([ADMIN, MANAGER, SUPERVISOR])`.
>
> Any other authenticated role gets **403**. Each write writes a tamper-evident `audit_log` row
> (resource types `operator_certification` / `training_record` / `skill_matrix`; create/update/delete —
> `GET /audit/`). On the create endpoints (and `update_training`'s re-pointed `work_center_id`), a
> `user_id` / `work_center_id` that does not belong to the active company is rejected with **422**
> (`"… does not reference a … in your company"`) before insert — a cross-tenant FK-injection guard. The
> read endpoints listed above are unchanged (any authenticated user, tenant-scoped). See
> `docs/RBAC_PERMISSIONS.md` → Operator Certifications & Training / Skill Matrix.

#### Inspection Schema

`POST /shop-floor/operations/{id}/inspection` records an operation's inspection as complete. It sets
`inspection_complete = True` (clearing the `inspection_incomplete` gate above), records who/when in a
tamper-evident audit row, and is **tenant-scoped** + role-gated to **Admin / Manager / Supervisor /
Quality** (there is no separate Inspector role). Both fields are optional:

```json
{
  "inspection_type": "final",
  "notes": "All critical characteristics within tolerance"
}
```

#### Time-entry approval

`POST /shop-floor/time-entries/{id}/approve` and `POST /shop-floor/time-entries/{id}/unapprove`
let a supervisor sign off on shop-floor labor (G5-A). Approve sets `approved` (timestamp) +
`approved_by` (the approver); unapprove clears both. Both:

> - are **role-gated to Admin / Manager / Supervisor / Quality** — any other role is **403**;
> - **forbid self-approval**: a user cannot approve or unapprove their **own** TimeEntry (segregation
>   of duties for the labor-cost gate) — **403** (`"You cannot approve or unapprove your own time
>   entry"`), even if the caller holds an approver role;
> - are **tenant-scoped**: an id belonging to another company returns **404** before any mutation;
> - are **idempotent** (approving an already-approved entry, or unapproving an already-unapproved one,
>   is a no-op that returns the current state with **no second audit row**);
> - respect the TimeEntry's optimistic-lock `version` column — a concurrent stale write returns
>   **409 Conflict** (`"This time entry was modified concurrently. Refresh and retry."`);
> - write **one** tamper-evident `audit_log` row (action `time_entry_approve` / `time_entry_unapprove`).
>
> Both return the updated `TimeEntryResponse` (now carrying `approved` / `approved_by`; these also
> surface on `GET /shop-floor/my-active-job`). Approval is what the opt-in
> `REQUIRE_APPROVED_LABOR_FOR_COST` flag keys on: when that flag is **on**, only approved TimeEntries
> feed the labor-cost legs (job costing, completion cost rollup, and the analytics OEE/labor leg).
> When the flag is **off** (the default), approval is recorded but does not affect costing. See
> `docs/ENVIRONMENT_VARIABLES.md`.

#### Clock Out Schema

```json
{
  "time_entry_id": 1234,
  "quantity_completed": 50,
  "quantity_rejected": 2,
  "scrap_reason": "Drill bit broke",
  "notes": "Replaced drill bit, resumed operation"
}
```

### Quality

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/quality/inspections/` | List inspections | Yes |
| POST | `/quality/inspections/` | Create inspection | Yes |
| GET | `/quality/inspections/{id}` | Get inspection by ID | Yes |
| POST | `/quality/inspections/{id}/approve` | Approve inspection | Quality |

### QMS Standards & Audit Readiness

Standards/clause/evidence management for AS9100D, ISO 9001, CMMC and similar quality systems, all
under `/qms-standards`. Every endpoint is **tenant-scoped to the caller's active company**
(`get_current_company_id`). Reads (list / get / detail) are available to **any authenticated user**
in the tenant, while writes are **role-gated** — the read-broad / write-restricted model documented
in `RBAC_PERMISSIONS.md`.

**Standards**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/` | List standards with compliance-summary counts (`active_only` filter) | Yes |
| POST | `/qms-standards/` | Create standard | Admin / Manager / Quality |
| POST | `/qms-standards/{standard_id}/upload-pdf` | AI clause extraction from an uploaded PDF | Admin / Manager / Quality |
| GET | `/qms-standards/audit-readiness` | Audit-readiness dashboard summary across active standards | Yes |
| GET | `/qms-standards/{standard_id}` | Get standard with all clauses and evidence | Yes |
| PUT | `/qms-standards/{standard_id}` | Update standard | Admin / Manager / Quality |
| DELETE | `/qms-standards/{standard_id}` | Delete standard and all its clauses/evidence | Admin |

**Clauses**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/{standard_id}/clauses` | List clauses for a standard (flat list) | Yes |
| POST | `/qms-standards/{standard_id}/clauses` | Add a clause | Admin / Manager / Quality |
| POST | `/qms-standards/{standard_id}/clauses/bulk` | Bulk-import clauses (e.g. from a parsed document) | Admin / Manager / Quality |
| PUT | `/qms-standards/clauses/{clause_id}` | Update clause, incl. compliance-status assessment | Admin / Manager / Quality |
| DELETE | `/qms-standards/clauses/{clause_id}` | Delete a clause and its evidence links | Admin / Manager |

**Auto-evidence discovery**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/clauses/{clause_id}/auto-evidence` | Discover live ERP/MES evidence for a single clause (read-only, nothing persisted) | Yes |
| POST | `/qms-standards/{standard_id}/auto-link` | Auto-discover and persist evidence links for all clauses in a standard | Admin / Manager / Quality |

**Evidence links**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/qms-standards/clauses/{clause_id}/evidence` | Link evidence to a clause | Admin / Manager / Quality |
| PUT | `/qms-standards/evidence/{evidence_id}` | Update evidence, incl. verification | Admin / Manager / Quality |
| DELETE | `/qms-standards/evidence/{evidence_id}` | Remove an evidence link | Admin / Manager / Quality |

> **PDF clause extraction:** `POST /qms-standards/{standard_id}/upload-pdf` requires a text-based
> PDF (≤ 20 MB; scanned/image-only PDFs are rejected) and a configured `ANTHROPIC_API_KEY` — it
> returns **500** if the key is missing. Claude extracts the numbered clauses and persists them
> against the standard.

> **Deletes are soft (records retained):** the three `DELETE` endpoints above return **204** but
> do not physically remove rows — the standard / clause / evidence is marked deleted and disappears
> from all reads (including the nested clauses/evidence on `GET /qms-standards/{standard_id}`), while
> the record is retained for AS9100D traceability. All QMS create / update / delete operations — plus
> a status-change entry when a clause's `compliance_status` changes — are captured in the tamper-evident
> audit trail (`GET /api/v1/audit/`).

#### Audit-Readiness Summary Schema (`GET /qms-standards/audit-readiness`)

```json
{
  "total_standards": 2,
  "total_clauses": 142,
  "compliant": 120,
  "partial": 8,
  "non_compliant": 3,
  "not_assessed": 9,
  "not_applicable": 2,
  "compliance_percentage": 85.7,
  "total_evidence_links": 310,
  "verified_evidence": 240,
  "unverified_evidence": 70,
  "clauses_needing_review": 4
}
```

#### Clause Auto-Evidence Schema (`GET /qms-standards/clauses/{clause_id}/auto-evidence`)

```json
{
  "clause_id": 42,
  "clause_number": "8.5.2",
  "discovered_evidence": [
    {
      "evidence_type": "ncr",
      "title": "Non-Conformance Reports (NCR)",
      "description": "12 NCRs processed in last 12 months, 2 currently open",
      "module_reference": "/quality/ncr",
      "total_count": 12,
      "recent_count": 7,
      "health_status": "healthy",
      "health_detail": "All NCRs resolved within SLA",
      "examples": [],
      "suggested_compliance": "compliant"
    }
  ],
  "overall_suggested_compliance": "compliant"
}
```

### Engineering Change Orders (ECO)

Engineering-change endpoints are mounted under `/eco`; the router's own routes are also `/eco/…`, so
the public paths are `/eco/eco/…`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/eco/eco/dashboard` | ECO dashboard aggregates (counts by type/priority, cycle time) | Yes |
| GET | `/eco/eco/` | List ECOs | Yes |
| GET | `/eco/eco/{id}` | Get an ECO | Yes |
| POST | `/eco/eco/` | Create an ECO | Admin / Manager |
| PUT | `/eco/eco/{id}` | Update an ECO | Admin / Manager |
| POST | `/eco/eco/{id}/submit` | Submit a draft ECO for review | Admin / Manager |
| POST | `/eco/eco/{id}/approve` | Record an approval decision | Admin / Manager |
| POST | `/eco/eco/{id}/reject` | Reject an ECO | Admin / Manager |
| POST | `/eco/eco/{id}/implement` | Start implementation of an approved ECO | Admin / Manager |
| POST | `/eco/eco/{id}/complete` | Mark an ECO completed | Admin / Manager |
| GET | `/eco/eco/{id}/approvals` | List an ECO's approvals | Yes |
| POST | `/eco/eco/{id}/approvals` | Add an approval requirement | Admin / Manager |
| POST | `/eco/eco/{id}/tasks` | Add an implementation task | Admin / Manager |
| PUT | `/eco/eco/{id}/tasks/{task_id}` | Update an implementation task | Admin / Manager |
| GET | `/eco/eco/affected-items/{id}` | Resolve the ECO's affected parts / work orders / documents | Yes |

> **Tenant isolation (all ECO endpoints).** Every ECO lookup is scoped to the caller's **active
> company** (`get_current_company_id`). An ECO id (or a child task id) belonging to another tenant
> returns **404 before any read or mutation** (not 403, so a guessed id can't confirm another tenant's
> ECO exists). The `/eco/eco/dashboard` aggregates (counts by type/priority, average cycle time) are
> likewise company-scoped, and `/eco/eco/affected-items/{id}` resolves affected parts / work orders /
> documents **only within the active company** (and excludes soft-deleted parts/WOs).
>
> **Cross-tenant affected ids are rejected with 422.** `affected_parts`, `affected_work_orders`, and
> `affected_documents` are id lists. On create and update, every referenced id must resolve to a live row
> **in the active company**; the first unknown or cross-tenant id returns **422 Unprocessable Entity**
> (`{"detail": "Unknown or cross-tenant <part|work order|document> id(s): [...]"}`).
>
> **Mutations require Admin / Manager.** All state-changing ECO endpoints (create, update, submit,
> approve, reject, implement, complete, add/update task, add approval) require role **ADMIN or
> MANAGER**; any other authenticated user receives **403**. The read endpoints (list, get, dashboard,
> list approvals, affected items) remain available to any authenticated user. Adding an approval also
> verifies the named approver belongs to the active company (else **404**).
>
> **ECO state changes are audited.** Create, update, submit, approve, reject, implement, and complete —
> plus task create/update and approval create — write to the tamper-evident `audit_log` (`GET /audit/`),
> so the engineering-change lifecycle is fully traceable for AS9100D.

### Purchasing

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/purchasing/pos/` | List purchase orders | Yes |
| POST | `/purchasing/pos/` | Create purchase order | Yes |
| GET | `/purchasing/pos/{id}` | Get PO by ID | Yes |
| POST | `/purchasing/po-upload` | Upload PO from PDF | Yes |

> Material receiving and incoming inspection are **not** under `/purchasing`. They live under
> `/receiving` (see below). The duplicate `/purchasing/receiving*` endpoints were removed.

### Receiving & Inspection

Canonical material-receiving and incoming-inspection endpoints, all under `/receiving`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/receiving/open-pos` | List POs available for receiving (sent/partial) | Yes |
| GET | `/receiving/po/{po_id}` | Get full PO detail for receiving | Yes |
| POST | `/receiving/receive` | Receive material against a PO line | Admin / Manager / Supervisor |
| GET | `/receiving/inspection-queue` | List receipts pending inspection | Yes |
| GET | `/receiving/receipt/{receipt_id}` | Get receipt detail | Yes |
| POST | `/receiving/inspect/{receipt_id}` | Complete inspection (accept/reject, auto-NCR on rejection) | Admin / Manager / Quality |
| GET | `/receiving/history` | Receiving history with inspection results | Yes |
| GET | `/receiving/stats` | Receiving statistics for dashboard | Yes |
| GET | `/receiving/locations` | Receivable inventory locations | Yes |

### Inventory

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/inventory/` | List inventory items | Yes |
| POST | `/inventory/receive` | Receive inventory into stock | Yes |
| POST | `/inventory/issue` | Issue inventory to a work order | Yes |
| POST | `/inventory/transfer` | Transfer inventory between locations | Yes |
| POST | `/inventory/adjust` | Adjust inventory | Admin / Manager / Supervisor |
| GET | `/inventory/{part_id}` | Get inventory for part | Yes |

> **Stock movements are audited.** Each of `/receive`, `/issue`, `/transfer`, and `/adjust` writes
> tamper-evident audit rows (`GET /audit/`) — one for the `InventoryTransaction` and one per
> stock-level change it produces (a transfer logs both the source decrement and the destination
> increment) — flushed inside the same atomic transaction as the inventory write so the audit row
> commits with the movement. The new `InventoryTransaction` rows are tenant-tagged with the active
> `company_id`.

### Traceability

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/traceability/lot/{lot_number}` | Full lot trace (source, usage, as-built genealogy, history) | Yes |
| GET | `/traceability/serial/{serial_number}` | Serial trace (transactions, work orders, NCRs) | Yes |

> **As-built genealogy (`consumed_components`).** `GET /traceability/lot/{lot_number}` returns a
> `consumed_components` array (default `[]`). When the traced lot is a finished-goods lot **produced by
> a work order** (it has a work-order RECEIVE transaction), this section reconstructs the as-built
> genealogy by enumerating that work order's component `ISSUE` transactions — so a single trace shows
> the parent finished lot **and** the component part / lot / quantity consumed to build it. It is empty
> for purchased/raw lots. Each entry carries `work_order_id`, `work_order_number`,
> `component_part_id`, `component_part_number`, `component_part_name`, `lot_number`, and `quantity`
> (reported positive). Component genealogy is populated by the **backflush** path, so a lot's
> `consumed_components` is non-empty only when the producing part had `backflush_components = true`.
> `GET /traceability/serial/{serial_number}` mirrors the lot trace's work-order and NCR collection.
> Every query is scoped to the active company.

### Shipping

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/shipping/` | List shipments | Yes |
| POST | `/shipping/` | Create shipment | Yes |
| POST | `/shipping/{shipment_id}/ship` | Mark as shipped (decrements FG, closes WO, auto-issues CoC when required) | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/coc` | Issue / generate the Certificate of Conformance (idempotent) | Admin / Manager / Quality |
| GET | `/shipping/{shipment_id}/coc` | Get CoC metadata (404 if none issued) | Yes |
| GET | `/shipping/{shipment_id}/coc/pdf` | Download the rendered CoC PDF (`application/pdf`) | Yes |

#### Carrier integration (rate / label / freight / pickup / tracking)

Multi-carrier endpoints on the shipping router. All carrier round-trips that transmit customer data
are gated by the per-company `allow_carrier_egress` kill switch (default **OFF**) — when disabled the
service makes **no** external call and returns **409**. Write actions are RBAC-gated to
`Admin / Manager / Supervisor / Shipping`; reads are open to any authenticated tenant user. Money is
`Decimal`/`Numeric(12,2)` throughout. See
[docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/shipping/validate-address` | Validate / normalize a postal address via the carrier (egress-gated). Optional `?carrier_account_id=` | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/rate-shop` | Rate-shop the shipment and persist the quotes (egress-gated). Body: `parcels` / `pallets` / optional `ship_from` / `ship_to` / `carrier_account_id` | Admin / Manager / Supervisor / Shipping |
| GET | `/shipping/{shipment_id}/rates` | List the persisted rate quotes (read-only, no egress) | Yes |
| POST | `/shipping/{shipment_id}/buy-label` | Purchase a parcel label (egress-gated, **idempotent**, audited). Body: `rate_id` (+ optional `carrier_account_id`) | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/buy-bol` | Purchase an LTL Bill of Lading (egress-gated, idempotent, audited). **Returns 501 on EasyPost** (freight is unimplemented — see note) | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/schedule-pickup` | Schedule a carrier pickup for a purchased shipment (egress-gated). Body: `pickup_date` / `window_start` / `window_end` | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/void-label` | Void a purchased label (egress-gated, idempotent, audited as CANCEL) | Admin / Manager / Supervisor / Shipping |
| POST | `/shipping/{shipment_id}/refund` | Request a refund for a purchased label (alias of void; same money-moving CANCEL) | Admin / Manager / Supervisor / Shipping |
| GET | `/shipping/{shipment_id}/tracking` | Stored tracking status + event history (read-only, not egress-gated) | Yes |

> **Egress kill switch (409).** `validate-address`, `rate-shop`, `buy-label`, `buy-bol`,
> `schedule-pickup`, and `void`/`refund` are blocked with **HTTP 409** (`EgressDisabledError`) until an
> admin enables `allow_carrier_egress` on the company shipping profile
> (`PUT /admin/settings/shipping-profile`). This is the CUI / data-egress gate — those calls transmit
> the customer ship-to address to a third-party aggregator. `test-connection` is the only carrier
> round-trip exempt (it sends no customer data).
>
> **Idempotency.** `buy-label` / `buy-bol` pre-check for an already-purchased label/BOL and return the
> existing shipment with `already_purchased: true` (no provider call). A deterministic idempotency key
> (`sha256(company_id:shipment_id:rate_id)`) is persisted (partial-unique index) and sent to the
> provider as an `Idempotency-Key` header.
>
> **Freight is scaffolded, not functional on EasyPost.** `buy-bol` (and the underlying freight
> rate-shop) raise `NotSupportedError` on the EasyPost adapter → **HTTP 501**. EasyPost LTL is an
> Enterprise-gated feature with no public REST wire format; the freight path is real at the
> service/model/schema layers and waits on a future Zenkraft adapter. Parcel rate/label/track is fully
> implemented.
>
> **Carrier-error → HTTP mapping** (`_map_carrier_error`): `EgressDisabledError` → 409,
> `AddressInvalidError` → 422, `NotSupportedError` → 501, a `CarrierError` containing "not found" → 404,
> any other provider failure → 502. Provider internals and secrets are never surfaced.
>
> **Tracking is informational.** Webhook / poll tracking events update `tracking_status` and set
> `actual_delivery` on a `DELIVERED` event, but **never** auto-close the work order — `mark_shipped`
> remains the only WO-closing action.

> **Shipment-close is audited.** Marking a shipment shipped closes its work order
> (status → `CLOSED`); that terminal status change is recorded in the tamper-evident audit trail
> (`GET /audit/`), flushed so the audit row commits atomically with the closure.
>
> **`POST /shipping/{shipment_id}/ship` is RBAC-gated to Admin / Manager / Supervisor / Shipping.**
> Marking a shipment shipped is the terminal shipping action that **CLOSES the work order**, so it is
> restricted to the documented Shipping **"Complete"** role set
> (`require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])`) rather than any authenticated user. A
> non-privileged tenant user now gets **403**. See `docs/RBAC_PERMISSIONS.md` → Shipping. (The two
> read CoC endpoints below stay open to any authenticated company user; issuing a CoC is
> Admin / Manager / Quality.)
>
> **Marking shipped decrements finished-goods inventory (G2).** `POST /shipping/{shipment_id}/ship`
> now writes the offsetting outbound stock movement for the goods leaving the building — the mirror of
> the Batch-6 finished-goods receipt on completion. It writes a `SHIP` `InventoryTransaction`
> (`quantity = -quantity_shipped`, `reference_type = "shipment"`) and decrements the finished-goods
> lot's on-hand / available (the lot is matched on `part_id` + finished-goods location +
> `work_order.lot_number`, exactly the row the receipt created). Both the SHIP transaction and its
> audit rows join the same unit of work as the SHIPPED status change + WO close, so they commit
> atomically. The decrement is **idempotent**: a re-submitted or concurrent double-ship (the shipment
> row is locked `FOR UPDATE` and a prior SHIP transaction for the shipment short-circuits) never
> double-decrements on-hand. **No new request/response field** — this is a side effect of marking
> shipped.
>
> **Over-ship and missing-FG-lot are warn-and-record, not blocking (G2).** Neither condition fails
> the ship — the ship/close still proceeds (mirrors the warn-and-record posture of the completion
> backflush-shortage and quality gates):
> - **Over-ship:** if cumulative `quantity_shipped` across the work order's non-cancelled shipments
>   exceeds what was produced (`WorkOrder.quantity_complete`), the ship is **allowed** but a
>   tamper-evident `audit_log` row (action `OVER_SHIP`) + a warning operational event record the
>   overage. There is no sales-order quantity to ship against; produced quantity is the ceiling.
> - **FG lot not found:** if no matching finished-goods lot row exists (the receipt was skipped, the
>   lot changed, or the stock was already moved), on-hand is **not** decremented and a tamper-evident
>   `audit_log` row (action `SHIP_FG_LOT_MISSING`) + a warning operational event record the
>   discrepancy; the ship/close still proceeds.

> **Certificate of Conformance (CoC) generation (G6-B).** A CoC is a real, per-shipment compliance
> artifact (previously just a `cert_of_conformance` boolean). It is a **DB frozen snapshot** — the
> `certificates_of_conformance` row stores the immutable certified facts at issue time and the PDF is
> rendered **deterministically on download** (there is no filesystem blob). CoC content is an AS9100D
> conformance statement + part/revision + WO# / customer-PO + quantity + lot/serial table +
> signature/issuer block. All three endpoints are **tenant-scoped** (a cross-tenant `shipment_id`
> returns **404**):
> - `POST /shipping/{shipment_id}/coc` — issue or return the existing CoC. **Idempotent**: at most one
>   CoC per shipment, DB-enforced (`uq_coc_company_shipment`); re-issuing returns the same CoC with no
>   second audit row. RBAC: **Admin / Manager / Quality** (quality artifact). First issue writes a
>   tamper-evident `log_create` audit row.
> - `GET /shipping/{shipment_id}/coc` — CoC metadata; **404** if none issued. Any authenticated company
>   user (read-broad / write-restricted, like the other shipping reads).
> - `GET /shipping/{shipment_id}/coc/pdf` — streams the rendered PDF (`application/pdf`,
>   `Content-Disposition: attachment`). Any authenticated company user.
>
> **Auto-issue on ship.** `POST /shipping/{shipment_id}/ship` auto-issues a CoC when one is
> **required** — required = the shipment's `cert_of_conformance` flag is set **OR** a company-scoped
> `Customer` matched by `work_order.customer_name` has `requires_coc` (which **defaults `True`**, so
> auto-issue fires for essentially every customer-matched shipment — the intended fail-safe).
> Auto-issue is **idempotent and best-effort**: a CoC failure never fails the ship — it records a
> `coc_generation_failed` warning operational event (mirrors the warn-and-record posture of the FG /
> over-ship guards). A successful auto-issue commits atomically with the ship and sets the shipment's
> `cert_of_conformance` flag.

### Reports

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/reports/work-orders` | Work order report | Yes |
| GET | `/reports/production` | Production report | Yes |
| GET | `/reports/quality` | Quality report | Yes |
| POST | `/reports/custom` | Generate custom report | Yes |

### Analytics

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/analytics/overview` | Analytics overview | Yes |
| GET | `/analytics/kpis` | KPI dashboard (OEE, OTD, FPY, scrap, NCRs, …) | Yes |
| GET | `/analytics/production-trends` | Production trends | Yes |
| GET | `/analytics/quality-metrics` | Quality metrics | Yes |
| GET | `/analytics/cost-analysis` | Job cost analysis (estimated vs. actual) | Yes |
| POST | `/analytics/custom-report` | Run a custom-report query (returns rows) | Admin / Manager |
| GET | `/analytics/custom-report/export` | Export a saved report template (csv / xlsx / pdf) | Admin / Manager |

> **Custom reports are tenant-scoped.** Both `POST /analytics/custom-report` and
> `GET /analytics/custom-report/export` run the report through the shared `ReportBuilderService`, which
> now **always restricts the query to the caller's active company** (`company_id`) before applying any
> user-supplied filters/group-by/sort. Every supported data source (work orders, parts, inventory, NCRs,
> purchase orders, quotes) carries `company_id`, so a report can never return another tenant's rows. This
> is a scoping-only fix — the request/response shape is unchanged.
>
> **Custom-report labor honesty (G3-content).** Two changes make labor columns read truthfully when
> labor cost is not being tracked:
> - **`estimated_hours` is no longer a selectable WORK_ORDERS column.** It has no writer anywhere in
>   the system (it is structurally 0 in every tenant), so it has been dropped from
>   `GET /analytics/data-sources` and from the report builder's field map. Selecting it is no longer
>   possible (it silently dropped out before).
> - **Labor-not-tracked response headers on `POST /analytics/custom-report`.** When
>   `LABOR_COST_ROLLUP_ENABLED` is **off** (the default) **and** the report selects any labor-derived
>   WORK_ORDERS column (`actual_hours`, `actual_cost`, `estimated_cost`) — which then render a literal
>   `0` meaning "not tracked", not a measured zero — the response sets two headers so a consumer can
>   tell the two apart: `X-Report-Labor-Not-Tracked` (a JSON array of the affected column names) and
>   `X-Report-Labor-Note` (a human-readable explanation). The **response body is unchanged** (the
>   bare-list contract the export + clients rely on); the headers are set only when applicable. When
>   the flag is on, the data source isn't WORK_ORDERS, or no labor-derived column is selected, no
>   headers are set.
>
> **KPI values can be `null` ("n/a").** Each KPI on `GET /analytics/kpis` is a `KPIValue` whose
> **`value` (and `prior_value` / `change_pct`) are nullable**. A genuinely-uncomputable metric returns
> `null` rather than a misleading 0/100, and the frontend renders **"n/a"**:
> - **OEE** is `null` when the work center (or plant) has **no staffed (clocked) time** in the window —
>   there is no availability denominator, so it is uncomputable, not 0%.
> - **On-time delivery (OTD)** is `null` when **no work order with a due date completed** in the window
>   (empty denominator) — not a fabricated 100%.
>
> **OEE convention (`Availability × Performance × Quality`).** Computed per work center on the
> **staffed-time** basis, identical on this headline and on the persisted `OEERecord` (see OEE Tracking
> below): Availability = productive-run hours ÷ staffed (clocked) hours, productive run = (RUN+SETUP) −
> UNPLANNED downtime; Performance = ideal hours ÷ productive run, ideal hours = Σ((produced + scrapped)
> × routing `run_time_per_piece`) over RUN+REWORK (every piece run consumes a standard cycle, including
> scrap); Quality = good ÷ (good + scrapped) over RUN+REWORK.
>
> **OTD rule.** On-time = `actual_end.date() <= due_date`. A **COMPLETE work order with a null
> `actual_end` counts as NOT on time** (no verifiable completion date). The completed-set is
> tenant-scoped and soft-delete-filtered (`is_deleted == False`).

> **Cost-analysis labor/overhead is gated by `LABOR_COST_ROLLUP_ENABLED`.** `GET /analytics/cost-analysis`
> derives each job's labor and overhead from the work order's actual hours at the shared work-center
> rate — the **same** source the completion rollup uses, so the report and `WorkOrder.actual_cost` agree.
> When the flag is **off** (the default) the computed **labor and overhead legs report `$0`** (not
> tracked), uniformly across live- and reconcile-completed work orders. The **material leg is never
> gated** — it is real issued-material from inventory (the completion ISSUE transactions), so it stays
> accurate either way. The on-demand `POST /job-costs/{id}/calculate` recomputes job-cost labor from time
> entries regardless of the flag and is **tenant-scoped** (a job cost is looked up by id **and**
> company, closing a prior cross-tenant lookup).

### OEE Tracking

OEE = **Availability × Performance × Quality** per work center. **Reads** (dashboards/trends) are open
to any authenticated user in the tenant so the shop floor can view them; **writes** (auto-calculate,
records, targets) require **Admin / Manager / Supervisor**.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/oee/dashboard` | OEE per work center, plant-wide OEE, targets (`period` 7d/30d/90d/365d) | Yes |
| GET | `/oee/trends` | OEE time-series for charts (`work_center_id`, `period`) | Yes |
| GET | `/oee/six-big-losses/{work_center_id}` | Six-big-losses breakdown | Yes |
| GET | `/oee/records` | List OEE records (filters: WC, date range, shift) | Yes |
| GET | `/oee/records/{record_id}` | Get one OEE record | Yes |
| POST | `/oee/calculate/{work_center_id}` | Auto-calculate the day/shift OEE record from data | Admin / Manager / Supervisor |
| POST | `/oee/records` | Create an OEE record (manual inputs) | Admin / Manager / Supervisor |
| PUT | `/oee/records/{record_id}` | Update + recalculate an OEE record | Admin / Manager / Supervisor |
| DELETE | `/oee/records/{record_id}` | Delete an OEE record | Admin / Manager / Supervisor |
| GET | `/oee/targets` | List OEE targets | Yes |
| POST | `/oee/targets` | Create/update a work center's OEE target | Admin / Manager / Supervisor |
| PUT | `/oee/targets/{target_id}` | Update an OEE target | Admin / Manager / Supervisor |
| DELETE | `/oee/targets/{target_id}` | Delete an OEE target | Admin / Manager / Supervisor |

> **RBAC split (read-broad / write-restricted).** The write/mutation endpoints depend on
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`OEE_WRITE_ROLES` in `app/api/endpoints/oee.py`); they
> were previously open to any authenticated user. Read endpoints depend on `get_current_user` only, so
> operators/viewers can still load dashboards. Superuser / Platform Admin bypass role checks, as
> elsewhere. See `docs/RBAC_PERMISSIONS.md` → OEE.
>
> **OEE writes are audited.** All OEE record/target mutations — `POST /oee/calculate/{work_center_id}`,
> `POST/PUT/DELETE /oee/records`, and `POST/PUT/DELETE /oee/targets` — now write a tamper-evident
> `audit_log` row (`AuditService` `log_create` / `log_update` / `log_delete`, resource types
> `oee_record` / `oee_target`). The audit row is flushed and logged **before** the terminal commit, so
> it commits atomically with the record/target. The auto-calc upsert writes one representative row per
> call. (These were RBAC-gated but unaudited prior to 2026-06-09.)

> **`POST /oee/calculate/{work_center_id}` (auto-calculate).** Builds (or upserts, per work center +
> date + shift) a real `OEERecord` for `record_date` (default today) from the day's **closed**
> `TimeEntry` rows, the routing standard cycle time, and reported `DowntimeEvent` rows — on the
> **staffed-time** convention so it agrees with the `/analytics/kpis` headline:
> - **Availability** = productive-run minutes ÷ **staffed (clocked)** minutes at the WC; productive run
>   = (RUN+SETUP) minutes − **UNPLANNED** `DowntimeEvent` minutes. (Returns/stores 0 availability when
>   there is no staffed time for that WC/day.)
> - **Performance** = ideal hours ÷ productive run; ideal hours = Σ((`quantity_produced` +
>   `quantity_scrapped`) × `WorkOrderOperation.run_time_per_piece`) over RUN+REWORK — derived from the
>   routing, not a hardcoded cycle. Every piece run (including scrap) consumes a standard cycle.
> - **Quality** = good ÷ (good + scrapped); good = Σ `quantity_produced`, scrapped =
>   Σ `quantity_scrapped` over RUN+REWORK.
>
> This endpoint previously referenced `TimeEntry.start_time` / `end_time` (which do not exist) and
> returned **500** on every call; it now uses `clock_in` / `clock_out`. All queries are tenant-scoped;
> a foreign `work_center_id` returns **404**.

### Users (Admin)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/users/` | List all users | Admin |
| POST | `/users/` | Create user | Admin |
| PUT | `/users/{id}` | Update user | Admin |
| DELETE | `/users/{id}` | Delete user | Admin |

### Admin Settings (Admin)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/admin/settings` | Get system settings | Admin |
| PUT | `/admin/settings` | Update system settings | Admin |
| GET | `/admin/settings/audit-log` | Settings/quote-config change history (filterable, up to 1yr) | Admin |

> **Settings-audit tenancy:** `GET /admin/settings/audit-log` reads the `SettingsAuditLog` trail
> (admin / quote-config changes) and is **scoped to the caller's active company**
> (`get_current_company_id`). Writes to this trail are tagged with that same active company, so a
> platform admin's changes attribute to the company they have switched into — matching the
> `/audit/*` (`AuditLog`) attribution. This is a separate trail from `/audit/*` and is **not** part
> of the tamper-evident hash chain.

### Carrier Integrations (Admin)

Per-company carrier-aggregator credentials + ship-from / egress profile for the multi-carrier
shipping integration. All routes are mounted under `/admin/settings` and gated to **Admin**. See
[docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/admin/settings/carrier-accounts` | List the company's carrier accounts (secrets masked) | Admin |
| GET | `/admin/settings/carrier-accounts/{id}` | Get one carrier account (secrets masked) | Admin |
| POST | `/admin/settings/carrier-accounts` | Create a carrier account (api key / webhook secret encrypted at rest) | Admin |
| PUT | `/admin/settings/carrier-accounts/{id}` | Update a carrier account; sending `api_key` / `webhook_secret` rotates the stored secret | Admin |
| DELETE | `/admin/settings/carrier-accounts/{id}` | **Soft-delete** a carrier account (never physical — purchased labels/BOLs reference it) | Admin |
| POST | `/admin/settings/carrier-accounts/{id}/test-connection` | Validate the stored credential (the **only** carrier call exempt from the egress kill switch — sends no customer data) | Admin |
| GET | `/admin/settings/shipping-profile` | Get the company shipping profile (ship-from origin + egress flag); **404** until created | Admin |
| PUT | `/admin/settings/shipping-profile` | Create / update the shipping profile, including the `allow_carrier_egress` kill switch | Admin |

> **Secrets are write-only.** `api_key` and `webhook_secret` are accepted on create/update,
> **Fernet-encrypted** before storage, and **never returned** — read responses expose only
> `api_key_last4` and `has_webhook_secret`, and secrets never appear in audit / event payloads.
> Create / update / delete are audited; an update flags `api_key_rotated` / `webhook_secret_rotated`
> rather than recording the value.
>
> **`allow_carrier_egress` is the CUI kill switch (default OFF).** A new profile is created with
> egress **disabled**; it flips only when an admin sets it on `PUT /shipping-profile`, and that toggle
> is recorded as a **status change** on the tamper-evident audit trail. While OFF, every
> customer-data-bearing carrier call (`/shipping/validate-address`, `/rate-shop`, `/buy-label`,
> `/buy-bol`, `/schedule-pickup`, `/void-label`, `/refund`) is blocked with **409**.

### AI Usage Telemetry

Read-only cost/latency observability over the per-call LLM usage ledger (`ai_usage_events` — one
row per Anthropic API call, written by the shared client `app/services/llm_client.py`). Aggregates
are **scoped to the caller's active company** (`get_current_company_id`).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/ai-usage/summary` | Per-task and per-model AI usage aggregates over a trailing window | Admin / Manager |

**Query parameters:** `days` — aggregation window in days, integer `1`–`365` (default `30`).

**Response shape:** `{ window_days, since, totals, by_task[], by_model[] }`. `totals` and each
`by_task` / `by_model` row carry the same aggregate fields: `calls`, `input_tokens`,
`output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `estimated_cost_usd` (nullable —
`null` when the bucket has no priced calls; models missing from the price table in
`llm_client.MODEL_PRICING_USD_PER_MTOK` record cost as `NULL`), `avg_latency_ms` (nullable), and
`error_rate` (failed calls / total calls, `0.0`–`1.0`). `by_task` rows add `task` (e.g.
`po_extraction`, `routing_generation`); `by_model` rows add `model` (the exact model id used).

> **Telemetry, not audit data.** `ai_usage_events` rows record task, model/tier, prompt version,
> token counts, estimated USD cost, latency, and success/error per LLM call. They are operational
> telemetry — not on the tamper-evident `audit_log` hash chain — and the endpoint is read-only
> (no `AuditService` involvement).
>
> **UI surface / dormant Manager allowance.** The endpoint backs the **Admin Settings → AI Usage &
> Cost** tab (`/admin/settings?tab=aiusage`). The server allows **Admin and Manager**
> (`require_role([ADMIN, MANAGER])`), but the only consuming UI today is the AdminRoute-gated
> Admin Settings page, so Managers can currently exercise the allowance only via direct API calls.

### Bulk Imports & Templates (Excel Migration Kit)

One shared CSV/XLSX upload kit for go-live data migration — see
[docs/EXCEL_MIGRATION_RUNBOOK.md](EXCEL_MIGRATION_RUNBOOK.md) for the operational sequence. All
import endpoints below accept **`.csv`** (UTF-8) or **`.xlsx`** (first worksheet only) via the
shared parser (`app/services/import_service.py`): headers are normalized to snake_case
(`"Part Number"` → `part_number`), rows whose **first cell starts with `"# "`** (hash + space — the
template guidance marker; a bare `#` is data) are skipped, blank rows are tolerated, and files are
capped at **10 MB / 10,000 data rows**. File-level problems (type, encoding, missing required
columns, duplicate-after-normalization headers, caps) return **400** with a plain-English `detail`;
two distinct columns that collide after normalization are a **hard error** naming both offenders
(refusing the file beats silently merging columns in a migration tool). Row-level validation stays
per-endpoint with the partial-success contract: on commit each row (each PO, for the PO import) is
saved independently, bad rows are skipped and reported in `errors[]`.

**Templates** (static workbooks, no tenant data — any authenticated user):

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/import/templates` | List the 9 downloadable templates (entity, title, columns, download path) | Yes |
| GET | `/import/templates/{entity}` | Download the styled XLSX template (`werco-import-template-{entity}.xlsx`); 404 lists valid entities | Yes |

Template entities: `users`, `parts`, `materials`, `customers`, `vendors`, `work-centers`,
`work-orders`, `purchase-orders`, `bom`. Each workbook has an **Import** sheet (styled header + one
`# `-prefixed guidance row, skipped on import) and an **Examples** sheet (never read by the
importer).

**Entity imports** (all pre-existing; now accepting XLSX, a `dry_run` query param, and audit-logging
every created row):

| Method | Endpoint | Required columns | Auth Required |
|--------|----------|------------------|---------------|
| POST | `/users/import-csv` | `employee_id`, `first_name`, `last_name` | Admin |
| POST | `/parts/import-csv` | `part_number`, `name`, `part_type` | Admin / Manager / Supervisor |
| POST | `/materials/import-csv` | `part_number`, `name`, `part_type` | Admin / Manager / Supervisor |
| POST | `/customers/import-csv` | `name` | Admin / Manager |
| POST | `/purchasing/vendors/import-csv` | `name` | Admin / Manager |
| POST | `/work-centers/import-csv` | `code`, `name`, `work_center_type` | Admin / Manager |

**Open-document migration imports** (new):

| Method | Endpoint | Required columns | Auth Required |
|--------|----------|------------------|---------------|
| POST | `/work-orders/import` | `part_number`, `quantity` | Admin / Manager / Supervisor |
| POST | `/purchasing/purchase-orders/import` | `vendor_code`, `part_number`, `quantity`, `unit_price` | Admin / Manager |

> **`dry_run=true` (all eight import endpoints).** Validates and previews with **zero writes** —
> the migration imports run every row inside a SAVEPOINT that is rolled back (including audit rows
> and operational events), and a terminal `db.rollback()` backstops the whole request. The response
> carries everything the commit would: counts, per-row `errors[]`, and (WO/PO imports) per-row
> `results[]`. Numbers the system would generate (`wo_number` / `po_number` / vendor & customer
> codes) are **not** reserved by a dry run — they report as `null` / "generated at commit".
>
> **Response shapes.** The six entity imports keep their existing response models
> (`total_rows`, `imported_count` — `created_count` on users — `skipped_count`, `created_ids`,
> `errors[]`) plus an **additive** `dry_run: bool` field (default `false`), so commit responses stay
> backward compatible. The WO/PO imports return `WorkOrderImportResponse` /
> `PurchaseOrderImportResponse` (`app/schemas/import_kit.py`): `dry_run`, `total_rows`,
> `created_count`, `skipped_count`, `created_ids`, `results[]`, `errors[]` (the PO response adds
> `created_line_count`, and its `results[]` entries are per-PO, not per-row).
>
> **All import rows are audited.** Every committed row writes a tamper-evident `audit_log` entry via
> `AuditService` tagged `extra_data.source = "import"` (previously the CSV imports skipped audit
> logging). The **user import never logs `new_values`** — the model carries `hashed_password` and
> secrets must not land in the audit log. The user import also **rejects `role = platform_admin`**
> per row: a tenant spreadsheet must not mint the cross-company oversight role (see
> `docs/RBAC_PERMISSIONS.md` → Bulk Imports).
>
> **`POST /work-orders/import` — open (in-flight) work orders.** Optional columns: `wo_number`
> (generated when blank; uniqueness checked **case-insensitively**, in-file and against the DB),
> `due_date` (**past dates allowed** — open WOs can be overdue; this intentionally differs from the
> interactive `WorkOrderCreate` schema), `customer` (existing customer **code or name**),
> `customer_po`, `priority` (1–10, default 5), `completed_through_seq`. The part must exist **with a
> released routing** (operations are generated through the same path as `POST /work-orders/`, never
> raw inserts); the WO is released on import (first pending op promoted to READY) so it lands in
> floor queues. **Paper-complete seeding:** operations with `sequence <= completed_through_seq` are
> set COMPLETE at target quantity with **no fabricated `actual_start`/`actual_end`, operators, or
> TimeEntry labor** (that evidence doesn't exist; inventing it would corrupt cycle-time/labor
> analytics and the AS9100D story). Each paper-completed op emits an `operation_completed`
> OperationalEvent with `source = "import"`, and the WO's audit rows record the exact
> `paper_completed_sequences`. A `completed_through_seq` covering **every** operation is rejected —
> only open WOs may be imported.
>
> **`POST /purchasing/purchase-orders/import` — open (issued) purchase orders.** Rows sharing a
> `po_number` become **lines of one PO** (blank `po_number` = single-line PO, number generated at
> commit); a PO imports whole-or-not-at-all — one invalid line skips its whole group, and all lines
> must share one `vendor_code`. Imported POs land in **`sent`** status (receivable on day 1) with
> **`order_date` deliberately NULL** — the real order date predates the system and is unknown; NULL
> means "pre-migration", mirroring the WO no-fabricated-provenance decision. `expected_date` is the
> max `promised_date` across lines. **Admin / Manager only** — the interactive `/send` transition is
> Admin/Manager, so allowing Supervisor here would let a spreadsheet issue POs the UI forbids.

### Audit Log

Tamper-evident audit trail (CMMC Level 2 AU-3.3.8). Audit rows are **tenant-tagged** with
`company_id`, so retrieval and the per-record lookup are **scoped to the caller's active
company**. The integrity hash chain itself is a single global sequence interleaved across all
tenants, so the aggregate chain-verification endpoints are **platform-admin only**.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/audit/` | List audit logs for the active company (filterable) | Admin / Manager |
| GET | `/audit/summary` | Audit activity summary for the active company | Admin / Manager |
| GET | `/audit/actions` | Distinct action types in the active company | Admin / Manager |
| GET | `/audit/resource-types` | Distinct resource types in the active company | Admin / Manager |
| GET | `/audit/integrity/status` | Global chain status (counts, sequence range) | Platform Admin |
| GET | `/audit/integrity/verify` | Full hash-chain verification (optional range) | Platform Admin |
| GET | `/audit/integrity/verify-recent` | Verify the N most recent records | Platform Admin |
| GET | `/audit/integrity/record/{sequence_number}` | Verify a single record | Admin (own company only) |

> **Tenancy:** the four retrieval endpoints filter by the active company (`get_current_company_id`),
> returning only that tenant's audit data. `/integrity/record/{sequence_number}` lets a
> company-scoped Admin verify one record **belonging to their active company**; a record from
> another tenant returns **404** (not 403, so cross-tenant probing can't confirm the record
> exists). Platform Admins / superusers may inspect any record.
>
> **Why the aggregate `/integrity/*` endpoints are Platform-Admin only:** the hash chain is one
> global sequence spanning every tenant, so its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to a single company without leaking other tenants' data. A company
> Admin's "are my records intact?" need is served by the per-record endpoint above.

## Real-time Updates (WebSocket)

Real-time work-order, dashboard, and shop-floor updates are delivered over WebSocket. **All three
endpoints require a valid JWT**, passed as a `token` query parameter (the frontend's API client
already attaches it). An unauthenticated or invalid-token connection is rejected with WebSocket
close code **1008** (policy violation).

| Endpoint | Purpose |
|----------|---------|
| `WS /ws/updates?token=<jwt>` | Dashboard and system-wide updates |
| `WS /ws/shop-floor/{work_center_id}?token=<jwt>` | Shop-floor updates for one work center |
| `WS /ws/work-orders/{work_order_id}?token=<jwt>` | Status updates for one work order |

> **Tenant-scoped broadcasts.** Each connection is bound at connect time to the caller's **active
> company** (resolved the same way as `get_current_company_id` — via the token's `cid` claim, with
> a fallback to the user's own company for legacy tokens). Work-order / dashboard / shop-floor
> completion broadcasts are delivered **only to that company's connections**, never globally, so a
> client never sees another tenant's events. `/ws/updates` previously accepted unauthenticated
> connections for general updates; that is no longer permitted (tenant isolation).

## Common Response Formats

### Success Response
```json
{
  "id": 1,
  "created_at": "2024-01-01T10:00:00",
  "updated_at": "2024-01-01T10:00:00"
}
```

### Error Response
```json
{
  "detail": "Error message description"
}
```

### Validation Error (422)
```json
{
  "detail": [
    {
      "loc": ["body", "field_name"],
      "msg": "Field is required",
      "type": "value_error.missing"
    }
  ]
}
```

### Not Found error (404)
```json
{
  "detail": "Resource not found"
}
```

### Unauthorized error (401)
```json
{
  "detail": "Could not validate credentials"
}
```

## Pagination

List endpoints support pagination via query parameters:

```
GET /work-orders/?page=1&limit=50&sort=created_at&order=desc
```

Parameters:
- `page`: Page number (default: 1)
- `limit`: Items per page (default: 50, max: 100)
- `sort`: Field to sort by
- `order`: Sort direction (`asc` or `desc`)

Response:
```json
{
  "items": [...],
  "total": 234,
  "page": 1,
  "limit": 50,
  "pages": 5
}
```

## Webhooks

The platform can POST outbound webhooks to per-tenant registered endpoints when a work order is
completed or closed. Webhooks are **tenant-scoped**: a company only ever receives events for its own
work orders, delivered only to endpoints registered under that company.

> Webhook endpoints are currently provisioned via the backend service (seeded through
> `WebhookService`); there is no self-service webhook-admin REST endpoint yet.

### Events

| Event | Fires when |
|-------|------------|
| `work_order.completed` | A work order reaches **COMPLETE** (operation/WO completion paths) |
| `work_order.closed` | A work order reaches **CLOSED** (shipment is marked shipped) |

### Payload

The outbound payload is **intentionally minimal and redacted** — it carries only the structured
identifiers a subscriber needs to react and then re-fetch full detail via the authenticated API
(keyed on `work_order_id`). Free-text and customer-identifying fields are **deliberately excluded**:

```json
{
  "work_order_id": 1,
  "work_order_number": "WO-10001",
  "part_id": 123,
  "status": "COMPLETE",
  "quantity_complete": 100.0,
  "quantity_scrapped": 2.0,
  "company_id": 42,
  "completed_at": "2026-06-07T14:30:00"
}
```

- `status` is the terminal work-order status: `"COMPLETE"` (for `work_order.completed`) or `"CLOSED"`
  (for `work_order.closed`).
- `customer_name` and any notes/free-text are **not** included by design (CUI minimization for an
  egressing payload). To obtain customer or other detail, re-fetch the work order via
  `GET /work-orders/{work_order_id}` with an authenticated request.

Delivery is asynchronous (ARQ background worker), enqueued after the completion commits and
best-effort — a webhook failure never affects the work-order completion. Note that the **internal**
`WO_COMPLETED` notification (email to the tenant's own users) may carry richer context than the
egressing webhook payload above.

### Inbound carrier tracking webhooks

The carrier integration also **receives** inbound tracking webhooks from the aggregator:

| Method | Endpoint | Auth |
|--------|----------|------|
| POST | `/webhooks/carriers/{provider}` (e.g. `/webhooks/carriers/easypost`) | **None** — HMAC-verified |

This is the **only unauthenticated route in the API** — a carrier cannot present a JWT. Trust and
tenancy are established without any caller-supplied identity:

- The signature is verified (constant-time) against the stored per-tenant `webhook_secret` (EasyPost:
  HMAC-SHA256 over the raw body, hex, in the `X-Hmac-Signature` header). A request matching **no**
  tenant's secret is dropped with **204** (no body — no existence oracle).
- The owning tenant is resolved **only from stored shipment data** (`Shipment.aggregator_shipment_id`,
  falling back to `tracking_number`), **never** from the path or body. No matching shipment → **204**.
- A verified, resolvable event returns **200** quickly; the normalized events are enqueued to the ARQ
  `process_tracking_webhook_job` with the *resolved* `company_id` + `shipment_id`, and the DB write
  (de-dup + status flow-back) happens in the job. If enqueue fails (Redis hiccup) the handler still
  acknowledges with **202** — the poll-cron fallback re-delivers state.

See [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md) for setup and the poll
fallback.

## Rate Limiting

API endpoints are rate limited:
- Default: 100 requests per 60 seconds per IP
- Health check endpoints: Exempt from rate limiting

Rate limit headers are included in responses:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 75
X-RateLimit-Reset: 1704097200
```

## CORS

Cross-Origin Resource Sharing is configured to allow requests from:
- Development: `http://localhost:3000`, `http://localhost:8000`
- Production: Your configured frontend domain

## Trusted Hosts

When `ALLOWED_HOSTS` is configured (production), a request whose HTTP `Host`
header is not on the allowlist is rejected with **HTTP 400** before any route
runs. The default `*` allows any host (validation disabled — dev). See
[Trusted Hosts](ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header).

## Health Check

```http
GET /health
```

Response:
```json
{
  "status": "healthy",
  "app": "Werco ERP",
  "environment": "production",
  "version": "1.0.0"
}
```

## Error Codes

| Status Code | Description |
|-------------|-------------|
| 200 | Success |
| 201 | Created |
| 204 | No Content |
| 400 | Bad Request (also returned for a `Host` header not on the `ALLOWED_HOSTS` allowlist) |
| 401 | Unauthorized |
| 403 | Forbidden |
| 404 | Not Found |
| 409 | Conflict — concurrent modification of an operation / work order / time entry on a completion or clock endpoint (the row was updated by another writer between read and commit; refresh and retry) |
| 422 | Validation Error |
| 429 | Too Many Requests |
| 500 | Internal Server Error |

## Interactive Documentation

When the backend is running, visit:
- **Swagger UI**: `/api/docs` - Interactive API explorer
- **ReDoc**: `/api/redoc` - Alternative documentation view
- **OpenAPI JSON**: `/api/openapi.json` - Raw specification

For more details on specific endpoints, use the interactive documentation above.
