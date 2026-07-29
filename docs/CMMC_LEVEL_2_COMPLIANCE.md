# CMMC Level 2 Compliance Roadmap

## Overview

**CMMC Level 2** requires implementation of **110 security controls** from **NIST SP 800-171** across **14 control families**. This document tracks Werco ERP's compliance status and remediation roadmap.

**Target Certification Date**: _________________  
**Last Updated**: January 2026  
**Assessment Type**: Third-Party (C3PAO)

---

## Executive Summary

| Category | Status |
|----------|--------|
| Controls Implemented | ~45 of 110 |
| Critical Gaps | 6 |
| High Priority Items | 10 |
| Estimated Remediation | 8-12 weeks |

---

## Control Family Status

### ✅ ACCESS CONTROL (AC) - 22 Controls

**Current Implementation:**
- [x] Role-based access control (7 roles: admin, manager, supervisor, operator, quality, shipping, viewer)
- [x] Permission-based feature access
- [x] JWT token authentication
- [x] Session management with absolute timeout (24 hours)
- [x] Account lockout after failed attempts
- [x] Scoped single-endpoint display tokens for shop-floor TV wallboards (AC-3.1.2
  transaction/function limiting, A0.5): a `type="display"` JWT authenticates **only** the
  read-only `GET /shop-floor/wallboard` — every other endpoint rejects it with 401 via
  `verify_token`'s `type == "access"` check, so it can never act as a user session. Issuance and
  revocation are least-privilege gated (ADMIN/MANAGER) and tamper-evidently audit-logged; the
  `display_tokens` DB row — not the JWT — is the revocation/expiry and tenant-scope authority,
  re-checked on every request (revocation takes effect within one ~30s poll). The endpoint
  performs zero writes and truncates operator names to "First L." for public screens. The raw JWT
  is shown once at issuance and never stored server-side.
- [x] Multi-tenant data isolation enforced on shop-floor / work-order completion paths
  (AC-3.1.3 boundary control): the operation, clock, and completion endpoints
  (`/shop-floor/clock-in`, `/clock-out/{id}`, `/operations/{id}/start|complete`, and
  `work-orders` `/operations/{id}` update/start/complete plus `/work-orders/{id}/complete`/`/start`)
  scope every work-order, operation, and `TimeEntry` lookup to the caller's active company and
  return **404 before any mutation** on a foreign id, so a guessed identifier cannot drive another
  tenant's production records. Traceability, analytics/OEE, scheduling, and MRP services are
  tenant-scoped, and the real-time `/ws/updates` channel now requires authentication and delivers
  completion broadcasts only to the originating company's connections.
- [x] Concurrency-safe production records on the completion path (data-integrity hardening,
  Batch 2): the completion/clock endpoints take row locks (`SELECT … FOR UPDATE`) around the
  over-completion read-modify-write and enforce optimistic locking (`version_id_col` on
  `WorkOrderOperation` / `TimeEntry`) — a concurrent stale update returns **HTTP 409** rather than
  silently losing the write. A partial unique index
  (`uq_open_time_entry ON time_entries(user_id, operation_id) WHERE clock_out IS NULL`) DB-enforces a
  single open clock-in per user + operation (duplicate → **HTTP 400**), so a double-submit cannot
  create a second open entry and double-count labor/production. Migration `039`'s one-time dedupe of
  pre-existing duplicate open entries is non-destructive (closes the older rows, preserves
  `quantity_produced`) and logs the altered labor-record ids to the deploy output for AS9100D
  traceability rather than to the tamper-evident `audit_log`.
- [x] Cross-tenant job-cost recompute closed (AC-3.1.3 boundary control, Batch 7 / rank 10):
  `POST /job-costs/{id}/calculate` now looks up the job cost by id **and** active `company_id` and
  returns **404 before any recompute** on a foreign id — previously it resolved a `JobCost` by id
  alone and could recompute another tenant's job. The `WorkOrderOperation` lookup inside the shared
  recompute helper (`recompute_from_time_entries`) is likewise company-scoped, and the labor-rate
  resolver (`labor_cost_service`) filters every work-center lookup by company, so no cross-tenant rate
  or labor record can leak into a cost figure.
- [x] OEE-metric write authorization tightened (AC-3.1.5 least-privilege, Batch 8 / rank 11): the OEE
  **write/mutation** endpoints — `POST /api/v1/oee/calculate/{work_center_id}`,
  `POST`/`PUT`/`DELETE /oee/records`, and `POST`/`PUT`/`DELETE /oee/targets` — now require
  **ADMIN / MANAGER / SUPERVISOR** (`require_role(OEE_WRITE_ROLES)` in `app/api/endpoints/oee.py`),
  matching the sibling Analytics-write posture; they were previously open to **any** authenticated user,
  so any operator could create or overwrite OEE records and targets. OEE **read** endpoints
  (dashboard / trends / six-big-losses / list records & targets) remain open to any authenticated user
  so the shop floor can view dashboards (read-broad / write-restricted). See `docs/RBAC_PERMISSIONS.md`
  → OEE.
- [x] Database-level deny-by-default beneath app-layer tenant isolation (AC-3.1.3 boundary
  control, defense-in-depth — 2026-07-07, migration `059_supabase_rls_hardening`). The production
  Supabase Postgres exposed an auto-generated REST API ("Data API") whose roles
  `anon`/`authenticated` held FULL privileges on all 127 `public` tables with RLS off — the
  Security Advisor's `rls_disabled_in_public` ERROR, and real exposure: the ERP database was
  readable/writable to anyone holding the project anon key, bypassing the app entirely. A stray
  dashboard-created SELECT-to-public policy on `companies` additionally made tenant company data
  anon-readable. Migration `059` drops the stray policy, enables (non-`FORCE`) RLS on every
  `public` table with **zero policies (deny-all by design)**, and revokes all
  table/sequence/function privileges, schema `USAGE`, and default privileges for future objects
  from `anon`/`authenticated`. App-layer tenancy (`TenantMixin` + `tenant_query`/`tenant_filter`)
  **remains the enforcement**; RLS is a hard stop for the Data API surface, not a second tenancy
  implementation. No-op for the app (it connects as the table-owning `postgres` role with
  `BYPASSRLS`). Manual dashboard follow-ups (disable the unused Data API, SSL enforcement) are
  tracked in `docs/SUPABASE_SECURITY.md`.
- [x] Inventory tenant isolation + stock-mutation authorization closed (AC-3.1.3 boundary control /
  AC-3.1.5 least-privilege, inventory hardening PR 0 — `app/api/endpoints/inventory.py`).
  **Tenant isolation:** location codes, lot numbers, and warehouse names are not unique across
  companies, and several inventory lookups resolved them by value/id alone. `/inventory/receive`
  and `/inventory/transfer` now resolve the `InventoryLocation` and the existing/destination
  `InventoryItem` row against the active company (a foreign location code is **404**, never a valid
  receipt or transfer destination); `/inventory/low-stock` scopes the per-part on-hand aggregate, so
  another tenant's stock can no longer be summed into this company's on-hand figure; and the
  cycle-count path is scoped end to end — `POST /cycle-counts` enrolls only the active company's
  stock rows, `.../items/{item_id}/count` resolves both the parent count and the count item by
  company (**404** otherwise), and `.../complete` only adjusts inventory rows belonging to the
  active company.
  **Records-integrity finding — scope of impact (no affected rows).** Two `TenantMixin` inserts
  omitted the `company_id` stamp: `CycleCountItem` (in `POST /cycle-counts`) and the COUNT
  `InventoryTransaction` (in `.../complete`). Both columns are **NOT NULL** (`TenantMixin`; set NOT
  NULL for `cycle_count_items` and `inventory_transactions` by migration `026_add_multi_tenancy`),
  so those inserts raised `IntegrityError` at commit and the surrounding transaction **rolled
  back**: creating a cycle count whose scope matched at least one stock row, and completing a count
  with an adjustment to post, **always failed with a 500**. **No untagged row and no cross-tenant
  row was ever persisted by either path, and there are no affected records to remediate.** The
  unscoped enrollment query in `POST /cycle-counts` and the unscoped `InventoryItem` lookup in
  `.../complete` describe what those *queries* selected; because every such transaction aborted on
  the NOT NULL violation, they were **latent defects masked by the constraint**, not a source of
  cross-tenant data. The missing `company_id` predicate on `.../items/{item_id}/count` was likewise
  a genuine authorization defect in code but not exploitable in the field — `POST /cycle-counts` is
  the only writer of `cycle_count_items`, so no cross-tenant row could exist to be written onto
  (pre-`026` rows were backfilled to the single seeded company). Adding the stamps is what makes
  the enroll and complete paths function for the first time; the lifecycle guards and audit trail
  below ship with them for that reason.
  **Authorization (AC-3.1.5):** `POST /inventory/issue`, `POST /inventory/receive`, and
  `POST /inventory/transfer` now require **ADMIN / MANAGER / SUPERVISOR** (`require_role`), matching
  the sibling stock-mutating `/inventory/adjust` and the PO-receipt path `POST /receiving/receive`,
  which writes the same tables. All three previously depended on `get_current_user` only, so any
  authenticated tenant user — Viewer included — could create, move, or issue stock. The
  `docs/RBAC_PERMISSIONS.md` → Inventory **Transfer** row is now an enforced control rather than
  intended policy, and a **Receive** row was added. `POST /inventory/receive` additionally refuses a
  **soft-deleted** part with **400** (it resolved the part with no `is_deleted` predicate, so new
  stock and a ledger row could be created against a deleted part); `/inventory/low-stock` carries
  the same predicate.
  **Cycle-count authorization — one gate changed:** `POST /cycle-counts/{id}/start` and
  `.../items/{item_id}/count` were bare `get_current_user`, so **Viewer** — the read-only role,
  granted `inventory:view` and nothing else — could open a count and write the counted quantities a
  manager's ledger-posting `complete` derives its adjustment from. Both now use
  `require_role(COUNT_WRITE_ROLES)`, defined by **exclusion**
  (`ADMIN / MANAGER / SUPERVISOR / OPERATOR / QUALITY / SHIPPING`) so the entire shop-floor counting
  path is preserved and only the read-only role loses write access. `POST /cycle-counts` /
  `.../complete` keep the Admin / Manager / Supervisor gates they already had. Narrowing `start`
  further was proposed and **reverted before merge** — with the `record_count` IN_PROGRESS guard it
  would have left an operator unable to work a scheduled count. No owner sign-off is outstanding
  (no *working* role loses a capability); see `docs/RBAC_PERMISSIONS.md` → Inventory.
  **Audit coverage (AU-3.3.1):** `.../complete` previously adjusted stock and wrote **no**
  `audit_log` row at all. It now writes the `/inventory/adjust` dual-row convention per adjusted
  item (`inventory` CREATE for the COUNT movement + `inventory` UPDATE for the stock level) plus a
  `cycle_count` STATUS_CHANGE; `.../start` audits its status transition (or the re-assignment). The
  remaining two lifecycle writes are audited as well: `POST /cycle-counts` writes a `cycle_count`
  CREATE recording the declared scope and the number of stock rows enrolled (the step that defines
  what `complete` later adjusts), and `.../items/{item_id}/count` writes a `cycle_count_item` UPDATE
  for every counted quantity — carrying the previous values, so a legal re-count while the parent is
  IN_PROGRESS no longer destroys the only record of the quantity it replaced.
  Terminal-state **409** guards on `start` / `complete` close a ledger double-post in which a second
  `complete` appended a second COUNT transaction for the same physical variance; `complete` also
  takes a `SELECT ... FOR UPDATE` row lock on the count before that check-then-act guard, so two
  concurrent completions cannot both pass it and both post. `CycleCount.total_variance_value` is
  priced on the COUNT ledger row's own cost basis (the current `InventoryItem.unit_cost`), so it
  reconciles with the rows the completion wrote even when the unit cost moved after enrollment. See
  `docs/RBAC_PERMISSIONS.md` → Inventory and `docs/API.md` → Inventory.

**GAPS:**
- [ ] **AC-3.1.10 - Session Inactivity Timeout** ⚠️ HIGH
  - Need: 15-30 minute inactivity lockout
  - Effort: 3-5 days
- [ ] **AC-3.1.1 - Multi-Factor Authentication** 🔴 CRITICAL
  - Need: TOTP/SMS/Hardware token support
  - Effort: 2-3 weeks
- [ ] **AC-3.1.12 - Remote Access Control**
  - Need: VPN or additional controls for remote access
  - Effort: 1 week

---

### ✅ AUDIT & ACCOUNTABILITY (AU) - 9 Controls

**Current Implementation:**
- [x] Comprehensive audit logging (AuditService)
- [x] Correlation IDs for request tracing
- [x] IP address and user agent tracking
- [x] User action logging (create, update, delete, login, etc.)
- [x] Old/new value tracking for changes
- [x] Structured JSON logging in production
- [x] Production-event coverage (AU-3.3.1 audited events) extended to the work-order
  completion/close lifecycle: operation and work-order **start** and **completion** (both the
  shop-floor clock-out path and the office/admin `/operations/{id}/complete` path), the manual
  `/work-orders/{id}/complete` (status change plus the completion quantities it records),
  **shipment-close** (`mark_shipped` → work order `CLOSED`), inventory stock movements
  (`/receive`, `/issue`, `/transfer`, `/adjust` — each logs the transaction plus the resulting
  stock-level change(s)), and work-order **blocker** create / update / resolve (including any
  operation hold/resume they trigger). Each is written to the tamper-evident hash chain and
  flushed so the audit row commits atomically with the state change.
  AU-3.3.1 coverage also includes status transitions performed by the **reconcile-on-read** path
  (`reconcile_work_orders_from_completion_evidence`, invoked from dashboard / list / detail reads):
  when a read drives an operation or work order to COMPLETE from durable time-entry evidence, the
  read handler writes a tamper-evident status-change row per transition, **attributed to the
  requesting user** and tagged `extra_data.source = "reconcile_on_read"` (the reconcile itself has no
  actor, so it returns the transitions for the handler to audit before commit). This closes the
  previously-tracked AUD-3 gap. The reconcile write is best-effort — on any failure the mutation and
  its audit rows are rolled back atomically and the read still serves 200 (no orphaned, unaudited
  state change).
  AU-3.3.1 coverage also now records **quality-gate bypasses on completion** (Batch 4 / rank 7,
  warn-and-record): when an operation or work order completes while a quality gate is unsatisfied —
  `inspection_incomplete`, `open_ncr`, `fai_not_passed`, or `open_blocker` — the completion still
  succeeds but the system writes a tamper-evident `audit_log` row with action
  **`COMPLETED_WITH_QUALITY_EXCEPTION`** (distinct from a plain completion, so a bypass is greppable in
  the trail) carrying the exception codes and offending-record references, alongside a warning
  operational event. The new `MARK_OPERATION_INSPECTED` writer (the audited
  `inspection_complete = True` sign-off) is likewise recorded. This makes a completion past an open
  inspection / NCR / FAI / blocker an **attributable, tamper-evident record** rather than a silent
  event — the recorded-nonconformance control for **AS9100D 8.7 (control of nonconforming output)**:
  the system does not prevent the completion, but every nonconforming completion leaves a traceable
  record of who completed it and which gate was unsatisfied.
  AU-3.3.1 coverage also now records **completion-driven inventory movements** (Batch 6 / rank 9).
  When a work order reaches COMPLETE the system always receives the finished goods into inventory
  (a `RECEIVE` `InventoryTransaction`) and, when the part opts into backflush, consumes its BOM
  components (`ISSUE` transactions) — **every one of these movements is written to the tamper-evident
  hash chain** via `AuditService`, flushed atomically with the completion, exactly like the manual
  `/inventory` movements. A **backflush shortage** (a component driven to negative on-hand) is not
  silent: it writes a tamper-evident `BACKFLUSH_SHORTAGE` `audit_log` row (shortfall qty + consumed lot
  + producing work order) plus a `backflush_shortage` warning operational event, so the negative
  material-trail condition is attributable and recorded.
  **"When the part opts into backflush" became a reachable condition on 2026-07-27 (PR 4.5).**
  `parts.backflush_components` had **no writer anywhere in `app/`** through PR 4.4, so every sentence
  in this paragraph about the BOM component leg described code that had never executed against
  production data. The flag is now settable — on `PUT /parts/{id}` and `PUT /materials/{id}` only,
  behind one shared refusal gate (**409** while a blocking BOM-readiness diagnostic stands, evaluated
  **before** any field is written), absent from every create path and both CSV importers, and still
  `server_default false`. **Arming it is itself an audited state change** whose `extra_data` carries the
  readiness verdict that authorized it — the fact an assessor cannot reconstruct afterwards, since the
  BOM it judged stays editable. **That gate is a ONE-TIME check and it covers the BOM half only.** It is
  evaluated at the instant of the flip and never again; the routing half is not evaluated even then; and
  every input it read stays editable afterwards by anyone with `boms:edit` / `routings:edit`, with
  nothing on those edit paths aware that a part is armed. What backs it after the flip is a
  completion-time **refusal**: each `blocking` diagnostic the resolver raises drops the demand it
  describes and writes a tamper-evident **`BACKFLUSH_DEMAND_REFUSED`** `audit_log` row naming the
  condition, the BOM line or operation, and the quantity that did **not** move — plus a warning
  **`material.backflush_demand_refused`** notification, once per refused scope, so the refusal is not
  merely recorded where nobody is watching. The paragraph's claims
  about the component leg therefore remain **unproven in production** rather than **unreachable**: the
  difference is that a shop can now create the condition, and the accepted residuals on who may do so
  (Supervisor-tier, no reason captured, no 409 on a concurrent flip) are in the 2026-07-27 (PR 4.5)
  changelog row — which also records that **sign-off was not obtained** and that exposure preceded the
  owner acceptance PR 4 made its condition.
  **Two corrections to the above landed with PR 4.4 (2026-07-27).** (i) The `backflush_shortage` event
  had **no notification-catalog entry** from Batch 6 until PR 4.4, and the outbox tee ignores
  uncataloged event types by design — so the shortage was recorded on the hash chain but **notified to
  nobody**. Catalog key **`material.backflush_shortage`** (Purchasing, warning, in-app + email) closes
  that; the `audit_log` row, not the notification, remains the compliance record. (ii) The shortfall
  used to be computed against lots the draw never walked, so a **multi-lot** component could be driven
  deeply negative with **no** shortage row and **no** event at all; it is now computed against the lots
  actually walked, and the row additionally discloses **why the rest of the stock was not drawn** — on
  an unpinned draw, the segregated stock the predicate passed over (`held_quantity_skipped` /
  `held_lot_numbers`), so a shortage is never reported bare against material physically on the rack; on
  a **pinned** draw, the pin itself (`pinned_lot`), because there the pin — not any lot's status — is
  the constraint, and naming held lots would send an MRB reviewer to release quarantined stock the pin
  excludes anyway while omitting the available stock it also excluded. The two clauses are mutually
  exclusive by construction. **Both live in the `audit_log` row and the event payload only**: the
  as-built genealogy reconstruction reads `inventory_transactions`, so a skipped lot is never named on
  a genealogy line — see the owner-acceptance item in the 2026-07-27 changelog row.
  Coverage extends to **tied-material consumption** with the material-consumption engine
  (2026-07-25, PR 1). Where an OPEN **operation-scoped** `work_order_material_allocation` ties a
  material part to an operation, every completion-path entry posts per-run `ISSUE` transactions
  (`reference_type='work_order_operation'`, `reference_id` = the operation) and **each movement is
  written to the tamper-evident hash chain** via `AuditService` — the ledger row (`inventory` CREATE),
  the resulting stock-level change (`inventory` UPDATE), and the tie's `qty_consumed` advance
  (`work_order_material_allocation` UPDATE) — flushed atomically with the completion on the live paths
  and with the read's own commit on the reconcile-on-read path. The tie's own lifecycle is audited on
  the same chain: create, edit, and untie (`log_delete` with `soft_delete=True` — the tombstone is
  `status = cancelled`, **not** `is_deleted`, and the row is never physically deleted so the ledger's
  `allocation_id` back-reference keeps resolving), plus both automatic cancellations (work-order
  delete and nest re-import, below). A **tied-material shortage** mirrors the backflush case: the
  source lot is driven negative and the system writes a tamper-evident **`ALLOCATION_SHORTAGE`**
  `audit_log` row (shortfall / required / available quantity, allocation, part, consumed lot, work
  order and operation) plus a `material_allocation_shortage` warning operational event (notification
  catalog `material.allocation_shortage`). The action string is deliberately **distinct** from
  `BACKFLUSH_SHORTAGE` so the two mechanisms stay separable in the trail.
  The **work-order-scoped** tie shape is audited symmetrically (closed in the PR-1 compliance
  re-audit): its `qty_consumed` advance, written when the completion backflush drains it, is a state
  change on a tenant table *and* the exact field the untie guard keys on (409
  once anything is consumed), so it writes its own `work_order_material_allocation` UPDATE row.
  **PR 4.4 corrected what that advance records** (`extra_data.reference_type` is now
  `"work_order_backflush"`): it used to be set to `qty_planned` *regardless of what actually posted*,
  making the cache a claim rather than a record, and it is now re-read from that tie's **own signed
  ledger net** after posting. Two verbs key on it exactly — `return_and_untie` gives back precisely
  `qty_consumed`, and `correct_over_consumption`'s allowance is `qty_consumed − target` — so
  cache == net is what makes both of them exact rather than approximately right.
  **That correction reaches the work-order-scoped shape only.** The operation-scoped engine still
  writes `qty_consumed = target` (its run-scaled `qty_per_run × (complete + scrapped)`), so since
  PR 4.4 the same column is a ledger-backed **record** on one tie shape and the engine's **intent** on
  the other. The asymmetry is deliberate — the per-run engine recomputes `target` from live operation
  state on every pass and converges by reconciliation — and the standing rule is unchanged and covers
  both: **an authoritative consumed total comes from the signed ledger sum on that `allocation_id`,
  never from the cache.**
  Two further tamper-evident actions were added at the
  same time, both for conditions that were previously invisible: **`HELD_MATERIAL_CONSUMED`** — a
  PINNED lot that is inactive or not `available` (`on_hold` / `quarantine` / `rejected`) is still
  consumed (the material is already in the part, and the path runs from a GET where refusing would be
  unattributable) but the fact is recorded with lot, status, tie and quantity for the AS9100D 8.7
  segregation review; pinning such a lot is refused up front with **422** at the tie endpoint, so the
  row can only mean "held after it was pinned". Both tie shapes run the same `is_consumable_item`
  predicate.
  **PR 4.4 narrowed this row to exactly one meaning, which is a strengthening of the 8.7 posture.**
  Until then the *unpinned* work-order-scoped leg had **no `status` predicate at all** and could pick
  an already-held lot automatically, recording it with `extra_data.pin_directed = false` instead of
  skipping it. Both engines now share one consumable predicate, so an unpinned draw **skips** held and
  inactive stock; what replaces the record is **disclosure on the shortage row**
  (`held_quantity_skipped` / `held_lot_numbers`, also in the description), so segregated material is
  never silently consumed *and* never silently omitted from the reason a job came up short. On a
  **pinned** draw the row names the pin (`pinned_lot`) instead, because there the pin is the constraint
  and a held-lot clause would state a false cause. `pin_directed` survives as a field and is now always
  `true`.
  > **Two consequences of the skip that the AS-BUILT surface does not carry — owner acceptance, not a
  > defect.** Skipping segregated stock is the 8.7-correct answer and is not in question. But (a) the
  > `held_*` disclosure lives only in `audit_log` and the operational-event payload — the as-built
  > reconstruction reads `inventory_transactions` — so where a part's stock is **wholly** held, the
  > genealogy line now names a **lot-less placeholder** and no heat at all, where before PR 4.4 it named
  > the held lot with a `HELD_MATERIAL_CONSUMED` row beside it. The new record is arguably more truthful
  > (the material genuinely was not drawn) and the shortage now actually notifies, but an auditor
  > reading the as-built record **alone** can no longer reach the segregated-material fact and must pull
  > the audit row. And (b) the skip makes the zero-quantity placeholder row newly reachable on the
  > backflush leg, and the PR 3 return verb refuses to credit a placeholder — so those rows are
  > **permanently un-returnable**. Both are recorded in the 2026-07-27 changelog row's owner-acceptance
  > list and in `docs/MATERIAL_CONSUMPTION_PLAN.md` → Open questions, item 6.
  One deliberate **widening** ships with it and is the only live behaviour change in that PR: the
  shared predicate is `COALESCE(status, 'available') = 'available'`, so a legacy **NULL-status** lot is
  consumable. `inventory_items.status` has a Python-side default with no `server_default` and no
  backfill, so a bare `status = 'available'` would have hidden real stock and driven the engine to mint
  a lot-less placeholder and record a **false** shortage against material that exists.
  > **Disclosure — lot-hold state is not an operating control yet.** **Nothing anywhere in `app/`
  > writes a held `InventoryItem.status`.** No endpoint or schema exposes the column, it is only ever
  > set to `"available"` at row creation, and there is no verb that deactivates a lot. Both halves of
  > the control above — the 422 pin refusal and the `HELD_MATERIAL_CONSUMED` row — can therefore only
  > fire on state set **outside** the application (a direct database write, an import, or a future
  > hold verb). They are built and tested ahead of the feature that will produce that state and must
  > **not** be presented as evidence that quarantine/hold segregation is enforced in-system today.

  The last of the three new actions is **`ALLOCATION_CONSUMPTION_FAILED`**
  (`success=false`) for an allocation whose consumption raised and was rolled back to its savepoint,
  since material that should have depleted and did not is a stronger control gap than the shortage
  case that already wrote a row.
  **PR 4.4 added its twin on the backflush leg — `BACKFLUSH_COMPONENT_FAILED`** (`success=false`,
  component part + work order + the exception text), for exactly the same reason, and closed the defect
  that made the pair necessary. Both non-indexed `ISSUE` legs used to post with
  `duplicate_is_noop=True`, which swallows **every** `IntegrityError` and reports it to the caller as
  "a concurrent completion already wrote this row". That is only correct while a unique index backs the
  row — and **no index has ever covered either leg**, so the operation-scoped tie engine has been
  silently converting real faults (foreign key, NOT NULL, or `chk_inventory_items_quantity_non_negative`
  where it is live) into recorded *shortages* on a live path since PR 1. Both now pass
  `duplicate_is_noop=False`: a genuine fault rolls back that one component or allocation, writes its
  chain row, leaves the rest of the work order unaffected, and leaves the outer transaction committable
  so a reconcile-on-read `GET` still returns 200 rather than a `PendingRollbackError` 500. **A refused
  write can no longer be recorded as a shortage** — a wrong cause on a compliance record is worse than
  a missing one. The finished-goods `RECEIVE` deliberately keeps `duplicate_is_noop=True`, because
  `uq_wo_inventory_receipt` genuinely backs it and it is genuinely exposed to the lock-free reconcile
  race migration `041` was written for.
  **Both failure actions also NOTIFY, and that is load-bearing rather than tidy.** The audit rows alone
  made the degraded path strictly **quieter** than the lesser condition it degrades from: a shortage
  still moves stock and reaches Purchasing through `material.allocation_shortage` /
  `material.backflush_shortage`, while "the draw raised and rolled back, so nothing moved at all"
  reached nobody. That is not a corner case — on a database where
  `chk_inventory_items_quantity_non_negative` **is** live, *every* shortage arrives here instead, so the
  shortage notification would be exactly the one that never fires. Catalog keys
  **`material.allocation_consumption_failed`** and **`material.backflush_failed`** (Purchasing, warning,
  in-app + email) carry the degraded case, separately keyed so "stock went negative" and "stock never
  moved" can be told apart without opening the audit log. The `audit_log` rows remain the compliance
  record. **What is still undecided is the posture, not the signal**: on a CHECK-live database no stock
  moves, no `ISSUE` row posts, the ledger under-reports the job's consumption and the as-built record
  shows material that was never drawn. See the open item in the 2026-07-27 changelog row.

  > **OPEN — owner decision required: negative on-hand on shortage, now TWO mechanisms.** The
  > negative-stock-on-shortage posture flagged for review in
  > `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` (Batch 6) — a component driven below zero still
  > completes the work order by design — **has never been accepted or rejected by an owner, and PR 1
  > extends it to a second, independently-reachable mechanism**: `ALLOCATION_SHORTAGE` on tied
  > material, which additionally creates a **zero-quantity placeholder stock row** when the part has
  > no lot at all, so the negative movement is still recorded against a real inventory row rather
  > than a dangling reference. Both mechanisms *record* the condition (tamper-evident row + warning
  > event); **neither prevents it**, and both can be driven from the reconcile-on-read GET path,
  > where the actor is whoever happened to load the page. This is an **outstanding
  > quality/compliance acceptance item, not a settled posture** — if the owner rejects it, both
  > mechanisms change together.

  **AS9100D 8.5.2 (identification & traceability):** because the finished-goods receipt assigns and
  records a work-order lot and the backflush carries the consumed component lots, **as-built lot
  genealogy is now reconstructable** from a single trace — `GET /traceability/lot/{lot}` reports the
  producing work order and its `consumed_components` (component part / lot / quantity), and
  `GET /traceability/serial/{serial}` mirrors the work-order/NCR collection. All trace queries are
  tenant-scoped. Genealogy reads **all three** work-order ledger reference types — `work_order` (the
  finished-goods receipt, plus every legacy pre-PR-4.4 component `ISSUE`), `work_order_backflush`
  (PR 4.4: the reconciling component leg — BOM/routing demand *and* work-order-scoped ties —
  `reference_id` = the work order) and `work_order_operation` (per-run tied-material
  consumption, whose `reference_id` is resolved back to its work order so all three collapse into the
  same per-work-order genealogy lines). Each extension shipped **in the same PR as the reference
  type it reads**: there is deliberately no release in which consumption exists but the as-built record cannot
  see it. PR 4.4 needed **one line** in the shared `work_order_ledger_filter` and **no change** to
  `traceability.py` at all. One consequence is intended and should not be read as duplication: the
  component leg now **spills across lots**, so one logical draw appears as N genealogy lines naming N
  heats — which is the truthful as-built record the single summed row could not produce.
  **One limit of the as-built surface is stated here rather than left to be discovered:** the
  reconstruction reads `inventory_transactions` only, so a lot the draw **skipped** — segregated stock
  under the PR 4.4 8.7 posture — appears on no genealogy line at all; where a part's stock is wholly
  held, the line names a lot-less placeholder and no heat. The skipped lot and quantity are on the
  `BACKFLUSH_SHORTAGE` / `ALLOCATION_SHORTAGE` `audit_log` row, which is where that question has to be
  answered from. Owner-acceptance item, 2026-07-27 changelog row. Tied consumption posts as `ISSUE` **even for scrapped runs** (the good/scrap split is
  recorded in the transaction `notes`), precisely so audited scrap material cannot vanish from the
  as-built record — genealogy filters on `ISSUE`. Historical ledger rows are **not** backfilled; they
  truthfully carry `work_order` only, and pre-feature rows keep a NULL `allocation_id`. **PR 4.4 held
  that no-backfill rule where it cost something**: existing `('work_order', ISSUE)` rows were **not**
  re-keyed to the new shape. Re-keying would have mutated regulated, hash-chain-adjacent records that
  no audit row covers and silently moved history between reference shapes; the code-level legacy fence
  below achieves the same safety without touching a posted row.
  **Non-duplication of regulated inventory/traceability records — which mechanism guarantees what.**
  A re-completion, a concurrent double-complete, or a reconcile-on-read re-read must not duplicate a
  regulated inventory row. Two different guarantees apply, and they are not interchangeable — and as of
  PR 4.4 the **algorithmic** one covers a second path:

  * **Work-order-scoped movements** — the finished-goods `RECEIVE` and (pre-PR-4.4) the one-shot
    backflush / work-order-scoped-tie `ISSUE`, both `reference_type='work_order'` — are guarded **at the database
    level** by migration `041`'s two partial UNIQUE indexes: `uq_wo_inventory_receipt`
    (`company_id, reference_id`, WHERE `reference_type='work_order' AND transaction_type='RECEIVE'`)
    and `uq_wo_inventory_issue` (`company_id, reference_id, part_id`, same predicate with `'ISSUE'`).
    These indexes govern **those rows and only those rows**: at most one finished-goods receipt per
    work order, and at most one issue per (work order, component part). That coverage is **unchanged**
    by the material-consumption work, **including PR 4.4** — which ships **no Alembic revision at all**
    (head stays `076_uq_wo_inv_sqlite_parity`) and leaves both indexes at their exact `041`/`076`
    definitions on both dialects, verified by the untouched migration-lockstep test modules. What PR 4.4
    changed is which rows *arrive* under `reference_type='work_order'`: after it, the finished-goods
    `RECEIVE` is the only shape still written there. Every component `ISSUE` carrying it is now
    **legacy** (pre-4.4), and `_component_already_issued` — kept verbatim on that predicate — is their
    permanent fence: a work order carrying one is fenced out of the reconciling engine entirely, which
    makes a double-issue against a historical summed row **structurally impossible** rather than
    arithmetically avoided. Legacy work orders keep exactly the behaviour they had, forever.
  * **Reconciled component consumption (PR 4.4)** — `reference_type='work_order_backflush'`,
    `reference_id` = the work order — is the second path with **no DB-level uniqueness constraint, and
    that too is deliberate**: the leg spills across as many lots as the demand needs and must be able to
    post a later top-up row, so it needs N rows per (work order, part) where the index permits exactly
    one. Its guarantee is the same **algorithmic** one: per demand source,
    `delta = target − signed ledger net`, posted only when positive; a non-positive delta is a **silent
    no-op, never an auto-reversal** (the path runs from a `GET` with no actor and no reason). The two
    nets are disjoint and complete — the BOM net filters `allocation_id IS NULL` and every tie-driven
    row carries one — so neither leg can suppress itself or consume the other's history. **Serialization
    is pre-existing, not newly asserted**: all four write entries already hold `SELECT … FOR UPDATE` on
    the `work_orders` row before the inventory legs, and the two reconcile-on-read entries always UPDATE
    that same `version_id_col`-mapped row, so a loser raises `StaleDataError` and its whole reconcile
    rolls back. **No new `FOR UPDATE` was taken**, deliberately: it would have been the codebase's first
    on a `GET`, inside a loop guarded by `except Exception: pass`, which cannot rescue a lock *wait*.
    One addition is load-bearing — a `db.flush()` after the untied-work-order early return and **before
    the first savepoint**, so an autoflush `StaleDataError` propagates as the handler's documented 409
    instead of being degraded into a `BACKFLUSH_COMPONENT_FAILED` row.
    **What this path still does NOT have is a re-entry trigger.** PR 4.4 changed no call site, and every
    operation-completion handler refuses a terminal parent, `complete_work_order` early-returns for
    COMPLETE/CLOSED, reconcile-on-read strips terminal work orders from its candidate set, and
    COMPLETE → non-terminal is blocked — so **the leg still runs exactly once per work-order lifetime**
    and a later rise in `quantity_complete` still issues nothing. Convergence is the *precondition* for
    ever adding a trigger safely, not the trigger.
  * **Operation-scoped material consumption** — `reference_type='work_order_operation'` — deliberately
    posts **many `ISSUE` rows per (work order, part)**, one per top-up as runs complete, and therefore
    sits **outside** those predicates by construction. **This path has no DB-level uniqueness
    constraint, and that is deliberate**, because "one row per key" is the wrong rule for an
    incremental ledger. Its non-duplication guarantee is **algorithmic — sum-delta convergence**: on
    every call the engine recomputes `target = COALESCE(qty_per_run, 1.0) × (quantity_complete +
    quantity_scrapped)` from **live operation state** and posts only `delta = target − qty_consumed`,
    so a **replay** computes `delta = 0` and writes nothing.
    **That argument is sequential, and the distinction matters.** It holds for a re-entry that
    observes the previous call's committed `qty_consumed`; it does **not**, on its own, make two
    *simultaneous* completions of one operation safe, because these rows sit outside the unique index
    by design and `WorkOrderMaterialAllocation` carries no `version` column — nothing in the engine
    would stop both racers computing the same positive delta and both posting. What actually
    serializes them is the **optimistic lock** (invariant 4): `WorkOrder` and `WorkOrderOperation`
    map `version_id_col` directly, so every call site that drives a completion takes it and a stale
    racer raises `StaleDataError` → HTTP 409 before reaching the engine. The per-allocation
    `SAVEPOINT` and the rule that `qty_consumed` advances **only** when an insert actually landed are
    damage control behind that lock, not a substitute for it. Stated plainly: on this path
    non-duplication rests on the engine **plus the work-order lock**, not on the schema — so it must
    be preserved by review and test rather than assumed from a constraint, and a future completion
    call site that mutates neither locked row would step outside the protection. The sequential
    property is pinned by
    `backend/tests/api/test_material_consumption.py::test_replay_is_idempotent_by_construction`.
  * **What migration `076` means for test evidence.** The `041` indexes originally declared only
    `postgresql_where`, which SQLite ignores — so on SQLite (local dev and the entire pytest suite)
    they degraded into **full** unique indexes covering *every* `reference_type`. Any **pre-`076` test
    evidence** touching these guards therefore exercised **stricter, different constraint semantics
    than production Postgres** ever enforced. Migration `076` declares `sqlite_where` from the same
    predicate constant so the harness now enforces what production enforces; on **Postgres it emits
    zero DDL** (dialect-guarded no-op — the production indexes were always correct and were not
    rebuilt). For the rows the guards exist for, coverage is bit-identical before and after on both
    dialects; the relaxation reaches only rows the guards were never meant to cover (including this
    consumption path). See `docs/DEVELOPMENT.md` → Completion-inventory migrations.
  AU-3.3.1 coverage also now records **completion cost/hours rollup and job-cost status changes**
  (Batch 7 / rank 10), which surface in compliance-facing cost reports. The labor-hour + actual-cost
  rollup is opt-in (global flag `LABOR_COST_ROLLUP_ENABLED`, default OFF); **when enabled**, a
  completing work order writes one tamper-evident `audit_log` row recording the rolled-up actuals
  (action `cost_rollup`: old/new `actual_hours` and `actual_cost`), and the linked `JobCost` flip to
  status `COMPLETED` writes its own tamper-evident row — both via `AuditService`, flushed atomically
  with the completion. Separately, and **regardless of the flag**, a work order completed with one or
  more operations that recorded **zero** labor writes a tamper-evident `COMPLETED_WITH_QUALITY_EXCEPTION`
  row (code `no_labor_recorded`) plus a `quality_exception_on_completion` warning event, so a
  potentially understated cost/hour record is attributable rather than silent.
  AU-3.3.1 coverage also now records **laser-nest package (re-)import** symmetrically (2026-06-23).
  Importing a nest package onto a laser WO (the assembly's child laser WO — or, since the
  standalone-nest feature, a standalone/directly-addressed laser-cutting WO) replaces all prior
  nests — the
  IMPORT-REPLACES-EVERYTHING product decision. The destructive wipe is now audited: each superseded
  nest is written as a `log_delete` (`reason="superseded_by_reimport"`) **before** the rebuild, and
  each rebuilt nest as a `log_create`, for **both** import shapes — the legacy CNC-program path now
  also writes the per-nest `log_create` (`source="cnc_file_import"`), matching the PDF path
  (`source="pdf_import"`); previously the legacy path emitted only a websocket event and the wipe was
  unrecorded. All rows are flushed atomically with the rebuild. This closes a prior asymmetry where
  the destructive supersession wipe and the legacy create path left no `audit_log` trail.
  Coverage expanded further with the standalone-nest work (2026-07): a (re)import onto an existing
  laser WO also writes a **WO-level `log_update`** (reason `laser_nest_package_import`: forced
  RELEASED status, zeroed produced quantities, re-derived `quantity_ordered`), the manual nest add
  writes the same WO-level `log_update` (reason `manual_laser_nest_added`), and the standalone
  import audits the **creation of the fresh part-less laser work order** (`log_create`,
  `source="laser_nest_standalone_import"`) — all flushed atomically with their transactions.
  The material-consumption work (2026-07-25) extends this to the **material ties the wipe destroys**.
  The rebuild now resolves the operations it will delete **before** deleting anything, so the tie
  guard runs first: if any allocation on a to-be-wiped operation has **already consumed material**,
  the import is refused with **HTTP 409 and nothing is destroyed** — deleting an operation out from
  under its `ISSUE` rows would orphan the lot genealogy those rows carry. Unconsumed ties on the wiped
  operations are **cancelled, not deleted** (`status = cancelled`; the row is retained so the ledger's
  `allocation_id` back-reference always resolves), and each cancellation writes its own tamper-evident
  `log_delete` row on resource type `work_order_material_allocation`
  (`reason="superseded_by_reimport"`, recording the prior status and `qty_consumed`), flushed in the
  same transaction as the per-nest supersession rows above. **Work-order deletion** is covered
  symmetrically: a **soft** delete auto-cancels every OPEN tie (audited, `reason="work_order_deleted"`)
  and is **never refused** because of a tie — posted consumption stands, since the material was
  physically used and the ledger is the compliance record, so only the forward-looking demand is
  closed out; a **hard** delete (draft/cancelled only) is refused **409** when any tie carries
  consumption, and audits each unconsumed tie it removes with the work order.
  *Known gap (tracked, architectural follow-up):* the supersession wipe is still a **hard
  cascade-delete of soft-deletable `LaserNest` rows** (not a `soft_delete`), so the soft-delete
  invariant is not yet fully satisfied for this path — the improvement here is that the deletion is
  now *audited*, not that the rows are preserved. Re-modeling the import wipe as a soft-delete is a
  separately-tracked follow-up.
  *Known gap (tracked):* the root `audit_log.sequence_number` (`max()+1`) allocation is still not
  serialized under concurrent writes — see follow-up A1 in `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`
  (amplified in Batch 6 by the additional read-path inventory audit rows).

