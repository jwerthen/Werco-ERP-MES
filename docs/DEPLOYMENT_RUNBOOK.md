# Werco ERP - Deployment Runbook

**Version**: 1.0.0  
**Last Updated**: 2026-01-09  
**Environment**: Railway (Primary), Self-hosted (Optional)

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
# Deploy backend
cd backend; railway up --service werco-api . --path-as-root

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
| `ENVIRONMENT` | production/staging/development | Yes |
| `SENTRY_DSN` | Error tracking | Recommended |

---

## Pre-Deployment Checklist

### Before Every Deployment

- [ ] All tests passing locally
- [ ] Code reviewed and approved
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

# Deploy to Railway
railway up --service werco-api . --path-as-root

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

# Deploy to Railway
railway up --service werco-frontend . --path-as-root

# Monitor deployment
railway logs --service werco-frontend
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

### Step 4: Merge to Main
```powershell
git checkout main
git merge hotfix/description-of-fix
git push origin main
```

### Step 5: Deploy
Follow [Standard Deployment](#standard-deployment) steps.

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
railway up --service werco-api . --path-as-root

# Return to main
git checkout main
```

**Option 2: Railway Dashboard**
1. Open Railway dashboard
2. Navigate to werco-api service
3. Go to "Deployments" tab
4. Click on previous successful deployment
5. Click "Redeploy"

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
| `/health/detailed` | Full system info | 200, includes versions |

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
SESSION_ABSOLUTE_TIMEOUT_HOURS=24

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
