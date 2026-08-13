# CI/CD Pipeline Setup Guide

This document explains how to set up and use the GitHub Actions CI/CD pipeline for Werco ERP.

## Overview

The pipeline consists of three workflow files:

1. **ci-cd.yml** - Main CI/CD pipeline (runs on push to main/develop)
2. **pr-check.yml** - Pull request checks (runs on PRs)
3. **e2e.yml** - Playwright E2E suite against a real, seeded full stack (PRs, nightly, manual; deliberately **non-blocking** — see below)

## Pipeline Stages

### Pull Request Checks (pr-check.yml)

Triggered on: Pull requests to `main` or `develop`

| Stage | Description | Required |
|-------|-------------|----------|
| Detect Changes | Only runs checks for changed files | - |
| Frontend Checks | Linting, type check, tests, build | If frontend changed |

> **There is no backend job here, deliberately.** Until 2026-08-13 this workflow
> carried a `Backend Checks` job that ran the whole backend pytest suite (5516
> tests, ~38 min) on every PR touching `backend/**` — concurrently with
> `ci-cd.yml`'s `Backend Tests` running the identical suite — and a lint half that
> was a strict subset of `ci-cd.yml`'s `Backend Linting` (same Black/isort/Flake8/
> Bandit, minus MyPy). Nothing in `pr-check.yml` is a required status check, so
> that ~38 minutes per backend PR could not change whether the PR could merge.
> The backend gate is unchanged and still blocking — it just lives in `ci-cd.yml`
> only. `backend/tests/test_ci_workflow_gates.py::TestPrCheckDoesNotDuplicateTheBackendSuite`
> stops the duplicate growing back **and** asserts the `ci-cd.yml` gate still exists.

### Main CI/CD Pipeline (ci-cd.yml)

Triggered on: **Pull requests to `main`/`develop`, and pushes to `main`/`develop`.**
The PR run is where all five required status checks come from — including the backend
gate, since `pr-check.yml` deliberately has no backend job (see above). The deploy
stages are separately branch-gated to pushes, so a PR run does the full CI and no deploy.

| Stage | Description | Requires |
|-------|-------------|----------|
| Backend Lint | Black, isort, Flake8, MyPy, Bandit | - |
| Backend Tests | pytest with coverage | - |
| Frontend Lint | ESLint, TypeScript | - |
| Frontend Tests | Jest tests | - |
| Build | Docker images | All lint/test jobs |
| Security Scan | Trivy, npm audit, pip-audit | Build |
| Deploy Staging | Railway deployment | Security (develop branch) |
| Deploy Production | Railway deployment, then post-deploy health verification | Security (main branch) |

