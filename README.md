# Werco ERP-MES

A custom **Enterprise Resource Planning (ERP) + Manufacturing Execution System (MES)** for precision manufacturing (sheet metal, CNC, fabrication, welding, paint/powder coat, assembly, inspection). Built from the ground up for **AS9100D, ISO 9001, and CMMC Level 2** — in this system, audit trails, lot/serial traceability, multi-tenant isolation, and role-based access control are correctness requirements, not optional features. A query that returns another tenant's rows, or a state change that isn't recorded in the tamper-evident audit log, is treated as a bug.

## What it does

Shipped modules, grouped by domain:

### Werco Copilot (AI assistant)
- **Ask-anything chat over your own ERP data** — a read-only copilot (header button or `Ctrl+.` from any screen) that answers shop questions ("where is WO-2024-0512?", "what's blocked?", "how loaded is the laser this week?") via Claude tool-use over live work orders, blockers, schedules, inventory, customers, and search, with deep links to the records it cites. Strictly read-only, tenant-scoped server-side, streamed answers, per-user rate-limited.

### Sales & Quoting
- **AI RFQ quoting** — upload customer drawings (PDF/DXF/STEP); Anthropic Claude parses the package into parts and produces cost/lead-time estimates with confidence scores and stated assumptions, which a user reviews and converts to a quote.
- **Routing learning** — the AI improves routing/estimate suggestions from accepted historical jobs.
- Manual **quote calculator**, quote management, and customer records.

### Production & Shop Floor
- **Work orders** — release, dispatch, and full lifecycle tracking; priority P1–P10, customer-PO linkage, auto-loaded BOM and routing.
- **Shop-floor kiosk** — operator start/hold/resume/complete with qty produced/scrapped and notes; badge/employee-ID login for kiosks.
- **Operator kiosk** (`/kiosk`) — touch-first screen for fixed station terminals: badge-scan login, two-tap clock-in from the station queue, report production / complete / hold with structured scrap and hold reasons, idle auto-logout, and all writes tagged with the `kiosk` telemetry channel (see [docs/KIOSK.md](docs/KIOSK.md)).
- **TV wallboard** — read-only, full-screen `/wallboard` board for shop TVs: per-work-center live jobs, queue/blocker/downtime status, late & blocked tickers; 30s refresh, per-department filter; authenticated by scoped, revocable display tokens that can reach no other endpoint (see [docs/WALLBOARD.md](docs/WALLBOARD.md)).
- **QR travelers & badge printing** — printed travelers carry URL QR codes (one job-page header QR plus a per-operation shop-floor deep link a phone can open directly) and an "UNCONTROLLED WHEN PRINTED" control footer (part rev, printed at / printed by); CR80 employee badges with QR-encoded employee IDs print from the Users page; `POST /scanner/resolve-action` resolves any scan — traveler URL, bare `OP:`/`WO:` code, or badge — to the operation / work order / employee and the shop-floor actions currently legal, with display-ready blocker reasons (scan-to-act lands in Phase 1; see [docs/KIOSK.md](docs/KIOSK.md) → Scanning).
- **Scheduling & dispatch**, **OEE** tracking, **downtime** logging, and operator **time tracking / time clock**.

### Engineering
- **Part master** (make/buy classification, critical-characteristic flags), multi-level **BOM**, **routing**, and **engineering change orders (ECO)** with revision control.

### Warehouse
- Unified, tabbed warehouse: **inventory** (on-hand, reorder, low-stock, transfers), **receiving** with accept/reject inspection and lot capture, and **shipping** (create shipment, mark shipped, print packing slip).
- **Multi-carrier shipping** via a swappable aggregator (EasyPost-first): address validation, rate-shopping, label purchase, pickups, and inbound tracking — behind a per-company customer-data egress kill switch (default OFF) for CUI control. Parcel is fully implemented; LTL freight is scaffolded behind the same interface (see [docs/SHIPPING_CARRIER_INTEGRATION.md](docs/SHIPPING_CARRIER_INTEGRATION.md)).
- **Thermal receiving labels** — a 4×6 label (part / rev / qty / lot / Code128, CRITICAL banner for critical parts) printed on inventory receipt to a Westinghouse WHTP203e via a ProxyBox Zero (pbxz.io) bridge; manual reprint and auto-print-on-receipt, behind a per-company outbound-egress kill switch (default OFF) for CUI control (see [docs/THERMAL_LABEL_PRINTING.md](docs/THERMAL_LABEL_PRINTING.md)).
- **Lot/serial traceability** and genealogy.

