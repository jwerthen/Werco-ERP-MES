# Werco ERP - Deployment Runbook

**Version**: 1.0.0  
**Last Updated**: 2026-01-09  
**Environment**: Railway (Primary), Self-hosted (Optional)

> **Scope.** This runbook covers the two long-standing services, `werco-api` and
> `werco-frontend`. It does **not** cover the ARQ background worker (`werco-worker`), which
> now uses the gated worker steps in the main CI/CD pipeline. Its original cutover —
> pre-flight checks, per-cron effects, staged rollout, and rollback — is
> [`WORKER_DEPLOYMENT_RUNBOOK.md`](WORKER_DEPLOYMENT_RUNBOOK.md). Two differences matter if
> you ever deploy the worker by hand: it is deployed from the **repo root**, not `backend/`,
> and it has **no healthcheck** (it serves no HTTP).

> **Vercel frontend.** The ERP at `wercomfg.app` deploys to its existing Vercel
> project; Material Nesting's `/nest` route is included in the updated frontend
> build. This runbook's Railway
> frontend commands and release checks apply to `werco-frontend` on Railway;
> they do not establish that a build is serving on `wercomfg.app`. Use
> [DEPLOYMENT.md → Existing Vercel frontend](DEPLOYMENT.md#existing-vercel-frontend-material-nesting)
> for the native nesting feature's deployment scope and checks.

> **Saved nesting calculations.** The normal combined release must migrate the API
> through 102 before uploading the worker. CI builds the separate worker image, tests
> its fixed Node executable without network access, and preserves the bundle manifest.
> Before Vercel promotion, worker-touched releases require an active Railway SUCCESS
> deployment and a heartbeat no older than 90 seconds from that exact deployment, published
> after a successful Redis write, matching the release/manifest/Node identity. The gate
> rechecks active state. Failure blocks promotion; do not replace it with a manual upload
> or an old log check. Roll back compatible application images while retaining additive
> draft/run tables and immutable history. See [WORKER_SERVICE.md](WORKER_SERVICE.md#saved-nesting-calculation-runtime).

> **Spacing policies.** Apply additive migration 103 after 102 through the normal API
> boot migration path. It creates no seed/approved policy and introduces no secret,
> environment or cron setting. The API, frontend and compiled shared worker must all
> understand project 10/quote 9, so retain the existing exact-release API and fresh worker
> identity gates before public promotion. Validate policy approval/withdrawal and a new
> saved custom-spacing revision without changing historical reports. Application rollback
> retains policy/draft/run history and restores compatible images; do not downgrade 103
> or rewrite saved format versions as an ordinary rollback.

> **Piece observations.** Apply additive `104_stock_piece_observations` after
> `103_nesting_spacing_policies` in the API before frontend promotion. The PostgreSQL
> operations gate must verify both Alembic and model-bootstrap guards, RLS and table/
> sequence privilege revocation, plus real API tenant, idempotency and version races.
> Verify **Warehouse → Inventory → Piece observations**: explicit record, revision
> history and withdrawal with existing inventory permissions. This is an advisory
> register; it changes no inventory balance, availability, reservation, nesting input,
> certification eligibility or quote credit. There are no seeds, new secrets,
> dependencies, environment settings, crons or solver/profile changes. Keep the existing
> exact-release/runtime gates. On application rollback retain migration104, piece and
> observation records and audit history; do not invoke its destructive downgrade.

> **Original DXFs.** Apply additive `105_nesting_cad_sources` after 104 before
> releasing the saved-revision attachment UI. Verify PostgreSQL bootstrap/migration
> immutability and tenant guards plus the actual JWT/file-storage race gate. Run an
> explicit synthetic attachment, lost-response recovery and authenticated byte-exact
> download. Existing storage configuration is reused; verify provider durability
> separately. No worker/cron/profile changes or source backfill occur. Keep intent,
> attempt, receipt and binding history on application rollback; no object purge or
> destructive schema downgrade is part of ordinary rollback. See `CAD_SOURCE_STORAGE.md`.

> **Stock exclusions.** This increment adds no migration, permission, environment or
> cron change. Release the quote 11/project 12 API/frontend contract with the compiled
> `werco-contour-v5` worker through the same backend-first, exact-image/readiness gate.
> Legacy job 13 is local-only. Verify a saved exclusion-bearing scenario and its v2
> leftover ledger; absent/empty regions retain v1. These scenario constraints do not
> establish physical inventory or reserve stock. On rollback retain the original
> input versions, exclusion geometry and historical results; never remove constraints
> to make an older runtime accept them.

> **Compensated envelopes.** Release solver v6, quote 14/project 15 and the exact
> `werco-compensated-v1` source profile as a compatible API/frontend/worker set.
> CI checks the generated frontend adapter against normative backend JSON; isolated
> frontend/Node builds also self-check its digest. The manifest and hello bind
> `geometry_profile`; heartbeat fields and the active/fresh identity gate are unchanged.
> There is no migration, environment or cron change. Verify explicit legacy-input
> upgrade, v3 ledger evidence and frozen v4/v5 historical preview without rerunning it.
> Retain profile-bearing files/checkpoints on rollback; do not rewrite versions or
> remove constraints to force compatibility. Local job 16 remains local-only.

---

**Hank task storage.** Deploy additive `108_hank_tasks` after 107 in the API before
promoting the task-action frontend. The table starts empty; no production record
or task is seeded. Verify RLS and table/sequence revokes from `PUBLIC`, `anon`, and
`authenticated` on PostgreSQL, as well as the model-bootstrap equivalents. No new
secret or environment setting is introduced. Keep previews/receipts on ordinary
application rollback: the schema downgrade drops task history without undoing
completed ERP actions. See [Hank task storage](DEPLOYMENT.md#hank-task-storage).

**Hank follow-ups.** Reuse migration108 and release a matching API/worker/frontend
set. Scheduled checks require changing the worker's existing production
`WORKER_CRON_JOBS=none` to **`check_hank_watches_job`**, preserving every other disabled
cron. No new variable or secret is introduced. Verify exact active release, fresh
generic heartbeat, the one selected five-minute cron, and **13 of 14** suppressed
crons. Verify one opted-in watch's check timestamp and owner-only in-app outcome;
heartbeat health alone does not prove checks ran. **Check now** works independently
of scheduling. Restore `none` to suspend scheduled checks while retaining evidence.
See [Hank follow-up worker](DEPLOYMENT.md#hank-follow-up-worker).

**Hank personal preferences.** Apply additive **109_hank_preferences** after 108
before releasing the matching API/worker/frontend set. It seeds no rows and adds
no environment variable or cron; retain the Hank-only schedule. Verify save/reset,
current-company scoping, stale-version rejection and muted follow-up receipts.
Retain preference storage on application rollback. A deliberate 109 downgrade drops
only personal choices, preserving tasks/watch history. Suspend scheduled checks
before reverting to a worker that ignores saved alert muting, or retain a compatible
release. See [Hank personal preferences](DEPLOYMENT.md#hank-personal-preferences).

**Hank workflow expansion.** Apply **110_hank_workflows** after 109 before the
matching API/worker/frontend release. The empty intake, handoff and routine tables
retain the same RLS/revoke protections and seed no approved work. Verify shared
durable storage/Redis and registration of request-driven `process_hank_intake_file_job`;
no new cron or environment variable is needed, and the existing Hank-only cron
allowlist stays unchanged. Test upload/review/file recovery and participant/routine
access; retain tables and file evidence on application rollback. See
[Hank workflows and document intake](DEPLOYMENT.md#hank-workflows-and-document-intake).

**Consumables checkout.** Apply additive **112_consumable_checkouts** after
**111_continuous_improvement** before serving the matching API and frontend.
It creates the empty employee-checkout register, request/ledger uniqueness,
history indexes, deny-by-default PostgreSQL RLS, public/client table and sequence
revokes, and immutable UPDATE/DELETE guards. Matching model DDL protects
`create_all` bootstraps. There are no new environment variables or worker jobs,
and no automatic item, employee, opening-balance or paper-history import.

Verify **Warehouse → Consumables** in a test company: issue to an active employee,
confirm stock/ledger/history agree, replay the same request without another
deduction, refuse unavailable/insufficient stock, and check read-only access plus
Admin/Manager CSV export and its audit. Confirm Central date filters and that
checkout audit failure rolls back all effects. The migration suite covers a
disposable SQLite bootstrap upgrade/downgrade/reupgrade and compiles PostgreSQL
security DDL; it does not substitute for deployment verification on PostgreSQL.

Retain the additive table when rolling back application code. A deliberate
migration downgrade removes employee checkout history and retry receipts, while
existing inventory movements remain; preserve that history before considering a
production downgrade. See [Consumables setup and operation](CONSUMABLES.md) for
the attendant workflow. A code change alone does not deploy or initialize this
feature in production.

**Data API revoke parity.** **113_data_api_revoke_parity** applies after **112** through
the normal API boot migration path, with no deploy ordering and no matching
frontend/worker release. It is a catalog-only ACL change: it revokes
`mrp_supply_links`, `user_workspace_records` and their id sequences from `PUBLIC`,
and from `anon`/`authenticated` where those roles exist. No rows are read or written
and no table is rewritten; the app connects as the owning role, which the revoke does
not affect. It is very likely a no-op in prod for `FROM PUBLIC`. Its downgrade is a
deliberate no-op that never re-grants. Applied revision `093` also gained role-existence
checks in place; a database that already ran it is unaffected. See
[SUPABASE_SECURITY.md → Data API revoke parity](SUPABASE_SECURITY.md#data-api-revoke-parity-migration-113).

**Work-order dispatch plan (Dispatch Board v2).** Apply additive **114_wo_dispatch_rank**
after **113** through the normal API boot migration path (`start.sh` / `Dockerfile.prod` run
`alembic upgrade head` before the server starts). It adds the nullable
`work_orders.dispatch_rank` (no default, no backfill — metadata-only on PostgreSQL) and two
indexes, `ix_work_orders_company_dispatch_rank` and
`ix_operational_events_company_entity_time`. It creates no table and no RLS, revoke or role
statement, writes no row, and adds no environment variable, secret, cron or worker job.

- **The migration ships with the code, never after it.** The new API reads and writes
  `dispatch_rank`, so it must never serve against an un-migrated database. The old API never
  touches the column, so the migration may land ahead of it. The boot path already gives this
  order: the new image migrates before it serves. The worker loads the same models, so deploy it
  after the API, which is the order CI already uses.
- **What the migration locks, and for how long.** Alembic runs every pending revision in one
  transaction, and PostgreSQL holds each lock until that transaction commits. 114 splits its work
  around a commit so the expensive step locks nothing:
  - Inside the transaction, `ADD COLUMN` takes an ACCESS EXCLUSIVE lock on `work_orders`. That
    lock blocks reads as well as writes, but it covers only the `ADD COLUMN` and the small
    `ix_work_orders_company_dispatch_rank` build, both quick. The migration first runs
    `SET LOCAL lock_timeout = '5s'`. If a long transaction is holding `work_orders`, the
    migration fails after 5 seconds instead of making every `work_orders` query queue behind it.
    The new container then fails to start, the old deployment keeps serving, and redeploying is
    safe.
  - The migration then commits, which releases the `work_orders` lock. It builds
    `ix_operational_events_company_entity_time` with `CREATE INDEX CONCURRENTLY IF NOT EXISTS`,
    outside any transaction. A concurrent build blocks neither reads nor operational-event writes,
    however large the table has grown. It does wait for transactions that were already running
    to finish, so a long-running transaction delays the boot rather than the floor.
  - An interrupted concurrent build (a failed statement, or the boot killed by its health-check
    timeout) leaves an INVALID index. 114 is recorded as applied only after the build succeeds, so
    the next boot runs it again: the column and the `work_orders` index are already there and are
    skipped, and the migration checks `pg_index.indisvalid`, drops the INVALID index concurrently
    and builds it again.
  - Building the events index out of band first is no longer needed, even mid-shift. It is still
    harmless, because the migration skips a valid index:

    ```sql
    -- optional; autocommit: CONCURRENTLY cannot run inside a transaction block
    CREATE INDEX CONCURRENTLY ix_operational_events_company_entity_time
        ON operational_events (company_id, entity_type, entity_id, occurred_at);
    ```
- **Old and new versions overlap briefly.** The Vercel frontend can reach the old API for a short
  time after merge. During that window:
  - the Dispatch Board's Work orders view shows a retryable error, because the old API returns 404
    for it (the Machines view keeps working);
  - floor screens show no dispatch strip, because the notice fields are absent;
  - the old run-order schema ignores the new optional `expected_operation_ids`.
- **Verify after deploy:**
  - In a test company, add a released work order to the plan on `/dispatch`.
  - Confirm its jobs move to the front of each machine queue on the kiosk and in the Machines view.
  - Reorder the plan. A kiosk at an affected machine should show the RUN NEXT strip within one
    poll.
  - Check that the Audit Log shows one `dispatch_board` row for the save.
- **Rollback.** On an ordinary application rollback, keep the column and both indexes. Older code
  ignores `dispatch_rank`, so the queues fall back to the pre-plan order. Two things carry over:
  - Operations ordered inside a planned work order keep their `run_order` values, and the old sort
    reads those as machine ranks.
  - Redeploying the feature restores the stored plan. Clear the plan on the board first if it is no
    longer wanted.

  Downgrading 114 drops the column and both indexes and discards the plan. That is advisory data;
  the audit rows recording each change survive. The downgrade is not part of an ordinary rollback.
  See [API.md → Work-order dispatch plan](API.md).

**Customer name and code unique per company.** Apply **115_customers_unique_per_company** after
**114** through the normal API boot migration path. It replaces the global unique indexes
`ix_customers_name` / `ix_customers_code` (built by `create_all`; no earlier migration touched this
table) with plain indexes of the same names, and adds `uq_customers_company_name` /
`uq_customers_company_code` on `(company_id, name)` / `(company_id, code)`. It also drops any named
single-column unique constraint on either column if one exists. It creates no table and no RLS,
revoke or role statement, writes no row, and adds no environment variable, secret, cron or worker
job.

- **The upgrade cannot fail on data.** The new keys are strictly looser than the global ones, so
  every existing row satisfies them. No pre-flight dedup is needed.
- **Locks.** Adding each constraint takes an ACCESS EXCLUSIVE lock on `customers` (a small table,
  read by every customer picker). The migration first runs `SET LOCAL lock_timeout = '5s'`, so a
  long transaction holding `customers` fails the boot after 5 seconds instead of queueing every
  picker read behind it. The old deployment keeps serving and redeploying is safe.
- **Order.** Safe before or after the application: old code never relied on cross-company
  uniqueness. The boot path migrates before the new API serves.
- **Shipped with the same release (no migration involved):** customer edit is now Admin / Manager
  only, and customer create is Admin / Manager / Supervisor (the work-order create roles, so New
  Work Order's inline customer create keeps working for Supervisors). The Customers page still
  shows Add / Edit to every role, so tell Operator, Quality, Shipping and Viewer users that saving
  a new customer now fails with `Insufficient permissions`, and Supervisors that editing an
  existing customer does. See [RBAC → Customers](RBAC_PERMISSIONS.md#customers).
- **Verify after deploy:**
  - `SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'customers';` shows
    `ix_customers_name` and `ix_customers_code` without `UNIQUE`, plus the two `uq_customers_company_*`
    indexes.
  - As an Admin in a test company, create a customer with a supplied code that the company already
    uses. Expect **400** `Customer code already exists`, and confirm a successful create leaves one
    `customer` `CREATE` row in the Audit Log.
- **Rollback.** On an ordinary application rollback, keep the per-company constraints; older code
  works against them. The downgrade restores the global unique indexes only while no two customer
  rows share a name or code. Once two companies hold the same name or code it raises before any DDL,
  reporting how many values collide (never the values themselves). Resolving those collisions is an
  operator data decision; the migration will not pick which company's customer to rename.
  See [API.md → Customers](API.md#customers).

**Business numbers and codes unique per company.** Apply **116_business_keys_per_company** after
**115** through the normal API boot migration path. It re-keys eight columns from global to
per-company uniqueness: `ncrs.ncr_number`, `cars.car_number`, `fais.fai_number`,
`customer_complaints.complaint_number`, `return_material_authorizations.rma_number`,
`equipment.equipment_id`, `tools.tool_id` and `downtime_reason_codes.code`. For each it replaces the
global unique index `ix_<table>_<column>` with a plain index of the same name, and adds
`uq_<table>_company_<column>` on `(company_id, <column>)`. It also drops any named single-column
unique constraint on the column if one exists. It creates no table and no RLS, revoke or role
statement, writes no row, and adds no environment variable, secret, cron or worker job.

- **The upgrade cannot fail on data.** Each new key is strictly looser than the global one it
  replaces, so every existing row satisfies it. No pre-flight dedup is needed.
- **Locks.** Adding each constraint and dropping each index takes an ACCESS EXCLUSIVE lock on that
  table. The eight tables are small (tens to hundreds of rows), but the boot runs every pending
  revision in one transaction, so all eight locks are held until it commits. The migration first
  runs `SET LOCAL lock_timeout = '5s'`, so a long transaction holding any of the eight fails the
  boot after 5 seconds instead of queueing every NCR, FAI, gauge and tool read behind it. The old
  deployment keeps serving and redeploying is safe.
- **Order.** Safe to apply before the application: old code never relied on cross-company
  uniqueness. The new complaint and RMA numbering does rely on it. Against the old global indexes a
  company's next number can be one another company already holds, and that create returns **500**.
  The boot path migrates before the new API serves, so this matters only if migrations are run by
  hand.
- **What changes.** NCR, CAR and FAI numbers (`NCR-` / `CAR-` / `FAI-YYYYMMDD-NNN`) were already
  allocated per company, so a second company's first record of the day drew a number another
  company held and failed with **500** on every NCR, CAR and FAI writer. That stops. Equipment IDs,
  tool IDs and downtime reason codes are user-entered: a same-company duplicate is still **400**
  (`Equipment ID already exists`, `Tool ID already exists`, `Reason code already exists`), and
  another company's identical value is now accepted instead of refused.
- **Shipped with the same release (no migration involved):** complaint (`CC-YYYY-NNNN`) and RMA
  (`RMA-YYYY-NNNN`) numbers are now one sequence per company, allocated under a per-company
  advisory lock, so two companies may both hold `CC-2026-0001`. Existing numbers are not
  renumbered. Each company continues from its own highest existing number, so gaps left by the old
  shared sequence remain.
- **Verify after deploy:**
  - This query returns eight rows. `plain_index` must read `CREATE INDEX` (not
    `CREATE UNIQUE INDEX`), `company_unique` must read `CREATE UNIQUE INDEX … (company_id, <column>)`,
    and `constraint_type` must be `u`. A NULL means that object is missing.

    ```sql
    SELECT t.tbl || '.' || t.col AS business_key,
           ix.indexdef           AS plain_index,
           uq.indexdef           AS company_unique,
           con.contype           AS constraint_type
    FROM (VALUES ('ncrs', 'ncr_number'), ('cars', 'car_number'), ('fais', 'fai_number'),
                 ('customer_complaints', 'complaint_number'),
                 ('return_material_authorizations', 'rma_number'),
                 ('equipment', 'equipment_id'), ('tools', 'tool_id'),
                 ('downtime_reason_codes', 'code')) AS t(tbl, col)
    LEFT JOIN pg_indexes ix
           ON ix.schemaname = 'public' AND ix.indexname = 'ix_' || t.tbl || '_' || t.col
    LEFT JOIN pg_indexes uq
           ON uq.schemaname = 'public' AND uq.indexname = 'uq_' || t.tbl || '_company_' || t.col
    LEFT JOIN pg_constraint con
           ON con.conname = 'uq_' || t.tbl || '_company_' || t.col
          AND con.conrelid = ('public.' || t.tbl)::regclass
    ORDER BY 1;
    ```

  - In a test company, create an NCR and an FAI. Confirm they save with `NCR-YYYYMMDD-NNN` and
    `FAI-YYYYMMDD-NNN` numbers, and that each leaves one `CREATE` row (`ncr`, `fai`) in the Audit
    Log. Both writers are required-audited, so a missing row means the create rolled back.
  - In the same company, create equipment with an ID the company already uses. Expect **400**
    `Equipment ID already exists`.
- **Rollback.** On an ordinary application rollback, keep the per-company constraints; older code
  works against them. It numbers complaints and RMAs from the shared sequence again, which starts
  past the highest number any company holds and so cannot collide. The downgrade restores the
  eight global unique indexes only while no value in any of the eight columns is shared by two
  rows. It counts all eight columns before any DDL, and if any value is shared it raises, reporting
  `N table.column value(s)` per affected column (never the values themselves). Every generated
  number is now per company, so on a multi-company install the first same-day NCR, CAR or FAI, or
  the next complaint or RMA, in a second company is likely to create such a collision; treat the
  downgrade as one-way there. Resolving collisions is an operator data
  decision: renumbering an NCR, CAR or FAI rewrites the identifier a quality record was issued
  under, and the migration will not pick which company's record to change.
  See [API.md → Customer Complaints & RMA](API.md#customer-complaints--rma).

## Table of Contents

1. [Quick Reference](#quick-reference)
2. [Pre-Deployment Checklist](#pre-deployment-checklist)
3. [Standard Deployment](#standard-deployment)
4. [Hotfix Deployment](#hotfix-deployment)
5. [Rollback Procedures](#rollback-procedures)
6. [Database Operations](#database-operations)
7. [Health Checks & Verification](#health-checks--verification)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Emergency Contacts](#emergency-contacts)

---

## Quick Reference

### URLs
| Environment | Frontend | Backend API | Health Check |
|-------------|----------|-------------|--------------|
| Production | https://werco-frontend-production.up.railway.app | https://werco-api-production.up.railway.app | /health |
| Staging | (configure if needed) | (configure if needed) | /health |

### Commands Cheat Sheet
```powershell
# Deploy backend (stamp the commit first so /health/detailed can name the build,
# then delete the stamp — it is untracked on purpose and must not get committed)
cd backend; git rev-parse HEAD | Out-File -Encoding ascii RELEASE; railway up --service werco-api . --path-as-root; Remove-Item RELEASE

# What commit is production actually running?
(Invoke-RestMethod "https://werco-api-production.up.railway.app/health/detailed").checks.application.release

# Deploy frontend
cd frontend; railway up --service werco-frontend . --path-as-root

# View logs
railway logs --service werco-api
railway logs --service werco-frontend

# Database backup
.\scripts\db-backup.ps1

# Health check
curl https://werco-api-production.up.railway.app/health/ready
```

### Critical Environment Variables
| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | JWT signing key (64+ chars) | Yes |
| `DATABASE_URL` | PostgreSQL connection | Auto (Railway) |
| `CORS_ORIGINS` | Frontend URL(s) | Yes |
| `ALLOWED_HOSTS` | HTTP `Host`-header allowlist. Default `*` disables validation; lock to your real hostnames in prod. On Railway **must** include `healthcheck.railway.app` and `localhost` (health-check probes) or the deploy fails its health check — see [Trusted Hosts](ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header) | Recommended |
| `ENVIRONMENT` | production/staging/development | Yes |
| `SENTRY_DSN` | Error tracking | Recommended |
| `WERCO_MCP_HTTP_ENABLED` | Serve the API to agents as MCP tools at `/mcp` (see [Enabling the MCP door](#enabling-the-mcp-door-optional)). Default `false` = nothing mounted | Optional |

### Enabling the MCP door (optional)

[docs/MCP.md](MCP.md). **No new service** — the door is a route on the existing backend service.

1. Set `WERCO_MCP_HTTP_ENABLED=true` on the **backend** Railway service (`railway variables set
   WERCO_MCP_HTTP_ENABLED=true --service werco-api`); leave `WERCO_MCP_HTTP_PATH` at its default
   `/mcp` unless the path is taken. Redeploy (a variable change triggers one).
2. It is **stateless by construction**, so the existing `uvicorn --workers ${WEB_CONCURRENCY:-2}`
   command in `backend/Dockerfile` needs no change and no sticky sessions: every `POST /mcp` is
   self-contained, and consecutive calls may land on different workers.
3. Verify: `curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<api-host>/mcp` must answer
   **401** (the door is up and refusing an unauthenticated caller); before the change it answers
   **404**. The startup log line `MCP door serving at /mcp` confirms the lifespan armed it. A
   **503** `MCP door is not running` means the route is mounted but the lifespan never ran.
4. Hand agents an **API token** for a dedicated user (e.g. *Werco Assistant*): an Admin mints it
   with `POST /api/v1/api-tokens/` ([API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients)),
   it never expires unless issued with a lifetime, and it is revoked with a reason. No server-side
   secret and no new variable are involved — it is signed with the existing `SECRET_KEY`, so
   rotating that key revokes every API token at once. A normal 15-minute access token also works,
   but then the client must renew it itself.

Turning it off is the same variable set back to `false` plus a redeploy; nothing else is left
behind. The caps (`WERCO_MCP_MAX_UPLOAD_BYTES` 25 MB envelope, `WERCO_MCP_MAX_RESULT_CHARS`,
`WERCO_MCP_MAX_BLOB_BYTES`) are documented in
[ENVIRONMENT_VARIABLES.md → MCP](ENVIRONMENT_VARIABLES.md#mcp-model-context-protocol-door-and-bridge).

---

## Change Control & Production Deploy Model

**Production auto-deploys from `main`. There is no manual approval gate.** _(Governance
change effective 2026-06-22.)_

The change-control path is:

1. **All changes reach `main` through a pull request with passing CI.** `main` is
   protected by a repository ruleset: a PR is required before merge (hands-off /
   merge-when-green — **0 required human approvals**), the CI status checks must pass,
   and force-pushes and branch deletion are blocked. Repo admins retain a documented
   break-glass bypass for emergencies. Direct pushes to `main` are PR-gated for
   non-admins.
2. **A push to `main` triggers the production deploy automatically** via GitHub Actions
   (`ci-cd.yml` / `deploy-frontend-production.yml`) — no human approval step. The
   `production` GitHub environment previously carried a required-reviewer rule; that rule
   has been **removed**. Two compensating controls remain:
   - a **deployment-branch policy** on the `production` environment that allows **only
     `main`** to deploy; and
   - **post-deploy checks that fail the job unless the new commit is actually serving**
     — `Verify production is serving this commit` (`ci-cd.yml`) and `Verify the deployed
     bundle is this commit` (`deploy-frontend-production.yml`). Both poll the running
     service for the release SHA this run stamped (see item 4) via
     `.github/scripts/verify_release.py`, so they cannot pass on a deploy that never
     landed. They replaced a `curl /health` loop that the **previous** container answered
     just as happily — it proved the site was up, never that the deploy had landed.
3. **Rollback:** redeploy a known-good commit (see [Rollback
   Procedures](#rollback-procedures)); to reinstate a manual gate, re-add the required
   reviewer on the `production` environment.
4. **The deployed commit is stamped into the artifact.** The `Stamp release SHA into the
   backend artifact` step writes `$GITHUB_SHA` to `backend/RELEASE` immediately **before**
   `railway up` uploads the backend; the Dockerfile copies it into the image, and the app
   reads it into `APP_RELEASE` at startup. That value is reported to Sentry as the event
   `release` and by `/health/detailed` as `checks.application.release`, so a running
   container can always name the commit it was built from — see [Health Check
   Endpoints](#health-check-endpoints). It is deliberately **not** a Railway service
   variable: a variable is set independently of the build and would keep advertising the
   SHA CI last attempted even after a failed deploy or a rollback to an older image.

   The frontend carries the same stamp: `Stamp release SHA into the frontend artifact`
   writes `$GITHUB_SHA` to `frontend/public/release.txt`, Vite copies `public/` into the
   build output verbatim, and nginx serves it at `/release.txt`. Neither marker may be
   gitignored (`railway up` honors `.gitignore`) or committed (a committed SHA goes stale
   and lies); `test_ci_workflow_gates.py` and `test_sentry_observability_config.py` hold
   both rules.

5. **`railway up`'s exit status is advisory, and that is deliberate.** On **2026-08-04**
   two consecutive production deploys (PRs #198 and #203) were reported RED while the
   code shipped fine: the CLI uploaded, Railway built and released the new image, and
   then the CLI exited 1 with `Failed to stream build logs: Failed to retrieve build log`
   about 66 s later. Because a failed step skips the rest of the job, that also skipped
   the frontend deploy, the worker deploy, `verify_launch`, and the GitHub Release —
   production ran two commits that the release history denied, and `deploy-892` sat
   mislabelled as "Latest".

   The deploy steps therefore carry `continue-on-error: true`, and the job is gated on
   evidence instead:
   - **`Confirm Railway accepted the <service> upload`** — greps the teed CLI output for
     the `Build Logs:` URL, which is printed only once an upload is accepted and a build
     queued. This fails **fast** on a bad token or project ID, instead of waiting out the
     release poll.
   - **`Verify production is serving this commit`** — the real gate (item 2).

   Diagnosing a red deploy: if the receipt step passed but verification timed out, the
   upload reached Railway and the **build or container start** failed — open the
   `Build Logs:` URL from the deploy step. If the receipt step failed, the upload itself
   was rejected — check `RAILWAY_TOKEN` / `RAILWAY_PROJECT_ID`.

   `ci-cd.yml`'s `deploy-staging` job is **not** covered by any of this. It fires on
   `develop` (unused — every PR merges to `main`), stamps nothing, and keeps the older
   `curl /health` check, so it carries the same latent false-failure. Give it the same
   treatment if `develop` is ever revived.

The manual `railway up` commands below remain valid for **break-glass / out-of-band**
deploys (e.g. when a CI deploy job itself is broken). They are not the routine path —
merging a green PR to `main` is. A manual deploy skips CI's release stamp, so write the
SHA to `backend/RELEASE` yourself before `railway up` (the commands below do) or accept a
`null` release on that build.

---

## Pre-Deployment Checklist

### Before Every Deployment

- [ ] All tests passing locally
- [ ] Change merged to `main` via a PR with passing CI (the `main` ruleset enforces this;
      0 human approvals are required — merge-when-green)
- [ ] Git status clean (no uncommitted changes)
- [ ] Current branch is `main`
- [ ] Database backup taken (for DB changes)
- [ ] Team notified of deployment

### For Database Migrations

- [ ] Migration tested locally
- [ ] Rollback migration available
- [ ] Backup taken before migration
- [ ] Deployment window scheduled (if needed)

### Verification Commands
```powershell
# Verify clean git status
git status

# Verify on main branch
git branch --show-current

# Run tests
cd backend; python -m pytest tests/ -v
cd frontend; npm test
```

### Wave 2 rollout

**Floor gates for draft / on-hold work orders (owner ruling 2026-11-05).** No migration and no
environment variable. After this deploy, clock-in, both operation-start verbs and completing a
READY operation refuse **409** on a work order that is DRAFT or ON_HOLD (so does a hand-set
`status: in_progress` through `PUT /api/v1/work-orders/operations/{id}`), and a held work order goes
back to the floor through **Resume Work Order** (`POST /api/v1/work-orders/{id}/resume`), which
resumes to DRAFT when no release is on record. Before deploying, run these two **read-only** queries against
production (Supabase SQL editor; status enum labels are stored uppercase) and have a supervisor
clear what they find. Operators already clocked in are not affected: clock-out, production reports
and completing an IN_PROGRESS operation stay allowed on a held job.

1. **Draft or held jobs that still carry floor work.** These become un-joinable the moment the
   deploy lands. Release each DRAFT one (Work Order detail → **Release**) or, after the deploy,
   resume each held one.

   ```sql
   SELECT wo.company_id, wo.id, wo.work_order_number, wo.status,
     count(DISTINCT op.id) FILTER (WHERE op.status IN ('READY','IN_PROGRESS')) AS ready_or_running_ops,
     count(DISTINCT te.id) FILTER (WHERE te.clock_out IS NULL) AS open_time_entries
   FROM work_orders wo
   LEFT JOIN work_order_operations op ON op.work_order_id = wo.id AND op.company_id = wo.company_id
   LEFT JOIN time_entries te ON te.work_order_id = wo.id AND te.company_id = wo.company_id
   WHERE wo.is_deleted = false AND wo.status IN ('DRAFT','ON_HOLD')
   GROUP BY wo.company_id, wo.id, wo.work_order_number, wo.status
   HAVING count(DISTINCT op.id) FILTER (WHERE op.status IN ('READY','IN_PROGRESS')) > 0
       OR count(DISTINCT te.id) FILTER (WHERE te.clock_out IS NULL) > 0
   ORDER BY wo.company_id, wo.work_order_number;
   ```

2. **Held jobs with no release record.** These resume to **DRAFT** (their un-worked READY
   operations return to PENDING) and then need a **Release** click. Expect older laser nest
   packages, MRP auto-released work orders and work orders released through a raw `PUT` here.

   ```sql
   SELECT company_id, id, work_order_number, work_order_type, updated_at
   FROM work_orders
   WHERE is_deleted = false AND status = 'ON_HOLD' AND released_at IS NULL AND released_by IS NULL
   ORDER BY company_id, work_order_number;
   ```

3. **Training handouts.** The onboarding markdown (`docs/onboarding/02-operator-shop-floor.md`,
   `04-planner-supervisor-manager.md`) describes the new "Not released" / "WO on hold" refusals and
   Start / Hold / Resume Work Order, but the committed PDFs in `docs/onboarding/pdf/` were **not**
   regenerated — no generator in the repo produces them (they are Chrome print-to-PDF renders of the
   markdown; `docs/onboarding/generate_onboarding_pdfs.py` builds only `quick-reference/`). Before
   handing the operator or supervisor guide to anyone after this deploy, re-print those two PDFs from
   the current markdown (once, after the short-count PR if it ships in the same window).

See `docs/API.md` → Work Orders → "Draft and on-hold work orders refuse every start".

**Short count at operation completion (owner ruling 1, 2026-11-05).** One migration, no environment
variable. After this deploy a short count at operation completion is reported as scrap with a reason
or the operation stays open: `POST /api/v1/shop-floor/operations/{id}/complete` closes at the good
the floor recorded and refuses **409** (writing nothing) while units are unaccounted, the office
operation complete and force-complete apply the same rule, and an API token may not close work short
(**403**).

1. **Migration `117_operation_short_declared`** (down-revision `116_business_keys_per_company`) adds
   one nullable `Float` column, `work_order_operations.quantity_short_declared` — no default, no
   backfill, no index, no RLS or grant change. It runs at **API boot** (`alembic upgrade head` in the
   API image's start command, before uvicorn) and is a metadata-only change on Postgres, safe online.
   The worker image never migrates, so deploy the **API before or with the worker**: a worker on the
   new code against a database that has not run `117` would select a column that does not exist yet.
   Every existing row reads `NULL` ("never declared"), so clock-out, reconcile-on-read and the
   work-order header behave exactly as before on existing data. The downgrade drops the column and
   with it every short declared since the deploy (pre-117 code never reads it).

2. **Optional, read-only diagnostic — and the supervisor follow-up it finds.** Operations closed
   short before the rule existed (COMPLETE with good + scrap below their target, on a work order
   still open). The legacy operation itself reads exactly as before (no declaration, so no credit),
   but the **open later operations of the same work order can no longer close at what physically
   reached them**: they still owe the full target, and ruling 1 refuses to close them short without
   scrap. **Do not have the floor scrap units that never reached them.** The supervisor remedy is one
   of: lower `quantity_ordered` to what the legacy stage passed on (Work Order detail → edit — allowed
   down to the largest good + scrap any whole-order stage recorded; restated per-operation targets
   follow it), or force-complete the work order at the work-order grain (`POST
   /work-orders/{id}/complete`) with the work-order scrap and a reason covering the shortfall.

   ```sql
   SELECT wo.company_id, wo.work_order_number, wo.status AS wo_status,
          op.id AS operation_id, op.sequence, op.operation_number,
          CASE WHEN op.component_quantity > 0 THEN op.component_quantity
               ELSE wo.quantity_ordered END AS target,
          op.quantity_complete, op.quantity_scrapped
   FROM work_order_operations op
   JOIN work_orders wo ON wo.id = op.work_order_id AND wo.company_id = op.company_id
   WHERE wo.is_deleted = false
     AND wo.status NOT IN ('COMPLETE','CLOSED','CANCELLED')
     AND wo.work_order_type <> 'laser_cutting'
     AND op.status = 'COMPLETE'
     AND COALESCE(op.quantity_complete, 0) + COALESCE(op.quantity_scrapped, 0)
         < CASE WHEN op.component_quantity > 0 THEN op.component_quantity ELSE wo.quantity_ordered END
   ORDER BY wo.company_id, wo.work_order_number, op.sequence;
   ```

3. **During the deploy window — promote the API first.** Vercel (SPA) and Railway (API) both
   auto-deploy from `main`, and the SPA usually goes live first. For this release that order
   matters: the NEW floor clients send `/shop-floor/operations/{id}/complete` without
   `quantity_complete`, which the OLD API requires, so every kiosk / crew / desktop Complete answers
   **422** (*"Clocked out, but completing failed: …quantity_complete…"*) until the API is live. It
   fails closed — nothing is half-written; the operation stays open and Complete works once the API
   is up. To avoid the window, hold the Vercel promotion (or redeploy the frontend) until Railway
   reports the new API SUCCESS and `/health` answers from it. The reverse case: a kiosk or tablet
   still running the OLD bundle sends `{"quantity_complete": <target>}` after a short clock-out; it
   now gets the *"…exceeds the n good recorded…"* 409 and the operator sees *"Clocked out, but
   completing failed"*. The operation is left open, which is what the rule requires — reload every
   kiosk and floor tablet after the deploy. An MCP agent or script that relied on a partial floor
   `/complete` now gets 409, and any standing API token that closed work short, booked good above
   the floor's count, edited operation counts or lowered an order after work was recorded now gets
   403.
   One deliberate read-path change on old data: a work order whose operations all completed but whose
   header sits ABOVE its order is lowered to the order at the next reconciling read (and its
   finished-goods receipt posts at the order) — the header is now clamped to `quantity_ordered`. To
   see any before the deploy: `SELECT company_id, work_order_number, quantity_ordered,
   quantity_complete FROM work_orders WHERE is_deleted = false AND status NOT IN
   ('COMPLETE','CLOSED','CANCELLED') AND quantity_complete > quantity_ordered;`.

4. **Training handouts.** The onboarding markdown (`docs/onboarding/02-operator-shop-floor.md`,
   `04-planner-supervisor-manager.md`) now also covers the short-count flow (SHORT, report as scrap,
   CLOCK OUT · KEEP OPEN, office Save progress / Reopen). Re-print the two PDFs in
   `docs/onboarding/pdf/` from the current markdown once, after this deploy, as the floor-gates
   item 3 ("Training handouts") above says — they are not generated from the repo. The generated quick-reference role cards
   (`docs/onboarding/quick-reference/`, built by `docs/onboarding/generate_onboarding_pdfs.py`) do
   not mention the short-count flow yet; update the kiosk and crew-station card content in that
   script and regenerate before handing them out.

See `docs/API.md` → Work Orders → "Short count at completion".

---

## Standard Deployment

### Step 1: Prepare
```powershell
cd C:\Users\jmw\Desktop\Werco-ERP

# Pull latest changes
git pull origin main

# Verify clean status
git status
```

### Step 2: Deploy Backend
```powershell
cd backend

# Stamp the commit into the artifact so /health/detailed and Sentry can name it.
# CI does this automatically; a manual deploy must do it explicitly or the build
# reports release=null. Must run BEFORE `railway up` uploads the directory.
# (Out-File -Encoding ascii, not `>` -- Windows PowerShell 5.1 redirects as UTF-16.)
git rev-parse HEAD | Out-File -Encoding ascii RELEASE

# Deploy to Railway
railway up --service werco-api . --path-as-root

# Remove the stamp once it is uploaded. RELEASE is deliberately NOT gitignored (see
# Change Control), so leaving it behind lets a stray `git add -A` commit a SHA that
# then goes stale and misreports every later unstamped build.
Remove-Item RELEASE

# Verify launch readiness (config sanity checks)
railway run --service werco-api python -m scripts.verify_launch

# Wait for deployment (usually 2-5 minutes)
# Monitor build logs in Railway dashboard or:
railway logs --service werco-api
```

### Step 3: Verify Backend
```powershell
# Basic health check
curl https://werco-api-production.up.railway.app/health

# Detailed health check (includes DB connectivity)
curl https://werco-api-production.up.railway.app/health/ready

# Expected response for /health/ready:
# {
#   "status": "healthy",
#   "timestamp": "2026-01-09T...",
#   "checks": {
#     "database": {"status": "healthy", "latency_ms": ...},
#     "app": {"status": "healthy"}
#   }
# }
```

### Step 4: Deploy Frontend
```powershell
cd ../frontend

# Stamp the commit so /release.txt can name the build, exactly as CI does. Skipping this
# is survivable -- /release.txt then falls through nginx's SPA rule and returns
# index.html, i.e. "unknown release" -- but it means this build cannot be identified
# later. Must run BEFORE `railway up` uploads the directory.
git rev-parse HEAD | Out-File -Encoding ascii -NoNewline public/release.txt

# Deploy to Railway
railway up --service werco-frontend . --path-as-root

# Remove the stamp once uploaded. public/release.txt is deliberately NOT gitignored (so
# `railway up` ships it) and must never be committed (a committed SHA would go stale).
Remove-Item public/release.txt

# Monitor deployment
railway logs --service werco-frontend

# Confirm the running bundle is the commit you just deployed
(Invoke-WebRequest "https://werco-frontend-production.up.railway.app/release.txt").Content
```

### Step 5: Verify Frontend
1. Open browser to https://werco-frontend-production.up.railway.app
2. Verify login page loads
3. Test login with known credentials
4. Navigate to key pages (Dashboard, Parts, Work Orders)
5. Check browser console for errors

### Step 6: Post-Deployment
```powershell
# Check for errors in Sentry (if configured)
# Review Railway logs for any warnings/errors
railway logs --service werco-api --tail 50
```

---

## Hotfix Deployment

For urgent fixes that bypass normal release process.

### Step 1: Create Hotfix Branch
```powershell
git checkout main
git pull origin main
git checkout -b hotfix/description-of-fix
```

### Step 2: Make Fix
```powershell
# Make necessary changes
# Test locally
cd backend; python -m pytest tests/ -v
```

### Step 3: Commit and Push
```powershell
git add -A
git commit -m "hotfix: description of fix"
git push origin hotfix/description-of-fix
```

### Step 4: Merge to Main (via PR)
Open a PR from the hotfix branch and merge it once CI is green — `main` is PR-gated by the
ruleset, so a direct `git push origin main` is rejected for non-admins:
```powershell
gh pr create --base main --head hotfix/description-of-fix --fill
# merge once CI status checks pass (0 approvals required — merge-when-green)
gh pr merge --squash --auto
```
Repo admins may use the documented break-glass bypass if CI itself is broken and the fix
cannot wait.

### Step 5: Deploy
Merging to `main` **auto-deploys to production** (see [Change Control & Production Deploy
Model](#change-control--production-deploy-model)); the GitHub Actions deploy + post-deploy
health checks run automatically. Use the manual `railway up` steps in [Standard
Deployment](#standard-deployment) only for an out-of-band / break-glass deploy.

### Step 6: Cleanup
```powershell
git branch -d hotfix/description-of-fix
git push origin --delete hotfix/description-of-fix
```

---

## Rollback Procedures

### Rollback Backend (Railway)

**Option 1: Redeploy Previous Commit**
```powershell
# Find previous working commit
git log --oneline -10

# Checkout previous commit
git checkout <commit-hash>

# Deploy
cd backend
# Stamp the rolled-back commit, so /health/detailed reports what is ACTUALLY running
# rather than the SHA of the bad deploy this is replacing.
git rev-parse HEAD | Out-File -Encoding ascii RELEASE
railway up --service werco-api . --path-as-root

# Remove the stamp once uploaded — it is untracked and survives the checkout below,
# where a stray `git add -A` would commit a SHA that then goes stale.
Remove-Item RELEASE

# Return to main
git checkout main
```

**Option 2: Railway Dashboard**
1. Open Railway dashboard
2. Navigate to werco-api service
3. Go to "Deployments" tab
4. Click on previous successful deployment
5. Click "Redeploy"

> **Option 2 cannot restamp — and does not need to.** Redeploying a previous image from
> the dashboard replays the artifact as built, so `/health/detailed` reports the release
> that image was stamped with: the commit actually running. That is exactly the property
> that made the file-based stamp preferable to a service variable, which would still be
> advertising the SHA of the bad deploy you just rolled back.

### Rollback Frontend (Railway)

Same process as backend, use:
```powershell
cd frontend
railway up --service werco-frontend . --path-as-root
```

### Rollback Database Migration

```powershell
# Connect to Railway and run Alembic downgrade
railway run --service werco-api alembic downgrade -1

# Or downgrade to specific revision
railway run --service werco-api alembic downgrade <revision>
```

### Full System Rollback

If both backend and frontend need rollback:

1. **Stop traffic** (if possible via Railway settings)
2. **Rollback backend** to previous version
3. **Verify backend health**: `/health/ready`
4. **Rollback frontend** to matching version
5. **Verify full system** functionality
6. **Resume traffic**

---

## Database Operations

### Backup Database
```powershell
cd C:\Users\jmw\Desktop\Werco-ERP\scripts

# Create backup
.\db-backup.ps1

# List available backups
.\db-backup-utils.ps1 list

# Verify backup integrity
.\db-backup-utils.ps1 verify
```

### Restore Database

**WARNING: This replaces ALL data. Use with extreme caution.**

```powershell
cd C:\Users\jmw\Desktop\Werco-ERP\scripts

# List backups to find the one to restore
.\db-backup-utils.ps1 list

# Restore (requires typing "RESTORE" to confirm)
.\db-restore.ps1 -BackupFile "..\backups\database\werco_erp_backup_20260109_120000.sql.gz"
```

### Run Database Migrations

The warehouse/import workflow release adds migrations
`099_recoverable_import_batches` and `100_receiving_supplier_followup` after
`098_runtime_metrics`. Upgrade the API database before serving the matching
frontend: receiving queries now include supplier promises and certificate links.
New tables retain deny-by-default RLS and explicitly revoke Data API access to
tables and sequences. The disposable PostgreSQL CI check exercises 095–100 with
inherited grants and two upgrade/downgrade cycles.

For rollback, keep these additive tables and columns while rolling back application
code. Dropping them after use would remove import/retry receipts, certificate links
and supplier follow-up history. Test downgrades are for disposable databases;
production rollback must preserve those records. Vercel credential setup and public
domain promotion remain the separate release prerequisite described in
[release coordination](ux-operations/release-coordination.md).

```powershell
# Via Railway
railway run --service werco-api alembic upgrade head

# Check current revision
railway run --service werco-api alembic current

# View migration history
railway run --service werco-api alembic history
```

### Database Connection (Direct)
```powershell
# Get connection URL from Railway
railway variables get DATABASE_URL --service werco-api

# Connect using psql (if installed)
psql "postgresql://..."
```

---

## Health Checks & Verification

### Health Check Endpoints

| Endpoint | Purpose | Expected Status |
|----------|---------|-----------------|
| `/health` | Basic liveness | 200, `{"status": "healthy"}` |
| `/health/live` | Container alive | 200, `{"status": "alive"}` |
| `/health/ready` | Ready for traffic | 200 (or 503 if unhealthy) |
| `/health/detailed` | Full system info | 200, includes versions and the deployed commit (`checks.application.release`) |

> **"Is commit X actually deployed?" — one call.** The CI deploy job stamps the commit
> SHA into the artifact (see [Change Control](#change-control--production-deploy-model)),
> and the API reports it back:
> ```bash
> curl -s https://werco-api-production.up.railway.app/health/detailed \
>   | jq -r '.checks.application.release'
> ```
> Compare with `git rev-parse origin/main`. `null` means the running build carries no
> stamp — expected for a break-glass manual `railway up` that skipped the stamping step
> below, and for any image built before this was introduced. The sibling `version` field
> is a hardcoded `1.0.0` kept for existing monitors; it never distinguished deploys.

### Automated Health Check Script
```powershell
$apiUrl = "https://werco-api-production.up.railway.app"

# Basic check
$health = Invoke-RestMethod "$apiUrl/health"
if ($health.status -eq "healthy") {
    Write-Host "Basic health: OK" -ForegroundColor Green
} else {
    Write-Host "Basic health: FAILED" -ForegroundColor Red
}

# Readiness check
$ready = Invoke-RestMethod "$apiUrl/health/ready"
if ($ready.status -eq "healthy") {
    Write-Host "Readiness: OK (DB latency: $($ready.checks.database.latency_ms)ms)" -ForegroundColor Green
} else {
    Write-Host "Readiness: FAILED - $($ready | ConvertTo-Json)" -ForegroundColor Red
}
```

### Manual Verification Checklist

- [ ] Health endpoint returns 200
- [ ] Login page loads
- [ ] Can log in with valid credentials
- [ ] Dashboard displays data
- [ ] Can create/edit/delete records
- [ ] No console errors in browser
- [ ] No Sentry alerts triggered

---

## Troubleshooting Guide

### Deployment Failed

**Symptoms**: Railway build fails, service not starting

**Steps**:
1. Check build logs: `railway logs --service werco-api`
2. Look for error messages (missing dependencies, syntax errors)
3. Verify `requirements.txt` / `package.json` is complete
4. Check Dockerfile exists and is valid

**Common Fixes**:
```powershell
# Missing dependency
# Add to requirements.txt and redeploy

# Build cache issues (Railway)
# Go to Railway dashboard > Service > Settings > Clear build cache
```

### Backend Not Responding

**Symptoms**: 502 Bad Gateway, connection refused

**Steps**:
1. Check if service is running: `railway status`
2. Check health endpoint: `curl .../health`
3. View logs: `railway logs --service werco-api --tail 100`
4. Check DATABASE_URL is set

**Common Fixes**:
```powershell
# Restart service via Railway dashboard
# Check database is accessible
# Verify environment variables are set
```

### Database Connection Failed

**Symptoms**: Health check shows database unhealthy, 500 errors

**Steps**:
1. Check `/health/ready` response
2. Verify DATABASE_URL: `railway variables get DATABASE_URL`
3. Check PostgreSQL service in Railway

**Common Fixes**:
- Restart PostgreSQL service in Railway
- Check connection pool exhaustion (may need restart)
- Verify network connectivity

### CORS Errors

**Symptoms**: Browser console shows CORS errors, API calls fail

**Steps**:
1. Check `CORS_ORIGINS` env var
2. Verify frontend URL matches exactly (https, no trailing slash)
3. Check for typos

**Fix**:
```powershell
# Update CORS_ORIGINS
railway variables set CORS_ORIGINS="https://werco-frontend-production.up.railway.app"

# Redeploy backend
cd backend; railway up --service werco-api . --path-as-root
```

### Authentication Issues

**Symptoms**: Can't log in, token errors, 401 responses

**Steps**:
1. Verify SECRET_KEY is set and consistent
2. Check token expiration settings
3. Clear browser cookies/storage

**Common Fixes**:
- Regenerate tokens (log out and back in)
- Verify SECRET_KEY hasn't changed
- Check user account is active

### Frontend Not Loading

**Symptoms**: Blank page, 404 errors, missing assets

**Steps**:
1. Check browser console for errors
2. Verify build completed successfully
3. Check `REACT_APP_API_URL` is correct

**Common Fixes**:
```powershell
# Verify API URL
railway variables get REACT_APP_API_URL --service werco-frontend

# Should be: https://werco-api-production.up.railway.app/api/v1
```

### High Memory/CPU Usage

**Symptoms**: Slow responses, service restarts

**Steps**:
1. Check Railway metrics dashboard
2. Review recent changes for memory leaks
3. Check for runaway queries

**Common Fixes**:
- Optimize database queries
- Add pagination to large list endpoints
- Increase Railway plan resources

---

## Emergency Contacts

| Role | Name | Contact | Availability |
|------|------|---------|--------------|
| Primary On-Call | [TBD] | [TBD] | 24/7 |
| Database Admin | [TBD] | [TBD] | Business hours |
| Infrastructure | [TBD] | [TBD] | Business hours |
| Security | [TBD] | [TBD] | 24/7 for incidents |

### Escalation Path

1. **Level 1**: On-call engineer (15 min response)
2. **Level 2**: Team lead (30 min response)
3. **Level 3**: Engineering manager (1 hour response)
4. **Level 4**: CTO (critical incidents only)

### Incident Response

For security incidents or major outages:

1. **Assess** severity (P1-P4)
2. **Communicate** via Slack #incidents channel
3. **Mitigate** - implement temporary fix if available
4. **Investigate** root cause
5. **Resolve** and document
6. **Post-mortem** within 48 hours for P1/P2

---

## Appendix

### Environment Variable Reference

```env
# Required
SECRET_KEY=<64-char-random-string>
DATABASE_URL=postgresql://...  # Auto-set by Railway
ENVIRONMENT=production
CORS_ORIGINS=https://frontend-url.com

# Security
REFRESH_TOKEN_SECRET_KEY=<64-char-random-string>
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
# Session ceiling stamped into each refresh token. Because /auth/refresh recomputes
# the claim on every rotation, this bounds an IDLE window, not total session life.
# Matches the code default (168h = the 7-day refresh window); lower it to re-arm a
# tighter cap. See docs/ENVIRONMENT_VARIABLES.md#security
SESSION_ABSOLUTE_TIMEOUT_HOURS=168
# Host-header allowlist; default "*" disables validation. Must include the
# health-check probe hosts (healthcheck.railway.app, localhost) or the deploy
# fails its health check. See docs/ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header
ALLOWED_HOSTS=werco-api-production.up.railway.app,*.up.railway.app,healthcheck.railway.app,localhost

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_TIMES=100
RATE_LIMIT_SECONDS=60

# Monitoring
SENTRY_DSN=https://...@sentry.io/...
LOG_LEVEL=INFO

# Optional
REDIS_URL=redis://...
ANTHROPIC_API_KEY=...
# MCP door for agents (docs/MCP.md) — off unless set; no new service, stateless behind
# WEB_CONCURRENCY=2. See docs/ENVIRONMENT_VARIABLES.md#mcp-model-context-protocol-door-and-bridge
WERCO_MCP_HTTP_ENABLED=false
```

### Railway CLI Commands

```powershell
# Login
railway login

# Link to project
railway link

# List services
railway service list

# Set variables
railway variables set KEY=value --service service-name

# Get variables
railway variables get KEY --service service-name

# View logs
railway logs --service service-name

# Run command in service
railway run --service service-name command

# Open dashboard
railway open
```

### Git Commit Conventions

```
feat: Add new feature
fix: Bug fix
security: Security improvement
docs: Documentation
refactor: Code refactoring
test: Add/update tests
chore: Maintenance task
```

---

**Document maintained by**: DevOps Team  
**Review schedule**: Monthly or after major incidents
