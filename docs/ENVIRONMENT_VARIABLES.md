# Environment Variables Reference

This document describes all environment variables used by Werco ERP. Use this as a reference when configuring your development or production environment.

## Quick Start

1. Copy `.env.example` to `.env` in the project root
2. Copy `backend/.env.example` to `backend/.env`
3. Copy `frontend/.env.example` to `frontend/.env`
4. Fill in required values (marked with *)

## Backend Configuration

### Database

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes* | - | Supabase Postgres connection string from Project Settings > Database. Format: `postgresql://user:password@host:port/database` |
| `DATABASE_PROVIDER` | No | `supabase` | Database provider label used by health checks and deployment validation |
| `SUPABASE_DATABASE_URL` | No | - | Supabase Postgres URL alias; overrides a non-Supabase `DATABASE_URL` if injected by the platform |
| `SUPABASE_POSTGRES_URL` | No | - | Supabase Postgres URL alias; overrides a non-Supabase `DATABASE_URL` if injected by the platform |
| `POSTGRES_URL` | No | - | Common Supabase/Vercel Postgres URL alias; overrides a non-Supabase `DATABASE_URL` when it points to Supabase |
| `POSTGRES_PRISMA_URL` | No | - | Common pooled Postgres URL alias; used when it points to Supabase |
| `POSTGRES_URL_NON_POOLING` | No | - | Common direct Postgres URL alias; used when it points to Supabase |
| `SUPABASE_URL` | No | - | Supabase project URL, used to derive the project ref when `DATABASE_URL` is omitted |
| `SUPABASE_PROJECT_REF` | No | - | Supabase project ref, used with `SUPABASE_DB_PASSWORD` to build a direct Postgres URL |
| `SUPABASE_DB_PASSWORD` | No | - | Supabase database password, used when `DATABASE_URL` is omitted |
| `SUPABASE_DB_HOST` | No | `aws-1-us-west-2.pooler.supabase.com` | Supabase database host override |
| `SUPABASE_DB_PORT` | No | `5432` | Supabase database port |
| `SUPABASE_DB_NAME` | No | `postgres` | Supabase database name |
| `SUPABASE_DB_USER` | No | `postgres` | Supabase database user |

### Connection Pool

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DB_POOL_SIZE` | No | `5` | Number of persistent connections to maintain |
| `DB_MAX_OVERFLOW` | No | `10` | Max additional connections when pool is exhausted |
| `DB_POOL_TIMEOUT` | No | `30` | Seconds to wait for connection from pool |
| `DB_POOL_RECYCLE` | No | `1800` | Recycle connections after N seconds (default 30 min) |
| `DB_POOL_PRE_PING` | No | `true` | Test connections before use (handles stale connections) |

**Pool Sizing Guide:**
- **Development**: `pool_size=5, max_overflow=5` (10 max connections)
- **Production (small)**: `pool_size=10, max_overflow=10` (20 max connections)
- **Production (large)**: `pool_size=20, max_overflow=20` (40 max connections)

**Note:** Total max connections = `pool_size + max_overflow`. Ensure your PostgreSQL `max_connections` setting can accommodate this plus any other applications.

**Examples:**
```bash
# Supabase direct connection
DATABASE_URL=postgresql://postgres.meatfdvteugbeksckgqg:<your-supabase-db-pass>@aws-1-us-west-2.pooler.supabase.com:5432/postgres

# Supabase pooler/session connection, if provided by your project
DATABASE_URL=postgresql://postgres.<project-ref>:<your-supabase-db-pass>@<pooler-host>.pooler.supabase.com:5432/postgres

