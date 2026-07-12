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

### Station signin tokens (visitor sign-in tablet)

Scoped, revocable credentials for an unattended lobby **visitor sign-in tablet**. A signin token is a
JWT with `type="signin"`, **24 h** TTL, minted by the shared station **PIN** via
`POST /visitor-logs/station-login` (see Visitor Logs below). It authenticates **only**
`POST /visitor-logs/sign-in` and `POST /visitor-logs/sign-out` (via the dedicated
`get_signin_principal` dependency) — every other endpoint rejects it with **401** (`verify_token`
accepts only `type="access"` JWTs). It carries no user identity; the active company is taken from the
`signin_stations` DB row (never the JWT's `cid`), and the row's `revoked` flag is re-checked on every
request, so a revoked station's tokens die on the next call. See
[docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md).

### Kiosk station tokens + badge-minted operator tokens (crew-station kiosk)

Two-tier credentials for an unattended **shop-floor crew tablet** (`/kiosk?kiosk=1&station=<id>`,
see [docs/KIOSK.md](KIOSK.md) → Crew station mode):

- **Station tier** — a JWT with `type="kiosk"`, **24 h** TTL, minted by the shared station **PIN**
  via `POST /shop-floor/kiosk-stations/station-login` (see Shop Floor below). It authenticates
  **only** the roster-enriched `GET /shop-floor/work-center-queue/{id}` (its bound work center
  only, via the dedicated `get_kiosk_or_user` dependency) and the badge-token mint below — every
  other endpoint rejects it with **401** (`verify_token` accepts only `type="access"` JWTs). It
  carries no user identity; the active company and the bound work center come from the
  `kiosk_stations` DB row (never the JWT's claims), and the row's `revoked` flag is re-checked on
  every request.
- **Operator tier** — each badge scan exchanges (station token + badge) for a **5-minute**
  `type="access"` JWT carrying a **`scope="kiosk"`** claim and **no refresh token**:

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/auth/kiosk-badge-token` | Exchange a badge scan for a 5-min kiosk-scoped operator token. Body `{"employee_id"}` → `{"access_token", "token_type", "expires_in": 300, "user": {"id", "full_name", "employee_id"}}`. Unknown / inactive / locked / foreign-tenant badge → uniform **401** "Invalid badge"; ambiguous badge within the company → **409**. Issuance and failures are audited (`KIOSK_BADGE_TOKEN_ISSUED` / `KIOSK_BADGE_TOKEN_FAILED`). Rate-limited **30/minute** per IP | Kiosk station token |

> **Path fence.** A `scope="kiosk"` operator token is honored only on `/api/v1/shop-floor/*` and
> `POST /api/v1/auth/employee-logout`; `get_current_user` rejects it with **403** everywhere else
> (the token is valid — it just cannot reach the resource). Two shop-floor carve-outs are also
> **denied** to kiosk-scoped tokens regardless of role: `/shop-floor/kiosk-stations/*` (station
> lifecycle admin) and `/shop-floor/time-entries/{id}/approve|unapprove` (G5-A labor approval).
> Tokens without a `scope` claim are
> unaffected. On the allowed paths the operator IS `current_user`, so audit attribution, tenant
> isolation, and RBAC apply unchanged. Known residual: the WebSocket auth path
> (`get_current_user_from_token`) has no request path to fence, so a kiosk-scoped token can open
> the read-only `/ws/*` broadcast channels during its ≤5-minute life (documented in
> [docs/KIOSK.md](KIOSK.md)).

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
> send an explicit value (including `0`) to overwrite it. When the value written is **> 0** a
> scrap reason is **required** (else **422**) — free-text `scrap_reason`, or on
> `/work-orders/{id}/complete` alternatively a structured `scrap_reason_code_id` — see "Scrap reason
> is required when scrap is reported" below. Completing an **on-hold** operation is
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
>
> **Scrap reason is required when scrap is reported (AS9100D defect traceability).** The same rule
> the shop floor enforces (see "Scrap reason is required when scrap is reported" under Shop Floor) now
> guards the four office/admin work-order endpoints that can write scrap. On each, `scrap_reason` is
> **required whenever the request writes a positive scrap quantity** (`quantity_scrapped > 0`); a
> missing, `null`, or blank/whitespace-only reason in that case is rejected with
> **422 Unprocessable Entity** (`"scrap_reason is required when quantity_scrapped is greater than 0"`).
> When the scrap quantity is **0** (or scrap is left untouched), `scrap_reason` stays **optional**.
> The four endpoints:
> - **`PUT /work-orders/{id}`** (`WorkOrderUpdate`) — body gained an optional `scrap_reason` (max 255).
>   `quantity_scrapped` is optional on this partial update, so an update that doesn't touch scrap is
>   never forced to supply a reason.
> - **`PUT /work-orders/operations/{id}`** (`WorkOrderOperationUpdate`) — body gained an optional
>   `scrap_reason` (max 255), same partial-update semantics. This endpoint **now also writes a
>   tamper-evident `audit_log` row** (`log_update`, resource type `work_order_operation`) on every
>   update — previously it committed with no audit row at all (`GET /audit/`).
> - **`POST /work-orders/{id}/complete`** — gained a `scrap_reason` **query parameter** (alongside
>   `quantity_complete` / `quantity_scrapped`), and (Lean Phase 1) an optional
>   **`scrap_reason_code_id`** query parameter — a predefined code from
>   `GET /quality/scrap-reason-codes` (see Quality). On this endpoint **either** the code **or**
>   non-blank text satisfies the scrap-reason rule (the 422 detail reads `"scrap_reason or
>   scrap_reason_code_id is required when quantity_scrapped is greater than 0"`). The id is
>   validated **before any mutation** — unknown/cross-tenant → **404**, inactive → **422**. An
>   explicit scrap write (`quantity_scrapped` sent) **replaces** the stored categorization wholly:
>   `work_order.scrap_reason_code_id` is set to the sent code, or `null` when none was sent (unlike
>   the shop-floor paths' never-clear semantics — this verb states the WO's final scrap facts). Old
>   and new values ride the tamper-evident audit row.
> - **`POST /work-orders/operations/{id}/complete`** — gained a `scrap_reason` **query parameter**;
>   this path also now rejects a **negative** `quantity_scrapped` with **400 Bad Request**
>   (`"quantity_scrapped cannot be negative"`), matching `/work-orders/{id}/complete`.
>
> The `422` is enforced at the data boundary (Pydantic body validator on the two `PUT` bodies; an
> in-handler guard on the two query-param `complete` verbs), so a scripted/API client can no longer
> record reasonless scrap that the office/admin UIs already block. `scrap_reason_code_id` is accepted
> **only** on `/work-orders/{id}/complete` — the other three office endpoints remain free-text-only.

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
  "created_at": "2024-01-01T10:00:00Z",
  "updated_at": "2024-01-01T10:00:00Z"
}
```

### Laser Nests

Laser nests are the per-sheet laser-cutting tasks on the **child laser work order** of an
assembly WO. Each nest is backed by a clock-in-able `LASER` operation. There are two ways to
create nests, and per the product decision they are used **one or the other per job — never
mixed**:

1. **Package import** — upload a zipped Ermaksan/CNC package (or point at a server folder) and
   the system extracts one nest per CNC file. The package may be either CNC **program files**
   (fields inferred from filenames, as before) or nest-report **PDFs** (fields auto-extracted by
   AI — see "PDF auto-extraction" below). (Mounted under `/work-orders/{id}/laser-nest-packages/…`.)
2. **Manual entry** — key one nest at a time, with an optional reference PDF. Dropping a nest-report
   PDF into the create modal auto-fills the fields via `POST /laser-nests/extract` (see below).
   (The `…/manual` create lives under work orders; per-nest edit/delete/PDF routes live under
   `/laser-nests/{id}/…`.)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/work-orders/{id}/laser-nest-packages/preview` | Preview nests detected from a zipped package or server folder (writes nothing). PDF packages run AI extraction per sheet | Admin / Manager / Supervisor |
| POST | `/work-orders/{id}/laser-nest-packages/import` | Import a package — creates the child laser WO and one nest operation per CNC file (or per confirmed PDF row) | Admin / Manager / Supervisor |
| POST | `/laser-nests/extract` | Auto-extract nest fields (CNC #, material, size) from a single uploaded nest PDF. Stateless — no DB write, no audit | Admin / Manager / Supervisor |
| POST | `/work-orders/{id}/laser-nests/manual` | Manually add **one** nest to an assembly WO. Creates a clock-in-able `LASER` operation on the child laser WO | Admin / Manager / Supervisor |
| PATCH | `/laser-nests/{id}` | Edit a manual nest (all fields optional) | Admin / Manager / Supervisor |
| POST | `/laser-nests/{id}/attach-document` | Attach an already-uploaded PDF Document to the nest (PDF-only) | Admin / Manager / Supervisor |
| DELETE | `/laser-nests/{id}/document` | Detach the PDF (clears the FK; the Document row is left intact) | Admin / Manager / Supervisor |
| GET | `/laser-nests/{id}/document` | Serve the attached PDF **inline** for operator preview | Yes (any authenticated user) |
| DELETE | `/laser-nests/{id}` | Soft-delete the nest; its operation goes `ON_HOLD` | Admin / Manager / Supervisor |

> **Package import replaces everything (`POST …/laser-nest-packages/import`).** Importing a
> package **replaces all existing nests on the child laser WO — including any manually-entered
> ones** — rebuilding the nest operations from the package plan. This is by design (manual *or*
> import per job, never mixed); an import is authoritative and supersedes prior manual entry.
> The wipe is now **fully audited**: each superseded nest is written as a `log_delete`
> (`reason="superseded_by_reimport"`) **before** the rebuild, and each rebuilt nest as a
> `log_create` — for **both** import shapes (the legacy CNC-program path now also writes the per-nest
> `log_create` with `source="cnc_file_import"`; the PDF path uses `source="pdf_import"`). The audit
> rows commit atomically with the rebuild.
>
> **PDF auto-extraction (CNC #, material, material size).** Nest-report PDFs (SigmaNEST / Ermaksan
> style) are read automatically; the planner verifies before saving. Extraction is **layout-aware
> (vision)**: the PDF bytes are sent to Claude as a base64 `document` content block so the model
> reads the rendered sheet with its 2-D layout (PDFs over a ~20 MB native cap, or whose bytes can't
> be read, fall back to flattened-text extraction). Two entry points, both gated to
> **Admin / Manager / Supervisor** and both **AI-always** via the shared `run_llm_task` pipeline
> (prompt `laser_nest_extraction` 1.1.0, `feature="laser_nest_extraction"`, one tenant-scoped
> `ai_usage_events` row per call — telemetry, not audit):
>
> - **Single-PDF (`POST /laser-nests/extract`).** Multipart `file` (PDF; non-PDF → **400**).
>   **Stateless — no DB write, no audit**; `company_id` flows through only for usage telemetry.
>   Used by the manual-create modal to auto-fill fields from a dropped PDF. Returns
>   `{ cnc_number, material, thickness, sheet_size, planned_runs, confidence, source, warning }`
>   where `source` ∈ `{ai, filename}` and `confidence` ∈ `{high, medium, low}` (overall).
>   Declared as a static `/extract` route so it matches ahead of the dynamic `/{laser_nest_id}` routes.
>
> - **Batch ZIP (`…/laser-nest-packages/preview` → `…/import`).** A package is treated as a **PDF
>   package** iff it contains any `*.pdf` (PDFs and CNC extensions are disjoint); otherwise the
>   legacy CNC-program path runs unchanged. **Review-before-commit:** `preview` runs AI once per
>   sheet (parallelized, bounded concurrency) and returns editable rows — beyond the existing
>   `nest_name` / `cnc_file_name` / `planned_runs` / `material` / `thickness` / `sheet_size`, PDF
>   rows also carry **`source_file`** (the PDF's path within the package), **`cnc_number`**, and
>   **`confidence`**. The planner edits/confirms in the wizard, then `import` re-sends the same ZIP
>   **plus an optional `rows` form field** — a JSON array of confirmed rows
>   `{source_file, cnc_number, nest_name, planned_runs, material, thickness, sheet_size}`. When
>   `rows` is present, the backend matches each row to its PDF by `source_file`, stores each PDF as
>   a `DRAWING` `Document` (attached via `document_id`), sets `cnc_number`, writes one `log_create`
>   audit row per nest, and builds the child laser WO — **no second AI call** (the re-sent ZIP only
>   supplies PDF bytes). When `rows` is absent, the legacy CNC-file import is unchanged.
>
>   `rows` is **strictly validated** before anything is persisted (`LaserNestImportRow`):
>   `source_file` required (1–1000 chars), `planned_runs` required and **≥ 1**, and
>   `cnc_number` / `nest_name` / `material` / `thickness` / `sheet_size` length-bounded as on the
>   manual path. Import-specific **400** cases: `rows` not valid JSON / not a JSON array; any row
>   failing validation; a **duplicate `source_file`** across rows; and a DB constraint/length fault
>   (`IntegrityError`/`SQLAlchemyError` — e.g. tripping `uq_laser_nests_package_file` — now returns a
>   clean **400** rather than a 500). A `source_file` that escapes the package or is missing from the
>   re-sent ZIP → **400**.
>
> - **50-PDF cap.** A package (or `rows` array) with more than **50** PDFs is rejected with **400**.
> - **Graceful degrade.** A PDF the model can't read falls back to the **filename stem** as the
>   `cnc_number` (`05749.pdf` → `05749`) with a `warning` and `source="filename"` at low confidence —
>   one bad sheet never hard-fails a batch. The native-PDF (vision) path reads scanned/image-only
>   sheets directly; only when it can't (>20 MB cap or unreadable bytes) does the flattened-text
>   fallback run (with its OCR step in `pdf_service`).
>
> **Manual nest create (`POST /work-orders/{id}/laser-nests/manual`).** Body: `cnc_number`
> (required, 1–100 chars), `planned_runs` (required, **≥ 1**), and optional `nest_name`,
> `material`, `thickness`, `sheet_size`. Resolves (or creates) the child laser WO and an active
> laser work center — **400** if no active laser work center exists. The first nest on the child
> is created **READY** (clock-in-able now); subsequent nests are **PENDING**. This is a standalone
> creation path that **does not change** the package-import behavior. Returns **201** with the new
> nest plus its backing operation (`work_order_operation_id`, `operation_status`).
>
> **Manual nest edit (`PATCH /laser-nests/{id}`).** All-optional body (`cnc_number`, `nest_name`,
> `planned_runs`, `material`, `thickness`, `sheet_size`). A `planned_runs` change **reverse-syncs**
> the operation's `component_quantity` and re-derives the child laser WO's `quantity_ordered` over
> its non-deleted nests. Lowering `planned_runs` below `completed_runs` is allowed (over-run is
> acceptable); only the schema's `ge=1` floor applies.
>
> **Reference PDF (attach / detach / preview).** The attached PDF is a plain **shop-reference
> drawing** — optional, with **no approval workflow**, and it **never gates clock-in**. Attach
> references a Document already uploaded via `POST /documents/upload`; non-PDF documents are
> rejected with **400**. `GET /laser-nests/{id}/document` serves it `Content-Type: application/pdf`,
> `Content-Disposition: inline` so the kiosk/operator station can preview it; **404** if none is
> attached. Detach only clears the FK — the Document row and its stored bytes survive.
>
> **Soft delete (`DELETE /laser-nests/{id}`).** Soft-deletes the nest (`SoftDeleteMixin`; never a
> hard delete) and sets its operation to **`ON_HOLD`**, which removes it from the operator/work-center
> queue and the child WO's quantity rollup. Soft-deleted nests are filtered out of `WorkOrderResponse`
> operations, the operator queue, and the quantity rollup.
>
> **Compliance.** All of these writes are **tenant-scoped** by `company_id` (a cross-tenant or
> soft-deleted id returns **404**) and recorded in the tamper-evident audit trail (`GET /audit/`)
> via `AuditService` — create/edit/attach/detach as updates, delete as a soft-delete record.
>
> **`LaserNestOperationInfo` (embedded in `WorkOrderResponse` operations) gained fields:**
> `cnc_number`, `document_id`, `has_document` (bool), and `document_file_name`. `cnc_file_name`
> is now **nullable** — a manual nest has no uploaded CNC file.
>
> **Operator-facing nest payload.** The operator reads `GET /shop-floor/work-center-queue/{id}` and
> `GET /shop-floor/my-active-job` embed the same nest as a `laser_nest` object (carrying these new
> fields) so the active nest shows at clock-in — see Shop Floor → "Laser-nest payload on operator
> reads".

### Parts

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/parts/` | List all parts | Yes |
| POST | `/parts/` | Create part | Yes |
| GET | `/parts/{id}` | Get part by ID | Yes |
| PUT | `/parts/{id}` | Update part | Yes |
| DELETE | `/parts/{id}` | Delete part (soft delete — restorable) | Admin |
| POST | `/parts/{id}/restore` | Restore a soft-deleted part | Admin / Manager |
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
  "created_at": "2024-01-01T10:00:00Z"
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

#### BOM Import (document upload)

AI-assisted BOM/part import from an uploaded document — a separate flow from the
[Bulk Imports kit](#bulk-imports--templates-excel-migration-kit). Excel uploads are parsed
directly into a reviewable table plus a suggested column mapping (no LLM call); PDF/Word
uploads go through text extraction + LLM. See
[docs/EXCEL_MIGRATION_RUNBOOK.md](EXCEL_MIGRATION_RUNBOOK.md) Step 7 for the migration flow.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/bom/import/preview` | Upload a BOM/part document (PDF/DOC/DOCX/XLSX/XLS), get a reviewable preview (Excel: raw table + suggested mapping; PDF/Word: LLM extraction) | Admin / Manager / Supervisor |
| POST | `/bom/import/commit` | Commit a reviewed preview payload — creates parts, the BOM, and BOM items | Admin / Manager / Supervisor |
| POST | `/bom/import` | One-shot upload → LLM extraction → create parts/BOM items (`create_missing_parts` form flag, default `true`) | Admin / Manager / Supervisor |

> **Excel scanning is bounded** with the same caps as the shared Bulk Imports parser: at most
> **256 columns** are read per row, more than **10,000 collected data rows** refuses the file
> (**400**), and scanning more than **100,000 raw rows** workbook-wide refuses the file (**400**).
> Two deliberate differences from the Import Center parser: **all sheets are read** (not just the
> first worksheet), and a run of more than **1,000 consecutive blank rows ends that sheet's scan
> only** — scanning continues with the next sheet, and there is **no refusal** for data sitting
> past such a gap (BOM spreadsheets legitimately scatter data blocks down a sheet; the preview
> shows exactly which rows parsed before anything is committed). The header row is padded to the
> widest data row, so unheadered trailing data columns remain mappable in the preview.
> Corrupt/unreadable Excel returns **400** `"Could not read the Excel file. Re-save it as a
> standard Excel workbook."`. On `POST /bom/import` (the LLM path), Excel **text extraction
> degrades gracefully** at the scan cap — partial text at `"medium"` confidence — rather than
> refusing the file.

> **Commits are audited.** `POST /bom/import` and `POST /bom/import/commit` write tamper-evident
> `audit_log` entries via `AuditService` tagged `extra_data.source = "bom_import"`: one CREATE per
> part created (assembly or component), one UPDATE when an existing part is promoted to
> `part_type = assembly`, and one CREATE for the BOM with its items summarized on the parent row
> (`item_count` + `component_part_numbers` in `extra_data` — the same parent-row pattern as the
> WO/PO imports' audit rows). Audit rows are flushed before the terminal commit so they persist
> atomically with the import. `POST /bom/import/preview` writes nothing.

> **Conflicts are refused with actionable 400s** rather than silently reusing soft-deleted rows or
> dying with an IntegrityError **500**. On refusal the whole import rolls back — no partial
> parts/BOM (or their audit rows) persist. The cases: an assembly or component part number matching
> a **soft-deleted part** → `"Part 'X' matches a deleted part. Restore it from Parts (or use a
> different part number) and re-import."` (the deleted row still owns the number — same contract as
> `POST /parts/`, recoverable via `POST /parts/{id}/restore`); a **deleted BOM** on the assembly
> part → `"A deleted BOM exists for part 'X' — restore it before importing."`; an **inactive BOM**
> → `"An inactive BOM exists for part 'X' — reactivate or delete it before importing."` (previously
> an IntegrityError 500); an **active BOM** → `"A BOM already exists for assembly part 'X'"` (now
> names the part).

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
| POST | `/routing/{id}/release` | Release a draft routing for production (status → `released`, stamps `approved_by`/`approved_at`/`effective_date`) | Admin / Manager |
| POST | `/routing/{id}/copy` | Copy a routing to a target part or new revision as a new **draft** — query params `target_part_id` (required) and `new_revision` (default `A`); copies all operations incl. `process_sheet_id`; **404** if the source routing or target part isn't found; writes one tamper-evident `audit_log` CREATE for the new routing (`extra_data.copied_from` = source routing id) | Admin / Manager |
| POST | `/routing/{id}/operations` | Add an operation (**400** on a released routing) | Admin / Manager / Supervisor |
| PUT | `/routing/{id}/operations/{operation_id}` | Update an operation — draft: all fields; released: **time standards only** (see note) | Admin / Manager / Supervisor (released-routing edits: Admin / Manager) |
| DELETE | `/routing/{id}/operations/{operation_id}` | Delete an operation (**400** on a released routing) | Admin / Manager / Supervisor |
| POST | `/routing/{id}/operations/reorder` | Reorder operations (**400** on a released routing) | Admin / Manager / Supervisor |
| POST | `/routing/import/preview` | Upload a routing CSV/XLSX (multipart `file`), preview it WITHOUT writing (dry-run, fully rolled back) | Admin / Manager / Supervisor |
| POST | `/routing/import/commit` | Commit a routing CSV/XLSX import — one draft routing per part, with one `audit_log` CREATE per routing | Admin / Manager / Supervisor |

> **Editing a RELEASED routing's operations — time standards only (`feat/routing-editable-time-standards`).**
> A released routing's manufacturing **process** is frozen on release: `PUT /routing/{id}/operations/{operation_id}`
> (`update_operation`) accepts in-place edits only to the **time-standard** fields — `setup_hours`,
> `run_hours_per_unit`, `move_hours`, `queue_hours`, `cycle_time_seconds`, `pieces_per_cycle`. Any
> other changed field on a released routing returns **400** (*"Released routing: only time standards
> (setup, run/unit, move, queue, cycle) can be edited — create a new revision to change the
> process."*) — change the work center, instructions, sequence, inspection points, or the set of
> operations by creating a **new revision** instead. Adding / deleting / reordering operations on a
> released routing also returns **400**; an **obsolete** routing is fully locked (**400**). The
> released-edit path is gated **in code** to **Admin / Manager** — a **Supervisor** receives **403**
> (*"Editing a released routing's time standards requires the Admin or Manager role."*), matching the
> `/release` role set (superuser / Platform Admin bypass). On a **draft** routing every field is
> editable by Admin / Manager / Supervisor. Every applied change writes a tamper-evident `audit_log`
> UPDATE (old→new values); a successful **released** time-standard edit also re-stamps
> `routing.approved_by` / `approved_at` (the editor / now), leaving `effective_date` and the revision
> letter unchanged. See [docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) → Routings.

> **Attaching a process sheet to an operation (`feat/process-sheets-library`).** Routing operations
> carry an optional **`process_sheet_id`** (on create, update, and every operation response) that
> attaches a Process Sheets library entry by reference (see Process Sheets below). The attach target
> is validated on `POST /routing/{id}/operations` and `PUT /routing/{id}/operations/{operation_id}`:
> a sheet that doesn't exist in the **active company** (missing, cross-tenant, or soft-deleted)
> returns **404** (*"Process sheet not found"*); a sheet that is not **RELEASED** returns **409**
> (*"Only a released process sheet can be attached (sheet PS-000123 is draft)"*) — only released
> inspection content may reach a traveler. Sending an explicit `process_sheet_id: null` on update
> **detaches** (no validation needed). `process_sheet_id` is a structural (process) field, so on a
> **released** routing changing it returns **400** like any non-time-standard field — the attach
> validation is only reachable on a draft. `POST /routing/{routing_id}/copy` carries
> `process_sheet_id` onto the copied draft's operations. The attached sheet is snapshotted onto WO
> operations at WO creation in a later PR (see
> [docs/PROCESS_SHEETS_SCOPE.md](PROCESS_SHEETS_SCOPE.md)); in this PR the field is a validated
> reference only.

> **Routing import (CSV/XLSX).** Both endpoints are multipart uploads with two form fields:
> `file` (the CSV or XLSX upload, via the shared `parse_import_file`) and an optional `assignments`
> field. `assignments` is a **JSON string** mapping a source file **row number → `work_center_id`**
> (e.g. `{"2": 5, "3": 5, "4": 7}`); keys and values must both be integers. Malformed JSON or
> non-integer keys/values return **HTTP 400** — JSON booleans are rejected too (`{"2": true}` is a
> 400, not silently coerced to `work_center_id: 1`). An `assignments` entry is **authoritative for
> its row**: it supplies the work center for an operation whose file `work_center_code` is blank,
> **and overrides** the file code on a row that has one. Preview accepts it too (to re-validate the
> UI's choices before commit) but works with none.
>
> Both endpoints return `RoutingImportResponse` (`app/schemas/routing_import.py`): `dry_run`,
> `total_rows`, `parts_detected`, `routings_created`, `total_operations`,
> `operations_needing_work_center` (count of operations with no work center resolved yet),
> `skipped_count`, `created_ids[]`, `results[]`, and `errors[]` (`RoutingImportError`: `row`,
> `part_number`, `reason`). Each `results[]` entry (`RoutingImportRowResult`) carries `rows[]`,
> `part_number`, `routing_revision`, `routing_id` (`null` in dry-run), `operation_count`,
> `total_setup_hours`, `total_run_hours_per_unit`, `status` (always `"draft"`), and an
> `operations[]` array of per-operation detail (`RoutingImportOperation`: `row`, `sequence`,
> `operation_name`, `work_center_code` (`null` if blank), `work_center_id` (`null`),
> `work_center_name` (`null`), `needs_work_center` (`true` when no valid work center is resolved
> yet), `setup_hours`, `run_hours_per_unit`, `is_inspection_point`, `is_outside_operation`) — this
> drives the wizard's per-operation work-center dropdown.
>
> Columns (in order): `part_number`, `routing_revision` (default `A`), `routing_description`,
> `sequence` (int, **unique within a part**), `operation_name`, `work_center_code` (**OPTIONAL** —
> see below), `setup_hours`, `run_hours_per_unit` (numeric, default 0), `description`,
> `is_inspection_point`, `is_outside_operation` (`Y/N`/`true/false`, default false). Required per
> row: `part_number`, `sequence`, `operation_name`.
>
> **`work_center_code` is optional.** A **blank/missing** code is **not** an error — it means
> "assign the work center in the wizard after upload" (the operation comes back with
> `needs_work_center: true`). A **non-blank** code must still resolve to an **active**,
> tenant-scoped work center, or that row errors (likely a typo). Each operation's work center is
> resolved by precedence: (a) an `assignments` entry for that operation's row **wins and overrides
> any file `work_center_code`** on that row (the assigned `work_center_id` must be an active,
> tenant-scoped work center — unknown/cross-tenant/inactive errors that row); else (b) a non-blank
> file `work_center_code` is resolved by code; else (c) the operation is left unassigned. The file
> `work_center_code` is just a **default that pre-fills the wizard dropdown** — an explicit
> assignment always overrides it. (A preview with no `assignments` still resolves the file code, so
> the wizard pre-fills from the file.) If **any** operation in a routing still has no work center
> after assignments, that routing is reported in `errors[]` (naming the unassigned rows) and is
> **NOT created** — no routing is ever created with an unassigned operation. (Dry-run preview leaves
> unassigned operations as `needs_work_center` rather than erroring.)
>
> Rows are grouped by `part_number` into **one draft routing each** (first-seen order). The part
> must **pre-exist** and be a manufactured/assembly part, not soft-deleted — **parts are never
> created**. A part that **already has a routing at the same revision** is refused ("choose a new
> revision"); any other revision creates a **new draft revision alongside** the existing ones,
> which are **never mutated or deactivated** (compliance: prefer new revisions over editing shipped
> data). A duplicate `sequence` within a part is an error. Commit is **per-routing** (partial
> success — one bad routing never poisons the rest); each created routing writes one tamper-evident
> `audit_log` CREATE (`resource_type = "routing"`, `extra_data.source = "import"`).
> `POST /routing/import/preview` (dry-run) writes nothing — every routing runs inside a SAVEPOINT
> that is rolled back, with a terminal `db.rollback()` backstop. See
> [docs/EXCEL_MIGRATION_RUNBOOK.md](EXCEL_MIGRATION_RUNBOOK.md) Step 8 for the migration flow.

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
  "notes": "Use roughing tool",
  "process_sheet_id": null
}
```

### Process Sheets

Typed, revision-controlled operation-step documents ("process sheets") authored in engineering and
attached by reference to routing operations (see the Routing attach note above). Library CRUD +
lifecycle only in this PR — the WO-creation snapshot and shop-floor per-step capture land in later
PRs (see [docs/PROCESS_SHEETS_SCOPE.md](PROCESS_SHEETS_SCOPE.md)).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/process-sheets/` | List process sheets, all revisions, newest sheet number first (`?status=`, `?search=` on number/title, `skip`/`limit` paging) | Yes |
| GET | `/process-sheets/{id}` | Get a process sheet with its steps | Yes |
| POST | `/process-sheets/` | Create a process sheet (status `draft`, Rev `A`, auto-numbered `PS-000123`). Body: `{"title", "description"}` | Admin / Manager / Supervisor / Quality |
| PATCH | `/process-sheets/{id}` | Update sheet header fields (`title` / `description`) — **409** unless the sheet is a draft | Admin / Manager / Supervisor / Quality |
| DELETE | `/process-sheets/{id}` | Soft-delete a **draft** sheet (**409** for released/obsolete — obsolete those instead) | Admin / Manager / Supervisor / Quality |
| POST | `/process-sheets/{id}/release` | Release a draft sheet (status → `released`, stamps `effective_date`; **400** with no steps, **409** if not a draft) | Admin / Manager / Quality |
| POST | `/process-sheets/{id}/obsolete` | Obsolete a released sheet (status → `obsolete`, stamps `obsolete_date`, clears `is_active`; **409** if not released) | Admin / Manager / Quality |
| POST | `/process-sheets/{id}/new-revision` | Copy a released/obsolete sheet **and its steps** to a new draft row with the next revision letter (**409** on a draft — edit it directly — or when a draft revision of the sheet already exists) | Admin / Manager / Supervisor / Quality |
| POST | `/process-sheets/{id}/steps` | Add a typed step to a **draft** sheet (**409** otherwise; per-type config validation — see note) | Admin / Manager / Supervisor / Quality |
| PATCH | `/process-sheets/{id}/steps/{step_id}` | Update a step on a **draft** sheet — the merged (effective) definition is re-validated, not just the delta | Admin / Manager / Supervisor / Quality |
| DELETE | `/process-sheets/{id}/steps/{step_id}` | Delete a step from a **draft** sheet (hard delete — steps only exist on drafts) | Admin / Manager / Supervisor / Quality |

> **Draft-only mutability (409 semantics).** Only a **draft** sheet is mutable — header updates,
> step add/edit/delete, and delete of the sheet itself all return **409** on a released or obsolete
> sheet (*"Cannot update a released process sheet — only drafts are editable. Create a new revision
> to change released content."*). Released content changes go through `POST
> /process-sheets/{id}/new-revision`, which mirrors routing revisions: revisions are separate rows
> sharing `sheet_number`, with Excel-style letter increments (`A` → `B` → … → `Z` → `AA`), and at
> most **one draft revision per sheet family** at a time (**409** otherwise). Sheet numbers are
> generated per company under an advisory lock and **never reused** (soft-deleted sheets still hold
> their number). Every mutation writes a tamper-evident `audit_log` row (create / update /
> soft-delete / status change).
>
> **Roles.** Authoring (create / header edit / step CRUD / delete / new-revision) is **Admin /
> Manager / Supervisor / Quality**; release and obsolete are **Admin / Manager / Quality** (quality
> owns released inspection documents); GETs are any authenticated user (tenant-scoped). See
> [docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) → Process Sheets.
>
> **Step schema + per-type `config` validation.** A step is `{"sequence"` (int > 0)`, "label",
> "instruction_text", "step_type", "is_required", "config", "requires_gauge",
> "spc_characteristic_id"}` with `step_type` one of `measurement | checkbox | list | value | photo |
> file | instruction`. The service validates the per-type shape (**400** on violation):
> `measurement` requires a `config` with **numeric `lsl` / `nominal` / `usl`** satisfying
> `lsl <= nominal <= usl` and `lsl < usl`; `list` requires a `config` with a non-empty `options`
> array; `requires_gauge` is valid **only** on measurement steps; `spc_characteristic_id` is
> measurement-only and must resolve to an SPC characteristic in the active company (**404**
> otherwise). `instruction` steps are display-only and **never required** — the server forces
> `is_required: false` regardless of the payload. Step updates validate the **merged** (existing +
> payload) definition so a partial payload can't sneak an invalid combination past per-field checks.

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
| GET | `/shop-floor/work-center-queue/{id}` | Get work center queue, each row carrying the live crew `roster` (see note below) | User **or** kiosk station token |
| GET | `/shop-floor/wallboard` | Read-only TV wallboard snapshot (`?dept=` narrows to one work-center type, case-insensitive) | User **or** display token |
| POST | `/shop-floor/kiosk-stations/station-login` | Unlock a crew tablet with the shared station PIN. Body `{"station_id", "pin"}` (PIN 4–8 digits) → `{"access_token", "token_type", "expires_in", "station": {"id", "label", "work_center_id", "work_center_code", "work_center_name"}}` (24 h scoped `type="kiosk"` JWT). Bad/revoked station or wrong PIN → **401** (indistinguishable; failed attempt audited) | **Public** (PIN-gated, 5/minute per IP) |
| POST | `/shop-floor/kiosk-stations` | Create a PIN-protected crew-station kiosk bound to a work center. Body `{"label", "work_center_id", "pin"}` → **201** `KioskStationResponse` (PIN hashed, never echoed; a work center outside the active company → **404**) | Admin / Manager |
| GET | `/shop-floor/kiosk-stations` | List this company's kiosk stations (no PIN/`pin_hash`) → `{"stations"}` | Admin / Manager |
| POST | `/shop-floor/kiosk-stations/{id}/revoke` | Revoke a kiosk station (idempotent status flip; tablet loses access next request) → `KioskStationResponse` | Admin / Manager |
| POST | `/shop-floor/kiosk-stations/{id}/reset-pin` | Re-hash a kiosk station's shared PIN. Body `{"pin"}` → `KioskStationResponse` | Admin / Manager |

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
> qty_target}`), `late_wos[]`, `blocked_wos[]` (tickers capped at 25), an optional **`kpi_strip`**
> block (trailing-30-day floor KPIs — `otd_ship_pct_30d`, `fpy_pct_30d`, `scrap_pct_30d`,
> `open_wip_count`, `avg_wip_age_days`; company-wide, never narrowed by `?dept=`; values are
> ~5-minute server-side cached and each nullable = insufficient data; the whole block is `null` only
> when its computation failed — see [docs/WALLBOARD.md](WALLBOARD.md) → KPI strip), and
> `generated_at`. Token issuance/revocation: see Authentication → Display tokens. Operating a TV:
> see [docs/WALLBOARD.md](WALLBOARD.md).

> **Crew roster on `GET /shop-floor/work-center-queue/{id}` (crew-station kiosk).** The queue read
> accepts **either** a normal user access token **or** a crew-station kiosk token (the dedicated
> `get_kiosk_or_user` dependency — the only *endpoint dependency* that honors `type="kiosk"` JWTs;
> the badge-token mint validates the station token itself against the same DB-row checks). A station
> may only read **its own** work center's queue (any other id → **403**, "Kiosk station may only
> read its own work center queue"); users read any queue in their company, as before. Each queue
> row now carries `quantity_scrapped` (feeding the kiosk's crew tally, "37 of 50 · 2 scrap") and a
> `roster` array of the operation's **open labor** TimeEntries (labor entry types only — an open
> BREAK/DOWNTIME row never renders as a crew member), each
> `{time_entry_id, user_id, operator_name, employee_id, entry_type, clock_in}` with
> `operator_name` in the public-screen-safe "First L." form. The response adds top-level
> `server_time` (UTC ISO — the kiosk anchors its per-person timers to the server clock) and
> `station` (`{id, label}` for a station caller, `null` for users). The response also carries a
> top-level **`scrap_reason_codes`** array — the tenant's **active** scrap reason codes
> (`{id, code, name, category, display_order}`, in display order) — so the crew station's scrap
> picker works **without widening any token scope**: the station token is still honored only by
> this read + the badge mint, and badge-minted kiosk tokens (path-fenced to `/shop-floor`) cannot
> call `GET /quality/scrap-reason-codes`. Old clients ignore the extra key. Station lifecycle + PIN
> model: see the `/shop-floor/kiosk-stations` rows above and [docs/KIOSK.md](KIOSK.md) → Crew
> station mode.
>
> **`closed_time_entries` on `POST /shop-floor/operations/{id}/complete`.** When a completion is
> fully complete it auto-closes **every** operator's open time entry on the operation (existing
> behavior); the response now names them —
> `closed_time_entries: [{time_entry_id, user_id, operator_name}]`, empty on a partial/progress
> update — so the crew kiosk can toast who was auto-clocked-out. Read-only addition; the
> auto-close mutation is unchanged.

> **Laser-nest payload on operator reads (`/work-center-queue/{id}`, `/my-active-job`).** So the
> kiosk/operator station can surface the laser nest at clock-in, **every `/work-center-queue/{id}` row
> now carries a `laser_nest` object** (it returned none before), and the `laser_nest` that
> `/my-active-job` has always returned **gained four fields** (`cnc_number`, `document_id`,
> `has_document`, `document_file_name`). Both build it from the same `_laser_nest_payload`, so the
> shape is identical and is **`null` for any non-laser operation**. A soft-deleted manual nest never
> appears — the payload routes through `active_laser_nest`. Shape: `{ id, nest_name, cnc_file_name`
> (**nullable** — manual nests have no uploaded CNC file)`, cnc_file_path, cnc_number` (nullable)`,
> planned_runs, completed_runs, remaining_runs, material, thickness, sheet_size, document_id`
> (nullable)`, has_document` (bool — true when a reference PDF is attached)`, document_file_name`
> (nullable) `}`. The attached PDF is served **inline** by `GET /laser-nests/{id}/document` (see Laser
> Nests above), so `has_document` / `document_file_name` let the kiosk flag that a reference PDF is
> attached and label it without a second round-trip.

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
> `POST /shop-floor/operations/{id}/production` accepts a `scrap_reason` string — the
> same shape and destination as the clock-out field (the `TimeEntry.scrap_reason` column,
> 255 max), persisted onto the caller's **active** time entry. It is stored only when the report
> actually carries scrap (`quantity_scrapped_delta > 0`); an omitted/`null` reason never clobbers a
> reason recorded by an earlier in-shift report. When stored, the reason is also appended to the
> tamper-evident `REPORT_OPERATION_PRODUCTION` audit description.
>
> **Structured scrap reason CODE (Lean Phase 1).** Both `POST /shop-floor/clock-out/{id}`
> (`ClockOut`) and `POST /shop-floor/operations/{id}/production` (`ProductionReportRequest`) also
> accept an optional **`scrap_reason_code_id`** — the id of a predefined scrap reason code
> (`GET /quality/scrap-reason-codes`, see Quality below); the free-text `scrap_reason` stays as
> narrative detail alongside it. The id is validated **before any mutation**: an unknown **or
> cross-tenant** id returns **404** (indistinguishable, so a foreign id discloses nothing), an
> **inactive** code returns **422**. Persistence follows the same never-clear semantics as the text
> field: the code is stored on the caller's time entry (`scrap_reason_code_id` on
> `TimeEntryResponse`, `null` = uncoded/legacy row) whenever the write carries one, and onto the
> operation's `scrap_reason_code_id` when the write also carries a positive scrap quantity; a
> code-less write never clears a previously recorded code. A stored code is appended to the
> tamper-evident `REPORT_OPERATION_PRODUCTION` audit description (`"Scrap reason code: <code>"`) and
> rides the `labor_clock_out` event payload (`scrap_reason_code_id`).
>
> **A scrap reason is required when scrap is reported (AS9100D defect traceability).**
> On both `POST /shop-floor/clock-out/{id}` (`ClockOut`) and
> `POST /shop-floor/operations/{id}/production` (`ProductionReportRequest`), a reason is
> **required whenever the request reports a positive scrap quantity** — `quantity_scrapped > 0` on
> clock-out, `quantity_scrapped_delta > 0` on the production report. **Either** a
> `scrap_reason_code_id` **or** a non-blank free-text `scrap_reason` satisfies the rule (the code is
> preferred; text-only clients keep working unchanged). A request with neither — no code, and the
> text missing, `null`, or blank/whitespace-only — is rejected with **422 Unprocessable Entity**
> (`"scrap_reason or scrap_reason_code_id is required when quantity_scrapped is greater than 0"` /
> `"… quantity_scrapped_delta is greater than 0"`). When the scrap quantity is **0**, both stay
> **optional** and may be omitted (e.g. the kiosk COMPLETE flow clocks out with zero scrap and no
> reason). This invariant is enforced at the data boundary, so a scripted/API client can no longer
> record reasonless scrap that the kiosk/desktop UIs already block.
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

`POST /shop-floor/clock-out/{time_entry_id}` body (`ClockOut`):

```json
{
  "quantity_produced": 50,
  "quantity_scrapped": 2,
  "scrap_reason": "Drill bit broke",
  "scrap_reason_code_id": 3,
  "notes": "Replaced drill bit, resumed operation"
}
```

> When `quantity_scrapped` > 0 a reason is **required** — either `scrap_reason_code_id` (a
> predefined code from `GET /quality/scrap-reason-codes`) or a non-blank free-text `scrap_reason`;
> neither present returns **422**. Both stay optional when no scrap is reported. See "A scrap reason
> is required when scrap is reported" and "Structured scrap reason CODE" under the shop-floor notes
> above.

### Scanner (QR / barcode)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/scanner/resolve-action` | Resolve a scanned traveler/badge code into a typed action context (A0.4) | Yes |
| POST | `/scanner/lookup` | Look up a scanned barcode: supplier part number → internal part number → work order number | Yes |
| GET | `/scanner/mappings` | List supplier part-number mappings | Yes |
| POST | `/scanner/mappings` | Create a supplier part-number mapping | Yes |
| DELETE | `/scanner/mappings/{mapping_id}` | Deactivate a supplier part-number mapping | Yes |

> **`POST /scanner/resolve-action` (A0.4 QR traveler / badge scan plumbing).** Every scan surface
> (kiosk, wedge scanner, phone) posts the raw scanned text and gets back a **discriminated union**
> keyed on `kind` — `operation` | `work_order` | `employee` | `unknown`. Request body:
> `{ "code": "<raw scanned text>", "work_center_id": <optional station work center id> }` (`code`
> 1–255 chars, whitespace stripped; `work_center_id` only drives the `work_center_match` flag on
> operation scans — it never widens access). Open to **any authenticated user** — it mirrors the
> read-broad shop-floor reads. Code formats (prefix/scheme matching is case-insensitive):
> - `OP:{operation_id}` — a traveler routing-step code → `kind: "operation"`.
> - `WO:{work_order_number}` — a work-order code → `kind: "work_order"` (exact match, with a
>   case-insensitive exact fallback). Still accepted, though current travelers print URL QRs
>   (below) rather than bare `WO:` codes.
> - **URL-shaped codes** (`http://` / `https://`) — what the printed traveler QRs now encode, so a
>   phone camera opens the app while a wedge gun types the same text into this endpoint. Two URL
>   forms resolve; the **host is deliberately not validated** (travelers may be printed against any
>   deployment origin — the URL carries no tenant authority; tenancy comes from the authenticated
>   caller, same as every other code shape):
>   - a `scan` query param (the per-operation traveler QR, e.g.
>     `{origin}/shop-floor/operations?scan=OP%3A123`) — URL-decoded **one level only** and
>     re-resolved as `OP:` / `WO:` / badge; a `scan` value that is itself a URL is a structured
>     miss.
>   - a `/work-orders/{id}` path (the traveler header QR; trailing slash allowed) — resolves the
>     work order by integer primary key → the same `kind: "work_order"` shape as `WO:{number}`.
>
>   Every result — hit or miss — echoes the **original scanned URL** in `code`, so operators see
>   exactly what was scanned.
> - anything else — probed as an employee badge id (exact match on an **active** user's
>   `employee_id`) → `kind: "employee"`.
> - no match / malformed → `kind: "unknown"` with `{ code, reason }`, returned with **HTTP 200** —
>   a structured miss, not an error, because wedge scanners hit unknown codes constantly.
>
> **`kind: "operation"`** carries an operation summary (sequence, status, WO number/status, part,
> work center, quantities, plus `work_center_match` — true/false when the request named a station,
> `null` otherwise), `legal_actions` — the subset of
> `clock_in | report_production | complete | hold | resume` the **calling user** could perform
> right now — and `blockers`, a map of action → human-readable reasons, present only for actions
> **not** in `legal_actions`. The gating is derived from the same predicates the live shop-floor
> write endpoints enforce (`app/services/operation_action_gates.py`, extracted from those
> handlers, which now call the same helpers), and it **mirrors the live endpoints' gating
> verbatim — clients should treat blocker text as display-ready** (a kiosk showing a resolver
> blocker and a kiosk showing the endpoint's 400 show the same message).
>
> **Routing-staleness warning is a documented proxy.** `warning: "routing_revision_changed"` (with
> the accompanying `routing_revision_check` object) flags that the part's current **released**
> routing was released after the work order's release/creation baseline — i.e. any traveler
> printed from this WO predates the routing now in force. This is **timestamp inference, not an
> exact check**: work orders do not snapshot the routing revision their operations were generated
> from, and traveler prints are not recorded server-side. `routing_revision_check` carries the
> current released routing's `current_released_revision`, the boolean
> `released_routing_changed_after_wo_creation` (`null` when either side lacks a usable timestamp),
> the `checked_against` baseline (WO `released_at`, else `created_at`), and a `note` restating the
> proxy semantics. An exact check requires a WO-level routing snapshot (pending; not in the data
> model today).
>
> **`kind: "work_order"`** returns the WO summary plus its operation list (id / sequence /
> operation number / name / status) and `current_operation_id` — the first non-complete operation
> by sequence (computed, not the stale column).
>
> **`kind: "employee"` is a lookup ONLY** — `{ employee_id, first_name, last_initial }`, no
> tokens, no session, **no auth side effects**. Badge **login** stays exclusively on
> `POST /auth/employee-login`.
>
> **Read-only / zero-write.** Resolving a scan writes **no audit rows** and emits **no operational
> events** — it has GET semantics in a POST body (POST keeps raw scanner text out of URLs and
> access logs). **Tenant-scoped:** every lookup filters the active company; a code that exists in
> another tenant — and a soft-deleted work order (or an operation whose WO is soft-deleted) —
> resolves to `kind: "unknown"` exactly like a code that exists nowhere.

### Quality

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/quality/inspections/` | List inspections | Yes |
| POST | `/quality/inspections/` | Create inspection | Yes |
| GET | `/quality/inspections/{id}` | Get inspection by ID | Yes |
| POST | `/quality/inspections/{id}/approve` | Approve inspection | Quality |
| GET | `/quality/scrap-reason-codes` | List scrap reason codes (active only by default; `category` / `include_inactive` filters) | Yes |
| POST | `/quality/scrap-reason-codes` | Create a scrap reason code | Admin / Manager / Quality |
| PUT | `/quality/scrap-reason-codes/{reason_code_id}` | Update a scrap reason code (deactivate via `is_active: false`) | Admin / Manager / Quality |

> **Scrap reason codes (Lean Phase 1).** The tenant's structured scrap vocabulary, referenced by the
> optional `scrap_reason_code_id` accepted on the three scrap write paths —
> `POST /shop-floor/clock-out/{id}`, `POST /shop-floor/operations/{id}/production`, and
> `POST /work-orders/{id}/complete` (see those sections). Shape:
> `{id, code, name, category, description, is_active, display_order}`; `category` is one of
> `material | machine | tooling | operator | setup | programming | engineering | supplier | handling |
> other`. `code` is unique **per tenant** — a duplicate returns **400** (`"Scrap reason code already
> exists"`). Reads are open to any authenticated user (the kiosk/desktop scrap pickers); writes are
> role-gated to **Admin / Manager / Quality** and write tamper-evident `audit_log` rows (resource type
> `scrap_reason_code`). There is deliberately **no DELETE endpoint** — historical scrap rows reference
> these ids (traceability), so retirement is `is_active: false`, never a row removal.

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
| GET | `/purchasing/vendors` | List vendors (`active_only` default true; `approved_only` default false) | Yes |
| POST | `/purchasing/vendors` | Create vendor | Admin / Manager |
| GET | `/purchasing/vendors/{vendor_id}` | Get vendor by ID | Yes |
| PUT | `/purchasing/vendors/{vendor_id}` | Update vendor — `code` is editable (see note) | Admin / Manager |
| GET | `/purchasing/purchase-orders` | List purchase orders (filters: `status`, `vendor_id`) | Yes |
| POST | `/purchasing/purchase-orders` | Create purchase order with its lines | Admin / Manager / Supervisor |
| GET | `/purchasing/purchase-orders/{po_id}` | Get PO by ID | Yes |
| PUT | `/purchasing/purchase-orders/{po_id}` | Update purchase order | Admin / Manager / Supervisor |
| POST | `/purchasing/purchase-orders/{po_id}/send` | Issue a PO to the vendor — status → `sent`, stamps `order_date`; only `draft`/`approved` POs (else **400**) | Admin / Manager |
| POST | `/purchasing/purchase-orders/{po_id}/lines` | Add a line to a `draft` PO (else **400**) and roll the PO subtotal/total | Admin / Manager / Supervisor |

> Material receiving and incoming inspection are **not** under `/purchasing`. They live under
> `/receiving` (see below). The duplicate `/purchasing/receiving*` endpoints were removed.
> The AI PO/quote document-upload flow is likewise not under `/purchasing` — it lives at
> `/po-upload` (see **PO Upload** below).
>
> **Vendor `code` is editable on update.** `PUT /purchasing/vendors/{vendor_id}` accepts an optional
> `code` (2–20 chars: letters, digits, hyphens; lowercase input is normalized to uppercase). The new
> code must stay unique within the company (**400** "Vendor code already exists") and cannot be
> blanked: an explicit JSON `null` returns **400** "Vendor code cannot be blank", while an empty or
> whitespace-only string fails schema validation (**422**, min length checked after strip). Vendor
> **creates and updates** both write to the tamper-evident `audit_log` (`GET /audit/`) — the direct
> `POST` create, `PUT` updates, and the per-row audit of CSV/XLSX-imported vendor creates.
>
> **PO writes are audited.** The interactive purchase-order write endpoints record to the
> tamper-evident `audit_log` (`GET /audit/`): create writes one CREATE row for the PO (resource type
> `purchase_order`; vendor code + line count in `extra_data`, no per-line rows at document creation);
> `PUT` writes an UPDATE row with the changes diff (a no-change PUT writes none); `/send` writes a
> STATUS_CHANGE row (`draft`/`approved` → `sent`, stamped `order_date` in `extra_data`); `/lines`
> writes two rows — a CREATE for the new line (resource type `purchase_order_line`) and an UPDATE on
> the PO recording the subtotal/total roll (`extra_data.cause = "po_line_added"`). Audit rows are
> flushed before the terminal commit so they commit atomically with the change. (These endpoints
> were RBAC-gated but unaudited prior to 2026-07-12; the import loader was already per-row audited.)

### PO Upload (AI document extraction)

Upload a vendor PO or quote document, AI-extract its data for human review, then create the PO
from the reviewed result (`app/api/endpoints/po_upload.py`, mounted at `/po-upload`). Extraction
runs through the shared `run_llm_task` pipeline (prompt `po_extraction` 1.0.0,
`feature="po_upload"`, one tenant-scoped `ai_usage_events` row per call — telemetry, not audit)
and is covered by the per-company `allow_ai_egress` kill switch (see **Company (self-service)**
below).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/po-upload/upload-po` | Upload a PO document (`.pdf`/`.doc`/`.docx`, 10 MB cap; else **400**) — AI-extracts data for review before commit | Yes |
| POST | `/po-upload/upload-quote` | Upload a vendor quote document — AI-extracts data to build a PO | Yes |
| POST | `/po-upload/upload-invoice` | Legacy alias of `upload-quote` (same extraction behavior) | Yes |
| POST | `/po-upload/create-from-upload` | Create the PO from the reviewed extraction — can create the vendor and missing parts; **400** if the PO number already exists | Admin / Manager / Supervisor |
| GET | `/po-upload/pdf/{path}` | Serve the uploaded source document for preview (`s3://` refs and local paths) | Yes |
| GET | `/po-upload/search-parts` | Part typeahead for extraction-review matching (`q`, `limit` ≤ 50) | Yes |
| GET | `/po-upload/search-vendors` | Vendor typeahead for extraction-review matching (`q`, `limit` ≤ 50) | Yes |

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
| POST | `/receiving/receipt/{receipt_id}/print-label` | Manually (re)print the 4×6 thermal receiving label | Admin / Manager / Supervisor |
| GET | `/receiving/print-profile` | Get the company ProxyBox print profile (key masked; **404** until created) | Admin |
| PUT | `/receiving/print-profile` | Create / update the print profile, incl. the `allow_print_egress` kill switch | Admin |

> **Thermal receiving-label printing (ProxyBox / WHTP203e).** A 4×6 PDF (part / rev /
> qty / lot / Code128, CRITICAL banner for critical parts) is rendered, stored as a
> `Document` (`RECEIVING_LABEL`, linked via `POReceipt.label_document_id`), and sent to
> a ProxyBox Zero bridge. See [docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md).
>
> - **`POST /receiving/receipt/{receipt_id}/print-label`** — body (optional)
>   `{ "copies": <1–20> }` overrides the profile default. Response:
>   `{ receipt_id, receipt_number, label_document_id, printed, message }`. Errors:
>   **409** when `allow_print_egress` is OFF / the profile is incomplete, **404** for a
>   missing or cross-tenant receipt, **502** on a ProxyBox / printer failure (the label
>   `Document` is still persisted, so a later reprint works). Same role gate as
>   `POST /receiving/receive` (Admin / Manager / Supervisor).
> - **`PUT /receiving/print-profile`** — fields: `proxybox_base_url` (full base incl.
>   `/api/v1`), `proxybox_target`, `api_key` (**write-only**, Fernet-encrypted at rest,
>   never returned — sending it rotates the stored key), `default_paper_size`,
>   `default_copies` (1–20), `auto_print_on_receipt`, `allow_print_egress`, `is_active`.
>   Omitted fields are left unchanged. Read responses expose only `api_key_last4` /
>   `has_api_key`; secrets never appear in audit / event payloads. Flipping
>   `allow_print_egress` (default OFF) is recorded as a **status change** on the
>   tamper-evident audit trail.
>
> Auto-print on receipt is a separate, best-effort ARQ job enqueued by
> `POST /receiving/receive` after commit; it no-ops unless the profile is active with
> **both** `auto_print_on_receipt` and `allow_print_egress` ON.

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
| GET | `/reports/ship-otd` | Ship-based OTD/OTIF detail report (`period` today/7d/30d/90d/ytd/custom + `start_date`/`end_date`) | Yes |
| POST | `/reports/custom` | Generate custom report | Yes |

> **`GET /reports/ship-otd` (Lean Phase 1).** The customer-experienced delivery report: measures
> `Shipment.ship_date` against the **promise** (`must_ship_by`, falling back to `due_date`), counting
> only real shipments (dated, not soft-deleted, not CANCELLED); multiple partial shipments roll up
> cumulatively, and the **full-ship date** is the shipment that crossed the ordered quantity.
> Returns: headline `otd_ship_pct` (**fulfillment-anchored** — of WOs whose full quantity finished
> shipping in the window, the share on/before promise) and `otif_pct` (**promise-anchored** — of WOs
> promised in the window, the share fully shipped **by** the promise date, so an open WO past promise
> counts as a miss immediately), both `null` on an empty denominator; per-WO `rows[]` (promise
> source/date, first/last/full ship dates, `on_time`, `days_late` — for an open WO past promise, days
> past so far); a `by_customer[]` rollup; and `promise_hygiene[]` — shipped/open WOs with **neither**
> promise field set (unmeasurable). These are the same legs as the `on_time_delivery_ship` / `otif`
> KPIs on `GET /analytics/kpis` (see Analytics).

### Analytics

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/analytics/overview` | Analytics overview | Yes |
| GET | `/analytics/kpis` | KPI dashboard (OEE, OTD, FPY, scrap, NCRs, …) | Yes |
| GET | `/analytics/production-trends` | Production trends | Yes |
| GET | `/analytics/quality-metrics` | Quality metrics | Yes |
| GET | `/analytics/cost-analysis` | Job cost analysis (estimated vs. actual) | Yes |
| GET | `/analytics/flow` | Measured flow: lead times, queue times, Little's Law, PCE | Admin / Manager / Supervisor |
| GET | `/analytics/wip-aging` | WIP aging snapshot (open WOs, days since release / in current op) | Admin / Manager / Supervisor |
| GET | `/analytics/fpy` | First-pass yield / rolled throughput yield by part and work center | Admin / Manager / Supervisor / Quality |
| GET | `/analytics/scrap-pareto` | Scrap quantity/cost Pareto by reason code | Admin / Manager / Supervisor / Quality |
| GET | `/analytics/adoption` | Digital-adoption + hidden-factory metrics | Admin / Manager / Supervisor |
| POST | `/analytics/custom-report` | Run a custom-report query (returns rows) | Admin / Manager |
| GET | `/analytics/custom-report/export` | Export a saved report template (csv / xlsx / pdf) | Admin / Manager |

> **Flow & quality metrics (Lean Phase 1).** Five read-only, role-gated, tenant-scoped analytics
> endpoints. All but `/wip-aging` (a point-in-time snapshot) take the same window parameters as
> `/analytics/kpis`: `period` (`today` / `7d` / `30d` / `90d` / `ytd` / `custom`) plus
> `start_date` / `end_date` for `custom`. As throughout Analytics, uncomputable values are `null`
> ("n/a"), never a fake 0/100:
> - **`/flow`** — per completed WO: lead time (release → `actual_end`), release→first/last-ship days,
>   value-add RUN hours, and PCE (value-add ÷ lead time); summary adds median/avg lead time,
>   Little's Law WIP/throughput, and per-work-center queue times (measured from `operation_ready`
>   events where available, predecessor-end → start as fallback, with `from_ready_events` counting
>   the former).
> - **`/wip-aging`** — every open released WO with days since release, the current operation and days
>   in it (since its `actual_start`, or since it became READY), and days to due (negative = past due).
> - **`/fpy`** — quantity-weighted first-pass yield (`(complete − reworked − scrapped) ÷ attempted`)
>   grouped by part and by work center; RTY (product of per-op FPYs) per part. Optional
>   `work_center_id` / `part_id` filters; RTY is omitted when `work_center_id` is set (it is a
>   full-route metric). Rework tracking feeds from produced quantity booked on REWORK time entries.
> - **`/scrap-pareto`** — scrap quantity and cost (quantity × `standard_cost` where available) bucketed
>   by scrap reason code with cumulative %, uncoded scrap in an `unspecified` bucket. Optional
>   `work_center_id` / `part_id` filters.
> - **`/adoption`** — the A0.1 paper-to-digital telemetry read side: digital completion % (live
>   kiosk/desktop/scanner vs. backfill/import vs. unknown channel), clock-in coverage, backfill rate,
>   a weekly trend, plus **hidden-factory** metrics — rework hours/quantity share, planned-vs-reactive
>   maintenance mix, and per-work-center MTBF/MTTR.
>
> **Provenance rule.** Labor/scrap booked through the `backfill` / `import` channels is **excluded**
> from the measured baselines (value-add hours, FPY-feeding rework, Pareto buckets, hidden-factory
> hours) and reported separately on each response (`excluded_backfill_import_*`), so migrated history
> can't masquerade as measured shop-floor data.
>
> **`GET /analytics/kpis` gained two ship-based delivery KPIs.** `on_time_delivery_ship`
> (fulfillment-anchored ship OTD) and `otif` (promise-anchored on-time-in-full) now ride the KPI
> dashboard alongside the existing completion-based `on_time_delivery`, as regular `KPIValue`s
> (value / prior / change / sparkline, nullable per the "n/a" rule below). Semantics and the shared
> promise precedence (`must_ship_by` || `due_date`) are documented under
> `GET /reports/ship-otd` (Reports). The fields are optional-with-default in the schema so cached
> consumers/fixtures keep validating; the live endpoint always populates both.

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
| GET | `/oee/dashboard` | OEE per work center, plant-wide OEE, targets (`period` 7d/30d/90d/365d, or explicit `date_from`/`date_to` which take precedence) | Yes |
| GET | `/oee/trends` | OEE time-series for charts (`work_center_id`, `period`, or explicit `date_from`/`date_to`) | Yes |
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
> a foreign `work_center_id` returns **404**. The calculation itself lives in
> `app/services/oee_service.py` (`compute_oee_for_work_center`) — the nightly auto-calc cron runs the
> **same code** (below), so a manual trigger and the cron can never disagree on the math.

> **One OEE record per (work center, date, shift) — duplicates are 409 (Lean Phase 1).** A unique
> index (`uq_oee_company_wc_date_shift`, migration `063`) enforces at most one `OEERecord` per
> company + work center + `record_date` + shift, where a **`null` shift and an empty-string shift are
> the same "no shift" key**. `POST /oee/records` for an existing key — and a `PUT /oee/records/{id}`
> whose shift change collides with an existing record — return **409 Conflict** (`"An OEE record
> already exists for this work center, date, and shift. Update the existing record instead …"`) instead
> of silently creating a double-counting duplicate. `POST /oee/calculate/{work_center_id}` still
> **upserts** (overwrites the existing record for the key); only a lost create race surfaces as 409.
>
> **`calculation_source` — manual vs. auto (Lean Phase 1).** Every OEE record response now carries
> `calculation_source`: **`manual`** (hand-entered via `POST /oee/records`, or the on-demand
> `POST /oee/calculate/{work_center_id}` trigger — a human asked for it; all pre-existing rows
> backfill to it) or **`auto`** (minted only by the nightly ARQ cron, `run_oee_auto_calc_job` at
> **02:30 UTC**, which computes **yesterday's** whole-day record per active company + active work
> center). The cron **never overwrites a `manual` record** — a hand-entered record for that WC/day
> (any shift) is authoritative and the cron skips the WC; `auto` records **are** recomputed by
> re-runs (idempotent refresh). Idle work centers (no closed clocked entry and no unplanned downtime
> that day) are skipped entirely — no staffed time is uncomputable, not an all-zero measurement.
> Cron-written records are audited with the system as actor. See `docs/DOCKER_PRODUCTION.md` →
> Background Jobs.

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

### Company (self-service)

The active company's own profile and self-managed settings. Mounted under `/companies`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/companies/me` | Get the active company (includes `allow_ai_egress` and `user_count`) | Any authenticated user |
| PUT | `/companies/me` | Update the active company's settings | Admin |
| PUT | `/companies/me/ai-egress` | Toggle the company's **AI document-extraction egress kill switch** (`allow_ai_egress`) | Admin |

> **`allow_ai_egress` is the AI-egress CUI kill switch (default OFF).** It gates **all** outbound
> AI document-extraction egress to the Anthropic API (the AI analogue of `allow_carrier_egress` /
> `allow_print_egress`), enforced fail-closed at the shared LLM client. `PUT /companies/me/ai-egress`
> takes `{ "allow_ai_egress": boolean }` and returns the updated `CompanyResponse`; the flip is
> recorded on the tamper-evident audit trail as both a field update and an
> `ai_egress_enabled` / `ai_egress_disabled` **status change**. While OFF, AI features (PO/quote,
> BOM, QMS, routing, laser-nest PDF extraction, Copilot, NL search) **degrade gracefully** — no
> request leaves the boundary (laser-nest extraction falls back to filename-only). New companies are
> created **OFF**; existing companies were grandfathered **ON**. The toggle is **Admin-only**
> (`require_role([ADMIN])`), matching the sibling `allow_carrier_egress` / `allow_print_egress`
> controls. It is exposed in the UI at
> **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`) — interactive for Admin
> (enabling egress requires explicit confirmation), read-only for other roles. See
> [docs/AI_QUOTING_AGENT_RUNBOOK.md](AI_QUOTING_AGENT_RUNBOOK.md).

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

### Werco Copilot (read-only AI chat)

Ask-anything chat over the caller's **own company's** ERP data, answered via Claude tool-use
against existing read paths (`app/api/endpoints/copilot.py` + `app/services/copilot_service.py`).
Surfaced in the app as the Copilot drawer (header button / `Ctrl+.`); not available on the
`/kiosk` or `/wallboard` screens.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/copilot/chat` | One chat turn — SSE stream by default; `?stream=false` for plain JSON | Yes (any authenticated user) |

**Request body:** `{ "messages": [...], "context_hint": "..."? }`. `messages` is the
**client-held** conversation history (the server is stateless between turns): 1–40 entries of
`{ "role": "user" | "assistant", "content": string (1–8,000 chars) }`, oldest first, and the
**last message must be from the user** (422 otherwise). Server-side shaping forwards only the
trailing 30 messages at up to 4,000 chars each to the model. `context_hint` (optional,
≤ 500 chars) tells the copilot what page/entity the user is viewing.

**Streaming response (default).** `text/event-stream` of JSON frames (`data: {...}`):

| Frame `type` | Payload fields | Meaning |
|--------------|----------------|---------|
| `tool_use` | `tool`, `summary` | A read-only lookup ran (one frame per tool call) |
| `delta` | `text` | A chunk of the answer text |
| `final` | full `CopilotChatResponse` payload | Terminal success frame — same shape as the `?stream=false` body |
| `error` | `message` | Terminal error frame (failures after the stream starts arrive here, not as an HTTP status) |

**Response (`?stream=false` body, and the `final` frame):**
`{ answer, references[], tool_trace[], interaction_id, rounds, truncated }` —
`references[]` are deep links `{ type, id, label, url }` to the entities used in the answer;
`tool_trace[]` lists the tool calls `{ tool, summary }` in order; `truncated: true` means the
tool-round cap was hit and the model was forced (`tool_choice: none`) to answer from what it had
already gathered.

**Limits / error codes:**

- Per-user rate limit: **20 requests/minute** default (`COPILOT_RATE_LIMIT_PER_MINUTE`) → **429**.
  This is in addition to the app-wide per-IP slowapi limits.
- At most **8 tool rounds** per turn (`COPILOT_MAX_TOOL_ROUNDS`) plus one forced final answer
  call; per-call output cap `COPILOT_MAX_OUTPUT_TOKENS` (default 1024); per-call upstream timeout
  `COPILOT_LLM_TIMEOUT_SECONDS` (default 45s).
- **503** — AI not configured (no `ANTHROPIC_API_KEY`); **502** — upstream AI-service failure;
  **422** — invalid history (e.g. last message not from the user). With streaming (the default),
  429 and the last-message 422 are still HTTP statuses (checked before the stream opens), but
  configuration/upstream failures surface as a terminal `error` frame on an HTTP 200 stream.

**Read-only + tenant-injection contract:**

- Every tool is a thin wrapper over an existing read path — the copilot **cannot create, update,
  or delete anything**.
- The tenant is **never model-controlled**: `company_id` is injected server-side from the
  authenticated session into every tool call; tool input schemas carry no tenant identifier, and
  any undeclared input keys the model supplies (including a `company_id`) are dropped before
  dispatch.
- A failing tool returns an error tool-result to the model; it does not abort the turn.

**Per-tool access** (mirrors each tool's source endpoint):

| Tool | Wraps (source read path) | Access |
|------|--------------------------|--------|
| `lookup_work_order` | Work-order context (`GET /work-orders/{id}` + AI context service) | Any authenticated |
| `search_erp` | `GET /search` (shared core `run_global_search`) | Any authenticated; **employee (`user`-type) results are excluded entirely** (data minimization — employee names/emails never enter model prompts). The Admin/Manager-gated user results remain available on `GET /search` only |
| `list_blocked_work_orders` | `GET /work-order-blockers` (open + acknowledged) | Any authenticated |
| `work_center_load` | `POST /scheduling/load-chart` | Any authenticated |
| `schedule_conflicts` | `GET /scheduling/conflicts` | Any authenticated |
| `inventory_lookup` | `GET /inventory` (on-hand/available by location and lot) | Any authenticated |
| `customer_open_orders` | `GET /work-orders` + `GET /quotes` (open WOs, active quotes) | Any authenticated |
| `company_snapshot` | AI context service aggregate counts | Any authenticated |

> **Telemetry, not audit data.** Every model call in the loop writes one `ai_usage_events` row
> (task `copilot_chat`), and every turn records an `AIInteractionEvent`
> (`source_module = "copilot"`, content redacted by the learning service). The copilot performs
> zero domain writes, so nothing lands on the `audit_log` hash chain.

### AI Recommendations (Action Inbox)

Suggest-only recommendations that feed the **Action Inbox** (`/action-inbox`) — they never mutate
controlled ERP records. All routes are scoped to the caller's active company
(`get_current_company_id`); status changes flow through the learning service, which records an
`AIInteractionEvent` per transition (telemetry, not the `audit_log` hash chain).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/ai/recommendations` | List recommendations sorted by the deterministic score (see below) | Yes |
| POST | `/ai/recommendations` | Create a suggest-only recommendation | Admin / Manager / Supervisor |
| POST | `/ai/recommendations/{id}/accept` | Mark accepted; body `{ reason?, apply? }` — with `apply=true` runs allowlisted `AIActionApplier` | Yes |
| POST | `/ai/recommendations/{id}/apply` | Accept + apply convenience (`apply=true`) | Yes |
| POST | `/ai/recommendations/{id}/dismiss` | Dismiss with optional reason | Yes |
| POST | `/ai/recommendations/{id}/snooze` | Snooze a **pending** recommendation; body `{ "days": 1–30, "reason"? }` | Yes |
| POST | `/ai/recommendations/{id}/feedback` | Attach free-text feedback / rating | Yes |
| POST | `/ai/aggregate` | Run learning aggregation + domain sensors + expiry/snooze sweep for the active tenant | Admin / Manager / Supervisor |
| POST | `/ai/outcomes` | Manually record a downstream outcome (most plant outcomes are auto-captured — see below) | Yes |
| POST | `/ai/events` | Record an AI interaction / correction signal | Yes |

**List query parameters:** `status` — `pending` (default) | `accepted` | `dismissed` | `stale` |
`snoozed`; `source_module`, `target_entity_type`, `target_entity_id`, `limit` (1–100, default 50).

**Scoring.** Each listed recommendation carries an additive `score` field, computed at read time
(never persisted): `priority_weight × confidence × age_decay × impact_magnitude` — priority
weights `high 1.0 / medium 0.6 / low 0.35 / info 0.2`; confidence is `confidence_score`
(0.5 when null); `age_decay` declines linearly from 1.0 (fresh) to 0.2 at `expires_at` (without
an expiry: mild decay to a 0.5 floor over 30 days); `impact_magnitude` is read from a numeric
`magnitude`/`impact_score`/`estimated_value`/`estimated_savings`/`value` key in the `impact`
JSON — fractions (0, 1] pass through (0.25 floor), larger values are log-scaled and capped at
2.0, default 1.0. The list is sorted by this score, descending. `score` is `null` on
single-recommendation responses (accept/dismiss/snooze).

**Snooze / expiry lifecycle.** Snoozing sets `status = "snoozed"` (409 if not pending; the
wake-up time is recorded on the snooze interaction event — no schema change). The nightly
AI-learning job (5:30 AM, and `POST /ai/aggregate` for the active tenant) is a tenant-scoped
fan-out that marks pending/snoozed recommendations past `expires_at` as `stale`, returns
elapsed snoozes to `pending`, and runs **domain sensors** (late/at-risk WOs, inventory risk,
quality scrap trends) that mint suggest-only Action Inbox items without a human prompt. Its
summary reports `companies_processed`, `recommendations_created`, `stale_recommendations`,
`snoozed_recommendations_woken`, and `sensor_recommendations_created`.

**Always-on outcomes.** Completing a work order (via `emit_work_order_completed_event`)
auto-records `on_time_delivery`, `scrap_rate`, and optional `cost_variance` outcomes. Terminal
quote statuses (accepted / rejected / converted / expired) auto-record `quote_result`. See
[AI_ALWAYS_ON.md](AI_ALWAYS_ON.md).

> **Front door.** After login, Admin / Manager / Supervisor users land on `/action-inbox` by
> default (operators keep the kiosk station screen; deep links are unaffected). The page shows a
> "Top 3 today" hero — the three highest-scoring pending recommendations.

### Bulk Imports & Templates (Excel Migration Kit)

One shared CSV/XLSX upload kit for go-live data migration — see
[docs/EXCEL_MIGRATION_RUNBOOK.md](EXCEL_MIGRATION_RUNBOOK.md) for the operational sequence. All
import endpoints below accept **`.csv`** (UTF-8) or **`.xlsx`** (first worksheet only) via the
shared parser (`app/services/import_service.py`): headers are normalized to snake_case
(`"Part Number"` → `part_number`), rows whose **first cell starts with `"# "`** (hash + space — the
template guidance marker; a bare `#` is data) are skipped, blank rows are tolerated, and files are
capped at **10 MB / 10,000 data rows / 256 columns** (columns past the 256th are ignored). Scanning
is **bounded** so an XLSX with a bloated used range (one stray formatted cell can declare a
16,384 × 1,048,576 grid) parses in milliseconds instead of stalling a worker: a run of **more than
1,000 consecutive blank rows** is treated as end of data — and if a bounded look-ahead finds real
data past such a gap, the file is **refused** (400) rather than silently truncated — and scanning
more than **100,000 raw rows** total refuses the file outright. File-level problems (type, encoding,
missing required columns, duplicate-after-normalization headers, caps/scan bounds) return **400**
with a plain-English `detail`;
two distinct columns that collide after normalization are a **hard error** naming both offenders
(refusing the file beats silently merging columns in a migration tool). Row-level validation stays
per-endpoint with the partial-success contract: on commit each row (each PO, for the PO import) is
saved independently, bad rows are skipped and reported in `errors[]`.

**Templates** (static workbooks, no tenant data — any authenticated user):

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/import/templates` | List the 10 downloadable templates (entity, title, columns, download path) | Yes |
| GET | `/import/templates/{entity}` | Download the styled XLSX template (`werco-import-template-{entity}.xlsx`); 404 lists valid entities | Yes |

Template entities: `users`, `parts`, `materials`, `customers`, `vendors`, `work-centers`,
`work-orders`, `purchase-orders`, `bom`, `routings`. Each workbook has an **Import** sheet (styled header + one
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
| POST | `/routing/import/preview`, `/routing/import/commit` | `part_number`, `sequence`, `operation_name` | Admin / Manager / Supervisor |

> **Routing import** uses an explicit **preview/commit pair** (not a `dry_run` query param) and lives
> under the Routing router — see [Routing](#routing) above for the column list, the optional
> `assignments` form field, the `RoutingImportResponse` shape (per-operation detail +
> `operations_needing_work_center`), the optional `work_center_code` rule, and the same-revision
> conflict rule. `work_center_code` is **optional** (blank = assign the work center in the wizard
> after upload), so it's no longer in the required-columns set. Its `routings` template is in the
> templates index.

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

> **List query params (`GET /audit/`):** `action`, `resource_type`, `user_id`, and `search`
> (matches description / resource identifier / user name) filter the rows; `limit` (default 100,
> **max 500**) and `offset` (default 0) page them. Results are ordered `desc(timestamp)` (newest
> first), so paging with increasing `offset` walks back into older history. The list response
> carries no total count — clients infer "has next page" by over-fetching one row past the page
> size. The Audit Log UI uses this offset/limit paging (Prev/Next), so the **full audit history is
> reachable in the UI**, not just the most recent page.
>
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

### Visitor Logs

Lobby **visitor sign-in tablet** + admin visitor log (`/api/v1/visitor-logs`). The two write
endpoints (`/sign-in`, `/sign-out`) accept **either** a normal staff access token **or** a
PIN-minted station signin token (`type="signin"`, via `get_signin_principal`); everything else is
staff-only RBAC. All queries are tenant-scoped; visitor records are soft-deleted, never hard-deleted.
See [docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/visitor-logs/station-login` | Unlock a tablet with the shared station PIN. Body `{"station_id", "pin"}` (PIN 4–8 digits) → `{"token", "station_label", "expires_in"}` (24 h scoped `type="signin"` JWT, returned once). Bad/revoked station or wrong PIN → **401** (indistinguishable; failed attempt audited) | **Public** (PIN-gated)¹ |
| POST | `/visitor-logs/sign-in` | Record a visitor sign-in → **201** `VisitorLogResponse`. Best-effort host email on a matched internal host | Station token **or** any authenticated user |
| POST | `/visitor-logs/sign-out` | Sign out an open visit by `{"visitor_log_id"}` or `{"name"}` → `VisitorLogResponse`. Name with >1 open match → **409** disambiguation; no open match → **404** | Station token **or** any authenticated user |
| GET | `/visitor-logs/` | List visitor records for the active company (filters + offset paging) → `{"items", "total"}` | Admin / Manager / Supervisor |
| GET | `/visitor-logs/export.csv` | Stream the visitor log as CSV (audits an `EXPORT` action) | Admin / Manager |
| DELETE | `/visitor-logs/{id}` | Soft-delete a visitor record → **204** | Admin / Manager |
| POST | `/visitor-logs/stations` | Create a PIN-protected sign-in station. Body `{"label", "pin"}` → **201** `SigninStationResponse` (PIN hashed, never echoed) | Admin / Manager |
| GET | `/visitor-logs/stations` | List this company's sign-in stations (no PIN/`pin_hash`) → `{"stations"}` | Admin / Manager |
| POST | `/visitor-logs/stations/{id}/revoke` | Revoke a station (idempotent status flip; tablet loses access next request) → `SigninStationResponse` | Admin / Manager |
| POST | `/visitor-logs/stations/{id}/reset-pin` | Re-hash a station's shared PIN. Body `{"pin"}` → `SigninStationResponse` | Admin / Manager |

> ¹ **Rate-limited at `5/minute` per client IP** (enforced). `station-login` is registered in
> `main.py`'s `AUTH_RATE_LIMITS`; the per-path auth limiter now rejects over-limit requests with
> **429 + `Retry-After`**. With brute force throttled server-side, the interim 6–8 digit PIN
> recommendation can relax (see [docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md) → Security note).

**`GET /visitor-logs/` query params:** `status` (`signed_in` / `signed_out`), `q` (matches visitor
name / company / host), `date_from`, `date_to` (filter on `signed_in_at`), `on_site_only` (bool —
overrides `status`), `skip` (default 0), `limit` (default 50, **max 200**). Newest first.

#### Visitor sign-in request (`POST /visitor-logs/sign-in`)

```json
{
  "visitor_name": "Jane Smith",
  "visitor_company": "Acme Corp",
  "visitor_phone": "(555) 123-4567",
  "host_name": "John Doe",
  "purpose": "meeting",
  "purpose_note": null,
  "safety_acknowledged": true
}
```

- `purpose` is one of `meeting` · `delivery` · `contractor` · `interview` · `audit` · `other`.
- `purpose_note` is **required when `purpose == "other"`** (server-validated); `safety_acknowledged`
  **must be `true`** to sign in. `visitor_company` / `visitor_phone` / `host_name` are optional.

#### Visitor log schema (`VisitorLogResponse`)

```json
{
  "id": 42,
  "visitor_name": "Jane Smith",
  "visitor_company": "Acme Corp",
  "visitor_phone": "(555) 123-4567",
  "host_name": "John Doe",
  "host_user_id": 7,
  "purpose": "meeting",
  "purpose_note": null,
  "safety_acknowledged": true,
  "status": "signed_in",
  "signed_in_at": "2026-06-30T14:05:00Z",
  "signed_out_at": null,
  "signin_station_id": 1,
  "station_label": "Lobby Tablet"
}
```

`signed_out_at: null` means the visitor is **still on-site**. `signin_station_id` / `station_label`
are `null` for a staff-created row. `host_user_id` is set only when the typed host best-effort-matched
exactly one active internal user in the company.

#### Sign-out 409 disambiguation

Signing out by `name` when more than one open visit shares that name returns **409** with a minimal
list (no PII beyond company) so the tablet can show a picker, then re-POST by `visitor_log_id`:

```json
{
  "detail": {
    "message": "Multiple visitors signed in under that name — choose one to sign out",
    "matches": [
      { "id": 42, "visitor_company": "Acme Corp", "signed_in_at": "2026-06-30T14:05:00Z" },
      { "id": 51, "visitor_company": "Globex", "signed_in_at": "2026-06-30T15:20:00Z" }
    ]
  }
}
```

#### Sign-in station schema (`SigninStationResponse`)

```json
{
  "id": 1,
  "label": "Lobby Tablet",
  "revoked": false,
  "revoked_at": null,
  "revoked_by": null,
  "last_used_at": "2026-06-30T08:00:00Z",
  "created_by": 3,
  "created_at": "2026-06-29T17:00:00Z"
}
```

The PIN and its `pin_hash` are never returned. The tablet URL for a station is
`/visitor-signin?station=<id>`.

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

### Timestamps

All `datetime` fields in responses are serialized as **UTC ISO-8601 with a trailing `Z`**
(e.g. `"2026-07-01T19:17:00Z"`) — the store-UTC / serve-UTC contract, applied uniformly across
every endpoint (response schemas inherit `UTCModel`; hand-built dicts use
`app.core.time_utils.to_utc_iso(...)`). `date`-only fields (e.g. `due_date`) are unaffected and
stay `YYYY-MM-DD` with no time or zone. Clients should treat `Z` timestamps as UTC and convert for
display; the web UI renders them in shop-local Central time.

### Success Response
```json
{
  "id": 1,
  "created_at": "2024-01-01T10:00:00Z",
  "updated_at": "2024-01-01T10:00:00Z"
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
  "completed_at": "2026-06-07T14:30:00Z"
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

API endpoints are rate limited per client IP:
- Default: 100 requests per 60 seconds (all other paths)
- Health check endpoints: Exempt from rate limiting

Sensitive auth endpoints carry stricter, **enforced** per-path limits (previously declared but only
the global default applied):

| Path | Limit |
|------|-------|
| `POST /auth/login` | 5/minute |
| `POST /auth/register` | 3/minute |
| `POST /auth/register-public` | 3/minute |
| `POST /auth/refresh` | 30/minute |
| `POST /auth/employee-login` | 3/minute |
| `POST /auth/kiosk-badge-token` | 30/minute |
| `POST /visitor-logs/station-login` | 5/minute |
| `POST /shop-floor/kiosk-stations/station-login` | 5/minute |
| `POST /scanner/resolve-action` | 60/minute |

An over-limit request returns **HTTP 429** with a `Retry-After` header (seconds until the window
resets) and body:
```json
{ "detail": "Rate limit exceeded: 5/minute" }
```
Enforcement fails open: if the limiter backend errors, the request is allowed (the global default
limit still applies).

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
| 502 | Bad Gateway — upstream AI-service failure on an AI endpoint (e.g. `/copilot/chat?stream=false`) |
| 503 | Service Unavailable — an AI endpoint was called but the AI features are not configured (`ANTHROPIC_API_KEY` unset) |

## Interactive Documentation

When the backend is running, visit:
- **Swagger UI**: `/api/docs` - Interactive API explorer
- **ReDoc**: `/api/redoc` - Alternative documentation view
- **OpenAPI JSON**: `/api/openapi.json` - Raw specification

For more details on specific endpoints, use the interactive documentation above.
