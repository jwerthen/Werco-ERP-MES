# ERP codebase analysis — September 13, 2026

**Recommendation: keep the existing stack and repair the security boundaries and planning defects before doing broad structural cleanup.** This codebase has substantial domain safeguards and regression coverage. The largest risks come from uneven enforcement across older endpoints and from shared state that outlives its tenant, user, or transaction.

Reviewed commit: **a4a085a31ae3783f5be7d26e182b91951ab67693**. This was a codebase review with isolated reproductions, fresh-install verification, dependency checks, and parallel backend, frontend, and security reviews. No production database, deployed configuration, customer records, or live user sessions were inspected. Findings describe the checked-out code, not evidence that exploitation or data loss has occurred in production. Application code was not changed.

## What was examined

| Area | Measured scope |
|---|---:|
| Backend application | 443 tracked Python files; 172,030 physical lines |
| Backend tests | 414 tracked Python files; 195,448 physical lines |
| API declaration inventory | 784 router decorators across 79 endpoint modules |
| Frontend production TypeScript | 422 files; 164,111 physical lines |
| Frontend test files | 438 files; 95,851 physical lines |
| Browser test directory | 14 TypeScript files, including fixtures |
| Marketing site | 14 source files; 1,786 physical lines |
| Workspace contamination | 236 untracked files, all byte-identical copies of existing files |

Counts include comments and blank lines. They measure size, not complexity or review coverage; not every line received manual inspection. Frontend production counts exclude test files and test utilities. The marketing site and infrastructure received structural/configuration inspection; this was not a visual UX audit.

## Fix first: access control and record integrity

### A1 — Critical: a tenant Admin can create a platform administrator

[auth.py:1337](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/auth.py:1337) accepts the broad UserCreate schema and assigns its requested role directly at line 1359. The ordinary user-creation route already refuses platform_admin, but this alternate route omits that restriction.

**Reproduced through the real application:** an ordinary tenant Admin was refused by POST /users/ for that role, accepted by POST /auth/register, and the newly created account could log in, read another company, and deactivate it.

**Fix:** use one user-provisioning service and tenant-safe role schema for all tenant creation paths. Platform principal creation must be exclusive to an explicitly authorized platform workflow. Add an HTTP regression test that exercises every account creation/import path with a tenant Admin and a requested platform role. Separately inspect existing platform principals when remediating; this audit did not inspect production accounts.

### A2 — Critical: a Viewer can delete another company's FAI evidence

[quality.py:838](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/quality.py:838) declares company_id but never uses it. Both characteristic and parent FAI lookups are unscoped; any authenticated user can physically delete the characteristic and change inspection totals without an audit entry.

**Reproduced:** a tenant-A Viewer deleted a tenant-B characteristic; the response was 200, the row was gone, the parent's totals changed, and audit count did not increase.

**Fix:** require the intended quality write permission, resolve the tenant-scoped parent before the child, scope both records, and record the action atomically. Define an amendment/void rule for retained quality evidence instead of silently removing completed inspection history. Test wrong-tenant IDs and Viewer access with assertions that no rows, totals, or audit state change on refusal.

### A3 — Critical: job-cost endpoints expose and alter other tenants' data

[Job-cost update:354](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/job_costing.py:354) looks up the record by ID alone. [Entry listing:402](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/job_costing.py:402) does not even receive tenant context. Related entry and variance-report operations share the problem.

**Reproduced:** a Viewer read another tenant's cost entries and variance report, changed its revenue, and deleted a cost entry through /api/v1/job-costs. This includes financial and customer information.

**Fix:** introduce a shared authorized, tenant-scoped JobCost resolver; apply it to detail, entries, variance, updates, creation, and deletion. Require explicit write roles and audit consequential changes in the same transaction. Test the entire route family, including nested records and response relationships.

### A4 — High: quality approval and controlled-document writes bypass intended roles

[FAI update:660](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/quality.py:660) allows any authenticated user to set PASSED and records that caller as approver. [Document upload:126](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/documents.py:126) similarly permits a Viewer to upload a work instruction which becomes released. Both were reproduced with no audit delta. These are existing policy/enforcement gaps; the FAI prefill comment explicitly acknowledges the broad historical role posture.