# Build from project settings instead of DATABASE_URL
SUPABASE_PROJECT_REF=meatfdvteugbeksckgqg
SUPABASE_DB_PASSWORD=<your-supabase-db-pass>
```

### Security

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes* | - | JWT signing key. Must be at least 32 characters. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `REFRESH_TOKEN_SECRET_KEY` | Yes* | - | Separate key for refresh tokens. Use different value from SECRET_KEY |
| `ALGORITHM` | No | `HS256` | JWT algorithm. Options: HS256, HS384, HS512 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `15` | Access token lifetime in minutes |
| `REFRESH_TOKEN_EXPIRE_DAYS` | No | `7` | Refresh token lifetime in days |
| `SESSION_ABSOLUTE_TIMEOUT_HOURS` | No | `24` | Force re-login after this many hours |

### Rate Limiting

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RATE_LIMIT_ENABLED` | No | `true` | Enable/disable rate limiting |
| `RATE_LIMIT_TIMES` | No | `100` | Max requests per window |
| `RATE_LIMIT_SECONDS` | No | `60` | Rate limit window in seconds |
| `RATE_LIMIT_EXEMPT_PATHS` | No | `/health,...` | Comma-separated paths exempt from rate limiting |

### CORS (Cross-Origin Resource Sharing)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CORS_ORIGINS` | Yes* | `http://localhost:3000,http://localhost:3001,http://localhost:5173,http://localhost:8000` | Comma-separated list of allowed origins. Default includes `http://localhost:5173` (the Vite dev server) so the SPA works in local dev |
| `CORS_ALLOW_CREDENTIALS` | No | `true` | Allow credentials in CORS requests |
| `CORS_ALLOW_METHODS` | No | `GET,POST,PUT,PATCH,DELETE,OPTIONS` | Allowed HTTP methods |
| `CORS_ALLOW_HEADERS` | No | `Authorization,Content-Type,...` | Allowed request headers |

**Production Example:**
```bash
CORS_ORIGINS=https://erp.werco.com,https://api.werco.com
```

### Trusted Hosts (HTTP Host header)

`TrustedHostMiddleware` validates the incoming HTTP `Host` header against an allowlist
and rejects anything else with **HTTP 400** — defense-in-depth against Host-header
poisoning (the Starlette CVE-2026-48710 class), which matters here because middleware
keys security decisions off `request.url.path` (CSRF exemptions, rate-limit selection,
the read-only platform-admin write guard).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ALLOWED_HOSTS` | Yes** | `*` | Comma-separated allowlist of `Host` values the API serves. `*` allows any host (validation **disabled** — dev only). Supports exact hosts (`api.werco.com`) and wildcard subdomains (`*.werco.com`); a port in the request `Host` is ignored when matching |

> **\*\* Production:** the default `*` disables Host validation (the app logs a startup
> warning when `ENVIRONMENT=production` and `ALLOWED_HOSTS=*`). **Set `ALLOWED_HOSTS` to
> the API's real hostnames in production** to enable it. A wildcard subdomain matches
> subdomains only, not the apex (`*.werco.com` does not match `werco.com`). `ALLOWED_HOSTS`
> also governs **WebSocket** upgrades, so the public host the SPA connects to must be listed
> or real-time updates silently fail. A missing/empty `Host` is also rejected with `400`.
>
> **⚠️ When you lock it down, you MUST also include the health-check probe hosts — otherwise
> the deploy's own health checks return `400`, the container is marked unhealthy, and the
> new release never goes live:**
> - **`localhost`** — the container `HEALTHCHECK` (`Dockerfile`, `Dockerfile.prod`, `docker-compose.prod.yml`) probes `http://localhost:8000/health…`, sending `Host: localhost`.
> - **`healthcheck.railway.app`** — Railway's health-check probe (the backend sets `healthcheckPath="/health"` in `railway.toml`). It is **not** covered by `*.up.railway.app` (different domain) — list it explicitly, or the deploy fails its health check.
> - The Railway public domain (`*.up.railway.app`) and/or your mapped custom domain — how clients actually reach the API.
>
> (The nginx `/health` location forwards the real client `Host`, so the `backend` upstream name does **not** need allowlisting.)

**Production Example (Railway):**
```bash
ALLOWED_HOSTS=api.werco.com,erp.werco.com,*.up.railway.app,healthcheck.railway.app,localhost
```

