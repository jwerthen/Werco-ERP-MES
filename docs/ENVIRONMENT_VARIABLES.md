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
| `DATABASE_URL` | Yes* | - | PostgreSQL connection string. Format: `postgresql://user:password@host:port/database` |

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
# Local development
DATABASE_URL=postgresql://werco_user:<your-db-pass>@localhost:5432/werco_erp

# Railway (automatically provided)
DATABASE_URL=postgresql://postgres:<auto>@<auto>.railway.internal:5432/railway

# Docker Compose
DATABASE_URL=postgresql://werco_user:<your-db-pass>@db:5432/werco_erp
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
| `CORS_ORIGINS` | Yes* | `http://localhost:3000,...` | Comma-separated list of allowed origins |
| `CORS_ALLOW_CREDENTIALS` | No | `true` | Allow credentials in CORS requests |
| `CORS_ALLOW_METHODS` | No | `GET,POST,PUT,PATCH,DELETE,OPTIONS` | Allowed HTTP methods |
| `CORS_ALLOW_HEADERS` | No | `Authorization,Content-Type,...` | Allowed request headers |

**Production Example:**
```bash
CORS_ORIGINS=https://erp.werco.com,https://api.werco.com
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

## Frontend Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REACT_APP_API_URL` | Yes* | `http://localhost:8000/api/v1` | Backend API URL (include /api/v1 suffix) |
| `NODE_ENV` | No | `development` | Environment: development, production, test |

**Important:** All frontend environment variables must be prefixed with `REACT_APP_` to be accessible in the browser.

**Examples:**
```bash
# Local development
REACT_APP_API_URL=http://localhost:8000/api/v1

# Production
REACT_APP_API_URL=https://api.werco.com/api/v1
```

## E2E Testing

For Playwright E2E tests, set these variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `E2E_ADMIN_EMAIL` | Yes* | - | Admin user email for tests |
| `E2E_ADMIN_SECRET` | Yes* | - | Admin user password for tests |
| `E2E_MANAGER_EMAIL` | No | - | Manager user email for tests |
| `E2E_MANAGER_SECRET` | No | - | Manager user password for tests |
| `E2E_OPERATOR_EMAIL` | No | - | Operator user email for tests |
| `E2E_OPERATOR_SECRET` | No | - | Operator user password for tests |

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
- `REACT_APP_API_URL` - Your backend URL (for frontend service)

## Security Best Practices

1. **Never commit `.env` files** - They're in `.gitignore` for a reason
2. **Use strong secrets** - Generate with `secrets.token_urlsafe(64)`
3. **Different secrets per environment** - Don't share keys between dev/staging/prod
4. **Rotate secrets periodically** - Especially after team member departures
5. **Use environment-specific CORS** - Don't use `*` in production
6. **Disable DEBUG in production** - Set `DEBUG=false`

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