**Fix:** establish explicit author, release, and approve capabilities and enforce them at the API. Keep upload and revision behavior deliberate for each document type. Write audit rows for approvals, controlled-document release, and revision replacement. [Platform company updates:102](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/platform.py:102) also commit company activation/deactivation without audit; the escalation reproduction confirmed that omission.

### A5 — High: WebSocket authentication does not enforce the HTTP identity rules

[websocket.py:19](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/websocket.py:19) accepts a verified token's user/company claims and normally skips loading the current user. It therefore misses active-account and credential-scope checks enforced by HTTP dependencies.

**Reproduced:** disabled-user and kiosk-scoped credentials connected to /ws/updates while corresponding HTTP access was refused. Broadcasts have tenant grouping, which is a useful protection; the defect is admission and continued authorization, not proof every broadcast crosses tenants.

**Fix:** share identity resolution between HTTP and WebSockets, enforce active user/company and allowed token scope, validate requested resource ownership, and define revalidation/disconnection for revoked or expired sessions.

**Systemic improvement for A1–A5:** add an endpoint authorization inventory with an explicit policy for public, tenant, and platform routes. Generate parameterized HTTP tests for Viewer writes, wrong-tenant parents/children, disabled accounts, and scoped credentials. Declaring a dependency is insufficient: the test must exercise real lookups and assert absence of side effects. Extend the existing tenancy tests instead of creating a parallel security framework.

## Fix next: shared state and ERP correctness

### A6 — High: refresh, request replay, and dashboard caching cross session boundaries

[api.ts:473](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/services/api.ts:473) commits refresh results without checking that the initiating session still exists. [Retry handling:431](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/services/api.ts:431) retries an old request using the current token. [Dashboard cache:518](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/services/api.ts:518) keys only by URL/parameters, accepts late completions after clearing, and returns stale data for all errors, including 403.

A deterministic controlled-transport harness against the actual TypeScript client reproduced:

- Two concurrent requests initiating two refreshes.
- An old refresh restoring credentials after logout or overwriting a newer login.
- A mutation started under company A retrying with company B's credential.
- An A dashboard response repopulating the cleared cache, then appearing as stale data when B's request fails.
- Cached data being returned after a 403.

The active cached production caller is the dashboard, not every API method. Server authorization still applies to retried requests; the reproduction establishes changed identity, not success of every retried write.

[Company switching:69](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/context/CompanyContext.tsx:69) aggravates this by changing credentials before a fallible company-info request. The usual successful switch reloads the page. If the follow-up company-info request fails after credentials change, the previous page stays mounted with new credentials.

**Fix:** introduce an immutable session generation covering user and active company, one refresh promise per generation, cancellation/rejection of obsolete requests, and cache keys and completions bound to that generation. Never retry mutations across identities. Restrict stale fallback to same-identity transient failures. Suspend tenant-scoped forms during switching and expose a coherent recovery state on failure. Reuse this transport for Copilot's separate refresh path.

### A7 — High: MRP counts earlier shortages again on later demand dates

[MRP calculation:335](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_service.py:335) emits a supply action but [never credits that planned receipt:379](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_service.py:379) into projected availability.

**Reproduced:** zero inventory and two dates requiring five units each produce recommendations of five and ten units: **15 recommended for 10 required**. Auto-processing copies those quantities into draft supply documents. This is not a claim that purchase orders are automatically issued.

**Fix:** make the date-based inventory recurrence account for generated planned receipts and safety stock. Test multiple demand dates, partial stock, minimum quantities, safety stock, and aggregate AUTO_DRAFT output. Also improve date-phased supply: current on-order totals collapse supply dates and can make late incoming stock appear available for earlier demand.

### A8 — High: one tenant's database failure breaks later tenants' scheduled runs

[MRP jobs:33](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/jobs/mrp_jobs.py:33) and [scheduling jobs:34](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/jobs/scheduling_jobs.py:34) reuse one Session across companies, catch exceptions, and continue without rollback.

**Reproduced with a real constraint failure:** tenant 1 raised IntegrityError; tenant 2 then raised PendingRollbackError. Both wrappers returned a success-shaped empty result.

