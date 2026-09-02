# Werco ERP - Deployment Runbook

**Version**: 1.0.0  
**Last Updated**: 2026-01-09  
**Environment**: Railway (Primary), Self-hosted (Optional)

> **Scope.** This runbook covers the two long-standing services, `werco-api` and
> `werco-frontend`. It does **not** cover the ARQ background worker (`werco-worker`), which
> no workflow in this repo has ever deployed. Its one-time cutover — pre-flight checks, the blast radius of
> twelve crons that have never fired, the staged rollout, and rollback — is
> [`WORKER_DEPLOYMENT_RUNBOOK.md`](WORKER_DEPLOYMENT_RUNBOOK.md). Two differences matter if
> you ever deploy the worker by hand: it is deployed from the **repo root**, not `backend/`,
> and it has **no healthcheck** (it serves no HTTP).

---

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