### Application

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_NAME` | No | `Werco ERP` | Application name (shown in logs, emails) |
| `DEBUG` | No | `false` | Enable debug mode (never in production!) |
| `ENVIRONMENT` | No | `development` | Environment name: development, staging, production |
| `API_V1_PREFIX` | No | `/api/v1` | API route prefix |
| `PORT` | No | `8000` | Server port (Railway sets this automatically) |
| `LOG_LEVEL` | No | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |

> **Not environment-configurable (intentional).** The work-order-completion finished-goods receipt
> location is **not** an env var — the warehouse (`MAIN`) and location (`FINISHED-GOODS`) are module
> constants (`FINISHED_GOODS_WAREHOUSE` / `FINISHED_GOODS_LOCATION` in
> `app/services/completion_inventory_service.py`). Likewise, **component backflush on completion** is
> not a global switch: it is a per-part database flag (`parts.backflush_components`, default `false`),
> set on the part record, not via configuration. Neither has an environment variable.
>
> The **operator-qualification gate** (Batch 11C / G5-B) adds **no** env var either: its minimum
> `SkillMatrix` skill level is the module constant `MIN_SKILL_LEVEL = 2` in
> `app/services/operator_qualification_service.py`, and the per-work-center certification requirement
> is the database column `work_centers.required_certification_type` (nullable; set on the work-center
> record). **Certificate of Conformance** issuance (G6-B) likewise has no env var — whether a CoC is
> required is driven by the per-shipment `cert_of_conformance` flag and the per-customer
> `customers.requires_coc` flag (default `true`), not by configuration.

### Labor Cost Rollup (work-order completion)

Batch-7 opt-in labor-hour + cost rollup. `LABOR_COST_ROLLUP_ENABLED` gates **all** automatic
cost/hours surfacing on work-order completion; it ships **OFF** so cost stays opt-in until shop-floor
labor check-in data is trusted. When **ON**, completing a work order rolls up `actual_hours` and
`actual_cost` (= labor + issued material + overhead), syncs a linked `JobCost` to `COMPLETED`, and the
`/analytics/cost-analysis` report computes labor/overhead at the resolved rate. When **OFF** (default),
completion does not auto-populate cost/hours and the cost-analysis report shows `$0` computed
labor/overhead (issued material is still shown — it is real inventory cost). The on-demand
`POST /job-costs/{id}/calculate` endpoint recomputes from time entries regardless of the flag.

The labor rate is resolved per work center from `WorkCenter.hourly_rate`, falling back to
`DEFAULT_LABOR_RATE` when a work center has no positive rate; the same shared resolver
(`app/services/labor_cost_service.py`) feeds both the completion rollup and the cost-analysis report,
so the two can never disagree. This replaces the old hardcoded `$45`/`$50` labor rates.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LABOR_COST_ROLLUP_ENABLED` | No | `false` | Master switch for the opt-in cost/hours rollup on WO completion (and the flag-gated labor/overhead legs of the cost-analysis report). Currently a **global** flag — see note below. |
| `REQUIRE_APPROVED_LABOR_FOR_COST` | No | `false` | Opt-in: when **ON**, labor-cost rollups count **only supervisor-approved** TimeEntries (`approved IS NOT NULL`). Default **OFF** → behavior byte-identical to before this flag existed. Currently a **global** flag — see note below. |
| `DEFAULT_LABOR_RATE` | No | `75.0` | Fallback labor rate ($/hour) used when a work center has no positive `WorkCenter.hourly_rate`. **Placeholder — a finance owner should set the real shop rate.** |
| `DEFAULT_OVERHEAD_RATE` | No | `0.0` | Overhead/burden rate ($/hour) charged on actual labor hours when a work center carries no overhead rate. |

> **Note:** `LABOR_COST_ROLLUP_ENABLED` is **global** today because the `Company` model has no
> per-company settings/feature-flags column yet. The resolution helper
> (`labor_cost_service.is_labor_cost_rollup_enabled`) already accepts a `company_id` and is the single
> chokepoint to repoint at a per-company field when one is added — promoting the flag to per-tenant will
> not require touching the rollup callers. The `no_labor_recorded` data-quality signal on completion
> fires **regardless** of this flag.