### Purchasing & Supply Chain
- Vendors, **purchase orders** (create/send), **MRP** (shortage detection and suggested-PO generation), receiving, **supplier scorecards**, and **PO upload** (AI parsing of PO/quote PDFs).

### Quality & Compliance
- **NCR / CAR / FAI**, **SPC**, **calibration** management, **customer complaints**, **QMS standards**, and **operator certifications / skill matrix**.

### Maintenance & Tooling
- Preventive/corrective **maintenance** and **tool management**.

### Analytics & Reporting
- **Analytics** (production, quality, inventory, forecasting, costs), **reports**, and **job costing**.

### Administration & Governance
- **RBAC** (8 roles, server-side `require_role` gating), **multi-tenant** company scoping, **tamper-evident audit log** (SHA-256 hash chain), users/employee provisioning, work centers, custom fields, admin settings (incl. an **AI usage & cost** dashboard — per-task/per-model token, spend, and latency telemetry for the LLM features), setup wizard, and a platform-admin overview for cross-company oversight.
- **Import Center / Excel migration kit** — XLSX + CSV bulk imports (users, parts, materials, customers, vendors, work centers, routings) plus open-work-order and open-purchase-order loaders for go-live, with server-generated Excel templates and a dry-run-preview-then-commit flow (see [docs/EXCEL_MIGRATION_RUNBOOK.md](docs/EXCEL_MIGRATION_RUNBOOK.md)).

## Architecture

Monorepo with a layered FastAPI backend and a React SPA frontend.

- **`backend/`** — FastAPI app under `app/`: thin routers in `api/endpoints/` (~53 routers under `/api/v1/`), business logic in `services/`, SQLAlchemy 2.0 `models/`, Pydantic 2 `schemas/`, and the auth/tenancy/RBAC dependency seam in `api/deps.py`.
- **`frontend/`** — React 19 + TypeScript + Vite SPA; typed Axios client with ETag conditional caching and a refresh-token interceptor; React Context for auth and active-company switching.
- **`landing/`** — separate marketing site (React + Vite), deployed independently.
- **`docs/`** — operational runbooks and compliance documents.
- **infra** — `docker-compose*.yml`, `nginx/`, `supabase/`, `load-tests/`.

Cross-cutting platform properties:
- **Multi-tenant** — domain tables carry `company_id` (`TenantMixin`); every query is scoped to the active company.
- **Background work** — Redis 7 + **ARQ workers** (`app/worker.py`, `app/jobs/`) for email, MRP runs, and long tasks; enqueued from services, never blocking request handlers.
- **Realtime** — WebSocket push for live shop-floor activity and dashboard updates.
- **Tamper-evident audit** — the `audit_log` table is an append-only SHA-256 hash chain (`sequence_number`, `previous_hash`, `integrity_hash`); state changes flow through `AuditService`. (Known gap: the interactive user-management and work-center endpoints do not yet emit audit entries — their bulk-import endpoints do — see Compliance below.)
- **Auth** — JWT, ~15-min access token, ~7-day rotating refresh, 24h absolute session cap; account lockout after 5 failed password attempts (the email/password login path).

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI 0.136, Starlette 1.2, Uvicorn/Gunicorn |
| ORM / DB | SQLAlchemy 2.0, Alembic 1.18, PostgreSQL (Supabase), psycopg2 |
| Validation | Pydantic 2.12 + pydantic-settings |
| Auth / security | python-jose (JWT), passlib + bcrypt, slowapi (rate limiting), bleach |
| Background jobs | Redis 7, ARQ, croniter |
| Realtime | websockets |
| AI / LLM | Anthropic Claude (`anthropic` SDK) — Haiku / Sonnet / Opus tiers |
| Document parsing | pypdf, pdf2image, pytesseract (OCR), python-docx, ezdxf (DXF), rapidfuzz |
| Email / export | aiosmtplib, Jinja2, openpyxl, pandas, reportlab |
| Monitoring | Sentry (optional) |
| Frontend | React 19, TypeScript 5.9, Vite 7, React Router 7 |
| UI / styling | Tailwind CSS 4, DaisyUI, Heroicons, Headless UI |
| Forms / data | React Hook Form 7, Zod 4, Axios, Recharts, date-fns |
| Frontend testing | Jest 30, Testing Library, Playwright |

## Quick start (development)

```bash
docker compose up
```

This brings up the backend (`:8000`), frontend (`:3000`), Redis, and the ARQ worker.

