# Deployment Guide

This guide covers deploying Werco ERP to production environments.

## Prerequisites

- Production server with:
  - Python 3.11+
  - Node.js 22+ (LTS; see frontend/.nvmrc)
  - Supabase project with Postgres database access
  - Redis (recommended for caching)
  - Nginx or similar reverse proxy
- Domain name with SSL certificate
- S3-compatible storage for document files (`STORAGE_BACKEND=s3`; alternatively a mounted, backed-up volume at `UPLOAD_DIR` for `STORAGE_BACKEND=local`)

## Environment Configuration

### Production .env File

Create a secure `.env` file in the backend directory:

This file supports a direct host deployment. Docker images exclude local environment
files and credentials; inject values through runtime environment settings instead.
For Compose, use `docker compose --env-file .env.prod -f docker-compose.prod.yml …`.
See [Docker build contexts](DOCKER_PRODUCTION.md#build-contexts-and-runtime-secrets)
for the inclusion checks and [Redis queue retention](DOCKER_PRODUCTION.md#redis-queue-retention-and-memory-pressure)
for the required `noeviction` policy on shared queue/cache Redis, including managed Redis.

```env
# Database
DATABASE_URL=postgresql://postgres.meatfdvteugbeksckgqg:<SUPABASE_DB_PASSWORD>@aws-1-us-west-2.pooler.supabase.com:5432/postgres
DATABASE_PROVIDER=supabase

# Security - MUST be random and strong
SECRET_KEY=generate-with-openssl-rand-base64-32
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=480

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_TIMES=100
RATE_LIMIT_SECONDS=60

# Application
APP_NAME=Werco ERP Production
DEBUG=false
ENVIRONMENT=production
API_V1_PREFIX=/api/v1

# CORS - Update with your frontend domain
CORS_ORIGINS=https://werco-erp.yourdomain.com
CORS_ALLOW_CREDENTIALS=true
CORS_ALLOW_METHODS=GET,POST,PUT,DELETE,PATCH,OPTIONS
CORS_ALLOW_HEADERS=*

# File Storage — durable bytes for document uploads, shipping labels/BOLs, RFQ
# files, and PO source documents (app/services/storage_service.py). The default
# STORAGE_BACKEND=local keeps files on the container filesystem (only durable if
# /app/uploads is a mounted volume); "s3" uses AWS S3 or any S3-compatible store
# and fails fast at boot if bucket/credentials are missing.
# See docs/ENVIRONMENT_VARIABLES.md -> File Storage.
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
S3_BUCKET_NAME=werco-erp-documents-prod
# Required for S3-compatible stores (Railway buckets, Cloudflare R2); omit for AWS S3
# S3_ENDPOINT_URL=https://your-s3-compatible-endpoint

# Monitoring
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
LOG_LEVEL=WARN

# Redis Cache
REDIS_URL=redis://localhost:6379/0

# LLM Integration (optional)
ANTHROPIC_API_KEY=your_anthropic_key

# Audit Log Retention / Archival (CMMC AU-3.3.8) — runs in the ARQ worker.
# Audit logs are immutable and never deleted; aged rows are archived monthly to
# this directory. Point it at a persistent, backed-up volume on the worker.
# See docs/AUDIT_LOG_RETENTION_RUNBOOK.md.
AUDIT_ARCHIVE_DIR=/var/lib/werco/audit-archive
```

> **Background jobs run in the ARQ worker** (`arq app.worker.WorkerSettings`), separate from the API
> process — make sure it is running. **"Make sure it is running" is not a one-liner on Railway:
> see `docs/WORKER_SERVICE.md`** for creating the service (it must have **no** healthcheck path, and
> it deploys from the repo root, not `backend/`), the variables it needs, and the staged order for
> arming crons — several of them write or email in bulk on their first run. The worker and the API
> must resolve the **same** Redis: set `REDIS_URL` on both and confirm via `/health/ready` →
> `job_queue_redis`. Its monthly `archive_aged_audit_logs_job` (1st of month, 03:00)
> exports aged audit rows to `AUDIT_ARCHIVE_DIR`; provision that path as durable, backed-up storage so
> archives survive restarts/rebuilds. The weekly `cleanup_old_logs_job` purges only ephemeral
> job/notification logs — **not** audit logs. See `docs/AUDIT_LOG_RETENTION_RUNBOOK.md` and the
> Audit Log Retention / Archival section of `docs/ENVIRONMENT_VARIABLES.md`.

### Generate Secure Secret Key

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

or

```bash
openssl rand -base64 32
```

### MCP door (optional)

The API can serve itself to agents as MCP tools at `/mcp` ([docs/MCP.md](MCP.md)). It is **off by
default** and needs no new service, port or process — one variable on the backend:

```env
# Off by default. When true, a stateless Streamable HTTP endpoint is served at
# WERCO_MCP_HTTP_PATH (/mcp) on the API itself; every tool call dispatches back
# into this app as the caller's own user (bearer JWT, same RBAC/tenancy/audit).
WERCO_MCP_HTTP_ENABLED=true
WERCO_MCP_HTTP_PATH=/mcp
```

The door is stateless by construction (no session lives in a worker), so it is safe behind
`uvicorn --workers N` / `WEB_CONCURRENCY=2` and behind a load balancer without sticky sessions.
Callers authenticate with a normal 15-minute ERP access token; the proxy in front must pass the
`Authorization` header through and allow request bodies up to `WERCO_MCP_MAX_UPLOAD_BYTES`
(25 MB — a file upload rides base64-encoded inside the JSON-RPC envelope) on that path, e.g.
`client_max_body_size 25m;` in the Nginx `location` that fronts it. All settings:
[docs/ENVIRONMENT_VARIABLES.md → MCP](ENVIRONMENT_VARIABLES.md#mcp-model-context-protocol-door-and-bridge).

## Deployment Methods

### Hank task storage

Before exposing Hank task actions, apply additive migration **108_hank_tasks**
after **107_fabrication_quote_profiles** through the normal API startup migration
path, then promote the matching frontend. It creates task previews and receipts,
not production jobs, orders, or seed tasks. PostgreSQL must have RLS enabled and
table/sequence access revoked from `PUBLIC`, `anon`, and `authenticated`; the same
guards run on model bootstrap. Existing server authentication and tenant/owner
checks remain authoritative. This schema adds no secret or environment setting.

For application rollback, retain the task table and its receipts. The tested
schema downgrade drops Hank task history and is only appropriate for a deliberate
schema removal with preservation of any required evidence. It does not reverse
business records already created by a completed task.

### Hank follow-up worker

Hank's opt-in follow-ups reuse migration **108_hank_tasks**. Deploy matching API,
worker and frontend releases through the existing combined-release path. No new
environment variable, secret or service is required. The existing production cron
selection was `none` after quote-nesting retirement; to enable scheduled Hank checks,
set **`WORKER_CRON_JOBS=check_hank_watches_job`** on the worker and redeploy. This exact
allowlist enables only the new five-minute check and preserves suppression of all
13 unrelated schedules. Do not use `all` or restore a retired nesting relay.

Verify the active worker release and fresh generic heartbeat, then verify its startup
log lists `cron:check_hank_watches_job` and reports **13 of 14** crons suppressed.
For an explicitly created test follow-up with alerts enabled, verify `last_checked_at`
advances and a matched condition creates one owner-only in-app notification linked to its receipt.
Each pass checks at most 100 due watches; it is periodic polling, not an immediate
notification guarantee. A healthy heartbeat alone does not prove a watch was checked.
With the cron disabled or delayed, **Check now** still works in the API and an
unchecked watch displays **Waiting for first check**. To suspend scheduled checks,
restore `WORKER_CRON_JOBS=none`; retain task history and completed notifications.

### Hank personal preferences

Apply additive **109_hank_preferences** after **108_hank_tasks** through the normal
API migration path before deploying the matching worker and frontend. It creates
an empty tenant/user preference table with a unique company/user key and version
constraint. No employee defaults are seeded; reads return typed defaults without
creating rows. PostgreSQL RLS and table/sequence revokes from `PUBLIC`, `anon`, and
`authenticated` match model bootstrap. No environment variable, secret or cron
is added; retain the [Hank-only follow-up selection](#hank-follow-up-worker).

Verify save/reload/reset in the current company, a stale-version refusal, and that
muted follow-ups still produce a receipt without a new in-app notification. Briefing
detail/focus choices must retain the same permissions and evidence links. Keep the
preference table on ordinary application rollback. A deliberate 109 downgrade drops
only preferences, preserving task/watch receipts and their audit history. A worker
from before this preference feature ignores saved alert muting: suspend its watch
cron or use a compatible API/worker release when rolling back.

### Hank workflows and document intake

Apply additive **110_hank_workflows** after **109_hank_preferences** through the
normal API migration path, then deploy matching API, worker and frontend builds.
It creates empty intake-batch/file, handoff, routine and routine-run tables, with
matching model/migration PostgreSQL RLS and table/sequence revokes. It creates no
approved routine, business action or user work. Existing task/watch history stays.

The worker registers request-driven **`process_hank_intake_file_job`**. Keep the
existing Hank-only cron selection unchanged; this job needs no cron, and
`WORKER_CRON_JOBS=none` still permits requested intake processing. API and worker
must use the same durable storage and Redis. Existing Anthropic configuration and
the company AI-egress gate control extraction; no new secret or environment
variable is introduced. Worker timeout remains 600 seconds; each intake model
call is limited to 90 seconds with no SDK retries, after bounded source parsing.
Word/Excel intake and reviewed PO import reuse these tables and the existing
document libraries; no migration, secret or cron change is needed. Deploy matching
API, worker and frontend versions for PDF/DOCX/XLSX/XLS format metadata, native
source previews and source-bound purchase-order tasks. Native Office parsing has
a 20-second subprocess timeout; macros and encrypted files are unsupported.

Verify a controlled PDF upload appears in the private queue, analysis reaches
review, source pages load, and explicit filing produces one recoverable receipt.
Check a retry with the same request key and a conflicting version; do not treat
`queued` or a healthy heartbeat as evidence that analysis completed. Queue/worker
failure leaves durable files recoverable through Retry; stale analyzing work can
be retried after 15 minutes. Also verify participant handoff access and approved
routine/version behavior with their explicit controls. No shipment dispatch,
automatic label printing, carrier purchase or supplier messaging is added.

Retain these tables and stored source/photo bytes on application rollback. A
deliberate schema downgrade removes Hank workflow evidence and is not an ordinary
rollback step; it does not undo documents, receipts, production or shipments
already created by completed actions. Intake-linked Documents cannot be deleted
through the application because the saved source and receipt must remain valid.

### Floor PIN for badge sign-on (migration 130)

Apply **130_floor_pin** after **129_owned_property** through the normal API boot migration path.
No environment variable, secret, cron or worker job is added.

- **Catalog-only change, no backfill.** Adds five columns to `users` (`floor_pin_hash` nullable,
  `floor_pin_set_at` nullable, `floor_pin_failed_attempts` `integer NOT NULL` server default `0`,
  `floor_pin_locked_until` nullable, and `floor_pin_reset_required` `boolean NOT NULL` server
  default `false`) and one to `companies` (`require_floor_pin` `boolean NOT NULL` server default
  `false`), each guarded. On PostgreSQL 11+ every `ADD COLUMN` is metadata-only: the nullable
  columns have no default and the three NOT NULL ones a constant
  default kept in the catalog, so there is no table rewrite, no NOT NULL scan, no index, FK or
  CHECK, and no `UPDATE`. After the upgrade no user has a PIN, nothing is locked or flagged and
  every company is off, so badge sign-on behaves exactly as before until someone sets a PIN.
- **Locks.** `SET LOCAL lock_timeout = '5s'`, handed back once the DDL is done. Each table takes a
  brief ACCESS EXCLUSIVE lock held to the end of the migration transaction, `users` first, then
  `companies` (the order request authentication reads them). `users` is read by every
  authenticated request, so requests queue behind that lock for at most the 5-second wait. A
  timeout fails the boot with the database back at `129`, and redeploying is safe.
- **Order.** Run the migration before or with the code that maps the columns (the API boot does).
  Old code is safe on the new schema: it never selects the columns and its inserts take the
  defaults. New code on an un-migrated database fails every sign-in and authenticated request
  (every user and company load selects the new columns), so deploy the worker with or after the
  API, never before. Deploy the API before the frontend: the Users page's Floor PIN dialog and
  Admin Settings → **Badge Sign-in** call routes the old API does not have (badge sign-in itself
  works against either).
- **SQLite only (local and test databases; production is untouched).** Right after it adds
  `require_floor_pin`, the upgrade runs `UPDATE companies SET require_floor_pin = 0 WHERE
  require_floor_pin = 'false'`. This is a storage-spelling fix, not a backfill: SQLite's
  `ADD COLUMN` leaves pre-existing rows holding the default as the text `'false'`, which
  SQLAlchemy reads as true, so without it every company on an incrementally migrated local
  database would require PINs and lock every PIN-less badge out of local dev. PostgreSQL gets no
  `UPDATE`. The later boolean, `floor_pin_reset_required`, uses `sa.false()` as its server
  default, which SQLite stores as `0`, so it needs no such fix.
- **Owner runbook: turning PINs on.**
  1. **Set PINs first.** Users → the **Floor PIN** column (Admin and Manager) reads *Not set* for
     everyone who has none; **Manage → Set** gives a person a 4–6 digit PIN (not one digit
     repeated, not a straight run such as 1234, not their badge digits). A Manager can do this only
     for Operator, Supervisor, Shipping and Viewer accounts; an Admin's, a Manager's or a Quality
     user's PIN needs an Admin. Tell each person their PIN privately; the app never displays a
     saved PIN. A person with a PIN is challenged for it at once, whatever the policy. Promoting
     someone who has a PIN into Admin, Manager or Quality (or approving them into one) clears the
     PIN and flags the account: their badge is refused "Your role changed. Ask an Admin to set a
     new floor PIN.", whatever the policy, and the Users page shows *New PIN needed (role
     changed)* until an Admin sets a new PIN (or clears it, to allow badge-only sign-on).
  2. **Before turning the policy on for the first time, rotate `REFRESH_TOKEN_SECRET_KEY`** (this
     signs everyone out once). A badge session signed in **before this deploy** carries no `amr`
     claim, so the policy cannot recognize it as a badge session and does not end it: it keeps
     refreshing without ever passing the PIN step, and a desk Employee-ID session that refreshes at
     least once a week never lapses, because each refresh restarts its 7-day window, so it could
     survive the policy indefinitely. Its writes carry no `extra_data.auth_methods` either, so in
     the audit trail they read like password sessions. Rotating the key ends every such session:
     - Generate a new value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`, at least
       32 characters, different from `SECRET_KEY`) and set it on `werco-api`
       (`railway variables set REFRESH_TOKEN_SECRET_KEY=<new value> --service werco-api`; a
       variable change triggers a redeploy). Set the same value on `werco-worker` too, which needs
       the variable to start but signs nothing with it.
     - Every refresh token is then refused, so each person signs in again once, when their current
       access token (≤ 15 minutes) runs out: desk users, solo kiosks and Badge Only sessions alike.
       Their new badge sessions carry `amr`. API tokens, wallboard display tokens, crew-station
       station tokens and badge-minted operator tokens are signed with `SECRET_KEY` and keep
       working. An MCP stdio bridge holding a `WERCO_ERP_REFRESH_TOKEN` needs a new sign-in.
     - It is needed once, for the sessions that predate this deploy. Sessions signed in after it
       carry `amr`, and turning the policy on ends their badge-only ones at the next refresh
       (`floor_pin_reauth_required`).
  3. **Then turn the policy on**, when the Floor PIN column shows no badge user still *Not set*:
     Admin Settings → **Badge Sign-in** → *Require a PIN for badge sign-in* (Admin). The answer's
     count of users without a PIN covers active accounts that have an employee ID, platform
     accounts excluded, so a desk user with an employee ID who never badges in still counts. Anyone
     still without a PIN is refused at the kiosk, the crew station and Badge Only sign-in with "No
     PIN is set for this badge. Ask a supervisor to set one."
  4. **A forgotten PIN or a PIN lock.** Wrong PINs add up until the person's next correct PIN,
     and every 5th locks the PIN for longer: 15 minutes, 2 hours, 24 hours, 48 hours, 96 hours,
     then 7 days for each further 5. **Every lock expires on its own.** A lock of 24 hours or more
     sends the company's Admins an in-app alert, *Floor PIN locked out*, which opens Users; Users
     lists every locked floor PIN above the table with its end time. **Unlock** there (or in
     Manage) lifts the lock early, resets the count and keeps the PIN; setting a new PIN also lifts
     it (same roles as step 1). The Users page's password Unlock clears the password lockout only.
     Anyone who knows a badge number can lock that person's PIN by typing wrong PINs, so treat a
     long-lock alert as a possible guessing attempt: check the `EMPLOYEE_LOGIN_FAILED` rows, and
     consider a new PIN rather than an unlock.
  5. **What a shop behind one internet address will see.** The desks and solo kiosks usually share
     one address, and with it a budget of **60 wrong PINs per 15 minutes** on the public sign-in.
     Correct PINs give their unit back, so ordinary sign-ins never use it up. If 60 wrong PINs
     (and attempts against locked PINs) pile up from that address inside 15 minutes, **PIN sign-in
     from the building pauses for up to 15 minutes**: everyone with a PIN is refused at the desk and
     the solo kiosk with "Too many wrong PINs from this network", and the pad offers only Back.
     People without a PIN (where the policy allows it), password sign-in and the crew stations
     keep working. If it trips, wait it out; there is no admin reset.
  6. **A leaked PIN: set a new one.** Setting a new PIN (or clearing the PIN) ends **every badge +
     PIN session of that person**, at a desk, a solo kiosk or a crew station, at its next request:
     the person is signed out and asked to sign in again with badge and PIN. Do not rely on
     deactivating the user instead: deactivation only pauses a session, and once the account is
     reactivated its old refresh tokens, still inside their 7-day window, work again.
- **Verify.** On production a read is enough: the query below returns six rows (the five `users`
  columns and `require_floor_pin`), the Users page loads with its Floor PIN column, and a badge
  with no PIN still signs in. In a test company, with an invented badge: set a PIN, sign in by
  Badge Only and get the PIN step, enter a wrong PIN ("Invalid badge or PIN"), then the right one.
  The Audit Log shows `FLOOR_PIN_SET`, `EMPLOYEE_LOGIN_FAILED` and `EMPLOYEE_LOGIN_SUCCESS`.

    ```sql
    SELECT table_name, column_name, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name IN ('users', 'companies')
       AND column_name LIKE '%floor_pin%'
     ORDER BY table_name, column_name;
    ```
- **Rollback.** Keep the migration on an ordinary application rollback: pre-130 code never reads
  the columns. Once `130` has run, an older image cannot boot until the database is stamped back,
  so from a checkout of this release run `alembic stamp 129_owned_property` before redeploying a
  pre-130 image (the next forward deploy re-runs 130 as a guarded no-op that keeps the PINs and
  the policy). **While the older code runs, sign-on is badge-only again**: it has no PIN step, so
  every badge signs in on the badge alone whatever the policy says, and its tokens carry no `amr`,
  so rows written then carry no `auth_methods`; after rolling forward again, those sessions are the
  pre-deploy kind, so repeat the `REFRESH_TOKEN_SECRET_KEY` rotation (owner runbook step 2) before
  relying on the policy. The downgrade is **lossy** and not a rollback step:
  it drops every PIN (each person needs a new one after a re-upgrade), every PIN lock, every
  promotion's reset flag and each company's policy (back to off). The `FLOOR_PIN_SET` and policy audit rows remain, but nothing
  re-derives the columns from them. See
  [API.md → Badge sign-on and the floor PIN](API.md#badge-sign-on-and-the-floor-pin-as9100d-852).

### Customer- and supplier-owned property (migration 129)

Apply **129_owned_property** after **128_po_flowdown** through the normal API boot migration path.
No environment variable, secret, cron or worker job is added.

- **What it does, and no backfill.** `inventory_items`, `tools` and `po_receipts` each gain
  `ownership` (`varchar(20) NOT NULL`, server default `'company'`, CHECK `ck_<table>_ownership`),
  `owner_customer_id` (FK `fk_<table>_owner_customer_id` to `customers`) and `owner_vendor_id` (FK
  `fk_<table>_owner_vendor_id` to `vendors`), and a CHECK `ck_<table>_owner_matches` pairing the kind
  with its owner. `inventory_items` and `tools` also get an index `ix_<table>_ownership` on
  `(company_id, ownership)`; `po_receipts` gets none, since nothing filters receipts by owner. No FK
  has an `ON DELETE`. No `UPDATE` runs: every existing row becomes `company` with both owner columns
  NULL, which is exactly what every row meant before, so company stock and company receipts behave
  as they did. All three tables already exist with RLS on, so the revision emits no RLS, `REVOKE` or
  role statement. Every object is guarded, so a re-run is a no-op.
- **Locks.** On PostgreSQL 11+ the `NOT NULL` column with a constant default is **metadata-only**
  (no table rewrite, no NOT NULL scan), and the two nullable owner columns are too. Each FK
  validates against an all-NULL column (a quick scan) and briefly takes SHARE ROW EXCLUSIVE on
  `customers` / `vendors` (blocks writes to them, not reads); each CHECK scans its table once; the
  two indexes are plain `CREATE INDEX`, not `CONCURRENTLY`. Each table is ACCESS EXCLUSIVE until the
  boot transaction commits, in the order `inventory_items`, `tools`, `po_receipts`, so inventory,
  tool and receiving reads and writes wait for the boot. The tables are small, so this is brief.
  `SET LOCAL lock_timeout = '5s'`; a timeout fails the boot with the database back at `128`, and
  redeploying is safe.
- **Order.** Run the migration before or with the code that maps the columns (the API boot does).
  Old code is safe on the new schema: it never selects the columns, and its inserts take the
  `'company'` default. New code on an un-migrated database fails every inventory, tool and PO receipt
  load, so deploy the **worker with or after the API**. Deploy the **API before the frontend**: an
  older API silently ignores the new `ownership` / owner fields, so a customer's material received
  from the new screens in that window would be filed as company stock with no error, and the
  ownership route and the list filters do not exist there.
- **Delivery retries across the deploy still replay.** The `POST /receiving/deliveries` idempotency
  fingerprint leaves out a line's ownership fields while they hold the company defaults, so a
  company delivery posted before the deploy and retried after it hashes as it did and replays its
  stored receipts.
- **Verify** in a test company, with invented names: receive a few units of a test part as
  customer-owned (an invented customer) at a location and lot where company stock of that part
  already sits. Expect a second stock row with a `Customer: <name>` chip on the Inventory page, an
  unchanged `GET /reports/inventory-value`, and `owned_by_others_on_hand` on that part in
  `GET /inventory/summary`. Receive a second line as customer-owned with **Requires Inspection**
  ticked, accept it in the inspection queue, and expect it on a customer-owned row too. Use
  **Change ownership** to return a row to the company with a reason and find the `inventory` UPDATE
  row in the Audit Log. On production a read is enough: `SELECT ownership, count(*) FROM
  inventory_items GROUP BY 1;` returns only `company` right after the deploy, and `SELECT conname
  FROM pg_constraint WHERE conrelid IN ('inventory_items'::regclass, 'tools'::regclass,
  'po_receipts'::regclass) AND conname LIKE '%owner%';` returns twelve names: per table
  `ck_<table>_ownership`, `ck_<table>_owner_matches`, `fk_<table>_owner_customer_id` and
  `fk_<table>_owner_vendor_id`.
- **Rollback.** Do not run the downgrade as a rollback step: it drops the columns, discarding which
  stock rows, tools and PO receipts belong to a customer or supplier, and who. For an application
  rollback keep the columns: from a checkout of this release run `alembic stamp 128_po_flowdown`
  before redeploying a pre-129 image (the next forward deploy re-runs 129 as a guarded no-op that
  keeps the markers), and roll the frontend back with the API, for the silent-drop reason above.
  **While stamped back, the old code cannot see the markers: it FIFO-draws customer- and
  supplier-owned rows as company stock, merges receipts into them, and counts them in inventory
  value, and an owned receipt it accepts out of inspection or releases from its hold lands as
  company stock.** Before stamping back, export the owned rows (`GET /inventory/?ownership=customer`
  and `?ownership=supplier`, or the inventory export's `ownership` column) so that anything drawn or
  merged during the window can be found and corrected, and keep the window short. See
  [API.md → Customer- and supplier-owned property](API.md#customer--and-supplier-owned-property).

### Purchase-order quality flow-down (migration 128)

Apply **128_po_flowdown** after **127_ncr_car_records** through the normal API boot migration path.
No environment variable, secret, cron or worker job is added.

- **Additive, no backfill.** It creates two tenant tables, `po_flowdown_profiles` (one row per
  company) and `po_quality_clauses`, each with RLS enabled and no policies, and table and sequence
  revoked from `PUBLIC`, and from `anon` / `authenticated` where those roles exist, matching model
  bootstrap. It adds two nullable JSON columns to `purchase_orders`, `quality_clause_codes` and
  `flowdown_snapshot`. No row is read or written. Both tables start empty, so no company prints a
  flow-down block until a person turns it on in **Purchasing → Quality flow-down**, and every
  existing purchase order prints exactly as before (an already-issued PO has no snapshot and never
  prints a block).
- **Locks.** `SET LOCAL lock_timeout = '5s'`. The new foreign keys briefly take SHARE ROW EXCLUSIVE
  on `companies` and `users` (blocks writes to them, not reads); each `ADD COLUMN` is metadata-only
  on PostgreSQL 11+ but takes a brief ACCESS EXCLUSIVE lock on `purchase_orders`. A timeout fails the
  boot with the database back where the boot started, and redeploying is safe.
- **Order.** Deploy the **worker with or after the API**, never before: the new code selects the two
  new `purchase_orders` columns on every PO load, so a new worker on an un-migrated database fails
  them (the MRP auto-PO job included). Old code is safe on the new schema. Deploy the API before the
  frontend: the Purchasing page's Quality flow-down tab, the Quality page's PO Flow-down tab and the
  PO clause checklist call routes the old API does not have.
- **Every PO's email fingerprint changes once.** The document-delivery source fingerprint of a
  purchase order covers its print data (which gains `quality_flowdown`) and its header columns
  (which gain `quality_clause_codes`), so every PO's fingerprint changes at this deploy. A PO email
  preview prepared before the deploy is refused at Send with **409** *"The document changed after
  this PDF was prepared. Create a fresh preview before sending."*; prepare it again. Re-opening the
  email composer for a PO whose email was already accepted prepares a new preview instead of showing
  the "already sent" replay; nothing goes out unless someone clicks Send, which would email the PO
  again. Both are harmless and happen once per PO.
- **Verify** in a test company, with invented text: in **Quality → PO Flow-down** (or **Purchasing →
  Quality flow-down**), turn the block on with a document reference and one clause marked
  *Pre-check on new purchase orders*. Print a draft PO and find the block after the totals. Send
  that PO, edit the clause's title, and print both again: the sent PO still shows the old title and
  a new draft shows the new one. The Audit Log shows `po_flowdown_profile` and `po_quality_clause`
  rows, and the sent PO's STATUS_CHANGE row carries `flowdown_snapshot_sha256`. `SELECT relname,
  relrowsecurity FROM pg_class WHERE relname IN ('po_flowdown_profiles', 'po_quality_clauses');` is
  true for both.
- **Rollback.** Keep the migration on an ordinary application rollback: older code never reads the
  new tables or columns. Once `128` has run, an older image cannot boot until the database is
  stamped back, so from a checkout of this release run `alembic stamp 127_ncr_car_records` before
  redeploying a pre-128 image (the next forward deploy re-runs 128 as a guarded no-op that keeps the
  data). The downgrade is **lossy** and is not a rollback step: it drops both columns and both
  tables, discarding every issued PO's frozen flow-down record (AS9100D 8.4.3 evidence), every
  clause choice, the catalog and the settings. Export the snapshots first if it is ever genuinely
  needed. See
  [API.md → Quality flow-down on the purchase order](API.md#quality-flow-down-on-the-purchase-order).

### NCR and CAR evidence columns (migration 127)

Apply **127_ncr_car_records** after **126_qms_company_id** through the normal API boot migration path.
No environment variable, secret, cron or worker job is added.

- **What it does.** Adds nullable columns only, with no default and no backfill. A pre-127 NCR or CAR
  simply never recorded them. On `ncrs`: `action_taken`, the customer concession
  (`concession_reference`, `concession_authorized_by`, `concession_date`), the re-verification
  (`reverification_result` with CHECK `ck_ncrs_reverification_result`, `reverification_notes`,
  `reverified_by` FK `fk_ncrs_reverified_by`, `reverified_at`), and `dispositioned_by` (FK
  `fk_ncrs_dispositioned_by`) / `dispositioned_at`. On `cars`: `effectiveness_result` (CHECK
  `ck_cars_effectiveness_result`), `effectiveness_verified_by` (FK `fk_cars_effectiveness_verified_by`),
  `effectiveness_verified_at`, `effectiveness_due`, `human_factors_review`, `similar_nonconformities_review`.
  Every object is guarded, so a re-run is a no-op.
- **Locks.** `SET LOCAL lock_timeout = '5s'`. Each table takes a brief exclusive lock (no rewrite, the
  columns are nullable with no default), and the three FKs briefly take SHARE ROW EXCLUSIVE on `users`.
  A timeout fails the boot with the database back at `126`; redeploying is safe.
- **Order.** Run the migration before or with the code that maps these columns (the API boot does).
  Old code runs safely on the new schema; new code fails on an unmigrated one.
- **Behavior change to tell Quality before the deploy.** Open NCRs and corrective CARs now need the new
  evidence to close (see [API.md → Quality](API.md#quality), the "NCR disposition evidence" and "CAR corrective-action
  evidence" notes). Werco's controlled CAR form gains boxes on the printed PDF, so its form revision
  should be bumped.
- **Rollback.** The downgrade drops every recorded re-verification, concession and effectiveness
  verification. For an application rollback keep the columns: from a checkout of this release run
  `railway run --service werco-api alembic stamp 126_qms_company_id` before redeploying a pre-127 image.
- **Verify** after the deploy: an NCR and a CAR detail screen open (their GETs answer 200), and the new
  NCR re-verification panel and CAR effectiveness panel render.

### QMS `company_id` repair (migration 126)

Apply **126_qms_company_id** after **125_work_order_comments** through the normal API boot migration
path. No environment variable, secret, cron or worker job is added, and no model or route changes.

- **What it does.** Production's `qms_standards`, `qms_clauses` and `qms_clause_evidence` have no
  `company_id` (their tables predate `TenantMixin` on the QMS models, and the database was stamped past
  `026`, which adds it), so every tenant-scoped QMS query fails. The revision adds the column exactly as
  `create_all` builds it: `integer NOT NULL`, FK `<table>_company_id_fkey` to `companies(id)` with no
  `ON DELETE`, index `ix_<table>_company_id`. Production holds zero rows in the three tables, so
  nothing is backfilled there. On any database that already has the column (every `create_all`
  database, every one that ran `026`) it reads the catalog and changes nothing.
- **Backfill and refusal** (only where rows exist without the column). A standard takes its creator's
  company (a superuser or `PLATFORM_ADMIN` creator does not count), a clause its standard's, evidence
  its clause's; a row left over gets the company only when the database holds exactly one. With two or
  more companies and an unattributable row, the revision raises, naming the tables, counts and ids: the
  boot fails, the transaction rolls back to `125`, and the old deployment keeps serving. A person
  decides the owner, sets it by hand (`ALTER TABLE <t> ADD COLUMN IF NOT EXISTS company_id integer;
  UPDATE <t> SET company_id = …`) and redeploys; the revision keeps values already set.
- **Locks.** `SET LOCAL lock_timeout = '5s'`. Where it repairs, the QMS tables are ACCESS EXCLUSIVE
  until the boot transaction commits and the FK briefly takes SHARE ROW EXCLUSIVE on `companies`
  (blocks writes to it, not reads); on empty tables this is milliseconds. A timeout fails the boot
  with the database back at `125`, and redeploying is safe.
- **Order.** None beyond the boot path: the code already on `main` expects the column.
- **Verify** after the deploy: the query below returns three rows, each `NO` with both names present,
  and the QMS Standards page loads (its list call answers 200 where it answered 500 before). A read
  is enough: there is no need to create a quality record on production to prove the repair.

    ```sql
    SELECT c.table_name, c.is_nullable, con.conname, ix.indexname
      FROM information_schema.columns c
      LEFT JOIN pg_constraint con
             ON con.conrelid = ('public.' || c.table_name)::regclass
            AND con.conname = c.table_name || '_company_id_fkey'
      LEFT JOIN pg_indexes ix
             ON ix.schemaname = 'public' AND ix.indexname = 'ix_' || c.table_name || '_company_id'
     WHERE c.table_schema = 'public' AND c.column_name = 'company_id'
       AND c.table_name IN ('qms_standards', 'qms_clauses', 'qms_clause_evidence')
     ORDER BY 1;
    ```

- **Rollback.** Keep the column: the pre-126 code needs it too. Once `126` has run, an older image
  cannot boot until the database is moved back to `125`; from a checkout of this release run `alembic
  stamp 125_work_order_comments` (or `alembic downgrade -1`, which is equivalent here) before
  redeploying a pre-126 image. The downgrade deliberately leaves the column, FK, index and values in
  place, because they belong to revision `125`'s schema; the next forward deploy re-runs `126` as a
  no-op.

### Work-order comments, mentions and watchers (migration 125)

Apply **125_work_order_comments** after **124_maintenance_assets** through the normal API boot
migration path. No environment variable, secret, cron or worker job is added; the two notification
events (`comment.mention`, `comment.added`) ride the existing outbox and worker.

- **Purely additive.** It creates three tenant tables — `comments` (soft-deleted, never physically
  removed), `comment_mentions` and `entity_watchers` — with their indexes and unique constraints, and
  touches no existing table, enum or row: there is no backfill. Each new table gets RLS enabled with
  no policies, and table and sequence revoked from `PUBLIC`, and from `anon` / `authenticated` where
  those roles exist, matching model bootstrap. There is deliberately no CHECK on `entity_type`; v1
  enables `work_order` in the service layer only.
- **Locks.** The revision runs `SET LOCAL lock_timeout = '5s'`. Creating the new foreign keys takes a
  brief SHARE ROW EXCLUSIVE lock on `users` and `companies` (it blocks writes to them, not reads),
  held until the boot transaction commits; the tables are new and empty, so the DDL itself is quick.
  A long transaction holding either table fails the boot after 5 seconds with the database back
  where the boot started (a boot that also ran **121** has the caveat under
  [One boot, two transactions](#job-cost-subcontract-element-migration-121)), and redeploying is safe.
- **Order.** The migration ships with the API, which the boot path does. Deploy the **worker with the
  API**: an older worker has no catalog source for `comment_mentioned` / `comment_added`, so it marks
  those events handled without notifying anyone, and a comment posted before it updates notifies
  nobody. Deploy the API before the frontend: the work-order detail page's new Comments panel calls
  routes the old API does not have.
- **Verify** in a test company: on a work order, post a comment that @mentions another user. The
  comment appears in the thread, both of you show as watching, and the mentioned user gets a "You
  were mentioned" notification (and email, by default) that carries no comment text. The Audit Log
  shows a `comment` CREATE row. `SELECT relname, relrowsecurity FROM pg_class WHERE relname IN
  ('comments', 'comment_mentions', 'entity_watchers');` is true for all three.
- **Rollback.** Keep the migration on an ordinary application rollback: older code never reads these
  tables. Once `125` has run, an older image cannot boot until the database is stamped back, so from
  a checkout of this release run `alembic stamp 124_maintenance_assets` before redeploying a pre-125
  image (the next forward deploy re-runs 125 as a guarded no-op). The downgrade is **lossy** and is
  not a rollback step: it drops all three tables, so every comment thread and watch list is lost.
  Export them first if they matter. The `audit_log` rows that recorded each comment write remain,
  but nothing rebuilds the tables from them. See
  [API.md → Comments and watchers](API.md#comments-and-watchers).

### Approved Supplier List, equipment assets and maintenance downtime (migrations 123 and 124)

Apply **123_vendor_asl** (after **122_car_form_quote_rev**), then **124_maintenance_assets**, through
the normal API boot migration path. No environment variable, secret, cron or worker job is added.

- **123** adds six nullable Approved Supplier List columns to `vendors` (`approval_status`,
  `approved_scopes`, `approval_bases`, `reevaluation_due`, `last_performance_evaluation`,
  `approval_notes`), the CHECK `ck_vendors_approval_status`, the index
  `ix_vendors_company_reevaluation_due`, and `parts.supply_scope`. No table, so no RLS statement. **No
  backfill:** every vendor reads "not on the ASL" until the ASL import or **Update approval** records a
  status, and `is_approved` is untouched until then.
- **124** creates the tenant table `maintenance_assets` (RLS enabled with no policies; table and
  sequence revoked from `PUBLIC`, and from `anon` / `authenticated` where those roles exist, matching
  model bootstrap). On `maintenance_schedules`, `maintenance_work_orders` and `maintenance_logs` it
  adds `asset_id` (FK and index), makes `work_center_id` nullable and adds the CHECK
  `ck_<table>_one_target` (exactly one target; every existing row has a work center, so it validates).
  It adds `downtime_events.maintenance_work_order_id` (FK and index) and `downtime_events.maintenance_adopted`
  (Boolean NOT NULL DEFAULT false — a constant default, so no table rewrite on PostgreSQL 11+). No data changes.
- **Locks.** Both revisions run `SET LOCAL lock_timeout = '5s'`. They take ACCESS EXCLUSIVE locks on
  `vendors`, `parts`, the three maintenance tables and `downtime_events`, held until the boot
  transaction commits; the CHECKs validate and the indexes build inside it. A long transaction holding
  any of those tables fails the boot after 5 seconds. When that boot started at 121 or later, the
  database is back where it started, the old deployment keeps serving and redeploying is safe. When
  the same boot also ran **121** (a stacked release deployed in one go), it is not: 121's autocommit
  step had already committed 119 and 120, so the database is left at `120_production_calendar` and an
  older image cannot start until it is stamped back. Stamp before that image restarts (see
  [One boot, two transactions](#job-cost-subcontract-element-migration-121) under 121).
- **Order.** The migration ships with the API or ahead of it: the old API ignores the new columns and
  always writes a work center on maintenance rows. Deploy the API before the frontend (the normal CI
  order): the new Supplier Scorecards ASL tab, the Create PO check, the Maintenance Assets tab and both
  import wizards call routes the old API does not have.
- **Verify** in a test company: Supplier Scorecards → Approved Supplier List → **Update approval** on a
  vendor (a `vendor` UPDATE audit row carries the reason); preview an ASL file and confirm it lists
  only matched vendors; mark a test vendor Disqualified and confirm a purchase order to it is refused
  (409); add an equipment asset and a schedule on it; start a work-center maintenance work order with
  *requires shutdown* and confirm `/downtime` shows an open event, then complete it and confirm the
  event resolves. `SELECT relrowsecurity FROM pg_class WHERE relname = 'maintenance_assets';` is true.
- **Rollback.** Keep both migrations on an ordinary application rollback, with these cautions. Once
  `123` has run, an older image cannot boot until the database is stamped back, so from a checkout of
  this release run `alembic stamp 122_car_form_quote_rev` before redeploying a pre-123 image (the next
  forward deploy re-runs both as guarded no-ops). Old code cannot start or complete a maintenance
  work order that targets an **asset**: its event-log insert carries no target, so the 124 CHECK
  refuses it (500, nothing saved). Old code does not
  resolve the downtime event a maintenance work order opened; resolve those on `/downtime`. Old code
  also neither refuses a purchase order to a Disqualified vendor nor stops the vendor edit from
  contradicting an ASL status. A deliberate **124** downgrade refuses, naming the counts, while any
  schedule, work order or log targets an asset (re-point or remove those first), and otherwise drops
  the asset register; a **123** downgrade drops every recorded ASL value and part scope. Export them
  first. See
  [API.md → Approved Supplier List on the vendor record](API.md#approved-supplier-list-on-the-vendor-record)
  and [API.md → Maintenance](API.md#maintenance-pm-schedules-maintenance-work-orders-event-log).

### CAR form fields, quote line revisions and the FAI log (migration 122)

Apply additive **122_car_form_quote_rev** after **121_job_cost_subcontract** through the normal API
boot migration path. It adds 21 nullable columns to `cars` (the controlled CAR form's fields, with
the foreign keys `fk_cars_part_id`, `fk_cars_customer_id`, `fk_cars_vendor_id` and
`fk_cars_quality_approved_by`, the index `ix_cars_part_id` and the CHECK
`ck_cars_initiation_reason`) and one, `part_revision`, to `quote_lines`. It creates no table and runs
no RLS, revoke or role statement (both tables have had RLS since `059`). It reads and writes no row,
so there is no backfill. Each `ADD COLUMN` is metadata-only on PostgreSQL, but each statement still
takes a brief ACCESS EXCLUSIVE lock, bounded by `SET LOCAL lock_timeout = '5s'`. A timeout fails the
boot. When the same boot also ran 121, the database is then left at `120_production_calendar`, not
where it started, and an older image must be stamped back before it restarts (see
[One boot, two transactions](#job-cost-subcontract-element-migration-121) under 121). No environment
variable, secret, cron or worker job is added. The FAI log of record and its export need no
migration.

Deploy order: the migration must run before or with the API, which the boot path does. Old code is
safe on the new schema. New code on an un-migrated database fails every CAR and quote-line load, so
deploy the API before or with the worker.

Tell users before the deploy:

- **Every existing quote line has no drawing revision.** Until each line's Rev is entered (or `N/A`
  on a line with no drawing), **Send**, the quote email and the PDF of a quote not yet issued
  (draft, pending, rejected, expired) answer **409** naming the lines. Revisions can be entered only
  on a draft or pending quote. An already sent, accepted or converted quote still reprints its PDF
  as issued (its Rev column shows `-`). To email one, move it back to draft (possible only when no
  line is converted to a work order), enter its revisions and send it again.
- **Quote email previews prepared before the deploy cannot be sent.** The send answers **409**
  "The document changed after this PDF was prepared". Create a fresh preview.
- **Quote emails still in flight at the deploy keep their quote's status.** A delivery that was
  `sending` or `unknown` when the deploy landed and is later accepted, or reconciled as accepted,
  does not move its quote to SENT: the snapshot it was prepared from no longer matches (the delivery
  says "The source changed; its workflow status was retained."). Mark that quote sent by hand once
  its revisions are entered.
- **The quote PDF's FOB term comes from the company address.** It prints "FOB *City*, *ST*." when
  the last address line ends in "City, ST [ZIP]", else "FOB Origin.". Check the company record.

Verify in a test company:

- Open a CAR with a reason, a part and a vendor. **Print CAR**, then **Record quality approval**. A
  second approval should be refused (409). The Audit Log should show the `car` CREATE and APPROVE
  rows.
- On a draft quote, enter a Rev on each line and send it. Before the Rev is entered, **Send** should
  be refused with the line numbers.
- Disposition an FAI as Passed, record its customer approval, and find it in the FAI tab's **Log**
  view. As an Admin or Manager, export the log and confirm an `fai` EXPORT row in the Audit Log.

Rollback: keep the columns. Older code never reads them. Once `122` has run, an older image cannot
boot until the database is stamped back, so from a checkout of this release run
`alembic stamp 121_job_cost_subcontract` before redeploying it. The values stay, and the next forward
deploy re-runs `122` as a guarded no-op. The downgrade is **not** a rollback step: it drops every
recorded CAR form field, Quality Manager approval and quote-line revision. The audit rows that
recorded them remain, but nothing rebuilds the columns from them. See
[API.md → Quality](API.md#quality) and [API.md → Quotes](API.md#quotes).

### Job cost subcontract element (migration 121)

Apply additive **121_job_cost_subcontract** after **120_production_calendar** through the normal API
boot migration path. It adds `job_costs.actual_subcontract_cost` (FLOAT NOT NULL DEFAULT 0 —
metadata-only on PostgreSQL 11+, every existing row reads 0, which is true: no subcontract entry could
exist before it) and, where `cost_entries.entry_type` is the native `costentrytype` enum, the
`SUBCONTRACT` label (in its own autocommit step; a database whose column is migration 024's VARCHAR
has no type and needs no label). No table, environment variable, secret or worker job is added.
Deploy the API before or with the ARQ worker. **Rollback:** pre-121 code cannot load a cost entry of
type `SUBCONTRACT`; before redeploying an older image, list them (`SELECT id, company_id, job_cost_id
FROM cost_entries WHERE entry_type = 'SUBCONTRACT'`) and re-key them to `other` through the API
(delete + re-add, both audited). The enum label stays on downgrade.

**One boot, two transactions.** The API boot runs every pending revision in one transaction, but
121's autocommit step first **commits everything that boot has already applied**. So a boot that
takes the database from 118 or 119 through 121 (and on to 122–125 in a stacked release) is not
all-or-nothing. If 121's `job_costs` ALTER, or any later revision, then fails (a `lock_timeout` behind
a long transaction, say), the boot fails with the database left at **120_production_calendar**:
119's columns, 120's tables and the `SUBCONTRACT` label stay; 121's column and every later revision
roll back. The old deployment keeps serving only until its container restarts. An image whose
migrations stop before 120 runs `alembic upgrade head` on start, fails `Can't locate revision
identified by '120_production_calendar'`, and crash-loops. Right after such a failed boot, stamp the
database back to the head of the image that is still serving, from a checkout of this release:
`alembic stamp 119_api_token_scope` for the 119 image, `alembic stamp 118_dashboard_layouts` for an
image before it. Then fix the cause and redeploy. The forward boot re-runs 119 and 120 as guarded
no-ops and applies 121 onward. Releasing 119, 120 and 121 as separate deploys avoids the split.

### Production calendar (migration 120)

Apply additive **120_production_calendar** after **119_api_token_scope** through the normal API boot
migration path. It creates four empty
tenant tables — `production_calendar_settings`, `shift_patterns`, `shift_pattern_work_centers`,
`shift_definitions` — and adds the nullable `oee_records.shift_id` FK + index with **no backfill**
(legacy free-text OEE `shift` labels stay). No calendar is seeded: until an Admin saves one, every
company's production day is its company-local calendar day. PostgreSQL RLS and table/sequence revokes
from `PUBLIC`, `anon` and `authenticated` match model bootstrap. No environment variable, secret or
worker job is added; the 02:30 OEE cron keeps its schedule and now targets each company's previous
production day. **Behavior change on deploy, for every tenant:** OEE, reports, downtime filters,
job-cost labor dates and analytics move from UTC-date day boundaries to company-local (production)
days (the export, job-cost and analytics **calendar-date** filters move to company-local days in the
same release — see `DEPLOYMENT_RUNBOOK.md` → Company-local dates and export datetime cells). Once a
calendar is saved, the wallboard TODAY bar and the dashboard's "Operations Completed Today" reset at
the production-day start instead of Central midnight. After deploy, an Admin applies Werco's calendar
(Admin Settings → Production Calendar, or `docs/API.md` → Production calendar & shifts → worked
example), then runs `POST /production-calendar/working-calendars/sync` as a dry run, reviews it, and
commits — the commit changes scheduling capacity for every covered work center.

### Read-only and AI-external API tokens (migration 119)

Apply additive **119_api_token_scope** after **118_dashboard_layouts** through the normal API boot
migration path. It adds two columns to the existing `api_tokens` table, and one CHECK, each
guarded on its own:

- `scope VARCHAR(16) NOT NULL DEFAULT 'full'`;
- `ai_external BOOLEAN NOT NULL DEFAULT false`;
- `ck_api_tokens_scope`, which admits only `'full'` and `'read_only'`.

There is no backfill. Every existing token reads `full` / `false` and behaves exactly as before,
its JWT included. No new table, so there is no RLS statement. On PostgreSQL the DDL runs under
`SET LOCAL lock_timeout`.

One new setting, `WERCO_MCP_READ_ONLY_DENY_TAGS`, has a working default; leave it unset. The API
**refuses to start** if it names a tag no route carries, because a misspelled tag would deny
nothing ([ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#mcp-model-context-protocol-door-and-bridge)).
No secret, cron or worker job is added. The export-control middleware is part of the API image.

Deploy the API before the frontend: **Admin → API Tokens** and the Customers **Export control**
panel call the new fields and routes. The same release also gates
`PUT /shop-floor/operations/{id}/start` and `POST /shop-floor/operations/{id}/complete` to Admin /
Manager / Supervisor / Operator / Quality. A Viewer or Shipping user who used them is refused
**403**; operators and both kiosks are unchanged
([RBAC → Work Orders](RBAC_PERMISSIONS.md#work-orders)).

Verify in a test company:

1. **Read-only.** Mint `{"scope": "read_only", "ai_external": false}`. A `GET` succeeds. A `POST`
   answers **403** `Read-only API token cannot modify data.`, and `GET /api/v1/users/me` answers
   **403** `Read-only API token cannot access this resource.`.
2. **AI egress off.** Mint `{"scope": "read_only"}` (AI-external by default). With the company's
   AI egress off it answers **403** `AI egress is disabled …` on every route, and at `/mcp` when
   the door is enabled.
3. **Redaction.** As an Admin, add an **ITAR controlled** contract to a customer (Customers →
   Export control). The Audit Log shows a `customer_contract` CREATE row. With that test company's
   AI egress on, the token from step 2 reads that customer's work order with `[export-controlled]`
   text, and `controlled_access_events` gains a row.
4. **Existing tokens.** `SELECT scope, ai_external, count(*) FROM api_tokens GROUP BY 1, 2;` shows
   every pre-existing row as `full` / `false`.

**Rollback.** Older code ignores both columns and the narrowing claims, so it accepts a read-only or
AI-external token **as a full token** and redacts nothing. **Revoke every narrowed token before
rolling the API back** (Admin → API Tokens). A deliberate 119 downgrade drops both columns and the
CHECK, and with them the record of which tokens were narrowed, so it **refuses** (raises before any
DDL, naming the count) while any un-revoked read-only or AI-external token exists: revoke them
first (`SELECT id, label FROM api_tokens WHERE (scope <> 'full' OR ai_external) AND revoked =
false`). An offline `--sql` downgrade cannot check: run that query by hand. A code-only rollback
(database left at 119) is not checked at all, so the revoke step is still yours. See
[EXPORT_CONTROL_AI_EGRESS.md](EXPORT_CONTROL_AI_EGRESS.md).

### Saved dashboard layouts

Apply additive **118_dashboard_layouts** after **117_operation_short_declared** through
the normal API boot migration path. It creates an empty tenant/user `dashboard_layouts`
table with a unique company/user key, a `version >= 1` check and indexes on `company_id`
and `user_id`. No layout is seeded: until a person saves one, reads return
`layout: null` without creating rows and every role keeps the default dashboard.
PostgreSQL RLS and table/sequence revokes from `PUBLIC`, `anon`, and `authenticated`
match model bootstrap. No environment variable, secret, cron or worker job is added.

Deploy order is free. The frontend tolerates an API that does not have the routes yet:
a 404 (or 405) from `GET /dashboard/layout` renders the default dashboard with
**Customize** hidden. An older frontend never calls the new routes.

Verify in a test company: open `/`, press **Customize**, add a panel from **Add widgets**
and press **Done**, then reload in another browser and see the same layout; open edit mode
in two tabs, press **Done** with a change in one and then in the other, and get the
"changed in another tab" refusal (a tab that was merely left open is not stale enough:
Customize re-reads the layout on entry); **Reset to default**. The Audit Log should show
`dashboard_layout` CREATE and UPDATE rows. Confirm RLS with
`SELECT relrowsecurity FROM pg_class WHERE relname = 'dashboard_layouts';` (true).

Keep the table on ordinary application rollback: older code ignores it and renders
the fixed dashboard. A deliberate 118 downgrade drops every saved layout, returning
everyone to the default; the audit rows recording each change remain. See
[API.md → Personal dashboard layout](API.md#personal-dashboard-layout) and
[DASHBOARD.md → Customizing the dashboard](DASHBOARD.md#customizing-the-dashboard).

### Existing ERP frontend: Fabrication Quoting

The fabrication estimator lives at [wercomfg.app/fabrication-quotes](https://wercomfg.app/fabrication-quotes), under **Sales & Quoting → Fabrication Quoting**. It uses the existing ERP authentication, API, Railway services and Vercel project. Old estimator URLs redirect here. See [the implementation and operating guide](FABRICATION_QUOTING.md).

Use the existing GitHub Actions combined-release path. It validates the backend, frontend and container images, applies migrations **106** and **107** through normal API startup, verifies the exact API release, deploys the shared worker, and promotes the matching frontend. Historical quote tables and migrations remain intact.

Both API Dockerfiles install the hash-locked CAD/document interpreter at `/opt/werco-quote-worker` and set `WERCO_QUOTE_WORKER_PYTHON`. DXF, STEP, PDF and nesting processing run through bounded API subprocesses. CI exercises this interpreter without network access in a read-only container; the PostgreSQL verifier checks the new migrations and immutable history.

The old quote-nesting Node runtime, relay job and quoting APIs have been removed. That release set production `WORKER_CRON_JOBS=none`, preserving the prior suppression of unrelated schedules. The [Hank follow-up rollout](#hank-follow-up-worker) explicitly enables only its new check. Existing shared ARQ queue functions remain available. A generic heartbeat verifies the active worker deployment, exact release and fresh successful Redis publication; it does not depend on enabled cron jobs. Do not re-enable a retired relay name.

Keep the existing archive-root separation: API uploads from `backend/`, frontend from `frontend/`, worker from the repository root. Railway's ignore rules must retain frontend `public/` assets and the stamped `public/release.txt`. The existing SPA rewrite serves direct workspace URLs.

After rollout, verify `wercomfg.app/release.txt` matches the release SHA, the new route loads, signed-out users reach login, and unauthorized users cannot open the estimator. With a reviewed shop package, verify file evidence, save/reopen, approval, draft customer-quote handoff and historical revision access. The application does not seed shop rates or invent supplier prices.

Production laser-nest import/dispatch and **Warehouse → Inventory → Piece observations** remain independent ERP functions. Historical quote-nesting evidence remains in its original database tables; there is no destructive data migration.

### Option 1: Docker Compose (Simpler)

1. **Use the production compose file with Supabase configured:**
   ```yaml
   services:
     redis:
       image: redis:7-alpine
       restart: always

     backend:
       build: ./backend
       ports:
         - "8000:8000"
       environment:
         - DATABASE_URL=${DATABASE_URL}
         - DATABASE_PROVIDER=supabase
         - REDIS_URL=redis://redis:6379/0
       depends_on:
         - redis
       restart: always

     frontend:
       build: ./frontend
       ports:
         - "3000:80"
       restart: always

   ```

2. **Start services:**
   ```bash
   docker-compose -f docker-compose.prod.yml up -d
   ```

3. **Run database migrations:**
   ```bash
   docker-compose -f docker-compose.prod.yml exec backend alembic upgrade head
   ```
   > On a **brand-new, empty** database, `alembic upgrade head` alone will fail —
   > follow the bootstrap order in [Database Setup](#database-setup) (create_all →
   > `alembic stamp <baseline>` → incremental `upgrade head`) first. This step is
   > the standard path only once the DB has been bootstrapped.

### Option 2: Systemd Services (Recommended for Production)

#### Backend Service

Create `/etc/systemd/system/werco-erp-backend.service`:

```ini
[Unit]
Description=Werco ERP Backend
After=network.target postgresql.service

[Service]
Type=notify
User=werco
Group=werco
WorkingDirectory=/opt/werco-erp/backend
Environment="PATH=/opt/werco-erp/backend/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/werco-erp/backend/.env
ExecStart=/opt/werco-erp/backend/venv/bin/gunicorn app.main:app \
    --workers 4 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --access-logfile /var/log/werco-erp/backend-access.log \
    --error-logfile /var/log/werco-erp/backend-error.log \
    --log-level warning

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### Frontend Service

Create `/etc/systemd/system/werco-erp-frontend.service`:

```ini
[Unit]
Description=Werco ERP Frontend
After=network.target

[Service]
Type=simple
User=werco
Group=werco
WorkingDirectory=/opt/werco-erp/frontend
ExecStart=/usr/bin/node /opt/werco-erp/frontend/node_modules/.bin/serve \
    -s build -l 3000

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### Start services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable werco-erp-backend werco-erp-frontend
sudo systemctl start werco-erp-backend werco-erp-frontend
```

## Nginx Configuration

Create `/etc/nginx/sites-available/werco-erp`:

```nginx
# Rate limiting
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;

# Upstream servers
upstream backend {
    server 127.0.0.1:8000;
}

upstream frontend {
    server 127.0.0.1:3000;
}

# HTTP to HTTPS redirect
server {
    listen 80;
    server_name werco-erp.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

# HTTPS server
server {
    listen 443 ssl http2;
    server_name werco-erp.yourdomain.com;

    # SSL certificates
    ssl_certificate /etc/letsencrypt/live/werco-erp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/werco-erp.yourdomain.com/privkey.pem;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Frontend
    location / {
        proxy_pass http://frontend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Backend API
    location /api/ {
        limit_req zone=api burst=20 nodelay;

        proxy_pass http://backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Health check endpoint
    location /health {
        proxy_pass http://backend;
        # Forward the real client Host (not the "backend" upstream name) so the
        # backend's TrustedHostMiddleware allowlist (ALLOWED_HOSTS) sees a real
        # hostname. See docs/ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header
        proxy_set_header Host $host;
        access_log off;
    }
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/werco-erp /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Trusting forwarded headers (required behind any TLS proxy)

Setting `X-Forwarded-Proto` at the proxy (above) is only half of it — the ASGI server must
also be told to **trust** that header, or it ignores it. Uvicorn honors the forwarded scheme
only when the immediate peer is in `forwarded_allow_ips` (default `127.0.0.1`); a proxy
(nginx, Railway's edge, a load balancer) is almost never localhost, so the default drops it.
When the scheme is dropped, `request.url.scheme` stays `http` and FastAPI's automatic
trailing-slash **307 redirect** (e.g. `GET /api/v1/work-centers` → `/api/v1/work-centers/`)
emits an `http://` `Location`. A browser on an `https` page refuses to follow the downgrade,
so the request fails with **status 0** — this silently broke the OEE dashboard.

The launch commands (`backend/Dockerfile`, `nixpacks.toml`, `start.sh`) therefore pass
`--proxy-headers --forwarded-allow-ips="*"`. In addition, both `backend/Dockerfile` and the
gunicorn image (`Dockerfile.prod`) set `FORWARDED_ALLOW_IPS=*` in the environment (uvicorn —
and gunicorn's UvicornWorker — read it independently of the start command). The env var is
belt-and-suspenders: **Railway can override the container start command**, in which case the
`--forwarded-allow-ips=*` CLI flag never applies and the `http://` trailing-slash redirect
re-breaks — so trusting the proxy via the environment keeps the fix in force regardless of
how the container is launched. `"*"` is acceptable because the container is only reachable
through the proxy; pin it to the proxy's IP/CIDR if the app is ever directly reachable. A
regression guard lives in `backend/tests/test_deploy_proxy_headers.py`.

## SSL Certificates (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d werco-erp.yourdomain.com
```

## Database Setup

1. **Create database user:**
   ```sql
   CREATE USER werco_prod_user WITH PASSWORD 'strong_password';
   CREATE DATABASE werco_erp OWNER werco_prod_user;
   ```

2. **Bootstrap the schema (first time, empty database):**

   A bare `alembic upgrade head` against an **empty** database is **not** the
   supported path and will fail — the core tables are created by
   `Base.metadata.create_all()` on first app boot (`001` only adds indexes), and
   `002_add_laser_press_brake_types.py` runs `ALTER TYPE workcentertype ...`,
   which errors if the enum type doesn't exist yet. Instead:
   ```bash
   cd /opt/werco-erp/backend
   source venv/bin/activate

   # 1. Create the schema (first app boot creates it via create_all; or do it explicitly)
   python -m scripts.seed_data            # calls create_all (+ seeds demo data)

   # 2. Mark the DB as at the migration baseline -- 058, NOT head: 059 (RLS hardening) and
   #    060 (audit_log immutability triggers) build what create_all cannot, so 059+ must
   #    actually run (each is guarded to no-op over create_all's objects)
   alembic stamp 058_process_sheets

   # 3. Apply migrations newer than the baseline
   alembic upgrade head
   ```
   See `docs/DEVELOPMENT.md` → "Database Migrations" → "Bootstrap order" for
   details. After bootstrap, normal incremental `alembic upgrade head` is the
   standard path.

3. **Set up automatic backups:**
   ```bash
   # Add to crontab
   0 2 * * * /usr/bin/python3 /opt/werco-erp/scripts/backup_database.py >> /var/log/werco-erp/backup.log 2>&1
   ```

## Monitoring & Logging

### Log Rotation

Create `/etc/logrotate.d/werco-erp`:
```
/var/log/werco-erp/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0640 werco werco
    sharedscripts
}
```

### Monitoring Setup

- **Sentry** for error tracking (configured via `SENTRY_DSN`). Performance transactions
  are sampled at `SENTRY_TRACES_SAMPLE_RATE` (default `0.1`) — that knob governs traces
  **only**, never error capture; every exception is reported at any value. Events are
  tagged with `APP_RELEASE`, the commit SHA CI stamps into the artifact.
  - Every request's events are also tagged `correlation_id`. This is the `X-Correlation-ID` value,
    which is also the `reference` a user quotes from a 500 (see
    [API.md → Internal Server Error (500)](API.md#internal-server-error-500)). A caller-supplied
    `X-Correlation-ID` is adopted only if it is 1–64 characters of `A–Z a–z 0–9 . _ -`; anything
    else is replaced by a server-generated id. An unhandled 500 currently produces **two** events,
    both tagged with the same `correlation_id`: the handler's own and one from the request-logging
    middleware's log line, which Sentry's dedupe does not catch across that middleware's task.
  - Once a user or API token authenticates through `get_current_user`, the events also carry
    `company_id` (the **active** company, which differs from the home company for a platform admin
    who switched context), `user_id` and `role`. These are ids and the role only, never a name or an
    email.
  - A reconcile-on-read side-effect failure is reported tagged `reconcile_step`. A leg that only
    hit a session an earlier failure had already broken (`PendingRollbackError`) logs a WARNING and
    sends no event, so one broken reconcile is one event, not one per leg.

  See
  [ENVIRONMENT_VARIABLES.md → Monitoring & Error Tracking](ENVIRONMENT_VARIABLES.md#monitoring--error-tracking)
- **Prometheus + Grafana** for metrics monitoring
- **Health checks**: `GET /health`; `GET /health/detailed` additionally reports the
  running commit at `checks.application.release`. The frontend reports its own commit as
  the plain-text body of `GET /release.txt`. Both are what CI polls to prove a deploy
  landed — compare the **body**, never the status code: nginx's SPA fallback answers a
  missing `/release.txt` with `200` and `index.html`. See
  [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md#change-control--production-deploy-model)
- **Application logs**: Check `/var/log/werco-erp/`

## Backup Strategy

### Automatic Backups

```bash
# Daily database backup at 2 AM
0 2 * * * /usr/bin/python3 /opt/werco-erp/scripts/backup_database.py

# Weekly full backup on Sundays at 3 AM
0 3 * * 0 /usr/bin/rsync -avz /opt/werco-erp /backups/werco-erp-weekly

# Monthly cleanup of old backups
0 4 1 * * /usr/bin/find /backups -name "*.sql.gz" -mtime +30 -delete
```

### Backup Locations

- **Local**: `/backups/`
- **S3**: Configured via AWS credentials
- **Retention**: 30 days

## Scaling Considerations

### Horizontal Scaling

1. **Load balancer**: Use Nginx or HAProxy
2. **Multiple backend instances**: Use `gunicorn --workers N`
3. **Session storage**: Use Redis for shared sessions
4. **Database**: Use connection pooling, consider read replicas
5. **MCP door** (if enabled): stateless — every `POST /mcp` is self-contained, so no sticky sessions and no shared session store are needed across workers or instances

### Vertical Scaling

- Increase server resources (CPU, RAM)
- Optimize database queries
- Add indexes
- Enable caching

## Security Checklist

- [ ] Change default passwords
- [ ] Use strong, random SECRET_KEY
- [ ] Enable firewall (UFW)
- [ ] Configure SSL/TLS
- [ ] Set up rate limiting
- [ ] Enable error monitoring (Sentry)
- [ ] Regular security updates
- [ ] Restrict database access
- [ ] Use fail2ban for brute force protection
- [ ] Regular backup verification
- [ ] Enable audit logging

## Troubleshooting

### Backend not starting

```bash
# Check logs
sudo journalctl -u werco-erp-backend -f

# Check database connection
psql -h localhost -U werco_prod_user -d werco_erp

# Check port availability
sudo netstat -tulpn | grep 8000
```

### Frontend not loading

```bash
# Check build artifacts
ls -la /opt/werco-erp/frontend/build/

# Check Nginx
sudo nginx -t
sudo systemctl status nginx
```

### Performance issues

```bash
# Check system resources
htop

# Check database performance
psql -c "SELECT * FROM pg_stat_activity;"

# Check Redis
redis-cli INFO stats
```

## Updates & Maintenance

### Update Application

```bash
# Pull latest code
cd /opt/werco-erp
git pull origin main

# Backend
cd backend
source venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
alembic upgrade head
sudo systemctl restart werco-erp-backend

# Frontend
cd /opt/werco-erp/frontend
npm ci --include=dev
npm run build
sudo systemctl restart werco-erp-frontend
```

### Rolling Updates (Zero Downtime)

1. Deploy new version alongside running instance
2. Switch Nginx upstream to new instance
3. Test new instance
4. Remove old instance

## Disaster Recovery

1. **Have backups**: Database and application code
2. **Document recovery procedures**
3. **Regular restore tests**
4. **Monitoring alerts for failures**
5. **Failover procedures documented**

## Support

For production issues:
- Check logs: `/var/log/werco-erp/`
- Monitor: Sentry dashboard
- Health check: `https://werco-erp.yourdomain.com/health`
- Documentation: `docs/DEVELOPMENT.md`