> **Production auto-deploys from `main`.** A push to `main` (only reachable through a
> merged PR — see Branch Protection below) runs full CI and then deploys to production
> with **no manual approval gate**. The `production` GitHub environment no longer carries
> a required-reviewer rule; the compensating controls are (a) a deployment-branch policy on
> the `production` environment that allows **only `main`** to deploy, and (b) post-deploy
> checks that **fail the job unless the new commit is actually serving** —
> `Verify production is serving this commit` (`ci-cd.yml`) and `Verify the deployed bundle
> is this commit` (`deploy-frontend-production.yml`). Both poll the running service for the
> release SHA the run stamped into the artifact, via `.github/scripts/verify_release.py`.
> Rollback is re-adding the reviewer rule (and/or redeploying a known-good commit).
> _(Governance change 2026-06-22; checks strengthened 2026-08-04 — they previously curl'd
> `/health`, which the **previous** container answers just as happily, so they proved the
> site was up and never that the deploy had landed. See
> [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md#change-control--production-deploy-model).)_

### E2E Tests (e2e.yml)

Triggered on: PRs to `main`/`develop` (paths-filtered to `backend/**`, `frontend/**`, and
the workflow itself), nightly at 09:00 UTC (3am CST / 4am CDT), and manual dispatch.

The single `Playwright E2E` job boots the full stack inside the runner and runs the
browser suite (`frontend/e2e/*.spec.ts`) against it:

1. `postgres:15-alpine` service container
2. `python -m scripts.seed_data` — creates the schema and seeds the default company,
   users, work centers, parts, and work orders the specs expect
3. uvicorn with `ENVIRONMENT=test` and `RATE_LIMIT_ENABLED=false` (load-bearing:
   `/api/v1/auth/login` is rate-limited to 5/min and nearly every spec logs in through
   the UI from one IP — with limits on, the suite 429s immediately)
4. Vite dev server (`playwright.config.ts` leaves `webServer` undefined when `CI` is set;
   the workflow boots the app itself)
5. `npx playwright test`

**Credentials are not secrets.** The `E2E_*` env vars are set inline in the workflow to
the throwaway dev-seed users (`admin@werco.com`/`admin123`, `jsmith@werco.com` and
`bwilliams@werco.com` with `password123`) against an ephemeral CI database. The email
overrides are required — `frontend/e2e/fixtures.ts` defaults to `manager@werco.com` /
`operator@werco.com`, which the seed does **not** create. See
[ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) → E2E Testing.

**Intentionally non-blocking.** The job is a separate workflow, not a required status
check, so failures stay visible on PRs without blocking merges until the suite's flake
behavior in CI is known. Promotion path: add the "Playwright E2E" job to the `main`
branch-protection ruleset (github-manager owns that policy change).

**Artifacts:** the Playwright HTML report is always uploaded as `playwright-report`
(14-day retention; download from the run's Artifacts section and open `index.html`).
Backend and Vite server logs upload as `server-logs` on failure only.

**Known follow-up:** the crew-station kiosk station tests self-skip —
`E2E_KIOSK_STATION_ID` / `E2E_KIOSK_PIN` / `E2E_BADGE_A` / `E2E_BADGE_B` are deliberately
unset until the seed provisions a kiosk station + badges (the spec's admin-modal test
still runs).

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

### 4. Configure Branch Protection (`main` ruleset)

`main` is protected by a repository **ruleset** (Settings > Rules > Rulesets), enforced as
of 2026-06-22:

- [x] Require a pull request before merging — **0 required approvals** (hands-off /
      merge-when-green; changes still land only via a PR, not a direct push)
- [x] Require status checks to pass before merging (the CI deploy/build checks)
- [x] Block force-pushes (non-fast-forward) and branch deletion
- [x] Repo-admin bypass for emergencies (documented break-glass)

Net effect: every commit that reaches `main` — and therefore every commit that
auto-deploys to production — has passed CI in a PR. Direct pushes to `main` are
PR-gated for non-admins.

### 5. Configure Environments

1. Go to Settings > Environments
2. Create `staging` and `production` environments
3. For `production`:
   - **No required reviewers** — production deploys run automatically on push to `main`
     (removed 2026-06-22; see "Production auto-deploys from `main`" above). Re-adding a
     required reviewer here is the documented rollback if a manual gate is needed again.
   - Set the deployment branch policy to allow **only `main`** to deploy.

## Workflow Triggers

### Automatic Triggers

| Event | Workflow | Action |
|-------|----------|--------|
| PR to main/develop | ci-cd.yml | Full CI — the five required checks (Backend Linting, Backend Tests, Frontend Linting, Frontend Tests, Security Scanning); no deploy |
| PR to main/develop | pr-check.yml | Frontend checks on changed files (backend is gated by ci-cd.yml) |
| PR to main/develop (backend/frontend paths) | e2e.yml | Playwright E2E against seeded full stack (non-blocking) |
| Push to develop | ci-cd.yml | Full CI + deploy to staging |
| Push to main | ci-cd.yml | Full CI + deploy to production |
| Nightly, 09:00 UTC | e2e.yml | Playwright E2E against seeded full stack |

### Manual Triggers

You can manually trigger the CI/CD pipeline:

1. Go to Actions tab in GitHub
2. Select "CI/CD Pipeline"
3. Click "Run workflow"
4. Select branch and deploy target

"E2E Tests" supports the same `workflow_dispatch` flow (steps 1–3, selecting "E2E Tests").

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
- `npm audit` findings below **high** severity (the frontend gate blocks only
  high/critical), plus a non-fatal warning for any stale allowlist entry
- `pip-audit -r requirements.txt -r requirements-dev.txt` for Python packages
  (scoped to the app's dependency set), plus `--ignore-vuln PYSEC-2026-1325` for
  the one accepted backend suppression — there is no backend allowlist file, so
  that flag and
  [Security Advisory Suppressions](SECURITY_ADVISORY_SUPPRESSIONS.md) are its
  only record

Review these periodically and update dependencies.

The frontend step (`Run npm audit (Frontend)` → `npm run audit:ci`) **does** fail
the build on any high/critical advisory that is not explicitly allowlisted with a
written not-applicable justification. See
[Security Advisory Suppressions](SECURITY_ADVISORY_SUPPRESSIONS.md).

## Customization

### Adjusting Coverage Requirements

The backend floor lives in **`backend/pytest.ini`** (`addopts` → `--cov-fail-under`,
currently **78**, against ~81% actual). That file is the source of truth: `ci-cd.yml`'s
required `backend-test` job passes no threshold of its own and inherits `addopts`.

No workflow passes `--cov-fail-under` on the command line any more. `pr-check.yml`
used to — and because a CLI `--cov-fail-under` **overrides** `addopts`, that copy had to
be kept in lockstep or the job would silently hold an older, laxer floor. That job was
removed on 2026-08-13 (see above), so `backend/pytest.ini` is now the single place the
floor is written.

If you ever re-add an explicit `--cov-fail-under=N` to a workflow, it must equal
`pytest.ini`'s value: `backend/tests/test_ci_workflow_gates.py` asserts no workflow
diverges from it, and that the floor is never lowered. The frontend equivalent is `frontend/jest.config.js` →
`coverageThreshold.global` (statements 52 / branches 43 / functions 38 / lines 52),
pinned by the same test file.

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

### Promoting E2E Tests to a Required Check

E2E tests already run in CI via `e2e.yml` (see "E2E Tests (e2e.yml)" above) but are
intentionally not a required status check. Once the suite has demonstrated stable
(non-flaky) behavior in CI, promote it by adding the "Playwright E2E" job to the required
status checks in the `main` branch-protection ruleset (Settings > Rules > Rulesets).

## Cost Considerations

GitHub Actions is free for public repositories. For private repos:

- Free tier: 2,000 minutes/month
- Average pipeline run: ~5-10 minutes
- Estimated runs: 200-400/month with free tier

## Related Documentation

- [Deployment Runbook](./DEPLOYMENT_RUNBOOK.md)
- [Development Guide](./DEVELOPMENT.md)
- [Production Checklist](./PRODUCTION_CHECKLIST.md)