**Required environment variables** (the compose file fails fast without them):

- `DATABASE_URL` — a **Supabase Postgres** connection string. **There is no bundled Postgres container** — you must point at a Supabase (or other Postgres) instance.
- `SECRET_KEY` — JWT signing secret.
- `REFRESH_TOKEN_SECRET_KEY` — refresh-token signing secret.

Set `ANTHROPIC_API_KEY` to enable the AI features (Werco Copilot chat, RFQ quoting, PO/BOM/QMS document parsing, routing learning, natural-language search). See **[docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md)** for the full list (Anthropic model tiers, SMTP, Sentry, Redis, webhook key, etc.).

Once up:

- Frontend: <http://localhost:3000>
- Backend API: <http://localhost:8000>
- API docs (OpenAPI/Swagger): <http://localhost:8000/api/docs> (ReDoc at `/api/redoc`)

**Seed the database (first time):**

```bash
docker compose exec backend python -m scripts.seed_data
```

**Frontend without Docker** (Vite dev server):

```bash
cd frontend
npm install
npm run dev
```

## Default accounts (development)

The seed script (`backend/scripts/seed_data.py`) creates these accounts in the demo company. **Passwords are intentionally omitted here** — read them from the seed script if you need them.

| Email | Role |
|-------|------|
| `admin@werco.com` | admin (superuser) |
| `jsmith@werco.com` | manager |
| `mjohnson@werco.com` | supervisor |
| `bwilliams@werco.com` | operator |
| `sjones@werco.com` | quality |
| `dwilson@werco.com` | operator |

> ⚠️ **These are development seed credentials only. Change them before any non-development use.** The first user ever created on a fresh system is automatically promoted to `platform_admin` during initial setup.

## User roles

Eight roles, gated server-side via `require_role()`. Writes/state-changes are role-gated; operational reads (work orders, parts, BOMs, routings, inventory, purchasing, receiving, shipping, quality) are tenant-scoped but readable by any authenticated user; administrative reads (users, admin settings, audit logs) are role-gated. All data is company-scoped (multi-tenant). See **[docs/RBAC_PERMISSIONS.md](docs/RBAC_PERMISSIONS.md)** for the full permission matrix.

| Role | Scope |
|------|-------|
| `platform_admin` | Werco oversight — can switch company context; read-only cross-company access |
| `admin` | Full access including Admin Settings, single company |
| `manager` | Broad operational control and approvals; no admin-only settings |
| `supervisor` | Shop execution and planning; limited user/admin controls |
| `operator` | Execute work only (shop-floor kiosk) |
| `quality` | Inspections and quality approvals |
| `shipping` | Shipping operations |
| `viewer` | Read-only (auditors, executives, guests) |

## Deployment

- **Backend + ARQ worker** → Railway
- **Frontend + landing site** → Vercel
- **Database** → Supabase (PostgreSQL)
- **CI/CD** → GitHub Actions

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**, **[docs/DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md)**, **[docs/DOCKER_PRODUCTION.md](docs/DOCKER_PRODUCTION.md)**, and **[docs/CI_CD_SETUP.md](docs/CI_CD_SETUP.md)**.

## Documentation

