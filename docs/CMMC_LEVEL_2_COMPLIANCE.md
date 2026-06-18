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

**GAPS:**
- [ ] **CM-3.4.3 - Track Configuration Changes**
  - Need: Automated tracking of infrastructure changes
  - Effort: 1-2 weeks
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
- [ ] **IA-3.5.7 - Password Complexity** 🔴 CRITICAL
  - Need: Minimum 12 chars, uppercase, lowercase, numbers, special chars
  - Effort: 3-5 days
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
- [x] API rate limiting
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
| Password Policy Enforcement | 1 week | | ⬜ Not Started |
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
```
Backend (app/core/security.py):
- Minimum length: 12 characters
- Require: uppercase, lowercase, number, special char
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
| 2026-06-10 | AC-3.1.2 / IA / AU-3.3.1 (TV wallboard, A0.5, branch `feat/tv-wallboard`): added scoped display tokens for unattended shop TVs — `type="display"` JWTs that authenticate **only** the new zero-write `GET /shop-floor/wallboard` (401 everywhere else via `verify_token`'s type check, so they can never act as a user session); issuance/revocation ADMIN/MANAGER-gated, tenant-scoped, and tamper-evidently audit-logged; the `display_tokens` DB row is the revocation/expiry/tenant authority re-checked per request; raw JWT shown once at issuance, never stored; operator names truncated to "First L." for public screens. Additive mechanism — no existing compliance claim changed. See `docs/WALLBOARD.md` / `docs/API.md` / `docs/RBAC_PERMISSIONS.md` | Droid |
| 2026-06-18 | SC-3.13.1 (carrier-shipping egress kill switch): catalogued the **per-company, default-off** outbound-egress control `allow_carrier_egress` on `CompanyShippingProfile` (`company_shipping_profiles`, `nullable=False, default=False, server_default="false"`) — requires explicit human opt-in before any customer address/parcel data leaves the boundary for EasyPost; `ShippingService._require_egress` gates every outbound carrier call (validate/rate/buy-label/freight-BOL/pickup/void) and the tracking-poll job re-checks it (no provider call when OFF); flag flips are tamper-evidently audit-logged as a status change. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/SHIPPING_CARRIER_INTEGRATION.md` | Claude |
| 2026-06-18 | SC-3.13.1 (thermal-label print egress kill switch): catalogued the **per-company, default-off** outbound-egress control `allow_print_egress` on `CompanyPrintProfile` (`company_print_profiles`, `nullable=False, default=False, server_default="false"`) — requires explicit human opt-in before a rendered label (part number, lot/heat/serial, critical-characteristic marker) is transmitted to the pbxz.io ProxyBox cloud relay; both the request path (`PrintService._require_egress`) and the auto-print ARQ job (`app/jobs/label_jobs.py`) gate on it (no outbound call when OFF); flag flips are tamper-evidently audit-logged as a status change. Documentation-only — describes shipped behavior, no compliance claim changed. See `docs/THERMAL_LABEL_PRINTING.md` | Claude |

---

*This document should be reviewed and updated monthly during remediation and quarterly after certification.*
