# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Werco ERP-MES is a custom Enterprise Resource Planning + Manufacturing Execution System for precision manufacturing, built for **AS9100D, ISO 9001, and CMMC Level 2** compliance. The compliance posture is not incidental — audit trails, traceability, and access control are correctness requirements, not features. See the "Compliance-critical invariants" section below before changing data-writing code.

## Subagent delegation policy

This repo ships a team of specialized subagents in `.claude/agents/`. Route work to them automatically — don't wait to be asked by name:

- **backend-engineer** — FastAPI endpoints, services, models, schemas.
- **frontend-engineer** — React/TS pages, components, forms, styling.
- **database-migration-specialist** — any Alembic migration (schema/enum/constraint change).
- **ai-integration-specialist** — Anthropic-powered RFQ parsing / quoting / learning.
- **devops-engineer** — Docker, CI/CD, Railway/Vercel, workers, env/secrets.
- **github-manager** — PRs, issues, releases/tags, labels, branch protection, CODEOWNERS; drives merge-when-green via `gh` (waits on the review gates; doesn't edit the Actions YAML — that's devops-engineer).
- **compliance-auditor** — review for tenant isolation, audit logging, RBAC, soft-delete, traceability.
- **code-reviewer** — diff review for correctness + cleanups, runs the lint/type/security gate.
- **test-engineer** — pytest / Jest / Playwright coverage.
- **documentation-engineer** — README, docs/ runbooks, CLAUDE.md, API/OpenAPI, compliance docs.

**Definition of done for any code change.** A change is not complete until:
1. The **test-engineer** has added or updated tests for the changed behavior (and they pass), AND
2. The **documentation-engineer** has updated any docs the change affects — API endpoints, env vars, roles/permissions, deploy steps, features, or documented behavior (or has confirmed none are affected).

Invoke these two after the implementing agent finishes, as a matter of course — they are the standing QA and documentation gates, not optional extras. For changes touching data access, auth, or deletion, also route through **compliance-auditor** before considering the work done. Skipping these is only acceptable for genuinely non-code changes (e.g. a typo fix in a comment) — and say so explicitly when you skip.

## Monorepo layout

- `backend/` — Python 3.11 / FastAPI API (the bulk of the system)
- `frontend/` — React 19 + TypeScript + Vite SPA (the application UI)
- `landing/` — separate marketing site (React + Vite), deployed to Vercel
- `load-tests/` — load testing suite
- `docs/` — operational runbooks and compliance docs (see below)
- `supabase/`, `nginx/`, `docker-compose*.yml` — infra

## Commands

### Backend (run from `backend/`)
```bash
# Tests (pytest, async mode auto, runs in parallel via -n auto, 50% coverage floor)
pytest                                  # full suite
pytest tests/test_work_orders.py        # single file
pytest tests/test_work_orders.py::test_create_work_order   # single test
pytest -m unit                          # by marker: unit | integration | api | slow | requires_db | evals
pytest -m "not slow and not requires_db"
pytest -m evals tests/evals             # AI eval harness — excluded from the default run; offline by default,
                                        # live API opt-in via RUN_LIVE_EVALS=1 + ANTHROPIC_API_KEY (see tests/evals/README.md)

# Lint / format / typecheck (also enforced by .pre-commit-config.yaml)
black . && isort . && flake8 app && mypy app
bandit -c pyproject.toml -r app         # security scan
pre-commit run --all-files

# Migrations (Alembic)
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic downgrade -1
```

### Frontend (run from `frontend/`)
```bash
npm run dev            # Vite dev server
npm run build          # production build
npm test               # Jest unit/component (watch disabled)
npm test -- path/to/File.test.tsx     # single test file
npm run test:coverage
npm run test:e2e       # Playwright E2E (test:e2e:ui / test:e2e:headed for debugging)
npm run lint           # eslint src --ext .ts,.tsx  (lint:fix to autofix)
npm run type-check     # tsc --noEmit
npm run format         # prettier

# Safe browser harness — headless-Chromium screenshot/snapshot/logs/pdf of a running app.
# Fixed subcommands, default-deny origin allowlist, sandboxed output. See docs/BROWSER_HARNESS.md.
npm run harness -- screenshot http://localhost:5173
```