| Document | What it covers |
|----------|----------------|
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local dev setup, the `create_all` → `stamp` → `upgrade` bootstrap path |
| [docs/API.md](docs/API.md) | REST endpoint reference (OpenAPI lives at `/api/docs`) |
| [docs/RBAC_PERMISSIONS.md](docs/RBAC_PERMISSIONS.md) | The 8-role permission model |
| [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md) | All config and secrets |
| [docs/EXCEL_MIGRATION_RUNBOOK.md](docs/EXCEL_MIGRATION_RUNBOOK.md) | Go-live migration off Excel: load order, dry-run discipline, rehearsals, cutover checklist |
| [docs/AI_QUOTING_AGENT_RUNBOOK.md](docs/AI_QUOTING_AGENT_RUNBOOK.md) | Operating the Anthropic-powered RFQ/quoting feature |
| [docs/IMPLEMENTATION_NOTES_AI_QUOTING_AGENT.md](docs/IMPLEMENTATION_NOTES_AI_QUOTING_AGENT.md) | AI quoting design/implementation notes |
| [docs/CMMC_LEVEL_2_COMPLIANCE.md](docs/CMMC_LEVEL_2_COMPLIANCE.md) | CMMC L2 compliance posture |
| [docs/AUDIT_LOG_RETENTION_RUNBOOK.md](docs/AUDIT_LOG_RETENTION_RUNBOOK.md) | Audit-log retention operations |
| [docs/DATABASE_BACKUP.md](docs/DATABASE_BACKUP.md) | Backup and restore procedures |
| [docs/onboarding/](docs/onboarding/README.md) | **Employee onboarding & training** — plain-language, role-by-role guides (Getting Started, Operator/Shop-Floor, Warehouse, Planner/Supervisor/Manager, Admin/IT) with screenshots and printable PDF handouts |
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | Developer onboarding & local setup |
| [docs/BROWSER_HARNESS.md](docs/BROWSER_HARNESS.md) | Safe headless-Chromium CLI for screenshots/snapshots/logs/PDFs |
| [docs/WALLBOARD.md](docs/WALLBOARD.md) | Shop-floor TV wallboard: display-token setup, kiosk-mode TVs, revocation |
| [docs/THERMAL_LABEL_PRINTING.md](docs/THERMAL_LABEL_PRINTING.md) | 4×6 thermal receiving labels: ProxyBox/WHTP203e setup, egress kill switch, manual reprint vs. auto-print, troubleshooting |
| [docs/SMOKE_TESTS.md](docs/SMOKE_TESTS.md) · [docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md) · [docs/PRODUCTION_CHECKLIST.md](docs/PRODUCTION_CHECKLIST.md) | Pre-launch verification |

## Compliance

Built for **AS9100D**, **ISO 9001**, and **CMMC Level 2**. The mechanisms below are enforced in code as correctness invariants:

- **Tenant isolation** — `company_id` scoping on all domain data; cross-tenant reads are defects.
- **Tamper-evident audit log** — append-only SHA-256 hash chain over create/update/delete/status-change events; never backfilled or edited out of band. **Known coverage gap:** the **interactive** user-management endpoints (`app/api/endpoints/users.py` — create/update/activate/deactivate/role-change/password-reset) and work-center endpoints (`app/api/endpoints/work_centers.py`) currently emit **no** audit entries; do not represent those actions as audited to auditors until they route through `AuditService`. (The bulk-import endpoints in both routers — `/users/import-csv`, `/work-centers/import-csv` — **do** audit every created row, tagged `source = "import"`.)
- **Soft delete** — `SoftDeleteMixin` (`is_deleted` / `deleted_at` / `deleted_by`); no physical deletes on traced data.
- **Traceability** — part/BOM revision control, critical-characteristic flags, and lot/serial genealogy; shipped data is preserved via new revisions rather than mutation.
- **RBAC + access control** — server-side role gating, account lockout (5 failed password attempts → 30-min lock, email/password login path), JWT session caps.

See **[docs/CMMC_LEVEL_2_COMPLIANCE.md](docs/CMMC_LEVEL_2_COMPLIANCE.md)**.

## Project structure

```
Werco-ERP-MES/
├── backend/                  # Python 3.11 / FastAPI
│   ├── app/
│   │   ├── api/
│   │   │   ├── endpoints/     # ~53 REST routers under /api/v1/
│   │   │   └── deps.py        # auth / tenancy / RBAC dependency seam
│   │   ├── core/             # config, security, cache, pagination, realtime
│   │   ├── db/               # database, mixins, tenant_filter helpers
│   │   ├── models/           # SQLAlchemy 2.0 models
│   │   ├── schemas/          # Pydantic 2 request/response contracts
│   │   ├── services/         # business logic (incl. audit_service.py)
│   │   ├── jobs/             # ARQ background jobs
│   │   ├── worker.py         # ARQ worker entrypoint
│   │   └── main.py           # app factory + middleware
│   ├── alembic/              # migrations
│   ├── scripts/             # seed_data.py and utilities
│   └── tests/                # pytest suite
├── frontend/                 # React 19 + TypeScript + Vite SPA
│   └── src/
│       ├── pages/            # route-level screens
│       ├── components/       # reusable UI by domain
│       ├── services/         # Axios API client (ETag + refresh interceptor)
│       ├── context/          # auth, active-company, shortcuts, tours
│       └── validation/       # Zod schemas
├── landing/                  # marketing site (React + Vite → Vercel)
├── load-tests/               # load testing suite
├── docs/                     # runbooks + compliance docs
├── nginx/ · supabase/        # infra
└── docker-compose*.yml
```

## Support

For questions or issues, contact the Werco IT department.

---
Built for Werco Manufacturing — AS9100D / ISO 9001 / CMMC Level 2.
