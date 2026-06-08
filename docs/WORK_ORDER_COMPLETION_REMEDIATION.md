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
> 4. **Pre-existing: `mark_shipped` has no `require_role`.** `POST /shipping/{shipment_id}/ship`
>    depends only on `get_current_user` + `get_current_company_id` — any authenticated user in the
>    tenant can close a WO by shipping it. This pre-dates Batch 5 (not introduced here) and is raised
>    separately for an RBAC gate.

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
> 4. **A1 (`audit_log.sequence_number` race) is amplified by read-path inventory audits.** Each FG
>    receipt / backflush now writes additional `audit_log` rows, and on the reconcile-on-read path
>    these are emitted under a GET — increasing the volume of concurrent audit writers that can collide
>    on the unserialized `max()+1` `sequence_number`. A1 remains the tracked dedicated fix (serialize
>    sequence allocation or catch-and-retry the collision).
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