**Fix:** own a fresh Session and transaction per tenant. Isolate individual actions with savepoints where partial success is intended. Return structured failures and make total failure visible to job monitoring. Test that the second tenant succeeds after the first fails and that no pending writes leak between iterations.

### A9 — Medium: manual FAI characteristic creation fails

[quality.py:755](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/quality.py:755) constructs a tenant-owned FAICharacteristic without company_id.

**Reproduced:** adding a characteristic returned 500 with a NOT NULL violation. Set company_id from the authorized parent/context and test the ordinary create/read/update path using persisted rows. Include this in the quality-route repair, not a standalone workaround.

### A10 — Medium: optional costing failure can abort mandatory completion

[completion_cost_service.py:349](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/completion_cost_service.py:349) catches errors from optional costing performed in the caller's transaction without savepoint isolation.

**Reproduced:** an injected constraint failure was swallowed and the helper returned None, but the caller's Session remained unusable. The feature flag defaults OFF, so this is an opt-in-path defect.

**Fix:** flush mandatory completion changes, isolate optional cost changes in a nested transaction, and roll back only that savepoint on failure. Test both a failed flush and a Python exception after old generated costs have been removed; previous costs and completion must remain valid.

### A11 — Medium: MRP numbering uses a tenant lock for a global unique number

[MRP model:35](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/models/mrp.py:35) makes run_number globally unique; [allocation:37](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_service.py:37) searches globally, while [the lock:396](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_service.py:396) is per company.

Two concurrent companies can select the same next number. This is a code-backed race, not a PostgreSQL concurrency reproduction.

**Fix:** use a brief global allocator/sequence, or make both uniqueness and allocation tenant-scoped through a migration. Keep long planning work outside any global lock. Validate with two independent PostgreSQL connections.

### A12 — Medium: frontend permissions change incorrectly between login and reload

[AuthContext.tsx:208](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/context/AuthContext.tsx:208) asks an [admin-only endpoint:1008](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/admin_settings.py:1008) for permissions on every login. Non-admin roles cannot load tenant overrides. Successful admin loading [replaces the frontend permission map:229](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/utils/permissions.ts:229) with backend defaults that omit five frontend capabilities.

**Reproduced:** renumber, combine inventory, process-sheet author/release, and visitor-log viewing permissions changed from true to false for an ordinary Admin. Restore does not reload overrides; logout does not reset the module global.

**Fix:** expose effective current-user capabilities through an authenticated endpoint and keep them in reactive auth/company state. Test login, restore, logout, and company switching. This finding concerns UI correctness; it is not evidence that the frontend can override server authorization.

## Code cleanup with the highest return