### Full stack
```bash
docker compose up      # backend + frontend + redis + worker (ARQ background jobs)
```

## Backend architecture

Layered FastAPI app under `backend/app/`:

- `main.py` — app factory, middleware wiring (CORS, GZip, rate limiting via slowapi, Sentry, Host-header allowlist via TrustedHostMiddleware).
- `api/endpoints/` — ~53 REST routers, one per domain, mounted under `/api/v1/`. Thin: validate, call a service, return a Pydantic schema.
- `api/deps.py` — **the dependency-injection seam.** Auth, tenancy, and RBAC all flow through here. Use these rather than re-implementing:
  - `get_current_user` / `get_current_active_user`
  - `get_current_company_id` — returns the *active* company (handles platform-admin context switching; do not read `current_user.company_id` directly for scoping)
  - `require_role([UserRole.X, ...])`, `require_platform_admin`, `get_admin_user`
  - `get_audit_service` — request-scoped `AuditService`
- `services/` — business logic. Multi-step / state-changing operations belong here, not in routers.
  - **All Anthropic LLM calls go through `services/llm_client.py`** (`run_llm_task`: model routing via `llm_model_router`, prompt caching, per-call usage telemetry into the tenant-scoped `ai_usage_events` table — telemetry, not audit data). Don't instantiate `anthropic.Anthropic` at call sites.
  - **Prompt text is versioned in `services/prompts/`** — bump the prompt's semver `version` and add a `CHANGELOG.md` entry there whenever prompt text or request layout changes; the version is recorded on every usage row.
- `models/` — SQLAlchemy 2.0 declarative models (~48). All extend `Base` from `app.db.database`.
- `schemas/` — Pydantic 2 request/response contracts. Keep API I/O typed through these.
- `db/mixins.py` — shared model behavior (see invariants below).
- `jobs/` + `worker.py` — ARQ async jobs on Redis (email, MRP runs, long tasks). Enqueue from services; don't block request handlers.
- `core/` — cross-cutting: `config.py` (settings), `security.py` (JWT/bcrypt), `cache.py`, `pagination.py`, `realtime.py`/`websocket.py`, `sanitization.py`.

API style: REST, JSON, OpenAPI at `/docs`. JWT auth — 15-min access token, 7-day refresh, 24h absolute session cap.

## Frontend architecture

React 19 SPA under `frontend/src/`:

- `pages/` — route-level screens (~59). `components/` — reusable UI grouped by domain.
- `services/` — Axios API client with **ETag-based conditional caching and a refresh-token interceptor**. Route API calls through this client, not raw axios.
- `context/` — React Context for cross-cutting state: auth, active-company switching, keyboard shortcuts, tours. No Redux; server data is fetched per-page and cached at the client.
- Forms: React Hook Form + Zod (`validation/`). State that should survive reload (pagination, filters) goes in URL params.
- Styling: Tailwind CSS 4 + DaisyUI, with the Werco brand palette (werco-navy `#1B4D9C`, accent red `#C8352B`, steel grays) and an "instrument-panel" aesthetic — sharp corners, hairline borders, minimal shadows. Match it.
- Modals/dialogs: build with the shared `<Modal>` primitive (`components/ui/Modal.tsx`), not hand-rolled `fixed inset-0` overlays — it portals to `document.body` at `z-[60]` (clearing the fixed `z-50` sidebar) and normalizes the instrument-panel panel chrome, backdrop/Escape close, and modal stacking. A few non-dialog overlays (mobile sidebar backdrop, Tour spotlight, GlobalSearch's Headless UI palette, CopilotPanel side panel, LoadingOverlay) are intentionally not Modals.

## Compliance-critical invariants

These are the rules that make this system AS9100D/CMMC-viable. Treat violations as bugs.

1. **Tenant isolation.** Most domain tables carry `company_id` via `TenantMixin` (`app/db/mixins.py`), `nullable=False`. Every query against tenant data MUST be scoped to the active company — use `tenant_query()` / `tenant_filter()` from `app.db.tenant_filter` and derive the company from `get_current_company_id`. A query that returns another tenant's rows is a security defect.

2. **Audit logging.** State changes (create/update/delete/status-change) must be recorded through `AuditService` (`services/audit_service.py`) via its `log_create` / `log_update` / `log_delete` / `log_status_change` helpers — obtained from the `get_audit_service` dependency. The `audit_log` table is **tamper-evident**: it uses a hash chain (`sequence_number`, `previous_hash`, `integrity_hash` SHA-256). Never write to it directly or backfill rows out of band.

3. **Soft delete, not hard delete.** Models using `SoftDeleteMixin` are deleted via `.soft_delete(user_id)` (sets `is_deleted` / `deleted_at` / `deleted_by`). Queries must filter `is_deleted == False`. Don't issue physical `DELETE` on these.

4. **Optimistic locking.** Models with `OptimisticLockMixin` carry a `version` column — respect it on concurrent updates rather than blind overwrites.

5. **Traceability & revisions.** Parts/BOM carry revision control and critical-characteristic flags; lot/serial traceability is a product requirement. Preserve historical records — prefer new revisions over mutating shipped data.

## Migrations — handle with care

There are 37+ Alembic versions over live, multi-tenant data. When adding a migration:
- Make it **idempotent and reversible** (provide a real `downgrade`). The most recent commit history shows migrations being hardened for idempotency — follow that precedent.
- Never edit a migration that has already been applied; add a new one.
- New tenant-scoped tables need a non-null `company_id` + index (the `TenantMixin` shape).
- Autogenerate is a starting point — review the diff; SQLAlchemy doesn't always detect enum/constraint changes.
- Autogenerate only sees tables registered on `Base.metadata` — every model module must be imported in `app/models/__init__.py` (`alembic/env.py` does `from app.models import *`). Adding a model file without wiring it in there makes autogenerate miss it or crash with `NoReferencedTableError`.
- **Bootstrap is not `alembic upgrade head`.** On an empty Postgres the schema is created by `Base.metadata.create_all()` on first boot (not by an initial migration; `001` only adds indexes), so the path is `create_all` → `alembic stamp <baseline>` → incremental `upgrade`. A bare `upgrade head` on an empty DB fails (`002` does `ALTER TYPE workcentertype`). See `docs/DEVELOPMENT.md` → Database Migrations.

## Where to look for operational context

`docs/` holds the runbooks that aren't obvious from code: `RBAC_PERMISSIONS.md` (the role model), `CMMC_LEVEL_2_COMPLIANCE.md`, `AI_QUOTING_AGENT_RUNBOOK.md` + `IMPLEMENTATION_NOTES_AI_QUOTING_AGENT.md` (the Anthropic-powered RFQ/quoting feature), `SHIPPING_CARRIER_INTEGRATION.md` (the swappable multi-carrier aggregator — EasyPost adapter + registry, the `allow_carrier_egress` CUI kill switch, inbound HMAC-verified webhooks, the tracking poll cron, and label/BOL Document storage), `EXCEL_MIGRATION_RUNBOOK.md` (the A0.2 Excel migration kit — CSV/XLSX import load order, server templates, dry-run-then-commit discipline, open-WO/PO loaders, cutover checklist), `KIOSK.md` (the `/kiosk` operator station screen — URL params, badge login, idle logout, lockdown, telemetry/offline behavior), `WALLBOARD.md` (the A0.5 read-only shop-floor TV board — scoped single-endpoint display tokens, TV setup, revocation), `DEPLOYMENT.md` / `RAILWAY_DEPLOYMENT.md`, `ENVIRONMENT_VARIABLES.md`, `API.md`, `DEVELOPMENT.md`, and `BROWSER_HARNESS.md` (the safe headless-Chromium CLI for screenshots/snapshots/logs/PDFs of a running app).

## Conventions worth matching

- Backend line length is 120 (flake8/black configured to it). Status/priority/role values are `str`-backed `enum.Enum` classes co-located with their model.
- Keep routers thin and push logic into `services/`; keep query scoping in `db/tenant_filter` helpers.
- Frontend: typed API responses, forms via RHF+Zod, brand palette + instrument-panel styling.
