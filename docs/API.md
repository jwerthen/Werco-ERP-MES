# Werco ERP API Documentation

This is a high-level overview of the Werco ERP API. For interactive documentation, visit `/api/docs` when the backend is running — outside production, where it is deliberately disabled (see [Interactive Documentation](#interactive-documentation)).

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

#### Login when an address exists in more than one company

Email is unique **per company** (`uq_users_company_email`), never globally, and every
company-scoped creation path (`POST /auth/register`, `POST /users/`, the user CSV importer,
`PUT /users/{id}`) enforces uniqueness only within its own tenant. Two tenants may therefore
legitimately hold the same address, while `POST /auth/login` has no tenant to scope to — the
company context is derived *from* the matched row.

Login used to resolve such an address with an unordered "pick one", so which tenant's account it
authenticated as depended on row order. It now refuses instead:

| Matches for the submitted address | Result |
|------|------|
| exactly one | normal login (existing 401/403 rules unchanged) |
| two or more | **409** `Email is not unique. Please contact an administrator.` |

The refusal happens **before** the password is checked, so it never increments another account's
failed-login counter, and it writes a `LOGIN_BLOCKED` audit row (`error_message`: `Email resolves
to more than one account`) — that row is the only place the collision is reported. Same shape as
the existing 409 on `POST /auth/employee-login` for a non-unique badge.

**Operators: check for duplicate addresses before deploying this behavior.**

```sql
SELECT lower(email) AS address, count(*) AS accounts, array_agg(company_id) AS companies
FROM users GROUP BY lower(email) HAVING count(*) > 1;
```

Any address returned is a user who will get 409 at login until an admin renames one side
(`PUT /users/{id}`). Resolving the collision automatically is not possible without a tenant
discriminator (company slug/subdomain) on the login form — an open product decision, not a defect
in this rule.

### Public registration (`POST /auth/register-public`)

Unauthenticated, 3/minute per IP, and **install-wide**: its email and employee-ID uniqueness
checks span every company, not just the one it registers into.

It used to answer `400 Email already registered` or, distinctly, `400 Employee ID already
exists` — two account-existence oracles over every tenant's user list and badge numbers, on a
route that also inserts rows into `users`. Both 400s are gone. Outside the first-user bootstrap
**every** outcome returns the same body, and a duplicate does not insert:

```json
{ "message": "Account submitted for approval", "is_first_user": false }
```

The password is hashed before the duplicate check so accepted and refused calls do the same
bcrypt work (skipping it would rebuild the oracle in the response time), and a lost insert race
(`IntegrityError`) returns that same body rather than a 500. Refusals are recorded server-side as
a `PUBLIC_REGISTRATION_REJECTED` audit row.

The first-user bootstrap is unchanged and still returns its distinct
`{"message": "Admin account created successfully", "is_first_user": true}` — with zero users
nothing can collide, so it never reaches the uniform path.

The uniqueness checks stay install-wide **on purpose**: scoping them per company would let this
public route mint the cross-tenant duplicate emails and badge numbers that make login and
badge login refuse with 409.

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
| POST | `/auth/display-token` | Issue a display token. Body: `{"label", "expires_days", "dept"?, "show_customer_names"?}` (label 1–100 chars; lifetime default **90** days, capped at **365**; optional `dept` ≤ 50 chars — the work-center-type preset the TV opens with; `show_customer_names` bool, default **false** = public-safe — opt this display in to rendering work-order customer names on the board, gated server-side, see the wallboard callout). Response carries the one-time `token` **plus** the one-time `setup_code` + `setup_code_expires_at` (15-min TTL — see callouts) | Admin / Manager |
| GET | `/auth/display-token` | List this company's display tokens (metadata only, incl. `dept` and `show_customer_names` — the JWTs and setup codes are never returned) | Admin / Manager |
| POST | `/auth/display-token/{id}/setup-code` | Reissue the one-time TV setup code for an existing display → `{"id", "label", "dept", "setup_code", "setup_code_expires_at"}`. The previous code — used or not — is invalidated immediately; the new code is shown once and expires in **15 minutes**. Revoked/expired token → **400**; cross-tenant id → **404** | Admin / Manager |
| POST | `/auth/display-token/claim` | Exchange a one-time setup code for the display JWT. Body `{"code"}` (case-, space- and dash-insensitive) → `{"token", "label", "dept", "expires_at"}`. **Every** failure mode (unknown / used / expired code, revoked / expired display) → the same generic **404** | **Public** (rate-limited **10/minute** per IP) |
| DELETE | `/auth/display-token/{id}` | Revoke a display token (status flip, idempotent; cross-tenant id → 404) | Admin / Manager |

> **One-time reveal.** The raw JWT **and the 8-char setup code** are returned exactly **once** —
> the `token` / `setup_code` fields on the POST response. Neither is stored server-side (only the
> JWT's `jti` and the code's **SHA-256 hash** land in the `display_tokens` row) and neither appears
> in the list response. A lost token cannot be recovered — but a lost or expired setup code can be
> **reissued** via `POST /auth/display-token/{id}/setup-code`.
>
> **Setup-code claim (TV pairing).** `POST /auth/display-token/claim` is deliberately **public** —
> a TV pairing itself has no credential yet; the high-entropy single-use code (8 chars of CSPRNG
> output over a 31-symbol alphabet excluding `0/O/1/I/L`, **15-minute TTL**) *is* the credential,
> and the matched `display_tokens` row is the company-binding authority. The endpoint is
> rate-limited (**10/minute per IP**, see Rate Limiting) and returns the **same generic 404 for
> every failure mode**, so it cannot be used as an oracle for why a code failed. On success the
> code is burned (single use), the pairing is audit-logged (a `CLAIM` event on the row's company —
> no user identity; it's a TV), and the JWT is **re-minted from the row** (same `jti` / company /
> `expires_at` as the issuance JWT), so the revocation semantics below are unchanged. See
> [docs/WALLBOARD.md](WALLBOARD.md) → Setting up a TV.
>
> **Revocation is DB-authoritative.** `DELETE` flips the row's `revoked` flag (the row is kept as
> the issuance record, not deleted). Issuance, revocation, setup-code reissue, and each successful
> claim all write tamper-evident `audit_log` rows (never the code value or its hash). The
> wallboard auth dependency re-checks the `display_tokens` row (exists / not revoked /
> not past its DB `expires_at`) on **every** request, so a revoked or expired token stops working
> on the TV's next poll (~30s) even though the JWT itself is still signature-valid — regardless of
> whether the TV was paired via URL or setup code.

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

> **`POST /auth/employee-logout` requires authentication** and takes the actor from the **bearer
> token**, never from the request body. The body (`{"employee_id"}`) is still accepted for wire
> compatibility but is ignored for identity, and an authenticated caller always gets **200** — there
> is no 404/200 distinction to probe. It previously took no auth at all and resolved any
> `employee_id` through a globally unscoped lookup, which made it both an audit-forgery surface
> (anyone could write a tenant-tagged `EMPLOYEE_LOGOUT` row naming a real employee, visible at
> `GET /audit/?resource_type=authentication`) and a cross-tenant badge-enumeration oracle.

> **Path fence.** A `scope="kiosk"` operator token is honored only on `/api/v1/shop-floor/*` and
> `POST /api/v1/auth/employee-logout`; `get_current_user` rejects it with **403** everywhere else
> (the token is valid — it just cannot reach the resource). Three shop-floor carve-outs are also
> **denied** to kiosk-scoped tokens regardless of role: `/shop-floor/kiosk-stations/*` (station
> lifecycle admin), `/shop-floor/time-entries/{id}/approve|unapprove` (G5-A labor approval), and the
> manager dispatch tools — `GET /shop-floor/dispatch-board` plus
> `PUT /shop-floor/work-centers/{id}/run-order` (a shared crew terminal must not read the whole
> shop's board or dictate what every machine runs next). Operators keep **reading** their `RUN`
> chips: the work-center-queue endpoint is a different path and stays allowed.
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
| GET | `/work-orders/` | List all work orders (`skip` ≥ 0, `limit` 1–5000 default 100 — the standard list tier, see [Pagination](#pagination)) | Yes |
| POST | `/work-orders/` | Create work order. `work_order_type` is validated against the `WorkOrderType` vocabulary (**422** on an unknown value), and `'laser_cutting'` is **refused on create** (422) — nest-dispatch WOs are minted only by the laser nest import paths (see note below) | Yes |
| GET | `/work-orders/{id}` | Get work order by ID | Yes |
| PUT | `/work-orders/{id}` | Update work order (body requires the WO's current `version` — stale → 409; also 409 if it moves a terminal WO back to a non-terminal status, **or sets `status` to COMPLETE/CLOSED from any status other than COMPLETE/CLOSED** — see "Terminal-state lock" below) | Yes |
| DELETE | `/work-orders/{id}` | Delete work order (soft by default; `hard_delete=true` only for draft/cancelled) | Admin / Manager |
| POST | `/work-orders/{id}/release` | Release to production | Yes |
| POST | `/work-orders/{id}/start` | Start production | Yes |
| POST | `/work-orders/{id}/complete` | Complete work order (409 if the WO is CANCELLED) | Yes |
| POST | `/work-orders/{id}/duplicate` | **Duplicate a work order** — copy its *plan* (operations, laser nests, open material ties, re-snapshotted process-sheet steps) onto a new **DRAFT** WO. Body `{quantity_ordered, due_date}`; **201** returning an **envelope**, not a bare work order. See "Duplicating a work order" below | Admin / Manager / Supervisor |
| POST | `/work-orders/{id}/operations` | Add an operation to a work order | Admin / Manager / Supervisor |
| PUT | `/work-orders/operations/{id}` | Update an operation (body now also accepts `work_center_id` — move the operation to another work center; see note below). **409** if it sets `status` to COMPLETE on a not-yet-complete operation — completion goes through the completion endpoints (see "Terminal-state lock") | Admin / Manager / Supervisor |
| POST | `/work-orders/operations/{id}/start` | Start an operation | Yes |
| POST | `/work-orders/operations/{id}/complete` | Complete an operation (or record partial progress; 409 if the parent WO is terminal) | Yes |
| POST | `/work-orders/operations/{id}/reduce-production` | Supervisor/office over-count correction — walk back good-count across **any** operator's **unapproved** labor on the operation; **a COMPLETE operation is correctable here** (unlike the operator's twin), a terminal WO is not; no clock-in required (see note below) | Admin / Manager / Supervisor |
| GET | `/work-orders/{id}/material-allocations` | List the work order's material ties (`include_inactive`, default `true`) | Yes |
| POST | `/work-orders/{id}/material-allocations` | Tie a material part to the work order or one of its operations (**201**) | Admin / Manager / Supervisor |
| PATCH | `/work-orders/{id}/material-allocations/{allocation_id}` | Edit an **open** tie's quantities, lot pin, or notes | Admin / Manager / Supervisor |
| DELETE | `/work-orders/{id}/material-allocations/{allocation_id}` | Untie (status → `cancelled`; the row is never physically deleted) | Admin / Manager / Supervisor |
| GET | `/work-orders/{id}/material-allocations/{allocation_id}/consumption` | Per-source-lot ledger position of a tie (`issued` / `returned` / `net`) — the pre-confirm read behind the return dialog | Yes |
| POST | `/work-orders/{id}/material-allocations/{allocation_id}/return` | **Return consumed material to its source lots** — reasoned, audited, compensating. The only verb on this router that posts inventory | Admin / Manager / Supervisor |
| GET | `/work-orders/{id}/backflush-preview` | **Dry run (PR 4.5)** — what completing this work order would consume, per component and per lot, before anything moves. Pure read: no ledger row, no audit row, no event | Yes |

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
> **Optimistic locking on the work-order header (`PUT /work-orders/{id}`).** The update body's
> required `version` is enforced: it must equal the work order's current `version` (returned on every
> work-order response) or the update is rejected with **409 Conflict**
> (`{"detail": "Work order was modified by someone else. Refresh and try again."}`) before any field
> is written. On 409 the client should re-fetch the WO (picking up the fresh `version`) and retry.
> `version` is never client-writable — a successful update increments it server-side (SQLAlchemy
> `version_id_col` on `WorkOrder`), and responses carry the real counter. Beyond this endpoint, the
> mapping makes **every** WorkOrder write path conflict-checked: a genuinely concurrent stale write on
> any other path (priority updates, kiosk status flips, soft delete) surfaces as **409 Conflict**
> (`{"detail": "This record was modified by someone else. Refresh and try again."}`) via an app-wide
> `StaleDataError` handler instead of silently losing the write.
>
> **Completion contract (shared finalizer).** Operation completion rolls up into the work order
> through one shared finalizer, so all completion paths behave identically. On the absolute
> completion verbs (`/operations/{id}/complete`, both here and on the shop floor) the stored
> `quantity_complete` is `clamp(max(existing, requested, recorded production evidence), 0, target)`:
> it never drops below the value already recorded or below durable production evidence, and never
> exceeds the operation target. The work order's `quantity_complete` only ever moves forward. On a
> **laser dispatch-pool WO** (`work_order_type='laser_cutting'`) the work-order rollup is the
> **sum** of per-nest progress rather than the sequential single-operation rule, and completing all
> nests does **not** snap the header to `quantity_ordered` — see "Pool WO header progress" under
> Laser Nests. Scrap
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
> - `PUT /work-orders/{id}` that sets `status` to **COMPLETE** or **CLOSED** from any status **other
>   than** COMPLETE/CLOSED returns **409 Conflict** — a blind status write would mark the job finished
>   while permanently bypassing every completion effect (FG receipt, tied-material consumption,
>   backflush, cost rollup), and nothing ever heals it because the completion verbs and the reconcile
>   refuse terminal WOs afterwards. The detail points at `POST /work-orders/{id}/complete`. The
>   exemption is deliberately COMPLETE/CLOSED only, **not** all terminal statuses: **COMPLETE → CLOSED**
>   is an archival move between two states whose completion chain already ran (still a 200), and a
>   resend of the WO's current status is idempotent — but **CANCELLED → COMPLETE/CLOSED** is refused
>   with its own detail (`"cannot mark a cancelled work order '<target>': its completion effect chain
>   never ran and never will. A cancelled work order stays cancelled."`) rather than pointing at
>   `POST /complete`, which also (correctly) refuses cancelled WOs.
> - `PUT /work-orders/operations/{id}` that sets `status` to **COMPLETE** on an operation that is not
>   already COMPLETE returns **409 Conflict** — it would be a fifth completion path outside the four
>   wired handlers (no `finalize_operation_completion`, no operation-scoped tied-material consumption,
>   no completion audit shape). The detail points at `POST /work-orders/operations/{id}/complete`.
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
> (zero completed quantity) receives nothing, and a **laser nest-dispatch WO**
> (`work_order_type='laser_cutting'` — the shared `is_laser_dispatch_work_order` predicate) **never
> receives FG at all**: it is a dispatch pool whose `quantity_complete` counts pooled nest **runs**, and
> a parented laser child carries the **parent assembly's** `part_id`, so receiving here would mint
> phantom finished goods of the parent part (5 nests × 8 runs = 40 phantom units) that the parent's own
> completion later books for real. Part-less standalone nest WOs are the same shape with no part at all.
> The skip covers both and is logged at debug (expected behavior, not an error). The receipt's lot is
> reconstructable end-to-end via [Traceability](#traceability).
>
> **Component backflush is opt-in per part (default off) — and as of PR 4.5 the flag is SETTABLE.** If
> the finished part has `backflush_components = true` (see [Part Schema](#part-schema)), completion
> **auto-consumes** the part's BOM components: one negative `ISSUE` `InventoryTransaction` per
> component, decrementing source stock and carrying the consumed lot for genealogy — each **audited**
> and **reconciled to target** per component (`reference_type='work_order_backflush'`). When the flag is
> **false** (the default, and every part's state until somebody changes it) completion moves no
> components, so a shop that issues material manually is never double-consumed. A **laser
> nest-dispatch WO** (`work_order_type='laser_cutting'`, same predicate as the FG-receipt skip above)
> never runs this leg even when its part is armed: a parented child's `part_id` is the parent's part
> and its `quantity_complete` counts nest runs, so exploding the parent's BOM against those numbers
> would consume the parent's components on the wrong demand basis while the parent's own completion
> runs the real backflush. Only the BOM leg is gated — the nest's **material ties** are its actual
> consumption mechanism and post unchanged.
>
> **Read the previous sentence's scope bound literally.** Until PR 4.5 the flag had no writer anywhere
> in the application, and the sentences below described what the leg *would* do rather than observable
> behavior. That is no longer true in principle — `PUT /parts/{id}` and `PUT /materials/{id}` can now
> turn it on, behind the refusal gate described under [Part Schema](#part-schema) — but it remains true
> in practice **until the first part actually opts in**: the column's `server_default` is `false` and no
> production work order has yet reached this leg. Treat the behavior described here as **unproven in
> production**, not as observed.
>
> A backflush shortage (insufficient stock, after the shared FIFO policy has walked every consumable lot
> and skipped the segregated ones) **does not fail the completion** — the remainder is drawn negative and
> the shortfall is recorded as a tamper-evident `BACKFLUSH_SHORTAGE` audit row plus a
> `backflush_shortage` warning event (notification catalog `material.backflush_shortage` since PR 4.4).
>
> **A dry run is available before any of this happens**, and it writes nothing:
> `GET /work-orders/{id}/backflush-preview` (below) resolves the same demand through the same issue loop
> and the same lot policy, so the preview and the outcome cannot name different heats.
>
> **How component demand is resolved** (hardened while still dark; see
> `docs/MATERIAL_CONSUMPTION_PLAN.md` → "The BOM/routing backflush leg"):
> - **Basis is `quantity_complete + quantity_scrapped`**, each BOM line extended by its `scrap_factor`
>   — the same basis the per-run tie engine uses, so one shop cannot report two different consumptions
>   for the same physical event depending on whether the material was tied. A **fully-scrapped** work
>   order therefore backflushes (it previously backflushed nothing).
> - **Alternate, optional and `reference` BOM lines are skipped.** An alternate group is an OR, not an
>   AND; optional lines have nothing on the work order recording which units got them; `reference`
>   lines are documentation and tooling. This matches `mrp_service`, so planning and consumption state
>   the same demand for one BOM. (There is still **no substitution logic** — alternates are inert
>   columns, not a feature.)
> - **Multi-level BOMs:** a `phantom` sub-assembly explodes into its children; a `make` sub-assembly is
>   issued as a stocked unit and its children are **not** (they were consumed when it was built).
> - **Routing precedence is per part, not all-or-nothing.** An operation's `component_part_id` states
>   *that component's* demand; the BOM supplies every part the routing does not name. Previously one
>   stray `component_part_id` disabled the entire BOM explosion for the work order. An operation naming
>   the work order's **own** part is refused (it would ISSUE the part the FG receipt just RECEIVEd).
> - **Suppression runs in two layers**, so tied material is never issued twice: an OPEN operation-scoped
>   tie owns its part's demand (even before it consumes), **and** the signed ledger net suppresses any
>   part the ledger already shows consumed against this work order's operations — recorded as a
>   tamper-evident **`BACKFLUSH_DOUBLE_ISSUE_BLOCKED`** audit row rather than silently. A **fully
>   returned** tie nets to zero and is deliberately allowed to re-issue: the material physically came
>   back, so the BOM's demand is genuinely unmet again.
> - **A blocking diagnostic REFUSES the demand it describes, and the refusal is recorded** (PR 4.5).
>   The opt-in gate is a one-time check and everything it reads stays editable afterwards by anyone
>   with `boms:edit`, so a BOM edited after a part was armed would otherwise move material against a
>   figure the resolver has itself judged wrong — previously with no log line, no audit row and no
>   event, leaving only an ordinary-looking ledger row. Each blocking diagnostic now drops **that
>   component's** demand when it names a `component_part_id`, or the **whole leg** when it names none
>   (the demand is then incomplete in a way no component owns — four codes today: `deleted_active_bom`,
>   `bom_depth_exceeded`, `missing_component_part`, and the foreign-component branch of
>   `foreign_component_part`, which carries no identity by design), and writes one
>   **`BACKFLUSH_DEMAND_REFUSED`** audit row carrying the code, the operator sentence, the BOM line or
>   operation it names, and the quantity that did **not** move. Under-issuing is the recoverable
>   direction — the material is still on the shelf; over-issuing writes it into an as-built record
>   that never contained it. `GET /work-orders/{id}/backflush-preview` reports the same refusal as
>   `suppression_reason: "blocking_diagnostic"`, so the dry run and the outcome agree.
>   **`refused_quantity` is attributed once per refused SCOPE, not once per row.** One BOM line can
>   raise several blocking diagnostics and two lines can name one component, so the first row naming a
>   given component carries the quantity and every later one carries `0` (the structural tier likewise
>   charges its whole-leg total to the first structural row). Summing `refused_quantity` over
>   `BACKFLUSH_DEMAND_REFUSED` therefore gives the real quantity that did not move — the rows still
>   number one per diagnostic, because each names a different thing to fix. The same rows that carry
>   the quantity emit the `material.backflush_demand_refused` notification, so one refused component
>   notifies once rather than once per condition it violates.
>
> **Completion also consumes tied material (material allocations).** A work order can optionally be
> **tied** to stock material via `…/material-allocations` (see "Material ties" below). Consumption is
> **never a separate endpoint** — authority to complete the work is what authorizes the movement — and
> it always joins the completing handler's transaction, so it is atomic with the status change. **An
> untied work order is untouched** — no ledger row, no audit row, no event (asserted by test).
>
> **Consumption fires when an OPERATION completes, and again (as a reconcile) when the WORK ORDER
> completes.** Two seams, deliberately different in scope:
> - **Operation completion** — `apply_operation_completion_inventory_effects`, called by the four
>   operation-completion handlers (`POST /shop-floor/clock-out/{time_entry_id}` when it closes the
>   operation, `POST /shop-floor/operations/{id}/complete`, `POST /work-orders/operations/{id}/complete`,
>   and the per-operation leg of `POST /work-orders/{id}/complete`) right after the operation is
>   flipped `COMPLETE`. It reconciles **that operation's ties only**. A laser child work order carries
>   **one operation per nest**, so completing nest 1 of 3 now deducts **nest 1's sheet**. Scope is one
>   operation on purpose: a still-`IN_PROGRESS` operation can still be walked back by **either**
>   reduce-production verb — including the operator's own self-service one — so consuming against one
>   would let an operator strand material with no supervisor in the loop. (A **COMPLETE** operation is
>   now correctable through the **office** verb, which is safe precisely because the reasoned material
>   return exists to hand the material back; the operator verb still refuses. See "Over-count
>   correction … (supervisor/office)" above.) Every call site
>   is inside its handler's non-terminal branch, so a finished/cancelled job never consumes.
> - **Work-order completion** — `apply_completion_inventory_effects`, unchanged, on all five of its
>   existing call sites (kiosk clock-out, shop-floor and office operation complete, force-complete,
>   reconcile-on-read). It reconciles **every** open tie on the work order and is now the **self-heal**:
>   whatever an operation-level post missed still flushes here, and whatever already posted computes
>   `delta = 0` and writes nothing. The FG receipt and the BOM backflush stay here and did **not** move
>   to operation completion (per-operation they would double-receive a multi-operation job and collide
>   with `uq_wo_inventory_issue`).
>
> **Reporting production is still NOT a consumption trigger.** A partial production report on an open
> operation — `POST /shop-floor/operations/{id}/complete` short of the full quantity, or a clock-out
> that does not close the operation — moves **no stock**. Keying 3 of 6 runs on an unfinished nest
> deducts nothing; the sheets move when that operation closes. Client copy built on these numbers must
> say "deducts when this operation completes", never "deducting now".
>
> **Force-complete is a no-op for consumption in practice.** `POST /work-orders/{id}/complete` calls
> the seam for symmetry, but it never writes `operation.quantity_complete`, so `target` is 0 and the
> sum-delta is non-positive: a force-completed operation's tie posts nothing unless the operation
> already carried produced quantity from an earlier report. See
> `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Residual gaps of the operation-completion trigger".
>
> The two tie shapes behave differently:
> - **Operation-scoped** ties (`work_order_operation_id` set — the laser-nest case) are reconciled
>   **per operation** as a **sum-delta**: `target = qty_per_run × (operation
>   quantity_complete + quantity_scrapped)`, and a negative `ISSUE` is posted for
>   `target − qty_consumed` whenever that delta is positive. Because the target is recomputed from
>   live operation state, the operation-completion post, a replay, and the work-order reconcile all
>   converge instead of double-issuing — the later caller simply sees `delta = 0`
>   (see `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Capability vs. wiring"). These rows
>   carry **`reference_type='work_order_operation'`** with `reference_id` = the **operation** id
>   (and `reference_number` = the work-order number) — deliberately outside the
>   `uq_wo_inventory_receipt` / `uq_wo_inventory_issue` idempotency predicates, which key on
>   `reference_type='work_order'`.
> - **Work-order-scoped** ties (no operation) are drained by the work-order-completion backflush, **as
>   their own leg**, and reconciled to `qty_planned` against that tie's signed ledger net (ISSUE −
>   RETURN, keyed on `allocation_id`). Their rows carry **`reference_type='work_order_backflush'`**
>   with `reference_id` = the **work order**, also outside the `uq_wo_inventory_*` predicates. Unlike
>   the BOM leg this is **not** gated on `backflush_components` — an explicit tie is itself the opt-in.
>   The drain advances the tie's `qty_consumed` to that ledger net (never to `qty_planned` regardless
>   of what posted) and **audits** the advance (`work_order_material_allocation` UPDATE,
>   `extra_data.reference_type = "work_order_backflush"`), exactly as the per-run engine audits its own.
>   Tie material **before** the work order completes.
>
>   > **Changed in PR 4.4 — the previous contract is stated so a stale integration is not read as a
>   > regression.** Through PR 4 a work-order-scoped tie was *summed per part* with any BOM demand and
>   > drained as **one** `ISSUE` row under `reference_type='work_order'`, because
>   > `uq_wo_inventory_issue` permitted exactly one row per (work order, part). It is now **two
>   > separately-attributed rows** — the tie row carries `allocation_id`, the BOM row has it NULL — and
>   > the tie leg posts **first**, so a shortage lands on the derived side and the tie's lot pin gets
>   > first claim on stock. **The total issued for a part carrying both demands is unchanged.** The
>   > unpinned draw also spills across lots now, so one logical draw can be N rows naming N lots.
>   > A `POST` of a work-order-scoped tie on a part carrying a **legacy** (pre-4.4) `work_order`-shaped
>   > `ISSUE` on the same work order is still refused **409** — see the error table below — but that
>   > refusal is now a legacy-only fence and is **unreachable** in practice.
>
> **Tied material counts as job-cost material.** `WorkOrder.actual_cost`, the synced `JobCost`, and
> the analytics cost variance sum `abs(total_cost)` over the work order's `ISSUE` rows under **all
> three** reference shapes (`work_order`, `work_order_backflush` and `work_order_operation`) — one
> shared predicate (`work_order_ledger_filter`), so the stored
> rollup and the analytics leg cannot drift. A nest that burned six $80 sheets contributes $480.
>
> **Scrap consumes**, and posts as `ISSUE` (not `SCRAP`): lot genealogy filters on `ISSUE`, so a
> `SCRAP` row would erase audited scrap material from the as-built record. The good/scrap split is
> recorded in the transaction `notes`.
>
> **Consumption never auto-reverses.** A negative delta (e.g. after an over-count walk-back) is a
> **no-op**, never an automatic `RETURN` — the material was already cut. Reversal is a separate,
> explicit, reasoned verb: `POST …/material-allocations/{allocation_id}/return`, shipped in PR 3 and
> documented under **Material ties** below. (This paragraph said the verb *"does not exist yet"* for
> three PRs after it shipped; corrected 2026-07-27.)
>
> **A shortage never fails the completion.** Insufficient stock drives the source lot negative,
> writes a tamper-evident **`ALLOCATION_SHORTAGE`** audit row, and emits a
> `material_allocation_shortage` warning event. It is the allocation twin of `BACKFLUSH_SHORTAGE` /
> `backflush_shortage` and is deliberately kept distinct from it in the audit trail. **Both are now in
> the notification catalog** — `material.allocation_shortage` and, since PR 4.4,
> `material.backflush_shortage` (both Purchasing / warning, in-app + email to the Purchasing and
> Inventory departments); the backflush event had been emitted with no catalog row since Batch 6 and
> therefore notified nobody. The **rolled-back** case — where the draw raised and the savepoint undid
> it, so no stock moved at all — is separately keyed as `material.allocation_consumption_failed` /
> `material.backflush_failed`, so "stock went negative" and "stock never moved" are distinguishable
> without opening the audit log. PR 4.5 adds a third backflush key,
> **`material.backflush_demand_refused`** (same category / severity / channels / recipients), for the
> case where no draw was attempted at all because a blocking diagnostic condemned the demand — see the
> completion-time refusal above. It is emitted **once per refused scope**, not once per diagnostic.
> See [docs/NOTIFICATIONS.md](NOTIFICATIONS.md).
>
> **`GET /work-orders/{id}/backflush-preview` — the dry run (PR 4.5).** Returns, for this work order,
> what a completion would draw out of stock: one `BackflushPreviewLine` per component, each carrying
> `required_quantity`, `already_issued`, the `delta_quantity` that would actually post, the ordered
> `lots` the draw would hit, `shortfall` / `would_go_negative`, and — where the shared policy passed over
> segregated stock — `held_quantity_skipped` / `held_lot_numbers`. Three fields answer questions the
> first cut of this endpoint could not: each lot carries **`is_shortfall`** (the writer posts the unmet
> remainder as a SECOND issue against the last lot it drew, driving that lot negative and putting *its*
> number on the as-built record — so a line may legitimately list one `inventory_item_id` twice);
> **`shortfall_creates_placeholder`** says the part has no stock row at all, so the completion would
> mint a lot-less placeholder row instead; and **`pinned_lot_is_held`** says a tie's pinned lot has gone
> on hold / quarantine / rejected *since* it was pinned and the completion will consume it anyway
> (recording `HELD_MATERIAL_CONSUMED`) — the one warning the `held_*` fields structurally cannot carry,
> because a pinned draw is never short. Response-level `blockers` /
> `advisories` are the demand resolver's diagnostics for **this** work order, including the routing
> conditions the part-level readiness check cannot see. Any authenticated tenant user; a cross-tenant or
> unknown id is **404**.
> - **It models the ISSUE LOOP, not just the demand resolver** — both legs in the real order
>   (work-order-scoped ties first, so a tie's lot pin gets first claim), the legacy
>   `('work_order', ISSUE)` fence, the reconcile-to-target delta, and the actual lot pick through the
>   same `consumable_source_items` + `plan_stock_draw` the writer uses. Preview and outcome therefore
>   cannot disagree about which heat gets consumed — which is exactly the failure a preview built on its
>   own predicate would produce silently.
> - **Pure read. It writes NOTHING** — no ledger row, no `audit_log` row (in particular no
>   `BACKFLUSH_DOUBLE_ISSUE_BLOCKED`, which the suppression layer used to write from inside the
>   resolver), no operational event, nothing to commit. That is **structural**, not careful: the
>   resolution layer takes no `AuditService` at all and the recording layer is a separate function only
>   the completion path calls. Same rule as the per-allocation consumption read on this router — a poll
>   is not an actor and records no reason.
> - **Lines appear whether or not the part has opted in** (each carries `requires_opt_in`, and the
>   response carries the part's current `backflush_components`), because the operator reading it is
>   deciding whether to opt in.
> - **`suppression_reason` values:** `converged` (the ledger already holds the whole target — the
>   healthy steady state), `already_issued` (a legacy pre-4.4 one-shot row fences this work order out
>   for that part, permanently), `ledger_consumed` (a tie already drew it), `open_operation_tie` (an
>   open operation-scoped tie owns the demand; the material still moves, on the per-run engine), and
>   `blocking_diagnostic` (a blocking diagnostic stands, so the completion refuses that component and
>   records `BACKFLUSH_DEMAND_REFUSED` instead of issuing it).
> - **`basis` is `quantity_complete + operation scrap`.** A work order that has produced nothing has a
>   basis of 0 and therefore **no BOM lines at all**. That is the resolver's real behavior, not a preview
>   artifact — and it is why part-level readiness runs at a synthetic basis of 1.0 instead.
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
>
> **Over-count correction — `POST /work-orders/operations/{operation_id}/reduce-production`
> (supervisor/office).** The role-gated twin of the operator's
> `POST /shop-floor/operations/{id}/reduce-production` (see "Over-count correction" under Shop
> Floor for the shared semantics — one shared core, the two paths cannot drift). It walks back
> good-count quantity that was over-reported on an operation — a
> miscount correction, **not** a scrap move (scrap fields and statuses are never touched).
> Differences from the shop-floor verb:
> - **Role-gated**: `require_role([ADMIN, MANAGER, SUPERVISOR])` — an Operator gets **403** (this
>   verb corrects **other operators'** labor records, a Work Orders **Edit** power, not operator
>   self-service). Kiosk-scoped tokens can't reach it (path-fenced away from `/work-orders`).
> - **A COMPLETE operation is correctable here.** The office verb passes
>   `allow_completed_operation=True`; the operator's twin does not, and **neither** accepts a
>   terminal work order. Both verbs previously hit the identical 409, so the operator was told to
>   "ask a supervisor" whose own front door refused the same thing. The refusal was set on the
>   rationale that a completed operation's downstream inventory / cost / FG effects had fired and
>   could not be walked back; the reasoned **material return**
>   (`POST /work-orders/{id}/material-allocations/{alloc}/return`) is now that walk-back, and
>   lowering a completed operation's count is precisely what opens the bounded
>   `correct_over_consumption` allowance the return is measured against. **Order matters: reduce
>   first** (the count is the record), then return the material the lower count no longer accounts
>   for. A corrected COMPLETE operation stays COMPLETE — it just carries a truthful count.
> - **No clock-in required** — the supervisor is correcting from the office, not working the
>   operation.
> - **Scope: ALL unapproved labor on the operation, any operator's** — the walk goes open entries
>   first, then closed entries newest-first; the bound is the **sum of unapproved
>   `quantity_produced`** across those entries. **APPROVED entries are excluded** (approval is the
>   immutability boundary, G5-A): to correct signed-off labor, unapprove it first via
>   `POST /shop-floor/time-entries/{id}/unapprove` (the audited segregation-of-duties front door),
>   then reduce.
> - **`notes` goes on the audit row only** — deliberately **not** written onto another operator's
>   labor record (the shop-floor verb appends it to the caller's own active entry).
> - **`source`**: `import` is rejected **422** (loader-reserved), same as the shop-floor verb; no
>   kiosk forcing applies here.
>
> Body is the same `ProductionReductionRequest` (`quantity_delta` required `> 0` finite; `reason`
> required non-blank ≤ 255 — a **correction** reason, not a scrap reason; optional `source` /
> `notes`). Everything else matches the shop-floor twin: **tenant-scoped 404** before any mutation,
> the **terminal-work-order** refusal (**409** `"This work order is complete, closed or cancelled --
> its recorded production can no longer be corrected"` — a message deliberately **split** from the
> operator's "ask a supervisor" one, since referring a caller to a supervisor for a terminal work
> order would be a false referral in the other direction; re-checked under the op→WO row locks with
> the same predicate the pre-lock read used, so the fast-fail and the authoritative check cannot
> drift), **row-locked +
> optimistic-locked** (concurrent stale write → **409**), the same **recomputed WO rollup** (max
> over non-component siblings — or, on a laser dispatch-pool WO, the **sum** of per-nest progress
> capped at the WO total — only ever lowered), a **tamper-evident `audit_log` row** (action
> `reduce_operation_production`, old→new quantities, the reason, and the per-entry before/after
> slices in walk order), an `operation_production_reduced` operational event, and the shop-floor /
> work-order / dashboard broadcasts. The bound refusal is **400** with an explanatory message:
> when approved labor exists on the operation, `"Only N piece(s) on this operation are unapproved
> and correctable; M piece(s) are on approved labor -- unapprove it first."`; otherwise `"Only N
> piece(s) are recorded on this operation's time entries; the correction cannot exceed the recorded
> evidence."`. Response **200** carries `message`, the updated `operation` block, a
> `work_order {id, quantity_complete}` block (the recomputed rollup), and `reduced_time_entries[]`
> — the per-entry paper trail of the walk (`{time_entry_id, entry_type, quantity_produced_before,
> quantity_produced_after}`, in walk order). See the "Over-count correction Schema" under Shop
> Floor. In the app this is the **Correct count** action on each operation row of the work-order
> detail page (gated on `work_orders:edit`).

> **Operation work-center reassignment (`PUT /work-orders/operations/{id}`).** The update body
> (`WorkOrderOperationUpdate`) accepts an optional **`work_center_id`** — a planner move of the
> operation to another work center (e.g. re-dispatching a laser nest to a different laser, but
> legitimate for any operation). Checked **before any mutation**:
> - The operation must be idle: while it is **IN_PROGRESS** or **any open time entry** exists on it
>   → **409 Conflict** (`"Clock out before moving the operation to another work center"`).
> - **COMPLETE** operations cannot be moved — a finished run's labor history belongs to the machine
>   it ran on → **409** (`"Completed operations cannot be moved to another work center"`).
> - The target must be an **active** work center in the caller's company — inactive or cross-tenant
>   → **404** (`"Work center not found"`).
>
> On success the operation's `operation_group` is re-derived from the new work center (the same
> derivation used at creation) so queue/grouping views stay consistent, its **`run_order` is cleared
> to `null`** (the manual dispatch rank is scoped to the work center it was ranked in, so the
> operation lands unranked at the tail of the new column — see "Dispatch run order" under Shop
> Floor), all three fields ride the endpoint's tamper-evident `audit_log` row (old → new), and both
> work centers' persisted availability rates are refreshed. An explicit `null` is ignored
> (`work_center_id` is non-nullable on the model), and re-sending the current work center is a
> no-op. In the app this is the per-nest work-center control on the WO detail page's Laser Nest
> Package card, and the Dispatch Board's per-card machine select / cross-column drag.
>
> The Scheduling page's `PUT /scheduling/operations/{id}/work-center` (drag / bulk move) enforces
> the **same contract** — tenant-scoped lookups, the two 409 refusals, `operation_group` refresh,
> the `run_order` clear, and an audited old → new diff — so the two reassignment paths cannot
> disagree.
>
> **Every reassignment path clears the rank, including the reschedule routes.**
> `PUT /scheduling/work-orders/{id}/schedule` and `POST /scheduling/work-orders/{id}/schedule-earliest`
> both accept a `work_center_id` that reassigns the work order's current operation — that is a move,
> and it drops the rank too. (Re-sending the operation's current work center is a no-op and leaves
> the rank alone.) All four call sites share one helper, `dispatch_service.clear_run_order_on_move`,
> so a rank can never be carried into a column where it would outrank work the manager actually
> ordered there.
>
> **Both reschedule routes are audited like the dedicated move endpoints.** Each writes a
> tamper-evident `audit_log` row (`log_update`, resource type `work_order_operation`) on the work
> order's current operation with the `work_center_id` / `run_order` / `scheduled_start` /
> `scheduled_end` / `status` old → new diff — previously they emitted only operational events. A
> `PENDING → READY` flip triggered by scheduling rides the same row's `status` diff, and the row
> commits atomically with the schedule write. Downstream operations rewritten by the schedule
> cascade are **not** individually audited (deliberate scope) — their count rides in the row's
> `extra_data` (`downstream_operations_scheduled`, alongside `via: "schedule" |
> "schedule_earliest"`, `work_order_id`, `forward_schedule`). A re-submit that changes nothing
> self-suppresses (no row is written).
>
> **Completion deliberately does *not* clear `run_order`.** A completed operation is already
> filtered off every queue, and the historical rank is evidence of what the shop was told to run.
>
> **Laser nest WOs refuse free-form operations.** `POST /work-orders/{id}/operations` returns
> **400** on a `laser_cutting` work order — dispatch pools are managed exclusively by the nest
> package import and manual nest entry, so a non-nest op can never ride the laser gating exemption.

> **Duplicating a work order (`POST /work-orders/{id}/duplicate`).** Re-runs a job's **plan** without
> re-entering it — the motivating case is a 40-nest laser package confirmed once through the import
> wizard and run again next month, without re-uploading the PDFs or re-confirming a single row. Body:
> `quantity_ordered` (required, `> 0`) and `due_date` (optional; `null` leaves it unset). Unlike
> `WorkOrderCreate`, `due_date` carries **no "not in the past" validator** — a duplicate is most often
> raised to re-run something that is already late. Response **201**, and it is an **envelope, not a
> bare work order** — the one endpoint in this section that does not return the resource directly:
>
> ```json
> {
>   "work_order": { "…": "the same shape GET /work-orders/{id} returns" },
>   "skipped_operations": [
>     {"source_operation_id": 812, "operation_number": "10", "sequence": 10, "reason": "laser_nest_deleted"}
>   ],
>   "skipped_material_allocations": [
>     {"source_allocation_id": 44, "part_id": 91, "source_work_order_operation_id": 812, "reason": "part_not_available"}
>   ]
> }
> ```
>
> **Copied (the plan):** every operation with its setup/run instructions, work center, inspection
> flags and component fields; the live laser nests (CNC number, material, thickness, sheet size,
> planned runs, work center) onto **one** new package; the **open** material ties; and a re-snapshot of
> the process-sheet steps. **Not copied (the production record):** `quantity_complete` /
> `quantity_scrapped` and their scrap reasons, actual dates / hours / cost, lot and serial numbers,
> release info, `current_operation_id`, scheduled dates, and time entries — copying any of it would
> fabricate history on a job that has not run, which an AS9100D reader would take for a real record.
> Three further omissions are decisions rather than oversights:
> - **`parent_work_order_id`** — the duplicate is an **independent** work order. Re-attaching it to the
>   source's assembly parent would add a second laser child against demand the first child already
>   satisfied, and the parent's completion rollup would count both. A genuine second child is a nest
>   import against the parent, not a duplicate.
> - **`must_ship_by`** — it is the **original** order's promise, and it outranks `due_date` in OTD/OTIF
>   scoring (see [docs/LEAN_ROADMAP.md](LEAN_ROADMAP.md)). Carrying it would silently override the
>   `due_date` just supplied and score the new job against a promise nobody made for it.
> - **`run_order`** — a manager's dispatch ranking for one machine's board, not part of the plan. A
>   40-nest duplicate arriving pre-ranked would, at release, displace the sequence the manager set for
>   work already queued at that laser. (`scheduled_start` / `scheduled_end` are dropped for the
>   adjacent reason: they are `SchedulingService` output for the *source's* dates, and release
>   reschedules anyway.)
>
> **A nest-bearing work order's quantity is DERIVED, not chosen.** When nests come across, the server
> **ignores** the requested `quantity_ordered` and stores the sum of the copied nests' `planned_runs` —
> the same definition `_recompute_child_quantity_ordered` enforces and every nest mutation path
> re-asserts, so honoring the caller here would leave the duplicate as the one laser WO in the system
> where that is false, until the next nest edit corrected it out from under the planner. Read the
> quantity back off the response rather than assuming what was sent was stored; the audit row records
> `requested_quantity` when the two differ. (The UI disables the field with that reason on it.)
>
> **Quantity-derived plan numbers are SCALED** by `new_qty / source_qty`: operation `run_time_hours`
> and header `estimated_hours` / `estimated_cost` are stored **pre-multiplied by the ordered
> quantity**, so an unscaled copy would claim the source's hours at the duplicate's quantity —
> scheduling sizes capacity from `run_time_hours` and job costing reads it first. `setup_time_hours`
> is deliberately **not** scaled (setup is per-job). The nest path returns a ratio of exactly **1.0**
> on purpose: the runs carry across verbatim, so per-run nothing about the plan changed.
>
> **Process-sheet steps are RE-snapshotted, never copied** (`wo_operation_steps`), from each sheet
> family's *currently released* revision — the same resolution `POST /work-orders/` performs. Copying
> the source's snapshot rows would freeze a revision that may since have been superseded; copying
> nothing would silently disarm the operation-completion gate on a job whose whole premise is "same
> plan as last time". See [docs/PROCESS_SHEETS_SCOPE.md](PROCESS_SHEETS_SCOPE.md) → snapshot semantics.
>
> **Material ties land inert.** `qty_consumed = 0`, `status = open`, and the **pinned lot / pinned
> inventory item are ALWAYS cleared** — a lot pin says "consume from *this* lot", and the lot the
> source job pinned was very likely consumed by the source job. `qty_planned` is **recomputed**, not
> copied: `qty_per_run × planned_runs` for a nest-backed operation tie (re-derived, because a nest tie
> has a better basis than the old number), and the **source value scaled by the quantity ratio** for
> every other tie — ordinary operation-scoped *and* work-order-scoped alike. That matters because
> `qty_planned` is caller-supplied and INDEPENDENT of `qty_per_run` on the tie API: a tie created as
> "500 lb to OP20" with no `qty_per_run` would otherwise reappear as `1.0 × quantity_ordered`, a silent
> rewrite with no skip and nothing on the response. A same-quantity duplicate therefore reproduces
> both shapes bit-for-bit. `unit_of_measure` is re-snapshotted from the part, since the
> column is a snapshot of `Part.unit_of_measure` *at tie time* and this tie's time is now.
>
> **Refusals — nothing is written in any of these cases:**
> - **404** — the source work order is not in the active company, or is soft-deleted. There is
>   **no status gate**: the headline case is duplicating a COMPLETE job, so a terminal source is
>   expected and allowed.
> - **409** — the source's produced part has since been **soft-deleted**. A retired part must not go
>   back into production in one click. (Part-less standalone laser WOs are exempt — there is no part.)
> - **409 `PROCESS_SHEET_UNAVAILABLE`** — an attached process-sheet family has no released revision.
>   The same structured detail `POST /work-orders/` raises for the same condition; the rule is that a
>   duplicate must never mint a work order the create path would have rejected.
> - **409** on an `IntegrityError` (a work-order-number race, a nest key collision, a violated CHECK on
>   the copied data). The message deliberately does **not** promise that a retry helps: only the number
>   race is transient, and the rest are properties of the source and would fail identically forever.
>
> **Skips are first-class, not silent.** A skip is **not** an error — the work order was created and is
> a valid draft — but it means the copy is *missing* something the source had, and the planner has to
> be told: a skipped material tie that nobody surfaces means the job runs, no shortage shows, and stock
> is never deducted until the inventory count disagrees. Every skip is written to the work order's
> audit `extra_data` **and** returned in the envelope, and both lists empty is the "clean copy" signal.
> Reasons — operations: `laser_nest_deleted` (the operation's nest was soft-deleted, so copying it
> would put a nest task with no nest on the kiosk queue at release). Ties: `part_not_available` (the
> tied part has been soft-deleted, which `POST …/material-allocations` refuses outright),
> `operation_not_copied` (its operation was skipped — re-scoping the tie to the work order is not
> available, since a work-order-scoped tie carrying `qty_per_run` is a 422 on the tie API), and
> `nest_runs_unavailable` — **server-side defence, not currently producible**: an operation that is
> nest-backed with no run count is already skipped upstream as `laser_nest_deleted`, so its tie reports
> `operation_not_copied` first. The branch is kept because the alternative it guards against is
> planning at the work-order quantity, which for a laser WO is the sum of *every* nest's runs and would
> inflate one nest's demand by roughly the nest count. **Treat the reason list as open** — render an
> unrecognized value verbatim rather than dropping the row.
>
> **Audit / lineage.** The duplicate carries **no FK back to its source**, so the work-order
> `log_create` row is the only place that lineage exists: it records `source_work_order_id` and
> `source_work_order_number`, plus `skipped_operations`, `skipped_material_allocations`,
> `process_sheet_snapshot` (which sheet family resolved to which released revision) and
> `quantity_ratio`. Nest and tie rows are audited byte-parallel to the import and tie-creation paths.
> Everything — header, operations, step snapshots, nest package, nests, ties and every audit row —
> flushes into **one** transaction, so a partial duplicate (a header with no nests) cannot survive a
> failure mid-copy.

> **Material ties (`/work-orders/{id}/material-allocations`).** The optional tie between a work order
> (or one of its operations) and a **material** part — what makes stock deplete as work completes.
> Ties are **opt-in and additive**: a work order with no allocation rows behaves exactly as it did
> before the feature, and there is no flag on `work_orders` and no default allocation. **Exactly one
> verb on this router posts inventory: `POST …/{allocation_id}/return`.** Every other endpoint
> manages the planning row only; consumption happens on the completion paths (see "Completion also
> consumes tied material" above). The return is the deliberate exception, because un-consuming can
> never be something a completion path does for you — see "Returning consumed material" below.
>
> The endpoints live on a **sibling router** under the same `/work-orders` prefix
> (`app/api/endpoints/work_order_materials.py`, OpenAPI tag **Work Order Materials**). They are no
> longer dark: the **Materials panel on the work-order detail page** reads `GET`, drives `PATCH` /
> `DELETE` and hosts the **return dialog** (`GET …/consumption` then `POST …/return`), and the two
> nest-creation paths write ties server-side (see Laser Nests → "Nest material
> ties"). The read-only floor surfaces — the dispatch-board `material_tie` chip and the kiosk
> `material_ties` line — do **not** call this router at all; they ride the shop-floor reads, which is
> what keeps the kiosk path fence unwidened.
>
> Reads are open to any authenticated tenant user; **every mutating verb** is
> `require_role([ADMIN, MANAGER, SUPERVISOR])`. The endpoints are deliberately **not** under
> `/api/v1/shop-floor`, so kiosk-scoped operator tokens are path-fenced away from them — tying
> material is an office/planning act, and the **return** sits on the same side of that fence for a
> stronger reason than the rest: moving stock back with a reason is a bigger power than tying it.
>
> - **`GET …/material-allocations`** — every tie on the work order, ordered by id. `include_inactive`
>   defaults to **`true`**: `cancelled` (and `closed`) rows are the tombstones the ledger's
>   `allocation_id` resolves to, so hiding them would make consumed material look untied. Pass
>   `include_inactive=false` for open ties only. A tie a **nest re-import detached** reads back with
>   `work_order_operation_id: null` — the column is cleared so the superseded operation row can be
>   deleted — plus **`detached_from_operation_id`** naming the operation it used to be scoped to,
>   read back off the audit chain. Without that echo a detached tie is byte-identical to one that
>   was always work-order-scoped. It is `null` on every tie that was never detached, and it is a
>   reporting field: the `audit_log` row remains the record of record.
> - **`POST …/material-allocations`** → **201**. Body (`MaterialAllocationCreate`): `part_id`
>   (**required** — the material part, never the part being produced), `work_order_operation_id`
>   (optional; **set ⇒ operation-scoped / per-run**, omit ⇒ work-order-scoped / one-shot), `source`
>   (`nest` | `bom` | `manual`, default `manual`), `qty_per_run` (optional, `> 0`; **operation-scoped
>   only** — the server stores **`1.0`** when omitted on an operation-scoped tie, and sending it on a
>   work-order-scoped tie is a **422**, the same answer `PATCH` gives, since there are no runs to
>   scale by), `qty_planned` (**required**, `> 0`), `pinned_inventory_item_id` (optional
>   — consume from *this* lot; omit for automatic lot selection at consume time; a **held or
>   inactive** lot is refused with 422). The pin is honored on **both** tie shapes: an
>   operation-scoped tie consumes from it per run, and a work-order-scoped tie carries it into the
>   completion backflush's tie leg. **Unpinned selection is now identical on both tie shapes**
>   (PR 4.4): `received_date ASC NULLS LAST, id ASC` FIFO across consumable lots, spilling across as
>   many lots as the demand needs. "Consumable" is `is_active` **and**
>   `COALESCE(status, 'available') = 'available'` — so a legacy **NULL-status** lot **is** eligible,
>   while `on_hold` / `quarantine` / `rejected` lots are **skipped**. Through PR 4 a work-order-scoped
>   tie instead took the **lowest-id active on-hand lot** with **no status filter**, could therefore
>   land on a held lot and consume it (writing `HELD_MATERIAL_CONSUMED` with `pin_directed: false`),
>   and used a single lot for the whole demand. Held stock is no longer consumed on the unpinned path;
>   it is **disclosed on the shortage record** instead (`held_quantity_skipped` / `held_lot_numbers`),
>   so a shortage is never reported bare against material sitting in segregated status. On a **pinned**
>   draw the shortage record names the **pin** instead (`pinned_lot`) — there the pin, not any lot's
>   status, is why the rest was not drawn, and the two clauses are mutually exclusive. Both live on the
>   audit record and the event payload only; a lot that was skipped appears on **no** genealogy line. A
>   `HELD_MATERIAL_CONSUMED` row now means one thing only: a **pinned** lot held after it was pinned
>   (`pin_directed` is always `true`). `notes`. `unit_of_measure` is
>   **snapshotted** server-side from the part at tie time and is not client-settable. The work order
>   must **not** be terminal (409).
> - **`PATCH …/material-allocations/{allocation_id}`** — all fields optional; omitted fields are left
>   alone. Accepts `qty_per_run`, `qty_planned`, `pinned_inventory_item_id`,
>   `clear_pinned_inventory_item` (`true` drops the pin, back to automatic lot selection — since
>   PR 4.4 that is the **same** `received_date` FIFO over consumable lots on **both** tie shapes, per
>   the `POST` note above; sending it **together with** a `pinned_inventory_item_id` is a **422**, since
>   the two ask for opposite things about a field that is a genealogy fact), and `notes`. Lowering `qty_planned` **below** `qty_consumed` is a
>   **422** — the engine never auto-reverses, so the row would immediately read as over-consumed. `part_id`,
>   `work_order_operation_id` and `source` are **deliberately not editable** — repointing a tie after
>   consumption posted would rewrite genealogy; untie and re-tie instead. Consumption already posted
>   is untouched: raising `qty_per_run` re-targets the sum-delta engine so the *next* completion tops
>   up the difference, and lowering it is a no-op until the target overtakes what was consumed.
> - **`DELETE …/material-allocations/{allocation_id}`** — the untie. Returns **200** with the updated
>   row; it sets `status = "cancelled"` and **never physically deletes** (the ledger's `allocation_id`
>   back-reference must keep resolving). Idempotent: untying an already-cancelled tie is a no-op that
>   writes no second audit row. Audited as a `log_delete(soft_delete=True)` on
>   `work_order_material_allocation`. Still refused **409** while the **ledger** shows material out
>   against the tie — cancelling a tie that moved stock, without moving it back, would strand the
>   ledger's `allocation_id` rows against a tombstone with no account of where the material went. The
>   409 names the verb that does both: `POST …/return` with `intent: "return_and_untie"`. **The basis
>   is the signed ledger net (ISSUE − RETURN), not the `qty_consumed` cache** (re-keyed in PR 4, the
>   last of the three guards still reading the cache), so a **fully returned** tie can be untied.
> - **`GET …/material-allocations/{allocation_id}/consumption`** — where this tie's material came
>   from, per source lot, and how much of each lot can still take it back. Open to **any
>   authenticated tenant user** (like the tie list — it discloses ledger facts about material the
>   company already owns, and a return dialog that could not show them would be asking for a
>   confirmation nobody could give). Answers from `inventory_transactions`, **never** from the tie's
>   `qty_consumed` cache, and works on a `cancelled` tie — whose consumption is exactly what an
>   operator most often needs to see. Array of `MaterialConsumptionLine`:
>   `{inventory_item_id, lot_number, issued, returned, net}`. `net` (`issued − returned`, float dust
>   clamped to 0) is the per-lot **cap** on any further return, and the array is ordered **newest
>   source lot first** — the exact order a return credits in, so preview and outcome cannot disagree.
>   A lot whose `net` is 0 is still listed: it is part of the tie's movement history, and dropping it
>   would make a fully-returned tie look as though the material had never touched that lot. There is
>   deliberately **no lot to choose** — this is a disclosure, not a picker. Pure read; it moves
>   nothing and writes nothing.
>
> **Returning consumed material — `POST …/material-allocations/{allocation_id}/return`.** The
> reasoned reversal, and the only un-consume there is. Consumption never auto-reverses (the consume
> path also runs from a reconcile-on-read `GET`, where there is no actor, no intent and no reason to
> record); this is that same reversal with all three attached — the compensating-transaction +
> required-reason + audit pattern the receiving corrections established. **Nothing historical is
> mutated**: every credit is an **appended** positive `RETURN` `InventoryTransaction`.
>
> Body (`MaterialReturnRequest`): `quantity` (**required**, `> 0`, in the tie's UoM), `intent`
> (**required**), `reason` (**required**, non-blank, ≤ 500 — validated at the Pydantic boundary, so a
> blank one is FastAPI's own **422**).
>
> **Two named intents, and nothing in between:**
>
> | `intent` | Bound on `quantity` | Tie afterwards |
> |---|---|---|
> | `correct_over_consumption` | `qty_consumed − live target` | stays `open` |
> | `return_and_untie` | must equal the **full** `qty_consumed` | `cancelled`, same transaction |
>
> The bound on `correct_over_consumption` is the engine's own arithmetic: it is exactly the negative
> delta the sum-delta engine computes and refuses to execute, so after the return `qty_consumed >=
> target` and the engine no-ops forever. Returning **less** than that on a still-open tie is refused,
> not merely discouraged — `target` is recomputed from live operation state on **every** call
> including a reconcile-on-read `GET`, so the material would be re-consumed on the next completion
> *or page load*, re-running FIFO and possibly crediting a **different lot** than it came from
> (fabricated heat/cert linkage in an as-built record, AS9100D 8.5.2). `return_and_untie`'s
> quantity is a **confirmation**, not a choice: a mismatch is a 422 that catches a stale client
> (a completion landed between page load and submit) rather than returning a different amount than
> the operator was looking at.
>
> **Material returns to the lots it came off, or not at all.** Source lots are walked **newest-first**
> (the reverse of how consumption posted), so a consumption that FIFO-spilled across three lots
> returns across those same three and one logical return becomes N ledger rows. Each row carries the
> compensated ISSUE's `reference_type` / `reference_id` (mirrored — `work_order_ledger_filter`
> matches on reference *shape*, never on transaction type, so job cost, analytics, lot genealogy and
> `GET /inventory/transactions?work_order_id=` pick it up unchanged), the same `allocation_id`,
> `reason_code: "MATERIAL_RETURN"`, and the **compensated row's `unit_cost`** — never the lot's
> current cost, since a revaluation between consume and return would strand residual material cost on
> the job. Returning **into a negative lot is expected** (a shortage-driven consumption drove it below
> zero) and is not guarded.
>
> **Idempotency is arithmetic, not an index**: capacity per `(allocation_id, inventory_item_id)` is
> `issued − already-returned`, so a replay cannot over-credit a lot. Allowed on a **`cancelled`** tie
> (a consumed-then-cancelled tie is a real state — a work-order soft delete cancels open ties
> regardless of consumption — and is exactly what the hard-delete 409 points at); a **soft-deleted**
> work order is still **404**, so restore the work order first (an audited verb that also re-opens
> the ties the delete cancelled) rather than moving stock against a job that is currently deleted.
>
> Response **200** (`MaterialReturnResponse`): `allocation_id`, `work_order_id`, `part_id`,
> `part_number`, `intent`, `unit_of_measure`, `quantity_returned`, `qty_consumed_before`,
> `qty_consumed` (after — still a **cache**; the ledger stays authoritative, and note this is the one
> path that makes `qty_consumed` go **down**), `status` (`open` after a correction, `cancelled` after
> an untie), and `returned_lots[]` —
> `{inventory_item_id, lot_number, quantity, unit_cost, transaction_id, compensated_transaction_id}`,
> one per credited lot. Render the per-lot breakdown, never one anonymous total.
>
> **Server-gated, therefore non-optimistic**: the whole point is that the server may refuse. Keep a
> loading state and render only what the server returns (the `detail` is safe to display verbatim).
>
> ⚠️ **A return does NOT unlock a nest re-import, a work-order hard delete, or the
> already-issued 409 on a work-order-scoped tie.** All three key on the **existence** of ledger rows,
> and a return *appends* a row rather than removing one — after a full `return_and_untie` the ISSUE
> **and** RETURN rows both still name the operation a rebuild would delete. Those refusals stand,
> correctly, and their messages say so. See `docs/MATERIAL_CONSUMPTION_PLAN.md` → Residual gaps.
>
> **The plain `DELETE` untie is the one refusal a full return DOES clear**, since PR 4 keyed it to the
> **signed** net rather than to existence. That is not an inconsistency with the three above: they ask
> "would this orphan a ledger reference?", where a RETURN row counts as durably as the ISSUE it
> compensates, while untie asks "is material still out?", where a returned tie is holding none. Do not
> generalize either answer to the other question.
>
> **Error contract** (all lookups are tenant-scoped, so a cross-tenant id is **404**, never 403):
>
> | Code | When |
> |------|------|
> | **404** | Work order unknown, cross-tenant, or soft-deleted (`"Work order not found"`) |
> | **404** | `part_id` unknown, cross-tenant, or soft-deleted (`"Material part not found"`) |
> | **404** | `work_order_operation_id` is not an operation **of this work order** (`"Operation not found on this work order"`) |
> | **404** | `pinned_inventory_item_id` unknown or cross-tenant (`"Pinned inventory lot not found"`) |
> | **404** | `allocation_id` unknown, cross-tenant, or not on this work order (`"Material allocation not found"`) |
> | **409** | **Duplicate open tie** — this part is already tied to the same scope. At most **one open tie per (work order, part)** for work-order-scoped ties and **one per (operation, part)** for operation-scoped ones. Enforced by an app-level check *and* by two partial unique indexes, so a concurrent race returns the same 409 rather than a 500. Re-tying after a `cancelled` row exists is allowed |
> | **409** | **Untie while material is still issued** — `DELETE` while the **signed ledger net** (ISSUE − RETURN) against this tie is positive (*"N <uom> of material is still issued against this allocation. Return the material with intent 'return_and_untie', which credits it back to its source lots and closes this tie in one step."*). The remedy is a single call; untie stays refused on its own terms, since cancelling a tie that moved stock without moving it back would strand the ledger's `allocation_id` rows against a tombstone. **The basis changed in PR 4: it reads the ledger, not the `qty_consumed` cache** — the cache misjudged this in both directions (a `correct_over_consumption` to a zero live target left it at 0 on a tie the ledger still backed; the backflush advances a work-order-scoped tie's cache to `qty_planned`, which is not what the ISSUE posted). **Signed**, not existence-keyed, so a **fully returned tie can be untied** — existence-keying would 409 forever while `return_and_untie` 422s with nothing left to return |
> | **409** | **`PATCH` on a non-open tie** (`"This allocation is <status>; only an open tie can be edited."`) |
> | **409** | **`POST` on a TERMINAL work order** (`complete` / `closed` / `cancelled`). Every completion path refuses to re-enter a terminal work order, so the tie could never consume — it would sit `open` at `qty_consumed` 0 advertising demand that will never be met. `PATCH` / `DELETE` / `GET` on an existing tie stay available, so a historical tie is still readable and fixable |
> | **409** | **`POST` of a work-order-scoped tie whose part carries a LEGACY one-time issue on this work order** (*"Part X already has a one-time issue recorded against work order Y; this tie could never consume — tie the material at the operation level instead, which posts outside the one-issue-per-work-order guard."*). **Wording corrected in PR 4.4; behaviour unchanged.** The guard keys on a `reference_type='work_order'` `ISSUE` row, and since PR 4.4 **nothing writes that shape** — the backflush posts `work_order_backflush` — so it matches only **pre-4.4** rows and is that work order's permanent fence out of the reconciling engine. The 409 names the remedy, and it is deliberately **not** "return the material": a return appends a compensating row and never removes the ISSUE row, while this check (and `uq_wo_inventory_issue` behind it) keys on that row's **existence** — so a return would leave this 409 firing exactly as before, having moved stock for nothing. An operation-scoped tie posts outside the index and is unaffected. **The refusal is also UNREACHABLE and is kept deliberately**: creating a tie requires a non-terminal work order, a `work_order`-shaped component ISSUE requires the backflush, the backflush only runs at COMPLETE, and COMPLETE → non-terminal is blocked — so no client can produce the state. It costs one existence query, fails safe, and stays correct if that reachability argument ever stops holding; a refusal whose *stated reason* was false is what PR 4.4 fixed |
> | **422** | **Cross-part / cross-UOM lot pin** — `pinned_inventory_item_id` names a lot of a *different* part. The detail names the unit-of-measure clash when the two parts also disagree on units (*"Unit-of-measure mismatch: … No unit conversion exists"*); otherwise it reads *"The pinned lot belongs to a different part"*. There is **no unit conversion anywhere in the platform** — cross-UOM is refused, never guessed |
> | **422** | **Held or inactive lot pin** — `pinned_inventory_item_id` names a lot whose `status` is not `available` (`on_hold` / `quarantine` / `rejected`) or that is inactive (*"Lot L is 'quarantine' and may not be tied to work…"*). FIFO already skips such lots; the pinned branch does not, so pinning one would consume nonconforming material into product (AS9100D 8.7). Refused at **tie** time because consumption also runs from a `GET`, where refusing is not an option — a lot held *after* it was pinned still consumes, and writes a `HELD_MATERIAL_CONSUMED` audit row instead. **Nothing in the application ever writes a held `InventoryItem.status`** (no endpoint or schema exposes the column; it is only ever set to `available` at creation, and there is no lot-deactivation verb), so both halves of this control can currently only fire on data set outside the app — a direct DB write, an import, or a future hold verb |
> | **422** | `qty_per_run` sent on a **work-order-scoped** tie, via `POST` **or** `PATCH` (`"qty_per_run applies to operation-scoped ties only."`) |
> | **422** | `PATCH` sent **both** `clear_pinned_inventory_item: true` and a `pinned_inventory_item_id`. The clear used to win silently, so a caller who wanted the new pin got an unpinned tie and a 200 |
> | **422** | `PATCH` lowering `qty_planned` **below** `qty_consumed` (*"qty_planned cannot be lowered to N: M sheets has already been consumed… Return the over-consumed material first (Return material on this tie), then lower the plan."*). Lowering it *to* the consumed quantity is allowed |
> | **422** | `PATCH` lowering `qty_per_run` so far that the **live target** (`qty_per_run × (quantity_complete + quantity_scrapped)`) falls below `qty_consumed` — the operation-scoped twin of the `qty_planned` rule, and until this shipped the cheapest way in the API to manufacture `consumed > target`. Refused rather than merely recorded, because `target` is exactly what bounds `correct_over_consumption`: lowering `qty_per_run` toward zero would open an **unbounded** return against a tie that stays `open`, which is the middle ground the two intents exist to close. The predicate is "never **worsen** the gap", not "never have a gap" — raising `qty_per_run` on an already-over-consumed tie is allowed, since it reduces the problem. Skipped entirely when the operation is no longer on the work order (its live target is already 0) |
> | **422** | Body validation — `qty_planned` / `qty_per_run` must be `> 0` |
>
> **`POST …/{allocation_id}/return` error contract.** Eleven distinct refusals. The service carries
> the status with the refusal so the split cannot drift: **422** means "ask differently" (a bound the
> caller can satisfy by naming the other intent or a smaller quantity), **409** means "the ledger
> cannot express this" — receiving's *409 rather than guess* posture. **Every refusal fires before
> the first ledger row is written**, so a refused return leaves the ledger untouched rather than
> half-credited.
>
> | Code | When |
> |------|------|
> | **404** | Work order unknown, cross-tenant, or **soft-deleted** (`"Work order not found"`). Restore the work order first — the restore is itself audited and re-opens the ties the delete cancelled |
> | **404** | `allocation_id` unknown, cross-tenant, or not on this work order (`"Material allocation not found"`; the service re-checks and answers *"Material tie not found on this work order."*) |
> | **422** | **Blank reason** — FastAPI's own validation from `MaterialReturnRequest` (`min_length=1` plus a strip-and-check validator), the same boundary `ReceiptCorrection.reason` uses. The service re-asserts it, since it is callable without the schema and an unreasoned compensating movement is what the audit chain must never contain |
> | **422** | **Non-positive `quantity`** (`gt=0` on the schema; `"Return quantity must be greater than zero."` from the service) |
> | **422** | **Nothing consumed** (*"Nothing has been consumed against this material tie, so there is nothing to return. Untie it instead if the material is no longer needed."*) |
> | **422** | **`quantity` exceeds `qty_consumed`** (*"Cannot return N: only M has been consumed against this tie."*) |
> | **422** | **`correct_over_consumption` past the live bound** (*"…the work still accounts for T and only A is over-consumed. Returning more would be re-consumed automatically the next time this work order is completed or read. Use return_and_untie to give all the material back and close the tie."*). The detail names the intent to use instead |
> | **422** | **`return_and_untie` that is not the full consumed quantity** (*"return_and_untie returns everything consumed against this tie, which is currently C, not N. Re-read the tie and confirm that quantity."*) |
> | **422** | **Unsupported `intent`** — unreachable while the enum has two members, and deliberately exhaustive rather than a permissive fall-through: a third intent added without a bound of its own would otherwise post an **unbounded** return against a live tie |
> | **409** | **The ledger has less returnable than asked** (*"…the ledger shows only R still returnable against this tie. The inventory ledger is authoritative and the tie's consumed quantity is only a cache; make a manual inventory adjustment if stock genuinely needs to move."*). This is the cache/ledger disagreement case — trusting `qty_consumed` here would credit stock no ISSUE row ever took |
> | **409** | **A source lot is gone** — the stock row the consumption came off no longer exists (nothing in `app/` deletes stock rows, so this means an out-of-band write). Crediting any other lot would misstate lot traceability |
> | **409** | **A source lot is a placeholder row** — the lot-less, finished-goods-located anchor the engine mints when a part has no stock at all. Crediting it would create unlabeled, FIFO-eligible stock out of a row that exists purely as a ledger anchor (AS9100D 8.5.2) |
>
> `GET …/{allocation_id}/consumption` refuses only on the two **404**s above (work order, allocation).
>
> **Concurrency.** The return takes `SELECT … FOR UPDATE` on the **operation, then the work order**
> (the completion paths' order) *before* computing the bound — a return writes neither row, so
> invariant 4's optimistic lock does not cover it, and a completion landing mid-request would
> otherwise raise `target` underneath the check and silently invalidate the
> `correct_over_consumption` guarantee.
>
> Every create, edit, untie, and **return** writes a tamper-evident `audit_log` row (`GET /audit/`)
> on resource type `work_order_material_allocation`. A return writes the `qty_consumed` change as an
> `UPDATE`, plus the dual `inventory` rows per credited lot (the `RETURN` ledger row and the on-hand
> move it caused); a `return_and_untie` writes a **second** row for the cancel, stamped
> `extra_data.reason: "material_returned"` — deliberately **not** the work-order-delete cancel
> reason, so a delete/restore round trip cannot resurrect a tie whose material was given back. The
> reason text lands in three places on purpose: the ledger row's `notes`, the audit `description`,
> and `extra_data.reason`.

> **Ties on work-order delete (`DELETE /work-orders/{id}`).** The two delete modes differ, and the
> split follows the rule that posted consumption is a fact:
> - **Soft delete** (the default) is **never refused** because of a tie. Every **open** tie is
>   auto-**cancelled** with an audit row (`reason: "work_order_deleted"`), closing out forward-looking
>   demand; **consumption already posted stands** — the material was physically used and the ledger is
>   the compliance record. `POST /work-orders/{id}/restore` is the inverse: it re-**opens** exactly the
>   ties that delete cancelled (audited, `RESTORE`), so a restored work order keeps depleting its tied
>   material. Ties cancelled for any *other* reason — a manual untie, a nest re-import supersede — are
>   deliberately left `cancelled`; the discriminator is the cancel's own audit `reason`. A
>   `return_and_untie` cancel is stamped `reason: "material_returned"` for exactly this reason — a
>   delete/restore round trip must not resurrect a tie whose material was given back.
> - **Hard delete** (`hard_delete=true`, draft/cancelled WOs only) returns **409** when any
>   `inventory_transactions` row actually references a tie on the work order (*"Material movement is
>   on the inventory ledger for N tied allocation(s) on this work order, so it cannot be permanently
>   deleted — returning the material does not remove that history. Soft delete instead; the work order
>   and its material record stay intact."*), because a hard delete physically removes the operations
>   and ties the ledger's
>   consumption rows point at. The guard queries the **ledger**, not the `qty_consumed` cache: the
>   cache is documented as non-authoritative and the `allocation_id` FK carries no `ON DELETE`, so
>   keying on it would turn any drift into a 500 instead of this 409. **A material return does not
>   clear this refusal** — a `RETURN` row carries the compensated ISSUE's `allocation_id`, so a fully
>   returned tie is still ledger-backed; the message used to say "Reverse consumption first", which
>   would now name a verb that exists and still would not help. Unconsumed ties are removed with
>   the work order, each audited first (`reason: "work_order_hard_deleted"`).

##### Material allocation schema (`MaterialAllocationResponse`)

```json
{
  "id": 12,
  "work_order_id": 480,
  "work_order_operation_id": 3311,
  "operation_number": "10",
  "part_id": 77,
  "part_number": "SHT-CR-0250",
  "part_name": "Cold Rolled Sheet 0.250",
  "source": "nest",
  "status": "open",
  "qty_per_run": 1.0,
  "qty_planned": 5.0,
  "unit_of_measure": "sheets",
  "qty_consumed": 3.0,
  "pinned_inventory_item_id": null,
  "pinned_lot_number": null,
  "notes": null,
  "created_by": 4,
  "created_at": "2026-07-25T14:03:11Z",
  "updated_at": "2026-07-25T15:20:02Z"
}
```

> `status` is one of `open` / `closed` / `cancelled` and is **the tombstone** — there is no
> `is_deleted` on this resource. `source` is `nest` / `bom` / `manual`. `qty_consumed` is a
> **denormalized cache**: the authoritative total is always the sum of the ledger rows carrying this
> allocation's id (`inventory_transactions.allocation_id`) — reconcile from the ledger, not from this
> field, in a compliance answer. `unit_of_measure` is the snapshot taken at tie time, so it stays
> readable after the part's UoM is changed. Nothing in this release writes `closed`; ties end their
> life either `open` or `cancelled`.

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
  "version": 3,
  "due_date": "2024-01-31",
  "created_at": "2024-01-01T10:00:00Z",
  "updated_at": "2024-01-01T10:00:00Z"
}
```

> `version` is the work order's optimistic-lock counter — echo it back in the `PUT /work-orders/{id}`
> body (see "Optimistic locking on the work-order header" above).

### Laser Nests

Laser nests are the per-sheet laser-cutting tasks on a **laser-cutting work order** — either the
**child laser WO** of an assembly WO (the classic flow) or a **standalone nest WO** created
straight from a package with no parent and no part (see "Standalone nest work orders" below).
Each nest is backed by a clock-in-able `LASER` operation. There are two ways to
create nests, and per the product decision they are used **one or the other per job — never
mixed**:

1. **Package import** — upload a zipped Ermaksan/CNC package, a **bare nest-report PDF** (single-
   or multi-page), or point at a server folder. Three upload shapes, auto-detected: CNC **program
   files** (fields inferred from filenames, as before), a ZIP of nest-report **PDFs** (fields
   auto-extracted by AI), or a bare PDF whose pages are first AI-segmented into per-nest page
   ranges — see "PDF auto-extraction" below. (Mounted under
   `/work-orders/{id}/laser-nest-packages/…`; the no-WO standalone pair under
   `/work-orders/laser-nest-packages/standalone/…`.)
2. **Manual entry** — key one nest at a time, with an optional reference PDF. Dropping a nest-report
   PDF into the create modal auto-fills the fields via `POST /laser-nests/extract` (see below).
   (The `…/manual` create lives under work orders; per-nest edit/delete/PDF routes live under
   `/laser-nests/{id}/…`.)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/work-orders/{id}/laser-nest-packages/preview` | Preview nests detected from a zipped package, a bare nest-report PDF (single- or multi-page), or a server folder (writes nothing). PDF uploads run AI extraction per sheet | Admin / Manager / Supervisor |
| POST | `/work-orders/{id}/laser-nest-packages/import` | Import a package — creates/rebuilds the child laser WO (or rebuilds the addressed WO directly when it is itself `laser_cutting`), one nest operation per CNC file (or per confirmed PDF row) | Admin / Manager / Supervisor |
| POST | `/work-orders/laser-nest-packages/standalone/preview` | Same preview with **no work order in the path** — used before a standalone import | Admin / Manager / Supervisor |
| POST | `/work-orders/laser-nest-packages/standalone/import` | Import a package into a **fresh standalone laser WO** — creates a released, part-less `laser_cutting` work order (no parent; quantity = total planned sheet runs; optional `due_date`) | Admin / Manager / Supervisor |
| POST | `/laser-nests/extract` | Auto-extract nest fields (CNC #, material, size) from a single uploaded nest PDF. Stateless — no DB write, no audit | Admin / Manager / Supervisor |
| POST | `/work-orders/{id}/laser-nests/manual` | Manually add **one** nest to a work order (child laser WO of an assembly, or the addressed `laser_cutting` WO directly). Creates a clock-in-able `LASER` operation | Admin / Manager / Supervisor |
| PATCH | `/laser-nests/{id}` | Edit a manual nest (all fields optional) | Admin / Manager / Supervisor |
| POST | `/laser-nests/{id}/attach-document` | Attach an already-uploaded PDF Document to the nest (PDF-only) | Admin / Manager / Supervisor |
| DELETE | `/laser-nests/{id}/document` | Detach the PDF (clears the FK; the Document row is left intact) | Admin / Manager / Supervisor |
| GET | `/laser-nests/{id}/document` | Serve the attached PDF **inline** for operator preview | Yes (any authenticated user) |
| DELETE | `/laser-nests/{id}` | Soft-delete the nest; its operation goes `ON_HOLD` | Admin / Manager / Supervisor |

> **Package import replaces everything (`POST …/laser-nest-packages/import`).** Importing a
> package **replaces all existing nests on the target laser WO — including any manually-entered
> ones** — rebuilding the nest operations from the package plan. This is by design (manual *or*
> import per job, never mixed); an import is authoritative and supersedes prior manual entry.
> The wipe is now **fully audited**: each superseded nest is written as a `log_delete`
> (`reason="superseded_by_reimport"`) **before** the rebuild, and each rebuilt nest as a
> `log_create` — for **both** import shapes (the legacy CNC-program path now also writes the per-nest
> `log_create` with `source="cnc_file_import"`; the PDF path uses `source="pdf_import"`). A re-import
> onto an **existing** laser WO additionally writes a WO-level `log_update` (reason
> `laser_nest_package_import`) recording the import's effect on the work order itself — status forced
> to **RELEASED**, `quantity_complete`/`quantity_scrapped` zeroed, `quantity_ordered` re-derived to
> the package's total planned runs. The audit rows commit atomically with the rebuild.
>
> **Re-import is refused (409) when a wiped operation already consumed tied material.** Because the
> rebuild destroys the laser operations, any **material allocation** scoped to one of them would be
> left pointing at nothing — and the `ISSUE` ledger rows that carry its lot genealogy reference the
> operation id. So the import resolves the operations it is about to wipe **before deleting
> anything** and checks their ties:
> - Any tie the **ledger** references → **409 Conflict** (*"Cannot rebuild this work order's
>   operations: this work order's material movement is already on the inventory ledger for N tied
>   allocation(s), and the rebuild would delete the operations those ledger rows are recorded against
>   — dropping them out of job cost, analytics and lot traceability. Returning the material does not
>   change that. Raise a new work order for the corrected nest package; this one keeps its material
>   history intact."*). **Nothing is destroyed** — the guard runs ahead of the wipe, and uploaded blobs
>   are reaped. **This is reachable as soon as ONE nest's operation completes**, not only after the
>   whole work order finishes: consumption moved to operation completion, so the first completed nest
>   on a three-nest package locks that package.
>
>   ⚠️ **The material RETURN verb is deliberately NOT the remedy here, and the guard is keyed to the
>   ledger rather than to `qty_consumed` precisely so it cannot be mistaken for one.** A full
>   `return_and_untie` drives the cache to 0, but the original `ISSUE` row **and** the new `RETURN`
>   row both still carry `reference_type='work_order_operation'` with `reference_id` = an operation
>   the rebuild is about to delete — and `work_order_ledger_filter` resolves operation ids through a
>   **live subquery**, so those rows would silently drop out of job cost, analytics and lot genealogy
>   while remaining in the ledger. (The FK also has no `ON DELETE`, so on Postgres the delete raises
>   `IntegrityError` that this endpoint reports as the misleading generic **400** below.) Returning
>   the material retires the tie's forward-looking demand; it cannot un-write the movement history
>   that names those operations. **The remedy is a new work order.**
> - Ties with no ledger rows are **cancelled** (status → `cancelled`, never deleted) with an audit row
>   (`reason: "superseded_by_reimport"`), the same posture as the superseded nests themselves, **and
>   detached** — their `work_order_operation_id` is set NULL. That FK carries no `ON DELETE`, so a
>   still-attached tie makes the operation delete raise `IntegrityError`, which this endpoint reports
>   as the misleading generic **400** below; the tie's original scope is preserved in the audit row
>   (`old_values.work_order_operation_id` / `extra_data.work_order_operation_id`).
>
> A laser WO with no material ties — every WO today, since ties ship dark — is unaffected: the guard
> is one tenant-scoped SELECT that returns nothing.
>
> **Standalone nest work orders (no parent, no part).** The `…/standalone` pair runs the same wizard
> flow with no work order in the path. `preview` is behaviorally identical to the `{id}` preview;
> `import` takes the same form fields (`file`/`source_path`, optional `work_center_id`, optional
> `rows`) plus an optional **`due_date`** form field (ISO date, standalone import only) — the
> planner-set due date stamped on the created WO; **past dates are allowed** (an open WO can
> already be overdue at import), and the date is editable later via `PUT /work-orders/{id}` (the
> WO-detail inline due-date edit). It creates a **fresh RELEASED `work_order_type='laser_cutting'` work order** with
> **`part_id` NULL** and **no parent**, `quantity_ordered` = **total planned sheet runs**, then
> builds the package onto it (`laser_nest_packages.parent_work_order_id` NULL,
> `child_work_order_id` → the new WO). Nest-PDF `DRAWING` Documents attach to the created WO itself
> (in the parented flow they attach to the parent assembly WO). The response exposes the created WO
> under the same `child_work_order` key, and the creation is audited (`log_create`,
> `source="laser_nest_standalone_import"`). Schema notes: `work_orders.part_id` is nullable **only**
> for `work_order_type='laser_cutting'` (DB CHECK `ck_work_orders_part_required_unless_laser`,
> migration `067`); `POST /work-orders/` (`WorkOrderCreate`) still **requires** `part_id` — part-less
> WOs are born only via this import. `WorkOrderCreate` also gates **`work_order_type`** now: the value
> must be a member of the `WorkOrderType` enum (**422** otherwise — the column is a free string and
> used to be persisted verbatim), and **`'laser_cutting'` is refused on create** (422). The FG-receipt
> and BOM-backflush completion skips key on exactly that value (`is_laser_dispatch_work_order`), so a
> hand-created `laser_cutting` WO with a real part and routed operations would silently lose its
> finished-goods receipt and backflush at completion. Nest-dispatch WOs are minted only internally
> (`_ensure_laser_child_work_order` and the nest import paths construct the ORM model directly), so
> the refusal closes the API surface without touching the import flow. Existing laser WOs still
> serialize their type on reads (`WorkOrderResponse` is not gated). On the read side, `part_id` (and the list rows'
> `part_number`/`part_name`/`part_type`) are **nullable** in work-order responses, and list rows now
> also carry `work_order_type` and `parent_work_order_id`. In the app the wizard is the
> **Import Nest Package** button on the Work Orders page.
>
> **`{work_order_id}` nest endpoints generalized to laser WOs.** When the WO addressed by
> `…/{id}/laser-nest-packages/preview|import` or `…/{id}/laser-nests/manual` is **itself**
> `laser_cutting` (a standalone nest WO, or a laser child addressed directly), the import/manual add
> operates **on that WO directly** — no child is nested under it. This is how re-import and manual
> nest-add work on standalone nest WOs from the WO-detail wizard; for every other WO type the classic
> find-or-create-child flow is unchanged.
>
> **A soft-deleted laser child is never rebuilt — `409`.** Find-or-create resolves only **live**
> children (`is_deleted = false`). When the addressed parent's only laser child is **soft-deleted**,
> `…/laser-nest-packages/import` and `…/laser-nests/manual` return **409** naming that work order and
> the remedy (`POST /work-orders/{id}/restore`) — they neither resurrect it (the rebuild force-sets
> `released`, which would put a deleted WO back on the floor with none of the restore path's
> controls, including the material-tie re-open) nor fork a **second** laser child alongside it. The
> directly-addressed route already answered **404** for a soft-deleted WO; this closes the
> parent-addressed one.
>
> **Laser WOs are dispatch pools — every nest READY, no sequence gating.** A laser-cutting WO's
> nest operations carry sequence numbers for stable labels/ordering only ("Nest N") — they have
> **no precedence semantics**. Every nest operation is created **READY** (package import and
> manual add alike), so the whole package is immediately visible and clock-in-able on its work
> center's kiosk queue, and `work_order_type='laser_cutting'` WOs are **exempt from the
> predecessor/sequence gate** on every path that enforces it — office
> `/work-orders/operations/{id}/start|complete`, shop-floor clock-in and
> `/shop-floor/operations/{id}/start|complete`, and the scanner resolver (which mirrors the live
> gates) — so operators run nests in **any order**, including across **different work centers**
> when a package's nests are spread over multiple lasers. The exemption keys off the WO type
> through one shared predicate (`is_laser_dispatch_work_order` in `work_order_state_service`), and
> the release helpers promote **all** PENDING laser ops to READY (not just the lowest sequence) —
> a pre-existing laser WO imported before whole-package-ready self-heals (its stranded PENDING
> nests go READY) on its next release/lifecycle event. Non-laser WOs keep the classic
> one-at-a-time READY promotion and predecessor gates unchanged.
>
> **Pool WO header progress is the SUM of per-nest progress.** On a `laser_cutting` WO the header
> `quantity_complete` (sheets complete) is derived as the **sum over its nest operations of
> `min(operation quantity_complete, that nest's planned runs)`, capped at `quantity_ordered`** —
> not the sequential single-operation rollup routed WOs use (where every operation processes the
> whole order). Every completion/production path and the read-time reconcile compute this pooled
> sum, so the header advances as **each** nest's sheets are cut rather than freezing at the largest
> single nest's count until every nest completes. Two consequences:
> - **No snap at completion.** When all nests reach COMPLETE the header is **not** snapped to
>   `quantity_ordered` — a nest completed short keeps the honest as-cut total (routed WOs keep
>   their existing complete-at-target behavior).
> - **Raise-only self-heal on read.** A pool WO whose header is stale-low is healed up to the
>   pooled sum by reconcile-on-read on its next work-order detail/list read — best-effort, never
>   lowers, no production post required.
>
> The header stays monotonic-up except through the sanctioned reduce-production verbs, where
> lowering one nest's evidence lowers the pool header by the same delta (see "Over-count
> correction" under Work Orders and Shop Floor).
>
> **Work-center selection (package-level pick, auto-detect order, per-row override).** Each nest's
> backing operation lands on a work center resolved in this order:
> 1. The row's **`work_center_id`** (PDF confirm-and-commit `rows` only — see the `rows`
>    validation below). Per-row overrides let one package be spread across multiple lasers.
> 2. The import's package-level **`work_center_id`** form field.
> 3. **Auto-detect** when neither is sent: among active work centers whose name/code/type matches
>    `%laser%`, the pick prefers entries mentioning **"ermaksan"** or **"fiber"** (the Ermaksan
>    fiber laser), then any other laser, with entries mentioning **"tube"** ranked **last** — the
>    HSG tube laser is never the silent default; lowest id breaks ties within a tier. No active
>    laser work center → **400**.
>
> An explicit `work_center_id` (package-level or per-row) always wins over auto-detect and must be
> an **active** work center in the caller's company (it need not match `%laser%`) — otherwise
> **404** (`"Laser work center not found"`); every distinct per-row override is validated
> **before** the atomic build, so a bad override persists nothing. Each operation's
> `operation_group` derives from **its own** work center. The legacy CNC-program path (no `rows`)
> is package-level only. In the wizard this is the dispatch strip's work-center select (defaulting
> to the Ermaksan fiber laser, with "(auto-detect)" available) plus the per-row work-center
> column; the same auto-detect resolves the manual-create path's laser work center.
>
> **PDF auto-extraction (CNC #, material, material size).** Nest-report PDFs (SigmaNEST / Ermaksan
> style) are read automatically; the planner verifies before saving. Extraction is **layout-aware
> (vision)**: the PDF bytes are sent to Claude as a base64 `document` content block so the model
> reads the rendered sheet with its 2-D layout (PDFs over a ~20 MB native cap, or whose bytes can't
> be read, fall back to flattened-text extraction). Extraction is **two-pass** everywhere PDFs are
> extracted (both entry points below): the extraction pass (prompt `laser_nest_extraction` 1.2.0,
> `feature="laser_nest_extraction"`) plus an **independent verification pass** (prompt
> `laser_nest_verification` 1.1.0, `feature="laser_nest_verification"`, same routing task) that
> re-reads the same sheet; the two reads are merged per field — agreement → **high** confidence, a
> one-sided null → the non-null value at **medium**, a conflict → the **verifier's** value at
> **low**, both null → null at **low** — and the overall `confidence` is the minimum across fields.
> A pass-2 failure of any kind keeps the pass-1 result untouched with a "verification skipped"
> `warning`; the response field `passes` (1 | 2) records how many AI reads produced the result.
> Both entry points are gated to **Admin / Manager / Supervisor** and both **AI-always** via the
> shared `run_llm_task` pipeline (one tenant-scoped `ai_usage_events` row per pass — telemetry,
> not audit):
>
> - **Single-PDF (`POST /laser-nests/extract`).** Multipart `file` (PDF; non-PDF → **400**).
>   **Stateless — no DB write, no audit**; `company_id` flows through only for usage telemetry.
>   Used by the manual-create modal to auto-fill fields from a dropped PDF. Returns
>   `{ cnc_number, material, thickness, sheet_size, planned_runs, confidence, source, warning, passes }`
>   where `source` ∈ `{ai, filename}`, `confidence` ∈ `{high, medium, low}` (overall — the
>   per-field minimum when both passes ran), and `passes` ∈ `{1, 2}`.
>   Declared as a static `/extract` route so it matches ahead of the dynamic `/{laser_nest_id}` routes.
>
> - **Batch ZIP (`…/laser-nest-packages/preview` → `…/import`).** A package is treated as a **PDF
>   package** iff it contains any `*.pdf` (PDFs and CNC extensions are disjoint); otherwise the
>   legacy CNC-program path runs unchanged. **Review-before-commit:** `preview` runs AI once per
>   sheet (parallelized, bounded concurrency) and returns editable rows — beyond the existing
>   `nest_name` / `cnc_file_name` / `planned_runs` / `material` / `thickness` / `sheet_size`, PDF
>   rows also carry **`source_file`** (the PDF's path within the package), **`cnc_number`**,
>   **`confidence`**, per-field **`field_confidence`** (`{field: high|medium|low}` from the
>   two-pass merge), **`warning`**, and **`passes`**. The planner edits/confirms in the wizard,
>   then `import` re-sends the same ZIP
>   **plus an optional `rows` form field** — a JSON array of confirmed rows
>   `{source_file, cnc_number, nest_name, planned_runs, material, thickness, sheet_size}` (plus
>   `source_pages` on bare-PDF rows — see below — an optional per-row `work_center_id`
>   override — see "Work-center selection" above — and an optional per-row **material tie**,
>   `material_part_id` + `qty_per_run`, see below). When
>   `rows` is present, the backend matches each row to its PDF by `source_file`, stores each PDF as
>   a `DRAWING` `Document` (attached via `document_id`), sets `cnc_number`, writes one `log_create`
>   audit row per nest, and builds the target laser WO — **no second AI call** (the re-sent ZIP only
>   supplies PDF bytes). When `rows` is absent, the legacy CNC-file import is unchanged.
>
>   `rows` is **strictly validated** before anything is persisted (`LaserNestImportRow`):
>   `source_file` required (1–1000 chars), `planned_runs` required and **≥ 1**,
>   `source_pages` optional (required on the bare-PDF path — see below): non-empty, entries
>   **≥ 1**, ascending and consecutive,
>   `work_center_id` optional and **> 0** (per-nest override; must resolve to an **active**,
>   company-scoped work center, else **404** before anything is persisted),
>   `material_part_id` optional and **> 0** with `qty_per_run` optional and **> 0** (the per-nest
>   material tie — see "Nest material ties" below), and
>   `cnc_number` / `nest_name` / `material` / `thickness` / `sheet_size` length-bounded as on the
>   manual path. Import-specific **400** cases: `rows` not valid JSON / not a JSON array; any row
>   failing validation; a **duplicate `source_file`** across rows; and a DB constraint/length fault
>   (`IntegrityError`/`SQLAlchemyError` — e.g. tripping `uq_laser_nests_package_file` — now returns a
>   clean **400** rather than a 500). A `source_file` that escapes the package or is missing from the
>   re-sent ZIP → **400**.
>
> - **Bare nest-report PDF (single- or multi-page).** An upload with `Content-Type:
>   application/pdf` or a `.pdf` filename is treated as a **bare PDF** rather than a ZIP, on all
>   four preview/import endpoints. Preview reads the page count locally (`pypdf`; unreadable bytes
>   → **400** "Could not read the PDF"; over the page cap → **400**, see below), then an **AI
>   segmentation pass** ("pass 0" — prompt `laser_nest_segmentation` 1.0.0,
>   `feature="laser_nest_segmentation"`; the whole PDF as a base64 `document` block,
>   Default/Sonnet tier via `has_pdf_document`) decides which pages form which nest and which to
>   skip as non-nest cover/summary pages. Single-page PDFs skip the call entirely; **any**
>   segmentation failure (egress off, unconfigured, > 20 MB, bad JSON, failed validation) degrades
>   to **one nest per page** at low confidence with a `segmentation_warning` — segmentation can
>   never fail an upload. The PDF is then split **locally** into per-segment PDFs with
>   deterministic names (`nest-p{first:03d}.pdf` for one page,
>   `nest-p{first:03d}-p{last:03d}.pdf` for a range) and each segment runs the same per-nest
>   two-pass extraction as a ZIP of PDFs. The preview response adds **`source_page_count`**,
>   **`segmentation_warning`**, and **`skipped_pages`**; each row's `source_file` is its derived
>   split name and each row carries **`source_pages`** (the segment's 1-based page list in the
>   original upload).
>
>   **Import (confirm-and-commit):** the client re-sends the same PDF plus the confirmed `rows`.
>   Every row **must** carry `source_pages`, and its `source_file` must exactly equal the name
>   derived from those pages — a mismatch is a **400** ("The preview is stale; re-run it and
>   confirm again"). A bare PDF **without** `rows` → **400** ("Preview the PDF first, then confirm
>   the rows") — the legacy no-`rows` import is CNC-programs-only. The server re-splits the
>   re-sent PDF by the confirmed page lists (local `pypdf`, **zero AI calls on import**), attaches
>   each nest's per-segment PDF as its `DRAWING` Document, and otherwise runs the identical
>   confirmed-rows machinery as the ZIP path (audit `source` stays `"pdf_import"`). Out-of-range,
>   duplicate, or page-**overlapping** segments (one page claimed by two rows) → **400**. ZIP and
>   CNC-program imports are unchanged.
>
> - **50-PDF cap (`LASER_PDF_PACKAGE_MAX`).** A package (or `rows` array) with more than **50**
>   PDFs — or a bare PDF with more than **50 pages** — is rejected with **400**.
> - **Upload size + abuse guards.** The upload body (ZIP or bare PDF) is capped at **50 MB**
>   (`LASER_UPLOAD_MAX_BYTES`), enforced while the body streams to disk → **413**. The local page
>   split refuses pathological amplification (pages sharing huge embedded resources that would
>   multiply on split) → **400**. The per-request AI fan-out shares one **process-global**
>   concurrency cap (5), and the two standalone routes carry a **10/minute** per-path rate limit.
> - **Graceful degrade.** A PDF the model can't read falls back to the **filename stem** as the
>   `cnc_number` (`05749.pdf` → `05749`) with a `warning` and `source="filename"` at low confidence —
>   one bad sheet never hard-fails a batch. **Bare-PDF segments are the exception:** their split
>   names (`nest-p001.pdf`) are synthetic, so a degraded or unpinnable segment leaves `cnc_number`
>   **null** (`source="none"`) for the planner to fill — the segmentation pass's `cnc_number_hint`
>   is offered to the model instead of the filename. The native-PDF (vision) path reads
>   scanned/image-only sheets directly; only when it can't (>20 MB cap or unreadable bytes) does the
>   flattened-text fallback run (with its OCR step in `pdf_service`). With `allow_ai_egress` **off**,
>   extraction degrades the same way (pre-filled from the filename for ZIP packages, blank
>   `cnc_number` for bare-PDF segments; the planner fills the rest manually) and bare-PDF
>   segmentation defaults to one nest per page — the page split itself is local `pypdf` and keeps
>   working.
> - **`planned_runs` reads differently on the two entry points, and the difference is load-bearing.**
>   The stateless single-PDF `POST /laser-nests/extract` passes the model's value straight through, so
>   it returns **`null`** when no run count was found. **Preview rows do not**: `LaserNestPreviewRow`
>   types `planned_runs` as a non-optional `int` and `_coerce_planned_runs` **floors it at 1**, so a
>   nest that genuinely runs once and a nest whose count neither pass could find are the **same `1`**
>   in the response. `field_confidence["planned_runs"] == "low"` is the **only** signal that separates
>   them — read the confidence, never the number, and the import wizard accordingly counts and labels
>   those rows rather than presenting them as read values. Accepted coercion shapes (widened
>   2026-08-05, since a model asked for one integer emits all of these): `3`, `3.0`, `"3"`, `"x3"`,
>   `"3 sheets"`, `"3 of 5"` — a **leading** integer is required for the free-text case, so `"sheet 4"`
>   reads as 1 rather than mistaking a label for a count. Anything else (a fractional float, junk,
>   missing) falls back to 1 rather than raising and 400-ing the whole preview batch.
>
> **Manual nest create (`POST /work-orders/{id}/laser-nests/manual`).** Body: `cnc_number`
> (required, 1–100 chars), `planned_runs` (required, **≥ 1**), and optional `nest_name`,
> `material`, `thickness`, `sheet_size`, plus the same optional **material tie** the import rows take
> (`material_part_id` **> 0**, `qty_per_run` **> 0** — see "Nest material ties" below). Resolves the target laser WO (find-or-create the child on
> an assembly WO; the addressed WO itself when it is `laser_cutting`) and an active
> laser work center — **400** if no active laser work center exists (auto-detected with the same
> Ermaksan-fiber-first, tube-last preference — see "Work-center selection" above). Every manual
> nest is created **READY** (clock-in-able now) regardless of how many nests already exist —
> laser WOs are dispatch pools (see "Laser WOs are dispatch pools" above). This is a standalone
> creation path that **does not change** the package-import behavior. Returns **201** with the new
> nest plus its backing operation (`work_order_operation_id`, `operation_status`). Besides the
> per-nest `log_create`, the add writes a WO-level `log_update` (reason `manual_laser_nest_added`)
> recording the forced-**RELEASED** status and the re-derived `quantity_ordered` on the laser WO.
>
> **Nest material ties (`material_part_id` + `qty_per_run`).** Both nest-creation paths — the PDF
> confirm-and-commit import row and the manual create body — take an optional pair naming the stock
> part (sheet/plate) the nest consumes. When `material_part_id` is set, the server creates an
> **operation-scoped** `WorkOrderMaterialAllocation` on that nest's freshly-created operation
> (`source: "nest"`, unpinned, `qty_per_run` defaulting to **1.0**, `qty_planned = qty_per_run ×
> planned_runs`, `unit_of_measure` snapshotted from the part), so the material is deducted **when
> the nest's operation completes** (each nest is one operation, so nest 1 of 3 deducts nest 1's
> sheets right then; the work-order completion reconcile is the self-heal — this sentence and the
> schema descriptions used to say "when the laser work order finishes", stale since PR 2.5). Omit it
> and the nest is untied and byte-identical to its pre-feature behavior: no allocation row, no audit
> row.
>
> The tie is created **inside the import transaction**, not by a follow-up `POST` to
> `…/material-allocations`: the import is atomic and a second call would not be, so a failed follow-up
> would leave nests a planner believes are tied that never deplete — with no compensating verb to fix
> it. Both paths run the same `create_nest_material_allocation` seam, so an imported tie and a
> hand-created one produce identical rows and identical `work_order_material_allocation` `log_create`
> hash-chain entries. Every distinct `material_part_id` is resolved **before** the rebuild wipes the
> prior nests, so a bad or cross-tenant id is a clean **404** ("Material part not found", never 403)
> with nothing persisted; soft-deleted parts are refused the same way. `qty_per_run` **without**
> `material_part_id` is a **422** on the manual path / **400** on the import path rather than a
> silently dropped field. There is deliberately **no fuzzy auto-match** from the AI-extracted
> `material` free text to a part — a wrong auto-tie would deplete the wrong heat lot into an as-built
> record — so the planner picks explicitly or the nest ships untied. See "Material ties" under Work
> Orders for the tie lifecycle and `docs/MATERIAL_CONSUMPTION_PLAN.md` for the design record.
>
> **Manual nest edit (`PATCH /laser-nests/{id}`).** All-optional body (`cnc_number`, `nest_name`,
> `planned_runs`, `material`, `thickness`, `sheet_size`). A `planned_runs` change **reverse-syncs**
> the operation's `component_quantity` and re-derives the laser WO's `quantity_ordered` over
> its non-deleted nests. Lowering `planned_runs` below `completed_runs` is allowed (over-run is
> acceptable); only the schema's `ge=1` floor applies. `material` / `thickness` / `sheet_size` are
> **canonicalized on write** — see below.

> **Sheet descriptors are canonicalized on every write (`services/laser_nest_text.py`).**
> `material` / `thickness` / `sheet_size` carry **no `Part` foreign key** — sheet recognition on
> this path is a deliberate heuristic — so the *string* is the only grouping key anything has, and
> the values arrive from an LLM extraction pass that spells the same sheet more than one way. A
> 2026-08-06 production reconciliation found one physical sheet split across two rows on whitespace
> alone (`144x60` vs `144 x 60`): 25 output rows for 19 real specs, so every group under-reported
> and the under-report looked like a smaller number rather than like an error.
>
> Every seam that writes a nest normalizes — the extraction mapper, the filename-inference
> fallback, the package build, `POST …/laser-nests/manual`, `PATCH /laser-nests/{id}`, and the
> work-order **duplicate** copy (canonicalized on the way across rather than copied byte-for-byte,
> so duplicating a pre-normalization job cannot re-inject a legacy spelling into new data).
> Canonical forms: `"a36 "` → `A36`, `"16 GA"` → `16ga`, `".25"` → `0.25`, `"144x60"` /
> `"144X60"` / `"120×48"` → `144 x 60` / `120 x 48`.
>
> **The rule is normalize spelling, never meaning**, and three things are deliberately NOT done:
> trailing zeros are **preserved** (`0.250` stays `0.250` — the digits state precision on a
> manufacturing thickness, so `0.25` and `0.250` do still group apart, which is the accepted cost of
> not rewriting a spec); units are **not inferred** (a bare `16` is not promoted to `16ga`); and
> alloys are **not expanded** (`SS` is not rewritten to `304 SS` — it could be 304 or 316 and the
> nest does not say). A descriptor the parser cannot read as two dimensions passes through with
> whitespace collapsed and nothing else touched, rather than being mangled into something tidy and
> wrong. The transform is idempotent, which the overlapping seams rely on.
>
> **This is forward-only.** Rows written before it are untouched; a backfill would rewrite historical
> nest records and is a separate, explicit decision.
>
> **Reference PDF (attach / detach / preview).** The attached PDF is a plain **shop-reference
> drawing** — optional, with **no approval workflow**, and it **never gates clock-in**. Attach
> references a Document already uploaded via `POST /documents/upload`; non-PDF documents are
> rejected with **400**. `GET /laser-nests/{id}/document` serves it `Content-Type: application/pdf`,
> `Content-Disposition: inline` so the operator can preview it; **404** if none is attached. Kiosk
> surfaces preview through the fence-safe `GET /shop-floor/documents/{document_id}/inline` twin
> instead (badge-minted kiosk tokens can't reach `/laser-nests` — see Shop Floor → "Kiosk doc
> viewer"); this route remains the desktop path. Detach only clears the FK — the Document row and
> its stored bytes survive.
>
> **Duplicating a work order copies its nests and SHARES their drawing.**
> `POST /work-orders/{id}/duplicate` (see Work Orders → "Duplicating a work order") copies every live
> nest — CNC number, material, thickness, sheet size, planned runs, work center — onto one new package
> on the new draft WO, carrying the `document_id` as a **reference**: no new `Document` row, no second
> document number, no blob copy. That reads correctly, because this route resolves the PDF by
> `nest.document_id` and filters the Document by **`company_id` only**, never by work order, so the
> operator preview works on the duplicate exactly as on the source. The consequence to know: the
> Document still belongs to the **source** work order, so deleting the source's drawing breaks the
> duplicate's preview. Copying the blob was rejected as the alternative — it doubles storage for a
> byte-identical PDF and mints a second document number for one drawing, which is worse for
> traceability than a shared reference. A nest that was **soft-deleted** on the source is not copied,
> and its operation is skipped and reported in the duplicate's response envelope (`reason:
> "laser_nest_deleted"`) rather than arriving as a nest task with no nest.
>
> **Soft delete (`DELETE /laser-nests/{id}`).** Soft-deletes the nest (`SoftDeleteMixin`; never a
> hard delete) and sets its operation to **`ON_HOLD`**, which removes it from the operator/work-center
> queue and the laser WO's quantity rollup. Soft-deleted nests are filtered out of `WorkOrderResponse`
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
| PUT | `/parts/{id}` | Update part (**409** when it turns `backflush_components` on and the part's readiness check reports blockers — see below) | Admin / Manager / Supervisor |
| DELETE | `/parts/{id}` | Delete part (soft delete — restorable) | Admin |
| POST | `/parts/{id}/restore` | Restore a soft-deleted part | Admin / Manager |
| GET | `/parts/{id}/bom` | Get BOM for part | Yes |
| GET | `/parts/{id}/backflush-readiness` | **PR 4.5** — may this part opt into automatic BOM component backflush, and what refuses it if not. Pure read (writes nothing) | Yes |

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
  "backflush_components": false,
  "is_active": true,
  "created_at": "2024-01-01T10:00:00Z"
}
```

> **`backflush_components` is in the payload again — and the history matters, because it was wrong in
> both directions before it was right.** The sample originally listed the field while no part endpoint
> returned it (the column existed on the `parts` **table** since migration `040`, but on no Pydantic
> schema); PR 4 removed it from the sample rather than re-annotating it, because a schema block is the
> one place a reader is entitled to take literally. **PR 4.5 put it on `PartResponse`**, so the sample is
> accurate for the first time. `GET /parts/{id}` and the **list** endpoints agree: the list helper
> hand-builds its kwargs, so the field is populated there explicitly rather than inherited.
>
> The flag (boolean, default `false`) opts a part into **component backflush on work-order completion** —
> its BOM/routing components auto-consumed via negative `ISSUE` transactions when a work order for it
> completes (see [Work Orders](#work-orders) → completion inventory effects).
>
> **Where it can be set — deliberately narrow.** The field is on **`PartResponse` and `PartUpdate` only**.
> It is **not** on `PartBase`, therefore not on `PartCreate`: both create endpoints and both CSV importers
> splat `Part(**data)`, so a field on `PartBase` would become settable on four write paths at once with
> no gate. A part is always **created off** and can only be switched on through an update. Sending an
> explicit `null` is **422** (the column is `NOT NULL`; omission, not `null`, is the "leave it alone"
> sentinel).
>
> **Turning it ON is gated (409).** Both update doors — `PUT /parts/{id}` and `PUT /materials/{id}`,
> which write the same `parts` rows through the same schema — run one **shared** refusal gate
> (`assert_backflush_change_allowed`, defined once in `parts.py` and imported by `materials.py`; a gate in
> only one of the two files would not be a gate). It runs **before the first field is written**, so a
> refusal leaves the row untouched. If the part's readiness check reports any **blocking** diagnostic the
> request is refused **409** with `detail` as a **plain string** — one sentence per blocker, joined —
> reading *"Part {part_number} cannot enable automatic backflush: {what is wrong}. {what to change}."*
> The **structured** form is on `GET /parts/{id}/backflush-readiness`, which the UI calls first.
> Turning it **OFF is always allowed** (stopping automatic consumption can never issue wrong material),
> and a request that re-states the flag's current value is not gated at all.
>
> **`GET /parts/{id}/backflush-readiness`** returns `{part_id, part_number, backflush_components,
> eligible, blockers[], advisories[]}`, each diagnostic carrying a stable `code`, a `severity`
> (`blocking` / `advisory`), an operator-facing `detail`, and optional `bom_item_id` /
> `component_part_id` / `component_part_number` / `operation_id` context. Any authenticated tenant user.
> **Pure read — writes nothing.** Blocking codes today: `deleted_part`, `no_demand_source`,
> `deleted_active_bom`, `phantom_without_bom`, `alternate_group_without_primary`, `zero_bom_quantity`,
> `negative_bom_quantity`, `unit_of_measure_mismatch`, `missing_component_part`, `circular_bom`,
> `bom_depth_exceeded`, `foreign_component_part`; plus, on the work-order preview only,
> `operation_names_own_part`, `operations_disagree_on_component`,
> `routing_component_excluded_by_bom`, `routing_bom_quantity_disagreement`. Advisories:
> **A BOM deleted through `DELETE /bom/{id}` reports as `no_demand_source`, not
> `deleted_active_bom`.** That verb clears `is_active` alongside the tombstone, and the shared BOM
> lookup requires `is_active`, so the deleted row is not resolved at all. `deleted_active_bom`
> covers the `is_deleted=true, is_active=true` shape a script or fixture can leave behind. Both are
> **blocking** and both refuse the whole leg — only the wording differs.
>
> `routing_only_no_bom`, `zero_quantity_ordered`, `incomplete_operation_demand`, `tie_basis_mismatch`,
> `tie_operation_missing`.
>
> Two of those carry a caveat worth stating at the contract:
> * **`no_demand_source` is blocking HERE and advisory on the preview.** At part opt-in it means
>   "arming this would consume nothing", which is a real refusal; at work-order scope it means
>   "this job has no BOM", which is the ordinary case for a turned part or a part-less nest package
>   and must not paint a red banner over a healthy job.
> * **`missing_component_part` and `foreign_component_part` never disclose a component outside this
>   company.** A BOM line whose `component_part_id` does not resolve to a part in the active company
>   yields `missing_component_part` carrying **only `bom_item_id`** — no `component_part_id`, no
>   `component_part_number`, and no name in `detail`. A same-tenant **soft-deleted** component does
>   name itself (`foreign_component_part`), because it is this company's own part and the sentence is
>   otherwise unactionable.
>
> **`eligible: true` is a snapshot, never authorization — and it covers the BOM half only.** Every input
> it reads — BOM lines and their `is_alternate` / `is_optional` / `item_type` / `quantity` /
> `unit_of_measure` — is mutable afterwards by other people, which is why the identical check re-runs
> server-side on the write. Readiness runs the explosion at a **synthetic basis of 1.0**, because the real
> basis is `quantity_complete + operation scrap` and would be zero at opt-in time — which would pronounce
> every part clean.
>
> **Three limits, because `eligible: true` (and the `backflush_readiness: "clean"` it writes onto the
> audit row) is routinely overread:**
> * **The routing half is never checked here at all.** `backflush_readiness_for_part` runs the BOM
>   explosion and nothing else; `operation_names_own_part`, `operations_disagree_on_component`,
>   `routing_component_excluded_by_bom` and `routing_bom_quantity_disagreement` need a work order and
>   appear only on `GET /work-orders/{id}/backflush-preview`. An eligible part can still resolve wrong
>   demand on a specific job.
> * **It is a one-time check.** It is evaluated at the flip and never again — not on a BOM edit, not on a
>   routing change, not at release, not at completion.
> * **The ROUTING edit path still does not know a part is armed**, and the BOM edit path only warns.
>   Anyone with `boms:edit` / `routings:edit` — the same ADMIN/MANAGER/SUPERVISOR tier — can change any
>   input afterwards. A **BOM**-line write now returns a `backflush_armed_warning` and stamps
>   `extra_data.backflush_armed_parts` on its `bom_line` audit row (see
>   [BOM line writes](#bom-line-writes)), so the edit is at least visible on the chain — but there is
>   still **no re-check, no refusal, and no notification**, and a routing change is still silent.
>
> What stands behind the flip is the **completion-time refusal** described under
> [Work Orders](#work-orders) → completion inventory effects: a blocking diagnostic drops the demand it
> describes and writes a `BACKFLUSH_DEMAND_REFUSED` audit row. That is a net, not a second gate.
>
> **Three residuals the owner accepted rather than designed around**, because exposure uses the ordinary
> part-edit field instead of a dedicated reasoned verb: the flip is **Supervisor-tier** (the same
> permission as editing a description); **no reason is captured** (the audit row records who, when,
> false→true, and the readiness verdict that authorized it in `extra_data` — not why); and **a concurrent
> flip does not 409**, because `Part` maps no `version` column, so `PartUpdate.version` is written onto an
> unmapped attribute and last write wins. See
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) and
> [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md) → Delivery, PR 4.5.
>
> **The flip is auditable from ONE query, through either door.** `PUT /materials/{id}` writes the same
> `parts` row and logs this change as `resource_type="part"` (not `"material"`) for exactly that reason,
> so *"who armed automatic stock movement, and when"* does not depend on which URL was used. The
> canonical query recipe — the predicate, how to narrow it to arming rather than disarming, and what it
> does and does not return — is written down once, in
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → the 2026-07-27 (PR 4.5) changelog row,
> item (3). `create_material` / `delete_material` still log `"material"`, so a material's *full* history
> is still a two-resource-type query.
>
> A **work-order material tie** remains the way to make one specific job consume material **without**
> this flag, and is the only one of the two with production mileage — see
> [Work Orders](#work-orders) → "Material ties".

### BOM (Bill of Materials)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/bom/` | List all BOMs (`skip` ≥ 0, `limit` default 100, **1–10000** — the one list endpoint above the standard 5000 ceiling; see [Pagination](#pagination)) | Yes |
| POST | `/bom/` | Create BOM | Yes |
| GET | `/bom/uom-mismatches` | **PR 4.5** — BOM lines whose stated unit of measure disagrees with the component part's. Pure read (writes nothing). **Has a UI:** `/bom/uom-mismatches`, sidebar **Engineering → BOM Unit Mismatches** — see [below](#where-this-is-worked--the-bom-unit-mismatches-screen) | Admin / Manager / Supervisor |
| GET | `/bom/{id}` | Get BOM by ID | Yes |
| GET | `/bom/{id}/explode` | Multi-level explosion (`max_levels` **1–20**, default 10) — see tenant-scoping note below | Yes |
| GET | `/bom/{id}/flatten` | Flattened multi-level view for reports/MRP (`max_levels` **1–20**, default 10) — same scoping | Yes |
| GET | `/bom/{id}/where-used` | Parent assemblies using this BOM's part — same scoping | Yes |
| PUT | `/bom/{id}` | Update BOM header. **`status` is not accepted**, and a released BOM accepts only `description` — see [Controlled-document state](#bom-controlled-document-state) | Admin / Manager / Supervisor |
| POST | `/bom/{id}/release` | Approve the BOM for production. Requires a draft with at least one line, else **400** | Admin / Manager |
| POST | `/bom/{id}/unrelease` | Withdraw the approval back to draft, clearing `approved_by` / `approved_at` / `effective_date` | Admin / Manager |
| DELETE | `/bom/{id}` | **Soft**-delete a draft BOM; its lines are retained. Returns `{"message": "BOM deleted", "can_restore": true}` | Admin / Manager |
| POST | `/bom/{id}/restore` | Restore a soft-deleted BOM (**400** if it is not deleted) | Admin / Manager |
| POST | `/bom/{id}/items` | Add a line to a BOM. ⚠️ **`unit_of_measure` changed behaviour on 2026-07-27** — see [below](#bom-line-unit-of-measure) | Admin / Manager / Supervisor |
| PUT | `/bom/items/{id}` | Update a BOM line. ⚠️ Same — an omitted `unit_of_measure` is left alone; an explicit clear re-inherits | Admin / Manager / Supervisor |
| DELETE | `/bom/items/{id}` | Remove a BOM line | Admin / Manager |

`POST /bom/` also accepts its lines inline, so it is a BOM-line write path too and carries the
same `unit_of_measure` change.

<a id="bom-controlled-document-state"></a>
> **Controlled-document state: what each verb refuses, and why.** A BOM's `status` is either
> `draft` or `released`; `released` is an approval, and the write surface enforces that. Every
> refusal below is **400** with a plain-string `detail` that is safe to render.
>
> | `status` | `PUT /bom/{id}` | line writes | `release` | `unrelease` | `delete` |
> |---|---|---|---|---|---|
> | `draft` | all fields | allowed | ✓ | 400 *"BOM is not released"* | ✓ (soft) |
> | `released` | **`description` only**; anything else 400 | **400** | 400 *"BOM is already released"* | ✓ | 400 *"Only draft BOMs can be deleted"* |
> | anything else (legacy) | 400 *"Cannot modify a BOM with status '…'"* | same 400 | 400 | ✓ → `draft` | 400 |
>
> - **`BOMUpdate.status` was removed.** It was an unvalidated free string that `PUT /bom/{id}`
>   blind-`setattr`'d behind a role gate one tier **wider** (Supervisor) than the `release` verb it
>   shadowed. A Supervisor could `PUT {"status": "released"}` and bypass both the Admin/Manager gate
>   and the "cannot release a BOM with no items" precondition, producing a released controlled
>   document with `approved_by` / `approved_at` **NULL** — an approved document with no approver.
>   `"draft"` un-released without clearing the approver, and any junk string stuck, which made the
>   frontend's `BOMStatus` union a lie. A client that still sends `status` gets **200**, the field
>   ignored (Pydantic `extra="ignore"`) and the true status echoed back in `BOMResponse`.
> - **A released BOM is frozen, not carved out.** Unlike `routing.update_operation`, which allows
>   in-place time-standard edits on a released routing, BOM has an `unrelease` verb — so the workflow
>   is **unrelease → edit → release**, which leaves withdrawal, changes and re-approval as three
>   separate rows on the audit chain instead of one silent mutation. Only `description` survives on a
>   released header, because it is metadata *about* the document rather than the configuration
>   (`revision` is the document's identity, `bom_type` changes how it explodes, `effective_date` is
>   AS9100D effectivity). A released `description` edit deliberately does **not** re-stamp the
>   approver. This is also the answer for the unit-of-measure worklist below: a mismatched line on a
>   released BOM cannot be corrected in place.
> - **`effective_date` on the request body is normalized to UTC before anything compares or stores
>   it.** The column is naive UTC while `BOMResponse` serves the field with a trailing `Z`, so a
>   client that PUT back exactly what `GET /bom/{id}` served it parsed to a *timezone-aware* value
>   that compared unequal to the stored naive one — the freeze above then answered **400** to a
>   request that changed nothing, and a payload carrying a non-UTC offset (`...T07:00:00-05:00`) was
>   stored as the local wall clock rather than the UTC instant. Both are fixed: the round trip is a
>   true no-op (**200**, nothing written, no audit row), an offset is *applied* rather than dropped,
>   and a genuine effectivity change on a released BOM is still refused.
> - **`unrelease` refuses only an already-`draft` BOM**, so it doubles as the de-corruption door for
>   any row the removed free-string field left holding a junk status — otherwise every verb would
>   refuse it and `BOM.part_id` being UNIQUE would strand the part with a permanently unusable BOM.
>   Its audit row records the *actual* prior status, not a hardcoded `"released"`.
> - **`POST /bom/` refuses when any BOM row already occupies the part** — active, inactive **or
>   soft-deleted** — because `BOM.part_id` is UNIQUE with no carve-out. The body names the recovery:
>   *"A deleted BOM exists for part 'X' — restore it before creating a new one."* The same probe and
>   bodies guard the import paths. Previously the probe looked only for active rows and the insert
>   then died on the constraint as an uncaught **500**; a residual row belonging to another tenant is
>   invisible to the (correctly company-scoped) probe and now yields a flat 400 rather than a 500.

<a id="bom-header-writes"></a>
> **BOM header writes are audited, and the delete is soft.** `create` / `update` / `release` /
> `unrelease` / `delete` / `restore` previously wrote **nothing** to `audit_log` — `release` was an
> unaudited approval of a controlled document, and `unrelease` NULLed `approved_by` / `approved_at`
> with no record that a named approval had ever existed. Each now writes one tamper-evident row
> under `resource_type="bom"`, logged **before** the terminal commit so it commits atomically with
> the change; `resource_identifier` is `"{part number} BOM rev {rev}"`, the part number resolved
> tenant-scoped. **The two import paths write that same handle**, so one `resource_type="bom"` chain
> carries one shape — see the audit note under the import endpoints for the forward-only
> discontinuity that creates. `POST /bom/` additionally writes one `bom_line` CREATE row per inline line.
> `unrelease`'s row carries the pre-image (`cleared_approved_by` / `cleared_approved_at` /
> `cleared_effective_date`) because that evidence exists nowhere else once the columns are cleared.
>
> `DELETE /bom/{id}` is a **soft** delete (`BOM` carries `SoftDeleteMixin`; the old handler issued a
> physical `db.delete` plus a bulk delete of every line — invariant 3). The lines are **kept
> physically intact** and reported as `extra_data.retained_line_count`, with no per-line DELETE audit
> rows: writing them for rows that still exist would be a false record, and `restore` would otherwise
> bring back an empty BOM. `POST /bom/{id}/restore` is a **prerequisite** of that conversion rather
> than a convenience — the UNIQUE `part_id` means a soft-deleted BOM permanently occupies its part's
> only BOM slot. Every BOM read path now filters `BOM.is_deleted`, including the shared
> `_get_active_bom` lookup that work-order release readiness, material requirements, job costing and
> MRP all resolve through, so a deleted structure stops driving production. Retention is only safe
> while **every** `BOMItem` reader reaches its lines through a header filtered on `is_deleted`; the
> two deliberate exceptions are the "is this part referenced anywhere" probes behind
> `DELETE /parts/{id}?hard_delete` and `DELETE /materials/{id}?hard_delete`, which must not filter
> because a retained line still holds a real foreign key. The inventory-demand forecast
> (`GET /analytics/predict/inventory-demand`) was an unlisted third and now joins the header like
> everything else; `delete_bom`'s docstring enumerates the full list with the grep that re-verifies it.

<a id="bom-line-writes"></a>
> **BOM line writes: audited, tenant-checked, and armed-part aware.** All three verbs above
> previously took no `AuditService` at all — creating, editing or deleting a line on a
> controlled document left no record of any kind, while the import paths in the same router
> always logged.
>
> - **Audit.** Each writes a tamper-evident `audit_log` row under `resource_type="bom_line"`
>   (CREATE / UPDATE / DELETE), logged **before** the terminal commit so the row commits
>   atomically with the change. `resource_identifier` is the human handle
>   `"{assembly} line {n} ({component})"`, with both part numbers resolved tenant-scoped.
>   `log_update` self-suppresses on an empty diff, so an idempotent `PUT` writes no row.
> - **The DELETE is physical, and that is recorded.** `BOMItem` carries no `SoftDeleteMixin`
>   (only `TenantMixin`), so there is no tombstone; the row really goes and the audit row —
>   `soft_delete: false`, carrying the full pre-image in `old_values` — is the only surviving
>   record of the line. Same shape and same handling as `routing.py`'s `delete_operation`.
>   Converting `BOMItem` to soft delete is a schema change and is deliberately **not** bundled
>   here; it is filed as a follow-up.
> - **`work_center_id` is tenant-checked on both write paths** (invariant #1). It is optional
>   on `BOMItemCreate`/`BOMItemUpdate` and previously rode in unvalidated, so a caller in one
>   company could point a BOM line at another company's machine and leak that machine's
>   identity through routing/explosion reports. A foreign or unknown id is a flat **404**
>   ("Work center not found"); an explicit `null` still clears the reference.
> - **`component_part.has_bom` now respects soft delete.** The probe ignored `BOM.is_deleted`
>   (and, at two of four sites, `company_id`), so a soft-deleted BOM made a component report
>   as an assembly — which is what drives the expand/drill-down affordance in the BOM tree.
> - **`backflush_armed_warning`** — a new optional response field on `BOMItemResponse`
>   (and a key on the DELETE response body), set **only** on these three writes and **only**
>   when the edited BOM helps state demand for a part armed via `Part.backflush_components`:
>   the BOM's own part, or an ancestor that reaches it through any line the explosion
>   descends into — that is, anything **except `buy`**. The same part numbers are hung on the
>   audit row as `extra_data.backflush_armed_parts`, which makes the arming verdict and the
>   later edit correlatable on one chain.
>
>   **`make` lines are followed, and `buy` is the only wall.** The tempting reading — a `make`
>   sub-assembly is issued as a stocked unit and its children never are, so an armed
>   grandparent is unaffected — is true of the BOM *demand* leg and **false** of the leg
>   beside it. `_explode_backflush_bom` still walks a `make` subtree in exclude-only mode, and
>   every line it passes there lands in `excluded_part_ids`; a routing operation naming a part
>   in that set raises `routing_component_excluded_by_bom` at **blocking** severity. So adding
>   a line under a `make` sub-assembly can newly *block* an armed ancestor's routing demand,
>   and deleting one can newly *un-block* it and let material issue that was being refused.
>   (`bom_depth_exceeded` is a second such path: it fires before the `consumed` check, so
>   deepening a `make` subtree past the level cap refuses the ancestor's whole leg.) A `buy`
>   line genuinely is a wall — the explosion never looks up a child BOM under one, so nothing
>   below it is read at all.
>
>   **It WARNS; it does not refuse.** The write succeeds. The opt-in gate
>   (`assert_backflush_change_allowed`) is a one-time check at the instant of the flip, and
>   `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Exposing the flag" is explicit that the
>   completion-time `BACKFLUSH_DEMAND_REFUSED` is the **net** behind it, *not* a second gate.
>   A 409 here would also block its own remedy: correcting a blocking unit mismatch is
>   documented as `PUT /bom/items/{id}`, which refusing on an armed part would make impossible
>   without first disarming. The warning states that a re-check is needed; it does not perform
>   one (re-running the readiness explosion on every line write would put the resolution
>   layer's verdict on the write path, which is exactly the coupling that layer avoids).

> **The three multi-level reads are tenant-scoped (invariant #1).** `GET /bom/{id}/explode`,
> `GET /bom/{id}/flatten`, and `GET /bom/{id}/where-used` resolve the top-level BOM against the
> active company only — a foreign or unknown id is a flat **404** ("BOM not found"), never an
> existence confirmation. The scoping runs all the way down: every sub-BOM lookup in the recursive
> explosion carries `company_id`, so the walk cannot descend into (or leak the structure of) another
> company's BOMs even through a corrupt/mis-parented line; `where-used` joins its BOMItem scan to
> BOM and filters `company_id`, so foreign parents never appear; and the circular-reference check on
> `POST /bom/{id}/items` walks only the active company's BOMs. Previously all of these were
> unscoped reads.

> **Part names on BOM responses are resolved tenant-scoped, not through the ORM relationships.**
> `BOM.part` and `BOMItem.component_part` both join on the foreign key alone and carry no
> `company_id` predicate, so on a mis-parented row (a residual foreign key — no supported write path
> creates one) they materialise **another company's** `Part` and every response builder printed its
> part number, name and revision straight back to the caller. Both the assembly's part and every
> line's component are now resolved through one batched, company-scoped read; a part that does not
> resolve renders as `null` (`part`, `component_part`) rather than as someone else's. This covers
> `GET /bom/`, `GET /bom/{id}`, `/by-part`, `/explode`, `/flatten`, `/where-used`,
> `PUT /bom/items/{id}`, `GET /work-orders/{id}/material-requirements`,
> `GET /work-orders/preview-operations/{part_id}`, and the `bom` / `routing` result titles on
> `GET /search`. The **write** side was already scoped. Clients must treat `part` and
> `component_part` as nullable on every one of these responses.

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

#### BOM line unit of measure

> ⚠️ **BEHAVIOUR CHANGE on shipped endpoints, 2026-07-27 (owner decision).** What the server
> stores for a BOM line that arrives **without** a `unit_of_measure` changed. **Before:** the
> literal `"each"`, from a schema default. **After:** the **component part's own**
> `unit_of_measure`. Same request, different stored value. No client change is required and
> no request is newly rejected — but a client that relied on omission meaning `"each"` no
> longer gets it, and **existing rows were not rewritten** (see the [report](#get-bomuom-mismatches--the-pre-arming-remediation-worklist)
> immediately below).

A BOM line's `unit_of_measure` is **optional on input**. When it is omitted, the server
resolves it to the component part's own; the literal `"each"` survives only as the last
resort — when the component cannot be resolved, or has no unit of its own. **A stated value
always wins**, so this resolves an *absence* and never second-guesses a caller. The two
document importers additionally normalise a stated value (`ea` → `each`, `lbs` → `pounds`)
exactly as they always have; the two JSON paths still store the client's string verbatim.
The **response** field stays non-null and its type is unchanged.

Applies to every BOM-line write path: `POST /bom/` (lines supplied inline),
`POST /bom/{id}/items`, `POST /bom/import/commit` and `POST /bom/import`.
`PUT /bom/items/{id}` is the one exception and deliberately so — **it is not a
backfill**: a request that does not mention `unit_of_measure` leaves the stored value
untouched, and only an explicit clear (`null` or `""`) is read as "no stated unit" and
re-inherits from the component.

Why the default was wrong rather than merely arbitrary: `unit_of_measure_mismatch` is a
**blocking** backflush diagnostic that reads a stored unit as a *stated claim*. Nothing in
the platform converts units, so a line stating `each` against a part stocked in `sheets`
would issue the wrong quantity of the right material — the diagnostic refuses
`Part.backflush_components` at opt-in (**409**) and refuses that component at completion.
Against a real sheet-metal BOM set that fired on a value nobody had chosen. **The fix was the
default; the severity was deliberately left alone.**

#### BOM line `scrap_factor` — a fraction, bounded `0 ≤ scrap_factor ≤ 1`

`scrap_factor` is a **fraction**, not a percentage: `0.05` is a 5% allowance. Entering `5`
meaning "5%" is the natural mistake, and it used to be accepted and stored as a 500%
allowance. `BOMItemBase` / `BOMItemUpdate` now bound it `ge=0, le=1`, so an out-of-range value
is a **422** naming the bound on `POST /bom/` (lines supplied inline), `POST /bom/{id}/items`
and `PUT /bom/items/{id}`. The ceiling matches `chk_bom_items_scrap_factor_range`, the DB CHECK
migration `080_restore_stamped_over_con` restored; without it the same request would fail as an
`IntegrityError` **500** at flush. The two document importers are unaffected — their schema has
no `scrap_factor` field, so an imported line always takes the `0.0` default.

Note the asymmetry with BOM-line **`quantity`**, which stays bounded at the API (`gt=0`) but
deliberately carries **no** DB CHECK: a non-positive quantity must remain *representable* so the
blocking `zero_bom_quantity` / `negative_bom_quantity` backflush diagnostics can surface it for a
human to correct. See `docs/DEVELOPMENT.md` → Database Migrations for why that exclusion is
deliberate.

#### `GET /bom/uom-mismatches` — the pre-arming remediation worklist

Existing lines are **not rewritten** — this series is correct-forward and does not backfill —
so lines written before the default changed keep their stored `"each"` and still block. This
endpoint is how a human finds and corrects them.

| Query param | Default | Meaning |
|---|---|---|
| `part_id` | – | Only lines on this assembly part's **own** BOM. Does **not** follow nested sub-assembly BOMs, which a readiness check *does* reach — so the **unfiltered** report is the authoritative pre-arming worklist |
| `bom_id` | – | Only lines on this BOM |
| `component_part_id` | – | Only lines naming this component part |
| `active_only` | `true` | Only active BOMs — the ones a backflush actually reads |
| `skip` / `limit` | `0` / `100` (max `500`) | Paging over the matched set |

Response: `{ total, returned, truncated, items[] }`, each item carrying `bom_id`,
`bom_revision`, `bom_status`, `bom_is_active`, `part_id`, `part_number`, `bom_item_id`,
`item_number`, `component_part_id`, `component_part_number`, `component_part_name`,
`component_is_deleted`, `line_unit_of_measure`, `component_unit_of_measure` and
`blocks_backflush`.

- The comparison is `models.part.uom_disagrees`, the **same predicate the blocking diagnostic
  uses**, so the report and the gate cannot list different rows. Comparison is exact-label:
  `ea` does **not** satisfy `each` — normalise the stored value rather than expecting the gate
  to accept it.
- A blank unit on **either** side is not a disagreement and is not reported.
- `blocks_backflush` is `false` on alternate / optional / reference lines: the backflush never
  issues those, so they raise no diagnostic and refuse nothing. Work them last.
- **`blocks_backflush: true` answers the LINE, not the whole tree — it can over-promise.** It is
  computed from the line's own type alone. A line sitting inside a `make` sub-assembly's BOM
  reports `true`, but `make` subtrees are walked exclude-only and collect no per-line
  diagnostics, so that line refuses nothing when the *parent* assembly is armed (its children
  were consumed when the sub-assembly was built). Treat `true` as "worth correcting", not as
  "this is what is refusing my part" — the authoritative list of what actually refuses a given
  part is `blockers` on `GET /parts/{part_id}/backflush-readiness`.
- Soft-deleted component parts are **included** (flagged by `component_is_deleted`), because
  the readiness explosion resolves them on purpose; filtering them out would hide a row that
  still blocks.
- `truncated: true` means the scan hit its candidate ceiling and `total` is a **floor**, not a
  count — narrow the filters and run it again.

##### Where this is worked — the **BOM Unit Mismatches** screen

The report shipped API-only in PR 4.5 and **is no longer API-only**. It has a UI at
**`/bom/uom-mismatches`**, reachable from the sidebar under **Engineering → BOM Unit
Mismatches** (directly after *Bill of Materials*). Route and nav entry are both gated on
`boms:edit` — **Admin / Manager / Supervisor**, exactly the endpoint's own
`require_role([ADMIN, MANAGER, SUPERVISOR])` — so a role that cannot act on a row neither sees
the link nor gets past the route guard by deep-linking. Client method:
`api.getBOMUomMismatches(params)` in `frontend/src/services/api.ts`, typed by
`BOMUomMismatchReport` / `BOMLineUomMismatch` / `BOMUomMismatchParams` in
`frontend/src/types/index.ts`; the page is `frontend/src/pages/BOMUomMismatches.tsx`.

What the screen is, and what it deliberately is not:

- **Read-only — it finds rows and hands off.** There is no inline BOM-line editor. Every row
  deep-links to `/bom?id={bom_id}` for the correction and to `/parts/{part_id}` for that
  assembly's own readiness. The original reason was that BOM-line create/update/delete wrote
  **no audit rows at all**, so making this screen the primary remediation flow would have put a
  compliance-critical correction on an un-audited endpoint. **That blocker is now closed** —
  all three verbs audit as `bom_line` (see [BOM line writes](#bom-line-writes)) — so an inline editor
  is no longer refused on compliance grounds; it is simply not built. Corrections stay on the
  BOM screen, where they already were.
- **Server-paged** (50 rows/page over `skip`/`limit`), so column sort is deliberately
  unavailable — sorting one page of a server-paged set reorders a window, not the worklist.
- **Filters live in URL params** (`part_id`, `bom_id`, `component_part_id`, `active_only`,
  `page`), so a filtered worklist is linkable and survives a reload. All five are parsed with
  the same strict guard — a positive integer or nothing — so a hand-edited `?page=1.1` cannot
  become a fractional `skip` the endpoint 422s, and `?page=2.5` cannot become a silently
  non-aligned window. The two part pickers search with `active_only: false` on purpose: a
  mismatch can name an inactive or soft-deleted component, and a picker that could not select
  one could not filter to the very rows this report exists to disclose.
- **`truncated` is surfaced loudly** — an amber banner above the table, and the count tile
  renders `≥ N` with the subtitle *"Floor — scan ceiling hit"*. Read as: the total is a
  **floor, not a count**, and this page is not evidence that a part is clean. Narrow the
  filters and run it again.
- **`blocks_backflush` is labelled "Line effect"**, valued *Would be issued* / *Never issued* —
  never "blocking your part", because `true` can over-promise. A permanent panel on the screen
  states that a line inside a `make` sub-assembly reads *Would be issued* and still refuses
  nothing when the parent assembly is armed, and that an assembly filter does not follow nested
  sub-assembly BOMs — so **the unfiltered list is the authoritative worklist**. The
  authoritative *per-part* answer is the **Part readiness** link on every row
  (`GET /parts/{part_id}/backflush-readiness`) — with the caveat the panel and the link tooltip
  both state: that card renders only for a part typed `manufactured` / `assembly` or one already
  armed, so a BOM hanging off a `purchased` part opens a page with **no readiness card at all**,
  and the part type is what to correct first. The link never promises a verdict silently.
- **`component_is_deleted` rows are kept and flagged**, not filtered: a red *Deleted part* chip,
  a tinted row, and a marker in the CSV export.
- **Empty means good news only when it is the whole answer.** "No rows" has four causes and the
  screen distinguishes them, because the unfiltered copy — *"No unit-of-measure mismatches —
  every BOM line states the unit its component part is stocked in. Nothing here is blocking a
  part from being armed"* — is a **conclusion about the shop**, and it is earned only on page 1
  of a complete, unfiltered scan. A **filtered** empty result says so separately and offers to
  clear the filters. A **truncated** scan that empties a page says the scan was incomplete and
  that the page is *not* an all-clear (the amber banner alone was not enough: printed directly
  above the clean copy it read as a contradiction, and the clean sentence is the one shaped like
  a conclusion). A page **past the end of the worklist** — `?page=` is durable URL state that
  outlives the rows it was written against: a shared link, a reload after remediation, an
  active-company switch — says exactly that and offers **Back to page 1**, because `DataTable`
  replaces the whole container, pager included, with the empty state. So no empty page —
  filtered, truncated, or out of range — is ever mistaken for an empty shop.

##### The remediation sequence — run this BEFORE arming any real part

1. **Run the report unfiltered.** Open `/bom/uom-mismatches` with no filters set (equivalently
   `GET /bom/uom-mismatches` with no `part_id` / `bom_id` / `component_part_id`). Leave
   `active_only=true` — those are the BOMs a backflush actually reads. Read the count; if the
   truncation banner is up, that count is a **floor**, so narrow the filters and re-run rather
   than reporting it as a total.
2. **Correct the lines on the BOM screen.** Follow each row's link to `/bom?id={bom_id}` and
   either fix the **line** (`PUT /bom/items/{id}` — state the real unit, or send
   `unit_of_measure: null` / `""` to re-inherit the component's) or fix the **component part's
   own** `unit_of_measure` where the stocking unit is the wrong record. Both are ordinary
   audited human edits; neither is a backfill. ⚠️ Correcting the **part** re-scores every BOM
   line that names it, across every assembly — so re-run the report **unfiltered** after any
   part-side correction. Rows reading *Never issued* (alternate / optional / reference) are
   cosmetic: work them last or not at all.
3. **Re-check the part** with `GET /parts/{id}/backflush-readiness` — the backflush card on the
   part page shows the same verdict and blocker list. This, not an empty report, is the
   authoritative per-part answer: the report answers *lines*, the readiness check answers the
   *part*, and it is the same function the arming gate runs.
4. **Arm it** — `PUT /parts/{id}` with `backflush_components: true`. A part whose lines still
   disagree is refused **409** with the blocker sentences as `detail`, and that refusal is the
   control working, not a bug to route around.

Clearing this list is not a lock. Every line stays editable afterwards by the same
Admin / Manager / Supervisor tier, so an empty report is evidence about the minute it was run.
The BOM edit path is no longer *blind* — a line write warns and annotates its audit row when
the edited BOM feeds an armed part (see [BOM line writes](#bom-line-writes)) — but a warning
is not a lock either. What backs the part after
the flip is the completion-time refusal (`BACKFLUSH_DEMAND_REFUSED`), not this list.

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
>
> The BOM row's `resource_identifier` is `"{part number} BOM rev {rev}"` — the **same** handle the
> six [BOM header verbs](#bom-header-writes) write, so an auditor pulling one document's
> `resource_type="bom"` chain sees one shape from the CREATE through every later row. Both importers
> previously wrote the **bare** assembly part number here, which made the import-born CREATE the only
> row about that BOM that did not match the rest (and omitted the revision the importer had just
> written to the row). Audit rows are immutable and are never backfilled (invariant 2), so this is
> **forward-only**: rows written before the change keep the bare shape. The discontinuity is
> one-directional — the new form *contains* the old one, and audit search matches
> `resource_identifier` with a substring `ILIKE`, so a search by part number still finds rows of both
> shapes; only a search for the full new string misses the older ones.

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
| PUT | `/work-centers/{id}` | Update work center (an `is_active: false` flip is guarded — see note below) | Admin / Manager |
| DELETE | `/work-centers/{id}` | Deactivate work center (`is_active` → `false`; `WorkCenter` has no soft-delete mixin) — guarded, see note below | Admin |

> **Deactivation is refused while live work still references the machine (409).** `DELETE
> /work-centers/{id}` and a `PUT /work-centers/{id}` that flips `is_active` `true → false` both
> count the work center's **incomplete operations** (every operation status except `COMPLETE` —
> deliberately broader than the dispatch queue: `PENDING`/`ON_HOLD` work is off the queue today but
> would be stranded on the machine just the same) on **live** work orders (non-deleted, not
> `COMPLETE`/`CLOSED`/`CANCELLED`). Any such work → **409** with a plain-string detail carrying the
> total, a per-status breakdown (ready → in progress → pending → on hold), and the remedy:
> `"Cannot deactivate LSR-1: 3 operations still have live work here (2 ready, 1 in progress). Move
> them to another machine (Dispatch Board -> Move to machine) or complete them first."` (singular
> grammar when the count is 1). The guard runs **before** anything mutates — a refusal leaves the
> row untouched — and a repeat `DELETE` of an **already-inactive** work center that still holds
> live work also 409s (previously an unconditional 200; nothing endorses the stranded state).
> Reactivation (`false → true`) is never guarded. Work stranded by a pre-guard deactivation
> surfaces on the dispatch board as a flagged read-only column — see Shop Floor →
> `GET /shop-floor/dispatch-board`.
>
> ⚠️ **`GET /work-centers/` caches per company, and the key used to be install-wide.** The
> default-parameter response is cached for 15 minutes. The query was always scoped to the
> caller's company; the cache key was the bare `work_centers:list`, so the first tenant to
> warm it had its machine roster served verbatim to **every other tenant** for the duration —
> a cross-tenant disclosure with no query defect anywhere near it. The key now carries the
> company id. Invalidation is unchanged and still a blanket wipe (`work_centers:list*`, which
> still matches the scoped keys): over-invalidation costs one recomputation, under-invalidation
> serves a stale roster. Note the cache no-ops entirely when Redis is not configured, which is
> why the regression tests drive the cache layer directly rather than relying on the endpoint.
>
> **Every state-changing work-center endpoint writes tamper-evident `audit_log` rows.** `PUT`
> logs a `work_center` `log_update` with a full before/after column diff; `DELETE` logs the
> `is_active` flip. Both self-suppress when nothing actually changed (a no-op update, or a
> `DELETE` of an already-inactive row — no fabricated diff on the hash chain). `POST
> /work-centers/` logs a `log_create` matching the CSV importer's row minus its
> `extra_data.source = "import"` marker, which is what distinguishes the two doors. `POST
> /work-centers/{id}/status` logs a `log_status_change` carrying old and new status; because
> `log_status_change` does **not** self-suppress the way `log_update` does, a request that
> restates the current status short-circuits — no write, no audit row, no broadcast.
>
> **RBAC on `POST /work-centers/{id}/status` was tightened to Admin / Manager** (it previously
> accepted any authenticated user in the tenant). It is the only writer of `current_status`
> outside the CSV importer, and flipping a machine to `offline`/`maintenance` changes what the
> dispatch board and the operator kiosk show. The tier matches `PUT`, not `DELETE`: the flip is
> reversible, and `PUT` already lets a Manager flip `is_active`. The page's inline status
> `<select>` is gated to the same role set client-side and renders as a read-only badge for
> everyone else, so a control the server will refuse is not offered. That client gate is
> **defense in depth, not the primary gate**: `/work-centers` is already route-guarded on
> `admin:settings` (`App.tsx` → `routeAccessRequirements`), which resolves to platform_admin
> + admin — a *narrower* set than these endpoints allow, so a **Manager can call the endpoint
> but cannot reach the page**. That route/nav/endpoint tier misalignment is tracked
> separately; the client gate is what keeps the control tied to its own verb's role set when
> it is resolved. ⚠️ Inside `update_work_center_status` the `status`
> query parameter **shadows the fastapi `status` module**, so `status.HTTP_*` there raises
> `AttributeError` at request time while type-checking and importing clean; use literal int
> status codes. The parameter name is part of the API contract and is not renamed.

> **`hourly_rate` is bounded `ge=0` — a negative rate is a 422, not a stored value.** `POST
> /work-centers/` and `PUT /work-centers/{id}` refuse `hourly_rate < 0` at the data boundary
> (Pydantic `Field(ge=0)` on `WorkCenterBase` / `WorkCenterUpdate`). It used to be accepted and
> persisted; the bound matches `chk_work_centers_hourly_rate_non_negative`, the DB CHECK migration
> `080_restore_stamped_over_con` restored, so without it the same request would now fail as an
> `IntegrityError` **500** at flush instead. No other work-center field changed, and the
> 2026-07-31 prod pre-flight found zero existing rows below 0.

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

### Maintenance (PM schedules, maintenance work orders, event log)

`app/api/endpoints/maintenance.py`, mounted at `/maintenance`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/maintenance/schedules` | List PM schedules | Yes |
| GET | `/maintenance/schedules/{id}` | PM schedule detail | Yes |
| POST | `/maintenance/schedules` | Create PM schedule | Admin / Manager / Supervisor |
| PUT | `/maintenance/schedules/{id}` | Update PM schedule | Admin / Manager / Supervisor |
| DELETE | `/maintenance/schedules/{id}` | Deactivate PM schedule (`is_active` → `false`; no soft-delete mixin) | Admin / Manager / Supervisor |
| GET | `/maintenance/work-orders` | List maintenance work orders (**pure read**, see below) | Yes |
| GET | `/maintenance/work-orders/overdue` | Overdue maintenance work orders (**pure read**) | Yes |
| GET | `/maintenance/work-orders/{id}` | Maintenance work order detail | Yes |
| POST | `/maintenance/work-orders` | Create maintenance work order | Admin / Manager / Supervisor |
| PUT | `/maintenance/work-orders/{id}` | Update maintenance work order | Admin / Manager / Supervisor |
| POST | `/maintenance/work-orders/{id}/start` | Start (→ `in_progress`) | Admin / Manager / Supervisor / Operator |
| POST | `/maintenance/work-orders/{id}/complete` | Complete (→ `completed`, advances the linked schedule) | Admin / Manager / Supervisor / Operator |
| GET | `/maintenance/calendar` | Work orders in a date window (**capped at 366 days**) | Yes |
| GET | `/maintenance/dashboard` | Counts, cost, per-machine MTBF/MTTR | Yes |
| GET | `/maintenance/history/{work_center_id}` | Event log for one machine (`limit` 1…5000, default 100) | Yes |
| POST | `/maintenance/log` | Add a maintenance event log entry | Admin / Manager / Supervisor / Operator |

> ⚠️ **Three write paths were returning 500 in production, two of them *after* committing.**
> `MaintenanceLog` carries `TenantMixin`'s NOT NULL `company_id` (migration `026` drops the interim
> `server_default`) and `start`, `complete` and `POST /maintenance/log` all built one without it, so
> the insert raised `IntegrityError`. `start` and `complete` had already committed the work-order
> state change in a separate `db.commit()`, so an operator saw a 500, reloaded, and found the job
> running or closed anyway — a silent partial success that left the PM event history permanently
> empty. All three now stamp `company_id` and share **one** commit with the state change, so a
> request either fully succeeds or fully rolls back.

> **Every handler is now tenant-scoped, and foreign ids 404.** Ten of the sixteen handlers took no
> company argument: `start` and `complete` resolved the work order by bare id (guessing an integer
> started or closed another company's maintenance, and `complete` then advanced whatever
> `MaintenanceSchedule` that work order pointed at), and `dashboard` / `calendar` / `history` /
> `overdue` aggregated across every tenant. `work_center_id` (create schedule, create work order,
> create log), `schedule_id` (create work order) and `maintenance_wo_id` (create log) are all
> validated against the caller's company and answer a flat **404** — never 403 — with the same
> detail a genuinely missing id gets, so the status code is not an existence oracle over another
> tenant's equipment list.
>
> **Expect the dashboard numbers to drop on a multi-company install.** They were summing every
> tenant; that is the correction, not a regression. On a single-company install nothing changes and
> a smoke test can detect neither the bug nor the fix.

> **`work_center_name` reads as `null` for a legacy cross-tenant row.** The write guards stop new
> mis-tenanted rows but do nothing about rows written before them, and the serializer's relationship
> carries no predicate — a row owned by company B pointing at company A's machine passes every
> `company_id` filter (it really *is* B's row) and used to render A's machine name straight back.
> The serializers now null the related field when its `company_id` differs; `work_center_id` stays
> visible so the row can be corrected. See the pre-deploy detection SQL in
> `docs/RBAC_PERMISSIONS.md` → Maintenance.

> **No GET persists a status change any more.** Both `GET /maintenance/work-orders` and `GET
> /maintenance/work-orders/overdue` used to walk their result set flipping `scheduled` → `overdue`
> and `db.commit()` it — a status change with no actor behind it, no reason recorded and no
> `AuditService` row (invariant 2); `/overdue` did it **unscoped**, i.e. on every tenant's rows.
> Both are now pure reads. The `overdue` label still appears in the payload (derived from
> `due_date`), so responses are unchanged. Both consumers of the signal — `GET
> /maintenance/dashboard` and the AS9100D evidence card in `auto_evidence_service` — now derive it
> from `due_date` too, which is strictly more accurate: the overdue count no longer depends on
> whether a human loaded the Maintenance page today.

> **RBAC was added; there was none.** Every endpoint was a bare `get_current_user`, so a **viewer
> could create, start and complete maintenance work orders**. Reads stay open to any authenticated
> user (the `/maintenance` route is gated on `work_orders:view`, which a viewer holds). Planning
> verbs match the `work_orders:create`/`work_orders:edit` role set; performing verbs (start /
> complete / log) additionally admit **Operator**, mirroring `work_orders:complete` — the
> maintenance tech doing the work signs in as one. `require_role` always admits platform admins and
> superusers.

> **Every state change writes a tamper-evident `audit_log` row** (invariant 2 — the router
> previously wrote none at all). Resource types: `maintenance_schedule` (`CREATE` / `UPDATE`, with
> the deactivation logged as the `is_active` flip), `maintenance_work_order` (`CREATE` / `UPDATE`,
> plus `STATUS_CHANGE` on start and complete — the complete row carries `total_cost`,
> `downtime_minutes`, `actual_duration_hours` and `schedule_id` in `extra_data`), and
> `maintenance_log` (`CREATE`). Rows are logged **before** the terminal commit so they commit
> atomically with the change.

> **`GET /maintenance/calendar` refuses a window wider than 366 days (400)**, and refuses
> `end_date < start_date` (400). It was the one unbounded read left in the file — `start_date` and
> `end_date` are caller-supplied and there is no `limit`, so a single request could serialize every
> maintenance work order a tenant has ever had. Same posture as the list/export bounds elsewhere.

> **`wo_number` allocation stays install-wide on purpose.** `MaintenanceWorkOrder.wo_number` carries
> a *global* unique constraint, so a per-tenant sequence would hand two companies the same number
> and the second insert would fail. The scan reads only the highest number, never row content.

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

> **Time standards are bounded `ge=0` — a negative `setup_hours` / `run_hours_per_unit` is a 422.**
> Enforced at the data boundary on `RoutingOperationBase` / `RoutingOperationUpdate`, so it covers
> `POST /routing/{id}/operations`, `PUT /routing/{id}/operations/{operation_id}`, and the AI
> routing-generation approve payload (`RoutingCreateFromGeneration.operations`, a list of
> `RoutingOperationCreate`). The CSV importer already rejected negative hours
> (`routing_import_service._parse_hours`); the interactive and AI paths did not. The bounds match
> `chk_routing_ops_setup_hours_non_negative` / `chk_routing_ops_run_hours_non_negative`, the DB
> CHECKs migration `080_restore_stamped_over_con` restored — without them a negative value would now
> be an `IntegrityError` **500**, and it would chain: work-order operations snapshot these into
> `setup_time_hours` / `run_time_hours`, which carry CHECKs of their own. `move_hours`,
> `queue_hours` and `cycle_time_seconds` are **not** bounded (no DB CHECK targets them).

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
| GET | `/shop-floor/operations` | List not-complete/cancelled operations for the desktop shop-floor pages (paginated, max 200/page; filters `work_center_id` / `status` / `search` / `due_today`) — rows in the **canonical dispatch order**, each carrying the advisory `run_order` display rank (see "Desktop parity" under the Dispatch run order note below) | Yes |
| GET | `/shop-floor/operations/{id}/documents` | Kiosk doc-viewer discovery: the operation's controlled part drawing, live nest reference PDF, nest material, and critical SPC characteristics (see note below) | Yes |
| GET | `/shop-floor/documents/{id}/inline` | Serve a kiosk-viewable document PDF inline — DRAWING-type or live-nest-referenced only, tenant-scoped, uniform **404** on any miss (see note below) | Yes |
| POST | `/shop-floor/clock-in` | Clock in to operation | Yes |
| POST | `/shop-floor/clock-out/{id}` | Clock out with production data | Yes |
| POST | `/shop-floor/operations/{id}/start` | Start an operation | Yes |
| POST | `/shop-floor/operations/{id}/production` | Add produced/scrapped quantity while staying clocked in | Yes |
| POST | `/shop-floor/operations/{id}/reduce-production` | Correct (walk back) good-count an operator OVER-reported on their **own unapproved** labor (open clock-in first, then their own earlier unapproved sessions), **before** the operation/WO is complete — a miscount fix, **not** scrap (see note + schema below) | Yes |
| POST | `/shop-floor/operations/{id}/complete` | Complete / report progress on an operation | Yes |
| PUT | `/shop-floor/operations/{id}/hold` | Put an operation on hold (closes open time entries; body optional — category/severity/note file a structured blocker) | Yes |
| POST | `/shop-floor/operations/{id}/inspection` | Record operation inspection complete (sets `inspection_complete`) | Admin / Manager / Supervisor / Quality |
| POST | `/shop-floor/time-entries/{id}/approve` | Approve a TimeEntry (sets `approved` / `approved_by`) | Admin / Manager / Supervisor / Quality |
| POST | `/shop-floor/time-entries/{id}/unapprove` | Clear approval on a TimeEntry | Admin / Manager / Supervisor / Quality |
| GET | `/shop-floor/work-center-queue/{id}` | Get work center queue, each row carrying the live crew `roster` and the manager-set `run_order` (see notes below) | User **or** kiosk station token |
| GET | `/shop-floor/dispatch-board` | Manager dispatch board — every **active** work center with its live queue, including work centers with an **empty** queue, plus any **deactivated** work center still holding queued work, flagged `is_active: false` (see note below) | Admin / Manager / Supervisor |
| PUT | `/shop-floor/work-centers/{id}/run-order` | Rewrite one work center's manual run order (dense 1..N; omitted operations become unranked) → that work center's refreshed column (see note below) | Admin / Manager / Supervisor |
| GET | `/shop-floor/wallboard` | Read-only TV wallboard snapshot (`?dept=` narrows to one work-center type, case-insensitive — scopes the work centers, the `jobs` grid (by each WO's **current** operation's work center), **and** the late/blocked lists + totals; ship/today/quality stay plant-wide) | User **or** display token |
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
> mutate state, and a display token has no user identity to attribute writes to. The payload is
> built to be **public-safe by default**: operator identity is truncated to "First L." (`crew` /
> `operator_name`), and the ship/today/quality blocks carry counts, ages, WO/part numbers and dates
> only — **no ship-to addresses, no dollar figures, no NCR titles/descriptions**. The **one gated
> exception** is `jobs[].customer_name`: it is populated **only** when the request principal is
> authorized — a display token with `show_customer_names=true`, OR a signed-in user whose role is
> `PLATFORM_ADMIN` / `ADMIN` / `MANAGER` — and is `null` for every public / un-flagged display token
> and every other signed-in role (`build_wallboard_payload(..., include_customer=...)`, derived from
> the principal, never from client input). Signed-in users can call it too (their active company
> scopes the data). Payload:
> - `work_centers[]` (`{code, name, status, active_jobs[], queued_count, blocked_count, down}`).
>   Each active job is **one row per operation** (crew-station grouping): `{wo_number, part_number,
>   op_name, crew[]` (up to 3 "First L." names)`, crew_count` (true headcount)`, operator_name`
>   (back-compat alias of `crew[0]`)`, elapsed_minutes` (earliest open clock-in)`, qty_done,
>   qty_target, is_late}`. `is_late` is server-computed: promise (`coalesce(must_ship_by,
>   due_date)`, the OTD precedence) before today's Central date on a live, non-terminal WO — the
>   same predicate as `late_wos` / `late_total`. Still shipped in full: old TV bundles render it
>   as the machine wall, and the current (Foundry, 2026-07-22) board joins `work_centers[].down`
>   for its card stop reasons/durations and the BLOCKED·DOWN rail rows.
> - **`jobs[]` / `jobs_total`** — the main work-order grid (the 2026-07-15 job wall, rendered
>   since 2026-07-22 as the Foundry 4×3 card grid): open
>   (**RELEASED / IN_PROGRESS**) WOs — **ON_HOLD deliberately excluded** (the quality rail counts
>   holds) — priority-sorted server-side (blocked/down → most-late → running → promise date asc),
>   capped at **24**, with `jobs_total` the true uncapped count for `+N more`; **dept-scoped**
>   via each WO's **current** operation's work-center type when `?dept=` is passed. Each job:
>   `{wo_number, part_number, customer_name` (**gated** — see below)`, status, qty_complete, qty_ordered` (WO-level)`, promise_date,
>   is_late, days_late` (the same shared lateness predicate)`, blocked` (any unresolved blocker
>   on the WO)`, down` (current op's work center has an open downtime event)`, running` (current
>   op has ≥1 open labor entry)`, ops_completed, ops_total, current_op}`; `current_op` — the
>   lowest-sequence IN_PROGRESS op, else lowest READY, else lowest PENDING; `null` when all ops
>   are complete — is `{sequence, name, work_center_code, work_center_name, status, qty_done,
>   qty_target, crew[]` (≤3 "First L.")`, crew_count, elapsed_minutes}`. Job tiles carry WO/part/op
>   identifiers, dates, quantities, and "First L." crew names only — never dollars or notes.
>   `customer_name` is the one **gated** field: populated only for an authorized principal (display
>   token opted in via `show_customer_names`, or a signed-in `PLATFORM_ADMIN` / `ADMIN` / `MANAGER`),
>   `null` on every public board. Absent only from a pre-job-wall backend (the current TV then renders a
>   `BOARD DATA UNAVAILABLE` state; only pre-redesign TV bundles still render the `work_centers`
>   machine wall).
> - `late_wos[]` (worst-first), `blocked_wos[]` (oldest-first) — capped at **12**; `late_wos[].due_date`
>   carries the promise date under the original field name. **Dept-scoped** when `?dept=` is passed
>   (late via any open op routed to a dept work center; blocked via the blocker's operation's work
>   center — a blocker with no operation appears only on the unfiltered board).
> - `late_total` / `blocked_total` / `down_total` — true **uncapped** counts (dept-scoped with the
>   lists); `down_total` = active work centers with an open downtime event.
> - **`ship`** (plant-wide, Central-day window): `due_today` = all WOs promised today via
>   `must_ship_by || due_date` (one population), `shipped_today` = of those, fully shipped (the
>   analytics counted-shipment rules), `due_this_week` (promised today..+6, not fully shipped),
>   `due_today_rows[]` (top 2 open by qty remaining — `{wo_number, part_number, promise_date,
>   qty_remaining}` only), `next_due_date` / `next_due_count` when nothing is promised today.
> - **`today`** (plant-wide, Central-midnight window): `ops_completed`, `pieces_completed`
>   (RUN+REWORK, provenance-excluded), `wos_completed`, `operators_on_clock` (distinct users with
>   an open time entry, any entry type), `hours_logged`, `receipts`, `scrap_events`
>   (provenance-excluded). Aggregates only.
> - **`quality`** (plant-wide): `open_ncr_count`, `newest_ncr_age_days`, `wos_on_hold` — counts and
>   ages only, never NCR text.
> - **`kpi_strip`** — **deprecated, always `null`** (the trailing-30-day strip was dropped from
>   the TV on 2026-07-15; the server no longer computes it, and the field survives only for wire
>   back-compat with old TV bundles, which render an em-dash cluster on `null` — see
>   [docs/WALLBOARD.md](WALLBOARD.md) → KPI strip — deprecated), and `generated_at`.
>
> Every block/field added after A0.5 v1 is **optional** (old TVs ignore them; a new TV against an
> old backend renders em-dashes — or the `BOARD DATA UNAVAILABLE` grid state, when `jobs` is
> absent), and `ship` /
> `today` / `quality` are each independently best-effort — a failed block is `null` on that poll,
> never a failed payload. The `jobs` block is core like `work_centers` — computed inline, not
> best-effort.
> Token issuance/revocation: see Authentication → Display tokens. Operating a TV:
> see [docs/WALLBOARD.md](WALLBOARD.md).

> **Dispatch run order (`run_order`) — advisory, never a gate.** `WorkOrderOperation.run_order`
> (nullable int, indexed; migration `068`) is a manager-dictated **dense 1..N rank within the
> operation's current work center**. `null` = unranked. It orders and labels the queue and it
> **never** gates a start — operators may start any queued job, the same posture as the laser
> dispatch pool. It is **not** `sequence`: `sequence` is routing-step precedence *within one work
> order* and does drive predecessor gating; `run_order` is cross-work-order, scoped to one work
> center, and gates nothing.
>
> **Queue ordering (`GET /shop-floor/work-center-queue/{id}`, kiosk *and* crew station).** The queue
> previously ordered by `scheduled_start` alone — nullable, no tiebreaker, and usually `null`, so
> the operator's order was effectively arbitrary *and* dialect-dependent (PostgreSQL sorts nulls
> last, SQLite first). It now orders by:
> 1. `run_order IS NULL` ascending — ranked work first, unranked last (an explicit boolean key, so
>    PostgreSQL and SQLite agree without a `NULLS LAST` clause);
> 2. `run_order` ascending;
> 3. `WorkOrder.priority`, then `WorkOrder.due_date`, then `WorkOrderOperation.sequence` — the
>    repo's canonical fallback for unranked work;
> 4. `WorkOrderOperation.id` as a final deterministic tiebreak.
>
> Each queue row now carries **`run_order`** (int or `null`) alongside the existing keys. The served
> value is the **gap-free display position** (1..N) within the ordered queue, not the raw stored
> rank — stored ranks go sparse in normal use (completing or moving a job takes its rank out of the
> column, leaving e.g. 1, 2, 4), and "RUN 4" on a three-job queue reads as a missing job. Unranked
> rows are `null` and take no position. The kiosk renders **server order** and only *displays* the
> rank (`RUN n` chip) — it never re-sorts client-side. The filter set is unchanged (operation
> `READY`/`IN_PROGRESS`, parent WO not `COMPLETE`/`CLOSED`/`CANCELLED` and not soft-deleted) and is
> now shared with the dispatch board below, so the manager's board and the operator's tablet can
> never disagree. See [docs/KIOSK.md](KIOSK.md).
>
> **Desktop parity (`GET /shop-floor/operations` — the Time Clock and Operations pages).** The same
> order is what the desktop surfaces show. `GET /shop-floor/operations` (any authenticated user;
> pagination and filter semantics unchanged) returns rows in the **canonical dispatch order**:
> across work centers by `WorkCenter.code` — the Dispatch Board's column order, operations with no
> work center last — and within each work center by the queue sort above, so a single
> `work_center_id` filter lists the queue rows in exactly the `work-center-queue/{id}` order. Each
> row carries **`run_order`**: the same gap-free position in that work center's **live** dispatch
> queue, computed over the full queue — never the returned page or the endpoint's filtered subset —
> so the number always equals the kiosk `RUN` chip and survives pagination/filtering; `null` when
> the operation is unranked or not currently queued. One nuance, because this endpoint also returns
> non-queued rows (`PENDING`/`ON_HOLD` by default, `COMPLETE` under `?status=complete`): a
> non-queued row that retains a **stale stored rank** still sorts by it inside its work-center
> group, but its `run_order` field is `null` — it shows no chip and steals no position from the
> live queue. The two desktop pages (`/shop-floor` "Time Clock" — fed by `work-center-queue/{id}` —
> and `/shop-floor/operations` "Operations") render the server order **verbatim** and display the
> rank with the same `RUN n` chip as the kiosk; the client-side dispatch-score re-sort both pages
> previously applied is removed, so the board, the kiosks, and the desktop can no longer disagree
> on what runs next. As everywhere, advisory only: the rank never gates a start.
>
> **Planner parity (`GET /scheduling/work-orders` — the Scheduling page's Dispatch Queue).** The
> planner list is **cross-machine**, so the per-machine rank is deliberately *not* a sort key
> there. Rows come back in the server's canonical planner order — `WorkOrder.priority`, then
> `due_date`, then `work_order_number` as a final deterministic tiebreak — and the Scheduling page
> renders that order verbatim (its client-side dispatch-score re-sort is removed, along with the
> score itself, which no longer exists anywhere in the product). Each row now also carries
> **`run_order`** — the current operation's gap-free position in its work center's **live**
> dispatch queue, computed over the full queue exactly as above so it always equals the
> kiosk/board/desktop chips; `null` when the operation is unranked or not currently queued (e.g.
> pending / on hold) — plus **`work_center_code`** for chip context on a cross-machine list. The
> page shows the rank as the same `RUN n` chip in its **Run** column: advisory context only, never
> an ordering input.
>
> **`GET /shop-floor/dispatch-board`** (Admin / Manager / Supervisor, tenant-scoped) — the whole
> board in one read. Response:
> `{"work_centers": [{"id", "code", "name", "work_center_type", "current_status", "is_active",
> "queue": [row…]}], "generated_at"}`, one column per **active** work center in code order,
> **including work centers whose queue is empty** so a manager can dispatch to an idle machine —
> **plus** any **deactivated** work center that still holds queued work, flagged
> **`is_active: false`** and merged into the same code-sorted list. A flagged column is
> **drain-only**: the client renders it read-only — its cards can be moved **off** it (the
> cross-machine move validates only the *target* is active), but it is not a drop/re-rank target,
> and the run-order `PUT` below still **404s** an inactive work center. A deactivated work center
> whose *queue* is empty emits no column — the column subquery mirrors the shared queue filter set
> (`READY`/`IN_PROGRESS` on live WOs), so a machine holding only `PENDING`/`ON_HOLD` work shows no
> column (matching the kiosk) even though that work still blocks deactivation (see Work Centers).
> Deactivation now **refuses (409)** while live work references the machine, so flagged columns
> surface pre-guard strays rather than being a normal state; the operator kiosk queue
> (`GET /shop-floor/work-center-queue/{id}`) deliberately keeps serving a deactivated work
> center's queue so a crew station bound to it can finish stranded work. `is_active` defaults
> `true` — older clients ignore it. Each row:
> `{operation_id, run_order, version, work_order_id, work_order_number, operation_number,
> operation_name, part_number, part_name, status, priority, due_date, quantity_ordered,
> quantity_complete, setup_time_hours, run_time_hours, laser_nest, material_tie}` — `version` is the
> operation's optimistic-lock counter, which the cross-machine move (`PUT /work-orders/operations/{id}`)
> requires. Rows arrive in the queue order above. **Zero-write read**: no reconcile, no audit rows,
> no events.
>
> **The zero-write guarantee covers material consumption explicitly.** The board reads material ties
> and stock levels, and it does **not** run either consumption seam
> (`apply_completion_inventory_effects` / `apply_operation_completion_inventory_effects`), post an `ISSUE`, or
> advance any `qty_consumed` — `material_tie_view.py` has no write path and must never grow one. This
> read is polled by every manager on the shop and by the kiosk queue every 10–15 seconds per station;
> a poll is not an actor, has no intent and records no reason, so material that moved from one would
> be unattributable in the audit chain. (Note the contrast with the *nest* block, which still syncs
> `nest.completed_runs` on the kiosk queue read but never on the board — see below.) If a future
> change is tempted to reconcile ties here, that is the reason not to.
>
> `laser_nest` is `null` for every non-laser row. For a laser-nest operation whose nest is live
> (**soft-deleted nests are never surfaced**, same `active_laser_nest` rule as the kiosk) it carries
> `{cnc_number, material, thickness, sheet_size, planned_runs, completed_runs, remaining_runs}` —
> a field-for-field **subset** of the kiosk queue's `laser_nest` block (the kiosk-only nest id, CNC
> file name/path and reference-PDF fields are omitted: the board sequences work, it does not open
> programs). Material, thickness and sheet size are what a planner batches nests by, so like work
> runs together and sheet/gas/nozzle changeovers are visible before the order is committed.
> `completed_runs` is the operation's completed quantity and `remaining_runs` is
> `max(0, planned − completed)` — the same numbers the kiosk shows, but derived read-only: unlike
> the kiosk payload the board never writes `nest.completed_runs` back (the same row builder serves
> the run-order `PUT`, which commits). The same block rides the column that `PUT` returns.
>
> `material_tie` is `null` for every **untied** row — the client draws nothing for those, no
> placeholder and no "not tied" nag, so an untied work order looks byte-identical to its pre-feature
> self. When the operation carries an **open, operation-scoped** tie it is
> `{allocation_id, part_id, part_number, unit_of_measure, qty_per_run, qty_planned, qty_consumed,
> qty_remaining, on_hand, short_by, pinned_inventory_item_id, pinned_lot_number}`.
> **Operation-scoped only**: a work-order-scoped tie belongs to the whole job and drains through the
> completion backflush's own tie leg, so hanging it on cards would fan one tie across every card of that work order
> and read as N separate ties. **One chip per card** — if an operation somehow carries two ties the
> **first by `allocation_id`** is sent (the same one the kiosk lists first); summing them is not an
> option, since they are different parts in different units and a merged number would be fiction.
> `qty_remaining` is `max(0, planned − consumed)`, `on_hand` is the **pinned lot's own** stock when
> the tie is pinned and the FIFO-eligible total for the part when it is not, and `short_by` is
> `max(0, remaining − on_hand)` — all three derived **server-side**, floored at the engine's own
> epsilon so float residue can't paint a false shortage. `short_by` is **advisory**: a shortage never
> blocks production, it drives the lot negative and warns. `qty_consumed` is a **cache** — the ledger
> rows carrying that `allocation_id` are the authoritative total. `qty_per_run` is carried **raw**
> (`null` means "not run-scaled" and reads as 1.0, which is not the same fact as an explicit 1).
> Ties are batched **once per response** for the whole board, so the board's cost stays flat in the
> number of cards. The same block rides the column the run-order `PUT` returns — that response
> **replaces** the column client-side, so omitting it there would blank every material chip and read
> as though reordering untied the shop's material. `material_tie` defaults to `null`, so a
> pre-feature client is unaffected by its arrival — exactly as `laser_nest` was.
>
> **`PUT /shop-floor/work-centers/{id}/run-order`** (Admin / Manager / Supervisor, tenant-scoped) —
> body `{"operation_ids": [11, 9, 14]}`, the **full** desired order for that column, rank 1 first.
> The listed ids get dense ranks `1..N` in that order; every **other** operation at the work center
> is set back to `null` (it falls to the unranked tail), so the column ends up exactly as submitted
> with no leftover drift. An empty list is valid and clears the whole column. Returns that
> work center's **refreshed column** (the `DispatchBoardColumn` shape above). Refusals — the request
> is all-or-nothing, a stale board never half-applies:
>
> | Status | Cause |
> |--------|-------|
> | **404** | Work center is inactive, or belongs to another company (indistinguishable from missing, by design) |
> | **400** | An id that is not a **live queued operation at this work center** — names the offending id and says to refresh the board |
> | **400** | A duplicate id in `operation_ids` — names the offending id |
> | **422** | More than **500** ids (`operation_ids` `max_length`; the service re-checks the same bound) |
> | **409** | Stale-write conflict — the queue changed mid-reorder; refresh and retry |
>
> **A rewrite is authoritative for the whole column, not just its live rows.** The submitted ids
> must be *live queued* operations (400 otherwise — a manager can only rank what is on the board),
> but the un-ranking half reaches **every** operation at that work center whatever its status. An
> `ON_HOLD` row that kept a stale rank would otherwise re-enter the column on resume ahead of the
> jobs the manager ranked after it. A held row therefore comes back **unranked, at the tail**, if
> the column was rewritten while it was off the queue — a hold on its own still preserves the rank.
>
> **A reorder does not bump `version`.** The ranks are written with Core `UPDATE`s that bypass the
> ORM's optimistic-lock counter, because a rank is display metadata: bumping `version` would 409 an
> operator's concurrent production post or clock-out on a job that is running right now, and would
> stale every card `version` the board just handed the client. The **409** above stays a real
> handler (it wraps the rank write *and* the commit, not just the commit) — a stale write is
> refused with "the queue changed while you were reordering", never a 500.
>
> Audited as **one** `AuditService.log_update` against the **`work_center`** resource
> (`old_values: {"run_order": [old ids…]}` → `new_values: {"run_order": [new ids…]}` — the ids that
> carried a rank in the column before, in rank order, including any off-queue ones the rewrite
> clears), not N per-operation rows: one manager action is one audit row. The audit row is written
> before the terminal commit so it lands atomically with the rank rewrite (invariant 2).

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

> **Kiosk telemetry / routing payload additions (Foundry redesign, 2026-07-23).** Read-only field
> additions feeding the redesigned kiosk's top bar, telemetry tiles, and complete modal — old
> clients ignore them, no shapes changed:
>
> - **`GET /shop-floor/work-center-queue/{id}`** — top-level **`work_center`**
>   (`{id, code, name, description, current_status}`, the kiosk top bar's machine identity;
>   `null` — never 404 — when the id is unknown or cross-tenant, and deliberately not filtered on
>   `is_active`: a deactivated work center keeps serving its queued work). Each queue row adds
>   **`part_revision`** (the part's revision letter; `null` on a part-less WO, e.g. a standalone
>   laser-nest WO) and **`last_report`** (below).
> - **`GET /shop-floor/my-active-job`** — top-level **`server_time`** (UTC ISO, on the empty
>   payload too — the kiosk clock and cycle timer run on server-corrected time, same contract as
>   the queue read). Each job dict adds **`part_revision`**; the open entry's own session counts
>   **`quantity_produced`** / **`quantity_scrapped`** (this clock-in's deltas, distinct from the
>   operation totals — feeds the AVG PER PC tile); **`last_report`**; **`downtime_minutes`**
>   (float — Σ over the operation's `WorkOrderBlocker`s of `(resolved_at or now) − reported_at`,
>   tenant-scoped; no shift math); and **`next_operation`**.
> - **`last_report`** — `{at, good, scrap} | null`: the operation's most recent production-evidence
>   report — the **deltas of that single report, not totals** — stamped by
>   `POST /operations/{id}/production` and by a quantity-carrying clock-out (backed by the new
>   nullable `work_order_operations.last_reported_at/_good/_scrapped` columns, migration `070`;
>   **correct-forward, no backfill** — `null` until the first post-migration report lands).
> - **`next_operation`** — `{operation_number, name, status, work_center: {id, code, name} | null}
>   | null`: the next routing step by `sequence` (id tiebreak) in the same WO **regardless of
>   status** — "where the job goes", not "what is startable" — `null` on the last operation. Rides
>   the my-active-job job dict **and** the `POST /operations/{id}/complete` response (the complete
>   modal's "ROUTES TO" row).

> **Material ties on operator reads (`/work-center-queue/{id}`, `/my-active-job`).** Both payloads
> gained a **`material_ties`** array so the kiosk can state what leaves inventory — it rides these
> already-authorized, already-tenant-scoped reads rather than the tie API, because
> `/work-orders/{id}/material-allocations` sits **outside the kiosk path fence** (`deps.py` allowlists
> `/api/v1/shop-floor` only, so a badge-minted `scope="kiosk"` token is 403 there). Same precedent as
> `scrap_reason_codes`: carry the data on a read the fence already permits rather than widen the
> fence. Both endpoints build it from the same batched `material_tie_view` read the dispatch board
> uses, so the manager's chip and the operator's line cannot disagree.
>
> `[]` on an untied operation — the kiosk renders nothing at all for those. Each entry:
> `{allocation_id, part_id, part_number, part_name, unit_of_measure, qty_per_run, qty_planned,
> qty_consumed, qty_remaining, on_hand, short_by, pinned_lot_number}` — the same projection the board
> sends, **plus `part_name`** and **minus `pinned_inventory_item_id`** (an operator reads a lot
> *number* off a tag and the kiosk has no verb that takes the id). Open, **operation-scoped** ties
> only. `short_by` is advisory — a shortage never blocks the job. **Pure read**: the tie read posts
> no `ISSUE`, writes no audit row and reconciles nothing, even though `_laser_nest_payload` on the
> same endpoint still syncs nest counters.
>
> **`/my-active-job` also gained `operation_quantity_scrapped`** — a **new, distinct key**, not a
> change to the existing one. Read the two carefully, they are a live footgun:
>
> | Key | Scope |
> |-----|-------|
> | `quantity_scrapped` | **THIS TIME ENTRY's** session scrap (this clock-in's delta; feeds the AVG PER PC tile) — unchanged, pre-existing |
> | `operation_quantity_scrapped` | The **OPERATION's** scrap total, alongside the existing `quantity_complete` |
>
> They are only equal on a single-session operation. The material prediction scales on
> `(complete + scrapped)` at the **operation** level — a scrapped run still ate its sheet — so a client
> that reaches for `quantity_scrapped` under-states the deduction on any job worked across two shifts.
>
> **Station disclosure note:** a station principal is an unattended, PIN-unlocked terminal with no
> operator identity, and `material_ties` adds material part numbers and **on-hand stock** to what it
> can read. Scoped to the tied parts of that work center's queued operations — it is not an inventory
> browser — but it is a genuine, if small, widening of the station's disclosure surface.

> **Kiosk doc viewer — `GET /shop-floor/operations/{operation_id}/documents` +
> `GET /shop-floor/documents/{document_id}/inline` (Foundry redesign).** The full-screen
> drawing/nest viewer's two reads. Both live under `/shop-floor` **on purpose** — badge-minted
> kiosk-scoped operator tokens are path-fenced to that prefix, so the crew station reaches them
> with zero fence changes — and both are open to **any authenticated user** with no role gate
> (operators must preview the shop drawing; mirrors the documented laser-nest inline stance).
> Pure reads: no state change, no audit rows.
>
> **Discovery** (`/operations/{id}/documents`, tenant-scoped **404** on a cross-tenant/missing
> operation):
>
> ```json
> {
>   "part": {"id", "part_number", "name", "revision"},
>   "drawing": {"document_id", "revision", "title", "status", "released_at", "file_name"},
>   "nest": {"laser_nest_id", "nest_name", "cnc_number", "document_id", "file_name"},
>   "material": "304 SS",
>   "critical_dims": [{"id", "name", "nominal", "usl", "lsl", "unit_of_measure"}]
> }
> ```
>
> `part` / `drawing` / `nest` / `material` are each `null` when absent. `drawing` is the newest
> **approved/released `DRAWING`** Document for the WO's part (`released_at DESC NULLS LAST`, then
> `id DESC`); `nest` routes through `active_laser_nest` (a soft-deleted nest never appears), and
> `material` is that nest's material. `critical_dims` are the part's **critical, active**
> `SPCCharacteristic` rows — rows scoped to this routing operation's number or unscoped
> (`operation_number` null) are preferred; when none match, all critical rows are returned rather
> than hidden.
>
> **Byte serving** (`/documents/{id}/inline`) — the **single kiosk byte-serving route** (the kiosk
> token fence blocks `/laser-nests/*` and `/documents/*`). Guard: the document must belong to the
> active company **and** be either `document_type == DRAWING` or the reference PDF of a **live**
> (non-deleted) laser nest in the same tenant. Any miss — cross-tenant, missing, or a document
> that is neither — is a uniform **404**, never a 403, so the route leaks no existence
> information. Serves exactly like `GET /laser-nests/{id}/document`: S3 stream or local file,
> `Content-Type: application/pdf`, `Content-Disposition: inline`.

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
> attached and label it without a second round-trip. Kiosk surfaces now fetch the bytes through the
> fence-safe `GET /shop-floor/documents/{document_id}/inline` instead (same serving, guarded — see
> "Kiosk doc viewer" above); the `/laser-nests/{id}/document` route remains for desktop callers.

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
> channel that produced the write: `kiosk` | `desktop` | `scanner` | `backfill` (any other value is a
> **422**). **`import` is rejected with 422 on these interactive endpoints** — it is reserved for the
> bulk-migration loaders, which write `TimeEntry` rows directly (never through these HTTP endpoints), so
> a normal request can never claim it. A **kiosk-scoped operator token** (the badge-minted crew-station
> `scope="kiosk"` token) is **authoritative and forces `source = "kiosk"`** on any of these writes
> regardless of the client hint, so a crew station can't be tricked into stamping another channel onto
> its labor. It is persisted on the time entry (`time_entries.source`, nullable; migration
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
> **Back-entry (`source = "backfill"`) is audited.** A `backfill` write is a manual, after-the-fact
> labor record (a supervisor-gated desktop back-entry / offline paper catch-up — the ShopFloor page's
> **Back-entry (offline catch-up)** toggle, gated on `work_orders:edit`), so when a `clock-in` or
> `clock-out` resolves to `backfill` it additionally writes a tamper-evident `audit_log` row
> (`time_entry` create / update, `source=backfill`) with an explicit who/when — a live capture is
> self-evidenced by its `labor_clock_in` / `labor_clock_out` `OperationalEvent`, but a back-fill needs
> its own audit trail.
>
> **Structured scrap reason on in-shift production reports (A0.3).**
> `POST /shop-floor/operations/{id}/production` accepts a `scrap_reason` string — the
> same shape and destination as the clock-out field (the `TimeEntry.scrap_reason` column,
> 255 max), persisted onto the caller's **active** time entry. It is stored only when the report
> actually carries scrap (`quantity_scrapped_delta > 0`); an omitted/`null` reason never clobbers a
> reason recorded by an earlier in-shift report. When stored, the reason is also appended to the
> tamper-evident `REPORT_OPERATION_PRODUCTION` audit description.
>
> **Over-count correction — `POST /shop-floor/operations/{id}/reduce-production` (operator self-service).**
> The inverse of `/operations/{id}/production`: it lets a shop-floor operator **walk back good-count
> quantity they accidentally OVER-reported** on an operation they are **actively working**, **before**
> it is complete. It is a **miscount correction, not a scrap move** — it never touches scrap fields,
> never changes status, and the operation / work order stay in progress. Open to **any authenticated
> user** (`get_current_user`); it works under a **kiosk-scoped badge token** (the path is under
> `/shop-floor`, in-fence). Body (`ProductionReductionRequest`):
> - `quantity_delta` (**required**, `> 0`, finite) — the good-count quantity to REMOVE.
> - `reason` (**required**, non-blank, ≤ 255) — a **correction** reason for the audit trail (e.g.
>   `"double-scanned the tray"`); this is **not** a scrap reason (no scrap is recorded here).
> - `source` (optional adoption-telemetry channel) — `kiosk | desktop | scanner | backfill`; `import`
>   is rejected **422** (loader-reserved) and a kiosk-scoped operator token forces `kiosk`, exactly like
>   the other labor writes (see "Adoption-telemetry `source` channel" above).
> - `notes` (optional) — appended to the caller's **open** (active) time entry.
>
> **Guardrails (the contract):** the walk-back is bounded to the **caller's OWN UNAPPROVED labor on
> this operation** — their open clock-in first, then their own **closed unapproved sessions
> newest-first** (the real-world "noticed after check-out" case); the allowed maximum is the **sum of
> `quantity_produced` across those entries**. **Approval — not clock-out — is the immutability
> boundary (G5-A):** APPROVED entries are excluded from the allowance, so a signed-off count can
> never be walked back here — approved labor needs a supervisor (the office twin at
> `POST /work-orders/operations/{id}/reduce-production`, after unapproving). The caller must still
> hold an **open clock-in** on the operation ("actively working" is the product framing — the
> correction UI is only reachable clocked-in). The reduction walks the delta down those entries'
> `quantity_produced` (the durable evidence — no entry ever goes below zero; crew-safe — an operator
> can never touch another operator's count) and lowers the operation's `quantity_complete` by the
> same delta (decrementing the operation's `quantity_reworked` by the portion walked off **REWORK**
> entries), then **recomputes** the work order's `quantity_complete` from its operations — the max
> over non-component operations (capped at the WO target), only ever lowered — so a multi-operation
> WO whose finished count is held by a different operation is never pulled below it (reducing a
> non-defining or a component operation leaves the WO total unchanged). On a **laser dispatch-pool
> WO** the recomputed rollup is instead the **sum** of per-nest progress (each nest capped at its
> own planned runs, the sum capped at the WO total), so lowering one nest lowers the pool header by
> the same delta — see "Pool WO header progress" under Laser Nests. Lowering the operation total
> together with its **backing evidence** is what makes the correction **reconcile-safe**: produced
> quantity is monotonic-up and re-derived from time-entry evidence on every WO read, so lowering the
> operation total alone would be re-raised on the next read — lowering the backing evidence with it
> is what makes it stick. It is **tenant-scoped**, **row-locked** (operation then
> WO, `SELECT … FOR UPDATE`, same order as the completion paths), **optimistic-locked**, and writes a
> **tamper-evident `audit_log` row** (action `reduce_operation_production`, old→new `quantity_complete`
> plus the operator-supplied reason and the walked entries' before/after produced quantity — summed in
> the diff, per-entry in `extra_data`). It also
> emits an `operation_production_reduced` operational event and the shop-floor / work-order / dashboard
> real-time broadcasts. Error codes:
> - **404** — operation missing / cross-tenant, or its work order not found.
> - **409** — the operation is **COMPLETE**: `"Completed work can't be corrected here -- ask a
>   supervisor"` — **post-completion corrections are an office/supervisor task by design**, and that
>   referral is now honest: the office twin
>   (`POST /work-orders/operations/{id}/reduce-production`) **accepts** a COMPLETE operation, where
>   it used to hit this identical refusal. This operator verb keeps it: correcting finished work is a
>   supervised act.
> - **409** — the work order is **terminal** (COMPLETE / CLOSED / CANCELLED): `"This work order is
>   complete, closed or cancelled -- its recorded production can no longer be corrected"`. A separate
>   message from the one above, on **both** verbs, because no supervisor can correct it either. A
>   concurrent stale edit also returns **409**
>   (`"This operation was modified concurrently. Refresh and retry."`).
> - **400** — no open clock-in of the caller on this operation
>   (`"You must be clocked in to this operation to correct its count"`), or `quantity_delta` greater
>   than the caller's own **unapproved** evidence on the operation. The message says why the
>   allowance is short: when some of the caller's evidence is already approved,
>   `"You can only remove up to the N piece(s) you recorded on this operation that are not yet
>   approved; approved labor needs a supervisor."`; otherwise `"You can only remove up to the N
>   piece(s) you recorded on this operation; ask a supervisor to correct more."`. (The approved-labor
>   refusal is this 400 allowance message — there is no longer a dedicated 409 for an approved
>   entry.)
> - **422** — schema validation (`quantity_delta` ≤ 0 / non-finite, `reason` blank or > 255, or a
>   loader-reserved `source`). See the "Over-count correction Schema" below.
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
> **Scrap → NCR on the production report (Foundry redesign).**
> `POST /shop-floor/operations/{id}/production` (`ProductionReportRequest`) also accepts
> **`open_ncr: bool = false`** and **`ncr_description`** (optional, ≤ 2000): when `open_ncr` is
> true and the report carries scrap, the endpoint files a **`NonConformanceReport`
> (`source=IN_PROCESS`)** for that scrap **in the same transaction** as the production write —
> `quantity_affected` = the report's scrap delta, part/WO/lot from the work order,
> `detected_by` = the caller, description = `ncr_description` or a generated line quoting the
> scrap reason. Deliberately **no hold and no blocker** — the machine keeps running; Quality is
> notified through the NCR itself plus a high-severity `ncr_created` operational event
> (deliberate contrast with the process-step OOT quality-hold, which does hold the job). The NCR
> create is audited (`log_create`, hash-chained). Response gains
> **`ncr: {id, ncr_number} | null`** — the kiosk success toast quotes the real `ncr_number`;
> `null` whenever no NCR was requested. `open_ncr` with `quantity_scrapped_delta <= 0` is a
> **400** before any mutation (an NCR documents scrap; a scrapless one is a client bug). The
> scrap-reason-required **422** above is unchanged and evaluated first.
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

#### Over-count correction Schema

`POST /shop-floor/operations/{operation_id}/reduce-production` body (`ProductionReductionRequest` —
the **same body** is shared by the office twin
`POST /work-orders/operations/{operation_id}/reduce-production`) — walk back an over-reported good
count on the caller's own **unapproved** labor (open clock-in first, then their own earlier
unapproved sessions; **not** scrap; see "Over-count correction" under the shop-floor notes above):

```json
{
  "quantity_delta": 3,
  "reason": "double-scanned the tray",
  "source": "kiosk",
  "notes": "recount was 47, not 50"
}
```

Response **200** (`operation` stays in progress; `active_time_entry.clock_out` is `null` for the
caller's still-open entry; `reduced_time_entries[]` is the per-entry paper trail of the walk, in
walk order — open entry first, then the caller's closed unapproved sessions newest-first):

```json
{
  "message": "Production quantity corrected",
  "operation": {
    "id": 812,
    "status": "in_progress",
    "quantity_complete": 47,
    "quantity_scrapped": 2,
    "quantity_ordered": 50
  },
  "active_time_entry": {
    "id": 4471,
    "quantity_produced": 0,
    "quantity_scrapped": 2,
    "clock_out": null
  },
  "reduced_time_entries": [
    {
      "time_entry_id": 4471,
      "entry_type": "run",
      "quantity_produced_before": 1,
      "quantity_produced_after": 0
    },
    {
      "time_entry_id": 4460,
      "entry_type": "run",
      "quantity_produced_before": 12,
      "quantity_produced_after": 10
    }
  ]
}
```

(The example walks a delta of 3: the open clock-in's 1 piece is exhausted first, then 2 come off
the caller's most recent closed unapproved session.)

> `quantity_delta` must be `> 0`; `reason` is **required** and non-blank (a correction reason, not a
> scrap reason). The delta may not exceed the caller's own **unapproved** recorded evidence on the
> operation (**400** — approved labor is excluded and needs a supervisor), and the endpoint refuses
> with **409** once the operation/WO is complete. See the full guardrail / error-code contract in
> "Over-count correction" under the shop-floor notes above.
>
> The **office twin** `POST /work-orders/operations/{operation_id}/reduce-production` (Admin /
> Manager / Supervisor; see "Over-count correction … (supervisor/office)" under Work Orders) returns
> the same shape except `active_time_entry` is replaced by
> `work_order {"id", "quantity_complete"}` (the recomputed rollup) — there is no caller clock-in on
> that path — and its `reduced_time_entries[]` may span **multiple operators'** unapproved entries.

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
| DELETE | `/quality/ncr/{ncr_id}` | **Void** an NCR (soft-delete + status → `void`); body `{ "reason": "<non-blank>" }`. Guarded, see note | Admin / Manager / Quality |
| POST | `/quality/ncr/{ncr_id}/restore` | Restore a voided NCR — reopens it to `open` | Admin / Manager / Quality |

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

> **NCR void + restore (`NonConformanceReport` now `SoftDeleteMixin`).** Voiding is the quality-record
> form of a soft delete: `DELETE /quality/ncr/{ncr_id}` marks the NCR `is_deleted` **and** moves it to
> `VOID` status (the status already existed), retaining it for AS9100D traceability and the
> tamper-evident audit trail while dropping it from all live reads (which now filter
> `is_deleted == false`). The request **requires a non-blank JSON body `{ "reason": "..." }`**
> (whitespace-only → **422**); gate **Admin / Manager / Quality**. **Guardrail:** the void is **refused
> with 400** while the NCR still **actively gates a work order** — an `OPEN`/`ACKNOWLEDGED`
> `WorkOrderBlocker` references it — resolve the blocker first; a re-void returns **400** ("already
> voided"). `POST /quality/ncr/{ncr_id}/restore` clears the soft-delete and reopens the NCR to
> **`OPEN`** (the pre-void status is not preserved — a safe reset). Both actions are **fully audited**:
> the void writes a `log_status_change` (→ `void`, reason in the description) **and** a `log_delete`
> (`soft_delete=true`, reason in `extra_data`); restore writes a `log_update` (`action="restore"`).
> This closes a prior gap where the ordinary `PUT /quality/ncr/{ncr_id}` update path emitted only an
> operational event and **no** `audit_log` row.

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
| DELETE | `/purchasing/vendors/{vendor_id}` | Soft-delete a vendor (also sets `is_active=false`) — guarded, see note below | Admin / Manager |
| POST | `/purchasing/vendors/{vendor_id}/restore` | Restore a soft-deleted vendor (re-activates it) | Admin / Manager |
| GET | `/purchasing/purchase-orders` | List purchase orders (filters: `status`, `vendor_id`). Bounded: `limit` **1–5000, default 5000**, `offset` ≥ 0 | Yes |
| POST | `/purchasing/purchase-orders` | Create purchase order with its lines | Admin / Manager / Supervisor |
| GET | `/purchasing/purchase-orders/{po_id}` | Get PO by ID | Yes |
| PUT | `/purchasing/purchase-orders/{po_id}` | Update purchase order | Admin / Manager / Supervisor |
| POST | `/purchasing/purchase-orders/{po_id}/send` | Issue a PO to the vendor — status → `sent`, stamps `order_date`; only `draft`/`approved` POs (else **400**) | Admin / Manager |
| POST | `/purchasing/purchase-orders/{po_id}/lines` | Add a line to a `draft` PO (else **400**) and roll the PO subtotal/total | Admin / Manager / Supervisor |
| DELETE | `/purchasing/purchase-orders/{po_id}` | Soft-delete a purchase order — guarded, see note below | Admin / Manager |
| POST | `/purchasing/purchase-orders/{po_id}/restore` | Restore a soft-deleted purchase order | Admin / Manager |

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
> **Blank dates are treated as omitted (create and update payloads).** On
> `POST /purchasing/purchase-orders` (and its line payloads / `POST .../lines`), an empty or
> whitespace-only string in the PO's `required_date` / `expected_date` or a line's
> `required_date` is coerced to `null` before validation instead of failing with **422** —
> HTML date inputs submit `""` when left blank. Explicit dates are validated as before
> (`expected_date` must be after `required_date` when both are set).
> `PUT /purchasing/purchase-orders/{po_id}` (`POUpdate`) applies the same coercion to its
> `required_date` / `expected_date`.
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
>
> **Vendor / PO soft-delete + restore (`Vendor`, `PurchaseOrder` now `SoftDeleteMixin`).** Both
> `DELETE` endpoints are **soft** deletes (compliance invariant #3 — never a physical `DELETE`):
> the row is marked `is_deleted` / `deleted_at` / `deleted_by`, disappears from all list/detail reads
> (which now filter `is_deleted == false`), and is restorable via the paired `POST .../restore`. Both
> the delete and the restore write a tamper-evident `audit_log` row (`log_delete` with
> `soft_delete=true` on delete; `log_update` with `action="restore"` on restore), flushed before the
> terminal commit so it commits atomically with the change. Guardrails:
> - **Vendor delete** additionally sets `is_active=false`, and is **refused with 400** while the vendor
>   still has any **active** (not `closed`/`cancelled`, not soft-deleted) purchase order — close or
>   cancel those first (the 400 names the count). A double delete returns **400** ("already deleted");
>   restore re-sets `is_active=true`.
> - **PO delete** is **refused with 400** when any line has received material
>   (`quantity_received > 0`) — *"Void the receipt(s) first, then delete."* (see Receiving → void
>   below) — so voided receipts / inventory can't be stranded behind a deleted PO. A double delete
>   returns **400** ("already deleted").
> - **Creating a PO against a soft-deleted or inactive vendor is refused** (**404** "Vendor not found"):
>   `POST /purchasing/purchase-orders` now resolves the vendor with `is_deleted == false` **and**
>   `is_active == true`.

### Supplier Scorecards, Audits & Approved Supplier List

`app/api/endpoints/supplier_scorecards.py`, mounted at `/supplier-scorecards`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/supplier-scorecards/supplier-scorecards/dashboard` | Avg score, below-threshold, audits/reviews due, top & worst performer | Yes |
| GET | `/supplier-scorecards/supplier-scorecards/ranking` | Vendors ranked by latest overall score | Yes |
| GET | `/supplier-scorecards/supplier-scorecards/vendor/{vendor_id}/history` | One vendor's scorecards over time | Yes |
| GET | `/supplier-scorecards/supplier-scorecards/` | List scorecards (`skip`/`limit` 1…5000) | Yes |
| GET | `/supplier-scorecards/supplier-scorecards/{id}` | Scorecard detail | Yes |
| POST | `/supplier-scorecards/supplier-scorecards/` | Create scorecard | Admin / Manager |
| PUT | `/supplier-scorecards/supplier-scorecards/{id}` | Update scorecard | Admin / Manager |
| POST | `/supplier-scorecards/supplier-scorecards/calculate/{vendor_id}` | Auto-calculate from PO / receipt / NCR data | Admin / Manager |
| GET | `/supplier-scorecards/supplier-audits/due-soon` | Audits due within `days` (1…365, default 30) | Yes |
| GET | `/supplier-scorecards/supplier-audits/` | List supplier audits | Yes |
| POST | `/supplier-scorecards/supplier-audits/` | Create supplier audit | Admin / Manager |
| PUT | `/supplier-scorecards/supplier-audits/{id}` | Update supplier audit | Admin / Manager |
| GET | `/supplier-scorecards/approved-suppliers/` | List ASL entries | Yes |
| GET | `/supplier-scorecards/approved-suppliers/{id}` | ASL entry detail | Yes |
| POST | `/supplier-scorecards/approved-suppliers/` | Create ASL entry | Admin / Manager |
| PUT | `/supplier-scorecards/approved-suppliers/{id}` | Update ASL entry | Admin / Manager |

> ⚠️ **`POST .../calculate/{vendor_id}` was returning 500 in production, for two reasons.** The
> `SupplierScorecard` insert omitted `TenantMixin`'s NOT NULL `company_id` — but the handler never
> reached it, because `calculate_overall()` runs on the in-memory object *before* the flush, where
> the four weight **column** defaults are still `None` (`float * None`). `POST
> /supplier-scorecards/` never hit that, because `ScorecardCreate` carries the same values as
> **schema** defaults. Both are fixed, and the weights used in the arithmetic are now stated
> explicitly so they are the weights that get stored.
>
> **`PUT .../supplier-scorecards/{id}` was the second call site of the same crash.** Every numeric
> field on `ScorecardUpdate` is `Optional`, and `model_dump(exclude_unset=True)` **keeps** an
> explicitly-sent `null`, so `{"quality_weight": null}` reached `calculate_overall` and 500'd. An
> explicit `null` on any numeric field is now refused at parse time with a **422** naming the field;
> *omitting* the field is unchanged (leaves the stored value), and nullable text fields
> (`notes`, `action_items`) may still be nulled.

> **Fifteen of sixteen handlers were reaching outside the caller's tenant.** Three of them were
> **cross-tenant writes** — `PUT` on scorecard, supplier audit and ASL entry each resolved their row
> by bare id (the scorecard handler even took a `company_id` dependency and never used it), so
> guessing an integer was enough to rewrite another company's AS9100D supplier evaluation, fail
> their audit, or set their approved supplier to `removed`. The reads (dashboard, ranking, both
> lists, due-soon, both detail reads) took no company argument at all, and the three creates stamped
> `company_id` correctly but resolved `vendor_id` with no predicate — and every serializer renders
> `vendor.name` / `vendor.code` straight back, so a create doubled as a read of the foreign
> supplier. All are scoped; a foreign `vendor_id`, scorecard, audit or ASL id answers a flat **404**
> with the same detail a genuinely missing id gets.
>
> **Expect the dashboard and ranking numbers to drop on a multi-company install** — they were
> summing every tenant.

> **`vendor_name` / `vendor_code` read as `null` for a legacy cross-tenant row.** Same shape as
> maintenance above: the create guards stop new mis-tenanted rows but do nothing about rows written
> before them, and the `vendor` relationship (plus the `joinedload` on it) carries no predicate.
> The serializers now null both fields when the vendor's `company_id` differs; `vendor_id` stays
> visible so the row can be corrected. Audit-row identifiers fall back to `vendor #{id}` in the same
> case, so recording an update can never become the thing that discloses the foreign supplier's
> code. See the pre-deploy detection SQL in `docs/RBAC_PERMISSIONS.md` → Supplier Scorecards.

> **The ASL duplicate check stays install-wide on purpose.** `ApprovedSupplierList.vendor_id`
> carries a *global* unique constraint, so the check must mirror the constraint exactly or a miss
> becomes an `IntegrityError` 500 instead of the **400** "Vendor already has an ASL entry". It is
> unreachable across tenants now that the vendor is scoped first — a vendor belongs to exactly one
> company. ⚠️ One consequence worth knowing: a **legacy** ASL row owned by company B against
> company A's vendor permanently consumes that vendor's single global slot, and company A can
> neither see nor edit it. The detection SQL above finds these; correcting them is a data task.

> **Every state change writes a tamper-evident `audit_log` row** (invariant 2 — the router
> previously wrote none at all). Resource types: `supplier_scorecard` (`CREATE` on both the manual
> and the auto-calculate path — the latter carries `period_start` / `period_end` / `auto: true` in
> `extra_data` — and `UPDATE`), `supplier_audit` (`CREATE` / `UPDATE`) and `approved_supplier`
> (`CREATE` / `UPDATE`). Rows are logged **before** the terminal commit so they commit atomically
> with the change.

> ⚠️ **Known frontend gap, not fixed here:** `apiClient.calculateSupplierScorecard` (`api.ts`) posts
> `calculate/{vendorId}` with **no body** while the handler requires `period_start` / `period_end`,
> so any UI call would 422 before reaching the handler — and nothing in `SupplierScorecards.tsx`
> calls it today.

### PO Upload (AI document extraction)

Upload a vendor PO or quote document, AI-extract its data for human review, then create the PO
from the reviewed result (`app/api/endpoints/po_upload.py`, mounted at `/po-upload`). Extraction
runs through the shared `run_llm_task` pipeline (prompt `po_extraction` 1.0.0,
`feature="po_upload"`, one tenant-scoped `ai_usage_events` row per call — telemetry, not audit)
and is covered by the per-company `allow_ai_egress` kill switch (see **Company (self-service)**
below).

All endpoints are stateless and **per-document** — there is no batch endpoint. The Upload PO
page's multi-file batch mode is pure client-side orchestration: one `upload-po` / `upload-quote`
call per file (at most 2 concurrent) and one `create-from-upload` call per reviewed document.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/po-upload/upload-po` | Upload a PO document (`.pdf`/`.doc`/`.docx`, 10 MB cap; else **400**) — AI-extracts data for review before commit | Yes |
| POST | `/po-upload/upload-quote` | Upload a vendor quote document — AI-extracts data to build a PO | Yes |
| POST | `/po-upload/upload-invoice` | Legacy alias of `upload-quote` (same extraction behavior) | Yes |
| POST | `/po-upload/create-from-upload` | Create the PO from the reviewed extraction — can create the vendor and missing parts. Part-number matching is **case-insensitive** and ignores surrounding whitespace: the same number repeated across lines / `create_parts` creates the part **once** (stored as the first occurrence's stripped form) and attaches every matching line to it; an active part already holding the number is reused, and a line with no `part_id` resolves by part number to an existing active part even when it isn't in `create_parts`. **400** if `line_items` is empty, the PO number already exists, a supplied `vendor_id` / line `part_id` doesn't exist in the active company **or is soft-deleted**, a line's part number matches no active part and isn't in `create_parts`, or a new part's number belongs to a **soft-deleted** part (restore it via `POST /parts/{id}/restore` or use a different number) | Admin / Manager / Supervisor |
| GET | `/po-upload/pdf/{path}` | Serve the uploaded source document for preview (`s3://` refs and local paths) | Yes |
| GET | `/po-upload/search-parts` | Part typeahead for extraction-review matching (`q`, `limit` **1–50**, default 10) | Yes |
| GET | `/po-upload/search-vendors` | Vendor typeahead for extraction-review matching (`q`, `limit` **1–50**, default 10) | Yes |

### Receiving & Inspection

Canonical material-receiving and incoming-inspection endpoints, all under `/receiving`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/receiving/open-pos` | List POs available for receiving (sent/partial); each PO carries `order_date` / `required_date` / `expected_date` plus its open lines | Yes |
| GET | `/receiving/po/{po_id}` | Get full PO detail for receiving | Yes |
| POST | `/receiving/receive` | Receive material against a PO line (`lot_number` optional — auto-assigned when blank, see below) | Admin / Manager / Supervisor |
| GET | `/receiving/inspection-queue` | List receipts pending inspection (`days_back` optional, bounded 1–3650; **no date cutoff by default** — pending receipts never age out, so the list matches the `/stats` `pending_inspection` count) | Yes |
| GET | `/receiving/receipt/{receipt_id}` | Get receipt detail | Yes |
| PATCH | `/receiving/receipt/{receipt_id}` | Correct a mis-keyed receipt in place (new total `quantity_received` + optional traceability fields; required `reason`) — reconciles PO line / PO status / inventory. Guarded, see note | Admin / Manager / Supervisor |
| POST | `/receiving/receipt/{receipt_id}/void` | Void (soft-delete) a receipt with full reversal of PO line / status / inventory; required `reason`. Terminal — no restore. Guarded, see note | Admin / Manager |
| POST | `/receiving/inspect/{receipt_id}` | Complete inspection (accept/reject, auto-NCR on rejection) | Admin / Manager / Quality / Supervisor |
| GET | `/receiving/history` | Receiving history with inspection results (`days` **1–365**, default 30) | Yes |
| GET | `/receiving/stats` | Receiving statistics for dashboard (`days` **1–365**, default 30) | Yes |
| GET | `/receiving/locations` | Receivable inventory locations | Yes |
| POST | `/receiving/receipt/{receipt_id}/print-label` | Manually (re)print the 4×6 thermal receiving label | Admin / Manager / Supervisor |
| GET | `/receiving/print-profile` | Get the company ProxyBox print profile (key masked; **404** until created) | Admin |
| PUT | `/receiving/print-profile` | Create / update the print profile, incl. the `allow_print_egress` kill switch | Admin |

> **Lot number (`lot_number`).** Optional on `POST /receiving/receive` (max 50 chars): when
> blank or omitted the server auto-assigns the receipt number (unique, company-scoped) as the
> lot, so `POReceipt.lot_number` is still always stored non-null and AS9100D lot traceability
> is preserved — supply the vendor's lot number when it is known. The receipt audit row and
> the inventory RECEIVE transaction both carry the effective (supplied or auto-assigned) lot.
>
> **Incoming-inspection default (`requires_inspection`).** On `POST /receiving/receive` the
> field defaults to **`false`** when omitted: the receipt is auto-accepted (dock-to-stock) and
> lands directly in inventory with **`inspection_status = not_required`**. No incoming
> inspection is performed on this path, so `inspection_method`, `inspected_by`, and
> `inspected_at` stay **null** — the record must not assert an inspection that never happened
> (AS9100D records integrity); the receiver and receipt time are still captured by `received_by`
> / `received_at`. Pass `true` to hold the lot in the inspection queue until
> `POST /receiving/inspect/{receipt_id}`, where it resolves to `passed` / `failed` / `partial`
> with a real inspector, method, and timestamp. The part master's `Part.requires_inspection`
> flag is **not** applied automatically — it is exposed on the `/receiving/open-pos` and
> `/receiving/po/{po_id}` line payloads, and the Receiving UI renders it as an amber advisory
> hint next to its "Requires Inspection" checkbox (which always starts unchecked) so the
> receiver can opt in deliberately.
>
> A receipt's **`inspection_status`** (returned by `/receiving/history` and
> `/receiving/receipt/{receipt_id}`) is one of: **`not_required`** (dock-to-stock — accepted
> without inspection; no inspector/method/time), `pending` (awaiting inspection in the queue),
> `passed`, `failed`, or `partial` (recorded by `/receiving/inspect/{receipt_id}`). The History
> view badges `not_required` as a neutral **"Not Required"**, visually distinct from a green
> **"Passed"** (which means a real incoming inspection passed). Vendor acceptance-rate analytics
> count `not_required` as **accepted** (dock-to-stock material enters stock without rejection),
> so acceptance rates are unaffected. Receipts auto-accepted before this change keep their
> prior values (they may still read `passed` / `visual`) — historical quality records are
> corrected forward, not rewritten.

> **Correcting or voiding a receipt (`POReceipt` now `SoftDeleteMixin`).** A mis-keyed receipt is
> fixed with **`PATCH /receiving/receipt/{receipt_id}`** (correct in place) or reversed entirely with
> **`POST /receiving/receipt/{receipt_id}/void`** (soft-delete). Both **require a non-blank `reason`**
> (recorded on the tamper-evident `audit_log`) and are **fully audited**; live reads now filter
> `is_deleted == false`.
>
> - **Correct** — body: `quantity_received` (the **new TOTAL** received, **> 0** — not a delta) plus
>   optional `lot_number` / `heat_number` / `cert_number` / `serial_numbers` / `notes`, and the
>   required `reason`. Gate **Admin / Manager / Supervisor** (the receive-tier — the same roles that
>   `POST /receiving/receive` and post inventory adjustments). Response: the updated `ReceiptResponse`.
> - **Void** — body: `reason` only. Gate **Admin / Manager** (tighter — void is delete authority).
>   **Terminal: there is no restore** — to redo, re-receive.
>
> **What gets reconciled (both paths).** All refusal guards run **before any mutation**, so a refusal
> never leaves the receipt half-reconciled:
> - **PO line** — `quantity_received` is rolled to match (a void drops it to 0); the line's `is_closed`
>   is recomputed, so a void can **reopen** a previously-closed line.
> - **PO status** — recomputed from all lines: `received` (all closed) → `partial` (any received) →
>   back to **`sent`** when nothing is left received (the pre-receipt open state). A status move is
>   itself audited (`log_status_change`).
> - **Inventory (dock-to-stock receipts only)** — a **signed compensating `InventoryTransaction`
>   `ADJUST`** is appended (`reason_code` `RECEIPT_CORRECTION` / `RECEIPT_VOID`) to move on-hand by the
>   delta. **AS9100D records integrity: the historical `RECEIVE` transaction is never mutated or
>   deleted** — reversal is always a new, signed compensating row (like a manual inventory adjustment).
>   `PENDING_INSPECTION` receipts placed no stock, so nothing is adjusted there.
>
> **State model — allowed only before inspection / while unconsumed.** A receipt is correctable/voidable
> only while it is **`pending`** (awaiting inspection) or **`not_required`** (dock-to-stock accepted).
> Refusals (all with actionable `detail`):
> - already **inspected** (`passed` / `failed` / `partial`) → **409** — handle via NCR / inventory
>   adjustment, not here.
> - **lot change after stock was placed** (dock-to-stock) → **400** — void and re-receive instead.
> - received stock for the lot **already allocated or consumed** (would drive `quantity_available`
>   negative) → **409** — make an inventory adjustment instead.
> - the reversal would drive the PO line's received total **negative** → **409**.
> - the receipt's PO line no longer exists (orphaned) → **400** (never a 500).
> - a cross-tenant / missing / already-voided receipt → **404**.

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
| GET | `/inventory/` | List inventory items (`part_id`, `warehouse`, `location_code`, `has_quantity` — `has_quantity` filters **nonzero**, not positive, so driven-negative lots stay visible; see note below). Bounded: `limit` **1–10000, default 10000**, `offset` ≥ 0 | Yes |
| GET | `/inventory/summary` | On-hand summary by part, with per-location breakdown (every active row with **nonzero** on-hand, negatives included). Bounded with the identical `limit`/`offset` as `/inventory/` — see note below | Yes |
| GET | `/inventory/low-stock` | Parts at/below reorder point (on-hand summed per part) | Yes |
| GET | `/inventory/locations` | List warehouse locations / bins | Yes |
| POST | `/inventory/locations` | Create a location | Admin / Manager |
| GET | `/inventory/transactions` | Inventory transaction (ledger) history, newest first | Yes |
| POST | `/inventory/receive` | Receive inventory into stock | Admin / Manager / Supervisor |
| POST | `/inventory/issue` | Issue inventory manually (**deprecated** — see below; **400** if `work_order_number` is sent) | Admin / Manager / Supervisor |
| POST | `/inventory/transfer` | Transfer inventory between locations | Admin / Manager / Supervisor |
| POST | `/inventory/adjust` | Adjust inventory | Admin / Manager / Supervisor |
| GET | `/inventory/cycle-counts` | List cycle counts (optional `status`) | Yes |
| POST | `/inventory/cycle-counts` | Create a cycle count (enrolls matching stock rows — every active row with **nonzero** on-hand, negatives included) | Admin / Manager / Supervisor |
| POST | `/inventory/cycle-counts/{count_id}/start` | Open a count for counting | All roles except Viewer |
| POST | `/inventory/cycle-counts/{count_id}/items/{item_id}/count` | Record a counted quantity | All roles except Viewer |
| POST | `/inventory/cycle-counts/{count_id}/complete` | Complete the count (optionally apply adjustments) | Admin / Manager / Supervisor |

> **There is no `GET /inventory/{part_id}`.** An earlier revision of this doc listed one; no such
> route exists in `app/api/endpoints/inventory.py`. Use `GET /inventory/?part_id=<id>` for that
> part's stock rows, or `GET /inventory/summary` for the per-part rollup with locations.

> **Movement quantities are strictly positive — direction is the verb, never the sign (422).**
> `quantity` on `/receive`, `/issue`, and `/transfer` is validated `> 0` at the schema
> (`Field(gt=0)`), so a zero or negative value is rejected **422** before any handler code runs. A
> negative issue would have **minted** stock while writing a positive-quantity ISSUE ledger row with
> a negative `total_cost`; a negative receive would remove stock; a negative transfer would move
> dest→source against locations/lots the response never named. `/adjust`'s `new_quantity` is `>= 0`
> (zero is a legitimate write-off, but a manual adjustment may not **dictate** a negative on-hand —
> only the shortage engine drives a lot negative, and the manual remedy is adjusting it back up), and
> a cycle count's `counted_quantity` is `>= 0` (nothing on the shelf is a real observation; a
> negative count is not).

> **Driven-negative lots are visible, listable, exportable, and countable.** The consumption
> engine's shortage posture deliberately drives a lot **negative** rather than fail a completion, so
> a negative row is a discrepancy someone has to see and fix. `GET /inventory/?has_quantity=true`
> and `GET /exports/inventory?has_quantity=true` therefore filter `quantity_on_hand != 0` (not
> `> 0` — the old predicate made driven-negative lots invisible to the one list view and to the
> spreadsheet a manager reconciles from), `GET /inventory/summary` likewise rolls up every active
> row with nonzero on-hand, and `POST /inventory/cycle-counts` enrolls every active
> row with **nonzero** on-hand — a driven-negative lot is exactly the row a cycle count exists to
> reconcile, and enrolling only positive rows made it permanently uncountable.

> **Lot-less rows merge instead of fragmenting.** The existing-row lookup shared by `/receive` and
> `/transfer` matches a `NULL` lot with `IS NULL` (the naive `lot_number == None` comparison
> compiles to `lot_number = NULL`, which never matches in SQL), so a lot-less receive or transfer
> now increments the existing lot-less row at that (part, location) instead of minting a brand-new
> fragment row each time. A legacy fragmented set resolves deterministically to its oldest row.

> **`/receive` and `/transfer` are role-gated (Admin / Manager / Supervisor).** Both previously
> depended on `get_current_user` only, so any authenticated tenant user — Viewer included — could
> create stock and write a ledger row. They now carry `require_role(STOCK_MUTATOR_ROLES)` =
> `[ADMIN, MANAGER, SUPERVISOR]`, matching the sibling stock mutators `/inventory/issue` and
> `/inventory/adjust` and the PO-receipt path `POST /receiving/receive`, which writes the same
> `inventory_items` / `inventory_transactions` tables. See `docs/RBAC_PERMISSIONS.md` → Inventory.

> **`POST /inventory/receive` refuses a soft-deleted part with 400.** The part lookup resolved on
> `(id, company_id)` with no `is_deleted` predicate, so a Manager could create brand-new stock *and*
> a ledger row against a part the business had deleted. It now returns **400** —
> *"Part '&lt;number&gt;' is deleted - restore it or use a different part number"* — the same
> deleted-part policy as the BOM and PO-upload import paths. An id that matches no part at all is
> still **404 "Part not found"**. `GET /inventory/low-stock` carries the same predicate (a deleted
> part must not raise a purchasing signal); it was incidentally covered before only because deleting
> a part also clears `is_active`.

> **`POST /inventory/issue` is role-gated, deprecated — and refuses work-order attribution (400).**
> It requires **Admin / Manager / Supervisor** (`require_role`), matching the sibling stock-mutating
> `/inventory/adjust`; it was previously open to any authenticated user, so any operator could
> issue stock off a lot. The route carries FastAPI's `deprecated=True`, so it renders struck
> through at `/docs` and the generated OpenAPI operation has `"deprecated": true`. A request that
> sends **`work_order_number`** is now rejected **400**: the only shape this endpoint could record it
> as is `reference_type='work_order'` + `reference_number` with a NULL `reference_id`, which is
> invisible to `work_order_ledger_filter` — i.e. to job costing, lot genealogy, analytics, and the
> backflush suppression nets. A movement the work-order record cannot see is worse than no
> attribution at all. The ledger row this endpoint writes therefore always carries
> `reference_type`/`reference_number` NULL. The supported paths it defers to: tie the material to
> the work order (`…/material-allocations` — consumption posts automatically through the completion
> flows), or `POST /inventory/adjust` for a manual stock correction. (An earlier revision said a
> work-order-scoped `POST /work-orders/{id}/issue-material` replacement was planned; material ties
> are that replacement.)

> **Transaction-history query params (`GET /inventory/transactions`).** All filters are optional
> and combine with AND: `part_id`, `transaction_type`, `reference_type`, `reference_id`,
> `lot_number`, `start_date` / `end_date` (ISO-8601 datetimes, matched inclusively against
> `created_at`), and `work_order_id`. Only `part_id` and `transaction_type` existed before this
> pass; the rest are new.
>
> **`work_order_id` matches all four ledger shapes a work order's movement takes**: the
> work-order-referencing rows (`reference_type='work_order'`, `reference_id=<id>` — the finished-good
> receipt, plus every **legacy** pre-PR-4.4 component `ISSUE`), the **reconciled component** rows
> (**`reference_type='work_order_backflush'`**, `reference_id=<id>` — BOM/routing backflush demand and
> work-order-scoped tie consumption, added in PR 4.4 and able to appear as **several rows per (work
> order, part)** when a draw spills across lots), the **operation-scoped**
> consumption rows (`reference_type='work_order_operation'`, `reference_id=<operation id>` — per-run
> depletion of tied material), and the id-less legacy shape `POST /inventory/issue` **used to** write
> (`reference_number=<WO number>`, `reference_id` NULL — historical rows only: the endpoint now
> refuses `work_order_number` with 400, see above). The first three come from the **shared**
> `work_order_ledger_filter` — the same predicate job costing, analytics and lot genealogy use — so
> this list cannot disagree with the cost of the job it is listing. (Until PR 1 it matched only
> `reference_type='work_order'` and silently under-reported an entire nest's material.) Soft-deleted
> work orders are deliberately **not** excluded: their posted movements are still real ledger facts.
>
> **`reference_type` is unconstrained free text on the ledger** (`String(50)`, no CHECK / enum /
> domain), so a client filtering `?reference_type=` must treat the value set as **open** — PR 1 added
> `work_order_operation` and PR 4.4 added `work_order_backflush`, neither with a migration. Filter on
> `work_order_id` rather than enumerating shapes.
>
> Paging is `limit` (default 100, **`ge=1, le=500`**) and `offset` (**`ge=0`**, default 0); a value
> outside those bounds — `limit=0`, `limit=1000`, a negative `offset` — is rejected **422** by
> FastAPI before the query runs. `limit` was previously an unvalidated `int`, so a negative or
> unbounded value reached the database; `offset` is new and bounded from the start. Results are
> ordered `created_at DESC, id DESC` (newest first, with the id as a stable tiebreaker), so paging
> with increasing `offset` walks back into older history.
>
> The envelope is **unchanged** — a bare JSON array of transaction rows with no wrapper and no total
> count, the same offset-paged convention as `GET /audit/`: clients over-fetch one row past the page
> size and infer "has next page" from the overflow (the frontend `DataTable` `serverPagination`
> contract).
>
> **The rows are now typed** by `InventoryTransactionResponse` (`app/schemas/inventory.py`, a
> `UTCModel`) instead of being raw ORM objects handed to `jsonable_encoder`. Two consequences:
> `created_at` serializes as UTC ISO-8601 **with the trailing `Z`** (the "store UTC, serve UTC (`Z`),
> display Central" invariant — it was previously zone-less), and the nested `part` object is
> narrowed to `{id, part_number, name, description, revision, unit_of_measure}`. The raw dump
> included the part's entire row, standard / material / labor / overhead cost included, which a
> ledger read has no business publishing. Every ledger column itself is unchanged and still
> top-level, and `transaction_type` still serializes as the lowercase enum **value** (`"receive"`,
> `"count"`, …).
>
> `work_order_id` is a convenience filter for "everything this work order consumed/produced". The
> authoritative statement of what it matches is the four-shape list above; this paragraph and the
> "known gap" that followed it described the **pre-PR-1** behaviour and were left stale through three
> PRs. They are corrected here rather than left to contradict the list: the id-less
> `POST /inventory/issue` shape (`reference_type='work_order'`, `reference_number` = the work-order
> **number**, `reference_id` NULL — no longer writable, historical rows only) is matched by a local
> clause, and the three id-keyed shapes are
> matched by the shared `work_order_ledger_filter`. The work-order number is resolved
> tenant-scoped, so an unknown or other-tenant id simply matches nothing.
>
> ~~**Known gap — `work_order_id` does not yet match operation-scoped material consumption.**~~
> **CLOSED in PR 1** (`work_order_operation`) **and extended in PR 4.4** (`work_order_backflush`).
> Both are returned by this filter today. `GET /traceability/lot/{lot_number}` resolves the same three
> shapes for its as-built genealogy, so the two reads cannot disagree.
>
> **`work_order_id` deliberately does not exclude soft-deleted work orders.** Voiding a work order
> does not un-move the material it consumed — those ledger rows are still real, posted facts, and
> this is a traceability/history read. Filtering `is_deleted == false` here would silently drop a
> voided WO's movements (including the `reference_number`-shaped rows) from its own history.
>
> **TRANSFER rows are signed for movement, not for net stock.** A `transfer` row carries a
> **positive** `quantity` together with `from_location` and `to_location`, and represents a
> **zero net change** in on-hand — the source decrement and destination increment are two stock
> writes recorded as one movement row. A naive `SUM(quantity)` over a filtered result set therefore
> over-counts. Callers reconstructing "what this work order consumed" must exclude or specially
> handle `transaction_type='transfer'` rather than summing the column blindly. For contrast, the
> other rows this router writes are net-correct as written: `receive` is positive, `issue` is
> negative (`-quantity`), and `adjust` / `count` carry the signed delta (`new − old` for both:
> a COUNT row carries `counted − on-hand at completion`, the **current-basis** delta — see the
> cycle-count variance-basis note below).

> **Stock movements are audited.** Each of `/receive`, `/issue`, `/transfer`, `/adjust`, and
> `/cycle-counts/{id}/complete` writes tamper-evident audit rows (`GET /audit/`) — one for the
> `InventoryTransaction` and one per stock-level change it produces (a transfer logs both the source
> decrement and the destination increment) — flushed inside the same atomic transaction as the
> inventory write so the audit row commits with the movement. The new `InventoryTransaction` rows are
> tenant-tagged with the active `company_id`. `POST /inventory/locations` is audited too — an
> `inventory_location` CREATE row on the hash chain, written before the commit so it lands with the
> row (a location is the scoping anchor for receives, transfers, and cycle counts).
>
> `.../complete` previously wrote **no** audit rows at all despite adjusting stock. It now follows the
> `/adjust` dual-row convention per adjusted item (`inventory` CREATE for the COUNT movement +
> `inventory` UPDATE for the stock level) and adds one `cycle_count` STATUS_CHANGE for the count
> itself, whose `extra_data` carries `apply_adjustments`, `items_adjusted`,
> `measured_variance_value`, and `posted_variance_value`. `.../start` likewise now audits — a
> `cycle_count` STATUS_CHANGE on the real SCHEDULED→IN_PROGRESS transition, or an UPDATE recording
> the `assigned_to` change when an already-IN_PROGRESS count is re-assigned.
>
> **The whole cycle-count lifecycle is now on the hash chain**, closing the last two gaps:
> `POST /cycle-counts` writes a `cycle_count` **CREATE** whose `extra_data` records the declared
> scope (`warehouse`, `location_code`, `part_id`) and `total_items` — the step that defines the
> count and enrolls the rows `complete` later adjusts — and
> `.../items/{item_id}/count` writes a `cycle_count_item` **UPDATE** for every counted quantity,
> carrying the previous values. That last one matters on a **re-count**: a second POST while the
> count is still `IN_PROGRESS` is legal, but it silently replaces `counted_quantity` / `variance` /
> `counted_by` on the row, so the audit entry is the only surviving record of the value it
> overwrote. The first count of an item is logged the same way, with null old values.

> **Every inventory lookup is tenant-scoped.** Location codes, lot numbers, and warehouse names are
> **not** unique across companies, so each of these endpoints resolves them against the active
> company only — a code or id belonging to another tenant behaves as **404 / not found**, never as a
> valid target. This covers the `location_code` lookups on `/receive`, `/transfer`, and
> `POST /cycle-counts`; the existing-lot row that `/receive` increments and the destination row that
> `/transfer` increments; the per-part on-hand aggregate behind `/low-stock`; the parent count and
> count item on `.../items/{item_id}/count`; and the inventory row that `.../complete` adjusts.
> `POST /cycle-counts` enrolls only the active company's stock rows, and the `CycleCountItem` and
> COUNT `InventoryTransaction` rows it and `.../complete` create are tenant-tagged with the active
> `company_id`.
>
> **What the missing `company_id` stamps actually did.** `cycle_count_items.company_id` and
> `inventory_transactions.company_id` are **NOT NULL** (`TenantMixin`; set NOT NULL by migration
> `026_add_multi_tenancy`). The two inserts that omitted the tag therefore raised `IntegrityError`
> at commit: `POST /cycle-counts` **always 500'd and rolled back** whenever the scope matched at
> least one stock row, and `.../complete` **always 500'd and rolled back** whenever it had an
> adjustment to post. No untagged and no cross-tenant row was ever persisted by either path — the
> unscoped enrollment query and the unscoped `InventoryItem` lookup in `.../complete` were latent
> defects masked by that constraint, not sources of bad data. The `company_id` stamps are what make
> these two endpoints function at all, which is why the lifecycle guards and audit trail below
> ship with them.

> **Cycle-count lifecycle guards.** The count is a state machine and the endpoints now enforce it.
> These are integrity guards, separate from authorization: `start` and `.../count` are open to every
> role **except Viewer** (counting is the operator task, and the gate is defined by excluding the
> one read-only role — see `docs/RBAC_PERMISSIONS.md` → Inventory); `POST /cycle-counts` and
> `.../complete` keep their existing Admin / Manager / Supervisor gates.
>
> - `POST /cycle-counts/{id}/start` — **409** if the count is terminal (`COMPLETED` or `CANCELLED`).
>   Re-opening a completed count would let a second `complete` double-post the same physical variance
>   to the ledger; a cancelled count was deliberately abandoned. Starting an **already-IN_PROGRESS**
>   count is still allowed (it re-assigns it to the caller) but **preserves the original
>   `started_at`** — that timestamp is the traceability record of when counting began.
> - `POST /cycle-counts/{id}/items/{item_id}/count` — **409** unless the parent count is
>   `IN_PROGRESS`. A client must `start` the count first; a `SCHEDULED` count has nothing to count
>   against, and once the count is closed the counted quantity is evidence the variance adjustment
>   was derived from and must not be overwritten.
> - `POST /cycle-counts/{id}/complete` — **409** if the count is already `COMPLETED` (or
>   `CANCELLED`). This closes a **ledger double-post**: a second call appended a second COUNT
>   `InventoryTransaction` for the same physical variance while writing the same on-hand figure,
>   permanently diverging the ledger from stock.
> - `POST /cycle-counts` — **404** when `location_code` does not resolve in the active company.
>   Previously an unknown code was silently ignored, producing a count whose declared scope did not
>   match the rows it enrolled.

> **`total_variance_value` is the POSTED variance (semantic change), and `measured_variance_value`
> is new.** `POST /cycle-counts/{id}/complete` now distinguishes two figures:
>
> - **posted** — the variance value of only those items that actually produced a COUNT
>   `InventoryTransaction`, priced on **that transaction's own basis**: the **current-basis
>   quantity delta** (`counted − on-hand at completion`, read under the row lock) `×` the
>   **current** `InventoryItem.unit_cost`, exactly what the ledger row carries. This is what is
>   persisted to `CycleCount.total_variance_value` and returned as `total_variance_value`, so the
>   column reconciles against the ledger rows the completion wrote. Under `apply_adjustments=false`
>   **nothing posts, so this figure is `0.0`** — previously the field was populated with the
>   measured total even though no ledger row existed.
> - **measured** — the variance value of every counted item with a non-zero variance, whether or not
>   it posted, on the **enrollment** basis: `CycleCountItem.variance` (`counted − system_quantity`
>   at enrollment) priced on the enrollment-time `CycleCountItem.unit_cost` snapshot. Returned as the
>   **new, additive** `measured_variance_value` response field. It is *not* a new column; per-item
>   `CycleCountItem.variance` / `variance_value` remain the record of what the counters found, and
>   the figure is also carried in the STATUS_CHANGE audit row's `extra_data`.
>
> **The COUNT ledger row's `quantity` is the current-basis delta, not the enrollment variance.**
> The ledger records actual stock movement, and stock routinely moves between enrollment and
> completion now that operation completion consumes tied material — posting the enrollment variance
> while writing on-hand absolutely made `SUM(ledger)` diverge from on-hand permanently and silently
> resurrected consumed stock. The enrollment variance stays untouched on the count item (the quality
> figure), and the ledger row's `notes` state **both** bases (counted, on-hand at completion,
> current-basis delta, system at enrollment, enrollment variance). When the current-basis delta is
> zero (within the shared ledger epsilon — fractional consumption leaves float residues that must
> not post) **no ledger row is written**: on-hand is still snapped to the counted figure, and the
> count outcome is already on the count item.
>
> The two figures differ whenever `apply_adjustments=false`, a count item points at a stock row that
> has since been removed, **the part's unit cost moved between enrollment and completion** (a
> re-cost, or a receipt at a different price), **or stock moved between enrollment and completion**
> (routine — operation-completion consumption). Accumulating the posted total from the
> enrollment-time snapshot made `total_variance_value` disagree with the very COUNT rows the same
> request had just written. Clients reading `total_variance_value` as "what the counters found" must
> switch to `measured_variance_value`.

> **Concurrency: `complete` locks the count row.** The terminal-state **409** above is
> check-then-act, so under PostgreSQL READ COMMITTED two overlapping requests could both read
> `IN_PROGRESS`, both pass the guard, and both post a COUNT transaction for the same physical
> variance (FastAPI runs these sync handlers in a threadpool, so the overlap is real, and
> `CycleCount` carries no optimistic-lock `version` column). The handler now takes a
> `SELECT id ... FOR UPDATE` row lock on the count *before* the guard, held until the completion
> commits. It is a deliberately separate id-only query: the main load uses
> `joinedload(CycleCount.items)`, and PostgreSQL refuses `FOR UPDATE` across the `LEFT OUTER JOIN`
> that produces. A count id from another tenant never resolves, so the lock is tenant-scoped too
> (**404**).

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
> (reported positive). `GET /traceability/serial/{serial_number}` mirrors the lot trace's work-order
> and NCR collection. Every query is scoped to the active company.
>
> **Three consumption sources are read, and all three must be.** Genealogy resolves ISSUE rows under
> **`reference_type='work_order'`** (LEGACY pre-PR-4.4 one-shot BOM / work-order-scoped-tie rows,
> `reference_id` = the work order — nothing writes this shape any more), **`work_order_backflush`**
> (PR 4.4: the reconciling component leg — BOM/routing demand and work-order-scoped ties,
> `reference_id` = the work order) **and** **`reference_type='work_order_operation'`** (per-run
> consumption of material tied to
> an operation, `reference_id` = the **operation**, mapped back to its work order here). All three collapse
> into the same per-`(work order, component part, lot)` lines, so a nest that consumed sheets and a
> BOM that backflushed hardware read identically. Historical rows are **not** migrated — they
> truthfully carry `work_order` only. **Expect more lines per component than before PR 4.4**: the
> reconciling leg spills across lots, so one logical draw of 25 over lots of 10/10/10 is three lines
> naming three heats. That is the intended as-built record — the single summed row it replaced could
> not express which heats went into the part. Consequently `consumed_components` is non-empty when the
> producing part had `backflush_components = true` **or** the work order carried material ties (see
> Work Orders → "Material ties"); it stays empty for purchased/raw lots and for untied work on a part
> that never opted into backflush. In practice **material ties are still the only source with production
> mileage**: PR 4.5 made `backflush_components` settable (see [Part Schema](#part-schema)), but the
> column defaults to `false` and no work order has yet reached the BOM/routing leg. The claim here used
> to be structural ("nothing writes the flag"); it is now merely factual, and it stops being true the
> first time a part opts in. All three `work_order`-family reference types are also what the lot
> and serial traces use to collect `work_orders_used`.

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
| GET | `/analytics/predict/delivery/{work_order_id}` | Predicted completion date + per-operation forecast for one work order | Admin / Manager / Supervisor |
| GET | `/analytics/predict/capacity` | Capacity utilization forecast by work center, by week | Admin / Manager / Supervisor |
| GET | `/analytics/predict/inventory-demand` | Predicted stockout dates / reorder urgency by part | Admin / Manager / Supervisor |
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

<a id="predictive-analytics"></a>
> **Predictive analytics (delivery / capacity / inventory demand).** Three read-only endpoints
> gated `require_role([ADMIN, MANAGER, SUPERVISOR])`, served by `PredictionService`
> (`app/services/prediction_service.py`). All three are heuristic forecasts off live operational
> data — they write nothing: no ledger row, no audit row, no event.
> - **`GET /analytics/predict/delivery/{work_order_id}`** — no query params. Returns
>   `DeliveryPrediction`: the header (`work_order_number`, `part_number`, `quantity`, `due_date`),
>   `predicted_completion`, `confidence` (0.5–0.9, scaled by how much historical data backs the
>   estimate), `on_time_probability` (0.1 / 0.5 / 0.75 / 0.95 by days of margin against `due_date`),
>   `bottleneck_work_center`, and `operations[]` — per routing step the `operation_name`,
>   `work_center_name`, `predicted_start` / `predicted_end`, `queue_position` and `estimated_hours`.
>   Estimates are planned hours scaled by that work center's trailing-90-day actual-vs-planned ratio,
>   offset by queue depth at 8 h/day.
> - **`GET /analytics/predict/capacity`** — `weeks_ahead` (int, default `4`, `ge=1`, `le=12`).
>   Returns `CapacityForecastResponse`: `weeks[]` (each `week_start` / `week_end`, `work_centers[]`
>   with `committed_hours` / `available_hours` / `utilization_pct` / `is_overloaded`, plus
>   `total_committed` / `total_available` / `overall_utilization`) and `alerts[]` for week 0
>   overloads (`severity` `high` above 110% utilization, else `medium`). Committed hours are the
>   remaining hours on RELEASED/IN_PROGRESS work orders spread evenly across the window; available
>   hours are `capacity_hours_per_day × 5 × efficiency_factor`.
> - **`GET /analytics/predict/inventory-demand`** — no query params. Returns
>   `InventoryDemandResponse`: `predictions[]` (**capped at the 50 most urgent**) with `part_number`
>   / `part_name`, `current_stock`, `daily_usage_rate` (net 90-day issues less returns ÷ 90),
>   `predicted_stockout_date`, `days_until_stockout`, `open_po_quantity`, `next_po_due` and
>   `urgency` (`critical` ≤ 7 days, `warning` ≤ 14, else `ok`, de-escalated one step when an open PO
>   lands before the stockout date), plus `critical_count` / `warning_count`. Considers active,
>   non-deleted `purchased` / `raw_material` parts only.
>
> **Refusals on `/predict/delivery/{work_order_id}` (new).** A work order belonging to **another
> company** now returns **404 `{"detail": "Work order not found"}` — byte-identical to the response
> for an id that does not exist at all**, and identical for a soft-deleted work order. The refusal
> carries no identifier, so the status code cannot be used as an existence oracle (the #189
> convention). Previously this route looked the work order up **by primary key with no ownership
> check of any kind**, so a sequential-integer walk returned any tenant's header *and* its sequenced
> routing. The second refusal, 404 `{"detail": "Work order has no routing operations"}`, is now
> reachable only for a work order the caller owns, so keeping it distinct discloses nothing.
>
> <a id="predictive-analytics-behavior-change"></a>
> **⚠️ Behavior change on multi-company installs: these figures will DROP, and the old ones were
> wrong.** All three endpoints previously ran **entirely unscoped** — `PredictionService` was
> constructed with a session and no `company_id`, so every read underneath it (parts, inventory
> items, inventory transactions, work orders, work-order operations, work centers, purchase orders
> and PO lines) summed across **every tenant on the install**. Concretely, before this fix:
> `/predict/capacity` listed every tenant's machines by name and folded their open jobs into
> `committed_hours` / `overall_utilization`; `/predict/inventory-demand` rendered foreign part
> numbers and names into `predictions[]` and inflated `current_stock`, `daily_usage_rate` and
> `open_po_quantity` for shared part ids; and `/predict/delivery` reported `queue_position` counting
> other tenants' queued operations and `estimated_hours` steered by their efficiency ratios.
> **Anyone comparing a dashboard across this deploy should expect utilization, committed hours,
> queue positions, stock figures and part counts to fall, and in some cases for work centers or
> parts to disappear from the list entirely. That is the correction, not a regression — the smaller
> numbers are the first correct ones.** Single-company installs are unaffected, with one exception
> noted below.
>
> **Also corrected (single-tenant installs too): soft-deleted rows stopped counting.** Independent of
> tenancy, these reads did not filter `is_deleted` and so a caller's **own** deleted records were
> steering its own forecasts — a soft-deleted work order inflated its own queue depth (which
> multiplies into every predicted date) and its own `committed_hours`; a soft-deleted BOM kept
> generating component demand; a soft-deleted purchase order still counted as inbound supply, which
> suppresses a real stockout warning; and retired (`is_deleted`) parts were still being forecast and
> reordered. All are now excluded, so these figures can move on a single-company install as well.

> **Custom reports are tenant-scoped.** Both `POST /analytics/custom-report` and
> `GET /analytics/custom-report/export` run the report through the shared `ReportBuilderService`, which
> now **always restricts the query to the caller's active company** (`company_id`) before applying any
> user-supplied filters/group-by/sort. Every supported data source (work orders, parts, inventory, NCRs,
> purchase orders, quotes) carries `company_id`, so a report can never return another tenant's rows. This
> is a scoping-only fix — the request/response shape is unchanged.
>
> **The exported CSV is formula-neutralized, header row included** — the header comes from the
> tenant-authored template's column list, not a fixed allowlist. Affected cells gain a leading `'`;
> see [Spreadsheet Exports](#spreadsheet-exports-csv--xlsx).
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
| GET | `/users/` | List all users | Admin / Manager |
| POST | `/users/` | Create user | Admin |
| PUT | `/users/{id}` | Update user | Admin |
| DELETE | `/users/{id}` | Deactivate user (sets `is_active=false`; cannot deactivate yourself) | Admin |
| POST | `/users/{id}/unlock` | Clear failed-login lockout | Admin |

> **User writes are Admin-only; the list read is Admin / Manager.** `GET /users/` (and
> `GET /users/{id}`) are `require_role([ADMIN, MANAGER])`; `POST` / `PUT` / `DELETE` are
> `require_role([ADMIN])`. Two guards apply to role assignment on the write paths:
> `role = platform_admin` is rejected with **400** (`"Platform admin role cannot be assigned"`) on
> both `POST /users/` and `PUT /users/{id}` — the cross-company oversight role is never assignable
> from a tenant path (matching the import and `POST /users/{id}/approve` guards) — and on
> `PUT /users/{id}` an Admin cannot change **their own** role (**400**, `"You cannot change your own
> role"`; editing one's own other fields stays allowed). Every user mutation (create, update,
> approve, password-reset, deactivate, activate, unlock) is recorded in the tamper-evident audit
> log; the self-service `POST /users/change-password` likewise records a `PASSWORD_CHANGE` audit
> event (`extra_data.source = "self_service"`, mirroring the admin `reset-password` path — the
> password/hash is never included). See
> [docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) → Users.
>
> **`POST /users/{id}/unlock` — the admin remedy for the failed-login lockout.** After 5 failed
> logins the auth endpoints set `locked_until` 30 minutes out and refuse further attempts with
> `"Account is locked. Please contact administrator."`; unlock resets `failed_login_attempts` to 0
> and clears `locked_until` so the user can log in immediately. Tenant-scoped (an id in another
> company is **404**) and **idempotent**: unlocking a user with no lock state returns **200** and
> writes **no** audit row. The audit row is truthful about the prior state: clearing a lock still
> **in force** (`locked_until` in the future) records a `STATUS_CHANGE` (`locked` → `unlocked`)
> with the prior `failed_login_attempts` / `locked_until` in `extra_data`; clearing only
> **residual** state (an expired lock, or failed attempts that never reached 5) records an
> `UPDATE` of the two fields instead — no lockout status actually changed.
> To let the admin UI surface the lock, `locked_until` is exposed on the **inline user-management
> `UserResponse`** (`app/api/endpoints/users.py`) only — every route serializing it is self-scoped
> (`GET /users/me`) or Admin/Manager-gated; general user serialization
> (`app.schemas.user.UserResponse`, the per-domain summaries) still omits lockout state.

> **Password-strength policy — enforced server-side on every password-set path.** A password set
> through `POST /users/` (create), `POST /users/{id}/reset-password`, or the self-service
> `POST /users/change-password` must satisfy exactly **two** rules: it must be **≥ 12 characters**,
> and it must not contain a **common weak substring** (case-insensitive) from the blocklist in
> `_COMMON_PASSWORD_PATTERNS` — keyboard walks, perennial top-100 passwords, digit runs, and the
> shop's own name (`werco`, `wercomfg`). A violation returns **422**; the blocklist message is
> `"Password contains a common word or pattern that is too easy to guess"`. There are **no
> character-class (composition) requirements** — the uppercase / lowercase / digit / special-character
> rules were removed on 2026-07-29 per NIST SP 800-63B §5.1.1.2 (length + blocklist over composition),
> and the blocklist was expanded from 6 entries to ~37 in the same change. A passphrase such as
> `correct horse battery staple` is now accepted, where the old rules rejected it. The 12-character
> minimum is unchanged and is additionally enforced as `Field(min_length=12)` on the request schemas.
> The **same policy** — the shared `validate_password_strength` (`app/schemas/user.py`) —
> also governs `POST /auth/register` (admin create), public self-registration
> `POST /auth/register-public`, and the **first-admin `admin_password`** on the two company-creation
> paths: the **unauthenticated** `POST /companies/register` (company self-registration) and
> platform-admin `POST /platform/companies`. (`POST /companies/register` previously skipped the
> common-substring check and `POST /platform/companies` had **no** strength check at all — both now
> enforce the full policy, so no first-admin can be seeded with a weak password.) The user CSV import
> (`POST /users/import-csv`) applies the same check to **user-supplied** passwords **per row** (a weak
> password fails only that row, `reason` = `"Weak password: …"`); operator **auto-generated**
> passwords (for badge/employee-ID logins) satisfy the policy by construction and are **exempt**.

### User self-service (My Settings)

Self-scoped profile + notification settings for the signed-in user. **No role gate beyond
authentication** — every route reads/writes only `current_user` and never accepts a user id, so no
caller can reach another user's phone or preferences. Backing UI: **My Settings** (`/settings`, all
roles). See [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#sms-channel-twilio).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/users/me` | Current user's own profile (the one general route exposing `phone`) | Any authenticated user (self) |
| PUT | `/users/me/phone` | Set or clear your own phone number (stored E.164, audited) | Any authenticated user (self) |
| GET | `/users/me/notification-preferences` | Your **effective** per-event channel matrix | Any authenticated user (self) |
| PUT | `/users/me/notification-preferences` | Save your **SMS** opt-ins (audited) | Any authenticated user (self) |
| POST | `/users/me/test-sms` | Send a test SMS to your own number — **`3/minute` per IP + `3/hour` per user** | Any authenticated user (self) |

> **`PUT /users/me/phone`** — body `{ "phone": "512-555-0100" }` (or `{ "phone": null }` / `""` to
> clear); `phone` is `max_length` 32. The number is parsed against `SMS_DEFAULT_REGION` (default
> `US`) and **normalized to E.164** before storage, so the SMS transport never has to guess a
> country code. An unparseable/invalid number returns **400** (`"Invalid phone number: …"`) rather
> than being stored and failing later at send time. Returns the full `UserResponse`. The change is
> written to the tamper-evident audit log (`extra_data.source = "self_service"`) — the phone is the
> destination of every SMS alert, so a silent redirect would be an audit gap. A no-op change
> (same number) short-circuits without an audit row. The **admin** paths `POST /users/` and
> `PUT /users/{id}` accept and normalize `phone` the same way (it was previously a phantom schema
> field that was silently dropped).
>
> **`GET /users/me/notification-preferences`** returns the channel map the dispatcher **would apply
> right now** — catalog defaults where the user has saved nothing, plus any mandatory channel forced
> on — resolved through the dispatcher's own `channels_from_pref`, so the settings UI can never
> disagree with what actually gets sent. It is **read-only and non-creating**: a user who has never
> saved preferences gets defaults and **no** `NotificationPreference` row is written.
> ```json
> {
>   "preferences": {
>     "wo.blocker_created": { "digest": false, "email": true, "in_app": true, "sms": false }
>   },
>   "has_saved_preferences": false,
>   "phone": "+15125550100",
>   "sms_egress_enabled": false,
>   "sms_configured": true
> }
> ```
> `sms_egress_enabled` (the company kill switch) / `sms_configured` (server-side Twilio config) /
> `phone` are what the UI uses to explain why an SMS toggle would currently be inert.
>
> **`PUT /users/me/notification-preferences`** — body
> `{ "preferences": { "<event_key>": { "sms": true }, … } }`, max 200 events. **Scope: the `sms`
> channel only.** Both models are `extra="forbid"`, so a payload carrying `in_app` / `email` /
> `digest` fails loudly with **422** instead of silently dropping channels this endpoint does not
> yet own (the full matrix is PR 3). Errors: an unknown `event_key` → **400**
> (`"Unknown notification event(s): …"`); enabling `sms` on an event that is not `sms_eligible` →
> **400** (`"SMS is not available for event(s): …"`). Events the user has never touched are seeded
> from catalog defaults before the `sms` bit is applied, and the stored row keeps the full
> `{in_app, email, sms, digest}` shape per event. The mandatory channel is **not** baked into the
> stored row — the dispatcher re-applies it at send time. Audited as a
> `notification_preference` update. Returns the same response shape as the `GET`.
>
> **`POST /users/me/test-sms`** — no body. Sends `Werco: test message - SMS alerts are configured
> for your account.` to **`current_user.phone`**; the destination can never be supplied by the
> caller, so this cannot be used to message an arbitrary number. It runs through the same
> `sms_service` path as real notifications (so the `allow_sms_egress` kill switch is enforced
> fail-closed) and writes a `notification_logs` row (`event_type = "sms.test"`) **before** the
> outbound call, so the attempt is recorded even if the process dies mid-flight.
>
> Bounded **twice**, because it is the one authenticated route that spends carrier money per call:
> **`3/minute` per IP** (`ENDPOINT_RATE_LIMITS` in `main.py`) and **`3/hour` per user**
> (`SMS_TEST_HOURLY_CAP_PER_USER`, `reserve_test_sms_quota`). The per-IP limiter alone is not
> sufficient — it keys on address, so one account multiplies it by rotating egress IPs, and it is
> disabled entirely wherever `RATE_LIMIT_ENABLED=false`. The per-user budget is separate from
> `SMS_HOURLY_CAP_PER_USER`, so testing never consumes the critical-alert allowance.
>
> Any phone number appearing in a provider/validation error is **masked** before it is written to
> `notification_logs.error` (`***0134`), because that field is served by
> `GET /notifications/logs` to roles that cannot see `phone`. The HTTP `detail` returned to the
> caller is unmasked — they own the number.
>
> | Status | When |
> |---|---|
> | **200** `{ status, sid, provider_status, detail }` | Sent (`status = "sent"`), **or** Twilio unconfigured on the server (`status = "skipped"`, `detail` explains) |
> | **400** | No phone on file; company SMS egress disabled; the stored number is invalid |
> | **429** | Per-user hourly test budget exhausted (`3/hour`) |
> | **503** | The quota backend (Redis) is unavailable — refuses rather than sending unmetered, and deliberately does *not* report this as a limit the user hit |
> | **502** | Provider rejected the message, or the provider was unreachable |

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

> **`POST /admin/settings/seed-database` ships no default credentials (Admin-only).** The one-time
> bootstrap seed **no-ops once any user exists** (returns `{"status": "already_seeded"}`); on an
> empty instance it creates the initial admin + sample users with **strong, per-user passwords
> generated at runtime** (policy-compliant by construction) and returns them **once** in the response
> `credentials` map for the calling admin to distribute and rotate. No `admin123` / `password123` or
> other hardcoded/default password is used anywhere on this path.

### Company (self-service)

The active company's own profile and self-managed settings. Mounted under `/companies`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/companies/me` | Get the active company (includes `allow_ai_egress`, `allow_sms_egress`, and `user_count`) | Any authenticated user |
| PUT | `/companies/me` | Update the active company's settings | Admin |
| PUT | `/companies/me/ai-egress` | Toggle the company's **AI document-extraction egress kill switch** (`allow_ai_egress`) | Admin |
| PUT | `/companies/me/sms-egress` | Toggle the company's **notification-SMS egress kill switch** (`allow_sms_egress`) | Admin |

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

> **`allow_sms_egress` is the notification-SMS CUI kill switch (default OFF).** It gates **all**
> outbound notification SMS to Twilio, which sits outside the CUI boundary.
> `PUT /companies/me/sms-egress` takes `{ "allow_sms_egress": boolean }` and returns the updated
> `CompanyResponse`. **Admin-only** (`require_role([ADMIN])`), matching the sibling
> `allow_ai_egress` / `allow_carrier_egress` / `allow_print_egress` controls, and it only ever
> mutates the caller's **own active company** (`get_current_company_id`; never taken from the
> request body). **Double-audited**: the flip writes both a field-level update **and** an
> `sms_egress_enabled` / `sms_egress_disabled` status change on the tamper-evident trail. Every
> company is created **OFF**; while OFF the SMS transport denies **fail-closed** on every send —
> including messages already queued in ARQ — and nothing (not even the destination number) leaves
> the boundary. Turning it on is necessary but not sufficient: SMS also requires server-side Twilio
> configuration and a per-user opt-in with a saved phone number. Exposed in the UI at
> **Admin Settings → SMS Privacy** (`/admin/settings?tab=smsprivacy`). See
> [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#sms-channel-twilio).

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

### Notifications (in-app inbox)

Per-user notification inbox for the bell / popover / `/notifications` page — PR 1 (Foundation +
in-app inbox) of the notification system. See [docs/NOTIFICATIONS.md](NOTIFICATIONS.md) for the
architecture (transactional outbox, event catalog, channels, compliance invariants). Per-user
channel preferences and the SMS channel live under
[User self-service (My Settings)](#user-self-service-my-settings). Every inbox
route is **self + tenant scoped**: rows are filtered to `user_id == current_user.id` **and**
`company_id == get_current_company_id` — there is no role gate (any authenticated user manages
their own inbox).

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/notifications` | Paged inbox for the current user (newest first) | Yes (own rows) |
| GET | `/notifications/unread-count` | Cheap unread badge count `{ "count": int }` | Yes (own rows) |
| GET | `/notifications/catalog` | The event catalog for the settings matrix | Yes (all roles) |
| POST | `/notifications/{id}/read` | Mark one notification read (404 if not owned) — **not audited** | Yes (own rows) |
| POST | `/notifications/read-all` | Mark all of the caller's unread read `{ "updated": int }` — **not audited** | Yes (own rows) |
| GET | `/notifications/logs` | Recent email/SMS delivery-attempt log (retained) | Yes (see below) |

> **List query params (`GET /notifications`):** `unread` (`true` = only unread, `false` = only
> read, omit = all), `category` (a catalog category, e.g. `Production` / `Quality` / `Purchasing &
> Inventory` — an unknown value returns an empty page, never all rows), `severity`
> (`info` | `warning` | `critical`), `page` (default 1), `page_size` (default 25, **max 100**).
> Ordered `desc(created_at, id)`.
>
> **List response** (`NotificationListResponse`) — note the `pagination` object differs from the
> generic offset paging under [Pagination](#pagination):
> ```json
> {
>   "items": [
>     {
>       "id": 812,
>       "event_key": "wo.blocker_created",
>       "severity": "critical",
>       "title": "Work order blocked / on hold: WO-1042",
>       "body": "A work order or operation was placed on hold or blocked.\n\nCategory: machine_down | Source: kiosk",
>       "link": "/work-orders/1042",
>       "related_type": "work_order",
>       "related_id": 1042,
>       "is_read": false,
>       "read_at": null,
>       "created_at": "2026-07-24T15:04:11Z"
>     }
>   ],
>   "pagination": {
>     "page": 1, "page_size": 25, "total_count": 3,
>     "total_pages": 1, "has_next": false, "has_previous": false
>   }
> }
> ```
> `event_key` is the catalog key (see `GET /notifications/catalog`); `link` is a **relative** SPA
> route the UI deep-links to; timestamps are UTC `Z` (display Central).
>
> **Content (revised 2026-07-29, after CMMC L2 was descoped):** `title` is the catalog label plus
> the record identifier. `body` is the catalog description, then a blank line, then a detail line
> composed from a curated payload allowlist — statuses and transitions, the `quantity_*` family,
> priorities, day counts, disposition/category/source/inspection method, and `reason` — pipe-joined,
> each value truncated at 120 chars. When the payload carries none of those keys the body is the
> description alone. **Part numbers and customer names are still absent**: the dispatcher reads the
> event payload only and never re-queries to resolve `part_id` into a part number (a scope/N+1
> decision, not a security boundary). See
> [docs/NOTIFICATIONS.md → Content rules](NOTIFICATIONS.md#content-rules-compliance) for the
> boundary decision of record.
>
> **`GET /notifications/catalog`** returns one object per catalog entry
> (`event_key`, `label`, `description`, `category`, `severity`, `default_channels[]`,
> `mandatory_channel`, `sms_eligible`) — the source of truth the settings UI renders (the frontend
> never hardcodes the event list). All roles may read it.
>
> **Mark-read is deliberately NOT audited** — read state is UI state, not domain state, so it does
> not write the `audit_log` hash chain (see [docs/NOTIFICATIONS.md](NOTIFICATIONS.md) §Compliance).
>
> **`GET /notifications/logs`** (retained delivery-attempt view): query `limit` (1–100, default 25),
> `status` (`sent` | `failed`), `mine_only` (default `true`). A non-Admin/Manager/Supervisor caller
> is always restricted to their own log rows regardless of `mine_only`; the full admin-scoped
> delivery-failure view is PR 3. Rows are returned for **both** the `email` and `sms` channels
> (`channel`); for SMS, a message suppressed by the per-user storm cap is recorded with the reason
> in `error` rather than being silently dropped — see
> [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#storm-control). `NotificationLogResponse` returns
> `id`, `user_id`, `event_type`, `channel`, `subject`, `body`, `sent`, `error`, `related_type`,
> `related_id`, `sent_at`, plus — for SMS — `provider_message_id` (the Twilio message SID) and
> `provider_status` (the provider-reported status). Those two make "did that alert actually go
> out?" answerable from the API instead of only from the table; they are an opaque provider id and
> a status string, not PII.

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
> `docs/RBAC_PERMISSIONS.md` → Bulk Imports). It likewise **rejects a weak user-supplied password
> per row** (`reason` = `"Weak password: …"`), applying the same strength policy as `POST /users/`
> (see [Users](#users-admin) above); operator auto-generated passwords are exempt.
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
> (matches description / resource identifier / user name) filter the rows; `limit`
> (**`ge=1, le=500`**, default 100) and `offset` (**`ge=0`**, default 0) page them — an out-of-range
> value is rejected **422** before the query runs. `GET /audit/summary` takes `days`
> (**`ge=1, le=365`**, default 30). Results are ordered `desc(timestamp)` (newest
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
>
> **The hash chain is pausable, and a paused window reports as legacy — not as tampering.** As of
> 2026-07-29 the chain is gated on the `AUDIT_HASH_CHAIN_ENABLED` setting, which **defaults to
> `true`** (unchanged behavior). If it is set to `false`, rows are still written, but with
> `previous_hash = null` and `integrity_hash = "LEGACY_CHAIN_PAUSED"` — a `LEGACY_`-prefixed
> placeholder that every one of these endpoints already skips rather than asserts correct. Effects on
> the responses:
> - `/integrity/verify` and `/integrity/verify-recent` return a new field
>   **`legacy_sequence_gaps`** (int, default `0`) alongside `legacy_records`. Sequence gaps that touch
>   a legacy/paused row are counted there **instead of** being raised as `sequence_gap` issues, so
>   `chain_valid` stays `true` and `issues` stays empty across a paused window. Gaps are expected
>   while paused because the sequence allocator's values are consumed even by rolled-back
>   transactions. **A non-zero `legacy_sequence_gaps` means gap-based deletion detection does not
>   apply over that span** — read it as a coverage caveat, not a fault. With the chain enabled, an
>   injected gap between two non-legacy rows is still reported as a `sequence_gap` issue and still
>   flips `chain_valid` to `false`.
> - `/integrity/record/{sequence_number}` returns `is_legacy: true` with `hash_valid: true` and
>   `chain_valid: true` for a paused row (skipped, not verified). The first row after a pause begins
>   is included in that skip.
> - `/integrity/status` counts paused rows in `legacy_records` (excluded from `protected_records`).
>   Its **`has_gaps` flag is not legacy-aware** — a plain `total != (last - first + 1)` comparison —
>   so it reads `true` across a paused window. `/integrity/verify` is the authoritative check.
>
> The database immutability triggers (migrations `008`/`060`) are independent of the setting and
> remain in force. Full operational detail, including what is permanently lost while paused:
> [docs/AUDIT_LOG_RETENTION_RUNBOOK.md](AUDIT_LOG_RETENTION_RUNBOOK.md) → **Pausing the hash chain**.
>
> **⚠️ Filtering by `resource_type` does NOT return a material's full history — read this before
> auditing one.** `parts` and `materials` are the *same table* behind two routers, and their audit
> rows are split across two `resource_type` values with a **discontinuity dated 2026-07-27 (PR 4.5)**:
> `POST /materials/` and `DELETE /materials/{id}` log `resource_type="material"`, while
> `PUT /materials/{id}` logs **`resource_type="part"`** — as `PUT /parts/{id}` always has. Updates
> before that date are under `"material"`; updates after it are under `"part"`. Nothing is missing and
> no row was rewritten (the chain is append-only), but a query filtered to one value silently returns a
> partial trail. **Query both**, e.g. `resource_type IN ('part','material') AND resource_id = <part id>`
> ordered by `timestamp`. The change was deliberate: it makes *"who armed automatic component
> backflush on this part, and when"* answerable from one query regardless of which URL was used — see
> [Parts](#parts) → `backflush_components` and
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → the 2026-07-27 (PR 4.5) row, item (3).

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
| POST | `/visitor-logs/manual` | **Staff back-entry** of an offline visit with its ACTUAL past times → **201** `VisitorLogResponse`. Marks the row staff-entered (`signin_station_id` NULL + `entered_by_user_id` set); sends **no** host email. Body = sign-in body + `signed_in_at` (required, past) + `signed_out_at` (optional, ≥ `signed_in_at`, past) | Admin / Manager (staff token only — station token **rejected**) |
| GET | `/visitor-logs/` | List visitor records for the active company (filters + offset paging) → `{"items", "total"}` | Admin / Manager / Supervisor |
| GET | `/visitor-logs/export.csv` | Stream the visitor log as CSV (audits an `EXPORT` action). Cells are formula-neutralized — visitor name / company / purpose note are free text typed at a lobby tablet; see [Spreadsheet Exports](#spreadsheet-exports-csv--xlsx) | Admin / Manager |
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

#### Staff back-entry request (`POST /visitor-logs/manual`)

```json
{
  "visitor_name": "Jane Smith",
  "visitor_company": "Acme Corp",
  "host_name": "John Doe",
  "purpose": "meeting",
  "purpose_note": null,
  "safety_acknowledged": true,
  "signed_in_at": "2026-07-11T14:05:00Z",
  "signed_out_at": "2026-07-11T15:30:00Z"
}
```

`VisitorManualEntryRequest` extends the sign-in body, so all the same visitor-field rules apply
(`visitor_name` required; `purpose_note` required when `purpose == "other"`; `safety_acknowledged`
must be `true`; `visitor_company` / `visitor_phone` / `host_name` optional). It adds the visit's
**actual** times: **`signed_in_at` is required and must be in the past**; **`signed_out_at` is
optional** and, when given, must be **on or after `signed_in_at` and in the past** (omit it if the
visitor is still on-site → `SIGNED_IN`; give it to close the visit → `SIGNED_OUT`). Future or
out-of-order times return **422**. The created row is marked staff-entered — `signin_station_id` /
`station_label` stay `null` and `entered_by_user_id` is set to the acting staff user — and **no host
check-in email is sent** (the visit already happened). The create is audited.

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
  "station_label": "Lobby Tablet",
  "entered_by_user_id": null
}
```

`signed_out_at: null` means the visitor is **still on-site**. `signin_station_id` / `station_label`
are `null` for a staff-created row. **`entered_by_user_id`** is non-null **only** on a staff
**back-entry** (`POST /visitor-logs/manual`) and names the staff user who recorded it — it stays
`null` for every live tablet or staff sign-in, so it positively distinguishes a back-dated row from a
live capture. `host_user_id` is set only when the typed host best-effort-matched exactly one active
internal user in the company.

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

List endpoints page in one of three shapes. There is no `sort` / `order` query parameter anywhere in
the API — each list endpoint has a fixed server-side ordering, stated with the endpoint.

| Shape | Parameters | Response envelope | Examples |
|---|---|---|---|
| **`skip` / `limit`** (most list endpoints) | `skip` (default 0), `limit` (default 100) | Bare JSON array — no wrapper, no total count | `GET /work-orders/`, `GET /parts/`, `GET /bom/`, `GET /routing/`, `GET /quality/ncr` |
| **`offset` / `limit`** | `offset` (default 0), `limit` | Bare JSON array; clients over-fetch one row past the page size to infer "has next page" | `GET /audit/`, `GET /inventory/transactions`, `GET /inventory/`, `GET /purchasing/purchase-orders` |
| **`page` / `page_size`** | `page` (default 1), `page_size` | `{ "items": [...], "pagination": { "page", "page_size", "total_count", "total_pages", "has_next", "has_previous" } }` | `GET /notifications/`, `GET /shop-floor/operations` |

```
GET /work-orders/?skip=100&limit=50
GET /audit/?offset=100&limit=50
GET /notifications/?page=3&page_size=25
```

### Bounds on paging and window parameters

**Every `skip` / `offset` / `limit` / `days` parameter in the API is range-validated by FastAPI
before the query runs.** An out-of-range value is rejected **422** (a validation error naming the
parameter); it is never clamped, and it never reaches the database. `limit=0` is refused rather than
read as "no rows", and `ge=1` also stops a negative value reaching `.limit()` — PostgreSQL rejects a
negative `LIMIT`/`OFFSET`, while SQLite silently reads a negative limit as "unbounded".

| Parameter | Range | Applies to |
|---|---|---|
| `skip` / `offset` | `≥ 0` | Every list endpoint that takes one |
| `limit` — standard list tier | `1 … 5000`, default `100` | `/work-orders/`, `/routing/`, `/work-centers/`, `/documents/`, `/downtime/`, `/oee/records`, `/eco/eco/`, `/quality/{ncr,car,fai}`, `/complaints/{complaints,rma}/`, `/supplier-scorecards/{supplier-scorecards,supplier-audits,approved-suppliers}/`, `/maintenance/history/{work_center_id}`, `/admin/settings/audit-log`, `/quotes/` |
| `limit` — `GET /bom/` | `1 … 10000`, default `100` | Higher ceiling than its neighbours because the Parts screen requests 5000 BOM rows in one call |
| `limit` — small analytic lists | `1 … 500` | `/estimate-workbench/shop-data/history` and `/estimate-workbench/job-actuals` (default `50`), `/mrp/runs` (default `20`) |
| `limit` — ledger / audit reads | `1 … 500`, default `100` | `GET /audit/`, `GET /inventory/transactions` |
| `limit` — typeahead & search | `1 … 50`, default `20` (`GET /search/`); `1 … 20`, default `10` (`GET /search/recent`); `1 … 50`, default `10` (`/po-upload/search-parts`, `/po-upload/search-vendors`) | |
| `days` — rolling window | `1 … 365`, default `30` | `/audit/summary`, `/admin/settings/audit-log`, `/receiving/history`, `/receiving/stats`, `/calibration/equipment/due-soon`, `/certifications/certifications/expiring`, `/supplier-scorecards/supplier-audits/due-soon` |
| `max_levels` | `1 … 20`, default `10` | `GET /bom/{id}/explode`, `GET /bom/{id}/flatten` |
| date **window width** | `start_date … end_date` at most `366` days, `400` over it (and `400` when `end_date < start_date`) | `GET /maintenance/calendar` |

Some endpoints carry their own **tighter** ceiling than the tier above — `GET /parts/`,
`GET /materials/`, `GET /process-sheets/` and `GET /bom/uom-mismatches` cap at `500`,
`GET /visitor-logs/` at `200` — and those are stated with the endpoint. **No default changed**: a
caller sending no paging parameters gets the same rows it got before (on the five previously
unbounded endpoints below, up to their ceiling).

`POST /search/nl` (natural-language search) takes its `limit` in the **request body**, not the query
string (`ge=1, le=50`, default 20 — the same ceiling as `GET /search/`, and the same one the handler
itself clamps to), so an out-of-range value surfaces as a body validation error with
`"loc": ["body", "limit"]`.

### List endpoints that were previously unbounded

Five list endpoints returned their entire matched set with no `limit` at all. They now accept
`limit` / `offset`, **with the default equal to the ceiling** — a caller that sends neither receives
exactly what it received before, and only a pathological request (`?limit=99999999`, or a table that
has grown past what fits in a worker's memory) is refused:

| Endpoint | `limit` default = max | Why that ceiling |
|---|---|---|
| `GET /inventory/` | `10000` | `inventory_items` is the highest-cardinality table in the app (one row per part × location × lot × serial) |
| `GET /inventory/summary` | `10000` | Identical cap on purpose — the Inventory page fetches both in one `Promise.all`, so capping one and not the other would let the stat tiles disagree with the table |
| `GET /purchasing/purchase-orders` | `5000` | The default filter already excludes closed/cancelled POs, so the live set is the shop's open book |
| `GET /customers/` | `5000` | |
| `GET /customers/names` | `5000` | Shares the cap because it feeds every customer picker |

> **Two `/customers` behaviour changes shipped alongside those parameters.**
> - **`GET /customers/names` now excludes soft-deleted customers.** It filtered `is_active` only, and
>   soft-delete does not imply `is_active = false`, so a deleted customer stayed selectable in every
>   quote / RFQ / order picker fed by this endpoint. The sibling `GET /customers/` has always applied
>   the `is_deleted == false` predicate.
> - **`GET /customers/?search=` now matches `name`, `code`, `contact_name` and `city`** (previously
>   name only). Searching by account code, buyer name or city — how a buyer actually finds an
>   account — returned nothing from the server before.

### `GET /quotes/` — a hard-coded cap became a real page

`GET /quotes/` was never *unbounded*; it applied a hard-coded `.limit(100)` with **no `offset`
beside it**, so the 101st quote was unreachable through the API at all and the truncation was
silent. It now takes `limit` / `offset` on the standard list tier above.

**The default is still `100`**, so a caller that sends no paging parameters receives exactly the rows
it received before — but the rows past the first page are now reachable via `offset`. This makes it
the one endpoint in the standard tier whose default is deliberately *not* its ceiling.

```
GET /quotes/?offset=100          # the page that was previously unreachable
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
| `POST /companies/register` | 3/minute |
| `POST /auth/refresh` | 30/minute |
| `POST /auth/employee-login` | 10/minute |
| `POST /auth/employee-logout` | 30/minute |
| `POST /auth/kiosk-badge-token` | 30/minute |
| `POST /auth/display-token/claim` | 10/minute |
| `POST /visitor-logs/station-login` | 5/minute |
| `POST /shop-floor/kiosk-stations/station-login` | 5/minute |
| `POST /scanner/resolve-action` | 60/minute |
| `POST /users/me/test-sms` | 3/minute |
| `POST /errors/log` | 60/minute |

(`POST /users/me/test-sms` is authenticated and self-targeted, but it is the one route that spends
real carrier money per call, so it is capped well below anything a human would click. The two
standalone laser-nest routes carry their own **10/minute** caps — see Laser Nests.)

`POST /companies/register` is **unauthenticated**, and one successful call mints a whole tenant
plus an **active admin** user with live access/refresh tokens — so it is capped like the sibling
register routes. It previously had no per-path entry and ran at only the global 100/minute default.

> **These caps assume a correctly configured edge proxy.** Every per-IP limit on this page is keyed
> on the forwarded client IP, so it is only as trustworthy as the proxy configuration in front of
> the app. **Operators: verify `--forwarded-allow-ips` is pinned to the platform's real edge CIDR**
> in the deploy entrypoints (`start.sh`, `nixpacks.toml`) rather than left permissive. Do not
> instead reach into the forwarded-for chain from application code — that is only correct at exactly
> one proxy hop, and with two it keys every client to the same value, collapsing the whole app into
> a single rate-limit bucket.

`POST /errors/log` (the SPA error beacon) is unauthenticated and CSRF-exempt because
`navigator.sendBeacon` cannot set headers. Its cap is deliberately generous — a whole shop shares
one NAT IP and a single tab flushes at most every 5 s — since a tight cap silently drops error
reports during exactly the mass-failure incident you want them for. The endpoint **writes nothing
to the database**: it logs to the application logger and, for global-boundary errors, to Sentry. It
used to resolve the client-supplied `userId` to a user row and write an audit row, which let anyone
forge tamper-evident audit-chain entries attributed to a named employee in a named company. The
body is additionally capped at **50 entries** with per-field length limits (**422** over either).

An over-limit request returns **HTTP 429** with a `Retry-After` header (seconds until the window
resets) and body:
```json
{ "detail": "Rate limit exceeded: 5/minute" }
```
Enforcement fails open: if the limiter backend errors, the request is allowed (the global default
limit still applies).

**`POST /auth/employee-login` additionally carries a per-IP failed-attempt throttle** — the
compensating control for its 10/minute limit (raised from 3/minute for shift-change badge cycling
on a shared kiosk): **8 FAILED attempts from one IP within 15 minutes → 429** ("Too many failed
sign-in attempts — wait a few minutes") with a `Retry-After` header and a 15-minute cooldown.
Successful logins never count toward the window; the check runs before any user lookup; each
throttled rejection is audited as `EMPLOYEE_LOGIN_BLOCKED`; a counter-storage outage fails open
with a logged warning (the 10/minute cap above still applies). Implementation:
`backend/app/core/login_throttle.py`.

## Request Size Limits

**JSON request bodies are capped at 256 KB** (`MAX_JSON_BODY_BYTES`, env-overridable; the
pre-rename `MAX_SANITIZED_JSON_BODY_BYTES` is still honored as a deprecated alias). Every
`application/json` `POST`/`PUT`/`PATCH` body is size-checked by middleware before the route
runs — ahead of route auth, so an unauthenticated caller cannot choose how many bytes the app
buffers and parses. Over the cap the request is **rejected**:

```json
{ "detail": "Request body too large: 300000 bytes exceeds the 262144-byte limit for JSON requests." }
```

Returned as **HTTP 413**. The check is applied to the declared `Content-Length` before the body
is read, and again to the bytes actually received (chunked encoding, or a header that lies).

**Not affected:** `multipart/form-data` uploads, which keep their own per-endpoint caps (20 MB
for QMS standard uploads, 50 MB `LASER_UPLOAD_MAX_BYTES` for laser-nest ZIP/PDF — those also
return **413**), and inbound carrier tracking webhooks, which bypass this middleware entirely so
their HMAC verifies against raw bytes.

The largest realistic JSON bodies fit — a 170-nest laser import is ~183 KB, a 1000-line-item BOM
create ~201 KB. A BOM create above roughly **1300 line items** exceeds the cap; raise
`MAX_JSON_BODY_BYTES` for that case, noting the sizing guidance in
[Request Body Size](ENVIRONMENT_VARIABLES.md#request-body-size-json).

**Request bodies are stored as sent.** The API does **not** strip or rewrite HTML in request
data — as of 2026-07-30 the middleware only measures size. Angle brackets in a note field (ASME
Y14.5 notation such as `2.500 <REF>`, or `<` meaning "less than") round-trip byte-for-byte
through create/read/update. Server-rendered PDFs escape at render.

## Spreadsheet Exports (CSV / XLSX)

### Access: bulk export is `ADMIN` / `MANAGER`, and every export is audited

All seven `/exports/*` endpoints and `GET /analytics/custom-report/export` require
`require_role([ADMIN, MANAGER])` and return **403** otherwise. This is a deliberate carve-out from
the read-broad domain default: the *reads* behind these datasets remain open to any authenticated
user in the tenant, but handing the **whole dataset over as a file** — the parts master with
`standard_cost`, the full inventory valuation, every PO line with `unit_price` and vendor, every
quote with its customer contacts — is a disclosure event, not navigation. See
[docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) → *Bulk Data Export* for the matrix and the full
in-scope / out-of-scope list.

Each successful export writes one audit row (`app/services/export_audit.py`, via `AuditService`):

| Field | Value |
|---|---|
| `action` | `EXPORT` |
| `resource_type` | the dataset — `work_order`, `part`, `inventory_item`, `purchase_order`, `purchase_order_line`, `quote`, `inventory_transaction`, `custom_report` |
| `description` | `Exported {n} {dataset} record(s) to {CSV or XLSX}` |
| `new_values` | `{"format": …, "columns": [...], "filters": {...}}` — the request, never the payload |

The row is committed **before** the file streams, so an abandoned download is still recorded. A
**403 writes no row**, and neither does a refusal (the 10,000-row cap below, an oversized filter
value, an unsupported `format`, or an empty custom-report result) — nothing was disclosed.

Because an `audit_log` row is immutable and undeletable, everything the request contributes to it is
bounded first. `columns` is filtered to the names the endpoint recognizes; free-text filters are
declared with a `max_length` and a value over it is a **422** before the export runs:

| Endpoint | Parameter | `max_length` |
|---|---|---|
| `GET /exports/inventory/export` | `warehouse` | 50 (the `inventory_items.warehouse` column width) |
| `GET /exports/quotes/export` | `customer` | 255 (the `quotes.customer_name` column width) |
| `GET /exports/inventory/transactions/export` | `transaction_type` | 40 |

Each bound matches the column the filter compares against, so no request that could match a row is
refused. (Auditing is best-effort in the usual repo-wide sense: `AuditService.log` never propagates
a failure to the caller, so a chain-write failure lets the export stream unrecorded rather than
failing the request.)

`GET /visitor-logs/export.csv` (`[ADMIN, MANAGER]`, already audited — visitor PII) and
`GET /estimate-workbench/{estimate_id}/export/*` (`[ADMIN, MANAGER, SUPERVISOR]`, one estimate
rather than a dataset) keep their existing, stricter gates. Single-record document downloads — CoC
PDFs, quote PDFs, nest drawings, kiosk document views — are **not** bulk exports and are unchanged.

### Formula-injection neutralization (CWE-1236)

Every endpoint that returns a spreadsheet artifact neutralizes formula-initiating cell text before
writing it, so an export cannot execute as a formula when the recipient opens it (CWE-1236).
Spreadsheet applications evaluate a cell whose text begins with `=`, `+`, `-`, `@`, TAB (`0x09`) or
CR (`0x0D`); tenant-supplied text — part descriptions, notes, customer names, visitor purposes,
report column names — lands in those cells verbatim. Applies to:

| Endpoint | Formats |
|--------|-------------|
| `GET /exports/{work-orders,parts,inventory,purchase-orders,purchase-orders/lines,quotes,inventory/transactions}/export` | `?format=csv` + `?format=xlsx` |
| `GET /analytics/custom-report/export` | csv |
| `GET /visitor-logs/export.csv` | csv |
| `GET /estimate-workbench/{estimate_id}/export/audit.xlsx` | xlsx |

**XLSX is neutralized without changing any value.** XLSX distinguishes a formula cell from a string
cell in the markup, so any cell holding a string is pinned to a string cell (`data_type="s"`) — the
text is written inside `<is><t>…</t></is>` with no `<f>` element. Cell values are **byte-exact**:
`+1-555-0134`, `-0.005 TIR`, `- check bore per print` and `@rev A` export exactly as stored. Numbers,
dates and booleans keep their types. Previously `openpyxl` turned a leading-`=` string into a real
formula cell, so this is a behavior change in the file's *markup* only, never in its values.

**CSV is neutralized with a leading `'`, which does change the bytes.** CSV carries no type
information, so the only available neutralization is to prefix the cell with a single quote, which
spreadsheet applications consume as "read the rest of this cell as text". The prefix is added
**only** when a value both starts with one of the characters above **and** is not a plain finite
number (anchored — no surrounding whitespace or `_` separators, the same rule on both stacks) — so
`-5.00`, `-0.005` and `+1e3` are exported unchanged and stay usable as numbers.
RFC 4180 quoting is unchanged and still applied *after* neutralization.

> **API consumers: the CSV bytes changed for affected cells.** A client that diffs exports across
> versions, or feeds them to a downstream parser, will see a leading `'` on cells it did not before.
> Strip a single leading `'` if the raw value is required. **XLSX consumers need no change.** This is
> lossy on the **exported artifact only** — the stored record is never modified, and reading the same
> row back over the JSON API returns the original text.

**Header rows are neutralized too**, because two header paths carry caller- or tenant-supplied text
rather than a fixed allowlist:

- the **`columns` query parameter** accepted by every `/exports/*` endpoint is written as row 1, so
  `?columns==HYPERLINK(...)` previously injected the header of the export; and
- the `GET /analytics/custom-report/export` CSV header is derived from the saved report template's
  column list, which is tenant-authored.

**Import parsing is unaffected.** The CSV/XLSX *readers* behind the bulk imports are untouched — a
cell read back in keeps whatever text it holds — as are the static XLSX import templates
(`GET /import/templates/{entity}`), which contain no tenant data.

### Row cap on `/exports/*` — refuses at 10,000 (400)

All seven `/exports/*` endpoints refuse rather than truncate above **10,000 matched rows**
(`MAX_EXPORT_ROWS` in `app/api/endpoints/exports.py` — a module constant, not env-configurable).
The query over-fetches one row past the cap, so the condition is exact rather than estimated, and
the refusal happens before any bytes are written:

```json
{ "detail": "This export matches more than 10,000 rows. Narrow the date range or add a filter, then export again." }
```

Returned as **HTTP 400**. Refusal is deliberate rather than truncation: these endpoints return a
`StreamingResponse` of a file, which has no channel to signal "this is partial", so a truncated
spreadsheet is indistinguishable from a complete one — and these are documents a manager reconciles
from. The cap deliberately mirrors the 10,000-row **import** cap (`MAX_IMPORT_ROWS`; see
[docs/EXCEL_MIGRATION_RUNBOOK.md](EXCEL_MIGRATION_RUNBOOK.md) → *File basics*): an export a human
opens in Excel and an import a human uploads are the same size problem. `generate_excel` builds the
whole workbook in memory before streaming, so the cap also bounds peak memory.

> **`GET /exports/inventory/transactions/export` changed behaviour — this one is user-visible.** It
> previously applied a hard `.limit(10000)` and returned the newest 10,000 rows **silently**: a
> ledger export that looked complete and was not. The same request now returns **400**, and the
> caller narrows `start_date` / `end_date`, `part_id` or `transaction_type` and exports again. The
> other six endpoints had no limit at all and returned everything, so for them the 400 replaces an
> unbounded read, not a silent truncation.

**Not covered by this cap:** `GET /analytics/custom-report/export`, `GET /visitor-logs/export.csv`
and `GET /estimate-workbench/{estimate_id}/export/audit.xlsx` live on their own routers and keep
their existing behaviour — `MAX_EXPORT_ROWS` applies to the `/exports/*` router only.

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

### Readiness

```http
GET /health/ready
```

Unauthenticated. Returns `checks.database`, `checks.redis` (a live PING of `REDIS_URL`), and
`checks.job_queue_redis` — where the **ARQ background-job queue** resolved its Redis, which is
a different question from whether `REDIS_URL` pings:

```json
{
  "status": "configured",
  "source": "REDIS_URL",
  "tls": false,
  "authenticated": true,
  "config_warnings": 0
}
```

`source` is `REDIS_URL`, `REDIS_HOST/REDIS_PORT/REDIS_DB`, or `defaults(localhost)`; `status`
is `configured` / `unconfigured` / `misconfigured`. No hostname and no credential is ever
exposed here — `config_warnings` is a **count**, and the messages themselves go to the startup
log. `unconfigured` means every enqueue will fail and no background job or cron can run. See
[`WORKER_SERVICE.md`](WORKER_SERVICE.md).

## Error Codes

| Status Code | Description |
|-------------|-------------|
| 200 | Success |
| 201 | Created |
| 204 | No Content |
| 400 | Bad Request (also returned for a `Host` header not on the `ALLOWED_HOSTS` allowlist, and for an `/exports/*` request matching more than `MAX_EXPORT_ROWS` — see [Row cap on `/exports/*`](#row-cap-on-exports--refuses-at-10000-400)) |
| 401 | Unauthorized |
| 403 | Forbidden (also returned to a caller below `ADMIN` / `MANAGER` on any bulk-export endpoint — see [Access: bulk export is `ADMIN` / `MANAGER`](#access-bulk-export-is-admin--manager-and-every-export-is-audited)) |
| 404 | Not Found |
| 409 | Conflict — concurrent modification of an operation / work order / time entry on a completion or clock endpoint (the row was updated by another writer between read and commit; refresh and retry) |
| 413 | Content Too Large — a JSON body over `MAX_JSON_BODY_BYTES` (default 256 KB, rejected by middleware before the route runs), or a file upload over its endpoint's own cap (e.g. 50 MB `LASER_UPLOAD_MAX_BYTES`). See [Request Size Limits](#request-size-limits) |
| 422 | Validation Error — including a paging or window parameter outside its range (`limit`, `skip`/`offset`, `days`, `max_levels`), and a free-text `/exports/*` filter over its `max_length` (`warehouse` 50, `customer` 255, `transaction_type` 40); see [Bounds on paging and window parameters](#bounds-on-paging-and-window-parameters) |
| 429 | Too Many Requests |
| 500 | Internal Server Error |
| 502 | Bad Gateway — upstream AI-service failure on an AI endpoint (e.g. `/copilot/chat?stream=false`) |
| 503 | Service Unavailable — an AI endpoint was called but the AI features are not configured (`ANTHROPIC_API_KEY` unset) |

## Interactive Documentation

When the backend is running **outside production**, visit:
- **Swagger UI**: `/api/docs` - Interactive API explorer
- **ReDoc**: `/api/redoc` - Alternative documentation view
- **OpenAPI JSON**: `/api/openapi.json` - Raw specification

All three are disabled when `ENVIRONMENT=production` and return **404** there —
including the raw OpenAPI schema, which would otherwise enumerate every
endpoint, payload shape and auth requirement to an unauthenticated caller. Use a
development or staging deployment to browse the API interactively; this file is
the reference for production. See `docs/PRODUCTION_CHECKLIST.md`.

For more details on specific endpoints, use the interactive documentation above.