| Improvement | Why it matters and what to do |
|---|---|
| Restore one clean development baseline | All 236 original untracked copies were byte-identical. Copied test names such as .test 2.tsx evade normal test exclusions and entered the application type-check. Preserve a backup, then remove confirmed duplicates and diagnose the copying/sync source. Reinstall dependencies from manifests. No originals or copies were removed during this audit. |
| Make static checks effective | [mypy.ini:11](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/mypy.ini:11) suppresses 18 error categories, including argument types, missing attributes, call signatures, return types, and union access. A typed wrong-return/wrong-argument probe passed; enabling two categories caught both defects. [eslint.config.js:22](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/eslint.config.js:22) registers React Hooks but enables neither rules-of-hooks nor exhaustive-deps; a conditional Hook passed current lint. Enable Hook correctness first and ratchet typing by module instead of turning every suppression off at once. |
| Extract business logic from HTTP modules | Backend work_orders.py is 5,848 lines; shop_floor.py is 5,915. [Costing:307](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/completion_cost_service.py:307) and [MRP supply:98](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_supply_service.py:98) import BOM behavior upward from a route module. Extract shared BOM resolution and draft-work-order creation services first. Preserve existing completion/state/inventory seams and intentional office/operator differences. |
| Split frontend transport from domain APIs | [api.ts](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/services/api.ts) is 5,732 lines. Separate session-aware transport, typed domain DTO/client modules, and page controllers. Then extract dialogs and tables as their behavior is touched. WorkOrderDetail is 3,731 lines, Purchasing 2,890, Receiving 2,855. File size alone is not the reason; coupled fetching, forms, permissions and mutations make changes hard to validate. |
| Replace full-list loading with real paging | [Inventory loading:214](/Users/jonwerthen/Documents/Werco-ERP-MES/frontend/src/pages/Inventory.tsx:214) waits for multiple full-list reads and sequential all-parts hydration before client pagination. [Backend summary:567](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/api/endpoints/inventory.py:567) aggregates after a 10,000-lot cap; totals can become silently partial. Purchasing has a 5,000-row cap without usable frontend continuation. Aggregate over the full authorized scope in SQL and page detail rows separately with total/has_next metadata. Current tenant volumes were not measured. |
| Remove repeated planning work | [Purchase batch review:25](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_purchase_batch_service.py:25) allows 25 actions, each triggering a full tenant demand/BOM traversal through [supply review:87](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/app/services/mrp_supply_service.py:87). Build one snapshot per tenant/horizon/transaction, batch-fetch BOM inputs, and preserve revalidation under the write lock. Add query-count and realistic-volume checks. |
| Isolate blocking worker work | Async MRP/scheduling/OEE jobs perform synchronous ORM/planning work on the shared ARQ event loop. Run a synchronous unit of work in an appropriate thread/process with its own Session, or isolate planning queues. Measure notification delay and heartbeat behavior during a large planning run before tuning concurrency. |
| Simplify dependency and operational ownership | Frontend has a lockfile; Python combines exact pins with open ranges and duplicates Redis constraints. Separate runtime/dev inputs, lock the resolved Python tree, and remove obsolete package/config entries only after usage checks. The marketing site has no build/audit job in the reviewed workflows. Make its basic checks explicit. Keep short current conventions in CLAUDE.md and move lengthy historical rationale into linked decision records. |

Production frontend build output contains a 572 KB minified entry chunk (161 KB gzip) and a 706 KB nesting chunk (211 KB gzip). These are prioritization inputs, not measured user latency or proof that every route loads nesting. Preserve existing lazy routes; profile the entry path and remove accidental eager dependencies before doing broad bundle surgery.

## Deployment and dependency follow-ups

