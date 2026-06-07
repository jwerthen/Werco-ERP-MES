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
| **4** | 7 | Quality-gate enforcement on completion | maybe (FAI/NCR op FK) | **yes — blocks some completions** |
| **5** | 8 | Uniform completion signal set (events/notify/webhook/sched) | no | new outbound signals |
| **6** | 9 | FG receipt + backflush + as-built genealogy | maybe (genealogy table) | inventory now moves |
| **7** | 10 | Labor-hour + job/actual-cost rollup | maybe (drop cols) | cost reports populate |
| **8** | 11 | OEE/OTD metric correctness + dead auto-OEE endpoint | no | **KPI values move** |
| **9** | 12 | Indexes + de-risk reconcile-on-read | **yes** | reconcile may move off read path |
| **10** | 13 | Frontend completion UX hardening | no | optimistic updates |

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
> **Open follow-ups (tracked, not yet fixed):**
> 1. **Worker cron kwargs bug** — `app/worker.py` schedules `cron(run_mrp_job, …, kwargs={"mode": "AUTO_DRAFT"})`; ARQ's `cron()` does not accept a `kwargs=` argument, so the daily MRP cron entry is mis-wired. The per-company fan-out in `run_mrp_task`/`run_scheduling_task` (and the `company_id=None` defaults on the job wrappers) is correct; only the cron registration needs fixing.
> 2. **`mrp_auto_service` field-name drift** — the `app.models.purchase_order`/`app.models.vendor` imports were corrected to `app.models.purchasing` (those modules don't exist), but remaining `PurchaseOrder`/`PurchaseOrderLine`/`Vendor` field references in `MRPAutoService` still need a pass against the `purchasing` model to confirm names line up.
> 3. **Periodic `notification_jobs.py` cross-tenant scope** — `get_notification_recipients` now takes a `company_id`, and the MRP/scheduling jobs pass it, but the daily notification jobs in `app/jobs/notification_jobs.py` (quality / supervisor / manager / purchasing / inventory / sales digests) still call it **without** `company_id`, so those notifications fan out across all tenants.

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
> **Residual follow-up A1 (tracked, not fixed in Batch 2):** `audit_log.sequence_number` is
> allocated by `max()+1` and is **not** serialized by the new `FOR UPDATE` row locks (those lock the
> operation / time-entry / work-order rows, not the audit table). Two concurrent audit writes on a
> hot completion path can therefore collide on the unique `sequence_number` and raise an uncaught
> `IntegrityError`, surfacing as an occasional **500**. This is pre-existing but **worsened** by the
> additional completion-path audit sites added in Batch 1. Track for a dedicated fix — e.g. serialize
> sequence allocation (advisory lock / `SELECT … FOR UPDATE` on the chain tail) or catch-and-retry on
> the sequence-collision `IntegrityError`.

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
> 1. **A1 — root `audit_log.sequence_number` race (carried over from Batch 2).** The `max()+1`
>    sequence allocation is still **not** serialized under concurrent writes; Batch 3 only guaranteed
>    that reconcile-on-read *reads* don't 500 when the collision happens (it swallows the failure and
>    drops the redundant write). The hot completion-path writers can still collide on the unique
>    `sequence_number` → occasional **500**. Needs a dedicated fix (serialize sequence allocation via
>    an advisory lock / `FOR UPDATE` on the chain tail, or catch-and-retry on the collision).
> 2. **Rank-12 tension — AUD-3 keeps mutation + audit on the read path.** The planned rank-12
>    "bound the dashboard reconcile / move it to a debounced ARQ job" (Batch 9) must **carry the
>    audit emission with it**: once reconcile no longer runs under the requesting user it has to
>    re-attribute the `reconcile_on_read` status-change rows to a **system actor** rather than
>    `current_user`. Do not drop the audit when moving reconcile off the read path.
> 3. **Batch-4 dependency — deferred `complete_work_order` ON_HOLD audited override.** Batch 3 only
>    makes the endpoints *consistently refuse* an ON_HOLD completion (409). The **audited QUALITY-role
>    override** that may clear a hold during completion is part of rank 7 (Batch 4) quality-gate
>    enforcement.

### Rank 7 — Enforce quality gates on completion ☐ (Batch 4 · behavior change)
`assert_operation_completable(op)` in the finalizer + inside reconcile: refuse COMPLETE when `requires_inspection and not inspection_complete`, open NCR (PENDING disposition), non-passed FAI, or open `WorkOrderBlocker` — or audited QUALITY-role override. Ship the missing `inspection_complete` writer. Auto-resolve/surface blockers on completion/resume.
Findings: QG-1, QG-2, QG-3, QG-4, QG-5, BLK-1, BLK-2, BLK-4.

