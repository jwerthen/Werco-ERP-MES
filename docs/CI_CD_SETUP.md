# CI/CD Pipeline Setup Guide

This document explains how to set up and use the GitHub Actions CI/CD pipeline for Werco ERP.

## Overview

The pipeline consists of two workflow files:

1. **ci-cd.yml** - Main CI/CD pipeline (runs on push to main/develop)
2. **pr-check.yml** - Pull request checks (runs on PRs)

## Pipeline Stages

### Pull Request Checks (pr-check.yml)

Triggered on: Pull requests to `main` or `develop`

| Stage | Description | Required |
|-------|-------------|----------|
| Detect Changes | Only runs checks for changed files | - |
| Backend Checks | Linting, security scan, tests | If backend changed |
| Frontend Checks | Linting, type check, tests, build | If frontend changed |

### Main CI/CD Pipeline (ci-cd.yml)

Triggered on: Push to `main` or `develop`

| Stage | Description | Requires |
|-------|-------------|----------|
| Backend Lint | Black, isort, Flake8, MyPy, Bandit | - |
| Backend Tests | pytest with coverage | - |
| Frontend Lint | ESLint, TypeScript | - |
| Frontend Tests | Jest tests | - |
| Build | Docker images | All lint/test jobs |
| Security Scan | Trivy, npm audit, safety | Build |
| Deploy Staging | Railway deployment | Security (develop branch) |
| Deploy Production | Railway deployment | Security (main branch) |

## Required Secrets

Configure these in GitHub Repository Settings > Secrets and variables > Actions:

### Required Secrets

| Secret | Description | How to Get |
|--------|-------------|------------|
| `RAILWAY_TOKEN` | Railway API token | Railway Dashboard > Account Settings > Tokens |

### Required Variables

Configure in GitHub Repository Settings > Secrets and variables > Actions > Variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `STAGING_URL` | Staging frontend URL | `https://werco-frontend-staging.up.railway.app` |
| `STAGING_API_URL` | Staging API URL | `https://werco-api-staging.up.railway.app` |
| `PRODUCTION_URL` | Production frontend URL | `https://werco-frontend-production.up.railway.app` |
| `PRODUCTION_API_URL` | Production API URL | `https://werco-api-production.up.railway.app` |

## Setup Instructions

### 1. Create Railway Token

```bash
# Login to Railway
railway login

# Generate a token (or use Railway Dashboard)
railway tokens create
```

Save this token as `RAILWAY_TOKEN` in GitHub Secrets.

### 2. Configure GitHub Secrets

1. Go to your GitHub repository
2. Navigate to Settings > Secrets and variables > Actions
3. Click "New repository secret"
4. Add `RAILWAY_TOKEN` with your Railway token

### 3. Configure GitHub Variables

1. In the same settings page, click "Variables" tab
2. Add each of the required variables listed above

### 4. Configure Branch Protection (Recommended)

1. Go to Settings > Branches
2. Add rule for `main` branch:
   - [x] Require a pull request before merging
   - [x] Require status checks to pass before merging
     - Select: `Backend Checks`, `Frontend Checks`
   - [x] Require branches to be up to date before merging
   - [x] Include administrators

### 5. Configure Environments (Optional)

For deployment approvals:

1. Go to Settings > Environments
2. Create `staging` and `production` environments
3. For `production`:
   - Add required reviewers
   - Set deployment branch rules (only `main`)

## Workflow Triggers

### Automatic Triggers

| Event | Workflow | Action |
|-------|----------|--------|
| PR to main/develop | pr-check.yml | Run checks on changed files |
| Push to develop | ci-cd.yml | Full CI + deploy to staging |
| Push to main | ci-cd.yml | Full CI + deploy to production |

### Manual Triggers

You can manually trigger the CI/CD pipeline:

1. Go to Actions tab in GitHub
2. Select "CI/CD Pipeline"
3. Click "Run workflow"
4. Select branch and deploy target

## Local Development Integration

### Pre-commit Hooks

The project includes pre-commit hooks that run the same checks locally:

```bash
# Install pre-commit
pip install pre-commit

# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

### Running Checks Locally

```bash
# Backend
cd backend
pip install -r requirements-dev.txt
black --check app tests
isort --check-only app tests
flake8 app
pytest tests/ -v --cov=app

# Frontend
cd frontend
npm install
npm run lint
npm test
npm run build
```

## Troubleshooting

### Pipeline Failed: Backend Tests

1. Check the test output in GitHub Actions logs
2. Run tests locally: `pytest tests/ -v`
3. Common issues:
   - Missing environment variables
   - Database connection (uses PostgreSQL service in CI)
   - Missing dependencies

### Pipeline Failed: Frontend Build

1. Check build output in logs
2. Run locally: `npm run build`
3. Common issues:
   - TypeScript errors
   - Missing dependencies
   - Environment variable issues

### Deployment Failed

1. Check Railway token is valid
2. Verify service names match your Railway setup
3. Check Railway logs for deployment errors

### Security Scan Warnings

Security scans may show warnings that don't fail the build:
- `npm audit` warnings for dev dependencies
- `safety check` for Python packages

Review these periodically and update dependencies.

## Customization

### Adjusting Coverage Requirements

In `pr-check.yml`, adjust the `--cov-fail-under` value:

```yaml
run: pytest tests/ -v --cov=app --cov-fail-under=50  # Require 50% coverage
```

### Adding Slack Notifications

Add to the `notify` job:

```yaml
- name: Slack Notification
  uses: 8398a7/action-slack@v3
  with:
    status: ${{ job.status }}
    fields: repo,message,commit,author
  env:
    SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK }}
```

### Adding E2E Tests

Add a new job after frontend-test:

```yaml
e2e-tests:
  name: E2E Tests
  runs-on: ubuntu-latest
  needs: [backend-test, frontend-test]
  steps:
    - uses: actions/checkout@v4
    - name: Run Playwright tests
      run: npx playwright test
```

## Cost Considerations

GitHub Actions is free for public repositories. For private repos:

- Free tier: 2,000 minutes/month
- Average pipeline run: ~5-10 minutes
- Estimated runs: 200-400/month with free tier

## Related Documentation

- [Deployment Runbook](./DEPLOYMENT_RUNBOOK.md)
- [Development Guide](./DEVELOPMENT.md)
- [Production Checklist](./PRODUCTION_CHECKLIST.md)