**GAPS:**
- [x] **AU-3.3.8 - Protect Audit Information** ✅ COMPLETE
  - Implemented: Immutable audit logs with hash chain integrity
  - Features: SHA-256 hashing, sequence numbers, database triggers prevent UPDATE/DELETE
  - API: /audit/integrity/status, /audit/integrity/verify (Platform-Admin only — the chain is a
    single global sequence across all tenants; per-record verification at
    /audit/integrity/record/{sequence_number} is available to a company Admin for their own
    company's records)

  > **DB-level immutability is (re)ensured by migration `060_audit_log_immutability` —
  > prod gap found and fixed 2026-07-07.** The `tr_audit_log_no_update` / `tr_audit_log_no_delete`
  > triggers this control relies on were found **missing in production**: prod was bootstrapped
  > via `Base.metadata.create_all()` + `alembic stamp` past migration `008`, which silently
  > skipped `008`'s raw DDL (trigger functions/triggers aren't in SQLAlchemy metadata, so
  > `create_all` never creates them) — until the fix deployed, `audit_logs` had no DB-level
  > UPDATE/DELETE protection in prod. During that window the hash chain still made **mid-chain**
  > tampering *evident* (hash break / sequence gap), but a deletion of the newest rows before the
  > next insert would have re-chained seamlessly and gone undetected (`AuditService` chains from
  > the current tail); the triggers make both *refused*. **Post-fix follow-up:** run the
  > Platform-Admin chain verification (`/audit/integrity/verify`) against prod after the deploy
  > and record the result — and the bootstrap date, i.e. the window's start, if determinable —
  > in the Change Log below. Migration `060` idempotently re-creates the `008` trigger functions with
  > `SET search_path = ''` pinned and recreates both triggers if missing; its downgrade only
  > resets `search_path` and never drops the objects (`008` owns their lifecycle). Applied via the
  > normal `alembic upgrade head` at container boot. Bootstrap guidance to prevent recurrence:
  > `docs/DEVELOPMENT.md` → Bootstrap order; posture and verification SQL:
  > `docs/SUPABASE_SECURITY.md`.

  > **`company_id` is deliberately excluded from the AU-3.3.8 integrity hash — do not add it.**
  > Audit rows now carry a `company_id` so audit *retrieval* can be tenant-scoped, but `company_id`
  > is intentionally **not** part of the SHA-256 hash input (`compute_audit_hash`). Reasons:
  > (a) audit rows are already immutable at the DB layer via the `tr_audit_log_no_update` /
  > `tr_audit_log_no_delete` triggers (migration 008), so `company_id` cannot be altered
  > post-insert; (b) every pre-existing row — including the rows migration 026 backfilled to
  > `company_id = 1` — was hashed without it, so including it would change the recomputed hash of
  > every historical record, failing verification and breaking the chain wholesale; (c) keeping it
  > out means `company_id` can be safely backfilled in future without invalidating any integrity
  > hash. Tenant isolation of audit data is enforced at the **query layer** (retrieval endpoints
  > filter by `company_id`), not in the hash. No schema migration or backfill of existing
  > NULL-`company_id` rows was performed for this change: historical rows are left as-is and new
  > rows are stamped going forward.
  >
  > **Settings-audit trail parity.** The separate `SettingsAuditLog` table (admin / quote-config
  > changes, written via `log_change` in `app/api/endpoints/admin_settings.py` and retrieved at
  > `GET /admin/settings/audit-log`) is a `TenantMixin` table whose retrieval was already
  > company-scoped. Its **write** path now tags each row with the **active** company
  > (`current_user._active_company_id`, the company resolved by `get_current_company_id`), falling
  > back to the user's home company on non-request paths — the same precedence as
  > `AuditService._resolve_company_id`. Previously it always wrote `current_user.company_id`. This
  > is a defense-in-depth correctness fix that brings settings-audit attribution to parity with the
  > main `AuditLog`; it is **not** a fix for a live cross-tenant write, because a platform admin who
  > switches into another company is placed in a **read-only** context (`switch_company` issues a
  > `read_only` token and `get_current_user` rejects all non-safe-method requests with 403), so the
  > admin-settings write endpoints are unreachable in that context.

  > **Retention vs. immutability — reconciled by archive-never-delete.** Records-retention
  > obligations do not override AU-3.3.8 immutability. Audit logs are **never row-deleted**: a missing
  > `sequence_number` reads as a `sequence_gap` tamper indicator, so deleting an aged row would itself
  > break verification. Reconciliation:
  > - The maintenance cleanup job (`cleanup_old_logs_task`) **no longer deletes audit logs** (it
  >   previously hard-deleted them after 90 days). It now purges only ephemeral, non-audit operational
  >   data (completed background-job tracking rows and notification logs).
  > - Aged audit rows are **archived to cold storage, not deleted**, by the monthly
  >   `archive_aged_audit_logs_task` (`AuditArchivalService`). It verifies each row's integrity hash,
  >   exports the segment to NDJSON, records the export in the governance `ExportEvent` ledger, and
  >   writes an `EXPORT` audit entry. **Live rows stay in place, so the hash chain remains fully
  >   verifiable.** Retention windows come from the per-company `security_audit_record`
  >   `RetentionPolicy` (migration 030; default 1095 days / 3 years), falling back to
  >   `AUDIT_RETENTION_DAYS_DEFAULT`.
  > - **Partition-drop is the only physical-removal path.** If aged rows must ever be physically
  >   removed from the online DB for storage, it is a deliberate, documented DBA partition-drop —
  >   preconditioned on the segment being archived + sha256-verified to cold storage, no active
  >   `LegalHold`, legal review where `requires_legal_review_before_purge` is set, and a **contiguous
  >   range across all tenants** (the chain is one global sequence). It is **never** an automated row
  >   delete and **never** done by disabling the `tr_audit_log_no_update` / `tr_audit_log_no_delete`
  >   triggers. Full procedure: `docs/AUDIT_LOG_RETENTION_RUNBOOK.md`.
- [ ] **AU-3.3.9 - Audit Log Backup**
  - Need: Audit logs backed up to separate system
  - Effort: 3-5 days

---

### ⚠️ AWARENESS & TRAINING (AT) - 3 Controls

**Current Implementation:**
- [x] In-app tour system for user onboarding
- [ ] Security training tracking

**GAPS:**
- [ ] **AT-3.2.1 - Security Awareness Training**
  - Need: Track employee security training completion
  - Effort: 1 week (or manual process)
- [ ] **AT-3.2.2 - Role-Based Training**
  - Need: Document role-specific security responsibilities
  - Effort: Process documentation

---

### ✅ CONFIGURATION MANAGEMENT (CM) - 9 Controls