> **`REQUIRE_APPROVED_LABOR_FOR_COST` (Batch-11B / G5-A) is an approval *filter*, not a master
> switch.** It does not turn cost rollup on or off — that is `LABOR_COST_ROLLUP_ENABLED`'s job. It only
> changes **which** closed TimeEntries are counted once rollup is happening. When **ON**, the three
> labor-cost consumers — the job-costing recompute (`POST /job-costs/{id}/calculate`), the completion
> cost rollup, and the analytics OEE/labor leg — additionally require `TimeEntry.approved IS NOT NULL`,
> so un-approved shop-floor labor is excluded from cost until a supervisor/quality lead signs off via
> `POST /shop-floor/time-entries/{id}/approve` (sets `approved` / `approved_by`). When **OFF** (the
> default), every closed TimeEntry feeds the labor-cost legs exactly as before — byte-identical
> behavior. Like `LABOR_COST_ROLLUP_ENABLED` it is **global** today and resolved through the same
> chokepoint module (`labor_cost_service.is_approved_labor_required`, which accepts a `company_id`) so
> it can be promoted to a per-tenant flag in one place later.

### Shop-Floor Dashboard Reconcile (work-order completion read path)

Batch-9 (rank 12) bound on the `GET /api/v1/shop-floor/dashboard` reconcile-on-read scan. The dashboard
reconciles open (RELEASED / IN_PROGRESS / ON_HOLD) work orders from durable shop-floor evidence on every
load; that scan was previously unbounded and write-amplifying as the open-WO set grew. It is now capped
to the most-recently-touched (`ORDER BY updated_at DESC`) N open WOs — those are the most likely to carry
new completion evidence. Reconcile is **best-effort and idempotent**, so any WO beyond the cap is still
reconciled when opened in its detail / operations-list views; nothing is permanently stranded.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT` | No | `250` | Max number of open work orders the dashboard read path reconciles per request (the most-recently-touched first). Bounds the reconcile cost/write-amplification on a large open-WO set. |

> **Note:** when a single dashboard run **fills this cap exactly**, the handler logs a **WARNING**
> ("the shop has outgrown read-path reconcile") — that is the operational signal that the open-WO set has
> grown past what read-path reconcile should carry. Raising this value increases per-request reconcile
> cost; the **durable fix is the deferred ARQ reconcile job** (move reconcile off the read path
> entirely), not an ever-higher cap. See `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` → Rank 12.

### Redis Cache

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | No | - | Full Redis connection URL. Takes precedence over individual settings |
| `REDIS_HOST` | No | `localhost` | Redis server hostname |
| `REDIS_PORT` | No | `6379` | Redis server port |
| `REDIS_DB` | No | `0` | Redis database number |

**Examples:**
```bash
# Using URL (recommended)
REDIS_URL=redis://localhost:6379/0

# With password
REDIS_URL=redis://:password@localhost:6379/0

# Railway Redis
REDIS_URL=redis://default:xxx@xxx.railway.internal:6379
```

### Email (SMTP)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SMTP_HOST` | No | `smtp.gmail.com` | SMTP server hostname |
| `SMTP_PORT` | No | `587` | SMTP server port (587 for TLS, 465 for SSL) |
| `SMTP_USER` | No | - | SMTP username/email |
| `SMTP_PASSWORD` | No | - | SMTP password or app-specific password |
| `SMTP_FROM` | No | `noreply@werco.com` | From email address |
| `SMTP_FROM_NAME` | No | `Werco ERP System` | From display name |

**Gmail Setup:**
1. Enable 2FA on your Google account
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the app password as `SMTP_PASSWORD`

### File Storage

