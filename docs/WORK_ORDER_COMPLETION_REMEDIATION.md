# Work-Order Completion Tracking — Remediation Plan

> Source: multi-agent audit (`/.claude/wf-workorder-audit.js`), 2026-06-07, branch `qa/full-pass-2026-06-04`.
> 76 findings, adversarially verified against the code. This doc is the working checklist for the batched fix effort.
> Status legend: ☐ not started · ◐ in progress · ☑ done (tests + compliance review passed)

## Headline

Work-order completion is a **status-only event**. Completing an operation/WO flips status + quantity fields, but:
- the rollup logic is **copy-pasted across 3–4 sites** that have drifted (root cause of most correctness bugs);
- several completion endpoints mutate operations/WOs by integer id with **no `company_id` filter** (cross-tenant writes), and most completion transitions write **no `audit_log` row**;
- production **never reaches the rest of the platform** — no finished-goods receipt, no component backflush, no lot/serial genealogy, no actual-cost / job-cost / labor-hour rollup;
- quality gates (inspection / NCR / FAI / blockers) **do not block completion**;
- concurrent completions can **lose updates** (no row lock; DB version columns exist but are unmapped/unenforced).

## Batch sequencing (each batch = checkpoint for review/sign-off)

| Batch | Ranks | Theme | Migration? | Behavior change? |
|---|---|---|---|---|
| **1** ☑ | 1–4 | Tenant isolation + audit logging | no | no (legit users unaffected) |
| **2** ☑ | 5 | Concurrency: row locks + version + partial unique index | **yes** | 409 on stale write |
| **3** ☑ | 6 | Shared completion finalizer (consolidation) | no | quantity semantics documented; ON_HOLD completion → 409 |
| **4** ☑ | 7 | Quality gates on completion (warn-and-record) | no | records a tamper-evident exception, does not block |
| **5** ☑ | 8 | Uniform completion signal set (events/notify/webhook/sched) | no | new outbound signals |
| **6** ☑ | 9 | FG receipt + backflush + as-built genealogy | **yes** (`040`/`041`) | inventory now moves |
| **7** ☑ | 10 | Labor-hour + job/actual-cost rollup | no | opt-in (flag default OFF); cost/hours roll up only when enabled |
| **8** ☑ | 11 | OEE/OTD metric correctness + dead auto-OEE endpoint | no | **KPI values move**; OEE-write endpoints now role-gated |
| **9** ☑ | 12 | Indexes + de-risk reconcile-on-read | **yes** (`042`) | bounded dashboard reconcile; cheap pre-reconcile ETag |
| **10** ☑ | 13 | Frontend completion UX hardening | no | no API change (dashboard cache invalidation + double-submit guards + list memo) |
| **11A** ☑ | G4-Fix1 / G3-scope / G6-A | Completeness-critic security/correctness (ECO tenant-scope+audit+RBAC, report tenant-scope, terminal-state lock) | no | ECO mutations now Admin/Manager; ECO + custom-report tenant-scoped; 422 on cross-tenant ECO ids; 409 resurrecting a terminal WO |
| **11B** ☑ | G2 / G5-A / G3-content | Completeness-critic data integrity (FG decrement + over-ship guard, TimeEntry-approval costing, report honesty) | no | ship decrements FG inventory + warn-and-records over-ship / missing-FG-lot; new TimeEntry approve/unapprove endpoints (no self-approval); opt-in `REQUIRE_APPROVED_LABOR_FOR_COST`; report drops phantom `estimated_hours` + flag-OFF labor-not-tracked headers |
| **11C** ☑ | G1 / G5-B / G6-B | Completeness-critic compliance (parent/child rollup, operator-cert gate, CoC generation) | **yes** (`043`/`044`) | parent completes + records `child_work_orders_incomplete` while laser children open (warn-only); last-laser-child-done emits `CHILD_WORK_ORDERS_COMPLETE` signal (no auto-complete); clock-in/start warn-and-record operator-qualification gaps (`qualification_exceptions`); per-shipment CoC artifact + auto-issue-on-ship (Admin/Manager/Quality issue) |

## Ranked actions

### Rank 1 — Tenant-scope completion endpoints ☑ (Batch 1)
Files: `shop_floor.py`, `work_orders.py`. Add `company_id` filter to every operation/clock/TimeEntry lookup currently keyed by id alone; add `get_current_company_id` dep to `work_orders.py` `update_operation`/`start_operation`/`complete_operation`/`add_operation`. Return 404 on mismatch **before** any mutation. Use `app.db.tenant_filter` helpers.
Findings: TEN-1, TEN-2, TEN-3, TEN-4, TEN-5, TEN-6, SD-1.

### Rank 2 — Tenant-scope traceability/analytics/OEE/scheduling/MRP ☑ (Batch 1)
Files: `traceability.py`, `analytics_service.py`, `analytics.py`, `oee.py`, `scheduling_service.py`, `mrp_service.py` (+ endpoints/jobs). Thread `company_id` through service constructors; scope every aggregation/lookup. **MS-1 also fixes a currently-broken MRP path (NOT NULL violation today).** Make `run_mrp_task`/scheduling jobs iterate per company.
Findings: TRACE-1, INV-5, OEE-2, OEE-3, MS-1, MS-3, MS-4 (isolation part).

### Rank 3 — Tenant-scope completion WebSocket broadcasts + auth `/ws/updates` ☑ (Batch 1)
Files: `core/websocket.py`, `api/websocket.py`, `work_orders.py`, `shop_floor.py`. Capture `company_id` per connection; add `broadcast_to_company`; route all completion broadcasts to the originating company only; require auth on `/ws/updates`.
Findings: EVT-6.

### Rank 4 — Tamper-evident audit on every completion/close/status-change ☑ (Batch 1)
Files: `shop_floor.py`, `work_orders.py`, `shipping.py`, `inventory.py`, `work_order_blocker_service.py`. `AuditService.log_status_change`/`log_update` (via `get_audit_service`) before each terminal commit; mirror `release_work_order`'s flush→audit→commit atomicity. Includes inventory `/receive,/issue,/transfer,/adjust` and blocker create/update/resolve/dismiss. DUP-1's office complete_operation needs BOTH rank-1 scope AND this audit row.
Findings: DUP-1, RUP-5, AUD-1, AUD-2, ~~AUD-3~~ (deferred to Batch 3), AUD-4, EVT-1, EVT-5, INV-4, BLK-3.
**AUD-3 — now closed in Batch 3 (rank 6):** reconcile-on-read status transitions (dashboard / list / detail / `get_all_operations` calling `reconcile_work_orders_from_completion_evidence`) were deferred here and are now audited. `reconcile_work_orders_from_completion_evidence` returns the terminal transitions and the read handler writes a tamper-evident status-change row per transition (attributed to the requesting user, `extra_data.source = "reconcile_on_read"`) before its commit. See the Batch 3 status note under Rank 6.

> **Batch 1 status (2026-06-07, ranks 1–4 landed).** Tenant isolation is now enforced on the
> completion/operation/clock endpoints (`/shop-floor/clock-in`, `/clock-out/{id}`,
> `/operations/{id}/start`, `/operations/{id}/complete`, and `work-orders` `/operations/{id}`
> update/start/complete plus `/work-orders/{id}/complete`/`/start`/`add_operation`) — every lookup
> is scoped to the active company and a foreign id returns **404 before any mutation**.
> Traceability/analytics/OEE/scheduling/MRP services and endpoints are tenant-scoped, `/ws/updates`
> now **requires a JWT** (close 1008 otherwise) and completion broadcasts go only to the originating
> company, and MRP/scheduling ARQ jobs run **per active company** with tenant-scoped notification
> recipients. Tamper-evident `audit_log` rows are written for WO/operation start + completion,
> shipment-close (`mark_shipped` → WO CLOSED), inventory `/receive,/issue,/transfer,/adjust`, and
> blocker create/update/resolve.
>
> The three residual cross-tenant leaks flagged during the pass were also closed: `clock_out`'s
> WO/operation re-fetch, the shop-floor dashboard / `get_all_operations` / active-user TimeEntry
> queries (now filter `company_id` + `is_deleted == False`), and the blocker-resume operation
> lookup. **AUD-3** (audit on reconcile-on-read transitions) is explicitly deferred to Batch 3.
>
> **Open follow-ups — all three RESOLVED 2026-06-09 on branch `fix/wo-remediation-followups`**
> (post-remediation HIGH-severity fixes, after a full code-vs-main verification sweep; tests added, gates green):
> 1. **Worker cron kwargs bug — ✅ RESOLVED (2026-06-09).** `app/worker.py` scheduled
>    `cron(run_mrp_job, …, kwargs={"mode": "AUTO_DRAFT"})`; ARQ's `cron()` accepts no `kwargs=` argument,
>    so the daily MRP cron entry was mis-wired and crashed worker startup. Fixed by adding a thin
>    `run_mrp_auto_draft_job(ctx)` wrapper that pins `mode="AUTO_DRAFT"` (delegating to `run_mrp_job`,
>    which fans out per active company), registered in both `functions` and `cron_jobs`
>    (`cron(run_mrp_auto_draft_job, hour=6, minute=0)`). The per-company fan-out in
>    `run_mrp_task`/`run_scheduling_task` was already correct.
> 2. **`mrp_auto_service` field-name drift — ✅ RESOLVED (2026-06-09).** `MRPAutoService._create_po`
>    constructed `PurchaseOrderLine(po_id=…, quantity=…, unit_cost=…)` using columns that do not exist on
>    the `app.models.purchasing` model, raising a `TypeError` at runtime. Corrected to the real columns —
>    `purchase_order_id`, `quantity_ordered`, `unit_price` — and now also sets `line_total`
>    (`quantity_ordered × unit_price`) and `company_id`.
> 3. **Periodic `notification_jobs.py` cross-tenant scope — ✅ RESOLVED (2026-06-09).** The four daily
>    digest jobs (calibrations, late work orders, low stock, quote expiry) queried their entities
>    **globally** and called `get_notification_recipients` **without** `company_id`, so a single cron run
>    notified every tenant's users about other tenants' overdue work, low stock, due calibrations and
>    expiring quotes (invariant #1 violation). Each job now iterates the active companies (new
>    `_active_company_ids` helper) and runs one isolated, tenant-scoped pass per company — every entity
>    query filters `company_id` (work orders also `is_deleted == False`) and `get_notification_recipients`
>    is passed `company_id`; one tenant's digest failure is caught and logged without aborting the rest.
>    **Three latent runtime bugs were fixed in passing:** (a) the calibrations digest queried a
>    non-existent `Calibration` class → now `Equipment` (with `CalibrationStatus.ACTIVE` and `.name`);
>    (b) the low-stock digest filtered on `InventoryItem.reorder_point`, which doesn't exist → now joins
>    `Part` and compares `quantity_on_hand <= Part.reorder_point` (soft-delete-filtered); (c) the
>    quote-expiry digest called `.date()` on a `date` (`quote.valid_until`) → now
>    `(quote.valid_until - now.date()).days`.

