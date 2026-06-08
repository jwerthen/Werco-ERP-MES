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

### File Storage (S3)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | No | - | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | No | - | AWS secret key |
| `AWS_REGION` | No | `us-east-1` | AWS region |
| `S3_BUCKET_NAME` | No | `werco-erp-documents` | S3 bucket for file storage |

### External Services

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | - | Anthropic API key for AI-powered PO extraction |
| `SENTRY_DSN` | No | - | Sentry DSN for error tracking |
| `WEBHOOK_ENCRYPTION_KEY` | No | - | Key for encrypting webhook payloads |

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