**Current Implementation:**
- [x] Environment-based configuration (.env files)
- [x] Docker containerization
- [x] Infrastructure as code (docker-compose)
- [x] Version control (Git)
- [x] Enforced change-control path to production (CM-3 partial). Application/source changes
  reach the deployed `main` branch **only through a pull request whose CI status checks
  pass** — enforced by a GitHub repository ruleset on `main` (PR required before merge,
  required status checks, force-push and branch deletion blocked). Merge-when-green: the
  ruleset requires **0 human approvals**, so the control is *tested-before-merge* (CI), not
  *peer-reviewed-before-merge*; do not claim a manual review gate. A merge to `main`
  **auto-deploys to production** via GitHub Actions with **no manual deployment-approval
  gate** (the `production` environment's required-reviewer rule was removed 2026-06-22).
  Compensating deploy-time controls: a deployment-branch policy that permits **only `main`**
  to deploy, and **post-deploy health checks that fail the job on a bad deploy**
  (`Verify Production Deployment` in `ci-cd.yml`; `Verify deployment serves the Vite
  frontend bundle` in `deploy-frontend-production.yml`). Repo admins hold a documented
  break-glass bypass for emergencies; rollback is redeploying a known-good commit (or
  re-adding the reviewer rule). See `docs/CI_CD_SETUP.md` and `docs/DEPLOYMENT_RUNBOOK.md`.
- [x] Change control over **released manufacturing routings** — "Proportionate (audit-trail)"
  posture (CM-3.4.1 / CM-3.4.2 baseline + change restriction, AS9100D 8.5.1 control of production,
  `feat/routing-editable-time-standards`). A routing is a controlled production baseline: once
  **released**, its manufacturing **process** — operation sequence, work center, work/inspection
  instructions, inspection points, and the set of operations (add/delete/reorder) — is **frozen** and
  can only change by creating a **new revision**, preserving the historical baseline (see the
  traceability/revision invariant in `CLAUDE.md`). The one sanctioned in-place change is to
  **time standards** (the editable set `TIME_STANDARD_FIELDS`: `setup_hours`, `run_hours_per_unit`,
  `move_hours`, `queue_hours`, `cycle_time_seconds`, `pieces_per_cycle`), which are estimating/cost
  inputs, not the process definition. `PUT /api/v1/routing/{routing_id}/operations/{operation_id}`
  (`update_operation`, `app/api/endpoints/routing.py`) enforces this:
  - **Change restriction.** On a released routing, any changed field outside `TIME_STANDARD_FIELDS`
    returns **400** (*"Released routing: only time standards (setup, run/unit, move, queue, cycle)
    can be edited — create a new revision to change the process."*). Adding, deleting, or reordering
    operations on a released routing likewise returns **400**. An **obsolete** routing is fully
    locked (all edits **400**).
  - **Least-privilege on released edits (AC-3.1.5).** Draft-routing edits are
    **Admin / Manager / Supervisor**; released time-standard edits are gated **in code** to
    **Admin / Manager only** (Supervisor → **403**), because editing live released content is
    release-adjacent authority and routing **Release** is itself Admin/Manager-only. Superuser /
    Platform Admin bypass mirrors the rest of the system. See `docs/RBAC_PERMISSIONS.md` → Routings.
  - **Tamper-evident change record (AU-3.3.1).** Every applied change is recorded on the
    `audit_log` hash chain — who / when / old→new values — via `AuditService.log_update` for
    operation edits, with `log_create` / `log_delete` on add/delete operation and
    `log_status_change` on release. The audit trail is the system of record for who-changed-what-when
    on time standards.
  - **Approval re-stamp.** A successful released time-standard edit re-stamps the routing's approval
    signature (`approved_by` = the editor, `approved_at` = now) so the live baseline reflects who
    last changed the production time standards; the original release date (`effective_date`) and the
    revision letter are **left unchanged** (an in-place edit, not a new revision). Draft edits do
    **not** re-stamp approval (the routing is not yet approved).
  - **Accepted residual.** Under the Proportionate posture there is **no per-operation history table**
    and **no optimistic-lock / version column** on routing operations — the tamper-evident `audit_log`
    is the sole history of record for time-standard changes, and concurrent released edits are
    last-write-wins rather than 409-guarded. This is an accepted limitation for this control, not an
    open gap.

**GAPS:**
- [ ] **CM-3.4.3 - Track Configuration Changes**
  - Partially met for application/source changes by the `main` PR-required-with-passing-CI
    ruleset above (every production change is a CI-passed, PR-tracked commit). Still need:
    automated tracking of *infrastructure* changes (Railway/env/secret config outside the
    repo). Effort: 1-2 weeks
- [ ] **CM-3.4.5 - Restrict Software Installation**
  - Need: Whitelist approved software
  - Effort: Process documentation

---

### ⚠️ IDENTIFICATION & AUTHENTICATION (IA) - 11 Controls

**Current Implementation:**
- [x] Unique user identification (employee_id, email)
- [x] Password hashing (bcrypt)
- [x] JWT-based authentication
- [x] Token refresh mechanism
- [x] Failed login tracking
- [x] Account lockout
- [x] Device-class credentials for unattended wallboard TVs: scoped display tokens — revocable,
  expiring (≤365 days, default 90), audit-logged issuance/revocation, single read-only endpoint,
  no user identity (cannot authenticate as a user; see ACCESS CONTROL above)

**GAPS:**
- [ ] **IA-3.5.3 - Multi-Factor Authentication** 🔴 CRITICAL
  - Need: MFA for all users accessing CUI
  - Effort: 2-3 weeks
- [x] **IA-3.5.7 - Password Complexity** ✅ COMPLETE
  - Implemented: Minimum 12 chars, plus at least one uppercase, lowercase, number, and special
    char, and a common-weak-substring blocklist (`password`, `123456`, `qwerty`, `admin`,
    `letmein`, `welcome`). A violation is rejected with HTTP 422.
  - Single source of truth: `validate_password_strength` in `app/schemas/user.py`, enforced
    server-side on **every** user- and first-admin-creation and password-change path —
    `POST /auth/register` (admin create), `POST /auth/register-public` (public self-registration),
    `POST /users/` (admin create), `POST /users/{id}/reset-password` (admin reset),
    `POST /users/change-password` (self-service), and the two company-creation paths that mint the
    initial admin: the unauthenticated `POST /companies/register` (company self-registration) and
    platform-admin `POST /platform/companies` — and on **user-supplied** passwords in the user
    CSV import (`POST /users/import-csv`, rejected per row). Operator auto-generated passwords
    (badge/employee-ID logins) satisfy the policy by construction and are exempt. This closes the
    last enforcement gaps: `POST /companies/register` previously omitted the common-substring check
    and `POST /platform/companies` had no complexity validator at all, so a weak first-admin
    password (e.g. `Password1234!`) was accepted; the admin-driven and self-service user paths were
    closed earlier.
  - Residual (tracked separately, **not** part of this control): NIST 800-171 3.5.7's
    "change of characters when new passwords are created", plus password history (IA-3.5.8) and
    expiration (IA-3.5.9), remain open — see the GAPS below and the Priority Remediation Roadmap.
- [ ] **IA-3.5.8 - Password History** ⚠️ HIGH
  - Need: Prevent reuse of last 12 passwords
  - Effort: 3-5 days
- [ ] **IA-3.5.9 - Password Expiration** ⚠️ HIGH
  - Need: 90-day password expiration
  - Effort: 3-5 days
- [ ] **IA-3.5.10 - Temporary Passwords**
  - Need: Force change on first login
  - Effort: 2-3 days

---

### ⚠️ INCIDENT RESPONSE (IR) - 3 Controls

**Current Implementation:**
- [x] Error logging and tracking
- [x] Structured logging with correlation IDs

**GAPS:**
- [ ] **IR-3.6.1 - Incident Response Capability** ⚠️ HIGH
  - Need: Documented incident response procedures
  - Effort: Process documentation
- [ ] **IR-3.6.2 - Incident Tracking** ⚠️ HIGH
  - Need: Automated alerting on security events
  - Effort: 2-3 weeks
- [ ] **IR-3.6.3 - Incident Testing**
  - Need: Regular incident response drills
  - Effort: Process/scheduling

---

### ✅ MAINTENANCE (MA) - 6 Controls

**Current Implementation:**
- [x] Docker-based deployment (easy updates)
- [x] Database migration system (Alembic)
- [x] Deployment runbook documentation

**GAPS:**
- [ ] **MA-3.7.5 - Remote Maintenance**
  - Need: Document and control remote maintenance sessions
  - Effort: Process documentation

---

### ⚠️ MEDIA PROTECTION (MP) - 9 Controls

**Current Implementation:**
- [x] S3 configuration for file storage
- [x] Webhook payload encryption

**GAPS:**
- [ ] **MP-3.8.1 - Media Protection** ⚠️ HIGH
  - Need: Encrypted file uploads for CUI
  - Effort: 1-2 weeks
- [ ] **MP-3.8.3 - Media Sanitization**
  - Need: Procedures for sanitizing media before disposal
  - Effort: Process documentation
- [ ] **MP-3.8.9 - Media Marking**
  - Need: CUI marking on exported files
  - Effort: 1 week

---

### ✅ PHYSICAL PROTECTION (PE) - 6 Controls

**Status**: Using Railway cloud hosting - physical security inherited from provider.

**Documentation Needed:**
- [ ] Document reliance on Railway's SOC 2 compliance
- [ ] Obtain Railway security documentation

---

### ⚠️ PLANNING (PL) - 2 Controls

**GAPS:**
- [ ] **PL-3.12.1 - System Security Plan (SSP)** 🔴 CRITICAL
  - Need: Comprehensive SSP document
  - Effort: 2-4 weeks
- [ ] **PL-3.12.2 - Plan of Action & Milestones (POA&M)**
  - Need: This document serves as starting point
  - Effort: Ongoing

---

### ✅ PERSONNEL SECURITY (PS) - 2 Controls

**Current Implementation:**
- [x] User account management
- [x] Role-based access

**GAPS:**
- [ ] **PS-3.9.2 - Personnel Termination**
  - Need: Documented termination procedures (disable accounts, revoke access)
  - Effort: Process documentation

---

### ⚠️ RISK ASSESSMENT (RA) - 3 Controls

**GAPS:**
- [ ] **RA-3.11.1 - Risk Assessment** ⚠️ HIGH
  - Need: Periodic vulnerability scanning
  - Effort: Tooling + process
- [ ] **RA-3.11.2 - Vulnerability Scanning**
  - Need: Automated security scanning
  - Effort: 1-2 weeks
- [ ] **RA-3.11.3 - Vulnerability Remediation**
  - Need: Track and remediate vulnerabilities
  - Effort: Ongoing process

---

### ⚠️ SECURITY ASSESSMENT (CA) - 4 Controls

**GAPS:**
- [ ] **CA-3.12.1 - Security Control Assessment**
  - Need: Periodic self-assessment
  - Effort: Process
- [ ] **CA-3.12.3 - Continuous Monitoring**
  - Need: Security monitoring dashboards
  - Effort: 2-3 weeks

---

### ⚠️ SYSTEM & COMMUNICATIONS PROTECTION (SC) - 16 Controls

**Current Implementation:**
- [x] HTTPS/TLS encryption in transit (Railway/nginx)
- [x] CORS controls
- [x] Input validation
- [x] API rate limiting (global default per client IP, plus **enforced** stricter per-path limits on
  sensitive auth endpoints — login `5/min`, register/register-public `3/min`, employee-login `10/min`,
  refresh `30/min`, visitor `station-login` `5/min`, scanner `resolve-action` `60/min`; over-limit →
  **429 + `Retry-After`**, fail-open if the limiter backend errors). Employee-login's raise from
  `3/min` to `10/min` (kiosk shift-change badge cycling, 2026-07-23) is paired with a
  **compensating control**: a per-IP FAILED-attempt throttle
  (`backend/app/core/login_throttle.py`) — 8 failed attempts from one IP within 15 minutes →
  **429** with a 15-minute cooldown, checked **before** any user lookup so a throttled IP does
  zero account probing; successful logins never count toward the window, every throttled rejection
  writes an `EMPLOYEE_LOGIN_BLOCKED` audit event, and a counter-storage outage fails open (logged
  with the SIEM-greppable marker `employee_login_throttle_fail_open`) with the slowapi `10/min`
  cap still in force.
- [x] Outbound webhook dispatch is **tenant-scoped and CUI-minimized** (SC-3.13.1 boundary /
  CUI-egress control). The work-order completion webhook (`work_order.completed` /
  `work_order.closed`) is dispatched only to the **owning company's** registered endpoints
  (`WebhookService.dispatch_event` requires a `company_id` and refuses an unscoped/cross-tenant
  dispatch; `WebhookDelivery` rows are tenant-stamped), and the egressing payload is a **minimal,
  redacted** identifier set — `work_order_id`, `work_order_number`, `part_id`, `status`,
  `quantity_complete`, `quantity_scrapped`, `company_id`, `completed_at`. It **deliberately omits**
  `customer_name` and free-text/notes (CUI minimization at the system boundary); subscribers re-fetch
  any detail via the authenticated API. A richer outbound payload is an explicit
  data-classification decision, not the default. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`
  (Batch 5 / rank 8).
- [x] Carrier-shipping outbound egress is a **per-company, default-off kill switch** (SC-3.13.1
  boundary / CUI-egress control). `allow_carrier_egress` on `CompanyShippingProfile`
  (`company_shipping_profiles`) is `nullable=False, default=False, server_default="false"` — it
  requires an **explicit human opt-in** before any customer ship-to/ship-from address or
  package/parcel data leaves the boundary for EasyPost. `ShippingService._require_egress`
  (`app/services/shipping_service.py`) gates every outbound carrier call — address validation,
  rate-shop, buy-label, freight BOL, pickup scheduling, void/refund — and the tracking-poll job
  (`app/jobs/shipping_jobs.py`) re-checks the flag before any provider call; with it OFF, **no
  customer-data-bearing carrier call is made** — a credential-only connection test (an EasyPost
  `GET /users` carrying no customer data) is the sole deliberate exemption. The egress state is
  captured on the tamper-evident `audit_log` at profile creation (via `log_create`); every later
  toggle is recorded as an `egress_enabled` / `egress_disabled` status change. See
  `docs/SHIPPING_CARRIER_INTEGRATION.md`.
- [x] Thermal-label print egress is a **per-company, default-off kill switch** (SC-3.13.1 boundary /
  CUI-egress control). `allow_print_egress` on `CompanyPrintProfile` (`company_print_profiles`) is
  `nullable=False, default=False, server_default="false"` — it requires an **explicit human opt-in**
  before a rendered label is transmitted to the pbxz.io ProxyBox cloud relay. The payload that
  crosses the boundary is the receiving-label field set — part number + revision, description,
  quantity/UoM, lot/heat/serial, PO number, vendor, receipt number, received date, destination
  location, and the critical-characteristic marker (full inventory in
  `docs/THERMAL_LABEL_PRINTING.md`). Egress is **necessary-but-not-sufficient**:
  `PrintService._require_egress` (`app/services/print_service.py`) raises `PrintEgressDisabledError`
  before any `ProxyBoxClient` call unless the profile is active, fully configured (base URL, target,
  API key), **and** the flag is on; the auto-print ARQ job (`app/jobs/label_jobs.py`) gates on a
  **second, independent** toggle (`auto_print_on_receipt`) on top of egress and returns early — no
  outbound call — when either is off. With egress OFF, **no print call is made**. The egress state is
  captured on the tamper-evident `audit_log` at profile creation (via `log_create`); every later
  toggle is recorded as an `egress_enabled` / `egress_disabled` status change. See
  `docs/THERMAL_LABEL_PRINTING.md`.
- [x] AI document-extraction outbound egress is a **per-company kill switch** (SC-3.13.1 boundary /
  CUI-egress control) — the AI analogue of the carrier and print switches above, one of the **four**
  egress kill switches (`allow_carrier_egress` / `allow_print_egress` / `allow_ai_egress` /
  `allow_sms_egress`; the SMS switch is the newest — see the bullet below it).
  `allow_ai_egress` on `Company` (`companies`) is `nullable=False, default=False,
  server_default="false"`; it gates **all** outbound AI document-extraction egress to the Anthropic
  API. Enforcement is a **single fail-closed point** in the shared LLM client
  (`app/services/llm_client.py` → `_ai_egress_allowed` → `run_llm_task` raises
  `LLMEgressDisabledError` before any Anthropic call), so it covers **every** AI feature on one seam
  (PO/quote, BOM, QMS-clause, routing-generation, laser-nest PDF extraction, Werco Copilot, NL
  search); when OFF, **no request leaves the boundary and no `ai_usage_events` row is written**, and
  callers degrade gracefully (e.g. laser-nest extraction → filename-only). The flag flips only via
  `PUT /api/v1/companies/me/ai-egress` (**ADMIN-only**, for symmetry with the carrier/print egress
  controls — a CUI-boundary decision reserved to Admins) and the flip is recorded on the tamper-evident
  `audit_log` as both a `log_update` and an `ai_egress_enabled` / `ai_egress_disabled` status change.
  **Default posture differs from carrier/print:** new tenants default **OFF** at the column level,
  but pre-existing tenants were grandfathered **ON** by a data backfill in migration
  `054_company_allow_ai_egress` (not an audited user action — see the data-flow note below for the
  auditor sign-off item on the grandfathered-ON default). See the **Data-flow note (AI extraction
  egress)** below and `docs/AI_QUOTING_AGENT_RUNBOOK.md`.
- [x] SMS notification outbound egress is a **per-company kill switch** (SC-3.13.1 boundary /
  CUI-egress control) — the **fourth** egress boundary, added with the notification SMS channel.
  `allow_sms_egress` on `Company` (`companies`) is `nullable=False, default=False,
  server_default="false"`; it gates **all** outbound SMS to Twilio. Enforcement is a **single
  fail-closed point** in `app/services/sms_service.py` (`_sms_egress_allowed` → the send raises
  before any Twilio call), and it is **stricter than the AI analogue**: an unresolvable tenant
  returns `False` (deny), because every SMS caller has a tenant and a missing one is a bug that must
  not egress. The flag flips only via `PUT /api/v1/companies/me/sms-egress` (**ADMIN-only**, for
  symmetry with the carrier/print/AI controls) and the flip is recorded on the tamper-evident
  `audit_log` as both a `log_update` and a status change. **Default posture is OFF for ALL tenants,
  new and existing — there is no grandfathering backfill** (deliberately unlike `054`'s AI
  backfill): SMS is a brand-new capability, so default-OFF is the correct final state everywhere.
  A second, independent control sits on top: SMS is **per-user opt-in** and inert without a stored
  phone number, and only catalog events flagged `sms_eligible` may ever send. See the **Data-flow
  note (SMS notification egress)** below and `docs/NOTIFICATIONS.md`.

**Data-flow note (SMS notification egress — SC-3.13.1 boundary):**
- **What crosses the boundary to Twilio:** (1) the recipient's **mobile phone number** (an employee
  PII element, stored E.164), and (2) a deliberately **terse, generic message body**. Nothing else.
- The body is machine-composed from a fixed set of vetted inputs — **never from caller-supplied free
  text**. **Changed 2026-07-29 (flagged for auditor review):** this bullet previously read "exactly
  two inputs"; there are now **three**. The third is a single optional **closed-vocabulary
  classifier** (an enum value such as `machine_down`), added when the notification content rules were
  revised after CMMC L2 was deprioritized on 2026-07-28 — see the boundary decision of record in
  `docs/NOTIFICATIONS.md` §11.1. Format:
  `Werco: {identifier} - {label} ({classifier}). Log in to view.` (e.g. `Werco: WO-1042 - Work order
  blocked / on hold (machine down). Log in to view.`); the classifier is omitted when absent or
  unsafe, and is the first element dropped when the 160-char budget is tight.
  - The **refusal of caller-composed `title`/`body` is unchanged** — `build_sms_body` still does not
    accept them, so free text written by crons and direct dispatchers cannot reach the carrier.
  - The identifier passes an allowlist accepting only record-number shapes; anything free-text-like
    is dropped and the body degrades to the bare label.
  - The classifier clears **two independent fences**: a fixed payload-**field** allowlist
    (`_SMS_DETAIL_KEYS` — `category`, `planned_type`, `source`, all enum-valued; operator-typed
    fields such as `title`, `note`, `reason`, `scrap_reason`, `defect_type` and `step_label` are
    deliberately excluded), and a **value** guard (`safe_detail`) requiring a single whitespace-free
    token of letters/`_`/`-` only, ≤ 24 chars and ≤ 3 words.
  - **The exclusion claim still holds:** **no customer names, no part descriptions, no quantities, no
    drawing/spec text** can reach the carrier. Digits are refused outright (excluding quantities and
    part numbers) and any value containing whitespace is refused (excluding names and prose); the
    field allowlist is what keeps a single-token human value out of an eligible field in the first
    place. Widening `_SMS_DETAIL_KEYS` would put this claim at risk and is a CUI-boundary decision,
    not a routine change.
  - The builder remains a single function (`app/services/sms_content.py`) so the rule has one
    auditable enforcement point.
- Enforcement is fail-closed and layered: company egress flag → per-user opt-in → stored phone →
  `sms_eligible` event. With egress OFF, **no request leaves the boundary**.
- Delivery provenance is retained for audit: each attempt writes a tenant-scoped `notification_logs`
  row carrying the Twilio message SID and provider status, tying a Werco record to the carrier's own.
- **Auditor sign-off item:** enabling `allow_sms_egress` adds a commercial telecom carrier to the
  assessed boundary. Werco's posture is that the transmitted content is deliberately non-CUI (an
  identifier plus an event label), but the *decision* that this content class may traverse SMS —
  and the treatment of employee mobile numbers — should be confirmed in the next CUI review.
  Werco's own admin UI requires an explicit confirmation naming this trade-off before the flag flips.

**Data-flow note (AI extraction egress — SC-3.13.1 boundary):**
- During AI document extraction, the **extracted text** of an uploaded document egresses to the
  Anthropic API. This applies to PO/quote, BOM, QMS-clause, and routing-generation extraction. As
  of 2026-06-23 it also applied to **laser-nest report PDFs** (prompt `laser_nest_extraction`,
  `feature="laser_nest_extraction"`; see `docs/AI_QUOTING_AGENT_RUNBOOK.md`) — both the single-PDF
  `POST /laser-nests/extract` and the PDF laser-nest-package preview/import.
- **Updated 2026-06-24 (laser-nest path):** the laser-nest path now sends the **full PDF (the
  rendered page image content), not just extracted text**, to Anthropic — the bytes ride in a
  base64 `document` content block (layout-aware vision). This is **strictly more data crossing the
  same boundary** (the whole rendered sheet rather than only its flattened text), to the same
  provider under the same ToS. The flattened-text path remains only as a fallback for PDFs that
  can't be read natively or exceed the ~20 MB native cap (`_MAX_NATIVE_PDF_BYTES`) — note this means
  the **common (<20 MB) case egresses the richer image content** and only oversized files fall back
  to text; the cap is a provider-size limit, **not** a data-minimization control. Laser-nest sheets
  describe defense parts, so this content is CUI-relevant.
- **Updated 2026-07-20 (laser-nest path — two-pass verification + multi-page segmentation):** the
  laser-nest pipeline now makes **up to three** Anthropic calls where it made one. A bare
  multi-page PDF upload first egresses the **entire multi-page PDF** once for AI page segmentation
  (prompt `laser_nest_segmentation` 1.0.0, `feature="laser_nest_segmentation"`; skipped for
  single-page PDFs), and every extracted nest sheet then egresses **twice** — the extraction pass
  plus an independent verification pass over the same content (prompt `laser_nest_verification`
  1.0.0, `feature="laser_nest_verification"`). **No new data classes cross the boundary** — the
  same nest-report content crosses more times per document. All three calls run through the same
  `run_llm_task` seam, so the `allow_ai_egress` kill switch covers them fail-closed (egress OFF:
  segmentation degrades to one-nest-per-page and extraction to filename-only; page splitting is
  local `pypdf`, and the confirm-and-commit import re-splits by confirmed pages with **zero** AI
  calls).
- **AI egress is now a per-company kill switch (`allow_ai_egress`, default OFF) — ⚠️ posture
  change, auditor sign-off needed.** The prior open item above ("no `allow_ai_egress` kill switch")
  has been **closed in code**: `Company.allow_ai_egress` (`companies.allow_ai_egress`, `Boolean
  nullable=False, default=False, server_default="false"`) now gates **all** outbound AI
  document-extraction egress to the Anthropic API, mirroring `allow_carrier_egress` /
  `allow_print_egress`. Enforcement is a **single fail-closed point** in the shared LLM client
  (`app/services/llm_client.py` → `_ai_egress_allowed` → `run_llm_task` raises
  `LLMEgressDisabledError` before any Anthropic call), so it covers **every** AI feature on one
  seam: PO/quote, BOM, QMS-clause, routing-generation, laser-nest PDF extraction, Werco Copilot,
  and natural-language search. When the flag is OFF, **no request leaves the boundary and no
  `ai_usage_events` telemetry row is written**; callers degrade gracefully (e.g. laser-nest
  extraction falls back to filename-only). The check fails **closed**: unknown tenant or any DB
  error → deny. The flag flips **only** via `PUT /api/v1/companies/me/ai-egress`
  (`app/api/endpoints/companies.py`), gated to **ADMIN-only** (for symmetry with the carrier/print
  egress controls — a CUI-boundary decision reserved to Admins), and the flip is recorded on the
  tamper-evident `audit_log` as **both** a `log_update` and an `ai_egress_enabled` /
  `ai_egress_disabled` `log_status_change`. New companies are created **OFF** (the column's
  `server_default "false"` governs future INSERTs); pre-existing companies were grandfathered **ON**
  by a **data backfill in migration `054_company_allow_ai_egress`** (`UPDATE companies SET
  allow_ai_egress = true`), preserving the prior AI-always-on behavior for tenants that already
  relied on it.
  - **Auditor note (default-vs-grandfather):** because pre-existing tenants were grandfathered ON,
    the control being *present and default-OFF* does **not** mean egress is currently OFF for
    established companies — the live per-tenant state is the source of truth. Their initial AI-ON
    posture was set by the migration backfill, **not** by an audited user action, so there is **no
    `audit_log` row** for that initial flip (migration `054` deliberately backfills no audit rows;
    only later operator toggles via `PUT /companies/me/ai-egress` land on the tamper-evident trail).
    Whether the grandfathered-ON default is acceptable for CUI documents is a compliance decision
    flagged here for sign-off; the SC-3.13.1 boundary statements above (full rendered PDF crossing
    the boundary for laser-nest sheets, AI-always when the switch is ON) are unchanged when egress
    is enabled.
- When egress is ON, extraction is otherwise unconditional per call: each call is tenant-scoped and
  recorded in `ai_usage_events` (telemetry, not the tamper-evident `audit_log`).

**GAPS:**
- [ ] **SC-3.13.8 - Data at Rest Encryption** 🔴 CRITICAL
  - Need: Encrypt CUI fields in database
  - Effort: 2-4 weeks
- [ ] **SC-3.13.11 - CUI Encryption**
  - Need: FIPS 140-2 validated encryption
  - Effort: Validation + implementation
- [ ] **SC-3.13.16 - Data at Rest Protection**
  - Need: Database-level or field-level encryption
  - Effort: 2-4 weeks

---

### ✅ SYSTEM & INFORMATION INTEGRITY (SI) - 7 Controls

**Current Implementation:**
- [x] Input validation (Pydantic schemas)
- [x] Error boundaries (React)
- [x] Database constraints
- [x] KPI reporting integrity (AS9100D 9.1.1 monitoring/measurement honesty, Batch 8 / rank 11): the
  analytics dashboard no longer reports a fabricated metric when there is no underlying data. On
  `GET /analytics/kpis`, **OEE** and **on-time delivery** return **`null` ("n/a")** when the metric is
  genuinely uncomputable — OEE when the work center/plant has no staffed (clocked) time in the window
  (no availability denominator), OTD when no work order with a due date completed in the window (empty
  denominator). Previously **OTD with no completed work orders reported a misleading 100% on-time** — a
  measurement that read "perfect" precisely when there was nothing to measure. `KPIValue.value` is now
  nullable to carry the honest n/a; the frontend renders "n/a". The OTD rule also no longer flatters
  the figure: a COMPLETE work order with a null `actual_end` (no verifiable completion date) counts as
  **not on time**, and the completed-set is soft-delete-filtered. The OEE convention
  (`Availability × Performance × Quality` on the staffed-time basis) is now identical on the KPI
  headline and the persisted `OEERecord`, derived from real clocked time, routing standard cycle, and
  reported downtime/scrap rather than hardcoded assumptions, so the reported number reflects the
  production records. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` → Rank 11.

**GAPS:**
- [ ] **SI-3.14.1 - Flaw Remediation**
  - Need: Patch management process
  - Effort: Process documentation
- [ ] **SI-3.14.6 - Security Alerting**
  - Need: Automated security event alerts
  - Effort: 1-2 weeks
- [ ] **SI-3.14.7 - Software/Firmware Integrity**
  - Need: Verify integrity of updates
  - Effort: 1 week

---

## Priority Remediation Roadmap

### Phase 1: Critical (Weeks 1-4)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Multi-Factor Authentication (TOTP) | 2-3 weeks | | ⬜ Not Started |
| Password Policy Enforcement | 1 week | | 🟡 Partial — complexity enforced server-side on all password-set paths (IA-3.5.7 ✅); history/expiration/min-age pending (IA-3.5.8/3.5.9) |
| Encryption at Rest | 2-4 weeks | | ⬜ Not Started |
| System Security Plan (SSP) | 2-4 weeks | | ⬜ Not Started |

### Phase 2: High Priority (Weeks 5-8)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Session Inactivity Timeout | 3-5 days | | ⬜ Not Started |
| Audit Log Protection (AU-3.3.8) | 1-2 weeks | | ✅ Complete |
| Incident Response Procedures | 1-2 weeks | | ⬜ Not Started |
| Automated Security Alerting | 2-3 weeks | | ⬜ Not Started |
| Vulnerability Scanning Setup | 1-2 weeks | | ⬜ Not Started |

### Phase 3: Medium Priority (Weeks 9-12)
| Item | Effort | Owner | Status |
|------|--------|-------|--------|
| Media Protection (Encrypted Uploads) | 1-2 weeks | | ⬜ Not Started |
| Security Training Tracking | 1 week | | ⬜ Not Started |
| Continuous Monitoring Dashboard | 2-3 weeks | | ⬜ Not Started |
| Configuration Change Tracking | 1-2 weeks | | ⬜ Not Started |

### Phase 4: Documentation & Process (Ongoing)
| Item | Owner | Status |
|------|-------|--------|
| System Security Plan (SSP) | | ⬜ Not Started |
| Incident Response Plan | | ⬜ Not Started |
| Personnel Termination Procedures | | ⬜ Not Started |
| Media Sanitization Procedures | | ⬜ Not Started |
| Risk Assessment Process | | ⬜ Not Started |
| Railway SOC 2 Documentation | | ⬜ Not Started |

---

## Technical Implementation Notes

### MFA Implementation (TOTP)
```
Backend:
- Add pyotp library
- Add mfa_secret, mfa_enabled fields to User model
- Create /auth/mfa/setup and /auth/mfa/verify endpoints
- Modify login flow to require MFA if enabled

Frontend:
- QR code display for setup
- 6-digit code input during login
- MFA management in user settings
```

### Password Policy Implementation

**Status:** the complexity portion is **implemented** — `validate_password_strength`
(`app/schemas/user.py`, not `core/security.py`) enforces length + character classes + a
common-weak-substring blocklist on every user-creation and password-change path (see IA-3.5.7
above). Password history, expiration, and minimum age remain outstanding (the plan below).

```
Backend (app/schemas/user.py — validate_password_strength, DONE):
- Minimum length: 12 characters
- Require: uppercase, lowercase, number, special char
Remaining:
- Password history: store last 12 hashes
- Expiration: 90 days
- Minimum age: 1 day

User model additions:
- password_history (JSON array of hashes)
- password_expires_at (DateTime)
- must_change_password (Boolean)
```

### Data at Rest Encryption
```
Options:
1. PostgreSQL TDE (Transparent Data Encryption)
   - Requires PostgreSQL Enterprise or AWS RDS
   
2. Application-level encryption
   - Encrypt CUI fields before storage
   - Use Fernet (symmetric) or RSA (asymmetric)
   - Store encryption keys in secrets manager
   
3. Column-level encryption
   - SQLAlchemy-utils encrypted types
   - Encrypt specific CUI columns
```

### Session Inactivity Timeout
```
Frontend:
- Track last activity timestamp
- Show warning modal at 25 minutes
- Auto-logout at 30 minutes

Backend:
- Add last_activity_at to session/token
- Validate inactivity on each request
- Return 401 if inactive too long
```

---

## Assessment Preparation Checklist

### Pre-Assessment (3 months before)
- [ ] Complete all Phase 1 & 2 remediation
- [ ] Document all controls in SSP
- [ ] Complete POA&M for any remaining gaps
- [ ] Train staff on security procedures
- [ ] Conduct internal assessment

### Assessment Readiness (1 month before)
- [ ] Review SSP for accuracy
- [ ] Verify all controls are operational
- [ ] Prepare evidence documentation
- [ ] Brief all staff on assessment process
- [ ] Schedule C3PAO assessment

### During Assessment
- [ ] Designate assessment coordinator
- [ ] Provide assessor workspace
- [ ] Have technical staff available
- [ ] Document any findings immediately

---

## Resources

### Official Documentation
- [CMMC Model Overview](https://dodcio.defense.gov/cmmc/)
- [NIST SP 800-171 Rev 2](https://csrc.nist.gov/publications/detail/sp/800-171/rev-2/final)
- [CMMC Level 2 Assessment Guide](https://dodcio.defense.gov/Portals/0/Documents/CMMC/AssessmentGuideL2v2.pdf)

### Tools & Services
- C3PAO Directory: [Cyber AB Marketplace](https://cyberab.org/Catalog)
- Self-Assessment: NIST 800-171 DoD Assessment Methodology

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-13 | Initial compliance roadmap created | System |
| 2026-01-13 | AU-3.3.8 Audit Log Protection implemented | Droid |
| 2026-06-05 | AU-3.3.8: audit rows tenant-tagged (`company_id`) for scoped retrieval; `company_id` documented as deliberately excluded from the integrity hash; integrity endpoints restricted to Platform Admin (per-record check stays Admin, own-company) | Droid |
| 2026-06-05 | AU-3.3.8: settings-audit trail (`SettingsAuditLog`, `log_change`) now tags rows with the active company to match `AuditService._resolve_company_id`; defense-in-depth parity fix (cross-company switches are read-only, so no live cross-tenant write) | Droid |
| 2026-06-05 | AU-3.3.8: audit-log retention reconciled with immutability — `cleanup_old_logs_task` no longer deletes audit logs; aged rows are archived to cold storage (never deleted) by `archive_aged_audit_logs_task` / `AuditArchivalService`; physical removal is a documented DBA partition-drop only. See `docs/AUDIT_LOG_RETENTION_RUNBOOK.md` | Droid |
| 2026-06-07 | AC-3.1.3 / AU-3.3.1 (work-order completion hardening, Batch 1): tenant isolation enforced on the operation/clock/completion endpoints (404-before-mutation on a foreign id) and on traceability/analytics/OEE/scheduling/MRP services; `/ws/updates` now requires auth with completion broadcasts scoped per company. Tamper-evident audit coverage extended to operation/WO start+complete, shipment-close (WO `CLOSED`), inventory `/receive,/issue,/transfer,/adjust`, and blocker create/update/resolve. Reconcile-on-read audit (AUD-3) deferred to Batch 3. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | Data-integrity hardening (work-order completion, Batch 2): completion/clock endpoints now take row locks (`SELECT … FOR UPDATE`) and enforce optimistic locking (`version_id_col` on `WorkOrderOperation`/`TimeEntry`) — concurrent stale write → HTTP 409 instead of a lost update; new partial unique index `uq_open_time_entry` DB-enforces one open clock-in per user+operation (duplicate → HTTP 400). Migrations `038_optimistic_lock_backfill` / `039_uq_open_time_entry` (non-destructive open-duplicate dedupe; closed-row ids logged to deploy output for AS9100D labor traceability, not to `audit_log`). Residual follow-up A1: `audit_log.sequence_number` `max()+1` allocation is not serialized by the new row locks (concurrent audit writes can collide → occasional 500) — tracked for a dedicated fix. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | AU-3.3.1 (work-order completion, Batch 3 — AUD-3 closed): reconcile-on-read status transitions (operation/WO driven to COMPLETE from durable time-entry evidence on dashboard/list/detail reads) now write a tamper-evident `audit_log` status-change row attributed to the requesting user, tagged `extra_data.source = "reconcile_on_read"`; the reconcile returns its transitions for the read handler to audit before commit, and the write is best-effort (rolled back atomically with its audit rows on failure — reads never 500/orphan an unaudited transition). Completion logic consolidated into the shared `finalize_operation_completion`; ON_HOLD completion now refused with HTTP 409 on both op-complete endpoints and `complete_work_order`. Follow-up A1 (`audit_log.sequence_number` race) still open. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | AU-3.3.1 / AS9100D 8.7 (work-order completion, Batch 4 — quality gates, warn-and-record): completing an operation/WO past an unsatisfied quality gate (`inspection_incomplete` / `open_ncr` / `fai_not_passed` / `open_blocker`) is no longer silent — it succeeds (200) but writes a tamper-evident `audit_log` row with action `COMPLETED_WITH_QUALITY_EXCEPTION` (codes + offending-record references), emits a warning operational event, and returns the exceptions on the completion response (`quality_exceptions`, default `[]`). Gates are read-only + tenant-scoped (`app/services/quality_gate_service.py`); they do **not** block. New audited `inspection_complete` writer `POST /shop-floor/operations/{id}/inspection` (`MARK_OPERATION_INSPECTED`, role-gated ADMIN/MANAGER/SUPERVISOR/QUALITY). Deferrals: missing-but-required FAI undetectable (no FAI-required flag); FAI-pass→`inspection_complete` auto-wire needs an FAI↔operation FK; reconcile-on-read records only `inspection_incomplete`. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | SC-3.13.1 (work-order completion, Batch 5 — uniform completion signals): completion now fires outbound `work_order.completed` / `work_order.closed` webhooks that are **tenant-scoped** (`WebhookService.dispatch_event` requires `company_id` and refuses an unscoped/cross-tenant dispatch; deliveries reach only the owning company's registered endpoints; `WebhookDelivery` rows are tenant-stamped) and **CUI-minimized** — the egressing payload is a redacted identifier set (`work_order_id`, `work_order_number`, `part_id`, `status`, `quantity_complete`, `quantity_scrapped`, `company_id`, `completed_at`) that deliberately omits `customer_name`/free-text; subscribers re-fetch detail via the authenticated API. Dispatch is async (ARQ) + post-commit + best-effort (a signal failure never affects the completion). Internal `WO_COMPLETED` notifications are tenant-scoped to the company's own users. Reconcile-on-read emits in-process events only (no outbound dispatch from a read). Follow-up: reconcile outbound notify/webhook deferred to rank 12 (re-attribute to a system actor when moved to ARQ). See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | AU-3.3.1 / AS9100D 8.5.2 (work-order completion, Batch 6 — FG receipt + backflush + as-built genealogy): WO completion now moves inventory. A finished-goods `RECEIVE` is always written (warehouse `MAIN` / location `FINISHED-GOODS`, lot `LOT-<wo#>`, `unit_cost = standard_cost`); component backflush (`ISSUE` per component, `scrap_factor`-scaled) runs only when the part opts in (`parts.backflush_components`, default false). Every movement is tamper-evidently audited; a backflush shortage writes a `BACKFLUSH_SHORTAGE` audit row + warning event (the source lot is still driven negative — completion never blocks, **negative-stock posture flagged for explicit quality/compliance acceptance**). As-built lot genealogy is reconstructable via `consumed_components` on `GET /traceability/lot/{lot}`; `trace_serial` mirrors the WO/NCR collection. MRP `on_order` now counts only RELEASED/IN_PROGRESS WO output (completed output is on-hand). Idempotency is DB-enforced (migration `041`, two partial UNIQUE indexes on `inventory_transactions`; duplicate guard fails loudly, never deletes); migration `040` adds the opt-in flag. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-07 | AC-3.1.5 / AS9100D 9.1.1 (OEE/OTD metric correctness, Batch 8 — rank 11): **reporting integrity** — `GET /analytics/kpis` now returns `null` ("n/a") for OEE when there is no staffed (clocked) time and for OTD when no due-dated WO completed in the window, replacing a fabricated **100% on-time on an empty set** (`KPIValue.value` is now nullable; frontend renders "n/a"). A COMPLETE WO with a null `actual_end` counts as **not on time**; the OTD set is soft-delete-filtered. OEE = Availability × Performance × Quality on the staffed-time basis is now identical on the KPI headline and the persisted `OEERecord` (derived from real clocked time / routing cycle / reported downtime+scrap). **Authorization** — the OEE write endpoints (`POST /oee/calculate/{wc}`, `POST/PUT/DELETE /oee/records`, `POST/PUT/DELETE /oee/targets`) now require ADMIN/MANAGER/SUPERVISOR (`OEE_WRITE_ROLES`); previously open to any authenticated user. Reads stay open so the shop floor can view dashboards. The dead `POST /oee/calculate/{wc}` (referenced non-existent `TimeEntry.start_time/end_time`, 500'd) is fixed. Tracked follow-up: `OEERecord` writes are not yet tamper-evidently audited. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` → Rank 11 | Droid |
| 2026-06-09 | AC-3.1.3 / AC-3.1.5 / AU-3.3.1 (operator-certifications write hardening, branch `fix/operator-cert-write-rbac-audit`): the seven `operator_certifications.py` write endpoints — previously open to any authenticated user, unaudited, and accepting a cross-tenant FK on create — are now least-privilege role-gated (cert/training writes → ADMIN/MANAGER/QUALITY; skill-matrix writes → ADMIN/MANAGER/SUPERVISOR; other roles → 403), write a tamper-evident `audit_log` row per create/update/delete (`operator_certification` / `training_record` / `skill_matrix`), and reject a `user_id`/`work_center_id` outside the active company with 422 before insert. Role sets are new defaults (the RBAC matrix had no rows for these record types); reads unchanged (any authenticated user, tenant-scoped). No migration, no new env var; strengthens the existing posture, no compliance claim changed. See `docs/RBAC_PERMISSIONS.md` / `docs/API.md` / `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-06-09 | AU-3.3.1 / AU-3.3.8 / AC-3.1.3 (work-order completion round-2 follow-ups): closed five tracked items. **Audit integrity (AU-3.3.8):** the residual follow-up **A1** is resolved — `audit_log.sequence_number` allocation in `AuditService.log()` is now serialized (transaction-level Postgres advisory lock + savepoint/retry), so concurrent audited writes no longer collide on the unique sequence (occasional 500) or poison the caller's transaction; the tamper-evident hash-chain semantics are unchanged. **Audit coverage (AU-3.3.1):** OEE record/target create/update/delete + auto-calc now write tamper-evident `audit_log` rows (were RBAC-gated but unaudited). **Authorization:** `POST /shipping/{shipment_id}/ship` (`mark_shipped`, closes the WO) is now `require_role`-gated to ADMIN/MANAGER/SUPERVISOR/SHIPPING — previously any authenticated user (non-privileged → 403). **Tenant isolation (AC-3.1.3):** the remaining cross-tenant read/write leak in `operator_certifications.py` is closed (cert dashboard aggregates + by-id cert/training/skill reads/updates now company-scoped, 404 cross-tenant); the `SkillMatrix` unique constraint is now tenant-qualified (`company_id, user_id, work_center_id`; migration `045_skillmatrix_company_unique`). All strengthen the existing posture; no compliance claim changed. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Droid |
| 2026-07-15 | IA / AC-3.1.2 / AU-3.3.1 (TV pairing setup codes, branch `worktree-wallboard-redesign`): display tokens can now be claimed on the TV via an 8-char one-time setup code (~40-bit CSPRNG, unambiguous alphabet) instead of a `#token=` URL — SHA-256-hashed at rest (never stored/logged in plaintext), 15-minute TTL, single-use burn-on-claim, ADMIN/MANAGER-gated issuance/reissue (tenant-scoped, audited); one new deliberately-public endpoint `POST /auth/display-token/claim` (rate-limited 10/min/IP, uniform 404 no-oracle, company bound by the matched DB row, claim audited with `user=None`); the claimed JWT is re-minted from the same `display_tokens` row so the existing revocation/expiry authority is unchanged; TV persists the display-scoped JWT in localStorage (credential no longer rides in any URL; revocation still enforced per 30s poll). Additive mechanism — no existing compliance claim changed. See `docs/WALLBOARD.md` / `docs/API.md` / `docs/RBAC_PERMISSIONS.md` | Claude |
| 2026-06-10 | AC-3.1.2 / IA / AU-3.3.1 (TV wallboard, A0.5, branch `feat/tv-wallboard`): added scoped display tokens for unattended shop TVs — `type="display"` JWTs that authenticate **only** the new zero-write `GET /shop-floor/wallboard` (401 everywhere else via `verify_token`'s type check, so they can never act as a user session); issuance/revocation ADMIN/MANAGER-gated, tenant-scoped, and tamper-evidently audit-logged; the `display_tokens` DB row is the revocation/expiry/tenant authority re-checked per request; raw JWT shown once at issuance, never stored; operator names truncated to "First L." for public screens. Additive mechanism — no existing compliance claim changed. See `docs/WALLBOARD.md` / `docs/API.md` / `docs/RBAC_PERMISSIONS.md` | Droid |
| 2026-06-18 | SC-3.13.1 (carrier-shipping egress kill switch): catalogued the **per-company, default-off** outbound-egress control `allow_carrier_egress` on `CompanyShippingProfile` (`company_shipping_profiles`, `nullable=False, default=False, server_default="false"`) — requires explicit human opt-in before any customer address/parcel data leaves the boundary for EasyPost; `ShippingService._require_egress` gates every outbound carrier call (validate/rate/buy-label/freight-BOL/pickup/void) and the tracking-poll job re-checks it (no provider call when OFF); flag flips are tamper-evidently audit-logged as a status change. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/SHIPPING_CARRIER_INTEGRATION.md` | Claude |
| 2026-06-18 | SC-3.13.1 (thermal-label print egress kill switch): catalogued the **per-company, default-off** outbound-egress control `allow_print_egress` on `CompanyPrintProfile` (`company_print_profiles`, `nullable=False, default=False, server_default="false"`) — requires explicit human opt-in before a rendered label (part number, lot/heat/serial, critical-characteristic marker) is transmitted to the pbxz.io ProxyBox cloud relay; both the request path (`PrintService._require_egress`) and the auto-print ARQ job (`app/jobs/label_jobs.py`) gate on it (no outbound call when OFF); flag flips are tamper-evidently audit-logged as a status change. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/THERMAL_LABEL_PRINTING.md` | Claude |
| 2026-06-22 | CM-3 (deploy governance reframe): the manual production-deployment approval gate was **removed** — the `production` GitHub environment no longer carries a required-reviewer rule, and production **auto-deploys from `main`**. Change control is now enforced by a **`main` repository ruleset** (PR required before merge, required CI status checks must pass, force-push/branch-deletion blocked, **0 human approvals** — merge-when-green, with documented repo-admin break-glass bypass), plus deploy-time compensating controls: a deployment-branch policy permitting only `main` to deploy, and post-deploy health checks that fail the job on a bad deploy (`Verify Production Deployment` / `Verify deployment serves the Vite frontend bundle`). Rollback = redeploy a known-good commit or re-add the reviewer rule. CM-3.4.3 remains a partial gap (covers application/source changes via CI-passed PRs, not out-of-repo infrastructure changes); stated as *tested-before-merge*, not peer-reviewed. Documentation-only — describes the live config, control reframed accurately not overstated. See `docs/CI_CD_SETUP.md` / `docs/DEPLOYMENT_RUNBOOK.md` | Claude |
| 2026-06-22 | CM-3.4.1/3.4.2 / AC-3.1.5 / AU-3.3.1 (released-routing change control, "Proportionate (audit-trail)" posture, `feat/routing-editable-time-standards`): catalogued the editable-time-standards policy on `PUT /routing/{id}/operations/{operation_id}`. A released routing's **process** (sequence, work center, instructions, inspection points, op add/delete/reorder) is **frozen** — those changes require a new revision (400 otherwise); only **time standards** (`setup_hours`, `run_hours_per_unit`, `move_hours`, `queue_hours`, `cycle_time_seconds`, `pieces_per_cycle`) are editable in place. Released time-standard edits are least-privilege gated **in code** to **ADMIN/MANAGER** (Supervisor → 403, matching Release); draft edits stay ADMIN/MANAGER/SUPERVISOR. Every applied change is tamper-evidently audit-logged (`log_update` on op edit; `log_create`/`log_delete`/`log_status_change` elsewhere); a successful released edit re-stamps `approved_by`/`approved_at` but leaves `effective_date` and the revision letter unchanged. **Accepted residual:** no per-operation history table and no optimistic-lock/version column on routing operations — `audit_log` is the sole history of record; concurrent released edits are last-write-wins. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/RBAC_PERMISSIONS.md` → Routings | Claude |
| 2026-06-23 | SC-3.13.1 (laser-nest PDF AI-extraction egress, data-flow note): catalogued that laser-nest report **PDF text now egresses to the Anthropic API** during AI extraction — both `POST /laser-nests/extract` (single PDF, stateless) and the PDF laser-nest-package preview/import (prompt `laser_nest_extraction` 1.0.0, `feature="laser_nest_extraction"`, one tenant-scoped `ai_usage_events` row per call). Same precedent and per-request trust boundary as the existing PO/BOM/QMS/routing extraction. Extraction is **AI-always with no `allow_ai_egress` kill switch** (unlike `allow_carrier_egress` / `allow_print_egress`); nest sheets describe defense parts, so the text is CUI-relevant. **Open item for auditor sign-off:** whether an AI-egress kill switch for CUI documents is warranted — flagged, not asserted as a control. Batch import writes one `log_create` per nest to the tamper-evident `audit_log`; the single-PDF extract is stateless (no audit). Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/API.md` → Laser Nests / `docs/AI_QUOTING_AGENT_RUNBOOK.md` | Claude |
| 2026-06-24 | SC-3.13.1 (laser-nest AI extraction — egress widened to native PDF): the laser-nest extractor (`app/services/laser_nest_extraction_service.py`) now sends the **raw PDF as a base64 `document` content block** (full rendered page image content — drawing views, title block, inspection/CUI stamps, handwritten annotations — not only the flattened text layer) for PDFs ≤ 20 MB (`_MAX_NATIVE_PDF_BYTES`), with a text-flatten fallback only above the cap; both `POST /laser-nests/extract` and the PDF laser-nest-package preview/import are affected, and native-PDF calls now route to the Sonnet/default tier (`has_pdf_document` flag), prompt `laser_nest_extraction` bumped 1.0.0 → 1.1.0. This is **strictly more CUI crossing the same boundary** than the prior text-only flow; the size cap is a provider limit, **not** a data-minimization control (the common <20 MB case egresses the richer image content). Still **AI-always with no `allow_ai_egress` kill switch** — the widening **raises the priority** of that open item (flagged for auditor sign-off, not asserted as a control). Per-call `ai_usage_events` telemetry only; `/extract` persists nothing. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/API.md` → Laser Nests / `docs/AI_QUOTING_AGENT_RUNBOOK.md` | Claude |
| 2026-06-23 | AU-3.3.1 (laser-nest (re-)import audit symmetry, hardening pass): `POST /work-orders/{id}/laser-nest-packages/import` now audits the IMPORT-REPLACES-EVERYTHING wipe symmetrically — each superseded nest writes a tamper-evident `log_delete` (`reason="superseded_by_reimport"`) **before** the rebuild, and the **legacy CNC-program path** now also writes one `log_create` per nest (`source="cnc_file_import"`), matching the PDF path (`source="pdf_import"`); previously the destructive wipe and the legacy create path left no `audit_log` trail (legacy emitted only a websocket event). Same pass also hardened input validation (new `LaserNestImportRow` schema validates the `rows` JSON before persistence; invalid rows, duplicate `source_file`, and DB `IntegrityError`/`SQLAlchemyError` now return **400** instead of 500). **Accepted residual (tracked follow-up):** the supersession wipe is still a **hard cascade-delete of soft-deletable `LaserNest` rows**, so the soft-delete invariant is not yet fully satisfied for this path — the improvement is that the deletion is now *audited*, not that rows are preserved; re-modeling the wipe as a soft-delete is a separate follow-up. Documentation-only — describes shipped behavior; closes a prior audit-completeness asymmetry, no compliance claim weakened. See `docs/API.md` → Laser Nests | Claude |
| 2026-06-24 | SC-3.13.1 (AI-extraction egress kill switch — ⚠️ **closes the prior open item, posture change**): the previously-flagged "no `allow_ai_egress` kill switch" open item is now **implemented in code**. `Company.allow_ai_egress` (`companies.allow_ai_egress`, `Boolean nullable=False, default=False, server_default="false"`) gates **all** outbound AI document-extraction egress to the Anthropic API (mirrors `allow_carrier_egress` / `allow_print_egress`). Enforcement is a **single fail-closed seam** in `app/services/llm_client.py` (`_ai_egress_allowed` → `run_llm_task` raises `LLMEgressDisabledError` before any Anthropic call), so it covers **every** AI feature: PO/quote, BOM, QMS-clause, routing-generation, laser-nest PDF extraction, Werco Copilot, and NL search. When OFF: **no request leaves the boundary, no `ai_usage_events` row**; callers degrade gracefully (laser-nest extraction → filename-only). Unknown tenant / DB error → **deny**. The flag flips **only** via `PUT /api/v1/companies/me/ai-egress` (**ADMIN-only**, for symmetry with the carrier/print egress controls — a CUI-boundary decision reserved to Admins; tightened from the initial ADMIN/MANAGER on 2026-06-25, see row below), recorded on the tamper-evident `audit_log` as both a `log_update` and an `ai_egress_enabled`/`ai_egress_disabled` status change; the same toggle is now exposed in the UI at **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`, control enabled for ADMIN, read-only otherwise). **Initial state:** new companies are created **OFF** (the column `server_default 'false'` governs future INSERTs); pre-existing companies were **grandfathered ON by a data backfill in migration `054_company_allow_ai_egress`** (`UPDATE companies SET allow_ai_egress = true`), **not** by an audited user toggle — so established tenants' initial AI-ON posture has **no `audit_log` row** (the migration deliberately writes none; only subsequent operator flips are audited). **Open item for auditor sign-off:** whether the grandfathered-ON default is acceptable for CUI documents (the control is present and default-OFF, but live per-tenant state — not the default — is the source of truth). Describes shipped working-tree behavior. See `docs/API.md` → Company (self-service) / `docs/AI_QUOTING_AGENT_RUNBOOK.md` / `docs/RBAC_PERMISSIONS.md` | Claude |
| 2026-06-25 | AC-3.1.1 / SC-3.13.1 (AI-egress toggle — authorization tightened to ADMIN-only): `PUT /api/v1/companies/me/ai-egress` (`app/api/endpoints/companies.py`) was narrowed from `require_role([ADMIN, MANAGER])` to `require_role([ADMIN])`, so flipping the `allow_ai_egress` CUI kill switch is now **ADMIN-only**. This brings it into symmetry with the two sibling CUI egress kill switches (`allow_carrier_egress` / `allow_print_egress`), which are already ADMIN-only — opening or closing the CUI boundary is a decision reserved to Admins. Managers can no longer flip the flag via any path (the prior allowance had been UI-dormant — `/admin/settings` is AdminRoute-gated — so this removes the latent direct-API path). Authorization-scope change only: the fail-closed enforcement seam, audit behavior, default-OFF/grandfathered-ON posture, and the auditor sign-off open item on the grandfathered-ON default are all unchanged. Docs reconciled in `docs/RBAC_PERMISSIONS.md` / `docs/API.md` / `docs/AI_QUOTING_AGENT_RUNBOOK.md` and the prior row above. Describes shipped working-tree behavior. | Claude |
| 2026-06-29 | AU-3.3.8 (audit-log reviewability — UI now pages the full history, branch `ui/ux-batch4-datatable`): the Audit Log screen (`frontend/src/pages/AuditLog.tsx`) now uses **server-side offset/limit pagination** (Prev/Next, `desc(timestamp)`) instead of a single fixed-`limit` fetch, so **older audit rows are reachable in the UI** — closing a practical reviewability gap where records beyond the first page were not navigable. **No backend change:** `GET /audit/` already supported `offset`/`limit` (`le=500`); only the frontend `api.getAuditLogs` gained an optional `offset` param and the page was migrated onto the shared `<DataTable>` primitive. Tamper-evident immutability, the hash chain, tenant-scoped retrieval, and retention/archival behavior are all unchanged; this strengthens the **practical accessibility** of the protected audit record without altering any control claim. Documentation-only — describes shipped working-tree behavior, no compliance claim changed. See `docs/API.md` → Audit Log | Claude |
| 2026-07-01 | AC-3.1.8 / SC-3.13.1 (per-path auth rate limiting — now **enforced**, branch `fix/auth-rate-limit-enforcement`): the stricter per-path limits for sensitive endpoints (`AUTH_RATE_LIMITS` / `ENDPOINT_RATE_LIMITS` in `app/main.py`) were **declared but never wired into slowapi** — only the app-wide default limit applied, so brute-force protection on `/auth/login`, `/auth/register(-public)`, `/auth/employee-login`, `/auth/refresh`, `/visitor-logs/station-login`, and `/scanner/resolve-action` was **not actually in force**. A new per-path middleware now hits the limiter's own strategy+storage (shared Redis/memory backend) and **rejects over-limit requests with 429 + `Retry-After`** (body `{"detail": "Rate limit exceeded: <limit>"}`), keyed per client IP: login `5/min`, register/register-public/employee-login `3/min`, refresh `30/min`, visitor station-login `5/min`, scanner resolve-action `60/min`; all other paths keep the global default (100/60s). Enforcement **fails open** (limiter-backend error → request allowed, global default still applies, warning logged) so a dead backend cannot hard-block auth. This **closes a genuine brute-force-throttling gap** (limits were documented as active but inert); it does not weaken any claim. As a follow-on, the interim 6–8 digit visitor-PIN-length mitigation can relax now that station-login is throttled server-side. Describes shipped working-tree behavior. See `docs/ENVIRONMENT_VARIABLES.md` → Rate Limiting / `docs/API.md` → Rate Limiting / `docs/VISITOR_SIGNIN.md` → Security note | Claude |
| 2026-07-06 | AU-3.3.1 (routing-copy audit coverage, branch `feat/process-sheets-library`): `POST /api/v1/routing/{routing_id}/copy` (`copy_routing`, `app/api/endpoints/routing.py`) now writes a tamper-evident `audit_log` CREATE for the newly created draft routing via `AuditService.log_create` (entity `routing`, the new routing's id + target part number, full new values, `extra_data.copied_from` = the source routing id) before the terminal commit — previously the copy endpoint was RBAC-gated (Admin/Manager) but the routing it created was **unaudited**, an AU-3.3.1 coverage gap. The `copied_from` reference also preserves derivation traceability from the new draft back to its source baseline. No API contract, role, or other behavior change; this brings the copy path into audit parity with `POST /routing/` create and `POST /routing/import/commit` (one audit CREATE per created routing). Regression-pinned by `backend/tests/api/test_routing_audit_persistence.py`. Describes shipped working-tree behavior. See `docs/API.md` → Routing | Claude |
| 2026-07-07 | AC-3.1.3 / AU-3.3.8 (Supabase DB hardening, branch `feat/supabase-security-hardening` — ⚠️ **closes two live prod exposures, flagged for auditor awareness**): the Supabase Security Advisor flagged `rls_disabled_in_public` (ERROR) on all 127 `public` tables — the Data API roles `anon`/`authenticated` held FULL privileges (incl. INSERT/UPDATE/DELETE/TRUNCATE) with RLS off, so the ERP DB was readable/**writable** to anyone holding the project anon key via the auto-generated REST API, and a stray dashboard policy made `companies` anon-readable. **Migration `059_supabase_rls_hardening`** drops the stray policy, enables deny-by-default RLS (no policies, on purpose) on every `public` table, and revokes all `anon`/`authenticated` privileges incl. default privileges for future objects; app-layer tenancy remains the enforcement (no-op for the app — it connects as the table-owning `postgres` role with `BYPASSRLS`). **Separately discovered: the `008` AU-3.3.8 immutability triggers did not exist in prod** (bootstrap `create_all` + `stamp` skipped `008`'s raw DDL), so `audit_logs` had no DB-level UPDATE/DELETE protection until now; **migration `060_audit_log_immutability`** idempotently re-creates the trigger functions (with `SET search_path = ''` pinned) and triggers. New-table convention going forward: every table-creating migration must ENABLE ROW LEVEL SECURITY. Manual dashboard follow-ups (disable unused Data API, SSL enforcement, network restrictions) tracked with verification SQL in `docs/SUPABASE_SECURITY.md` | Claude |
| 2026-07-12 | AU-3.3.1 (vendor-create audit coverage, branch `fix/vendor-create-audit-logging`): `POST /api/v1/purchasing/vendors` (`create_vendor`, `app/api/endpoints/purchasing.py`) now writes a tamper-evident `audit_log` CREATE for the newly created vendor via `AuditService.log_create` (entity `vendor`, the flushed vendor id + code, full new values) before the terminal commit, so the audit row commits atomically with the insert — previously the direct-create endpoint was RBAC-gated (Admin/Manager) but the vendor it created was **unaudited**, an AU-3.3.1 / invariant-2 coverage gap flagged in the PR #104 reviews (that PR audited `update_vendor` only). No API contract, role, or status-code change; this brings the direct create into audit parity with vendor updates and the per-row audit of `POST /purchasing/vendors/import-csv` creates. Regression-pinned by `backend/tests/api/test_vendor_create_audit.py`. Describes shipped working-tree behavior. See `docs/API.md` → Purchasing | Claude |
| 2026-07-12 | AU-3.3.1 (purchase-order audit coverage, branch `fix/po-audit-logging`): the four interactive purchase-order write endpoints in `app/api/endpoints/purchasing.py` — `POST /api/v1/purchasing/purchase-orders` (create), `PUT /purchasing/purchase-orders/{po_id}` (update), `POST /purchasing/purchase-orders/{po_id}/send` (issue), and `POST /purchasing/purchase-orders/{po_id}/lines` (add line) — now write tamper-evident `audit_log` rows via `AuditService`: create → one `log_create` (entity `purchase_order`, full new values, vendor code + line count in `extra_data`; no per-line rows at document creation); update → `log_update` with a column-only before/after diff (a no-change PUT writes no row); send → `log_status_change` (`draft`/`approved` → `sent`, stamped `order_date` in `extra_data`); add-line → `log_create` (entity `purchase_order_line`) plus `log_update` on the PO recording the subtotal/total roll (`extra_data.cause = "po_line_added"`). Rows are flushed before each terminal commit so the audit record commits atomically with the state change. Previously all four were RBAC-gated but **unaudited** — the same AU-3.3.1 / invariant-2 gap class as the vendor rows above; this brings interactive PO writes into audit parity with the per-row-audited `POST /purchasing/purchase-orders/import` loader. No API contract or role change; one behavior fix rode along: `add_po_line` previously **500'd unconditionally** (`float += Decimal` TypeError — Money-schema Decimal line math vs the Float PO money columns, the same defect class PR #98 fixed in `create_purchase_order`), so its happy path goes 500 → 200 in this change; the other three endpoints have no status-code change. The endpoints also gained OpenAPI docstrings describing the audit behavior. Describes working-tree behavior on the branch. See `docs/API.md` → Purchasing | Claude |
| 2026-07-12 | AC-3.1.3 (PO-upload / extraction-matching tenant isolation, branch `fix/po-upload-tenant-scope` — ⚠️ **closes a live cross-tenant data exposure, flagged for auditor awareness**): the AI PO-upload flow read across every tenant. `GET /api/v1/po-upload/search-parts` / `/search-vendors` (any authenticated user) returned **all tenants'** active parts (`id`/`part_number`/`name`/`description`) and vendors (`id`/`code`/`name`); the extraction-review matchers in `app/services/matching_service.py` (`match_vendor` / `match_part` / `match_part_by_description` / `match_po_line_items`) fuzzy-matched against every tenant's active vendors/parts, so the `POST /po-upload/upload-po` / `/upload-quote` / `/upload-invoice` extraction responses could surface another tenant's vendor names/codes and part numbers/names in match + suggestion payloads; `check_po_number_exists` was a cross-tenant PO-number existence oracle (and produced false duplicate-PO 400s off other tenants' POs); and the fallback QTE quote-number sequence (`_generate_quote_po_number`) was allocated globally across tenants (now per-tenant). On the write side, `POST /po-upload/create-from-upload` (Admin/Manager/Supervisor) **accepted a cross-tenant `vendor_id`** — the PO could be created against another tenant's vendor record — and its part resolution could bind PO lines to a same-numbered **foreign part id** (cross-tenant FKs on tenant-stamped rows); the generated-vendor-code uniqueness loop was likewise global. Every lookup is now scoped through the standard helpers — `tenant_query` (`app.db.tenant_filter`) with the company from the `get_current_company_id` dependency (newly added to the two search endpoints); the five `matching_service` functions take a **required** `company_id` so no unscoped call path remains, and `_upload_and_extract_document`'s `company_id` went `Optional` → required. Contract-visible changes: a `vendor_id` outside the active company now returns **400** "Vendor not found", and client-supplied line `part_id`s are verified in-tenant before use (**400** on a foreign/unknown id); response shapes, roles, and the existing `PO_CREATE_FROM_UPLOAD` audit row are unchanged; no migration, no new env var. Regression-pinned by `backend/tests/api/test_po_upload_tenant_isolation.py` (two-company tests: search exclusion, foreign `vendor_id` 400, foreign `part_number` creating a fresh in-tenant part, per-tenant existence checks and matching); existing matching/endpoint tests re-threaded for the now-required `company_id`. Describes working-tree behavior on the branch. See `docs/API.md` → PO Upload (AI document extraction) | Claude |
| 2026-07-12 | Data-integrity hardening (PO-upload deleted-part policy, branch `fix/po-upload-deleted-parts` — closes a soft-delete-integrity advisory from the 2026-07-12 PO-upload compliance review): `POST /api/v1/po-upload/create-from-upload` could silently bind PO lines to a **soft-deleted** part — the part-number reuse lookup, the description→part-number matcher (`_find_existing_part_number_by_description`, which also feeds extraction-review suggestions), and the client-supplied line-`part_id` fence all ignored `is_deleted` — and a concurrent duplicate part create **500'd** on `uq_parts_company_part_number` (TOCTOU). All three now exclude deleted rows (a deleted `part_id` gets the same **400** "Part id N not found" as a nonexistent one — no deleted-state oracle); a new part number still held by a soft-deleted part is rejected with **400** "Part number '…' belongs to a deleted part - restore it or use a different part number" — the `POST /parts/` policy, keeping the audited Admin/Manager `POST /parts/{id}/restore` the only resurrection path (soft-delete invariant-3); active holders keep being reused; `IntegrityError` backstops at the part flush and the terminal commit turn residual race/collision 500s into **400** "Part number already exists", with the `PO_CREATE_FROM_UPLOAD` audit row riding the transaction and rolling back with it (no orphan audit row on a failed create). No RBAC, schema, audit-call, or migration change. Describes working-tree behavior on the branch. See `docs/API.md` → PO Upload | Claude |

| 2026-07-13 | IA-3.5.7 / AU-3.3.1 (authenticator-management gap closure, branch `fix/ia-password-gaps`): closed the last password-strength enforcement gaps left after PR #115. The **unauthenticated** company self-registration `POST /api/v1/companies/register` previously omitted the common-substring/common-password check (its `CompanyRegister.admin_password` accepted e.g. `Password1234!`), and platform-admin company creation `POST /api/v1/platform/companies` (`CompanyCreate.admin_password`) had **no** complexity validator at all — both first-admin passwords now run through the shared `validate_password_strength` (`app/schemas/user.py`), so no company can be seeded with a weak initial admin credential (IA-3.5.7). The self-service `POST /api/v1/users/change-password` now records a tamper-evident `PASSWORD_CHANGE` audit event (`extra_data.source = "self_service"`, password/hash never included), mirroring the admin `reset-password` path and closing an AU-3.3.1 coverage gap on self-service authenticator changes. Separately, the admin-gated bootstrap `POST /api/v1/admin/settings/seed-database` no longer ships the hardcoded `admin123` / `password123` defaults — it generates strong, per-user one-time credentials at runtime and returns them once in the response (no-op once any user exists), removing a default-credential exposure. No new env var or migration; a dead/weaker internal `PasswordChange` schema was also removed (no API-surface change). Describes working-tree behavior on the branch. See `docs/API.md` → Users / Admin Settings, `docs/RBAC_PERMISSIONS.md` → Users, and IA-3.5.7 above. | Claude |
| 2026-07-16 | AU-3.3.1 / AS9100D 8.6 + 7.5.3 (receiving records-integrity fix — ⚠️ **corrects a production quality-records defect; compliance-auditor endorsed in review**, branch `fix/receiving-not-required-inspection-status`): the receiving dock-to-stock ("no incoming inspection required") path stamped `po_receipts` with `inspection_status=PASSED`, `inspection_method=VISUAL`, `inspected_by=<receiver>`, `inspected_at=<now>` — asserting an incoming **visual inspection that never occurred**; because PR #127 made no-inspection the receiving default, this became the norm for most receipts after that deploy. **Root cause:** the auto-accept branch reused the `PASSED`/`VISUAL` inspection stamp as shorthand for "accepted into stock." **Correction:** new `InspectionStatus.NOT_REQUIRED` (migration `066_inspection_not_required`: `ALTER TYPE inspectionstatus ADD VALUE 'NOT_REQUIRED'`, Postgres-guarded, idempotent, no-op downgrade, **no data backfill**) records dock-to-stock acceptance honestly — `inspection_method`/`inspected_by`/`inspected_at` left NULL, `received_by`/`received_at` retain custody; inspection-required receipts are unchanged (queue → `passed`/`failed`/`partial` with a real inspector/method/time); vendor acceptance-rate analytics count `NOT_REQUIRED` as accepted (taken into stock without rejection). The receipt-create `audit_log` snapshot now records the honest NULL-inspector state (a net audit-integrity improvement); **no `audit_log` row is mutated or backfilled**. **Affected historical population + disposition:** auto-accepted receipts created between the PR #127 deploy and this fix's deploy, identifiable by `requires_inspection=False` with a receiver-stamped `VISUAL` pass (`inspected_by == received_by`, `inspected_at ≈ received_at`) — **corrected forward with an effective date (this deploy); historical rows deliberately NOT rewritten**, because mutating shipped quality records would itself breach records integrity (invariant #5), the rows are per-row ambiguous (a genuine eyeball-pass cannot be distinguished from a fabricated auto-accept), and the tamper-evident `audit_log` chain preserves the original record. Effective date: migration 066 / app deploy. **Sign-off:** compliance-auditor endorsed in review; **quality-owner countersignature pending**. Describes working-tree behavior on the branch. See `docs/API.md` → Receiving / `docs/onboarding/03-warehouse.md` | Claude |
| 2026-07-16 | AU-3.3.1 / AS9100D 8.5.1 traceability (operator over-count correction, branch `feat/wo-completion-reduce-quantity`): new `POST /shop-floor/operations/{id}/reduce-production` lets an operator walk back good-count quantity they over-reported on an operation they are actively working — the tamper-evidently-audited inverse of `report_operation_production`. It lowers the caller's OWN open `TimeEntry.quantity_produced` and the operation total by the delta (and, for a REWORK clock-in, symmetrically decrements `quantity_reworked` for FPY), then **recomputes** `work_order.quantity_complete` from its operations (`max` over non-component ops of `min(op qty, target)`, only ever lowered) so a multi-op WO is never pulled below the count held by another operation. Reconcile-safe: lowering the backing evidence together with the operation total means the read-time evidence reconcile never re-raises it. It is **not** a scrap move — scrap fields and status are untouched, the op/WO stay in progress. Every correction writes a tamper-evident `audit_log` update row (`REDUCE_OPERATION_PRODUCTION`) carrying old→new `quantity_complete` **and** `time_entry_quantity_produced` (the produced-qty diff always moves, so the row can never be skipped) plus the operator's mandatory correction reason, `time_entry_id`, and WO before/after — reconstructing what/who/when/why, committed atomically with the mutation. Four server-enforced bounds (never UI-only): **tenant-scoped** (404 cross-tenant); **crew-safe** (delta capped at the caller's own open clock-in — one operator can never alter another's evidence); **before-completion only** (COMPLETE op / terminal WO → 409, re-checked **under the `SELECT … FOR UPDATE` row lock** so a concurrent WO-cancel can't slip through); and **approved-labor refused** (open-but-`approved` entry → 409, preserving the G5-A segregation-of-duties gate — a supervisor unapproves first). Optimistic-lock `version` respected (concurrent stale write → 409). Additive strengthening of the labor-evidence posture — no prior compliance claim changed. **Sign-off:** compliance-auditor endorsed in review. See `docs/API.md` → Shop Floor / `docs/RBAC_PERMISSIONS.md` / `docs/KIOSK.md` | Claude |
| 2026-07-16 | AU-3.3.1 / AS9100D 7.5.3.2 + 8.5.1 (over-count correction — **scope extension driven by production evidence**, branch `fix/reduce-qty-cross-session`; **supersedes the scope described in the 2026-07-16 `feat/wo-completion-reduce-quantity` row above** — that row truthfully describes what shipped and is preserved unrewritten): production use on day one showed over-counts are typically entered at check-out, so the shipped open-clock-in bound refused the first real correction. **The immutability boundary for labor evidence is redefined from clock-out to APPROVAL (G5-A)** — clock-out is an operational event, not a records-control event; before a second party endorses the evidence, a correction through a mandatory-reason, per-entry before→after, hash-chained audit row is the electronic equivalent of the single-line-strike/initial/date convention. (a) Operator self-service (`POST /shop-floor/operations/{id}/reduce-production`) now walks the caller's OWN **unapproved** evidence across sessions — open clock-in first, then their own closed sessions newest-first; approved entries are excluded from the allowance (the refusal is now the 400 allowance message naming the remedy, **replacing the prior dedicated 409** — a deliberate contract change); an approved open entry is never touched at all (no notes/source/`updated_at`/version writes — verified byte-for-byte by test). (b) New office verb `POST /work-orders/operations/{id}/reduce-production`, `require_role([ADMIN, MANAGER, SUPERVISOR])` (= Work Orders Edit; **QUALITY deliberately excluded** — quality endorses or repudiates evidence via the audited approve/unapprove endpoints, production management corrects it), no clock-in required, walking ALL unapproved evidence on the operation (any operator's); the audited unapprove endpoint is the SoD front door for approved labor, and supervisor notes are recorded on the audit row, never on another operator's labor record. Kiosk-scoped tokens cannot reach the office verb (path fence + RBAC). (c) Audit: the aggregate operation-level row (old→new op/WO/produced-sum + mandatory reason, un-skippable — the summed produced-qty diff always moves) is now joined by **one `time_entry`-keyed audit row per walked entry** (before→after, the original operator's `entry_user_id`, linked operation, reason) so an auditor sampling a specific TimeEntry surfaces the administrative reduction by a resource-keyed lookup; rows are hash-chain-consecutive and atomic with the mutation; `extra_data.path` disambiguates `shop_floor` vs `office`. REWORK decrement is portioned per walked entry (FPY true-inverse). All PR #129 invariants re-verified on both verbs: tenant 404, before-completion 409 re-checked under the op→WO row locks, optimistic-lock 409, no scrap/status side-effects, recomputed only-lower WO rollup. Hours/cost legs consume `duration_hours` only, so `REQUIRE_APPROVED_LABOR_FOR_COST` costing never desyncs. **Sign-off:** compliance-auditor re-reviewed and countersigned this extension (conditions — the approved-entry write-protection fix and these doc amendments landing in the same PR — both met). See `docs/API.md` → Shop Floor + Work Orders / `docs/RBAC_PERMISSIONS.md` / `docs/KIOSK.md` / `docs/onboarding/04-planner-supervisor-manager.md` | Claude |
| 2026-07-21 | AU-3.3.1 / AS9100D 8.5.1 production control (work-center deactivation guard + audit coverage, branch `fix/deactivated-wc-queued-work`): deactivating a work center that still had live queued work silently stranded that work — hidden from the Dispatch Board (which renders active machines only) while the operator kiosk kept serving the queue — and BOTH deactivation paths (`DELETE /work-centers/{id}`, ADMIN-only, and `PUT /work-centers/{id}` via `is_active`, ADMIN/MANAGER) committed the state change with **no audit row at all** (same unaudited-interactive-write gap class as the 2026-07-12 purchase-order rows). **Fix:** (a) both paths now REFUSE deactivation with a 409 naming the live-work counts and the remedy while any non-COMPLETE operation on a live (non-deleted, non-terminal) work order references the machine — guard runs before any mutation, count query tenant-scoped, 404-before-409 so a cross-tenant WC-id probe cannot leak op counts; (b) both paths now write tamper-evident `audit_log` rows via `AuditService` (snapshot → mutate → flush → `log_update` → terminal commit, atomic; re-DELETE of an already-inactive WC self-suppresses rather than fabricating a repeat `True→False` row on the hash chain); (c) any ALREADY-deactivated work center still holding queued work now renders on the Dispatch Board as a flagged read-only column (`is_active:false` — move-out only, never a move target, not reorderable) so stranded WIP stays planner-visible until drained; (d) hardened against the nullable-column edge: an explicit `"is_active": null` on PUT is dropped as no-change (a SQL NULL would have slipped the guard and vanished from every board query), and the flagged-column query uses `isnot(True)` so legacy NULL rows surface rather than disappear. The kiosk deliberately keeps serving a deactivated WC's queue (operators finish stranded work; completions/time entries flow through the same audited shop-floor endpoints regardless of WC activity — no records-integrity impact). Known remaining gap (documented in README/API.md, not closed here): interactive `POST /work-centers/` and `POST /work-centers/{id}/status` remain unaudited. No RBAC change. **Sign-off:** compliance-auditor endorsed in review. Describes working-tree behavior on the branch. See `docs/API.md` → Work Centers + Shop Floor | Claude |
| 2026-07-21 | AS9100D 7.5.3 records integrity / SI (work-order header optimistic locking made real, branch `fix/wo-optimistic-locking`): `PUT /work-orders/{id}` claimed optimistic locking (required `WorkOrderUpdate.version`) but never enforced it — the `WorkOrder` model never mapped the `version` column migration `004` created, the endpoint blind-setattr'd the client's value as a transient attribute, and every response serialized `version: 0`, so concurrent header edits silently last-write-won. **Fix:** `WorkOrder` now maps `version` + `__mapper_args__={"version_id_col": version}` (the `WorkOrderOperation`/`TimeEntry` precedent — deliberately not via `OptimisticLockMixin`); the update endpoint pops the client version **before** the setattr loop (the counter is never client-writable) and rejects a mismatch with 409 ("Work order was modified by someone else. Refresh and try again.") before any field is written; a successful update increments the counter server-side and responses now carry the real value. Every other WO write path (release/start/complete, priority, kiosk status flips, soft delete/restore, reconcile) becomes a SQLAlchemy-locked write: a genuine race raises `StaleDataError`, translated to 409 by the existing endpoint-local handlers plus a new app-wide handler in `app/main.py` — on such a conflict the flushed-but-uncommitted `audit_log` row rolls back atomically with the failed change (no audit row for an unapplied change, no unaudited applied change; the audit diff now records the genuine version bump). Migration `069_work_order_version_guard` mirrors `038`'s belt-and-suspenders guard scoped to `work_orders` (add-if-missing else backfill `NULL→1` + re-assert NOT NULL/server_default; documented no-op downgrade; zero-row UPDATE and no DDL on a normally-migrated DB; **no `audit_log` writes**). Server-gated verbs correctly take no client version (non-optimistic convention). No RBAC or tenant-scoping change (version check runs after the tenant-scoped 404 — no cross-tenant 409-vs-404 oracle). **Sign-off:** compliance-auditor endorsed in review. Describes working-tree behavior on the branch. See `docs/API.md` → Work Orders / `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` | Claude |
| 2026-07-21 | AU-3.3.1 (scheduling reassignment audit alignment, branch `fix/scheduling-audit-rows`): `PUT /scheduling/work-orders/{id}/schedule` and `POST /scheduling/work-orders/{id}/schedule-earliest` reassign an operation's `work_center_id`, rewrite its schedule, and can flip its status PENDING→READY — but wrote **no `audit_log` row** (operational events only), while the two dedicated move endpoints audit the identical mutation. **Fix:** both endpoints now write a tamper-evident `audit_log` UPDATE row via `AuditService` on the WO's current operation — five-key old→new diff (`work_center_id`, `run_order`, `scheduled_start`, `scheduled_end`, `status`), snapshot taken before any mutation (including `clear_run_order_on_move`'s rank clear), flushed before the endpoints' single pre-existing terminal commit so the row is atomic with the change; `extra_data` carries `via`, `work_order_id`, `forward_schedule`, and `downstream_operations_scheduled`. Schedule values are normalized to one midnight-anchored ISO form on both diff sides (the DateTime column vs date payload asymmetry would otherwise log a format-artifact "change" on every call and defeat genuine-no-op self-suppression — caught by compliance-auditor in review, fixed, and pinned by an identical-re-submit-writes-no-second-row test). Downstream operations rewritten by the schedule cascade are deliberately not individually audited (documented in-code). **Documented follow-ups, not closed here:** `PUT /scheduling/operations/{id}/schedule`, `/unschedule` (a status-flipping write with no audit and no event), `bulk-schedule-earliest`, `/run` + `/auto-schedule` (mass writes — audit design question), and a `downstream_operations_cleared` count for the non-forward branch. No RBAC or tenant-scoping change. **Sign-off:** compliance-auditor endorsed in review (no blockers; its one note-level finding is the normalization fix above). Describes working-tree behavior on the branch. See `docs/API.md` → Work Orders reassignment notes | Claude |
| 2026-07-20 | SC-3.13.1 (laser-nest AI egress — two-pass verification + multi-page segmentation, branch `feat/laser-nest-pdf-upload`): the laser-nest extraction pipeline (`app/services/laser_nest_extraction_service.py`) now (a) re-egresses each nest sheet a **second time** for an independent verification read (prompt `laser_nest_verification` 1.0.0, `feature="laser_nest_verification"`, same routing task; per-field agreement merge, a pass-2 failure keeps pass 1), applied everywhere nest PDFs are extracted incl. `POST /laser-nests/extract`, and (b) for the new **bare multi-page-PDF upload** on the laser-nest-package preview endpoints, egresses the **entire multi-page PDF** once for AI page segmentation (prompt `laser_nest_segmentation` 1.0.0, `feature="laser_nest_segmentation"`; single-page PDFs skip the call; any failure degrades locally to one-nest-per-page). **No new data classes cross the boundary** — the same nest-report content crosses up to three times per document instead of once. All calls stay behind the fail-closed `allow_ai_egress` kill switch in `run_llm_task` (egress OFF: filename-only rows + one-nest-per-page; page splitting is local `pypdf`) and each writes one tenant-scoped `ai_usage_events` row under its distinct feature string; the confirm-and-commit import re-splits by planner-confirmed pages with **zero** AI calls, and each nest's per-segment PDF is stored as its `DRAWING` Document (audit `source="pdf_import"` unchanged). Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/API.md` → Laser Nests / `docs/AI_QUOTING_AGENT_RUNBOOK.md` | Claude |
| 2026-07-23 | AU-3.3.1 / AS9100D 7.5.3 + 8.7 (soft-delete + receiving corrections for purchasing/quality, branch `feat/deletes-and-receiving-corrections`): `Vendor`, `PurchaseOrder`, `POReceipt`, and `NonConformanceReport` gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr` — metadata-only ADD COLUMN + `is_deleted` index on four existing tenant tables, no backfill, no `audit_log` writes, RLS already enabled by `059`), so these records are now **soft-deletable instead of physically removed** and all live reads filter `is_deleted == false`. New endpoints, each tenant-scoped and tamper-evidently audited: **PO delete + restore** and **vendor delete + restore** (`require_role([ADMIN, MANAGER])`; vendor delete also deactivates; guardrails refuse deleting a vendor with an active PO or a PO with received material, and opening a PO against a deleted/inactive vendor); **NCR void + restore** (`require_role([ADMIN, MANAGER, QUALITY])`, mandatory reason, soft-delete + `VOID` status, refused **400** while an `OPEN`/`ACKNOWLEDGED` `WorkOrderBlocker` still gates a work order) — this void path writes a `log_status_change` **and** a `log_delete`, **closing a prior audit-coverage gap where the `PUT /quality/ncr/{id}` update path emitted only an operational event and no `audit_log` row**; and **receipt correct-in-place (`PATCH`, `require_role([ADMIN, MANAGER, SUPERVISOR])`) + receipt void (`POST .../void`, `require_role([ADMIN, MANAGER])`)**, both requiring a reason and reconciling the PO line, PO status, and (dock-to-stock) inventory. **AS9100D records integrity for the inventory reversal:** the historical `RECEIVE` `InventoryTransaction` is never mutated or deleted — the correction/void appends a **signed compensating `ADJUST`** (`reason_code` `RECEIPT_CORRECTION`/`RECEIPT_VOID`), the same pattern as a manual inventory adjustment; corrections/voids are refused after inspection (**409**), after a lot change with stock placed (**400**), or once the received stock is allocated/consumed (**409**). Also: `DELETE /work-orders/{id}` widened `require_role([ADMIN])` → `require_role([ADMIN, MANAGER])` to match the long-documented RBAC matrix (code was stricter than the matrix; no matrix change). Additive strengthening of the records-integrity posture — no prior compliance claim weakened. **Sign-off:** compliance-auditor review pending. Describes working-tree behavior on the branch. See `docs/API.md` → Purchasing / Quality / Receiving & Inspection / Work Orders, `docs/RBAC_PERMISSIONS.md`, `docs/onboarding/03-warehouse.md` | Claude |
| 2026-07-24 | AC-3.1.3 / AC-3.1.5 / AU-3.3.1 (inventory tenant isolation + stock-mutation authorization + cycle-count lifecycle and audit, branch `hardening/inventory-tenant-rbac`, PR 0): closed cross-tenant resolution, three missing least-privilege gates, and a complete audit gap in `app/api/endpoints/inventory.py`. **Tenant isolation (AC-3.1.3):** location codes / lot numbers / warehouse names are not unique across companies, and several lookups resolved them by value or id alone — `/inventory/receive` and `/inventory/transfer` now resolve the `InventoryLocation` and the existing/destination `InventoryItem` against the active company (foreign code → **404**, never a valid receipt or transfer destination), `/inventory/low-stock` scopes the per-part on-hand aggregate (another tenant's stock could previously be summed into this company's on-hand figure), `POST /inventory/cycle-counts` enrolls only the active company's stock rows, `.../items/{item_id}/count` resolves the parent count **and** the count item by company (**404** otherwise), and `.../complete` adjusts only this company's inventory rows. **Scope of impact — no affected rows.** Two `TenantMixin` inserts omitted the tenant tag: `CycleCountItem` and the COUNT `InventoryTransaction` written on completion. `company_id` is **NOT NULL** on both tables (`TenantMixin`; set NOT NULL by migration `026_add_multi_tenancy`), so those inserts raised `IntegrityError` and the transaction **rolled back** — creating a cycle count that matched any stock row, and completing a count with an adjustment to post, **always 500'd**. **No untagged or cross-tenant row was ever persisted; there are no records to remediate.** The unscoped enrollment query and the unscoped `InventoryItem` lookup in `.../complete` describe what those queries *selected*, not any persisted effect — latent defects masked by the NOT NULL constraint. The missing company predicate on `.../items/{item_id}/count` was a real code-level authorization defect but not reachable in the field, since `POST /inventory/cycle-counts` is the only writer of `cycle_count_items` and it always failed (pre-`026` rows were backfilled to the single seeded company). Adding the stamps is what makes these paths work at all. **Authorization (AC-3.1.5):** `POST /inventory/issue`, `POST /inventory/receive`, and `POST /inventory/transfer` now require **ADMIN / MANAGER / SUPERVISOR** (`require_role`), matching the sibling `/inventory/adjust` and the PO-receipt path `POST /receiving/receive` into the same tables; all three previously accepted any authenticated tenant user (Viewer included). The `docs/RBAC_PERMISSIONS.md` **Transfer** row is now enforced rather than aspirational and a **Receive** row was added. **One cycle-count gate changed — VIEWER excluded:** `POST /inventory/cycle-counts/{id}/start` and `.../items/{item_id}/count` were bare `get_current_user`, so the read-only **Viewer** role (granted `inventory:view` and nothing else) could open a count and write the counted quantities a manager's ledger-posting `complete` derives its adjustment from. Both now carry `require_role(COUNT_WRITE_ROLES)`, defined by **exclusion** — ADMIN / MANAGER / SUPERVISOR / OPERATOR / QUALITY / SHIPPING — so the entire shop-floor counting path is preserved and only the read-only role loses write access; `POST /inventory/cycle-counts` / `.../complete` keep the ADMIN / MANAGER / SUPERVISOR gates they already carried. Narrowing `start` to the stock-mutator set was proposed in an in-progress revision and **reverted before merge**, because combined with the new `record_count` IN_PROGRESS guard it would have left an operator unable to work a SCHEDULED count at all (unable to open one, 409 on any attempt to count into it) — a shop-floor capability regression, not a hardening. The end-to-end operator path is pinned by `tests/api/test_inventory_hardening.py::test_operator_can_start_a_scheduled_count_and_record_into_it`, and every non-Viewer role by `test_start_allowed_for_every_working_role` / `test_record_count_allowed_for_every_working_role`. **No working role loses a capability**, so no owner sign-off is outstanding. `POST /inventory/receive` additionally refuses a **soft-deleted part** with 400 (it resolved the part with no `is_deleted` predicate — new stock and a ledger row could be created against a deleted part); `/inventory/low-stock` carries the same predicate. `/inventory/issue` is additionally marked **deprecated** in favor of a planned work-order-scoped `POST /work-orders/{id}/issue-material` (**not yet implemented**). **Audit + lifecycle (AU-3.3.1):** `.../complete` adjusted stock while writing **no `audit_log` row at all**; it now writes the `/inventory/adjust` dual-row convention per adjusted item (`inventory` CREATE for the COUNT movement + `inventory` UPDATE for the stock level) plus a `cycle_count` STATUS_CHANGE, and `.../start` audits its transition or re-assignment (preserving the original `started_at`). The remaining two lifecycle writes are audited too, so the whole cycle-count lifecycle is on the hash chain: `POST /cycle-counts` writes a `cycle_count` CREATE recording the declared scope (warehouse / location / part) and the number of stock rows it enrolled — the step that defines what `complete` later adjusts — and `.../items/{item_id}/count` writes a `cycle_count_item` UPDATE per counted quantity, carrying the previous values, so a legal re-count while the parent is IN_PROGRESS no longer destroys the only record of the value it replaced. Terminal-state **409** guards on `start` / `complete` close a **ledger double-post** (a second `complete` appended a second COUNT transaction for the same physical variance), and `complete` now takes a `SELECT ... FOR UPDATE` row lock on the count **before** that check-then-act guard, so two concurrent completions cannot both pass it and both post; `record_count` **409**s unless the parent count is IN_PROGRESS so a closed quality record cannot be overwritten; `POST /cycle-counts` **404**s on an unresolvable `location_code` instead of silently ignoring it. `CycleCount.total_variance_value` now stores the **posted** variance so it reconciles against the COUNT ledger rows (`0.0` under `apply_adjustments=false`), priced on the ledger row's own cost basis (the current `InventoryItem.unit_cost`) rather than the enrollment-time snapshot — the two diverge whenever a part is re-costed between enrollment and completion, which made the stored column disagree with the very rows the completion wrote — with the measured total (enrollment basis) returned as the new `measured_variance_value` response field. `GET /inventory/transactions` also gained reference/work-order/lot/date filters plus bounded `limit`/`offset` paging, and is now typed by `InventoryTransactionResponse` (a `UTCModel`), so `created_at` carries the `Z` and the nested `part` object is narrowed to its identifying fields instead of dumping the part's full row incl. cost columns (read-only; the ledger columns and the array envelope are unchanged); its `work_order_id` filter deliberately does **not** exclude soft-deleted work orders, since a voided WO's posted movements remain real ledger facts. Additive strengthening of the existing posture — no prior compliance claim weakened. **Sign-off:** compliance-auditor **PASS** — re-audited after the two blockers raised on first review were closed in-branch (stock mutation in `.../complete` with no `audit_log` row, and a ledger double-post with no terminal-state guard); no owner policy sign-off is outstanding — the only capability removed is **Viewer**'s ability to open a cycle count and write counted quantities (a read-only role writing a quality record), and every *working* role keeps the full counting path; the more aggressive proposal (gating `start` to Admin/Manager/Supervisor) was reverted. Describes working-tree behavior on the branch. See `docs/RBAC_PERMISSIONS.md` → Inventory, `docs/API.md` → Inventory | Claude |
| 2026-07-25 | AU-3.3.1 / AS9100D 8.5.2 / AC-3.1.3 / AC-3.1.5 (material consumption engine — tying stock material to a work order, branch `feat/material-consumption-engine`, PR 1): material now depletes as work completes, via an **optional** tie row. **What shipped:** a new tenant-scoped `work_order_material_allocations` table (migration `074`, with `ENABLE ROW LEVEL SECURITY` per the new-table convention) carrying planned/consumed quantity, a UoM snapshot, an optional lot pin and a source (`nest`/`bom`/`manual`); `inventory_transactions.allocation_id` as the durable genealogy key (migration `075`, nullable FK + index); migration `076` (dialect parity for the `041` indexes — see below); the consumption engine `app/services/material_consumption_service.py`, wired into `apply_completion_inventory_effects` so it inherits every existing completion call site (kiosk clock-out, shop-floor and office operation complete, force-complete, reconcile-on-read) without adding one; and the tie API `GET`/`POST`/`PATCH`/`DELETE /api/v1/work-orders/{id}/material-allocations[/{allocation_id}]`. No frontend ships in this PR. **Scope of impact — no backfill, and untied work orders are unchanged.** Nothing is backfilled: no historical work order gains an allocation (the "not tied" state *is* the absence of a row — there is no flag to migrate and no default tie is ever created), and every pre-existing ledger row keeps a NULL `allocation_id`, which truthfully means "written before the tie existed". A work order with no tie takes **exactly one** tenant-scoped SELECT against `work_order_material_allocations` — read once per completion by `apply_completion_inventory_effects` and threaded into every leg (it was three separate reads of the same table: two unconditional, plus a third when the finished part opted into backflush) — which returns `[]` and then short-circuits with **zero** writes — no inventory row, no ledger row, no audit row, no event — i.e. **byte-identical inventory movement to its pre-feature behavior**, asserted by `backend/tests/api/test_material_consumption.py::test_untied_work_order_transaction_set_is_identical` (which also pins that a tied work order in the same tenant does not perturb it). **Audit coverage added (AU-3.3.1):** every per-run consumption writes three tamper-evident rows through `AuditService` — the `ISSUE` ledger row (`inventory` CREATE), the stock-level change (`inventory` UPDATE) and the tie's `qty_consumed` advance (`work_order_material_allocation` UPDATE) — flushed atomically with the completion (and with the read's own commit on the reconcile-on-read path); tie create / edit / untie are audited, untie as `log_delete(soft_delete=True)` with the tombstone recorded as `status`, not `is_deleted`; both automatic cancellations are audited (nest re-import `reason="superseded_by_reimport"`, work-order delete `reason="work_order_deleted"`); and a shortage writes a **new tamper-evident `ALLOCATION_SHORTAGE` action** (shortfall / required / available, allocation, part, lot, WO + operation) plus a `material_allocation_shortage` warning event → catalog `material.allocation_shortage`, deliberately distinct from `BACKFLUSH_SHORTAGE` so the two mechanisms stay separable. **Non-duplication is algorithmic on this path, not DB-enforced** — consumption posts many `ISSUE` rows per (WO, part) under `reference_type='work_order_operation'`, outside the `041` predicates; a replay converges because `target` is recomputed from live operation state (`delta = 0`), and the AU-3.3.1 narrative above was **corrected** to stop claiming the `041` indexes cover it. Migration `076` is recorded there too: it adds `sqlite_where` to those indexes (zero DDL on Postgres), so **pre-`076` test evidence exercised stricter constraint semantics than production**. **Traceability (AS9100D 8.5.2):** lot/serial genealogy was extended to read the new `work_order_operation` reference type **in the same PR that introduced it** — there is no release in which consumption exists but the as-built record cannot see it — resolving the operation back to its work order so per-run consumption and backflush collapse into the same genealogy lines; scrapped runs post as `ISSUE` (not `SCRAP`) so audited scrap material cannot disappear from the trace. **RBAC for the new verbs (AC-3.1.5):** reads are open to any authenticated tenant user (a tie is shop-visible context, and the same rows are already reachable through lot traceability); `POST` / `PATCH` / `DELETE` require **ADMIN / MANAGER / SUPERVISOR** — Operator gets 403, and the router is mounted under `/work-orders`, **not** `/api/v1/shop-floor`, so kiosk-scoped operator tokens are path-fenced away from it entirely. The consumption itself has **no endpoint and no separate gate**: authority to complete the work is what authorizes the resulting movement, by design. **Tenant isolation (AC-3.1.3):** every lookup — work order, part, operation, pinned lot, allocation — and every engine query is company-scoped; a cross-tenant id is **404**, never 403, so an id cannot be probed. **Outstanding owner decision (not closed by this PR):** the negative-stock-on-shortage posture is now reachable by a **second** mechanism (`ALLOCATION_SHORTAGE`, including a zero-quantity placeholder stock row when the part has no lot at all) — still recorded, still not prevented, still drivable from a GET. It remains an explicit quality/compliance acceptance item; see the AU-3.3.1 callout above. **Compliance re-audit of PR 1 returned FAIL; all findings were closed before this row was finalized.** Closed: (1) BLOCKER — the work-order-scoped `qty_consumed` advance (`_mark_work_order_ties_consumed`) mutated a tenant row with **no audit row at all**, on the very field the untie guard refuses against, while the operation-scoped twin audited it: now audited symmetrically. (2) A **pinned** lot bypassed FIFO's hold filter as well as its ordering, consuming `on_hold` / `quarantine` / `rejected` / inactive material into an as-built record with no signal (AS9100D 8.7): pinning such a lot is now **422** at tie time, and a lot held *after* pinning still consumes but writes **`HELD_MATERIAL_CONSUMED`**. (3) Operation-scoped consumption was **excluded from job-cost actuals** — both `completion_cost_service._issued_material_cost` and `analytics_service._issued_material_cost` filtered on `reference_type='work_order'` only, so a nest burning six $80 sheets left $480 of real, ledgered, audited material out of `WorkOrder.actual_cost`, the synced `JobCost` and the analytics variance: both now share **one** predicate spanning both reference types. (4) `audit` is now a **required** parameter on `cancel_allocations_for_operations`, `cancel_open_allocations_for_work_order` and `build_laser_nest_child_work_order` (it defaulted to `None` behind an `if audit is not None`, making an unaudited CANCEL a one-line mistake). (5) A rolled-back consumption produced no record at all: now **`ALLOCATION_CONSUMPTION_FAILED`**. (6) The entry point documented as "NEVER raises" had its operation SELECT and `begin_nested()` **outside** the guard — it could 500 a live completion; the whole body is now wrapped. (7) A work-order-scoped tie on a part already ISSUEd to that work order could never consume and is now **409** (naming the remedy) instead of a silently-dead `open` row. (8) The hard-delete guard used the `qty_consumed` **cache** as its load-bearing check against an FK with no `ON DELETE`; it now queries the ledger, so drift can no longer surface as a 500 in place of the 409. (9) `POST` silently discarded `qty_per_run` on a work-order-scoped tie while `PATCH` refused it — POST now **422**s identically. (10) The backflush-precedence drop did not verify the tied operation still exists on the work order (the consume path did), so such a part was neither consumed nor backflushed; the two now agree. (11) Tying material to a **TERMINAL** work order is refused **409** — no completion path will reach it. (12) **Evidence integrity:** `test_untied_work_order_transaction_set_is_identical`, cited above as the evidence for the byte-identity claim, did not test it — it never ran the effects on the second work order and compared a fingerprint against itself. It now genuinely completes a CONTROL work order with the allocation table empty and a SUBJECT work order after a **tied** work order has consumed in the same tenant (asserted, so the comparison cannot be vacuous), and compares per-work-order ledger **and** audit fingerprints. **The byte-identity property held as written** — no production behavior had to change for the test to pass. **Second compliance re-audit returned TWO further BLOCKERS; both are closed.** (B1) **The nest re-import — the feature's headline flow — could not run on Postgres at all whenever a laser work order carried a material tie.** `cancel_allocations_for_operations` set `status = CANCELLED` but deliberately KEPT `work_order_operation_id` (for the ledger back-reference), and the caller's very next act deletes exactly those operations. That FK carries no `ON DELETE` (migration `074`) and is declared many-to-one with no parent backref, so SQLAlchemy does not null it and Postgres raises `IntegrityError` — which the import endpoint reports as a misleading **400** ("a nest conflicts with an existing record"). The documented cancel-and-rebuild path was therefore unreachable in production. **The defect was structurally invisible to the test suite**: SQLite defaults `PRAGMA foreign_keys` to **OFF** and nothing in `app/db/database.py` or `tests/` turns it on, so the entire suite runs with foreign keys unenforced against a production database that always enforces them. Fixed by CLEARING `work_order_operation_id` on the ties being cancelled for a wipe — safe for exactly those rows, since they are `cancelled` (so neither partial unique index applies) and carry no consumption (guarded), so no ledger row references the operation id being dropped — with the tie's ORIGINAL scope preserved on the hash chain in both `old_values.work_order_operation_id` and `extra_data.work_order_operation_id`, plus an explicit `extra_data.work_order_operation_id_cleared` marker. Pinned by `test_operation_delete_after_tie_cancel_survives_foreign_key_enforcement`, which enables the pragma for its own body and carries a **positive control** (with the tie still attached, the delete DOES raise) so it cannot pass vacuously; suite-wide FK enforcement is a separate, larger change. The other operation-delete site (work-order hard delete) was re-checked and is correct — it deletes the tie rows before the operations. (B2) **A work-order-scoped tie silently ignored its lot pin.** `POST`/`PATCH` accepted and validated `pinned_inventory_item_id` on both tie shapes, but the demand object that carries a work-order-scoped tie into the one-shot backflush dropped it, and that leg selected stock by lowest id with **no pin and no status filter**: a planner could pin a heat-certified lot, receive a 201, and have the ledger issue from a different lot — the as-built genealogy naming material the operator never touched (**AS9100D 8.5.2**) — and that lot could be `on_hold` / `quarantine` / `rejected` with **no `HELD_MATERIAL_CONSUMED` row**, because `is_consumable_item` was never consulted on this leg. Three shipped documents claimed the opposite, unscoped. Closed by **carrying the pin through** (option b, chosen over refusing the pin with 422, because it makes the shipped documentation true rather than narrowing the feature): the pinned lot is now the exclusive ISSUE target on this leg exactly as on the per-run leg, `is_consumable_item` runs, and a lot held after pinning writes the same `HELD_MATERIAL_CONSUMED` row (with a NULL `work_order_operation_id`, since there is no operation). **Also closed in the same pass:** the work-order-scoped `qty_consumed` advance now happens **only when an ISSUE row actually landed** — a duplicate no-op (the concurrent-completion race the unique index catches) inserted nothing, so advancing the cache claimed consumption that never posted, on the exact field the untie **409** keys on (the per-run engine has always had this guard); **work-order restore** now re-opens exactly the ties the soft delete cancelled, audited, keyed off the cancel's own audit `reason` so a manual untie or a nest-re-import supersede is never resurrected (leaving them cancelled meant a restored work order completed while its tied material silently never depleted, and — once `backflush_components` is exposed — would double-issue the same part); and `GET /inventory/transactions?work_order_id=` now uses the shared ledger predicate instead of a hand-built `reference_type='work_order'`-only clause, so it no longer under-reports an entire nest's material. The predicate itself (`work_order_ledger_filter`) and the two `reference_type` constants moved to `app/db/ledger_filter.py`; lot genealogy, job costing, analytics and the ledger list all import them rather than re-declaring string literals. **Two documentation claims were corrected as unsupportable:** the engine's "idempotent by construction" is **sequential-only** — these rows sit outside `uq_wo_inventory_issue` by design and the allocation row has no `version`, so what actually serializes concurrent completions is the `WorkOrder` / `WorkOrderOperation` `version_id_col` optimistic lock (invariant 4), now stated as load-bearing; and the terminal-work-order **409** is justified solely on "a tie that can never consume is a lie", with the claim that every completion path guards on `TERMINAL_WO_STATUSES` removed (force-complete refuses terminal work orders through its own explicit checks, so the mechanism claim was wrong while the conclusion held). **Third compliance re-audit returned a BLOCKER: B1's fix was INCOMPLETE, and BOTH defects it was recorded above as closing survived it.** The fix cleared `work_order_operation_id` only on the ties `cancel_allocations_for_operations` cancelled, but that query filtered `status != CANCELLED` — so a tie that was **already** `cancelled` was neither guarded nor detached and kept pointing at the operation about to be deleted. Two supported writers produce exactly that row, both deliberately: a **manual untie** (`DELETE …/material-allocations/{id}` sets `cancelled` and keeps the operation id) and the **work-order soft delete** (`cancel_open_allocations_for_work_order`, which keeps it so a restore can put the tie back on the same operation). Consequences: (a) the Postgres `IntegrityError` and the misleading **400** remained reachable through supported verbs alone — tie material to a nest operation, untie it (permitted, `qty_consumed` is 0), re-import the package — after which that work order could **never** be rebuilt; and (b) worse, the **consumed-material 409 was bypassed**. The soft delete cancels every `open` tie *regardless of* `qty_consumed`, so a tie carrying real consumption could reach the wipe as a `cancelled` row the guard never inspected, and its operation was deleted out from under the `ISSUE` rows carrying that operation's lot genealogy — the precise orphaning (**AS9100D 8.5.2**) the 409 exists to prevent. That second path was reachable because `_ensure_laser_child_work_order` had **no `is_deleted` filter**, so a parent-addressed re-import resolved the **soft-deleted** laser child and force-set it back to `released`. **Closed by:** selecting ties for the wipe **status-blind** (every status, so the `qty_consumed` guard sees all of them and the detach reaches all of them); auditing the detach of an already-cancelled tie as its own chain row — a `log_update` of `work_order_operation_id: <old> → None` with `reason="superseded_by_reimport"`, deliberately an `UPDATE` and not a second `DELETE` because `reopen_allocations_cancelled_by_delete` reads a tie's most recent `DELETE` row to decide what a restore may resurrect (that reader gained a matching guard: a tie **detached** after its delete cancelled it is not reopened, since it would come back silently converted to a work-order-scoped tie); and refusing a parent-addressed import or manual nest-add whose only laser child is soft-deleted with **409** naming the work order and the restore remedy — neither resurrecting it nor forking a second child alongside it. **Evidence note (compare finding 12):** the round-2 regression test passed the whole time the defect was live, because it exercised only an `open` tie; it now also carries an already-cancelled tie on a third operation with its **own** positive control (proving the FK rejects a cancelled-but-attached tie exactly as it rejects an open one), plus tests that the guard fires on a `cancelled`-with-consumption tie and that a re-import against a soft-deleted child is refused. **Also closed in the same pass (SHOULD-FIX):** the **unpinned** work-order-scoped leg selected stock as `is_active AND quantity_on_hand > 0 ORDER BY id` with **no status filter**, while its per-run twin filters `status = 'available'` — so it could ISSUE from an `on_hold` / `quarantine` / `rejected` lot with **no `HELD_MATERIAL_CONSUMED` row**, and three shipped sentences (`docs/API.md` ×2, `docs/MATERIAL_CONSUMPTION_PLAN.md`) claimed otherwise, unscoped. Lot **selection** is deliberately unchanged — adding the status predicate would newly exclude legacy NULL-status rows and alter the pre-existing BOM backflush — so the leg now **records** what it consumes: the same `HELD_MATERIAL_CONSUMED` row, marked `pin_directed: false` because on this leg the lot may have been held *before* it was picked, and the three sentences were corrected. Finally, a detached tie no longer reads back over the API as one that was always work-order-scoped: the tie response echoes `detached_from_operation_id`, read off the chain, where previously the original scope survived **only** on the hash chain. **Sign-off:** compliance-auditor re-verified this row's findings — rounds 1 and 2 by code review in their own passes, and round 3 (the reopened B1 `cancelled`-tie and soft-deleted-child paths, plus the unpinned held-lot SHOULD-FIX) by code review of `9542ee0` / `52436fb` / `60bc599` on 2026-07-25 — and returns **PASS**: no blocker or should-fix remains open, and the round-3 changes introduce no new tenant-isolation, audit-logging, soft-delete, RBAC or traceability defect. This row deliberately preserves the record that a finding already presented as closed (B1) was **reopened** on re-verification. Two **notes** stay open, neither a control gap: the soft-deleted-child **409** names a remedy (`POST /work-orders/{id}/restore`, ADMIN/MANAGER) that a SUPERVISOR permitted to run the import cannot perform — the detail now says so explicitly — and the consumed-tie **409** names a reversal verb that does not ship until PR 3, so a restored laser child carrying consumed ties cannot be re-imported until then (recorded in `docs/MATERIAL_CONSUMPTION_PLAN.md` → Known limitations). Test evidence is the implementer's reported run (3252 passed, 2 xfailed); the auditor reviewed the tests as written and did not re-execute the suite. Re-verify if the branch is rebased or amended before it is presented as assessment evidence. Describes working-tree behavior on the branch. See `docs/MATERIAL_CONSUMPTION_PLAN.md`, `docs/API.md` → Work Orders → Material ties, `docs/RBAC_PERMISSIONS.md` → Work Orders / Inventory, `docs/DEVELOPMENT.md` → Completion-inventory migrations | Claude |
| 2026-07-25 | AC-3.1.2 / AC-3.1.3 / AC-3.1.5 (material-tie UX — **station-token disclosure decision**, branch `feat/material-tie-ux`, PR 2): PR 2 gave the PR 1 engine a UI. No schema change, no new consumption call site, and no token scope was widened; the one thing that warrants a compliance record is a **deliberate widening of what an unattended terminal can read**, recorded here because the compliance-auditor's PASS carried a should-fix that the decision existed **only in a Python docstring**, which is not assessment evidence. **What widened:** `GET /shop-floor/work-center-queue/{id}` — readable by a `type="kiosk"` **station** token, i.e. an unattended, PIN-unlocked terminal with **no operator identity** — now carries, per queued operation, the tied material's **part number** and **on-hand quantity** (plus planned/consumed/remaining, shortage, UoM, and the pinned lot **number**). It previously carried **no inventory data of any kind**. **Verdict: ACCEPT.** **Scope bound (verified in code, not asserted):** the on-hand read is strictly limited to parts tied to operations queued at that station's **own** work center — `operation_ids` are derived from `dispatch_service.queued_operations(db, company_id, [work_center_id])`, and a station principal is fenced by `if principal.kind == "station" and principal.work_center_id != work_center_id: 403`, where `work_center_id` comes from the station's **`kiosk_stations` DB row, never client input**. There is **no part-id, work-center-id or company-id parameter a client can widen**, and both the allocation read and the on-hand aggregate are company-scoped (`material_tie_view.tie_views_for_operations`). **Why accepted:** the marginal disclosure is one part number and one quantity on a terminal that already exposes work-order numbers, part numbers and revisions, ordered/complete/scrap quantities, due dates, operator display names, employee IDs and scrap reason codes. Both alternatives are strictly worse: widening `KIOSK_TOKEN_PATH_PREFIXES` so the station could reach the office tie API (`/work-orders/{id}/material-allocations`) would expose an ADMIN/MANAGER/SUPERVISOR-gated mutation surface, and minting a new token scope adds a credential class to revoke and audit. Riding an **already-authorized, already-tenant-scoped** read matches the `scrap_reason_codes` precedent established on this exact payload. `pinned_inventory_item_id` is **deliberately withheld** — an operator reads a lot *number* off a tag and the kiosk has no verb that takes the id. The tie read is a **pure read**: `material_tie_view.py` has no write path, posts no `ISSUE`, writes no audit row and reconciles nothing (a poll is not an actor, has no intent and records no reason). **Stated limit on this precedent — do NOT extend it** to unit cost, extended value, vendor/supplier identity, or heat/cert data on the station payload. On-hand quantity of a part already tied to work queued at that machine is defensible; landed cost and supplier identity on an unattended terminal is a different decision requiring its own row. **`GET /shop-floor/my-active-job`** also gained `material_ties` and a new distinct `operation_quantity_scrapped`. Initially challenged as shipped-with-no-consumer; it is wired as the **fallback** for when the running job is not in the queue of the kiosk's selected machine (`OperatorKiosk.tsx` resolves `activeQueueItem` undefined and the deduction notice would otherwise silently vanish): `materialTies={activeQueueItem?.material_ties ?? view.job.material_ties}`. **Accepted on need-to-know grounds** — this is a per-operator **authenticated** read returning only that operator's own clocked-in job, not a station read. **Correction carried forward from the PR 1 row above (that row is preserved unrewritten):** its "material now depletes as work completes" and "every per-run consumption" wording refers to the sum-delta **scaling** of the quantity (`target = qty_per_run × (op complete + scrapped)`), **not** to when depletion occurs. Verified during PR 2 against every call site: depletion occurs at **work-order** completion only — all five `apply_completion_inventory_effects` call sites sit inside a work-order-completion branch, and a laser child work order carries one operation per nest, so completing nest 1 of 3 moves **no stock**. The engine is *capable* of incremental depletion; nothing *calls* it that way. An assessor reading the PR 1 row forward should read it with this correction. PR 2 shipped truthful UI copy ("deducts N when WO-#### finishes") rather than a new consumption call site; incremental wiring is tracked follow-up and should not land before PR 3's reversal verb. Test evidence is the implementer's reported run (3318 passed); this row's scope-bound and consumer claims were verified by code reading, not by re-executing the suite. Describes working-tree behavior on the branch — re-verify if it is rebased or amended before presenting as assessment evidence. See `docs/MATERIAL_CONSUMPTION_PLAN.md` → Capability vs. wiring, `docs/API.md` → Shop Floor → Material ties on operator reads, `docs/KIOSK.md` → Material deduction notice, `docs/RBAC_PERMISSIONS.md` → Work Orders → Material ties | Claude |
| 2026-07-25 | AU-3.3.1 / AS9100D 8.5.1 + 8.5.2 (incremental material consumption — **tamper-evident inventory records now produced at OPERATION completion**, branch `feat/incremental-material-consumption`, PR 2.5): **What changed.** Tied material previously depleted only when the **work order** completed. It now depletes when an **operation** completes — that operation's ties, and only that operation's — through a new seam `apply_operation_completion_inventory_effects` (`completion_inventory_service.py`) → `consume_tied_materials_for_operation` (`material_consumption_service.py`), called immediately after `finalize_operation_completion` at four handlers: kiosk clock-out, shop-floor operation complete, office operation complete, and the per-operation leg of force-complete. **No schema change, no migration, no new endpoint, no new role or gate, and no token scope widened.** Consumption still has no endpoint and no separate authorization: authority to complete the work is what authorizes the resulting movement, exactly the posture recorded for PR 1. **Why this warrants a row (AU-3.3.1).** It moves an inventory write — and the tamper-evident records it produces — onto the shop-floor **operation**-completion path. The record *set* per consumption is unchanged from PR 1 (the `ISSUE` ledger row as `inventory` CREATE, the stock-level `inventory` UPDATE, the tie's `qty_consumed` advance as `work_order_material_allocation` UPDATE, plus `ALLOCATION_SHORTAGE` / `HELD_MATERIAL_CONSUMED` / `ALLOCATION_CONSUMPTION_FAILED` where those conditions hold), and all of it still flushes **atomically with the status change** in the handler's own unit of work. What changed is **when and how often** those rows are written: on a three-nest laser job the chain now carries three dated consumption events at the three nest completions instead of one burst at job close — which is the more faithful production record, and is the change an assessor comparing pre- and post-branch audit trails will see. The **actor class is unchanged** (an operator badge-minted `scope="kiosk"` token could already trigger consumption by completing the *last* operation of a job); the frequency is not. **Scope is one operation, and that is a controls decision, not an optimization.** A whole-work-order reconcile fired from an operation completion would post against operations still `IN_PROGRESS`, and an in-progress operation is still **correctable** — `production_reduction_service` refuses a walk-back (409) only once `operation.status == COMPLETE` or the work order is terminal. Material consumed against a quantity that is later legitimately reduced would be stranded, because consumption **never auto-reverses** (a negative delta is a no-op, invariant 6b) and the reasoned RETURN verb does not ship until PR 3. Scoping to the just-completed operation makes that unreachable: it is reduce-immune at the instant the `ISSUE` posts, so its target can only move up. Work-order completion still runs the whole-work-order reconcile as the **self-heal**; sum-delta makes the two converge (`delta == 0` for anything already posted), so nothing double-issues. The FG receipt and the BOM backflush deliberately did **not** move — per operation they would double-receive a multi-operation job and collide with `uq_wo_inventory_issue`. **The untied-work-order zero-write property is preserved** (invariant 6(d)): `_open_allocations_for_operation` is tenant-scoped, returns `[]` for an untied operation, and the caller short-circuits with no inventory row, no ledger row, no audit row and no event. An untied work order now pays **one additional tenant-scoped SELECT per operation completion** and **zero additional writes** — the byte-identity promise is about writes and is intact. **Evidence status — read before citing this row.** The existing `backend/tests/api/test_material_consumption.py::test_untied_work_order_transaction_set_is_identical` asserts that property for the **work-order** seam only (it drives `apply_completion_inventory_effects`); the equivalent assertion for the new per-operation seam, and coverage of the new trigger itself, are being added under this repo's standing test gate and are **not** part of the evidence behind this row as written. This row was written from **code reading of the working tree**; no suite run is claimed, and the **compliance-auditor pass for this branch has not been recorded here yet**. Do not present it as assessment evidence until both land. **Known residuals, recorded rather than discovered (see `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Residual gaps of the operation-completion trigger"):** (1) **Force-complete consumes nothing** — `complete_work_order` never writes `operation.quantity_complete` and neither does `finalize_operation_completion`, so a force-completed operation reaches the engine at `target == 0` and posts no ledger row. **Pre-existing** (the work-order trigger had the same hole) and not fixable by hook placement; the fix is force-complete booking the operation quantities it implicitly asserts, which is an open **product decision about production-record honesty (AS9100D 8.5.1)** — a manager's "close it out" is not an operator's count, and fabricating one would deplete material for runs nobody reported. (2) **Reconcile-on-read can flip an operation `COMPLETE` without the work order completing**, and the reconcile inventory hook keys only on `resource_type == "work_order"` transitions, so that operation's tie waits for work-order completion. **Deliberate**: consuming from a GET has no actor, no intent and no reason to record. (3) **`qty_consumed > target` is now an ordinary steady state** — the office operation-complete path writes `quantity_scrapped` absolutely and both reduce-production verbs lower `quantity_complete`; the engine correctly no-ops, but data reviewers should not read it as corruption. (4) **The nest-re-import 409 on a consumed tie now becomes reachable once ONE nest completes**, not once the whole job does; the refusal is correct (the wipe would orphan the lot genealogy the `ISSUE` rows carry — AS9100D 8.5.2) but there is no self-service remedy until PR 3. **Correction carried forward (the PR 1 and PR 2 rows above are preserved unrewritten).** The PR 2 row states that depletion occurs at **work-order completion only** and that incremental wiring "should not land before PR 3's reversal verb". Both were true of PR 2 and are **superseded as of this branch**: depletion now occurs at operation completion, and the incremental trigger landed ahead of the RETURN verb on the reduce-immunity argument above. An assessor reading those rows forward should apply this one. What is **still** true and was not overstated a third time: consumption is **not** per-run — production reporting is deliberately not a trigger, so reporting 3 of 6 runs on an open nest still moves nothing. **Two further AU-3.3.1 changes in this branch, unrelated to material and recorded here because an assessor diffing audit trails will see them.** (1) **Request context restored on the kiosk clock-out path.** `clock_out` constructed a local `AuditService(db, current_user)` inside its completion branch, which — because `audit` is a handler *parameter* — rebound the injected request-scoped instance for the remainder of the function. Every row that branch wrote (operation and work-order `STATUS_CHANGE`, the FG receipt, the backflush, the cost rollup, quality-exception and labor-data-quality rows, the parent-advance row) therefore landed with NULL `ip_address` / `user_agent`, losing "source of the event". It was measurably inconsistent: the consumption rows, written by the injected service before the branch, already carried context, so a single clock-out wrote **two different attribution identities onto one hash chain**. The shadow is removed and the same defect was fixed in its twin (`shop-floor` operation-complete, which now also moves stock). This is **forward-only**: no existing row is mutated and nothing is backfilled. `ip_address` *is* an input to `compute_audit_hash`, but each row hashes over the values it stores, so pre-change rows continue to verify and the chain is unbroken — confirmed by the audit-integrity tests in a green full suite. It also corrects a code comment that asserted the opposite. (2) **AC-3.1.5 least privilege — `POST /api/v1/work-orders/operations/{id}/complete` is now gated to ADMIN/MANAGER/SUPERVISOR/QUALITY.** It was open to any authenticated tenant user, VIEWER and SHIPPING included, and its office siblings were already gated. The gate matches `complete_work_order` -- its larger sibling, which completes every operation on the work order -- so QUALITY is included; excluding it would allow a Quality user to complete a whole work order but not one of its operations. Pre-existing, but it became load-bearing the moment operation completion started decrementing stock: a Viewer could move inventory and write ledger + hash-chain rows. The UI button is gated to match. Operators are unaffected — they complete work through the shop-floor and kiosk routes, which is the documented design. **Invariant 4 hardened alongside:** an optimistic-lock `StaleDataError` inside the consumption engine is now re-raised rather than degraded into an `ALLOCATION_CONSUMPTION_FAILED` row (or, for a conflict raised by the tie read's own autoflush, into no record at all) — a lock conflict must surface as the handler's documented 409, never as a 200 that silently skipped a material deduction. Describes working-tree behavior on the branch — re-verify if it is rebased or amended. See `docs/MATERIAL_CONSUMPTION_PLAN.md` → Capability vs. wiring / Residual gaps, `docs/API.md` → Shop Floor → "Completion also consumes tied material", `docs/KIOSK.md` → Material deduction notice | Claude |
| 2026-07-25 | AU-3.3.1 / AS9100D 8.5.2 + 7.5.3.2 / AC-3.1.5 / AC-3.1.3 (reasoned **material RETURN verb** — a new stock-moving verb, **plus a deliberate relaxation of a countersigned control**, branch `feat/material-return-verb`, PR 3): **⚠️ Two items in this row warrant assessor attention: (1) a new verb that MOVES STOCK and writes tamper-evident ledger rows, and (2) the RELAXATION of the before-completion refusal on the office reduce-production verb — a control re-verified and countersigned by compliance-auditor in the 2026-07-16 `fix/reduce-qty-cross-session` row above. That row is preserved unrewritten; this row carries the change forward.** **(1) What shipped — the reversal (AU-3.3.1 / AS9100D 8.5.2).** `POST /api/v1/work-orders/{id}/material-allocations/{allocation_id}/return` (`require_role([ADMIN, MANAGER, SUPERVISOR])`) and the read `GET …/{allocation_id}/consumption` (any authenticated tenant user). This is the first self-service path for every "reverse consumption first" refusal in the system; until now the engine's never-auto-reverse posture (invariant 6b) left those refusals with no remedy. It is the **compensating-transaction + required-reason + audit** pattern established by the receiving corrections (2026-07-23 row): **no historical row is ever mutated** — each credit is an APPENDED positive `TransactionType.RETURN` `InventoryTransaction` (an enum member that already existed and that nothing wrote, so adopting it disturbed no reader), mirroring the compensated ISSUE's `reference_type`/`reference_id` and `allocation_id`, carrying `reason_code="MATERIAL_RETURN"` and the **compensated row's `unit_cost`** (the lot's *current* cost would strand residual material cost on the job across a revaluation). **No schema change and no migration.** **Traceability (AS9100D 8.5.2):** material returns to the **source lots** it came from, walked newest-first, refusing rather than guessing when a source lot is gone or is a lot-less placeholder anchor (409, receiving's "409 rather than guess" posture) — crediting a convenient sink would invent heat/cert linkage. Per-`(allocation_id, inventory_item_id)` capacity is `issued − already-returned`, which is the idempotency story in place of a unique index; a replay cannot over-credit a lot. **Two named intents and nothing between them, enforced (422):** `correct_over_consumption` is bounded by `qty_consumed − live target` and leaves the tie OPEN; `return_and_untie` returns the full consumed quantity and CANCELS the tie in the same transaction. The bound is not a safety add-on — it is exactly the negative delta the sum-delta engine computes and refuses to execute, so after the return `qty_consumed >= target` and the engine no-ops forever. **A return leaving a still-OPEN tie under-consumed is refused because the material would be re-drawn on the next completion *or the next reconcile-on-read GET*, re-running FIFO and potentially crediting a DIFFERENT lot — one physical sheet traced to two, fabricating heat/cert linkage in an as-built record.** **Audit coverage (AU-3.3.1):** every credit writes the dual `inventory` rows (the `RETURN` ledger row + the on-hand move) plus the tie's `qty_consumed` change as a `work_order_material_allocation` UPDATE; a `return_and_untie` writes a second row for the cancel, stamped `extra_data.reason="material_returned"` — deliberately NOT the work-order-delete cancel reason, so `reopen_allocations_cancelled_by_delete` cannot resurrect a tie whose material was given back. The mandatory non-blank `reason` (Pydantic boundary, per `ReceiptCorrection.reason`) is written in **three** places — the ledger row's `notes`, the audit `description`, and `extra_data.reason` — because the receiving void path recorded it in only one, and a reason absent from the record an auditor pulls is a reason nobody reads. **Records accuracy — every ledger reader nets returns in the SAME PR** (the standing rule that there is no release in which a movement exists but the records cannot see it): the as-built reconstruction in `traceability.py` (signed aggregation; a (WO, part, lot) line that nets to nothing is **dropped entirely**, since a returned lot was never built into the part), `completion_cost_service._issued_material_cost`, three `analytics_service` reads (per-WO material cost, window COGS/turnover, per-part COGS), and `prediction_service._calculate_daily_usage` (reorder points / MRP — counting returned material as usage would make the shop re-buy stock sitting on the rack). The sign is keyed on `transaction_type`, never on the stored sign, so **a dataset with no RETURN rows is numerically unchanged** — and note that widening a type filter *without* the sign flip would have been worse than omitting it, since every one of these readers took `abs()` and would have scored a credit as MORE consumption. Window-scoped analytics reads clamp at 0 (a pre-window issue returned inside the window nets negative as a reporting-boundary artifact); the work-order-scoped cost read deliberately does **not** clamp, so genuine drift would surface rather than hide. **Concurrency:** a return writes neither the `WorkOrder` nor the `WorkOrderOperation` row, so **invariant 4's optimistic lock does not cover it**; the service takes `SELECT … FOR UPDATE` on the operation then the work order (the completion paths' order) before computing the bound, so a completion landing mid-request cannot raise `target` underneath the check. **Tenant isolation (AC-3.1.3):** every lookup is company-scoped; a cross-tenant work order or tie is **404**, never 403. **Authorization (AC-3.1.5):** the return sits in the same ADMIN/MANAGER/SUPERVISOR tier as the tie verbs and **outside the kiosk path fence** — for a stronger reason than the rest of that router, which manages planning rows only: moving stock back with a reason is a bigger power than tying it. A crew-station or kiosk-scoped operator token cannot reach it. The asymmetry with consumption is deliberate: production authorizes depletion because production happened; nothing on the floor authorizes putting material back, so an actor must state why. **(2) CONTROL RELAXATION — office reduce-production now accepts a COMPLETE operation (AS9100D 7.5.3.2).** `POST /work-orders/operations/{id}/reduce-production` passes `allow_completed_operation=True`. The operator's shop-floor twin is **unchanged** (still 409 on a COMPLETE operation) and the **terminal-work-order refusal is unchanged on both**, now carrying its own distinct message. **What was relaxed and why it was set:** the before-completion 409 was justified on "downstream inventory / cost / FG effects have fired and cannot be walked back", and the 2026-07-16 row re-verified it as an invariant of both verbs. **Compensating control:** that justification is no longer true — the RETURN verb in item (1) *is* the walk-back for the material leg, and lowering a completed operation's count is precisely what opens the bounded `correct_over_consumption` allowance the return is measured against (the sanctioned order is reduce first, then return). The two powers travel together in the same role tier, so neither half is exercisable without the authority to do the other, and every correction still writes the same mandatory-reason, per-entry before→after, hash-chained rows the 2026-07-16 row describes. **What it also fixes:** the operator's *"Completed work can't be corrected here -- ask a supervisor"* was a **false referral** — the supervisor's own endpoint hit the identical 409, so the documented escalation path did not exist. **Residual scope not closed:** the FG-receipt and cost legs of a completed operation are **not** reversed by this verb; only the recorded count moves (scrap fields and statuses are untouched — a corrected COMPLETE operation stays COMPLETE with a truthful count) and only tied material has a reversal path. **This relaxation is flagged for auditor/owner sign-off rather than presented as closed.** **(3) CORRECTION carried forward — a documented remedy that PR 3 does NOT deliver.** The PR 1 row above closes with an open note that the consumed-tie **409** "names a reversal verb that does not ship until PR 3, so a restored laser child carrying consumed ties cannot be re-imported until then", and `docs/MATERIAL_CONSUMPTION_PLAN.md` claimed PR 3 would remedy it. **That claim was wrong and the owner chose to correct it rather than build to it. Both rows above are preserved unrewritten; the note stays OPEN.** A return does not unlock a nest re-import: it APPENDS a compensating row and removes nothing, so after a full `return_and_untie` the original ISSUE row **and** the new RETURN row both still carry `reference_type='work_order_operation'` with `reference_id` = an operation the rebuild is about to delete. `work_order_ledger_filter` resolves operation ids through a **live subquery**, so those rows would not merely lose a label — they would silently drop out of job cost, analytics and lot genealogy while remaining in the ledger, leaving an as-built record that disagrees with the ledger it summarizes (**AS9100D 8.5.2**); and the FK carries no `ON DELETE`, so on Postgres the delete raises `IntegrityError` that the import turns into a misleading 400 — the bug class that already shipped through one review because SQLite does not enforce foreign keys. **Accordingly the re-import guard was re-keyed from the `qty_consumed` cache to the LEDGER** (`ledger_backed_allocation_ids`, the basis hard delete has used since PR 1), because a full return drives the cache to 0 and a cache-keyed guard would have waved exactly the case PR 3 created straight through. The refusal messages on re-import and on work-order hard delete were reworded to stop pointing at a reversal as the remedy and to state plainly that returning the material does not remove the history; **"raise a new work order" remains the answer**. The structural fix — superseding operations on re-import instead of deleting them — is recorded as a future PR in `docs/MATERIAL_CONSUMPTION_PLAN.md` → Residual gaps, not promised here. **Also note:** `qty_consumed` is no longer monotonic-up (a return lowers it, float dust clamped to 0); it remains a **cache**, and the authoritative consumed total in a compliance answer is now the **signed** sum of `inventory_transactions` carrying that `allocation_id` (ISSUE minus RETURN). Test evidence is the implementer's reported run; this row's claims were written from code reading of the working tree. Describes working-tree behavior on the branch — re-verify if it is rebased or amended before presenting as assessment evidence. **Sign-off: NOT yet obtained — compliance-auditor review of item (1) and explicit owner/auditor acceptance of the item (2) relaxation are both outstanding.** See `docs/MATERIAL_CONSUMPTION_PLAN.md` → Returning consumed material / Residual gaps / Deliberately deferred, `docs/API.md` → Work Orders → "Material ties" + "Over-count correction … (supervisor/office)", `docs/RBAC_PERMISSIONS.md` → Work Orders → Material ties | Claude |
| 2026-07-25 | AU-3.3.1 / AS9100D 8.5.2 (**BOM/routing backflush leg hardened while still DISABLED** — a records-integrity fix to code that has never executed, plus **one** live guard change, branch `feat/backflush-breadth`, PR 4): **⚠️ Read the scope bound first: `Part.backflush_components` has NO WRITER anywhere in `app/` — no schema field, no endpoint, no UI, `server_default="false"` — so the BOM/routing backflush leg has never run against production data, and NOTHING IN THIS ROW MOVES STOCK IN PRODUCTION except the untie guard in item (3).** **(0) The plan's PR 4 row promised something else, and this row does not claim it was fulfilled.** It read *"Production breadth: expose `backflush_components`, per-op tie editor, BOM alternates fix"*. Reading the leg before exposing it found that turning the flag on would switch on a demand resolver whose output has never been produced, feeding a suppression check that has never executed, into an issue path exercised only with the single-part, tie-shaped demand of PRs 1–3 — carrying eight structural defects, two of which mis-state demand by whole multiples. **The owner re-scoped to harden the leg with the flag still OFF**, the same discipline PR 1 used shipping the engine dark; **exposure moves to a follow-up PR (4.5)** that must add a dry-run demand preview and a refusal gate for BOMs the resolver cannot answer cleanly. The plan's Delivery row was rewritten to state the original promise and the re-scope side by side rather than quietly restated. **(1) The records-integrity defect closed (AS9100D 8.5.2) — predicted in the PR 1 row, and worse than predicted.** Backflush suppression was **status**-keyed only (`_drop_allocation_covered_parts`, on `AllocationStatus.OPEN`), and three guards in a row miss the same case: a tie that **consumed** and is no longer OPEN is invisible to it; `_component_already_issued` keys on `reference_type='work_order'` and so cannot see tied consumption at all (it posts under `work_order_operation`); and `uq_wo_inventory_issue`'s partial predicate does not cover those rows either — which is the very reason the reference type was split. **The same material would therefore have left stock TWICE, writing two as-built lines naming two DIFFERENT lots for one physical consumption.** It was reachable through supported verbs (`cancel_open_allocations_for_work_order` cancels regardless of `qty_consumed`; a restore only re-opens ties whose most recent DELETE audit row carries the delete's own reason, so a cancel from anywhere else leaves a consumed tie CANCELLED forever). Closed by a second, **ledger**-keyed layer (`_drop_ledger_covered_parts`) running alongside the status one — keyed on the ledger because `status` and `qty_consumed` are both documented as non-authoritative planning state, and every other guard of comparable consequence already reads it. **Both layers are load-bearing**: only the status layer can suppress an OPEN tie that has not consumed *yet*. **New tamper-evident audit action `BACKFLUSH_DOUBLE_ISSUE_BLOCKED`** (suppressed quantity + ledger net + part + work order) — the system declining to issue material a planner's BOM asked for is exactly the control gap an as-built review cannot reconstruct after the fact, so it is recorded, not silent; cardinality matches `BACKFLUSH_SHORTAGE` (completion paths and a reconcile that applies a work-order transition, never an ordinary read). **A fully-returned tie nets to zero and is deliberately permitted to re-issue** — the material physically came back, so the BOM's demand is genuinely unmet again; suppressing on mere existence would refuse to consume material the shop is standing next to and hide the gap from the shortage machinery. **(2) Demand-resolution defects fixed, all flag-gated.** **BOM line semantics:** `is_alternate`, `is_optional` and `reference` lines were read **nowhere** on this path, so enabling the flag would have issued every member of an alternate group (an OR, not an AND — multiplying the group's demand by its size) plus tooling and documentation lines; all three are now skipped, matching `mrp_service` so planning and consumption cannot state different demand for one BOM. **State plainly what this is NOT: alternates remain two inert columns with no substitution logic — this is two BOM-explosion helpers agreeing about what a line means, not the "alternates feature" the plan's row named.** **Multi-level BOMs:** `phantom` explodes to its children, `make` is a stocked unit whose subtree is not consumed (issuing both consumes the same material twice). **Routing precedence is now per part:** one stray `component_part_id` on one operation previously disabled the **entire** BOM explosion for the work order — not hypothetical, since `_create_assembly_routing_operations` writes it only for components that *have* a released routing, so a ten-line BOM with two routed components lost the other eight. **Self-consumption refused:** an operation naming the work order's own part would have ISSUEd the part the FG receipt just RECEIVEd. **Scrap is in the basis** — `quantity_complete + Σ(operation.quantity_scrapped)`: a fully-scrapped work order previously backflushed **nothing**, and one shop reported two different consumptions for the same physical event depending on whether the material happened to be tied. **Correction to an earlier draft of THIS row, recorded because it was briefly stated here as a records-integrity fix that the code did not deliver:** the scrap term must come from OPERATION evidence. `WorkOrder.quantity_complete` is rolled up from operations (`sync_work_order_quantity_complete`, `max()`-guarded), but **`WorkOrder.quantity_scrapped` is not rolled up at all** — its only writers in `app/` are a child reset, a null-guard, force-complete's explicit override and the manual office edit. A basis of `work_order.quantity_complete + work_order.quantity_scrapped` therefore reads scrap as **zero** in the ordinary case (an operator scrapping 3 of 10 at the kiosk leaves `operation.quantity_scrapped = 3` and the work-order column at `0`), so the tie engine would consume for 10 and the backflush for 7 — *the exact divergence this item claims to close*. Summing across operations is correct rather than double-counting: a unit scrapped at op 10 and one scrapped at op 20 are different units and both consumed the job's material. `work_order.quantity_scrapped` is a fallback only when no operation carries scrap, which is the force-complete path that sets it. Caught by compliance review before merge; flag-gated throughout, so no production behavior was ever affected either way. **Two quantity defects nobody had predicted, both catastrophic on exposure — the strongest evidence for the harden-dark decision:** (a) `component_quantity` is the **whole-job total** (`qty_per_assembly × quantity_ordered`), not a per-unit rate, and the leg multiplied it by the produced quantity **again** — a 100-piece job at 2/unit would have demanded **20,000**; (b) one component's demand is **replicated across every operation that touches it** and the leg **summed** them, so a three-operation routing tripled demand. **(3) THE ONE LIVE CHANGE — the manual-untie guard now reads the signed ledger net, not the `qty_consumed` cache.** `DELETE …/material-allocations/{id}` refuses **409** while ISSUE − RETURN against that tie is positive. This closes an asymmetry PR 3 left standing (hard delete has read the ledger since PR 1, nest re-import since PR 3; untie alone still read the cache), and the cache misjudged it in **both** directions: a `correct_over_consumption` down to a zero live target leaves `qty_consumed` at 0 on a still-OPEN tie the ledger may still back (permitting an untie that strands `allocation_id` rows against a tombstone), while the backflush advances a work-order-scoped tie's cache to `qty_planned` — not what the ISSUE posted — refusing an untie that would have stranded nothing. **Signed rather than existence-keyed**, deliberately: existence-keying would dead-end a fully-returned tie, 409-ing forever while `return_and_untie` 422s with nothing left to return. Note the two ledger guards ask different questions on purpose — hard delete and re-import ask "would this orphan a ledger reference?" (existence is right, since a RETURN row references the tie as durably as the ISSUE it compensates), untie asks "is material still out?" (only the signed net is right). **No schema change, no migration, no RBAC change, no new endpoint; tenant scoping is on every new query and its operation subquery.** **(4) RESIDUALS DELIBERATELY NOT FIXED — recorded so the exposure PR chooses them rather than inherits them.** (a) **Two contradictory lot-selection policies remain**, and **both write lot genealogy**: the backflush picks lowest-`id`, ignores `status`, ignores `location`, uses a single lot and drives it negative, while the per-run engine walks `received_date` FIFO, filters `status='available'` and spills across lots — so on the same material they can name **different heats for the same physical draw** (AS9100D 8.5.2). Not fixed because `_issue_one_component` is **LIVE** (work-order-scoped ties drive it today), so changing it would alter shipped genealogy for work orders unrelated to the BOM backflush, and adding `status='available'` would newly exclude legacy NULL-status rows; it needs a PR that can carry the data review. (b) **The backflush is a one-shot, not a reconciler** — `uq_wo_inventory_issue` permits one ISSUE per (work order, part) forever, so a later raise to `quantity_complete` never issues the increment. Consistent with the FG receipt, and not a defect today, but it becomes live production behavior the moment a part opts in. (c) **A work-order-scoped `return_and_untie` leaves that part permanently un-issuable on the work order**, unlike the operation-scoped case; this is **not choosable in code** — netting `_component_already_issued` would attempt a re-issue, lose to the unique index, and have the loss swallowed as a duplicate no-op, **claiming a consumption that never posted**, which is strictly worse than refusing. The remedy is an operation-scoped tie, which posts outside the index. (d) `_mark_work_order_ties_consumed` advances a work-order-scoped tie's `qty_consumed` to `qty_planned` while the ISSUE row carries the **summed** BOM + tie demand under that tie's `allocation_id`, so the cache can sit **below** the ledger net; a `return_and_untie` may then be unable to drive the ledger to zero and the untie 409 stands — the conservative direction (the tie stays OPEN, nothing is stranded), reachable only through that drift. **(5) CORRECTIONS carried forward; the cited rows are preserved unrewritten.** The **2026-06-07 Batch 6** row describes the backflush as *"`ISSUE` per component, `scrap_factor`-scaled … scaled by produced qty … resolved from explicit WO-operation component demand first, else by exploding the active BOM"* — that description is **superseded** by items (1)–(2) above on every clause, and note it was never observable in production, since the flag it gates on has never been set. The **PR 1** row's forward-looking note that a delete/restore round trip on a consumed tie "would double-issue the same part once `backflush_components` is exposed" is **closed** by item (1), at the root rather than in the restore path. **Documentation-accuracy correction:** `docs/API.md`'s Part schema listed `backflush_components` in the response payload, which was **false** — the column is on the `parts` table but not on `app/schemas/part.py`, so no part endpoint has ever returned or accepted it. The field was removed from the sample rather than re-annotated, because a schema block is the one place a reader is entitled to take literally; the disclosure prose now states the column has no writer at all. **Evidence and status.** This row's claims were written from code reading of the working tree; **no test run is cited and pytest was deliberately not executed** (concurrent agents share fixed xdist database filenames). Describes working-tree behavior on the branch — re-verify if it is rebased or amended before presenting as assessment evidence. **Sign-off: NOT yet obtained — compliance-auditor review is outstanding, and the residuals in item (4), particularly (a) the divergent lot-selection policies and (b) the one-shot posture, warrant explicit owner/auditor acceptance BEFORE the follow-up PR exposes the flag, since that is the moment they become live production behavior.** See `docs/MATERIAL_CONSUMPTION_PLAN.md` → "The BOM/routing backflush leg (PR 4)" + Delivery rows 4 / 4.5, `docs/API.md` → Work Orders → completion inventory + Parts → Part Schema | Claude |
| 2026-07-27 | AU-3.3.1 / AS9100D 8.5.2 + 8.7 (**backflush lot policy + reconcile-to-target, via a reference-shape split**, branch `feat/backflush-lot-policy`, PR 4.4): **⚠️ Scope bound first, and it has not moved: `Part.backflush_components` still has NO WRITER anywhere in `app/`, and PR 4.4 does not expose it — the BOM/routing half still moves nothing in production.** **No Alembic revision, no schema change** (head stays `076_uq_wo_inv_sqlite_parity`); `uq_wo_inventory_receipt` / `uq_wo_inventory_issue` keep their exact `041`/`076` definitions on both dialects and their lockstep test modules stay green **unmodified**. **(1) A third ledger reference shape, and why it is not a schema change.** Reconciled component consumption — BOM/routing demand *and* work-order-scoped material ties — now posts `reference_type='work_order_backflush'` with `reference_id` = the work order, deliberately outside `uq_wo_inventory_issue` because reconcile-to-target is **not expressible** under a one-row-per-(company, WO, part) index: the leg spills across as many lots as the demand needs and must be able to post a later top-up row. `reference_type` is unconstrained `String(50)` free text (no CHECK, enum or domain in any migration), and `'work_order_operation'` was introduced the same way by PR 1, so this is a **data-value** change. A marker/no-op revision was deliberately not added either. The alternative considered and rejected was dropping `uq_wo_inventory_issue`: a production DDL on the hottest ledger table whose **downgrade becomes un-runnable the moment the first conforming row exists** (it would have to `CREATE UNIQUE INDEX` over data that legitimately violates it) — a one-way door on a regulated record, taken in the PR immediately before exposure. **(2) History is protected structurally, not arithmetically, and NOTHING is backfilled.** Existing `('work_order', ISSUE)` rows are **not** re-keyed — that would mutate regulated, hash-chain-adjacent records no audit row covers. `_component_already_issued` is kept **verbatim** on `reference_type='work_order'`, so it now matches **only** pre-4.4 rows and fences those work orders out of the new engine entirely, on **both** legs. This matters concretely: a historical *summed* BOM + tie row carries a **non-NULL** `allocation_id`, so a net-based guard would have read zero for the BOM portion and re-issued it; the fence makes that class of double-issue impossible rather than avoided. Legacy work orders keep exactly the behaviour they have, forever, and the change is **correct-forward and deploy-order-free**. **(3) One lot-selection policy, closing an AS9100D 8.5.2 residual PR 4 recorded and declined.** Both engines now share `consumable_source_items` (`received_date ASC NULLS LAST, id ASC`) and `plan_stock_draw`; the same predicate is splatted into `material_tie_view` so the dispatch chip and kiosk on-hand hint cannot drift from what the engine draws. Before this, the backflush took the **lowest-`id`** active row, ignored `status` entirely and wrote **one** row for the whole demand while the tie engine walked FIFO over `available` lots and spilled — two engines able to name **different heats for one physical draw**. **THE ONE LIVE BEHAVIOUR CHANGE** rides here: the shared predicate is `COALESCE(status, 'available') = 'available'`, so legacy **NULL-status** stock is consumable. It reaches the shipped operation-scoped tie engine, and it is a *fix* — a bare `status = 'available'` would have hidden real stock, minted a lot-less placeholder and recorded a **false** shortage — but it must be sized against prod before merge, not landed quietly. **(4) AS9100D 8.7 segregation strengthened; `HELD_MATERIAL_CONSUMED` narrowed to one meaning.** The unpinned draw no longer *picks* held / inactive lots (it did, with `pin_directed = false`); it **skips** them, and the fact is disclosed on the shortage row instead (`held_quantity_skipped` / `held_lot_numbers`, in `extra_data` **and** the description), so a shortage is never reported bare against material physically on the rack in segregated status. **The disclosure names the actual constraint, not a plausible one:** on a **pinned** draw the row carries `pinned_lot` instead (*"draw restricted to pinned lot X, other stock not eligible"*), because there the pin — not any lot's status — is why the rest was not drawn; a held-lot clause on that path would point an MRB reviewer at quarantined stock whose release changes nothing and omit the available stock the pin also excluded. The two clauses are mutually exclusive by construction. `pin_directed` is now always `true`; the pinned-lot control and its 422 pin refusal are unchanged. **Two as-built consequences of the skip are recorded as owner-acceptance items in (11) below**, not claimed as closed. **(5) A recorded defect closed: a multi-lot component could go deeply negative with NO shortage row and NO event.** The shortfall used to be computed against lots the draw never walked. It is now `plan_stock_draw`'s remainder over the lots actually walked, so exactly one `ComponentShortage`, one `BACKFLUSH_SHORTAGE` row and one `backflush_shortage` event are produced. A pinned draw that cannot be covered posts a take row plus a separate `(SHORT n)` row **against the same pinned lot** — same lot, same total, at most two rows — instead of one row for the full quantity. **(6) NEW tamper-evident action `BACKFLUSH_COMPONENT_FAILED`, and the latent defect that required it.** Both non-indexed `ISSUE` legs posted with `duplicate_is_noop=True`, which swallows **every** `IntegrityError` and reports it as a concurrent duplicate — correct only while a unique index backs the row, and **no index has ever covered either leg**. The live operation-scoped tie engine has therefore been converting real faults (FK, NOT NULL, or `chk_inventory_items_quantity_non_negative` where live) into recorded **shortages** since PR 1. Both now pass `duplicate_is_noop=False`; a genuine fault rolls back that component/allocation alone inside its savepoint, writes `BACKFLUSH_COMPONENT_FAILED` / `ALLOCATION_CONSUMPTION_FAILED` (`success=false`), leaves the rest of the work order unaffected, and leaves the outer transaction committable so a reconcile-on-read `GET` still returns 200. **A refused write can no longer be recorded as a shortage** — a wrong cause on a compliance record is worse than a missing one. The FG `RECEIVE` keeps `True` (genuinely index-backed, genuinely exposed to the race `041` addressed). **(7) `qty_consumed` is now a record, not a claim — on ONE of the two tie shapes, stated with that bound.** `_advance_tie_consumed` re-reads the **work-order-scoped** tie's **own signed ledger net** after posting instead of writing `qty_planned` regardless of what landed (PR 4's fourth residual, which was stated about that shape), so `return_and_untie`'s "give back exactly `qty_consumed`" and `correct_over_consumption`'s `qty_consumed − target` allowance are exact by construction. The **operation-scoped** engine still writes `qty_consumed = target`, so the same column is a ledger-backed record on one tie shape and the engine's run-scaled intent on the other. That asymmetry is deliberate (the per-run engine recomputes `target` from live operation state on every pass and converges by reconciliation), and it changes nothing about the standing rule: **the authoritative consumed total in a compliance answer is the signed ledger sum on that `allocation_id`, never the cache, on either shape.** The BOM/tie summing (`WorkOrderMaterialAllocationDemand`) is deleted: the two demand sources post **separately-attributed** rows (tie rows carry `allocation_id`, BOM rows are NULL) and **the total for a part carrying both is unchanged**. **(8) A notification that has been silently dropped since Batch 6 now fires — and so does the degraded case underneath it. THREE catalog entries, not one.** `backflush_shortage` was emitted with **no catalog entry**, and the outbox tee ignores uncataloged event types by design — a recorded BOM shortage notified **nobody**. New catalog key **`material.backflush_shortage`** (Purchasing, warning, in-app + email, departments Purchasing + Inventory), a deliberate sibling of `material.allocation_shortage` so a tied-material shortage and a BOM shortage stay separable and independently gateable. The emit site is unchanged, and the `audit_log` row — not the notification — remains the compliance record. **Two further keys were added in-branch after review, closing a gap item (6) would otherwise have opened:** the failure actions emitted no event, which made the rolled-back path strictly **quieter** than the shortage it degrades from — and on a database where `chk_inventory_items_quantity_non_negative` is live, *every* shortage arrives on that path, so the headline notification would have been exactly the one that never fires. **`material.allocation_consumption_failed`** and **`material.backflush_failed`** (Purchasing, warning, in-app + email) carry it, separately keyed so "stock went negative" and "stock never moved" are distinguishable without opening the audit log. **(9) Readers: one line.** `work_order_ledger_filter`'s first arm widens to `reference_type IN ('work_order','work_order_backflush')`; job costing, analytics, lot genealogy and `GET /inventory/transactions?work_order_id=` inherit it and `traceability.py` needed **no** code change. Genealogy now shows one spilled draw as N lines naming N heats — the truthful as-built record the summed row could not produce. Tenant scoping is on every new query. **(10) WHAT THIS PR DOES NOT DO — stated because two delivery rows in this series have already promised more than the PR delivered.** It **does not add a re-entry trigger**, so *"a later rise in `quantity_complete` issues nothing"* **REMAINS TRUE**: the arithmetic is fixed, the trigger is not added, and the leg still runs exactly once per work-order lifetime (every operation-completion handler refuses a terminal parent; `complete_work_order` early-returns for COMPLETE/CLOSED; reconcile-on-read strips terminal work orders; COMPLETE → non-terminal is blocked). It **does not expose** `backflush_components`, add a dry-run demand preview or a refusal gate, change whether a work-order-scoped tie adds to or replaces BOM demand (still additive), give BOM-driven material a return verb (`allocation_id IS NULL` rows stay outside PR 3's RETURN engine), fix the `_placeholder_stock_row` return dead-end — **which it in fact ENLARGES in two recorded ways: the held-lot skip makes a placeholder more reachable on the backflush leg, and a part carrying both a work-order-scoped tie and BOM demand with zero stock now mints TWO lot-less placeholders (one per leg) where the pre-4.4 summed draw minted one, doubling the un-returnable rows for that part** — address the `_drop_ledger_covered_parts` live-subquery blind spot, or decide the shortage posture against `chk_inventory_items_quantity_non_negative`. The `POST /material-allocations` `already_issued` 409 is **unreachable** (a tie needs a non-terminal work order; the backflush only runs at COMPLETE; COMPLETE → non-terminal is blocked) — its **wording and docstrings were corrected**, its behaviour was not, because shipping a refusal whose stated reason is false is a records-integrity defect even when nobody can trigger it. **(11) OWNER DECISIONS RECORDED NOW SO PR 4.5 CHOOSES THEM RATHER THAN INHERITS THEM — ACCEPTED RESIDUALS.** Exposure of `Part.backflush_components` will use **the ordinary part-edit field**, not a dedicated reasoned verb. Three consequences follow and are accepted: (a) the authorization tier is whatever `PUT /parts/{id}` already enforces — **`require_role([ADMIN, MANAGER, SUPERVISOR])`**, so a **supervisor** can arm a control that moves stock on every future completion of that part; (b) **no reason is captured** — the `AuditService` `log_update` row records who, when and false→true, but not why; (c) **a concurrent flip does not 409**, because `Part` maps no `version` column (invariant 4's optimistic lock does not reach it), so last-write-wins. None of these is forced by the design; a reasoned, separately-gated verb was available and was not chosen. Flagged for assessor attention on the question of whether arming a material-moving control should be a plain field edit. **Two more accepted residuals belong on this list, raised in compliance review of this PR and recorded here rather than fixed — they concern the HELD-LOT posture in item (4), whose correctness under 8.7 is NOT in question.** (d) **The as-built record no longer names a segregated lot.** `held_quantity_skipped` / `held_lot_numbers` live in the `audit_log` row and the operational-event payload **only**; the as-built reconstruction reads `inventory_transactions`, so a skipped lot appears on no genealogy line. Where a part's stock is **wholly** held, the genealogy line is now a **lot-less placeholder naming no heat**, where before PR 4.4 it named the held lot with a `HELD_MATERIAL_CONSUMED` row beside it. The new record is arguably more truthful — the material genuinely was not drawn — and the shortage now actually notifies; but **an auditor reading the as-built record alone can no longer reach the segregated-material fact** and must pull the audit row for it. (e) **That same skip makes the zero-quantity placeholder newly reachable on the backflush leg, and PR 3 refuses to credit a placeholder**, so those rows are **permanently un-returnable** (compounded by the two-placeholder case in item (10)). **Blast radius is zero while release-gate query 2 (`work_order_material_allocations WHERE work_order_operation_id IS NULL`) returns 0**, since the BOM half is still dark — which is why these are acceptance items rather than blockers. Owner acceptance is requested on (d) and (e) together with the exposure decisions above. **(12) Release gate — read-only prod counts before merge** (owner-run): `('work_order', ISSUE)` rows with non-NULL `reference_id` (the rows the legacy fence must cover), `work_order_material_allocations WHERE work_order_operation_id IS NULL`, `parts WHERE backflush_components = true`, whether `chk_inventory_items_quantity_non_negative` exists in `pg_constraint`, and the NULL-`status` stock count for item (3)'s blast radius. If the first three are zero, no shipped ledger row changes and rollback is a plain code revert with no data step. **Open question carried, not answered — and sharpened by review:** if the negative-stock CHECK **is** live in prod, the warn-and-record shortage posture (shared with the live tie engine, and matching `/inventory/adjust`) is unimplementable as designed. Concretely, the `duplicate_is_noop=False` flip in item (6) means the CHECK now **raises** instead of being swallowed, and the per-component savepoint **rolls that draw back** — so on such a database *every* shortage stops being a shortage: **no stock moves**, no `ISSUE` row posts, the ledger under-reports the job's consumption, the as-built record shows material that was never drawn, and the row written is `BACKFLUSH_COMPONENT_FAILED` / `ALLOCATION_CONSUMPTION_FAILED` rather than `BACKFLUSH_SHORTAGE` / `ALLOCATION_SHORTAGE`. That is a strictly better *record* than the pre-4.4 behaviour (which recorded the same fault as a **shortage** — a wrong cause) and it is still not the designed posture. The savepoint makes it *survivable*, not *correct*. **One consequence was closed in-branch, one was not:** the degraded path now notifies (item (8)'s two new keys), so a CHECK-live deployment is no longer silent; the **posture** — whether to drop the constraint or to record shortages without driving the lot negative — remains the owner's to decide, and 4.5 cannot expose the flag before it is decided. **(13) SUPERSESSION of the PR 4 row's residuals; that row is preserved unrewritten.** Its item (4)(a) (*two contradictory lot-selection policies*) is closed by item (3) here — except `location`, which is still not a selection input on either engine and is not claimed as fixed. Its (4)(d) (*a work-order-scoped tie's `qty_consumed` can sit below the ledger net*) is closed by item (7). Its (4)(b) (*the backflush is a one-shot, not a reconciler*) is closed **as arithmetic only** — read item (10): the trigger is unchanged and the observable statement *"a later raise to `quantity_complete` never issues the increment"* is **still true**. Its (4)(c) (*a work-order-scoped `return_and_untie` leaves that part permanently un-issuable*) is closed **for work orders created after this PR** and is **permanent for legacy ones**, by construction of the fence in item (2) plus the no-backfill rule; the claim in that row that it was *"not choosable in code"* was sound about the shape it described and is retired with that shape. **Evidence and status.** Written from code reading of the working tree. **Review gates have since run and this row was corrected against them.** compliance-auditor returned **PASS** with two pre-merge corrections and one owner sign-off item — the corrections (an over-claiming API docstring, and this document's sibling plan citing a helper the PR deletes) were made, and the sign-off item is item (11)(d)/(e) above; code-review's static gate (`black` / `isort` / `flake8` / `mypy` / `bandit`) was clean for every file this PR touches. Findings from those gates that were **not** fixed are recorded above as residuals rather than dropped: the two-placeholder case in item (10) and the CHECK-live consequence in the open item. **No test run is cited in this row as evidence** — the implementer reported a green full suite *before* the review-driven fixes landed, so the figure is stale against HEAD and must be re-run. Describes working-tree behaviour on the branch — re-verify if it is rebased or amended before presenting as assessment evidence. **Sign-off: NOT yet obtained** — items (3), (11) (including the newly added (d) and (e)) and (12) warrant explicit owner/assessor acceptance. See `docs/MATERIAL_CONSUMPTION_PLAN.md` → "The reconciling backflush and one lot policy (PR 4.4)" + Delivery rows 4.4 / 4.5, `docs/NOTIFICATIONS.md` → `material.backflush_shortage`, `docs/API.md` → Work Orders → "Material ties" + Inventory → transaction history. | Claude |
| 2026-07-27 | AC-3.1.5 / AU-3.3.1 / AS9100D 8.5.2 (**`Part.backflush_components` EXPOSED — the BOM/routing backflush leg becomes armable for the first time**, branch `feat/backflush-exposure`, PR 4.5): **⚠️ READ ITEM (9) BEFORE ACCEPTING THIS ROW. Exposure PRECEDED the owner acceptance that the 2026-07-25 (PR 4) row required as its condition for exposing the flag, the one-shot residual it named is still undischarged, and the release-gate question `docs/MATERIAL_CONSUMPTION_PLAN.md` open question 4 said exposure must wait on — *is `chk_inventory_items_quantity_non_negative` live in prod?* — is still unanswered. Sign-off is NOT obtained.** **⚠️ Scope bound second, and the bound MOVED under review. This row changes WHO CAN ARM the leg — and, because arming it made one silent completion-path behaviour untenable, it also changes what the leg DOES when a BOM the resolver has already judged wrong reaches completion. That change is item 8(b), it is a live behaviour change, and the first draft of this row denied it.** Otherwise unchanged: no ledger row, no reference shape, no lot policy, no schema change and no Alembic revision (the column landed in migration `040` with `nullable=False, server_default="false"`). **And nothing in production has opted in** — every part is still `false`, so the leg still has not executed against production data. The correct reading of this row is that a previously *unreachable* body of code is now *reachable by a supervisor's ordinary part edit*. **(1) Where the field lives IS the control.** `backflush_components` is on **`PartResponse` and `PartUpdate` only** — deliberately **not** on `PartBase`, therefore not on `PartCreate`. Both create endpoints and both CSV importers splat `Part(**data)`, so a field on `PartBase` would have become settable on **four** write paths at once with no gate and no readiness check, letting a spreadsheet column switch on a permanent, shop-wide policy that moves stock automatically forever after. A part is therefore always **created off** and can only be armed through an update. An explicit `null` is **422** rather than a `NOT NULL` violation. `_part_to_response` populates the field explicitly, so the list endpoints and `GET /parts/{id}` cannot disagree about whether a part auto-consumes its BOM — a divergence nobody notices until material has moved. **(2) One refusal gate, on BOTH part-write doors.** `PUT /materials/{id}` is a byte-identical `setattr` loop over the **same** `PartUpdate` schema writing the **same** `parts` rows, so the gate is defined once (`parts.assert_backflush_change_allowed`) and **imported** by `materials.py`; a gate implemented in only one of the two files would not be a gate. It runs **before the first `setattr`**, so a refusal leaves the row untouched. Enabling is refused **409** while any **blocking** diagnostic from `backflush_readiness_for_part` stands, `detail` a plain string of the blocker sentences joined (*"Part {pn} cannot enable automatic backflush: {what is wrong}. {what to change}."*), with the structured list on `GET /parts/{id}/backflush-readiness`. **Disabling is never gated** (stopping automatic consumption cannot issue wrong material) and re-stating the current value is not a state change. **(3) Arming is an audited state change, and the audit row carries the VERDICT, not just the value.** The flip lands in `AuditService.log_update`'s `changes` map like any part edit; `extra_data` adds `backflush_readiness` (`clean` / `not_evaluated_disable`), `backflush_readiness_checked_at`, and the advisory codes outstanding at the time. That verdict is **not reconstructable afterwards** — every input it read (BOM lines and their `is_alternate` / `is_optional` / `item_type` / `quantity` / `unit_of_measure`) is mutable by other people — which is the only reason it is recorded. **WHAT `backflush_readiness: "clean"` ASSERTS, AND WHAT AN ASSESSOR WILL OVERREAD IT AS.** It asserts one thing: *at that instant, `backflush_readiness_for_part` found no blocking diagnostic in the part's own BOM explosion.* Three limits belong beside the value every time it is quoted. **(i) It covers the BOM half only — the ROUTING half is never checked at opt-in at all.** `backflush_readiness_for_part` runs the BOM explosion and nothing else; the four routing codes (`operation_names_own_part`, `operations_disagree_on_component`, `routing_component_excluded_by_bom`, `routing_bom_quantity_disagreement`) are produced only by the work-order-scoped resolver and are **unreachable at part scope**. A `clean` verdict — and the `eligible: true` the UI shows next to it — therefore says nothing whatsoever about whether this part's routing will resolve wrong demand on a real job. **(ii) It is a one-time check with no downstream re-validation.** It is evaluated once, at the flip, and never again: nothing re-runs it when a BOM is edited, when a routing changes, or when a work order is released or completed. **(iii) Every input stays mutable afterwards, by the same tier, with no signal.** Anyone with `boms:edit` / `routings:edit` — the same ADMIN/MANAGER/SUPERVISOR trio — can change any of them, and **nothing on the BOM or routing edit path knows the part is armed**: there is no warning, no re-check, no audit annotation and no notification on the editing side. The only thing standing between "clean at 09:00 Monday" and wrong material at Wednesday's completion is the completion-path refusal in item 8(b), which is a **net, not a gate** — it catches the BOM conditions it can detect and records them; it does not restore the assurance the verdict is read as giving. **The query recipe, because the trail must be ONE query.** `PUT /materials/{id}` used to log this row as `resource_type="material"` while `PUT /parts/{id}` logged `"part"` — the same `parts` row, the same shared gate, so an auditor filtering `resource_type='part'` for *"who armed automatic stock movement"* would have silently missed every flip made through the materials door. `update_material` is now normalised to **`resource_type="part"`**; `create_material` / `delete_material` still log `"material"`, so reconstructing a material record's full history remains a two-type query exactly as before — but the *update* rows moved sides at this commit, which makes the trail **discontinuous on that date**, and a disclosure buried in this row and a call-site docstring is not one a shop-floor auditor will meet. It is therefore also stated where they filter: `docs/API.md` → Audit Log now carries a warning that `resource_type` alone returns a partial history for a part/material and that the query must be `resource_type IN ('part','material')`. **THE RECIPE — this is the canonical form; `docs/API.md`, `docs/RBAC_PERMISSIONS.md` and `docs/MATERIAL_CONSUMPTION_PLAN.md` point here rather than restating it.** `audit_log.extra_data` is a JSON column, so on Postgres: *every flip of the flag, in either direction* — `SELECT created_at, user_id, resource_id, resource_identifier, extra_data FROM audit_log WHERE resource_type = 'part' AND action = 'UPDATE' AND extra_data->>'backflush_readiness' IS NOT NULL ORDER BY sequence_number;`. Narrow to **"who ARMED automatic stock movement"** with `AND extra_data->>'backflush_readiness' = 'clean'` (the disarm rows carry `not_evaluated_disable`); `extra_data->>'backflush_components'` carries the same answer as `true`/`false`. Three things the recipe depends on, each of which would silently break it if changed: (i) **both doors log `resource_type='part'`** — the recipe is single-type only because `update_material` was normalised, so any new writer of this column must log `"part"` too; (ii) **`extra_data` is written only when the flag actually moves**, so the predicate is a state-change filter, not merely a "someone edited a part" filter; (iii) **the direction is also in the row's own `changes` map** (`backflush_components: {old, new}`), which is the cross-check if `extra_data` is ever restructured. The recipe returns WHO / WHEN / WHICH PART / WHICH VERDICT. It does **not** return **why** — no reason is captured on this control (residual (4)(b)) — and the verdict it returns is `clean` in the narrow sense set out in the paragraph above, not a statement that the part's demand will resolve correctly. **(4) ACCEPTED RESIDUALS ON THE OPT-IN GATE — the owner chose the ordinary part-edit field over a dedicated reasoned verb, and these follow from that choice rather than from oversight.** (a) **Supervisor-tier**: the gate is whatever `PUT /parts/{id}` / `PUT /materials/{id}` already enforce, `require_role([ADMIN, MANAGER, SUPERVISOR])` — **the same permission as editing a description**. (b) **No reason is captured.** Every other control change in this series (the PR 3 RETURN verb, untie, receiving void) requires a written reason; this one records the readiness verdict instead. (c) **A concurrent flip does not 409.** `Part` maps no `version` column — migration `004` versioned the *table*, the model never mapped it — so `PartUpdate.version` is required, written onto an unmapped attribute by the `setattr` loop, and `_part_to_response` returns a hard-coded `0`. Optimistic locking on parts is **cosmetic**; last write wins, and two people arming/disarming concurrently silently resolve to whoever committed last. (d) **`scripts/seed_data.py` splats `Part(company_id=..., **data)`** and can set the column with no code change, bypassing the gate entirely — not a production path, recorded because "the only writer" is a claim about `app/`. (e) **The second door is closed by a shared function, not structurally**: a dedicated verb with the field on `PartResponse` only would have made `PUT /materials/{id}` incapable of writing it. Flagged for assessor attention on the same question PR 4.4 raised and did not settle — whether arming a material-moving control belongs in a plain field edit. **(f) NOT a residual — closed before merge: a SOFT-DELETED part cannot be armed.** Neither PUT lookup filters `is_deleted` (a pre-existing omission this feature inherits) and `delete_part` checks dependencies only on a *hard* delete, so a soft-deleted part keeps its in-flight work orders and stays reachable by id; arming it would have moved component stock for a part the shop believes is gone. `backflush_readiness_for_part` now raises a blocking **`deleted_part`** diagnostic, which closes **both** doors and the readiness read at once without touching a lookup four other handlers share. **(g) NOT a residual — closed before merge: no diagnostic can disclose another tenant's part.** `BOMItem.component_part` was joinedloaded with no `company_id` predicate, and `bom.py`'s add-line validator resolves `component_part_id` unscoped, so a BOM line pointing at another company's sequential id was reachable through supported verbs. Harmless while nothing rendered the object — and a **disclosure** the moment this PR started serving diagnostics through a readiness GET open to every authenticated tenant user and echoing them in the 409 (invariant 1, one hop out). Fixed at the **lookup**: components resolve through a tenant-scoped batch read, so the foreign row is never materialised; an id that does not resolve in this company yields a `missing_component_part` diagnostic carrying **only the BOM line id** — no part number, no component id. Same-tenant soft-deleted components still name themselves, because that is this company's own part and the sentence is otherwise unactionable. `bom.py:1497`'s unscoped component validator is a separate, still-open defect. **(5) TWO NEW READS, AND THEIR PURITY IS STRUCTURAL.** `GET /parts/{id}/backflush-readiness` and `GET /work-orders/{id}/backflush-preview` write **nothing**: no `InventoryTransaction`, no `audit_log` row — in particular no **`BACKFLUSH_DOUBLE_ISSUE_BLOCKED`**, whose documented cardinality (completion paths and a reconcile that applies a transition, never an ordinary read) a preview taking the recording path would have falsified while polluting the hash chain with rows describing nothing that happened — and no operational event. That is enforced by shape: the resolution layer takes no `AuditService` at all, and the suppression **recording** was split into a separate function only the completion path calls (`_drop_ledger_covered_parts` now returns what it dropped instead of logging it). The preview was deliberately **not** built by savepoint-and-rollback, which would have coupled a read to a write path forever. Same boundary `material_tie_view.py` exists to hold: a poll is not an actor and records no reason. Both reads are open to any authenticated tenant user (read-broad / write-restricted, per `docs/RBAC_PERMISSIONS.md`), tenant-scoped, 404 on a cross-tenant id. **(6) PREVIEW FIDELITY IS A RECORDS-INTEGRITY PROPERTY, NOT A UX ONE.** The preview models the **issue loop** — both legs in the real order (work-order-scoped ties first, so a pin gets first claim), the legacy `('work_order', ISSUE)` fence, the reconcile-to-target delta, and the actual lot pick through the same `consumable_source_items` + `plan_stock_draw` the writer uses — because both engines write **lot genealogy**, and a preview built on its own predicate would show a heat the backflush will never draw and hide one it will. **Two fidelity defects were found and closed under review, both in the direction of the preview understating what moves.** (i) **The shortfall row.** `plan_stock_draw` returns only the *covered* takes; the writer additionally posts the unmet remainder as a **second ISSUE against the last lot it drew**, driving that lot negative and putting **its** lot number on the as-built record. The preview reported the remainder as a bare scalar, so a demand of 25 over lots A(10) and B(5) previewed *"A:10, B:5, short 10"* while the completion posted A:10, B:5 **and B:10** — B contributing 15, not 5. Both paths now go through one `_shortfall_anchor`, and the preview emits the remainder as a flagged lot row (`is_shortfall`) or, where no stock row exists at all, as `shortfall_creates_placeholder` (the writer would mint a lot-less placeholder row, which a read may not do). (ii) **A held PIN.** Where a tie's pinned lot has gone `on_hold` / `quarantine` / `rejected` **since** it was pinned, the writer consumes it anyway and records `HELD_MATERIAL_CONSUMED(pinned=True)`; that draw is not short, so no held-stock disclosure ran and the dry run showed a clean line over quarantined material about to enter product. Now surfaced as `pinned_lot_is_held` — the single most consequential thing a pre-completion dry run can say. **(7) A TYPED DIAGNOSTICS LAYER OVER THE RESOLVER, PURELY ADDITIVE.** The resolver's warn-and-continue sites and its three previously **silent** ones now also append a typed `_BackflushDiagnostic` (**21 codes: 16 that present as `blocking`, 6 as `advisory`** — `no_demand_source` is both, advisory at work-order scope and blocking at part opt-in, and `deleted_part` is part-scope only); every existing log line stays where it was and **the pure resolver still returns exactly what it returned before** — acting on a diagnostic is the caller's job (item 8(b)), never the read's. A **BOM depth cap of 20** was added mirroring `explode_bom_recursive`: the visited-set guard bounds repetition of a *part*, not recursion depth, and a `RecursionError` here would have been swallowed whole by the `except Exception: pass` at the two reconcile-on-read call sites — losing an entire completion's inventory effects, FG receipt included, to a log line. **(8) WHAT THIS PR DOES NOT DO — stated because exposure is the moment each of these stops being theoretical.** (a) **No re-entry trigger; PR 4.4's headline caveat stands verbatim** — a rise in `quantity_complete` after completion still issues nothing and still writes no record of the gap, while a *tied* part in the same situation would draw the extra material. (b) ~~A blocking diagnostic still does not refuse anything AT COMPLETION.~~ **CORRECTED BEFORE MERGE — it now does, and it records it.** The first draft of this row stated that the completion path "still logs-and-issues exactly as PR 4 left it". That was **inaccurate as well as unsafe**: eight of the newly-detected blocking conditions (`zero_bom_quantity`, `negative_bom_quantity`, `unit_of_measure_mismatch`, `foreign_component_part`, `alternate_group_without_primary`, `deleted_active_bom`, `missing_component_part`, `circular_bom`) had no log line either, so on the completion path they left **no production trace at all** — the only artifact was a ledger row that read like an ordinary consumption. Since the opt-in gate is a one-time check and every input it reads stays mutable by anyone with `boms:edit`, the reachable state was: arm a clean part, edit a BOM line, and the leg issues material against demand the system itself had judged untrustworthy, silently. **What ships instead:** `_resolve_backflush_components` now applies the resolver's blocking diagnostics — refusing the demand each one describes and writing one **`BACKFLUSH_DEMAND_REFUSED`** `audit_log` row per diagnostic, carrying the code, the operator-facing sentence, the BOM line / operation it names, and the quantity that did **not** move. **`refused_quantity` is attributed once per refused SCOPE, not once per row** — one BOM line can raise three blocking diagnostics and two lines can name one component, so the first row naming a given component carries the quantity and later ones carry `0` (the structural tier already charged its whole-leg total to the first structural row). Without that, an assessor summing the action would read a figure larger than what actually failed to move — a false figure on the tamper-evident chain, which is the same defect `_record_backflush_demand_suppressed` already guards against by recording a tie's unmet remainder rather than its gross plan. Those same rows emit a warning **`material.backflush_demand_refused`** `OperationalEvent` (Purchasing / in-app + email, catalog sibling of `material.backflush_shortage` and `material.backflush_failed`), under its own `begin_nested()` savepoint because the path is reachable from a reconcile-on-read GET. **That notification is not decoration:** the refusal fires at completion on a part that is *already armed*, nothing disarms it, and nothing on the BOM edit path knows — so an audit row alone would mean the same component silently under-issues on every subsequent job. **The scope rule is structural, not a code list:** a blocking diagnostic that names a `component_part_id` refuses **that component only**; one that names none refuses **the whole leg**, because the resolved demand is then incomplete in a way no component owns. Four blocking codes name none today — `deleted_active_bom`, `bom_depth_exceeded`, `missing_component_part`, and the *foreign-component* branch of `foreign_component_part`, which deliberately carries no identity at all (item (4)(g)) and which the tenant-scoped component lookup should make unreachable. (Earlier drafts of this row, of `docs/API.md` and of the module docstring said "three"; the fourth was missed because it is the defensive branch.) Direction follows this module's stated rule: under-issuing leaves material on the shelf where an operator draws it manually and the job cost shows the gap, while over-issuing writes material into an as-built genealogy record that never contained it (AS9100D 8.5.2) — and unlike the old answer it is not silent. `no_demand_source` is **advisory** at work-order scope (a job with no BOM is the ordinary case for a turned part or a part-less nest package) and **blocking** only at part opt-in, so a refusal gate that cries wolf does not train people to ignore it. The dry-run preview reports the same refusals (`suppression_reason: "blocking_diagnostic"`) through `blocked_demand_refusal`, the pure half of the same decision, so preview and outcome cannot disagree. **This is a live behaviour change, not a records-only one** — but it is a change to code that has never run in production (no part is armed), which is precisely why it was taken here rather than later. (c) **Part-scope readiness cannot see routing conditions at all** (`operation_names_own_part`, `operations_disagree_on_component`, `routing_component_excluded_by_bom`, `routing_bom_quantity_disagreement` need a work order), so an *eligible* part can still resolve wrong demand on a specific job, visible only to whoever opens the preview and nothing requires anyone to open it. **This is the limit that makes the recorded `backflush_readiness: "clean"` assert less than it appears to — see item (3), where it is stated beside the value.** (d) **BOM-driven backflushed material still has no return verb** (`allocation_id IS NULL`, outside the PR 3 RETURN engine) — unchanged, and reachable in production for the first time. (e) The `_placeholder_stock_row` un-returnable dead-end, the `_drop_ledger_covered_parts` live-subquery blind spot, and the nest-re-import lock are untouched. (f) **An untied, non-opted-in work order is byte-identical to its pre-feature behaviour** — no ledger row, no audit row, no event; invariant 6(d) intact, with no read and no write added to that path. **(g) THE BOM UNIT-OF-MEASURE DEFAULT WAS CORRECTED ON THIS BRANCH, AND EXISTING RECORDS WERE DELIBERATELY NOT BACKFILLED (owner decision, 2026-07-27).** `schemas/bom.py` defaulted a BOM line's `unit_of_measure` to the literal `"each"`, which the `blocking` `unit_of_measure_mismatch` diagnostic then read as a **stated claim** — so on real sheet-metal data (components stocked in sheets / lbs / ft) the readiness gate refused nearly every part over a value no human ever entered. **The severity was NOT softened; the default was fixed** — a new line now inherits the component part's own unit (`api/endpoints/bom.py` → `_resolve_line_uom`, on all four BOM-line write paths). **Two things an assessor should take from this.** First, **what a stored `unit_of_measure` MEANS changed on 2026-07-27**: before it, `"each"` on a BOM line may be an authored value or a schema default and the record does not distinguish them; after it, `"each"` is either authored or inherited from a component that is itself `each`. Second, **nothing was rewritten** — this series is correct-forward, no migration touched `bom_items`, and legacy lines keep what they have. They are corrected by ordinary audited human edits (`PUT /bom/items/{id}`, or a fix to the component part), found via **`GET /bom/uom-mismatches`** — a pure read gated to ADMIN/MANAGER/SUPERVISOR that writes nothing (not even an audit row) and shares `models.part.uom_disagrees` with the gate so the worklist and the control cannot disagree. **Operationally this means arming a pre-2026-07-27 part is conditional on a human data-remediation pass.** ⚠️ **CORRECTION, entered when that pass got a UI (see the 2026-07-27 BOM Unit Mismatches row below): the first draft of this clause said "a human data-remediation pass that leaves its own audit trail — the corrections are on the tamper-evident chain, the report that prompted them is not." That is HALF TRUE and the false half is the load-bearing one.** Only the **part-side** remedy is audited: `PUT /parts/{id}` runs `AuditService.log_update` like any part edit. The **line-side** remedy is **not** — `POST /bom/{bom_id}/items`, `PUT /bom/items/{id}` and `DELETE /bom/items/{id}` take no `AuditService` and write **no `audit_log` row at all** (migration `008`'s triggers protect `audit_log` itself, not domain tables; only the two BOM *import* paths audit, with `extra_data.source="bom_import"`). Since fixing the line is the **expected** remedy — it is the narrower blast radius and the one the report's own guidance recommends first — the ordinary path through this remediation leaves **no attributable record of who changed a BOM line's stated unit, when, or from what**, on the very data the arming gate then reads as clean. The report that prompted the pass is likewise unrecorded (it is a pure read). What *is* on the chain is the arming decision itself and the readiness verdict behind it (item (3)), plus any completion-time refusal (item 8(b)). **BOM-line audit coverage is a pre-existing gap this feature makes load-bearing, not one it introduced; it is recorded here as an open finding rather than as a discharged control.** **(9) THE PR 4 SIGN-OFF CONDITION — PARTIALLY DISCHARGED, AND THE REMAINDER IS NOW LIVE.** The 2026-07-25 (PR 4) row required explicit owner/auditor acceptance of its item (4) residuals **BEFORE** the follow-up PR exposed the flag, naming (a) the divergent lot-selection policies and (b) the one-shot posture. Status, stated exactly: **(a) is discharged by being FIXED, not accepted** — PR 4.4 gave both engines one `received_date`-FIFO, consumable-aware, spilling policy, so the two-as-built-rules condition no longer exists. **(b) is NOT discharged.** PR 4.4 closed it *as arithmetic* only; the observable statement — *a later raise to `quantity_complete` never issues the increment* — is still true, and this PR is the one that makes it a production behaviour rather than a property of dark code. **Owner acceptance of (b) is therefore recorded here as OUTSTANDING at merge.** So are PR 4.4's own open items: the item (12) release-gate prod counts (`('work_order', ISSUE)` rows with non-NULL `reference_id`; `work_order_material_allocations WHERE work_order_operation_id IS NULL`; `parts WHERE backflush_components = true`; the NULL-`status` stock count), the held-lot as-built consequences (11)(d)/(e), and — most consequentially — **whether `chk_inventory_items_quantity_non_negative` is live in prod**, which `docs/MATERIAL_CONSUMPTION_PLAN.md` open question 4 says exposure should wait on. **It did not wait, and that deviation is recorded here rather than dropped.** What this PR offers against it is a **brake, not an answer**: the flag is default-off, no part can be armed without passing the readiness gate, and the dry run shows the draw — per component and per lot — before anything moves. **(10) COMPENSATING CONTROLS for the residuals in (4), for an assessor reading this row alone:** the flip is attributable (who / when / false→true / readiness verdict) on the tamper-evident chain; it is refused at the flip on any **BOM** condition the part-scope check can see (the routing half is not checked — see item (3)); it is refused again, per component, and recorded, at every completion where a blocking diagnostic stands (item 8(b)); it cannot be set on create, on import, or on any endpoint other than the two update doors; it is default-off so inaction is safe; and it is reversible at any time with no gate, so the remedy for a wrong flip is immediate and available to the same tier that made it. **What these controls do NOT compensate for** is the gap named in item (3): between the flip and the completion, nothing re-validates and nothing on the BOM/routing edit path is aware the part is armed. **Evidence and status.** Written from code reading of the working tree at branch HEAD. **No test run is cited in this row**, and the standing QA gates (test-engineer coverage of the gate, the four cannot-set paths, the list-vs-detail agreement, dry-run purity, and one case per blocking condition) are the merge gates, not this row. **Compliance-auditor review DID run on this branch and returned FAIL** on two gating findings — blocking diagnostics computed at completion and discarded (item 8(b)) and a cross-tenant part number rendered into a diagnostic served by the readiness GET (item (4)(g)) — plus a soft-deleted part that could be armed (item (4)(f)) and the split `resource_type` that broke the arming trail into two queries (item (3)). All four were fixed on this branch before merge and are described above in their corrected form; the row is the record of that, not of a clean first pass. Describes working-tree behaviour — re-verify if the branch is rebased or amended before presenting as assessment evidence. **Sign-off: NOT obtained.** Item (4)(a)–(e), item (9)'s residual (b) (**the one-shot posture — a post-completion rise in `quantity_complete` still issues nothing and still writes no record of the gap**), the item (3) one-time-gate exposure, and PR 4.4's items (11)–(12) all warrant explicit owner/assessor acceptance; **release-gate query 4 — is `chk_inventory_items_quantity_non_negative` live in prod? — is unanswered**, and item (9) records that exposure preceded rather than followed both the acceptance and the answer. See `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Exposing the flag (PR 4.5)" + Delivery row 4.5, `docs/API.md` → Parts → Part Schema and Work Orders → completion inventory effects, `docs/RBAC_PERMISSIONS.md` → Parts. | Claude |
| 2026-07-27 | AC-3.1.5 / AC-3.1.2 (**BOM Unit Mismatches screen — a UI over the existing pre-arming remediation worklist**, follow-up to PR 4.5): **Scope bound first, because it is narrow. This row adds NO endpoint, NO schema change, NO migration, NO write path and NO new data.** It is a **frontend-only** change: the report `GET /bom/uom-mismatches`, shipped API-only by the PR 4.5 row above, now has a screen at **`/bom/uom-mismatches`** (sidebar: Engineering → *BOM Unit Mismatches*). The compliance interest is **access control** and **whether the screen states what the data actually supports**. **(1) The gate is unchanged and is enforced in three places, one of which is the only one that counts.** The endpoint keeps `require_role([ADMIN, MANAGER, SUPERVISOR])` — untouched. The client adds two *advisory* gates on the same role set: the nav entry and the route both require **`boms:edit`** (`{platform_admin, admin, manager, supervisor}`), so an ineligible role sees no link and is refused by the route guard on a deep link. Client-side gating is **usability, not enforcement** (a client can always call the API directly); the server gate is the control, and it did not move. The route entry `{ prefix: '/bom/uom-mismatches', permission: 'boms:edit' }` beats the `/bom` → `boms:view` entry via longest-prefix matching — a child page gated *more* tightly than its parent, which is worth stating because the opposite (a child gated *less* tightly) would be a real defect. Nav gating required a new mechanism (`NavItem.permission`, filtered at item **and** group-child level, empty groups dropped); every pre-existing nav item declares no `permission` and is unaffected, so no role's visible navigation changed except by this one addition. **(2) It discloses no data the endpoint did not already return**, to no one the endpoint did not already serve — same tenant scoping, same fields, same rows, including the soft-deleted components the report deliberately does not filter. **(3) It is READ-ONLY, and that is the compliance-relevant design decision, not an incidental one.** There is no inline BOM-line editor: rows deep-link to `/bom?id={bom_id}` and to the assembly part. The reason is item (8)(g)'s correction above — **BOM-line create/update/delete write no `audit_log` rows at all** — so building the remediation editor *into* this screen would have made an un-audited endpoint the primary interface for a compliance-critical correction and increased the volume of unattributable BOM edits. Handing off leaves the corrections exactly where they already were. **This does not close the gap; it declines to widen it.** BOM-line audit coverage remains an open finding, and it is now load-bearing on the arming prerequisite. **(4) It preserves the two places the underlying data can be over-read, rather than smoothing them into a cleaner-looking UI.** `truncated` renders as an amber banner plus a **`≥ N`** count captioned *"Floor — scan ceiling hit"* — a paged worklist that displayed a truncated scan as a plain total would be an artifact asserting a completeness the server never claimed. `blocks_backflush` renders as **"Line effect"** (*Would be issued* / *Never issued*), never as "blocking your part", with a standing on-screen note that a line under a `make` sub-assembly reads *Would be issued* and refuses nothing when the parent is armed, and that an assembly filter does not follow nested BOMs — so the unfiltered list is the authoritative worklist and the authoritative **per-part** answer is `GET /parts/{id}/backflush-readiness`, linked from every row. **(5) Documented remediation sequence, now concrete:** run the report unfiltered → correct the lines on the BOM screen (or the component part's stocking unit, the wider blast radius) → re-check `GET /parts/{id}/backflush-readiness` → arm via `PUT /parts/{id}`. The readiness re-check is a required step, not a formality: the report answers lines, the readiness check answers the part and is the same function the arming gate runs. **Evidence and status.** Written from code reading of the working tree (`frontend/src/pages/BOMUomMismatches.tsx`, `App.tsx` route table, `components/Layout.tsx`, `utils/permissions.ts`, `backend/app/api/endpoints/bom.py`). **The change was NOT browser-verified** — the local dev database lags the model schema, so a live authenticated render was not performed; verification stopped at `type-check` / `lint` / `build`. **Nothing in the PR 4.5 row above is discharged by this one:** sign-off is still not obtained, open question 4 is still unanswered, and no production part has opted in. See `docs/API.md` → BOM → *Where this is worked*, `docs/RBAC_PERMISSIONS.md` → BOMs, `docs/MATERIAL_CONSUMPTION_PLAN.md` → *Exposing the flag (PR 4.5)*. | Claude |

---

*This document should be reviewed and updated monthly during remediation and quarterly after certification.*
