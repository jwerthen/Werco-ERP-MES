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
  material-trail condition is attributable and recorded. *(See the negative-stock-on-shortage posture
  flagged for review in `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`, Batch 6 — a negative on-hand still
  completes the work order by design; this warrants explicit quality/compliance acceptance.)*
  **AS9100D 8.5.2 (identification & traceability):** because the finished-goods receipt assigns and
  records a work-order lot and the backflush carries the consumed component lots, **as-built lot
  genealogy is now reconstructable** from a single trace — `GET /traceability/lot/{lot}` reports the
  producing work order and its `consumed_components` (component part / lot / quantity), and
  `GET /traceability/serial/{serial}` mirrors the work-order/NCR collection. All trace queries are
  tenant-scoped. **DB-enforced idempotency** (migration `041`, two partial UNIQUE indexes) guarantees
  at most one receipt per work order and one issue per component, so a re-completion or reconcile
  re-read cannot duplicate a regulated inventory/traceability record.
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
  CUI-egress control) — the AI analogue of the carrier and print switches above, completing the set
  of three egress kill switches (`allow_carrier_egress` / `allow_print_egress` / `allow_ai_egress`).
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

---

*This document should be reviewed and updated monthly during remediation and quarterly after certification.*