### Rank 8 — Uniform completion signal set from the finalizer ☐ (Batch 5)
Emit `operation_completed`/`work_order_completed`/`work_order_closed` events; tenant-scoped broadcasts; enqueue ARQ → `NotificationService.WO_COMPLETED` + `WebhookService.dispatch_event('work_order.completed')` (scope `get_webhooks_for_event` first); refresh scheduling on `complete_work_order`+reconcile; reconcile returns transitions so read handlers emit events/audit.
Findings: EVT-1, EVT-2, EVT-3, EVT-4, EVT-5, MS-2.

### Rank 9 — FG receipt + backflush + as-built genealogy on completion ☐ (Batch 6 · data-sensitive)
In finalizer on WO COMPLETE: assign FG lot/serial; create/increment FG `InventoryItem` + RECEIVE txn; backflush components (ISSUE txns carrying consumed lot, apply `scrap_factor`, per-part flag); route through `get_audit_service`. **Idempotent** (reconcile re-enters). Mirror trace into `trace_serial`; populate MRP `on_order`.
Findings: INV-1, INV-2, INV-3, TRACE-2, TRACE-3, TRACE-4, TRACE-5, MS-4.

### Rank 10 — Labor-hour + job/actual-cost rollup ☐ (Batch 7)
Accumulate auto-closed TimeEntry `duration_hours` into op/WO actual hours; extract `JobCostingService.recompute_from_time_entries`; set `JobCost.status=COMPLETED`; populate `WorkOrder.actual_cost`/`estimated_cost` (or repoint report to JobCost); single configurable labor rate replacing hardcoded $45/$50.
Findings: COST-1, COST-2, COST-3, COST-4, COST-5.

### Rank 11 — OEE/OTD metric correctness + dead auto-OEE endpoint ☐ (Batch 8 · KPIs move)
Fix `oee.py` `TimeEntry.start_time→clock_in`/`end_time→clock_out` (endpoint dead today); derive ideal cycle/good/defect properly; availability on staffed time; consistent produced/scrapped; OTD returns n/a on empty set not 100%.
Findings: OEE-1, OEE-4, OEE-5, OEE-6, OEE-7, COST-5, MS-5.

### Rank 12 — Indexes + de-risk reconcile-on-read ☐ (Batch 9 · migration)
Migration: `ix_time_entries_operation_clock_out`, `ix_woo_work_order_sequence` (CONCURRENTLY, idempotent). Bound the dashboard reconcile (no `.limit()` today) / move to debounced ARQ; compute ETag before reconcile; grouped predecessor query; `commit=False` on `update_availability_rates`.
Findings: PERF-1, PERF-2, PERF-3, PERF-4, PERF-5.

### Rank 13 — Frontend completion UX hardening ☐ (Batch 10)
Invalidate `/shop-floor/dashboard` cache after completion mutations; in-flight guards on Complete buttons; memoize/window the WO list.
Findings: FEPERF-1, FEPERF-4, FEPERF-5.

## Completeness critic — follow-up gaps the audit did NOT cover
1. **[high] Parent/child assembly rollup entirely unimplemented** — completing child WOs never advances the parent; parent can complete with children open. (`work_order.py:47,98`, `laser_nest_service.py:98`)
2. **[high] Shipping `mark_shipped` has no inventory decrement and no over-ship guard.** (`shipping.py:282-325`) — *The unaudited part is closed in Batch 1: the WO CLOSED transition now writes a tamper-evident `audit_log` row via `AuditService.log_status_change`. The missing FG decrement and over-ship guard remain.*
3. **[high] Reports/exports surface the never-computed `actual_cost`/`actual_hours` as truth** — every WO cost/hours report is structurally zero. (`report_builder.py:38-52`)
4. **[high] ECO complete/implement has zero effect on `affected_work_orders`** (revision-control gap); `get_affected_items` is cross-tenant. (`engineering_changes.py:543,717`)
5. **[medium] TimeEntry approval is dead/disconnected** from costing; **no operator-certification gate** on clock-in/completion. (`time_entry.py:51`, `operator_certifications.py`)
6. **[medium] `complete_work_order` can resurrect a CLOSED/shipped WO** (no terminal-state lock); **CoC is a bare boolean, never generated.**

> These become Batch 11 (follow-up) after the ranked plan, pending triage.