**High — protect local Docker build contexts.** The backend Docker ignore file omits .env and .venv311 while the API Dockerfiles copy the entire context. A backend/.env file exists locally; its contents were not read. Check both API and repo-root worker contexts and exclude local secrets, credentials, database files, and environments before building. [Docker applies its own context exclusions](https://docs.docker.com/build/concepts/context/#dockerignore-files), independently of Git ignore rules. The worker context excludes .venv311 but still includes .env; API contexts include both. [The pinned Railway CLI](https://github.com/railwayapp/cli/blob/v5.41.2/src/controllers/upload.rs) honors Git ignore rules before uploading, so this is not evidence that a deployed Railway image contains credentials.

**High — update pypdf with parser regression verification.** [requirements.txt:60](/Users/jonwerthen/Documents/Werco-ERP-MES/backend/requirements.txt:60) pins 6.14.2. Auditing the fresh resolved environment found fixable pypdf advisories, with duplicate scanner rows; do not equate the raw row count to independent vulnerabilities. The upstream [XForm extraction advisory](https://github.com/py-pdf/pypdf/security/advisories/GHSA-763m-79hh-57f2) identifies excessive runtime/memory consumption and a fix in 6.16.1. Upgrade to a verified patched version and rerun PDF import, drawing, quote, and nesting parsing tests. No malicious PDF was run.

**Medium — separate evictable cache from durable job state.** [docker-compose.prod.yml:20](/Users/jonwerthen/Documents/Werco-ERP-MES/docker-compose.prod.yml:20) shares Redis between queue/cache consumers and sets a 256 MB allkeys-lru limit. Under pressure, that policy can evict queue data as well as cached responses. Use a dedicated noeviction job store with memory alerts and an independently bounded cache. Redis documents the [policy behavior](https://redis.io/docs/latest/develop/reference/eviction/). Live Railway Redis configuration was not inspected, so the finding applies to the supplied production Compose configuration.

**Keep PostgreSQL validation targeted and explicit.** SQLite unit tests are a documented owner decision. Retain them. The separate [E2E workflow:102](/Users/jonwerthen/Documents/Werco-ERP-MES/.github/workflows/e2e.yml:102) already runs dedicated PostgreSQL verification, including recent operational/nesting migrations. Extend that approach for allocator concurrency, tenant constraints, audit atomicity, and full supported migration upgrade paths. E2E is deliberately non-blocking; review promotion of a small reliable critical-path subset once it is stable. Do not present this intentional choice as accidental drift.

## Verification results and limitations

The original workspace failed frontend build/type-check because installed packages were stale/missing and duplicate files entered compilation. Both existing Python environments were stale as well: FastAPI 0.128.4 and pytest 9.0.2 versus the current manifest pins.

A temporary copy containing only tracked files was created. Frontend dependencies were installed with npm ci; Python runtime and development requirements were installed into a new isolated environment. The original dependency directories were left unchanged.

| Check | Result |
|---|---|
| Frontend production build | Passed |
| All three TypeScript programs | Passed |
| Frontend ESLint, zero warnings | Passed under current rule configuration |
| Frontend Jest with coverage | 438 suites, 4,303 tests passed |
| Frontend coverage | Statements 70.37%; branches 61.44%; functions 56.28%; lines 71.04% |
| Backend Black / isort / Flake8 | Passed |
| Backend mypy | Passed for 442 source files under the current suppressed configuration |
| Backend Bandit at the CI threshold | Passed; no reported issues at that threshold |
| Backend full pytest with coverage | 8,328 passed, 20 skipped; 7 temporary-checkout guard failures; 86.74% coverage |
| Backend affected guard rerun | All 18 checks passed after adding missing Git metadata; resolves all 7 failures |
| Frontend dependency policy audit | Passed with two allowlisted findings and one stale allowlist entry |
| Python dependency audit | Found fixable pypdf advisories in the freshly resolved environment |
| Isolated security/domain/client probes | Reproduced the defects described above |
| Browser E2E / PostgreSQL / live performance | Not run locally in this audit |

The manifest-resolving pip-audit command hit a local ensurepip subprocess crash. The fallback scanned the freshly installed environment's site-packages directly, not an unrelated ambient environment. The Python requirement ranges mean this is today's resolved tree, not proof of the exact deployed tree.

The backend full run took 657 seconds. Its seven failures all came from one guard that uses git grep: the initial temporary source copy lacked a .git directory. Initializing Git metadata in that temporary copy and rerunning the entire affected file produced 18 passes. No application source change was needed. Thus 8,335 unique backend tests passed across the full run and corrective rerun, with 20 skipped; a second complete suite run was not performed.

The probes demonstrate current failure behavior; they are not tests of implemented fixes. Passing regression suites do not invalidate those reproductions. No claim is made that this review found every security or correctness issue.

## Proposed implementation sequence

1. **Access-control repair:** A1–A5 and A9, beginning with cross-tenant routes and platform-role provisioning. Add role/tenant HTTP regression coverage and atomic audit writes. Verify blocked requests change nothing.
2. **Session-boundary repair:** A6 and A12. Introduce scoped transport and permission state, then test delayed responses, refresh failure, logout/new login, and failed company switches.
3. **Planning and transaction repair:** A7, A8, A10 and A11. Preserve draft/review semantics, validate exact supply quantities and per-tenant failure recovery, and run real PostgreSQL allocator tests.
4. **Build and dependency hygiene:** remove backed-up duplicate copies, fix Docker exclusions, update pypdf, establish reproducible installs, and enable the missing correctness gates.
5. **Incremental structural/performance cleanup:** extract BOM/draft-work-order commands and typed domain clients, replace capped aggregates and full-list hydration, share planning snapshots, and isolate blocking worker work.

Each batch should be a small set of reviewable changes with its own behavioral tests. Keep the existing React/FastAPI/PostgreSQL architecture, audit chain, tenant-scoped services, immutable revisions, and completion inventory safeguards. A broad rewrite would defer the concrete defects and make these established behaviors harder to preserve.

Evidence scripts and measurements are saved in [the evidence directory](/Users/jonwerthen/Documents/Werco-ERP-MES/docs/codebase-audit-2026-09-13/evidence/README.md). They use synthetic in-memory data and controlled fake transports. The security script explicitly sets an in-memory SQLite URL and checks the database dialect. Run only in a test environment; read each script's setup first.