Persistent document bytes — quality-document uploads, **purchased shipping labels /
Bills of Lading** from the carrier integration, **RFQ package files**, and **uploaded
PO source documents** — go through the shared storage service
(`app/services/storage_service.py`) with two backends selected by `STORAGE_BACKEND`:

- **`local`** (default) — today's on-disk behavior, byte-for-byte: files under the
  resolved `UPLOAD_DIR` (or repo-relative `uploads/...`), and the stored reference is
  the filesystem path.
- **`s3`** — AWS S3 or any S3-compatible store (Railway buckets, Cloudflare R2 via
  `S3_ENDPOINT_URL`). Stored references are `s3://{bucket}/{key}` with tenant-prefixed,
  never-user-controlled keys (`{company_id}/documents/...`, `{company_id}/shipping/...`,
  `{company_id}/rfq_packages/...`, `{company_id}/purchase_orders/...`).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STORAGE_BACKEND` | No | `local` | Backend for **new** document writes: `local` or `s3`. With `s3`, the app **refuses to boot** unless `S3_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` are all set (fail-fast, validated at startup) |
| `S3_BUCKET_NAME` | Conditional² | `werco-erp-documents` | Bucket for document storage (s3 backend) |
| `AWS_ACCESS_KEY_ID` | Conditional² | - | Object-storage access key (AWS or any S3-compatible store) |
| `AWS_SECRET_ACCESS_KEY` | Conditional² | - | Object-storage secret key |
| `AWS_REGION` | No | `us-east-1` | Region passed to the S3 client |
| `S3_ENDPOINT_URL` | No | - | Endpoint URL for S3-**compatible** stores (Railway buckets, Cloudflare R2). Leave unset/empty for AWS S3 |
| `UPLOAD_DIR` | No | `/app/uploads` | (local backend) Primary directory for document / label / BOL files. **In production point this at a mounted, backed-up volume** (or it lives only in the container's ephemeral filesystem). |
| `UPLOAD_DIR_FALLBACK` | No | `./uploads` | (local backend) Fallback directory used when `UPLOAD_DIR` is not writable (resolved to an absolute path). |

> ² **Required when `STORAGE_BACKEND=s3`** — missing values abort startup with a clear
> error rather than silently dropping documents.

> **⚠️ Record retention (AS9100D / CMMC): `local` mode on a volume-less host loses every
> stored document on redeploy.** Production Railway has **no persistent volume**, so the
> container filesystem — and every quality document, label/BOL, RFQ file, and PO source
> document on it — is discarded on each deploy. On such hosts set `STORAGE_BACKEND=s3`
> (or mount a durable volume at `UPLOAD_DIR`).

> **Switching to s3 is safe for existing rows but does not migrate files.** Stored
> references are dispatched **per-row** (`s3://...` → object storage, anything else →
> local path), so legacy local-path rows remain readable after flipping
> `STORAGE_BACKEND=s3` — *provided the local files still exist*. Existing local files
> are **not** copied to the bucket automatically; on hosts where local files are already
> ephemeral, only documents written after the switch are durable.

**Railway bucket example** (S3-compatible; the CLI provides the endpoint + keys):
```bash
railway bucket create werco-erp-documents
railway bucket credentials      # prints the endpoint URL + access/secret key pair

STORAGE_BACKEND=s3
S3_BUCKET_NAME=werco-erp-documents
S3_ENDPOINT_URL=<endpoint from `railway bucket credentials`>
AWS_ACCESS_KEY_ID=<access key from `railway bucket credentials`>
AWS_SECRET_ACCESS_KEY=<secret key from `railway bucket credentials`>
AWS_REGION=us-east-1
```