### Rank 5 — Serialize concurrent completion writes ☑ (Batch 2 · migration)
Immediate: `.with_for_update()` re-fetch before the over-completion read-modify-write in `clock_out`/`/production`/`/complete`. Structural: map `__mapper_args__={'version_id_col': version}` **targeted on `WorkOrderOperation` and `TimeEntry` only** (NOT on the shared `OptimisticLockMixin`, which intentionally stays inert so enabling native version_id_col globally doesn't change commit behavior for every consumer of the mixin — see `app/db/mixins.py`); translate `StaleDataError`→409. Migration: partial unique index `uq_open_time_entry ON time_entries(user_id, operation_id) WHERE clock_out IS NULL` (pre-flight dedupe; idempotent/reversible).
Findings: SFI-1, SFI-2, SFI-3, LOCK-1.

> **Batch 2 status (2026-06-07, rank 5 landed).** Optimistic locking is now **enforced on the
> completion write path**. `version_id_col` is mapped per-model on `WorkOrderOperation`
> (`app/models/work_order.py`) and `TimeEntry` (`app/models/time_entry.py`) — *not* on the shared
> `OptimisticLockMixin`, which remains deliberately inert (it declares the `version` column for
> application-managed comparison without SQLAlchemy enforcement; the docstring in `app/db/mixins.py`
> records why). A concurrent stale UPDATE of a work-order operation or time entry now raises
> `StaleDataError`, which the endpoint layer translates to **HTTP 409 Conflict** ("modified
> concurrently, refresh and retry") on the completion/clock paths: `/shop-floor/clock-in`,
> `/clock-out/{id}`, `/operations/{id}/start`, `/operations/{id}/production`,
> `/operations/{id}/complete`, and `work-orders` `/operations/{id}` (PUT) / `/operations/{id}/start`
> / `/operations/{id}/complete` plus `/work-orders/{id}/complete`. Row locks
> (`SELECT … FOR UPDATE`) now serialize the over-completion read-modify-write on those paths,
> closing the lost-update race. Duplicate open clock-in is **DB-enforced** by a new partial unique
> index `uq_open_time_entry ON time_entries(user_id, operation_id) WHERE clock_out IS NULL` — a
> racing double clock-in is rejected with the **HTTP 400** "already clocked in to this operation"
> (the `IntegrityError` on that index is the only one mapped to the 400; others surface as their own
> error).
>
> **Migrations:** `038_optimistic_lock_backfill` (backfills/normalizes the `version` column to a
> non-null managed value so the version_id_col mapping is provably safe before any locked write
> runs; idempotent + reversible — backfill downgrade is a documented no-op, the column lifecycle
> stays owned by `004_add_optimistic_locking`) and `039_uq_open_time_entry` (non-destructive
> pre-flight dedupe that **closes** older duplicate open entries — `clock_out=clock_in`,
> `duration_hours=0`, **preserving** `quantity_produced` and the rows themselves — then builds the
> index with `CREATE INDEX CONCURRENTLY`; idempotent + reversible; logs the closed-row ids to the
> deploy/migration output for AS9100D labor traceability rather than to the tamper-evident
> `audit_log`). See `docs/DEVELOPMENT.md` → Database Migrations for the bootstrap-path caveat.
>
> **Residual follow-up A1 — ✅ RESOLVED (2026-06-09, branch `fix/wo-followups-round2`).**
> `audit_log.sequence_number` was allocated by `max()+1` and was **not** serialized by the `FOR UPDATE`
> row locks (those lock the operation / time-entry / work-order rows, not the audit table), so two
> concurrent audit writes on a hot completion path could collide on the unique `sequence_number`, raise
> an uncaught `IntegrityError`, **poison the caller's transaction**, and surface as an occasional **500**
> — a pre-existing race **worsened** by the completion-path audit sites added in Batch 1 (and again by
> the Batch 6 read-path inventory audits). `AuditService.log()` (`app/services/audit_service.py`) now
> serializes the global chain's allocate+insert critical section: **(1)** a transaction-level Postgres
> advisory lock (`pg_advisory_xact_lock`, dialect-guarded — no-op on SQLite, which already serializes
> writers) is taken **before** reading the chain tail, so only one writer at a time reads the tail,
> allocates the next `sequence_number`, and inserts; and **(2)** the tail-read → allocate → hash →
> INSERT runs inside a **SAVEPOINT** (`begin_nested`) with a bounded retry (`_MAX_SEQUENCE_RETRIES = 5`)
> — a residual unique-`sequence_number` collision rolls back **only** the savepoint (leaving the
> caller's outer transaction usable), then re-reads the tail and retries with a freshly built entry.
> `db.add` lives **inside** the savepoint because `begin_nested` autoflushes on open. The tamper-evident
> hash-chain semantics (`sequence_number` / `previous_hash` / `integrity_hash`) are **unchanged**; this
> only serializes allocation so concurrent audited writes can no longer collide/500 or poison the
> transaction. If every retry collides, `log()` degrades to its existing best-effort contract
> (log + return `None`) with the session left un-poisoned.

### Rank 6 — Consolidate op-complete → WO-rollup into one shared finalizer ☑ (Batch 3 · keystone)
File: `work_order_state_service.py` (+ both endpoints). `finalize_operation_completion(db, wo, op, user, company_id)`: remaining-ops count (reuse loaded relationship), COMPLETE-vs-release branch, `max()`-guarded qty floored at TimeEntry evidence, always stamp `actual_start`, one absolute-vs-additive contract per verb, stop zeroing scrap from defaulted-0 param, self-healing READY release via `has_incomplete_predecessors`, populate/drop `current_operation_id`, return affected work_center_ids. `complete_work_order` delegates. Consistent ON_HOLD policy.
Findings: DUP-2, DUP-3, DUP-4, DUP-5, SFI-4, SFI-5, RUP-1, RUP-4, RUP-6, QG-5, BLK-1.

> **Batch 3 status (2026-06-07, rank 6 landed).** The op-complete → WO-rollup logic is now a
> **single shared finalizer**, `finalize_operation_completion(db, wo, op)` in
> `app/services/work_order_state_service.py`. Every completion path delegates to it — both
> `/operations/{id}/complete` endpoints (shop-floor and office), the additive verbs (`clock_out`,
> `/shop-floor/operations/{id}/production`), and the privileged `/work-orders/{id}/complete`
> override (which force-completes each still-open op through the finalizer rather than blind-flipping
> the WO). The three former inline copies can no longer drift (DUP-2 … DUP-5).
>
> **Unified completion contract:**
> - **Quantity (RUP-6 / SFI-5 / DUP-3).** Absolute verbs (both `/operations/{id}/complete`) store
>   `clamp(max(existing, requested, durable TimeEntry produced-evidence sum), 0, target)` via
>   `resolve_absolute_operation_quantity` — never below the value already stored, never below
>   recorded production evidence, never above the operation target. Additive verbs (`clock_out`,
>   `/production`) compute `existing + delta` then pass it through
>   `floor_operation_quantity_at_evidence` (same evidence floor + target cap). Work-order
>   `quantity_complete` is rolled up with a `max()` guard so an out-of-sequence / earlier-stage op
>   can never pull finished quantity backward.
> - **Scrap (DUP-3).** The office `complete_operation` `quantity_scrapped` param and the
>   `complete_work_order` `quantity_scrapped` param are now **optional** — an omitted value is left
>   untouched, so a defaulted call can no longer zero previously-accumulated operation / WO scrap.
>   Pass an explicit value (including `0`) to overwrite.
> - **ON_HOLD (QG-5 / BLK-1).** Completing an ON_HOLD operation is **refused with 409** on BOTH
>   `/operations/{id}/complete` endpoints ("Operation is on hold and cannot be completed"), and
>   `/work-orders/{id}/complete` returns **409** up front if any open operation is on hold ("…is on
>   hold; resolve the hold first"). The completion path no longer silently lifts a quality / material
>   hold. (Full quality-gate enforcement — inspection / NCR / FAI / blocker — is Batch 4 / rank 7.)
> - **Timestamps (DUP-2 / RUP-1).** A WO reaching COMPLETE always has BOTH `actual_start` and
>   `actual_end` set, with `actual_start` clamped `≤ actual_end` (no negative cycle time). The
>   finalizer maintains `current_operation_id` throughout: it points at the active/next operation
>   while the route is open and is cleared when the WO reaches COMPLETE.
>
> **AUD-3 closed.** Reconcile-on-read status transitions — an operation / WO driven to COMPLETE from
> durable TimeEntry evidence when a list / detail / dashboard endpoint is loaded — now write a
> tamper-evident `audit_log` status-change row **attributed to the requesting user** and tagged
> `extra_data.source = "reconcile_on_read"`. The reconcile itself has no actor, so
> `reconcile_work_orders_from_completion_evidence` returns the transitions (as `StatusTransition`
> records, enriched with the contributing `time_entry_ids`) and the read handler emits the audit rows
> before its commit. Reconcile-on-read remains **best-effort**: `_reconcile_and_commit` catches
> `SQLAlchemyError` broadly, rolls back the reconcile mutation **and** its audit rows atomically, and
> still serves the read with a 200 — a benign version race or a poisoned session (e.g. an
> `audit_log.sequence_number` collision) can never turn a GET into a 500.
>
> **Open follow-ups (tracked, not yet fixed):**
> 1. **A1 — root `audit_log.sequence_number` race (carried over from Batch 2) — ✅ RESOLVED
>    (2026-06-09, branch `fix/wo-followups-round2`).** Batch 3 only guaranteed that reconcile-on-read
>    *reads* didn't 500 when the collision happened (it swallowed the failure and dropped the redundant
>    write); the hot completion-path writers could still collide on the unique `sequence_number`. The
>    `max()+1` allocation is now serialized in `AuditService.log()` (advisory lock + savepoint/retry) —
>    see the resolved A1 note under Rank 5 (Batch 2) for the full description.
> 2. **Rank-12 tension — AUD-3 keeps mutation + audit on the read path.** The planned rank-12
>    "bound the dashboard reconcile / move it to a debounced ARQ job" (Batch 9) must **carry the
>    audit emission with it**: once reconcile no longer runs under the requesting user it has to
>    re-attribute the `reconcile_on_read` status-change rows to a **system actor** rather than
>    `current_user`. Do not drop the audit when moving reconcile off the read path.
> 3. **Batch-4 dependency — ON_HOLD policy.** Batch 3 makes the endpoints *consistently refuse* an
>    ON_HOLD completion (409). Batch 4 adopted **warn-and-record** rather than a blocking gate, so the
>    originally-planned "audited QUALITY-role override that clears a hold during completion" was **not
>    built** — Batch 4 records (does not clear) an unsatisfied gate, and the ON_HOLD 409 refusal from
>    Batch 3 stands unchanged. See the Batch 4 status note under Rank 7.

### Rank 7 — Quality gates on completion (warn-and-record) ☑ (Batch 4)
> **Posture change from the original plan.** The original action proposed a hard block
> (`assert_operation_completable(op)` refusing COMPLETE) with an audited QUALITY-role override. The
> product owner chose **warn-and-record instead of blocking**: completion still succeeds, but every
> unsatisfied gate leaves a tamper-evident `audit_log` row + a warning event and is returned on the
> response. No override path is needed because nothing is blocked. The detection logic lives in
> `app/services/quality_gate_service.py`; ship the missing `inspection_complete` writer (QG-2);
> surface still-open blockers on completion/resume.
Findings: QG-1, QG-2, QG-3, QG-4, QG-5, BLK-1, BLK-2, BLK-4.

> **Batch 4 status (2026-06-07, rank 7 landed — warn-and-record).** Quality gates are now evaluated
> on every completion but **do not block** it. When an operation or work order completes while a gate
> is unsatisfied, completion still succeeds (**200**) and the system, in the same unit of work that
> commits the completion:
> 1. writes ONE tamper-evident `audit_log` row with action **`COMPLETED_WITH_QUALITY_EXCEPTION`** (a
>    distinct, greppable verb — not a plain `COMPLETE`) carrying the exception codes + offending
>    record references in `extra_data`;
> 2. emits ONE warning `OperationalEvent` (`event_type = "quality_exception_on_completion"`,
>    `severity = "warning"`) for AI / realtime context; and
> 3. returns the exceptions on a new `quality_exceptions` response field (default `[]`).
>
> All detection is **read-only and tenant-scoped** (`app/services/quality_gate_service.py`); it never
> mutates a row and never raises on a failed gate. The single entry point
> `evaluate_and_record_completion_quality_exceptions(...)` runs on the live (locked-row) completion
> paths: shop-floor and office `complete_operation` (on a *true* completion only), `clock_out` when it
> completes an op/WO, and `complete_work_order` (which gathers the WO-grain NCR/FAI/blocker gates once
> plus one inspection gate per still-open operation). The inspection-only gate also runs on the
> **reconcile-on-read** path.
>
> **Gates (codes):**
> - `inspection_incomplete` (QG-1) — `operation.requires_inspection and not operation.inspection_complete`. Evaluated off the already-loaded operation row (no extra query); this is the one gate cheap enough to also run on the reconcile/read path.
> - `open_ncr` (QG-3) — an NCR on the WO whose status is not `CLOSED`/`VOID`, or whose disposition is still `PENDING`. Company-scoped.
> - `fai_not_passed` (QG-3) — a First Article Inspection on the WO whose status is not `PASSED`. Company-scoped.
> - `open_blocker` (BLK-2) — an `OPEN`/`ACKNOWLEDGED` `WorkOrderBlocker` on the operation or work order. Company-scoped.
>
> **QG-2 — the missing `inspection_complete` writer shipped.** New audited, tenant-scoped, RBAC-gated
> endpoint **`POST /api/v1/shop-floor/operations/{operation_id}/inspection`** (`mark_operation_inspected`)
> sets `inspection_complete = True` (and an optional `inspection_type`), writes a
> `MARK_OPERATION_INSPECTED` audit row, and emits an `operation_inspected` event — so the
> `inspection_incomplete` gate can actually be CLEARED. Role-gated to **ADMIN / MANAGER / SUPERVISOR /
> QUALITY** (this repo has no separate `INSPECTOR` role).
>
> **BLK-4 — `resume_operation` surfaces open blockers.** Resuming an on-hold operation does not
> resolve its blocker(s); `/operations/{id}/resume` now returns any still-`OPEN`/`ACKNOWLEDGED`
> blocker on the operation in an `open_blockers` array (and records their ids in the resume audit
> row's `extra_data`), so the operator/dashboard is warned that operation status and blocker status
> are diverging.
>
> **Response shape.** All completion responses carry
> `quality_exceptions: list[{code, message, reference_type, reference_id, severity}]` (default `[]`):
> shop-floor + office `complete_operation`, `complete_work_order`, and `clock_out`'s
> `TimeEntryResponse` (`QualityExceptionInfo` in `app/schemas/work_order.py`). Backward-compatible — an
> all-clear completion returns an empty list, indistinguishable from the pre-Batch-4 shape.
>
> **Deferrals / follow-ups (tracked, not fixed in Batch 4):**
> 1. **`fai_not_passed` cannot detect a *missing-but-required* FAI.** The FAI model carries no
>    "FAI required" flag (and no `operation_id`), so the gate only fires when an FAI *exists* and is
>    not passed — a required FAI that was never created is invisible to it.
> 2. **FAI-pass → `inspection_complete` auto-wire deferred.** An FAI passing cannot auto-clear an
>    operation's inspection gate, because there is no FAI↔operation FK to link them. Auto-wiring
>    requires a schema change (database-migration-specialist); until then the manual
>    `mark_operation_inspected` writer is the only clear path. Acceptable under warn-and-record because
>    nothing is blocked.
> 3. **QG-4 reconcile coverage is partial.** A completion can happen on a GET via reconcile-on-read;
>    that path records only `inspection_incomplete` (the cheapest, no-extra-query gate). The
>    NCR/FAI/blocker gates (which need extra queries) are **not** evaluated on the read path — they are
>    caught on the next live completion / WO-complete. Documented partial coverage.
> 4. **Defense-in-depth — `record_reconcile_inspection_exception` self-scope on `company_id`.** It
>    currently derives the company from the operation row it loads (`operation.company_id`) rather than
>    re-deriving it from the caller's active company; harden it to self-scope so a future caller can't
>    record against the wrong tenant.

### Rank 8 — Uniform completion signal set from the finalizer ☑ (Batch 5)
Emit `operation_completed`/`work_order_completed`/`work_order_closed` events; tenant-scoped broadcasts; enqueue ARQ → `NotificationService.WO_COMPLETED` + `WebhookService.dispatch_event('work_order.completed')` (scope `get_webhooks_for_event` first); refresh scheduling on `complete_work_order`+reconcile; reconcile returns transitions so read handlers emit events/audit.
Findings: EVT-1, EVT-2, EVT-3, EVT-4, EVT-5, MS-2.

> **Batch 5 status (2026-06-07, rank 8 landed).** Completion now emits a **uniform signal set**
> across every completion path, split into in-process events (always) and outbound dispatch
> (live paths only).
>
> **In-process `OperationalEvent`s (EVT-1/EVT-2/EVT-4).** Every completion path emits a tenant-scoped,
> best-effort event via the helpers in `app/services/completion_signal_service.py`
> (`emit_operation_completed_event` / `emit_work_order_completed_event`) and, for closure, an inline
> `OperationalEventService.emit` in `shipping.py`:
> - `operation_completed` — on op COMPLETE (`source_module = "shop_floor"` / live completion path);
> - `work_order_completed` — on WO COMPLETE;
> - `work_order_closed` — on the shipping close (`mark_shipped`, distinct from the existing
>   `shipment_shipped` event so AI/realtime consumers see the WO closure itself).
>
> Reconcile-on-read (`_emit_reconcile_events` in `shop_floor.py`) emits the **same** in-process
> `operation_completed` / `work_order_completed` events with `source_module = "reconcile_on_read"`, so
> reconcile-driven completions are not invisible to consumers — but it does **NOT** fire outbound
> notifications/webhooks (a read must not have outbound side-effects).
>
> **Outbound notifications + webhooks (EVT-3) on WO COMPLETE / CLOSED.** The live completion handlers
> call `enqueue_work_order_completion_signals(...)` (`completion_signal_service.py`) **after commit,
> best-effort**, which enqueues the ARQ job `dispatch_work_order_completion_signals_job`
> (`app/worker.py` → `dispatch_work_order_completion_signals_task` in
> `app/jobs/completion_signal_jobs.py`). A signal failure never fails a completion, and nothing fires
> for a rolled-back completion (enqueue is past the `db.commit()`). In the worker, with its own DB
> session:
> - `NotificationService.send_notification(WO_COMPLETED, …)` to the tenant's recipients
>   (SUPERVISOR + MANAGER in the active company, plus the WO creator) — every recipient query is
>   company-scoped, so a completion in one tenant never notifies another tenant's users; and
> - `WebhookService.dispatch_event("work_order.completed" | "work_order.closed", …, company_id=…)`.
>
> **Tenant-scoped, CUI-minimized webhooks (EVT-3).** `get_webhooks_for_event` / `dispatch_event` /
> `create_webhook` are all company-scoped (`app/services/webhook_service.py`); `dispatch_event` now
> **requires** `company_id` and raises if called unscoped, so a tenant only ever dispatches to its OWN
> registered endpoints and `WebhookDelivery` rows are tenant-stamped (invariant #1). The outbound
> webhook payload is the **minimal, redacted** set — `work_order_id`, `work_order_number`, `part_id`,
> `status`, `quantity_complete`, `quantity_scrapped`, `company_id`, `completed_at` — run through
> `redact_event_payload` as a belt-and-suspenders pass. It **intentionally OMITS** `customer_name` and
> free-text/notes (CUI minimization, since the payload egresses to a subscriber-controlled URL);
> subscribers re-fetch detail via the authenticated API keyed on `work_order_id`. The *internal*
> notification payload stays inside the tenant (email to the company's own users) and may carry richer
> context like `customer_name`.
>
> **Idempotency (EVT-1/EVT-3).** `mark_shipped` and `complete_work_order` are now idempotent: a
> re-invocation on an already-terminal shipment/WO returns `already_shipped` / `already_completed` and
> fires **no** second close/audit/event/signal — the close/audit/event/enqueue block runs exactly once
> per real transition.
>
> **Scheduling refresh (MS-2).** `complete_work_order` and the reconcile-driven WO completion
> (`_refresh_reconcile_scheduling` in `shop_floor.py`/`work_orders.py`) now refresh cached work-center
> availability (`SchedulingService.update_availability_rates`) for the affected work centers, so a
> completed WO's capacity is released rather than left stranded.
>
> **Follow-ups (tracked, not fixed in Batch 5):**
> 1. **Reconcile outbound notify/webhook deferred to rank 12 (Batch 9).** Reconcile-on-read emits the
>    in-process events but deliberately fires no outbound notification/webhook (no outbound I/O on a
>    read). When rank 12 moves reconcile to a debounced ARQ job, the outbound dispatch can move with it
>    — and at that point the signal must be **re-attributed to a system actor** (reconcile has no
>    requesting user under a background job), mirroring the AUD-3 re-attribution follow-up.
> 2. **Richer webhook payload is an explicit opt-in / data-classification decision.** The minimized
>    payload is the default by design; adding `customer_name` or any free-text field to an egressing
>    webhook must be a deliberate classification call, not a quiet default.
> 3. **No webhook-admin HTTP endpoint yet.** Webhooks are created/registered via `WebhookService`
>    (seeded through the service), not through a tenant-facing REST endpoint — there is no
>    `POST /api/v1/webhooks` route. Subscribers cannot yet self-register; track a CRUD endpoint
>    (RBAC-gated) separately.
> 4. **`mark_shipped` now `require_role`-gated — ✅ RESOLVED (2026-06-09, branch
>    `fix/wo-followups-round2`).** `POST /shipping/{shipment_id}/ship` (`mark_shipped`) previously
>    depended only on `get_current_user` + `get_current_company_id`, so any authenticated tenant user
>    could close a WO by shipping it. It now depends on
>    `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])`, aligned to the documented Shipping
>    **"Complete"** permission in `docs/RBAC_PERMISSIONS.md` (marking shipped is the terminal shipping
>    action that CLOSES the work order). **Intentional behavior change:** a non-privileged user now gets
>    **403** on this endpoint. See `docs/API.md` → Shipping and `docs/RBAC_PERMISSIONS.md` → Shipping.

### Rank 9 — FG receipt + backflush + as-built genealogy on completion ☑ (Batch 6 · data-sensitive)
In finalizer on WO COMPLETE: assign FG lot/serial; create/increment FG `InventoryItem` + RECEIVE txn; backflush components (ISSUE txns carrying consumed lot, apply `scrap_factor`, per-part flag); route through `get_audit_service`. **Idempotent** (reconcile re-enters). Mirror trace into `trace_serial`; populate MRP `on_order`.
Findings: INV-1, INV-2, INV-3, TRACE-2, TRACE-3, TRACE-4, TRACE-5, MS-4.

> **Batch 6 status (2026-06-07, rank 9 landed).** Work-order completion now **moves inventory**. The
> inventory side-effects live in `app/services/completion_inventory_service.py`
> (`apply_completion_inventory_effects`), invoked on all **four live completion paths** (office +
> shop-floor `complete_operation`, `clock_out`, `complete_work_order`) — atomic with the completion,
> the handler owns the commit — and on the **reconcile-on-read** path (best-effort, read-safe; a
> duplicate insert rolls back only a SAVEPOINT and can never turn a GET into a 500). None of these
> functions commit; they join the caller's unit of work.
>
> **FG receipt (INV-1 / TRACE-3) — ALWAYS.** On WO COMPLETE the system performs a finished-goods
> RECEIVE: it assigns `work_order.lot_number` if empty (`LOT-<work_order_number>`, de-collided
> per-company with a `-NN` suffix), creates or increments an `InventoryItem` at warehouse **`MAIN`** /
> location **`FINISHED-GOODS`** for `work_order.part_id` and quantity `quantity_complete`, writes a
> positive `RECEIVE` `InventoryTransaction` (`reference_type='work_order'`, `reference_id=<wo>`) with
> `unit_cost = part.standard_cost`, and **audits** the movement on the tamper-evident hash chain via
> `AuditService` (INV-4). Lot-only — `InventoryItem.serial_number` is left NULL (no Part serialization
> flag exists yet; serial assignment is a deferred follow-up). A fully-scrapped WO (zero
> `quantity_complete`) receives nothing.
>
> **Component backflush (INV-2) — OPT-IN, default OFF.** Only when the finished part's
> `parts.backflush_components` flag is **True** (migration `040`, default FALSE) does completion
> auto-consume the BOM components: one negative `ISSUE` `InventoryTransaction` per component (quantity
> scaled by produced qty and `BOMItem.scrap_factor`, resolved from explicit WO-operation component
> demand first, else by exploding the active BOM), decrementing source stock, each **audited**, and
> carrying the consumed source lot on the txn for genealogy. The default-OFF posture exists so material
> a shop issued manually is never double-consumed. **A shortage never fails completion** — the primary
> source lot is driven negative (matching the permissive manual `/inventory/adjust` behavior), and the
> gap is now recorded as a tamper-evident `BACKFLUSH_SHORTAGE` `audit_log` row **plus** a
> `backflush_shortage` warning `OperationalEvent` (item 3) — captured on both the live and reconcile
> paths.
>
> **As-built genealogy (INV-3 / TRACE-2 / TRACE-5).** `GET /traceability/lot/{lot}` (`trace_lot`) now
> reconstructs the second hop: from the FG lot it finds the producing WO (the WO-referenced RECEIVE),
> then enumerates that WO's component ISSUE txns and returns them as a new `consumed_components`
> section (component part / lot / quantity), so a single trace reconstructs the as-built genealogy.
> `GET /traceability/serial/{serial}` (`trace_serial`) now mirrors `trace_lot`'s WO + NCR collection
> (TRACE-4). All queries are tenant-scoped (invariant #1).
>
> **MRP `on_order` (MS-4).** `MRPService.get_inventory_summary` now populates `on_order` from the
> remaining output (`quantity_ordered − quantity_complete`) of the tenant's **RELEASED / IN_PROGRESS**
> make-WOs that produce the part. **COMPLETE WOs are excluded** — their output is now received into
> `InventoryItem` on completion (INV-1) and so is already counted in `on_hand`; counting it here too
> would double it.
>
> **DB-enforced idempotency.** At most **one FG RECEIVE per (company, work order)** and **one
> backflush ISSUE per (company, work order, component part)**. Beyond the app-level check-then-insert,
> migration `041` adds two partial UNIQUE indexes on `inventory_transactions` that back the exact
> idempotency keys, so a concurrent double-receive / double-issue race (two reconcile GETs, or a live
> completion racing a reconcile GET) loses on an `IntegrityError` the service catches as a clean no-op
> (no double-count). Each insert is wrapped in a SAVEPOINT so the loser rolls back only the savepoint,
> never the outer completion/reconcile transaction.
>
> **Migrations:** `040_add_part_backflush_flag` (the opt-in `parts.backflush_components` boolean,
> NOT NULL DEFAULT false; metadata-only add) and `041_uq_wo_inventory_idempotency` (the two partial
> UNIQUE indexes, built `CONCURRENTLY` in an autocommit block; idempotent + reversible). **`041`'s
> pre-flight duplicate guard fails LOUDLY** — it lists the offending `(company_id, reference_id[,
> part_id])` groups and **raises** rather than deleting any inventory rows (inventory transactions are
> regulated traceability records; silent dedup is not acceptable). See `docs/DEVELOPMENT.md` →
> Database Migrations.
>
> **Follow-ups (tracked, not fixed in Batch 6):**
> 1. **Serial assignment deferred.** FG receipt is lot-only; assigning a serial needs a Part
>    serialization flag (no schema field exists yet). `InventoryItem.serial_number` stays NULL until
>    then.
> 2. **Multi-lot FIFO backflush deferred.** A component is consumed by exactly ONE ISSUE per WO against
>    the primary (lowest-id on-hand) source lot — not a multi-lot FIFO split. The full required
>    quantity rides one lot; when that lot is insufficient it goes negative. A FIFO/multi-lot
>    consumption is a tracked follow-up.
> 3. **Reconcile-path inventory writes should move to the ARQ reconcile job (rank 12 / Batch 9).** FG
>    receipt + backflush currently run inline (best-effort) on reconcile-on-read; when reconcile moves
>    to a debounced background job the inventory writes should move with it (and re-attribute to a
>    system actor), mirroring the AUD-3 / EVT-4 reconcile-off-read follow-ups.
> 4. **A1 (`audit_log.sequence_number` race) was amplified by read-path inventory audits — ✅ RESOLVED
>    (2026-06-09, branch `fix/wo-followups-round2`).** Each FG receipt / backflush writes additional
>    `audit_log` rows (on the reconcile-on-read path, under a GET), which increased the volume of
>    concurrent audit writers that could collide on the unserialized `max()+1` `sequence_number`. The
>    allocation is now serialized in `AuditService.log()` (advisory lock + savepoint/retry) — see the
>    resolved A1 note under Rank 5 (Batch 2).
>
> ⚠️ **Auditor sign-off — negative-stock-on-shortage posture.** A backflush that exceeds available
> stock **drives the primary source lot negative and still completes the work order** (the demand is
> recorded; nothing blocks). This is deliberate (it matches the existing permissive `/inventory/adjust`
> behavior and avoids a backflush ever blocking production) and the shortage is now tamper-evidently
> recorded — but a negative on-hand is a material-trail condition a quality/compliance owner should
> review and explicitly accept for AS9100D/CMMC-L2.

### Rank 10 — Labor-hour + job/actual-cost rollup ☑ (Batch 7 · opt-in)
Accumulate auto-closed TimeEntry `duration_hours` into op/WO actual hours; extract `JobCostingService.recompute_from_time_entries`; set `JobCost.status=COMPLETED`; populate `WorkOrder.actual_cost`/`estimated_cost` (or repoint report to JobCost); single configurable labor rate replacing hardcoded $45/$50.
Findings: COST-1, COST-2, COST-3, COST-4, COST-5.

> **Batch 7 status (2026-06-07, rank 10 landed — OPT-IN, default OFF).** Labor-hour and
> actual/estimated-cost rollup on completion now exists but is gated by a global feature flag
> **`LABOR_COST_ROLLUP_ENABLED`** (env var in `app/core/config.py`, default **`false`**). The flag's
> single resolution chokepoint is `labor_cost_service.is_labor_cost_rollup_enabled`. Rationale: cost
> stays opt-in until shop-floor labor check-in data is trusted — no untrusted labor figures surface as
> cost truth before a shop validates them.
>
> **Flag-OFF (the default).** Completion does **not** auto-populate `actual_hours` / `actual_cost`,
> does **not** touch a linked `JobCost`, and the `/analytics/cost-analysis` report reports **$0**
> computed labor/overhead — uniformly across the live and reconcile-on-read completion paths, so no path
> leaks a non-zero labor figure flag-OFF. The on-demand `POST /job-costs/{id}/calculate` still
> recomputes from time entries regardless of the flag (the only way to materialize cost actuals
> flag-OFF).
>
> **Flag-ON.** On WO COMPLETE the finalizer rolls op/WO `actual_hours` **monotonic-up** from durable
> TimeEntry evidence (`app/services/completion_cost_service.py`), computes `WorkOrder.actual_cost` =
> **labor + issued material + overhead**, syncs the linked `JobCost` (TIME_ENTRY labor regenerated,
> variances recomputed, **status → `COMPLETED`**) via `job_costing_service.sync_job_cost_on_completion`,
> and writes ONE tamper-evident audit row for the rolled-up actuals — all atomic with the completion
> (joins the caller's unit of work). The cost-analysis report then computes labor/overhead from the same
> actuals at the same rate. **Best-effort:** a cost-side error can never fail an otherwise-valid
> completion. Wired into all four live completion paths and the reconcile-on-read path (read-safe).
>
> **Labor rate source (COST-5 — replaces hardcoded $45/$50).** ONE shared resolver
> (`app/services/labor_cost_service.py`) feeds BOTH the completion rollup and the cost-analysis report
> so the two can never disagree: labor rate is `WorkCenter.hourly_rate` per work center (cost reflects
> WHERE the work happened), falling back to env **`DEFAULT_LABOR_RATE`** (default `75.0`); overhead is
> env **`DEFAULT_OVERHEAD_RATE`** (default `0.0`), charged on actual labor hours. Hours are the **sum of
> `duration_hours` across ALL operators'** TimeEntries on an operation (multiple welders on one WO are
> summed, never deduped).
>
> **`no_labor_recorded` data-quality signal (fires regardless of the flag).** Completing a WO whose
> operation recorded **zero** labor (no TimeEntry, or only zero-duration entries) emits the Batch-4
> warn-and-record set — a tamper-evident `COMPLETED_WITH_QUALITY_EXCEPTION` audit row + a
> `quality_exception_on_completion` warning `OperationalEvent` + a `QualityException` (code
> `no_labor_recorded`, severity `medium`) on the existing `quality_exceptions` response field. It rides
> the existing Batch-4 channel rather than a parallel one, is evaluated whether or not the cost flag is
> on (it is a process/operator-accuracy signal, not a cost figure), and **never blocks** a completion.
> Helps surface missed clock-ins that would understate cost/hours.
>
> **Tenant fix (COST-2 cross-tenant hole closed).** `POST /job-costs/{id}/calculate` is now
> **tenant-scoped** (`JobCost.id == … AND JobCost.company_id == company_id`) — it previously looked up a
> JobCost by id alone and could recompute another tenant's job. The `WorkOrderOperation` lookup inside
> `recompute_from_time_entries` is likewise company-scoped.
>
> **Follow-ups (tracked, not fixed in Batch 7):**
> 1. **Promote the global flag to per-company.** `LABOR_COST_ROLLUP_ENABLED` is global because the
>    `Company` model has no settings/feature-flags column yet (see `app/models/company.py`).
>    `is_labor_cost_rollup_enabled` already accepts a `company_id` and is the single chokepoint to
>    repoint at a per-company field when one exists — a trusted shop could then enable cost rollup
>    without forcing it on every tenant.
> 2. **`estimated_cost` BOM material is best-effort.** The estimated-cost leg explodes the active BOM at
>    standard cost; when routing/BOM data is thin the corresponding leg is simply `0` (acceptable per
>    COST-1's best-effort note).
> 3. **Per-work-center overhead column is a future option.** Overhead is a single configurable default
>    today; `resolve_overhead_rate` already takes a `work_center_id` so a per-WC overhead column can be
>    threaded in without touching the rollup callers.
> 4. **Reconcile-off-read caveat (rank 12) applies to the new reconcile cost helpers too.** The
>    flag-gated hour/cost/JobCost rollup and the `no_labor_recorded` signal run inline (read-safe,
>    best-effort) on reconcile-on-read; when rank 12 / Batch 9 moves reconcile to a debounced ARQ job
>    these should move with it and re-attribute to a system actor — the same AUD-3 / EVT-4 / Batch-6
>    reconcile follow-up.
>
> ⚠️ **Finance sign-off — `DEFAULT_LABOR_RATE = 75.0` is a placeholder.** The chosen $75/hr default and
> the `$0` default overhead rate are engineering placeholders so a rate is always resolved when a work
> center has none; a finance owner should set the real shop labor + overhead rates (per work center via
> `WorkCenter.hourly_rate`, and the env fallbacks) before cost figures are relied on.

### Rank 11 — OEE/OTD metric correctness + dead auto-OEE endpoint ☑ (Batch 8 · KPIs move)
Fix `oee.py` `TimeEntry.start_time→clock_in`/`end_time→clock_out` (endpoint dead today); derive ideal cycle/good/defect properly; availability on staffed time; consistent produced/scrapped; OTD returns n/a on empty set not 100%.
Findings: OEE-1, OEE-4, OEE-5, OEE-6, OEE-7, COST-5, MS-5.

> **Batch 8 status (2026-06-07, rank 11 landed — KPI values move).** OEE and OTD now compute on one
> honest, consistent convention, and the dead auto-OEE endpoint is alive.
>
> **OEE-1 — the auto-OEE endpoint was dead; now it works.** `POST /api/v1/oee/calculate/{work_center_id}`
> (`auto_calculate_oee` in `app/api/endpoints/oee.py`) referenced `TimeEntry.start_time` / `end_time`,
> which **do not exist** on the model — every call **500'd**. It now reads `clock_in` / `clock_out`
> (preferring the stored `duration_hours`), actually consults `DowntimeEvent` (the old docstring claimed
> it did but never did), derives the ideal cycle from the routing instead of a hardcoded 60 s, and counts
> real scrap instead of assuming all-good (OEE-7). It writes/updates a real `OEERecord` for the day/shift.
>
> **OEE convention (now identical on the `/analytics/kpis` headline and the persisted `OEERecord`).**
> `OEE = Availability × Performance × Quality`, per work center, on the **staffed-time** basis:
> - **Availability** = productive-run hours ÷ **staffed (clocked) hours** at the work center. Staffed =
>   Σ `duration_hours` of **every** closed `TimeEntry` at the WC in the window (operators on the clock
>   there) — **not** the plant calendar, so idle/un-clocked time is excluded and availability is no
>   longer pinned near 1.0 (OEE-4). Productive run = (RUN+SETUP hours) − **UNPLANNED** `DowntimeEvent`
>   hours. **n/a when there is no staffed time** (genuinely uncomputable).
> - **Performance** = ideal hours ÷ productive run, cap 100%. ideal hours =
>   Σ((`quantity_produced` + `quantity_scrapped`) × `WorkOrderOperation.run_time_per_piece`) over the
>   production-bearing entries (RUN+REWORK) — i.e. **every piece run consumes a standard cycle, including
>   scrap** (scrap is discounted separately in Quality), derived from the routing, not a hardcoded
>   60 s (OEE-7). Weighting by produced+scrapped is what makes the `/analytics/kpis` headline and the
>   stored `OEERecord` agree for identical data.
> - **Quality** = good ÷ (good + scrapped), good = Σ `quantity_produced`, scrapped =
>   Σ `quantity_scrapped`, both over the production-bearing entries (RUN+REWORK) — not assumed all-good
>   (OEE-7). Pieces/scrap are counted from `PRODUCTION_BEARING_ENTRY_TYPES = [RUN, REWORK]` uniformly
>   across the availability/performance/quality/ideal-hours legs so a quantity logged on a REWORK
>   clock-out is never silently dropped (OEE-5).
>
> **OEE-6 — OTD honesty.** On-time-delivery (`_get_otd_value` in `app/services/analytics_service.py`)
> now returns **n/a (null) on an empty completed-set** instead of a fabricated 100% (no completed WO
> with a due date in the window → genuinely uncomputable, not perfect). On-time =
> `actual_end.date() <= due_date`; a **COMPLETE WO with a NULL `actual_end` counts as NOT on time** (no
> verifiable completion date), so a late job can no longer read as on-time by lacking a stamp. The
> completed-set query is now also soft-delete-filtered (`is_deleted == False`).
>
> **`KPIValue.value` is now `Optional[float]`** (`app/schemas/analytics.py`). The n/a OEE and OTD
> headlines serialize as `null`; the frontend null-guards and renders **"n/a"**. (The `OEEComponents` /
> `OEEDataPoint` chart series stay `float` and coalesce an uncomputable window to `0.0` — the honest
> n/a is surfaced on the `/analytics/kpis` headline `KPIValue`, not the chart glyph.)
>
> **MS-5 — capacity reservation released by data.** An operation reaching COMPLETE now clears its
> `scheduled_start` / `scheduled_end` (`release_operation_schedule_reservation` in
> `app/services/work_order_state_service.py`). Scheduling capacity is computed from non-COMPLETE
> operations; nulling the schedule on completion frees the reservation by **data** rather than relying on
> every reader to remember the `status != COMPLETE` predicate, so any future/third-party query over
> scheduled operations can't double-count finished work as still-reserved capacity.
>
> **OEE-write RBAC tightened (closes the review follow-up).** The OEE **write/mutation** endpoints —
> `POST /oee/calculate/{wc}`, `POST/PUT/DELETE /oee/records`, `POST/PUT/DELETE /oee/targets` — now
> require **ADMIN / MANAGER / SUPERVISOR** (`require_role(OEE_WRITE_ROLES)`), matching the sibling
> Analytics-write posture; they were previously open to **any** authenticated user. OEE **read**
> endpoints (`/oee/dashboard`, `/oee/trends`, `/oee/six-big-losses/{wc}`, list/get `/oee/records` and
> `/oee/targets`) stay on `get_current_user` so operators can still view dashboards (read-broad /
> write-restricted, per `docs/RBAC_PERMISSIONS.md`). See `docs/API.md` → OEE Tracking and
> `docs/RBAC_PERMISSIONS.md` → OEE.
>
> **Follow-ups (tracked):**
> 1. **`OEERecord` / `OEETarget` writes are now audited — ✅ RESOLVED (2026-06-09, branch
>    `fix/wo-followups-round2`).** The OEE write endpoints in `app/api/endpoints/oee.py` —
>    `create_oee_record`, `update_oee_record`, `delete_oee_record`, the `auto_calculate_oee` create/update
>    branches, and `create_oee_target` (create + update) / `update_oee_target` / `delete_oee_target` — now
>    write a tamper-evident `audit_log` row via `AuditService` (`get_audit_service` dependency;
>    `log_create` / `log_update` / `log_delete`). Each takes a pre-mutation column snapshot for the diff,
>    `db.flush()`es so the PK is populated, and logs **before** the terminal `db.commit()` so the audit row
>    commits atomically with the record/target. The auto-calc upsert writes one representative row per call.
>    The Batch-8 RBAC gate closed the access-control half; this closes the audit-trail half (resource types
>    `oee_record` / `oee_target`).
> 2. **Manual `POST /oee/records` keeps the legacy planned-time availability formula.** The *manual*
>    record-entry path (`calculate_oee` helper) still computes Availability = `actual_run_time ÷
>    planned_production_time` from the operator-supplied fields, because those are hand-entered inputs,
>    not the staffed-time derivation. Only the **auto-calc** path and the `/analytics/kpis` headline use
>    the staffed-time convention. A shop that enters OEE records by hand should understand the two paths
>    use different availability bases.

### Rank 12 — Indexes + de-risk reconcile-on-read ☑ (Batch 9 · migration)
Migration: `ix_time_entries_operation_clock_out`, `ix_woo_work_order_sequence` (CONCURRENTLY, idempotent). Bound the dashboard reconcile (no `.limit()` today) / move to debounced ARQ; compute ETag before reconcile; grouped predecessor query; `commit=False` on `update_availability_rates`.
Findings: PERF-1, PERF-2, PERF-3, PERF-4, PERF-5.

> **Batch 9 status (2026-06-08, rank 12 landed).** The work-order completion / reconcile-on-read read
> path is now indexed, cheaper to poll, atomicity-corrected, and bounded — without moving reconcile
> off the read path (that remains the deferred initiative; see below). All five PERF findings landed.
>
> **PERF-1 — supporting indexes (migration `042`).** New migration
> `042_wo_completion_perf_indexes` adds two **non-unique** btree indexes that back the hot completion
> query shapes that previously fell to sequential scans on high-row tables:
> - `ix_time_entries_operation_clock_out` on `time_entries(operation_id, clock_out)` — backs
>   `reconcile_work_orders_from_completion_evidence`'s per-operation production/scrap rollups
>   (`WHERE operation_id IN (…) GROUP BY operation_id`), the closed-only rollup
>   (`… AND clock_out IS NOT NULL` — covered by the trailing column), and the latest-entry scan
>   (`… ORDER BY operation_id, clock_out DESC` — both ORDER BY columns covered, no sort).
> - `ix_woo_work_order_sequence` on `work_order_operations(work_order_id, sequence)` — backs
>   `has_incomplete_predecessors` (`WHERE work_order_id = ? AND sequence < ?`) and
>   `release_next_ready_operation` (`WHERE work_order_id = ? ORDER BY sequence`).
>
> Unlike `041`'s partial UNIQUE indexes these enforce **no invariant** (pure read-path speedups), so
> there is **no pre-flight duplicate guard** — there is nothing to validate and the build cannot fail
> on existing data. Both are built with `CREATE INDEX CONCURRENTLY` inside an `autocommit_block()` to
> avoid the ACCESS EXCLUSIVE lock a plain `CREATE INDEX` would take on these high-write tables during
> deploy; the downgrade drops them CONCURRENTLY too. Idempotent and reversible, and **self-healing**
> against an interrupted CONCURRENTLY build: `_index_validity` reads `pg_index.indisvalid` (an aborted
> build leaves an INVALID index that a plain existence probe — and `if_not_exists` — would mask and
> never rebuild), and `_ensure_index` drops a found-INVALID index CONCURRENTLY before recreating it,
> so a killed deploy can't latch the table onto a dead index (no read speedup, write-time cost). The
> SQLite local-create_all / pytest path is skipped
> gracefully (CONCURRENTLY is Postgres-only; `create_all` already emits both indexes). The indexes are
> declared **in lock-step on the model `__table_args__`** — `TimeEntry.__table_args__`
> (`app/models/time_entry.py`) and `WorkOrderOperation.__table_args__` (`app/models/work_order.py`) —
> so the `create_all` bootstrap path produces them byte-for-byte (the `041` precedent). No
> deploy-ordering constraint (metadata-only; touches no tenant-isolation / audit / soft-delete
> behavior). See `docs/DEVELOPMENT.md` → Database Migrations.
>
> **PERF-2 — cheap pre-reconcile ETag + fast 304.** `GET /shop-floor/dashboard`'s ETag is now a cheap
> **state fingerprint computed BEFORE the reconcile** (`_dashboard_state_fingerprint` in
> `app/api/endpoints/shop_floor.py`), replacing the old "md5 of the fully-built payload" ETag that
> forced every poll — even an unchanged one destined to 304 — to pay for the write-amplifying reconcile
> AND the whole payload build before it could short-circuit. The fingerprint is an md5 over
> `{ today (UTC date.today()); central_today (Central-Time date); per-table (count, max(updated_at)) for
> WorkOrder (is_deleted == False) / WorkOrderOperation / TimeEntry / WorkCenter / User / Part
> (is_deleted == False), every aggregate filtered by company_id; and a sorted, company-scoped
> websocket-presence list (connected user ids + connected_since) }`. An INSERT bumps `count`, an
> in-place UPDATE / soft-delete bumps `max(updated_at)`, so together they dominate every payload field.
> The time keys are split deliberately: `today` (UTC) covers the due-today / overdue rollups, while
> `central_today` covers `completed_today` — a Central-Time rolling window that ages a completion OUT at
> **Central** midnight (hours after the UTC date rolls over) with **no row change**, so UTC `today`
> alone would serve a stale 304 across that boundary. `Part` is folded in because `active_assignments`
> surfaces `part_number` / `part_name` (dereferenced via the WO), so a part rename must move the ETag —
> a stale floor display of a part identity is an AS9100D traceability hazard. If `If-None-Match` matches
> the **pre-reconcile** fingerprint the handler returns **304 immediately**, having touched only the
> cheap aggregates — skipping the reconcile and the payload build. On a changed dashboard it runs the
> (bounded) reconcile, then computes the served ETag from the **post-reconcile committed snapshot
> BEFORE building the payload** (so the ETag describes the same snapshot the body is built from — a
> concurrent same-tenant commit during the build then merely forces a safe 200 on the next poll rather
> than a stale 304), and builds the payload reusing the company-scoped websocket presence captured
> **once** up front so the served `signed_in_users` matches the ETag exactly. The next poll over the
> now-stable state 304s with no extra round-trip. Tenant-scoped via `company_id` on every aggregate
> (invariant #1) — and scoping the websocket presence set (which `ConnectionManager` keeps **globally**
> across tenants) to the active company both keeps cross-tenant presence churn from spuriously moving
> this tenant's ETag **and closes a pre-existing cross-tenant leak** where another tenant's connected
> users could appear in this dashboard's `signed_in_users`.
>
> **PERF-3 — bounded dashboard reconcile + truncation warning (new setting).** The dashboard reconcile
> WO scan is now bounded:
> `.order_by(WorkOrder.updated_at.desc(), WorkOrder.id.desc()).limit(settings.SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT)`
> (the `id.desc()` secondary key is a stable tiebreak so two WOs with equal `updated_at` don't swap
> across the cap boundary between polls; new setting in `app/core/config.py`, type `int`,
> **default 250**) — the most-recently-touched open
> (RELEASED / IN_PROGRESS / ON_HOLD) WOs are the most likely to carry new completion evidence.
> Reconcile is best-effort and idempotent, so any WO **beyond the cap is still reconciled when opened
> in its detail / operations-list views** (both reconcile a single / page-bounded set); nothing is
> permanently stranded. When the scan **fills the cap exactly** the handler logs a **WARNING** that the
> open-WO set has outgrown read-path reconcile and to switch to the deferred ARQ reconcile job. (The
> list reconcile was already page-bounded and the detail reconcile is a single WO — both unchanged.)
> See `docs/ENVIRONMENT_VARIABLES.md`.
>
> **PERF-4 — grouped / in-memory predecessor gate.** `release_next_ready_operation`
> (`app/services/work_order_state_service.py`) now loads the WO's operations **once** (ordered by
> sequence) and runs the predecessor gate **in memory**
> (`blocked = any(op.sequence < candidate.sequence and op.id != candidate.id for op in non-COMPLETE ops)`)
> instead of issuing one `has_incomplete_predecessors` COUNT per PENDING candidate — the old N+1 that
> turned quadratic inside `complete_work_order`'s force-complete loop. The in-memory test replicates
> `has_incomplete_predecessors(...)` **exactly**, so the release / start / complete order gate is
> unchanged in behavior; `has_incomplete_predecessors` itself is untouched.
>
> **PERF-5 — `commit=False` atomicity fix + cache-invalidate-after-commit.** The four **live**
> completion handlers — `clock_out` and `complete_operation` in `shop_floor.py`, `complete_work_order`
> and `complete_operation` in `work_orders.py` — now call
> `SchedulingService.update_availability_rates(..., commit=False)`. Previously the default
> `commit=True` committed the WO / op state change **mid-handler**, before the audit rows / FG receipt /
> cost rollup / quality exceptions were written and committed separately — a two-transaction atomicity
> hole where a crash between the two commits left a completed WO with no audit / inventory / cost.
> `commit=False` joins the scheduling refresh into the handler's **single unit of work**. Because
> `commit=False` skips the in-service `invalidate_work_centers_cache()`, each handler now calls
> `invalidate_work_centers_cache()` **after** its terminal `db.commit()` succeeds (guarded by a
> `work_centers_refreshed` bool; success path only, never the rollback branch) so the cache reflects
> the freed capacity. Both `_reconcile_and_commit` paths (`shop_floor.py` + `work_orders.py`) likewise
> `invalidate_work_centers_cache()` after their `db.commit()` when any reconcile transition carried
> `work_center_ids` — the reconcile scheduling refresh already ran with `commit=False`. A cache
> invalidate is post-commit best-effort and can never 500 a read (invariant: reconcile-on-read stays
> read-safe).
>
> **DEFERRED — reconcile-on-read → debounced ARQ job (still its own initiative).** Batch 9 **bounds and
> de-risks** the read-path reconcile (PERF-3 cap + warning, PERF-2 fast-304, PERF-5 atomicity); it does
> **not** move reconcile off the read path. The full "move reconcile-on-read to a debounced ARQ
> reconcile job" remains a separate, deferred initiative — and when it lands it must **carry the
> system-actor re-attribution** flagged repeatedly upstream: under a background job reconcile has no
> requesting user, so the `reconcile_on_read` status-change audit rows (AUD-3 / Batch 3), the in-process
> reconcile events (EVT-4 / Batch 5), and the reconcile-path FG-receipt / backflush (Batch 6) and
> hour / cost / JobCost + `no_labor_recorded` writes (Batch 7) must all re-attribute to a **system
> actor** rather than `current_user`. This Batch-9 work **resolves the rank-12 follow-ups** noted in the
> Batch 3 (follow-up 2 — "bound the dashboard reconcile"), Batch 5 (follow-up 1), Batch 6 (follow-up 3),
> and Batch 7 (follow-up 4) status notes **to the extent of the index + bound + ETag + atomicity work**;
> the reconcile-off-read move (and its system-actor re-attribution) is what those notes carry forward.
>
> **Follow-ups (tracked, not fixed in Batch 9):**
> 1. **Reconcile-on-read → debounced ARQ job (with system-actor re-attribution).** As above — the full
>    move off the read path is the remaining rank-12 initiative; the PERF-3 truncation WARNING is the
>    operational trigger for when a shop has outgrown read-path reconcile.
> 2. **A1 — root `audit_log.sequence_number` race (carried over from Batch 2/3/6) — ✅ RESOLVED
>    (2026-06-09, branch `fix/wo-followups-round2`).** Unchanged by Batch 9 itself; the dedicated fix
>    landed in round-2 follow-ups — the `max()+1` allocation is now serialized in `AuditService.log()`
>    (advisory lock + savepoint/retry). See the resolved A1 note under Rank 5 (Batch 2).

### Rank 13 — Frontend completion UX hardening ☑ (Batch 10)
Invalidate `/shop-floor/dashboard` cache after completion mutations; in-flight guards on Complete buttons; memoize/window the WO list.
Findings: FEPERF-1, FEPERF-4, FEPERF-5.

> **Batch 10 status (2026-06-08, rank 13 landed — frontend-only).** The final ranked batch hardens the
> shop-floor / work-order completion UX on the client. It touches **only** `frontend/src/` — **no** API
> endpoint, request/response shape, env var, role/permission, or deploy step changed (the plan table's
> "behavior change?" column reads as a client-side UX change, not an API change). Implemented on
> `qa/full-pass-2026-06-04`, **pending tests / review / commit** (not yet merged), mirroring the Batch 9
> landed-but-unmerged posture. All three FEPERF findings landed.
>
> **FEPERF-1 — client ETag cache invalidated after every WO/operation mutation.** The Axios client
> (`frontend/src/services/api.ts`) keeps an in-memory ETag/conditional-request cache, and
> `/shop-floor/dashboard` is the **only** ETag-cached endpoint. A new private
> `invalidateDashboardCache()` (a thin wrapper over the existing `invalidateCache('/shop-floor/dashboard')`)
> now drops that cached entry after **each** state-changing work-order / operation / clock mutation:
> `releaseWorkOrder`, `startWorkOrder`, `completeWorkOrder`, `startWOOperation`, `completeWOOperation`,
> `clockIn`, `clockOut`, `startOperation`, `completeOperation`, `reportOperationProduction`, and
> `holdOperation`. So immediately after a completion / clock-out the next dashboard fetch **revalidates**
> instead of replaying a stale cached 304 body. This is **defense-in-depth on top of Batch 9's
> backend pre-reconcile state-fingerprint ETag (PERF-2)** — the server already moves the ETag when the
> state changes, but dropping the client entry also closes the **stale-cache-on-error fallback window**
> (the client serving its last cached body when a conditional request errors). No new dependency; no
> change to the request/response contract.
>
> **FEPERF-4 — double-submit guards on `WorkOrderDetail`'s Complete buttons.**
> `frontend/src/pages/WorkOrderDetail.tsx` previously let a fast double-click fire two completion POSTs.
> It now carries in-flight guards: a `completing` boolean for the WO-level **Complete** button and a
> `completingOpId` (keyed by operation id) for the per-operation **Complete** buttons. While a request
> is in flight the corresponding button is **disabled**, shows a spinning `ArrowPathIcon` + "Completing…"
> label, and the handler **early-returns on re-entry** — so a completion is submitted at most once. The
> guard wraps only the API call (set just before the `try`, cleared in `finally`), **not** the blocking
> `prompt()` quantity dialogs that precede it. (The shop-floor screens already had this pattern;
> `WorkOrderDetail` was the gap.) Backend completion is already idempotent / concurrency-safe (Batch 2
> 409, Batch 3 finalizer, Batch 6 DB-enforced idempotency) — this is the client-side complement that
> stops the duplicate request at the source.
>
> **FEPERF-5 — render-perf hardening of the work-order list (memoization, no virtualization).**
> `frontend/src/pages/WorkOrders.tsx` is **render-perf only — no visible change, no behavior change.**
> The per-row markup was extracted into a `React.memo`-wrapped `WorkOrderRow`, and
> `WorkOrderTable` / `WorkOrderMobileList` / `WorkOrderMobileCard` are now memoized too; `handleDelete`
> and `handleRelease` are wrapped in `useCallback` (stable identities so the memo holds). Crucially, rows
> now receive a **boolean `isReleasing`** prop instead of the shared `releasingIds` `Set` — passing the
> mutable Set defeated `React.memo` (every render is a new reference), so deriving a per-row boolean is
> what actually makes the memoization effective and stops the whole table re-rendering on every poll /
> release.
>
> **Deliberate scope decision — memoize, do NOT add list virtualization.** List
> windowing / virtualization (e.g. `react-window`) was **intentionally not** done: it would add a new
> runtime dependency, against this batch's dependency-free, low-risk precedent. Memoization captures the
> bulk of the re-render cost with zero new dependencies. **Windowing remains a deferred follow-up** if
> real-world list sizes grow enough to warrant it.
>
> **Docs check.** Frontend-only, so no API / env / RBAC / deploy doc needed touching. `docs/API.md`'s
> `/shop-floor/dashboard` caching note documents the **server-side** ETag/304 + bounded-reconcile
> contract (Batch 9, PERF-2/PERF-3) and stays accurate — the FEPERF-1 client-side cache invalidation is
> complementary and does not change that contract. No other doc references the frontend ETag-cache
> freshness behavior.
>
> **All 10 ranked batches are now implemented** (Batch 10 on `qa/full-pass-2026-06-04`, pending
> tests/review/commit). The post-plan completeness-critic gaps below have been triaged into **Batch 11**:
> sub-batch **11A** (G4-Fix1 / G3-scope / G6-A) merged to main (PR #27); sub-batch **11B**
> (G2 / G5-A / G3-content) merged to main (PR #28); sub-batch **11C** (G1 / G5-B / G6-B) is landed on
> branch `feat/wo-completion-batch11c` (one combined PR; tests green, review READY, compliance
> PASS-WITH-NOTES), with two XL items excluded. See "Completeness critic" → "Batch 11" below.

## Completeness critic — follow-up gaps the audit did NOT cover

These six gaps (labelled **G1–G6**) are the post-plan completeness-critic findings; their triage into
**Batch 11** sub-batches (11A + 11B landed, 11C planned, two XL items excluded) follows below.

1. **G1 · [high] Parent/child assembly rollup entirely unimplemented** — completing child WOs never advances the parent; parent can complete with children open. (`work_order.py:47,98`, `laser_nest_service.py:98`) → **landed in 11C** (laser-nest parent/child rollup: warn-and-record `child_work_orders_incomplete` on parent completion + `CHILD_WORK_ORDERS_COMPLETE` surface-flag signal, no auto-complete); the related **BOM child-WO spawn** is **EXCLUDED** (G1-general, XL).
2. **G2 · [high] Shipping `mark_shipped` has no inventory decrement and no over-ship guard.** (`shipping.py:282-325`) — *The unaudited part is closed in Batch 1: the WO CLOSED transition now writes a tamper-evident `audit_log` row via `AuditService.log_status_change`. The missing FG decrement and over-ship guard remain.* → **landed in 11B** (FG decrement on ship + warn-and-record over-ship / missing-FG-lot).
3. **G3 · [high] Reports/exports surface the never-computed `actual_cost`/`actual_hours` as truth** — every WO cost/hours report is structurally zero. (`report_builder.py:38-52`) Split into **G3-scope** (tenant isolation — the report builder queried across all tenants) → **landed in 11A**; and **G3-content** (report honesty about uncomputed cost/hours) → **landed in 11B**.
4. **G4 · [high] ECO complete/implement has zero effect on `affected_work_orders`** (revision-control gap); `get_affected_items` is cross-tenant. (`engineering_changes.py:543,717`) Split into **G4-Fix1** (ECO router tenant-scope + audit + RBAC, incl. the cross-tenant `affected-items` resolve and dashboard aggregate leak) → **landed in 11A**; and **G4-Fix2** (ECO actually drives revision/hold on `affected_work_orders`) → **EXCLUDED** (XL).
5. **G5 · [medium] TimeEntry approval is dead/disconnected** from costing; **no operator-certification gate** on clock-in/completion. (`time_entry.py:51`, `operator_certifications.py`) Split into **G5-A** (TimeEntry approval → costing) → **landed in 11B**; and **G5-B** (operator-qualification gate at clock-in / op start, warn-and-record) → **landed in 11C**.
6. **G6 · [medium] `complete_work_order` can resurrect a CLOSED/shipped/cancelled WO** (no terminal-state lock); **CoC is a bare boolean, never generated.** Split into **G6-A** (terminal-state lock) → **landed in 11A**; and **G6-B** (Certificate of Conformance generation — per-shipment DB frozen-snapshot artifact + auto-issue-on-ship) → **landed in 11C**.

### Batch 11 (completeness-critic follow-up) — sub-batched

Batch 11 is the post-plan completeness-critic effort, triaged into three sub-batches by theme/risk plus
two **EXCLUDED** XL items (deferred to their own initiatives, not part of Batch 11):

| Sub-batch | Items | Theme | Status |
|---|---|---|---|
| **11A** | G4-Fix1, G3-scope, G6-A | Security / correctness (tenant isolation, audit, RBAC, terminal-state lock) | ☑ landed on branch `feat/wo-completion-batch11` |
| **11B** | G2, G5-A, G3-content | Data integrity (FG decrement + over-ship guard, TimeEntry-approval costing, report honesty) | ☑ landed on branch `feat/wo-completion-batch11b` |
| **11C** | G1 (parent/child rollup), G5-B, G6-B | Compliance (parent/child rollup, operator-cert gate, CoC generation) | ☑ landed on branch `feat/wo-completion-batch11c` (one combined PR) |
| **EXCLUDED** | G4-Fix2 (ECO revision/hold drive), G1-general (BOM child-WO spawn) | XL — own initiatives | — out of Batch 11 scope |

> **Batch 11A status (2026-06-08, landed on branch `feat/wo-completion-batch11`, pending
> tests/compliance/review/merge).** Three completeness-critic security/correctness fixes — no migration,
> no schema change, no new env var. All three are tenant-isolation / audit / state-integrity hardening
> (AS9100D/CMMC-relevant), consistent with Batch 1's isolation+audit posture.
>
> **G4-Fix1 — ECO router is now tenant-scoped, audited, and Admin/Manager-gated**
> (`app/api/endpoints/engineering_changes.py`). The ECO router was a cross-tenant hole: `get_eco_or_404`
> filtered on `id` alone, so **every by-id ECO endpoint (read and mutate) was cross-tenant**, and
> `get_affected-items` resolved affected part/WO/document ids across **all** tenants.
> - **Tenant scope.** `get_eco_or_404(db, eco_id, company_id)` now filters `company_id`; a mismatch
>   returns **404** (never 403 — don't confirm another tenant's ECO exists). `update_task` re-verifies the
>   parent ECO's company and scopes the task query by `company_id`. `/eco/eco/affected-items/{id}` resolves
>   parts/WOs/documents **only within the active company** (and excludes soft-deleted parts/WOs;
>   `Document` has no `is_deleted` column). The `/eco/eco/dashboard` aggregates (by-type, by-priority, and
>   the completed-cycle-time set) are now all filtered by `company_id` — they previously **summed across
>   every tenant**.
> - **Cross-tenant affected ids → 422.** New `_validate_affected_ids_in_company` runs on create and
>   update: each id in `affected_parts` / `affected_work_orders` / `affected_documents` must resolve to a
>   live row in the active company, else **422** (`"Unknown or cross-tenant <label> id(s): […]"`). This
>   stops a crafted payload from persisting another tenant's ids (which the affected-items endpoint would
>   then resolve). `add_approval` likewise verifies the named approver belongs to the active company.
> - **RBAC.** All mutating ECO endpoints — create, update, submit, approve, reject, implement, complete,
>   add task, update task, add approval — now require `require_role([ADMIN, MANAGER])` (were any
>   authenticated user). Reads (list/get/dashboard/list-approvals/affected-items) stay open to all
>   authenticated users. New child rows (`ECOApproval`, `ECOImplementationTask`) are tenant-tagged
>   (`company_id` set — both are `TenantMixin`, `NOT NULL`).
> - **Audit.** ECO create/update and the status transitions (submit/approve/reject/implement/complete),
>   plus task create/update and approval create, now write to the tamper-evident `audit_log` via
>   `AuditService` (`db.flush()` before the log call so the audit row carries the assigned PK). The ECO
>   lifecycle was previously unaudited.
>
> **G3-scope — custom-report builder is now tenant-scoped** (`app/services/report_builder.py`,
> `app/api/endpoints/analytics.py`). `ReportBuilderService.execute_report` now takes `company_id` and
> applies `tenant_filter(query, model, company_id)` **before** any user-supplied filters/group-by/sort.
> Every supported data source (WorkOrder, Part, InventoryItem, NonConformanceReport, PurchaseOrder,
> Quote) carries `company_id` via `TenantMixin`, so the report can never return another tenant's rows.
> Both callers — `POST /analytics/custom-report` and `GET /analytics/custom-report/export` — pass the
> active company. (The export's template fetch was already company-scoped; the **data** query was not, so
> export returned every tenant's rows.) No request/response shape change — scoping only. This is the
> **G3-scope** half; **G3-content** (the report honestly representing uncomputed `actual_cost`/
> `actual_hours`) is deferred to 11B.
>
> **G6-A — terminal-state lock on work-order completion**
> (`app/services/work_order_state_service.py`, `app/api/endpoints/work_orders.py`,
> `app/api/endpoints/shop_floor.py`). A new module-level set `TERMINAL_WO_STATUSES = {COMPLETE, CLOSED,
> CANCELLED}` is the single source of truth for "this WO has finished its lifecycle." The prior guards
> checked only `COMPLETE`/`CLOSED`, so a **CANCELLED** WO could be driven to COMPLETE — re-firing
> FG receipt / backflush / cost rollup (Batch 6/7) and writing a spurious COMPLETE row onto the
> tamper-evident audit chain. Now:
> - **Manual complete** (`POST /work-orders/{id}/complete`) of a **CANCELLED** WO → **409**
>   (`"cannot complete a cancelled work order"`). (The existing COMPLETE/CLOSED no-op idempotency is
>   unchanged — that completion already happened; CANCELLED is different, it was deliberately pulled.)
> - **Operation complete** (`/work-orders/operations/{id}/complete` and
>   `/shop-floor/operations/{id}/complete`) against an operation whose parent WO is in **any** terminal
>   status → **409** (`"cannot complete operation: work order is <status>"`) before any mutation —
>   mirroring the existing ON_HOLD 409.
> - **Generic update** (`PUT /work-orders/{id}`) moving a **terminal → non-terminal** status → **409**
>   (`"cannot move work order out of terminal status '<current>' to '<target>'"`). This handler applies
>   `status` via a blind `setattr` with no transition validation; the guard is the minimal block on the
>   one dangerous flip, not a full state machine.
> - **Reconcile-on-read** (`_sync_work_order_status_from_operations`,
>   `finalize_operation_completion`) early-returns / is gated on `TERMINAL_WO_STATUSES`, so operation
>   evidence read on any GET can never reopen a terminal WO to IN_PROGRESS or resurrect a CANCELLED WO to
>   COMPLETE. (The reconcile path has no actor, so a resurrection there would also mis-attribute the
>   audit/inventory writes.)
>
> **Compliance note.** All three are isolation/audit/state-integrity hardening; none changes a
> compliance **claim**, so no edit to `CMMC_LEVEL_2_COMPLIANCE.md` was required. The ECO lifecycle moving
> onto the audit chain and the report/ECO tenant scoping *strengthen* the AS9100D traceability + CMMC
> access-control posture already documented there.
>
> **Docs updated for 11A:** `docs/API.md` (new Engineering Change Orders section; `/analytics/custom-report*`
> rows + tenant-scope callout; WO terminal-state 409s on the complete / operation-complete / update
> endpoints), `docs/RBAC_PERMISSIONS.md` (new ECO permission matrix — mutations Admin/Manager), and this
> doc.
>
> **11B / 11C remain triaged-not-started** (see table above), and **G4-Fix2** (ECO drives revision/hold
> on `affected_work_orders`) and **G1-general** (BOM-driven child-WO spawn) are **EXCLUDED** from Batch 11
> as XL items for their own initiatives.

> **Batch 11B status (2026-06-08, landed on branch `feat/wo-completion-batch11b`, pending
> tests/compliance/review/merge).** Three completeness-critic data-integrity fixes — no migration, no
> schema change. One new env flag (opt-in, default OFF). Two new endpoints. All three close the
> material-trail / cost-trail honesty gaps the completeness critic flagged, consistent with the
> warn-and-record posture established in Batches 4 and 6.
>
> **G2 — shipping now decrements finished-goods inventory + warn-and-records over-ship**
> (`app/api/endpoints/shipping.py`, `app/services/completion_inventory_service.py`). Marking a
> shipment shipped previously wrote nothing to inventory — the Batch-6 FG receipt on completion had no
> offset, so finished goods accrued on-hand forever. Now `mark_shipped` writes the mirror of that
> receipt:
> - **FG decrement.** A `SHIP` `InventoryTransaction` (`quantity = -quantity_shipped`,
>   `reference_type = "shipment"`, `reference_id = shipment.id`) and a decrement of the finished-goods
>   lot's `quantity_on_hand` / `quantity_available`. The lot is matched on `company_id` + `part_id` +
>   the finished-goods location + `work_order.lot_number` — exactly the row the receipt created (the
>   location constant is imported, not re-declared, so the two sides stay in lock-step). The SHIP txn,
>   the on-hand decrement, and both audit rows (`log_create` on the txn, `log_update` on the lot) join
>   the **same unit of work** as the SHIPPED status change + WO close, so they commit atomically.
> - **Idempotent under concurrency.** The shipment row is locked `with_for_update` while status is
>   re-checked, and a prior SHIP txn for the shipment short-circuits the decrement
>   (`_existing_shipment_ship_txn`); the txn insert is under a savepoint. A re-submitted or concurrent
>   double-ship can never double-decrement on-hand. (FOLLOW-UP, deferred — this batch is
>   migration-free: a DB partial-unique index on `(company_id, reference_type='shipment',
>   reference_id, SHIP)` would harden this the way `uq_wo_inventory_receipt` hardens the receipt.)
> - **Warn-and-record, never blocking** (mirrors the backflush-shortage posture):
>   - **Over-ship** (`record_over_ship_if_needed`): cumulative `quantity_shipped` across the WO's
>     non-cancelled shipments beyond `WorkOrder.quantity_complete` (the produced quantity — there is no
>     SalesOrder table to ship against) records a tamper-evident `audit_log` row (action `OVER_SHIP`)
>     + a warning `OperationalEvent`. The ship still proceeds (no 400).
>   - **FG lot not found** (`decrement_finished_goods_for_shipment`): no matching finished-goods lot
>     row (receipt skipped / lot changed / stock already moved) records action `SHIP_FG_LOT_MISSING` +
>     a warning event; on-hand is left untouched and the ship/close still proceeds.
>   - The warning-event emit is wrapped so a transient signal failure can never fail an in-flight ship
>     (the audit row is the compliance record).
> - **No new request/response field** — the decrement and discrepancy recording are side effects of
>   marking shipped.
>
> **G5-A — TimeEntry approval, wired to an opt-in labor-cost filter**
> (`app/api/endpoints/shop_floor.py`, `app/core/config.py`, `app/services/labor_cost_service.py`,
> `app/services/job_costing_service.py`, `app/services/completion_cost_service.py`,
> `app/services/analytics_service.py`). The `TimeEntry.approved` / `approved_by` columns existed but
> were dead — nothing set them and nothing read them. Now:
> - **Two new endpoints.** `POST /shop-floor/time-entries/{id}/approve` (sets `approved` now +
>   `approved_by`) and `POST /shop-floor/time-entries/{id}/unapprove` (clears both). Both are
>   role-gated to **ADMIN / MANAGER / SUPERVISOR / QUALITY**, tenant-scoped (cross-tenant
>   id → **404** before any mutation), idempotent (re-approving / re-unapproving is a no-op with no
>   second audit row), respect the `version` optimistic-lock column (concurrent stale write → **409**),
>   and write **one** tamper-evident `audit_log` row (`time_entry_approve` / `time_entry_unapprove`).
> - **Self-approval forbidden.** A user cannot approve or unapprove their **own** TimeEntry — **403**
>   even for an approver-role user (segregation of duties for the labor-cost gate). Unapprove is gated
>   the same way for symmetry. `approved` / `approved_by` also now surface on `TimeEntryResponse` and
>   `GET /shop-floor/my-active-job`.
> - **Opt-in cost filter `REQUIRE_APPROVED_LABOR_FOR_COST`** (default **OFF**). When ON, the three
>   labor-cost consumers — the job-costing recompute (`recompute_from_time_entries`), the completion
>   cost rollup (`_operation_duration_by_type`), and the analytics OEE/labor leg — additionally require
>   `TimeEntry.approved IS NOT NULL`, so un-approved labor is excluded from cost until a supervisor
>   signs off. Default OFF → no predicate added → **byte-identical** to pre-flag behavior. Resolved
>   through a new shared chokepoint `labor_cost_service.is_approved_labor_required(company_id)` (global
>   today for the same reason as `is_labor_cost_rollup_enabled`; promotable to per-tenant in one place).
>
> **G3-content — custom-report honesty about uncomputed labor** (`app/services/report_builder.py`,
> `app/api/endpoints/analytics.py`). The 11A G3-scope fix made the report tenant-isolated; this is the
> content-honesty half:
> - **Phantom `estimated_hours` column dropped.** `WorkOrder.estimated_hours` has no writer anywhere
>   (structurally 0 in every tenant), so it has been removed from the report builder `FIELD_MAPPINGS`
>   **and** from the `GET /analytics/data-sources` selectable catalog. It silently dropped out of
>   results before; it can no longer be selected. Computing it would need a routing/operations join
>   (out of scope).
> - **Labor-not-tracked headers.** When `LABOR_COST_ROLLUP_ENABLED` is **OFF** (default) **and** the
>   report selects any labor-derived WORK_ORDERS column (`actual_hours` / `actual_cost` /
>   `estimated_cost`) — which then render a literal `0` meaning "not tracked", not a measured zero —
>   `POST /analytics/custom-report` sets `X-Report-Labor-Not-Tracked` (JSON array of affected columns)
>   and `X-Report-Labor-Note` (human-readable note) response headers so a consumer can tell the two
>   apart. The **response body is unchanged** (the bare-list contract the export + clients rely on);
>   headers are set only when applicable. Resolved via the same flag chokepoint
>   (`labor_cost_service.is_labor_cost_rollup_enabled`) — mirroring the documented flag-OFF stance of
>   `get_cost_analysis`.
>
> **Compliance note.** G2's FG decrement + over-ship/missing-lot recording *strengthens* the AS9100D
> material-traceability and CMMC audit-trail posture (outbound stock movements now leave a
> tamper-evident trail; discrepancies are recorded, not silently dropped). G5-A's approval +
> segregation-of-duties gate strengthens the CMMC access-control / accountability posture for the
> labor-cost record. None of the three changes a compliance **claim** in
> `CMMC_LEVEL_2_COMPLIANCE.md`, so no edit to that audit artifact was required pre-merge — but the
> reviewer may want to add a changelog line noting the new `SHIP`/`OVER_SHIP`/`SHIP_FG_LOT_MISSING`
> audit actions and the `time_entry_approve` / `time_entry_unapprove` actions now on the hash chain
> (the audit artifact should not be edited pre-merge / without sign-off).
>
> **Docs updated for 11B:** `docs/API.md` (Shop Floor — two new time-entry approve/unapprove endpoints
> + callout; Shipping — FG decrement + warn-and-record over-ship/missing-lot callout; Analytics —
> custom-report labor-not-tracked headers + `estimated_hours` removed from the selectable catalog),
> `docs/ENVIRONMENT_VARIABLES.md` (new `REQUIRE_APPROVED_LABOR_FOR_COST` flag + relationship to
> `LABOR_COST_ROLLUP_ENABLED`), `docs/RBAC_PERMISSIONS.md` (new "Approve labor (TimeEntry)" row under
> Work Orders — ADMIN/MANAGER/SUPERVISOR/QUALITY, no self-approval), and this doc.
>
> **11C now landed** (G1 parent/child rollup, G5-B operator-cert gate, G6-B CoC generation — see the
> Batch 11C status note below); **G4-Fix2** and **G1-general** remain **EXCLUDED**.

> **Batch 11C status (2026-06-08, landed on branch `feat/wo-completion-batch11c`, tests green, review
> READY, compliance PASS-WITH-NOTES).** Three completeness-critic compliance features, delivered as
> **one combined PR**. Two new migrations (`043`, `044`); two new tenant-scoped non-env config surfaces
> (a work-center column + a customer/shipment flag — no new env var; G5-B's threshold is the module
> constant `MIN_SKILL_LEVEL = 2`). All three follow the **warn-and-record** posture established in
> Batches 4 and 6 — they record/surface, they never block — and strengthen the AS9100D
> traceability / CMMC accountability posture.
>
> **G1 — parent/child laser-nest completion rollup (warn-and-record)**
> (`app/services/work_order_state_service.py`, `app/services/quality_gate_service.py`,
> `app/services/completion_signal_service.py`, `app/api/endpoints/shop_floor.py`,
> `app/api/endpoints/work_orders.py`). Scope is **laser-only** by decision: only `LASER_CUTTING`
> children (linked by `parent_work_order_id`) are tracked for the rollup; the general BOM-driven
> child-WO spawn is the EXCLUDED **G1-general** item. Two halves:
> - **Parent completes with open children → recorded, not blocked.** When a parent WO is driven to
>   COMPLETE while one or more laser children are still non-terminal, the completion **still succeeds**
>   and records a `child_work_orders_incomplete` entry on the existing `quality_exceptions` response
>   field (severity `high`) + a `COMPLETED_WITH_QUALITY_EXCEPTION` audit row. A CANCELLED child counts
>   as resolved, not a blocker. The gate runs only once the WO is already COMPLETE (read-only,
>   tenant-scoped child query), so a mere progress/partial update never triggers it.
> - **Last child done → surface a signal, do not auto-complete.** When the **last** laser child reaches
>   a terminal status, the system emits a `child_work_orders_complete` OperationalEvent + a
>   tamper-evident `CHILD_WORK_ORDERS_COMPLETE` audit row attributed to the parent. Locked decision:
>   this is a **surface-flag advance, not an auto-complete** — parent and child WOs are **not**
>   operation-coupled in the data model, so the system never mutates the parent's route; a human
>   completes the parent. `find_parent_to_advance` returns the parent only when no laser child remains
>   non-terminal, which becomes true exactly once (the last child flip), and completion handlers are
>   idempotent + reconcile-on-read never re-flips a terminal child, so this records at most once. Fires
>   from the live completion paths and the reconcile-on-read path (tagged `source =
>   "reconcile_on_read"`); best-effort. **No API request/response shape change** — both halves ride the
>   existing `quality_exceptions` field / event+audit machinery. No new endpoint, no migration.
>
> **G5-B — operator-qualification gate at clock-in & op start (warn-and-record)**
> (`app/services/operator_qualification_service.py`, `app/api/endpoints/shop_floor.py`,
> `app/models/work_center.py`, `app/schemas/time_entry.py`; migration `043`). `POST /shop-floor/clock-in`
> and `POST /shop-floor/operations/{id}/start` evaluate the operator against the operation's work center
> and **record** (never block) any gap. Both legs (locked decision):
> - **Skill leg:** no active `SkillMatrix` entry at `skill_level >= MIN_SKILL_LEVEL` (= 2, "Basic", a
>   module constant matching the legacy check endpoint's threshold) for the work center →
>   `operator_not_skill_qualified` (severity `medium`).
> - **Certification leg (optional):** where the **new** nullable
>   `work_centers.required_certification_type` column is set, a missing/expired/revoked
>   `OperatorCertification` of that type → `operator_certification_missing_or_expired` (severity
>   `high`). When the column is NULL (the common case) this leg is skipped. The column reuses the
>   existing `CertificationType` native enum.
>
> Each gap writes an `OPERATOR_QUALIFICATION_EXCEPTION` audit row + a warning OperationalEvent and is
> surfaced on a new backward-compatible `qualification_exceptions` field (default `[]`) on the clock-in
> `TimeEntryResponse` and the start-operation response body. Clock-in / start stay **operator-facing**
> (any authenticated user); the gate only records, it does not gate the role. The gate is **tenant-scoped**
> — every skill/cert/work-center lookup filters the active company. (At 11C the legacy
> `GET /operator-certifications/skill-matrix/check/…` read endpoint was **not** company-scoped, only this
> new gate was — tracked as a compliance follow-up below; **the legacy skill-matrix read endpoints were
> since tenant-scoped 2026-06-09 on branch `fix/wo-remediation-followups`** — see the compliance-auditor
> follow-ups note.) Migration `043` adds the single nullable column (metadata-only, no backfill).
>
> **G6-B — Certificate of Conformance generation**
> (`app/models/shipping.py`, `app/services/coc_service.py`, `app/services/coc_pdf_service.py`,
> `app/api/endpoints/shipping.py`; migration `044`). A real CoC artifact now exists (was a bare
> boolean). Locked decisions: **per-shipment, DB frozen-snapshot** (the new `certificates_of_conformance`
> table stores the immutable certified facts; the PDF is rendered deterministically on download — no
> filesystem blob), **auto-issue-when-required**, and **Admin/Manager/Quality issue / read-broad**.
> - **Three endpoints** on the shipping router (`/api/v1/shipping`): `POST /{shipment_id}/coc` (issue or
>   return existing — idempotent, one CoC per shipment via `uq_coc_company_shipment`; RBAC
>   **Admin/Manager/Quality**; tenant-scoped, 404 cross-tenant), `GET /{shipment_id}/coc` (metadata, 404
>   if none; any authenticated company user), `GET /{shipment_id}/coc/pdf` (rendered `application/pdf`;
>   any authenticated company user).
> - **Auto-issue on ship.** `POST /{shipment_id}/ship` auto-issues when **required** = the shipment's
>   `cert_of_conformance` flag is set OR a company-scoped `Customer` matched by `work_order.customer_name`
>   has `requires_coc` (which **defaults `True`** — so auto-issue fires for essentially every
>   customer-matched shipment; intended fail-safe). Auto-issue is **idempotent + best-effort**: a CoC
>   failure never fails the ship (records a `coc_generation_failed` warning event). First issue writes a
>   `log_create` audit row. CoC content = AS9100D conformance statement + part/revision + WO# /
>   customer-PO + qty + lot/serial table + signature block. Migration `044` adds the tenant-scoped
>   `certificates_of_conformance` table — **append-only** (intentionally no soft-delete, like an issued
>   certificate). CUI note: CoC content lives only in the tenant-scoped DB and renders on demand (no
>   off-platform egress).
>
> **In-batch concurrency defect found + fixed.** The CoC `begin_nested()` savepoint that guards the
> `uq_coc_company_shipment` double-ship race originally had `db.add(coc)` **outside** the try block —
> but `begin_nested()` autoflushes on open, so the INSERT (and its collision `IntegrityError`) would
> have fired **outside** the savepoint, poisoning the outer ship transaction. Fixed by moving `db.add`
> inside the savepoint try (now mirrors the FG-receipt `_insert_txn_with_savepoint` precedent from
> Batch 6 / migration `041`).
>
> **Compliance note (PASS-WITH-NOTES).** All three *strengthen* the AS9100D traceability + CMMC
> accountability posture already documented in `CMMC_LEVEL_2_COMPLIANCE.md`: G6-B adds an issued,
> tamper-evident conformance artifact with frozen lot/serial provenance (AS9100D 8.5.2 / 8.6); G5-B
> records operator competency gaps at the point of work (AS9100D 7.2 competence / CMMC AT
> accountability); G1 records parent-completion-with-open-children and the all-children-complete signal
> on the tamper-evident chain. **None changes a compliance *claim***, so — following the 11A/11B
> precedent — `CMMC_LEVEL_2_COMPLIANCE.md` was **not** edited pre-merge. A reviewer/auditor may add a
> light changelog line under AU-3.3.1 noting the new `OPERATOR_QUALIFICATION_EXCEPTION`,
> `CHILD_WORK_ORDERS_COMPLETE`, and CoC `log_create` audit actions now on the hash chain (the audit
> artifact should not be edited without sign-off).
>
> **Compliance-auditor follow-ups (tracked):**
> - **Legacy operator skill-matrix read endpoints — ✅ RESOLVED (2026-06-09, branch
>   `fix/wo-remediation-followups`).** The four skill-matrix READ endpoints in
>   `app/api/endpoints/operator_certifications.py` — `GET /operator-certifications/skill-matrix/check/{user_id}/{work_center_id}`
>   (`check_operator_qualification`), `GET …/skill-matrix/user/{user_id}` (`get_user_skills`),
>   `GET …/skill-matrix/work-center/{work_center_id}` (`get_work_center_operators`),
>   `GET …/skill-matrix/` (`list_skill_matrix`) — plus the `POST …/skill-matrix/` writer
>   (`create_skill_entry`) are now **tenant-scoped**: each adds `company_id = Depends(get_current_company_id)`
>   and filters `SkillMatrix.company_id == company_id` (and `list_skill_matrix` also scopes its
>   `User`/`WorkCenter` grid lists; `create_skill_entry` scopes its duplicate-check and stamps
>   `company_id` on the new row). Closes a cross-tenant skill-data read leak. **No RBAC change** — these
>   remain open to any authenticated user, now company-scoped.
> - **Remaining `operator_certifications.py` cross-tenant read/write leak — ✅ RESOLVED (2026-06-09,
>   branch `fix/wo-followups-round2`).** Seven more endpoints in
>   `app/api/endpoints/operator_certifications.py` are now tenant-scoped (each adds
>   `company_id = Depends(get_current_company_id)`): the `GET /certifications/dashboard`
>   (`certification_dashboard`) aggregates — cert counts/compliance-rate, `operators_with_certs`,
>   `total_operators` (now `User.company_id`-scoped), and training-hours-this-month — are now all
>   company-scoped; `GET /certifications/expiring` filters `OperatorCertification.company_id`; and the
>   by-id reads/updates `GET /certifications/user/{user_id}`, `GET /certifications/{cert_id}`,
>   `GET /training/user/{user_id}`, `PUT /training/{training_id}` (`update_training`), and
>   `PUT /skill-matrix/{entry_id}` (`update_skill_entry`) all add `company_id` to the lookup, so a
>   cross-tenant id now returns **404** before any read/mutation. Closes the cross-tenant cert/training/
>   skill read+write leak. **No RBAC change** — these stay open to any authenticated user, now
>   company-scoped.
> - **`operator_certifications.py` WRITE endpoints (no `require_role` + no audit + FK-injection) — ✅
>   RESOLVED (2026-06-09, branch `fix/operator-cert-write-rbac-audit`).** The seven write endpoints on
>   `app/api/endpoints/operator_certifications.py` were open to **any authenticated user**, wrote **no**
>   `audit_log` row, and accepted a cross-tenant `user_id` / `work_center_id` FK on create (the prior
>   round-2 fix added company *scoping* to the by-id read/update paths, not RBAC, audit, or create-time
>   FK validation). Now:
>   - **RBAC.** `create_certification` / `update_certification` / `delete_certification` /
>     `create_training` / `update_training` → `require_role([ADMIN, MANAGER, QUALITY])`
>     (`CERT_TRAINING_WRITE_ROLES`); `create_skill_entry` / `update_skill_entry` →
>     `require_role([ADMIN, MANAGER, SUPERVISOR])` (`SKILL_MATRIX_WRITE_ROLES`). Any other authenticated
>     role → **403**. These role sets are **new defaults** chosen for this fix — the RBAC matrix had no
>     rows for these record types (cert/training are Quality-owned conformance records; skill-matrix
>     entries are Supervisor-and-above competency assessments). The READ endpoints are unchanged (any
>     authenticated user, tenant-scoped).
>   - **Audit.** Each write writes a tamper-evident `audit_log` row (resource types
>     `operator_certification` / `training_record` / `skill_matrix`; `log_create` / `log_update` /
>     `log_delete`), flushed before the terminal commit so it commits atomically (mirrors the Batch-1
>     completion-path audit precedent). `create_skill_entry`'s upsert-as-update path logs an update row.
>   - **Create-time FK validation.** New `_require_company_user` / `_require_company_work_center` helpers
>     reject a `user_id` / `work_center_id` that does not resolve to a row in the active company with
>     **422** before insert (and on `update_training`'s re-pointed `work_center_id`) — closing the
>     cross-tenant FK-injection vector on create. No migration, no new env var.
>   See `docs/RBAC_PERMISSIONS.md` → Operator Certifications & Training / Skill Matrix and `docs/API.md`
>   → Shop Floor (operator-certifications callout).
> - **`SkillMatrix` unique constraint is now tenant-qualified — ✅ RESOLVED (2026-06-09, branch
>   `fix/wo-followups-round2`).** The global `UniqueConstraint('user_id', 'work_center_id',
>   name='uq_user_work_center')` is replaced by tenant-scoped
>   `UniqueConstraint('company_id', 'user_id', 'work_center_id', name='uq_skill_matrix_company_user_wc')`
>   on the model (`app/models/operator_certification.py`) and via migration
>   **`045_skillmatrix_company_unique`**. The new key is strictly **looser** (it prepends `company_id` to
>   the same tuple), so no pre-flight dedup is required and the migration is safe to apply before/after
>   the model deploy; Postgres-guarded drop/recreate (SQLite `create_all` already builds the new
>   constraint from `__table_args__`); idempotent + reversible. Two tenants can now record the same
>   `(user_id, work_center_id)` skill row without a cross-tenant uniqueness collision (invariant #1).
> - Optional DB-level CoC-immutability hardening (the row is treated as append-only in code; a
>   DB-level guard would harden it further). **(still open)**
>
> **Docs updated for 11C:** `docs/API.md` (Shop Floor — `qualification_exceptions` warn-and-record gate
> on clock-in/start; Work Orders — `child_work_orders_incomplete` quality exception + the
> `CHILD_WORK_ORDERS_COMPLETE` parent-rollup signal; Shipping — three new CoC endpoints + auto-issue-on-
> ship + RBAC), `docs/RBAC_PERMISSIONS.md` (new "Issue Certificate of Conformance" Shipping row —
> Admin/Manager/Quality; CoC read = any authenticated user; record-only operator-qualification-gate
> note under Work Orders), `docs/ENVIRONMENT_VARIABLES.md` (extended the "Not environment-configurable
> (intentional)" note — confirms G5-B `MIN_SKILL_LEVEL` is a module constant and CoC requirement is
> flag-driven, no new env var), and this doc. `CMMC_LEVEL_2_COMPLIANCE.md` deliberately **not** edited
> (see compliance note above).
>
> **G4-Fix2** (ECO drives revision/hold on `affected_work_orders`) and **G1-general** (BOM-driven
> child-WO spawn) remain **EXCLUDED** from Batch 11 as XL items for their own initiatives.