### External Services

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | - | Anthropic API key for the AI features (PO/quote + BOM document extraction, AI routing generation, QMS clause extraction, Werco Copilot chat, `/search/nl` intent parsing). Every call goes through the shared client `app/services/llm_client.py`, which records per-call usage telemetry to the tenant-scoped `ai_usage_events` table (read via `GET /api/v1/ai-usage/summary` / the Admin Settings → AI Usage & Cost tab) |
| `ANTHROPIC_COPILOT_MODEL` | No | (router auto) | Per-task model override for Werco Copilot chat (task `copilot_chat`). Unset: the router uses the Default tier (Sonnet), escalating to the Reasoning tier for long multi-tool conversations |
| `ANTHROPIC_NL_SEARCH_MODEL` | No | (router auto) | Per-task model override for the `/search/nl` natural-language intent parse (task `nl_search`). Unset: pinned to the Fast tier (Haiku) |
| `SENTRY_DSN` | No | - | Sentry DSN for error tracking |
| `WEBHOOK_ENCRYPTION_KEY` | Conditional¹ | - | Fernet key for encrypting outbound-webhook secrets at rest. Also the **fallback** for `INTEGRATION_ENCRYPTION_KEY` when that is unset. |
| `INTEGRATION_ENCRYPTION_KEY` | Conditional¹ | (falls back to `WEBHOOK_ENCRYPTION_KEY`) | Fernet key that encrypts **integration secrets at rest** — carrier-account API keys and inbound-webhook signing secrets (see [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md)) **and the ProxyBox thermal-printer API key** (see [docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md)). **Reused, not new** for thermal printing. Resolution order: `INTEGRATION_ENCRYPTION_KEY` → `WEBHOOK_ENCRYPTION_KEY` → (dev/test only) an ephemeral generated key. |

> ¹ **Required to *use* carrier integration in `production`/`staging`** — not for boot. The app and Alembic migrations start fine without a key. But because carrier API keys / inbound-webhook secrets are encrypted at rest with it (CMMC SC-28), in prod/staging **creating or using a carrier account — or verifying an inbound carrier webhook — fails loudly** until at least one of `INTEGRATION_ENCRYPTION_KEY` / `WEBHOOK_ENCRYPTION_KEY` is set (a loud startup WARNING is logged while it's absent). The ephemeral-generated-key fallback exists **only** in dev/test — a generated key does not survive a restart and differs per worker/replica, which would leave stored secrets permanently undecryptable, so it is refused outright in prod/staging rather than used silently.

> **Generating a Fernet key.** `WEBHOOK_ENCRYPTION_KEY` and `INTEGRATION_ENCRYPTION_KEY`
> are **Fernet** keys (not `secrets.token_urlsafe` like `SECRET_KEY`). Generate one with:
> ```bash
> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```
> A single-secret deployment can leave `INTEGRATION_ENCRYPTION_KEY` unset and rely on
> `WEBHOOK_ENCRYPTION_KEY`; use a dedicated `INTEGRATION_ENCRYPTION_KEY` to rotate carrier
> secrets independently of webhook secrets.

### Werco Copilot (read-only AI chat)

Tuning knobs for `POST /api/v1/copilot/chat` (see [docs/API.md](API.md) → Werco Copilot). All
optional; the defaults are the shipped behavior.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COPILOT_RATE_LIMIT_PER_MINUTE` | No | `20` | Per-user request budget per minute (in-process sliding window, on top of the app-wide per-IP limits). Excess returns 429 |
| `COPILOT_MAX_TOOL_ROUNDS` | No | `8` | Tool-use rounds per chat turn before the model is forced to answer from gathered data (`truncated: true` in the response) |
| `COPILOT_MAX_OUTPUT_TOKENS` | No | `1024` | Output-token cap per model call in the tool loop |
| `COPILOT_LLM_TIMEOUT_SECONDS` | No | `45` | Upstream Anthropic timeout per model call (seconds) |

### ProxyBox Thermal-Label Printing

HTTP timing knobs for the 4×6 thermal **receiving label** sent to a ProxyBox Zero
(pbxz.io) bridge → Westinghouse WHTP203e printer (see
[docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md)). All optional; the
defaults are the shipped behavior. The per-company connection details (base URL /
target / API key / egress toggle) live on `CompanyPrintProfile`, **not** here.

The ProxyBox API key is **Fernet-encrypted at rest** with the same
`INTEGRATION_ENCRYPTION_KEY` (falling back to `WEBHOOK_ENCRYPTION_KEY`) used for
carrier secrets — **no new encryption key is required**. In `production`/`staging`,
storing or using a ProxyBox key fails loudly until one of those keys is set (CMMC
SC-28), exactly as for carrier secrets.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROXYBOX_TIMEOUT_SECONDS` | No | `30.0` | Per-request httpx timeout for the ProxyBox bridge |
| `PROXYBOX_POLL_INTERVAL_SECONDS` | No | `1.0` | Cadence for polling `GET /jobs/{id}` for a terminal print-job state |
| `PROXYBOX_MAX_WAIT_SECONDS` | No | `30.0` | Max wait for a terminal job state; on timeout the print returns a non-failed `timeout` result (the job may still print) rather than erroring |

### Audit Log Retention / Archival (CMMC AU-3.3.8)

Audit logs are immutable (database triggers block UPDATE/DELETE) and are **never row-deleted** by
maintenance jobs. The monthly `archive_aged_audit_logs_job` worker cron exports audit rows past their
retention window to cold storage instead. See `docs/AUDIT_LOG_RETENTION_RUNBOOK.md`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUDIT_ARCHIVE_ENABLED` | No | `true` | Master switch for the audit archival job. When `false`, the job archives nothing. |
| `AUDIT_ARCHIVE_DIR` | No | `/var/lib/werco/audit-archive` | Cold-storage destination for exported audit segments (NDJSON). **In production point this at a mounted, backed-up volume** (or object-store mount); the worker must have write access. |
| `AUDIT_RETENTION_DAYS_DEFAULT` | No | `1095` | Fallback retention window (days) used when a company has no active `security_audit_record` retention policy. 1095 = 3 years. |
| `AUDIT_ARCHIVE_MAX_ROWS_PER_RUN` | No | `50000` | Safety cap on rows exported per company per run; large backlogs drain over successive runs. |

**Note:** the retention window is normally driven by each company's `security_audit_record`
`RetentionPolicy` (seeded by migration 030); `AUDIT_RETENTION_DAYS_DEFAULT` is only the fallback.
Physical removal of aged rows from the online DB, if ever needed, is a deliberate DBA partition-drop —
never an automated delete and never by disabling the immutability triggers.

## Frontend Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REACT_APP_API_URL` | Yes* | `http://localhost:8000/api/v1` | Backend API URL (include /api/v1 suffix) |
| `NODE_ENV` | No | `development` | Environment: development, production, test |

**Important:** All frontend environment variables must be prefixed with `REACT_APP_` to be accessible in the browser. They are injected at build time via `vite.config.ts` (`define`).

The Vite dev server runs on **http://localhost:5173** (`npm start` / `npm run dev`). `REACT_APP_API_URL` defaults to `http://localhost:8000/api/v1` when unset (see `src/services/api.ts`), so a plain `frontend/.env` is optional for local dev. The backend's default `CORS_ORIGINS` already allows `http://localhost:5173`.

**Examples:**
```bash
# Local development
REACT_APP_API_URL=http://localhost:8000/api/v1

# Production
REACT_APP_API_URL=https://api.werco.com/api/v1
```

## E2E Testing

For Playwright E2E tests, set these in `frontend/.env` (see `frontend/.env.example`).
The `E2E_*_EMAIL` / `E2E_*_SECRET` pairs must match **actual seeded users** from
`backend/scripts/seed_data.py`, otherwise login-based tests fail:

| Variable | Required | Default (seed) | Description |
|----------|----------|----------------|-------------|
| `E2E_BASE_URL` | No | `http://localhost:5173` | Base URL Playwright targets (Vite dev server) |
| `E2E_ADMIN_EMAIL` | Yes* | `admin@werco.com` | Admin user email for tests |
| `E2E_ADMIN_SECRET` | Yes* | `admin123` | Admin user password for tests |
| `E2E_MANAGER_EMAIL` | No | `jsmith@werco.com` | Manager user email for tests |
| `E2E_MANAGER_SECRET` | No | `password123` | Manager user password for tests |
| `E2E_OPERATOR_EMAIL` | No | `bwilliams@werco.com` | Operator user email for tests |
| `E2E_OPERATOR_SECRET` | No | `password123` | Operator user password for tests |

**Notes:**
- Run the suite with the backend rate limiter disabled — set `RATE_LIMIT_ENABLED=false`
  on the API process. The suite logs in many times and otherwise trips the auth-login
  limit (5/min) and gets `429`s.
- `E2E_BASE_URL` defaults to `http://localhost:5173` (also wired into
  `frontend/playwright.config.ts` `baseURL` and `webServer.url`).

## Backend AI Eval Harness

Golden-fixture evals for the LLM extraction pipelines live in `backend/tests/evals/` and are
excluded from the default pytest run via the `evals` marker (see `backend/tests/evals/README.md`).
Offline mode (the default) scores stored golden outputs — **no API key, no network**.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUN_LIVE_EVALS` | No | - | Set to `1` to make `pytest -m evals tests/evals` re-run each eval case against the **real Anthropic API** (billable) instead of scoring stored outputs. Live mode also requires `ANTHROPIC_API_KEY`; if either is unset, the live-gated tests are skipped and only offline scoring runs. |

## Docker Compose Variables

When using Docker Compose, set these in `.env` at the project root:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DB_PASSWORD` | No | (default) | PostgreSQL password - change this! |
| `SECRET_KEY` | Yes* | - | JWT signing key |
| `ANTHROPIC_API_KEY` | No | - | For AI features |

## Railway Deployment

Railway automatically provides:
- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string (if Redis service added)
- `PORT` - Port for the application

Set these manually in Railway dashboard:
- `SECRET_KEY` - Generate a secure random key
- `REFRESH_TOKEN_SECRET_KEY` - Different secure random key
- `CORS_ORIGINS` - Your frontend URL(s)
- `ALLOWED_HOSTS` - Comma-separated hostnames the API serves (enables Host-header validation; see [Trusted Hosts](#trusted-hosts-http-host-header)). On Railway you **must** include `healthcheck.railway.app` and `localhost` (the health-check probes) alongside your public domain / `*.up.railway.app`, or the deploy's health check returns `400` and the release never goes live
- `STORAGE_BACKEND=s3` + `S3_BUCKET_NAME` / `S3_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` - Railway has **no persistent volume**, so the default `local` document storage loses files on every redeploy; see [File Storage](#file-storage)
- `REACT_APP_API_URL` - Your backend URL (for frontend service)

## Security Best Practices

1. **Never commit `.env` files** - They're in `.gitignore` for a reason
2. **Use strong secrets** - Generate with `secrets.token_urlsafe(64)`
3. **Different secrets per environment** - Don't share keys between dev/staging/prod
4. **Rotate secrets periodically** - Especially after team member departures
5. **Use environment-specific CORS** - Don't use `*` in production
6. **Disable DEBUG in production** - Set `DEBUG=false`
7. **Set `ALLOWED_HOSTS` in production** - Lock the Host-header allowlist to your real hostnames; don't leave it at `*`

## Generating Secrets

```bash
# Python (recommended)
python -c "import secrets; print(secrets.token_urlsafe(64))"

# OpenSSL
openssl rand -base64 48

# Node.js
node -e "console.log(require('crypto').randomBytes(48).toString('base64'))"
```

## Troubleshooting

### "Invalid DATABASE_URL"
Ensure the URL format is correct: `postgresql://user:password@host:port/database`

### "CORS Error"
Add your frontend URL to `CORS_ORIGINS`. Include protocol (http/https).

### "401 Unauthorized" on all requests
Check that `SECRET_KEY` matches between token generation and validation.

### "Redis connection failed"
Verify `REDIS_URL` or individual Redis settings. Redis is optional but recommended.

### "Email not sending"
For Gmail, ensure you're using an App Password, not your account password.
