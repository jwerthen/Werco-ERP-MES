# Werco ERP-MES

A custom **Enterprise Resource Planning (ERP) + Manufacturing Execution System (MES)** for precision manufacturing (sheet metal, CNC, fabrication, welding, paint/powder coat, assembly, inspection). Built from the ground up for **AS9100D and ISO 9001** on a secure-by-default multi-tenant foundation — in this system, audit trails, lot/serial traceability, multi-tenant isolation, and role-based access control are correctness requirements, not optional features. A query that returns another tenant's rows, or a state change that isn't recorded in the tamper-evident audit log, is treated as a bug.

## What it does

Shipped modules, grouped by domain:

### Werco Copilot (AI assistant)
- **Ask-anything chat over your own ERP data** — a read-only copilot (header button or `Ctrl+.` from any screen) that answers shop questions ("where is WO-2024-0512?", "what's blocked?", "how loaded is the laser this week?") via Claude tool-use over live work orders, blockers, schedules, inventory, customers, and search, with deep links to the records it cites. Strictly read-only, tenant-scoped server-side, streamed answers, per-user rate-limited.
- **Always-on Action Inbox** — nightly sensors + learners mint recommendations without a prompt; Claude (existing Anthropic `run_llm_task`) auto-executes allowlisted actions (draft PO, draft NCR, expedite priority, escalate blocker). Humans can still Accept & apply. Ambient strips on WO/Part detail + dashboard morning brief. See [docs/AI_ALWAYS_ON.md](docs/AI_ALWAYS_ON.md).

### Sales & Quoting
- **AI RFQ quoting** — upload customer drawings (PDF/DXF/STEP); Anthropic Claude parses the package into parts and produces cost/lead-time estimates with confidence scores and stated assumptions, which a user reviews and converts to a quote.
- **Routing learning** — the AI improves routing/estimate suggestions from accepted historical jobs.
- Manual **quote calculator**, quote management, and customer records.

### Production & Shop Floor
- **Work orders** — release, dispatch, and full lifecycle tracking; priority P1–P10, customer-PO linkage, auto-loaded BOM and routing. A per-work-order **Sequential operations** switch decides how work reaches the floor: a sequenced routing unlocks one step at a time (the default for new work orders, so a multi-step assembly on one cell keeps its build order), while turning it off makes the operations a work-center **dispatch pool** that goes live together — what a batch job of per-item lines wants. Turning it on is refused while work is already under way out of sequence, and pulls only un-worked operations back off the board (see [docs/API.md](docs/API.md) → Work Orders → "READY promotion: a sequenced ROUTING or a DISPATCH POOL"). An optional **Unit #** names the single unit a one-unit-per-work-order job builds (the weld assemblies) and follows it everywhere the job appears — the work-order list and detail, the kiosk and crew station, the dispatch board, the shop TV, and the printed traveler — and it is searchable; work orders that don't track one are unchanged (see [docs/API.md](docs/API.md) → Work Orders → "Unit #"). **Duplicate** re-runs a job's *plan* onto a new draft — operations and their instructions, laser nests (sharing the drawing, not re-uploading it), open material ties (unpinned and unconsumed), and a fresh snapshot of the currently-released process-sheet steps — while the last run's production record stays with the original; anything the copy could not carry across is reported, not swallowed (see [docs/API.md](docs/API.md) → Work Orders → "Duplicating a work order").
- **Laser nest packages** — import a zipped Ermaksan nest package (CNC program files, or nest-report PDFs auto-read by AI with review-before-commit) or a bare nest-report PDF (single- or multi-page, AI-segmented into per-nest pages with per-field confidence) onto an assembly work order to build its laser-cutting child WO, or standalone from the Work Orders page to create a released, part-less laser work order sized in sheet runs (no parent WO or part required); each nest is a clock-in-able operation, with manual per-nest entry as the alternative path (see [docs/API.md](docs/API.md) → Laser Nests).
- **Shop-floor kiosk** — operator start/hold/resume/complete with qty produced/scrapped and notes; operator self-service over-count correction (walk back a good-count miscount on your own unapproved labor — current clock-in and your earlier sessions — before completion; audited, not scrap), plus a supervisor **Correct count** action on the work-order page for any operator's unapproved counts; badge/employee-ID login for kiosks.
- **Operator kiosk** (`/kiosk`) — touch-first screen for fixed station terminals: badge-scan login, two-tap clock-in from the station queue, report production / complete / hold / correct over-count with structured scrap and correction reasons (scrap can file an in-process NCR in the same transaction — no hold, the machine keeps running), a full-screen controlled drawing / nest viewer with critical-dims rail, idle auto-logout, and all writes tagged with the `kiosk` telemetry channel (see [docs/KIOSK.md](docs/KIOSK.md)).
- **Dispatch board** (`/dispatch`) — manager-controlled run order: one column per work center, drag a job up/down to set the order operators run it in (or across columns to move it to another machine), with keyboard Move up/down controls and a per-card machine select as the accessible equivalent. The rank shows as a `RUN n` chip and drives the queue order on every operator queue — kiosk, crew station, and the desktop shop-floor pages — but stays **advisory**: any queued job can still be started (admin / manager / supervisor; see [docs/API.md](docs/API.md) → Shop Floor → "Dispatch run order").
- **TV wallboard** — read-only, full-screen `/wallboard` board for shop TVs (the high-fidelity "Foundry" TV design): a HUD command bar with DOWN/BLOCKED/LATE alert chips, sync status, and a Central wall clock; a fixed 4×3 grid of priority-sorted work-order cards (current operation, stop reason, progress; held/down/blocked/late/running/waiting precedence) whose top row pins the four most severe jobs while the lower two rows cycle on a 22-second dwell, so every open work order reaches the wall instead of only the first twelve; a right rail (SHIP TODAY, LATE — oldest first, BLOCKED/DOWN, open NCRs + holds); and a live TODAY KPI footer; 30s refresh, per-department filter; authenticated by scoped, revocable display tokens that can reach no other endpoint (see [docs/WALLBOARD.md](docs/WALLBOARD.md)).
- **QR travelers & badge printing** — printed travelers carry URL QR codes (one job-page header QR plus a per-operation shop-floor deep link a phone can open directly) and an "UNCONTROLLED WHEN PRINTED" control footer (part rev, printed at / printed by); CR80 employee badges with QR-encoded employee IDs print from the Users page; `POST /scanner/resolve-action` resolves any scan — traveler URL, bare `OP:`/`WO:` code, or badge — to the operation / work order / employee and the shop-floor actions currently legal, with display-ready blocker reasons (scan-to-act lands in Phase 1; see [docs/KIOSK.md](docs/KIOSK.md) → Scanning).
- **Scheduling & dispatch**, **OEE** tracking, **downtime** logging, and operator **time tracking / time clock**.

### Engineering
- **Part master** (make/buy classification, critical-characteristic flags), multi-level **BOM**, **routing**, and **engineering change orders (ECO)** with revision control.
- **Process sheets** (`/process-sheets`) — typed, revision-controlled operation steps (measurement with tolerances, checkbox, list, value, photo/file evidence, instruction) authored in engineering with a draft → released → obsolete lifecycle; released sheets attach to routing operations, snapshot immutably onto work orders at creation, and are captured on the shop-floor kiosks: typed step recording with server-enforced tolerance refusal (out-of-tolerance is never stored), per-serial capture, rear-camera photo/file evidence, append-only supersede corrections, and operation completion gated on required steps. Quality-loop integrations are built in: conforming measurements feed SPC automatically, gauge calibration is enforced at capture (scan the gauge ID; out-of-cal is refused), an out-of-tolerance measurement offers a one-tap hold + NCR, every record freezes an operator-qualification snapshot, and AS9102 FAI forms pre-fill from step records (see [docs/PROCESS_SHEETS_SCOPE.md](docs/PROCESS_SHEETS_SCOPE.md)).

### Warehouse
- Unified, tabbed warehouse: **inventory** (on-hand, reorder, low-stock, transfers), **receiving** with accept/reject inspection and lot capture, and **shipping** (create shipment, mark shipped, print packing slip).
- **Multi-carrier shipping** via a swappable aggregator (EasyPost-first): address validation, rate-shopping, label purchase, pickups, and inbound tracking — behind a per-company customer-data egress kill switch (default OFF) for CUI control. Parcel is fully implemented; LTL freight is scaffolded behind the same interface (see [docs/SHIPPING_CARRIER_INTEGRATION.md](docs/SHIPPING_CARRIER_INTEGRATION.md)).
- **Thermal receiving labels** — a 4×6 label (part / rev / qty / lot / Code128, CRITICAL banner for critical parts) printed on inventory receipt to a Westinghouse WHTP203e via a ProxyBox Zero (pbxz.io) bridge; manual reprint and auto-print-on-receipt, behind a per-company outbound-egress kill switch (default OFF) for CUI control (see [docs/THERMAL_LABEL_PRINTING.md](docs/THERMAL_LABEL_PRINTING.md)).
- **Combine / merge SKUs** — when a numbering recut leaves two part numbers describing the same physical article, fold one onto the other with a full preview first (blockers, per-lot lines, reservations, the cost delta). The move is net-zero by construction — two linked `ADJUST` ledger rows per lot line summing to exactly zero — never a receive (which would mint stock) and never an issue against a fabricated work order (which would destroy it). Lot, serial, cert and heat-lot traceability follow the material; costs are never reblended; the source number is retired, never deleted (see [docs/API.md](docs/API.md) → Inventory → "Combining two SKUs").
- **Lot/serial traceability** and genealogy.

### Purchasing & Supply Chain
- Vendors, **purchase orders** (create/send), **MRP** (shortage detection and suggested-PO generation), receiving, **supplier scorecards**, and **PO upload** (AI parsing of PO/quote PDFs, single or multi-document batch).

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

- **`backend/`** — FastAPI app under `app/`: thin routers in `api/endpoints/` (~59 routers under `/api/v1/`), business logic in `services/`, SQLAlchemy 2.0 `models/`, Pydantic 2 `schemas/`, and the auth/tenancy/RBAC dependency seam in `api/deps.py`.
- **`frontend/`** — React 19 + TypeScript + Vite SPA; typed Axios client with ETag conditional caching and a refresh-token interceptor; React Context for auth and active-company switching.
- **`landing/`** — separate marketing site (React + Vite), deployed independently.
- **`docs/`** — operational runbooks and compliance documents.
- **infra** — `docker-compose*.yml`, `nginx/`, `supabase/`, `load-tests/`.

Cross-cutting platform properties:
- **Multi-tenant** — domain tables carry `company_id` (`TenantMixin`); every query is scoped to the active company.
- **Background work** — Redis 7 + **ARQ workers** (`app/worker.py`, `app/jobs/`) for email, MRP runs, and long tasks; enqueued from services, never blocking request handlers.
- **Realtime** — WebSocket push for live shop-floor activity and dashboard updates.
- **Tamper-evident audit** — the `audit_log` table is append-only at the database layer (triggers refuse `UPDATE`/`DELETE`) and carries a SHA-256 hash chain (`sequence_number`, `previous_hash`, `integrity_hash`); state changes flow through `AuditService`. The chain is on by default and pausable via `AUDIT_HASH_CHAIN_ENABLED` (the triggers are not). (Known gap: the interactive user-management and work-center endpoints do not yet emit audit entries — their bulk-import endpoints do — see Compliance below.)
- **Auth** — JWT, ~15-min access token, ~7-day rotating refresh, ~7-day session cap (`SESSION_ABSOLUTE_TIMEOUT_HOURS`, default 168h — it restarts on every refresh, so it bounds an *idle* window rather than total session life); account lockout after 5 failed password attempts (the email/password login path). Password policy is ≥ 12 characters plus a weak-password blocklist — no character-class rules (NIST SP 800-63B §5.1.1.2).
- **Agent access (MCP)** — `backend/app/mcp/` serves the API to agents (Cursor, Claude Code, bots) as Model Context Protocol tools: 15 fixed-name convenience tools plus ~660 generated from the app's own OpenAPI document at startup, every call dispatched back through the real routers as the caller's user — same RBAC, tenancy and audit, no god token. A Streamable HTTP door at `/mcp` (off by default, `WERCO_MCP_HTTP_ENABLED`) and a stdio bridge (`python -m app.mcp`). See [docs/MCP.md](docs/MCP.md).

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI 0.136, Starlette 1.3, Uvicorn/Gunicorn |
| ORM / DB | SQLAlchemy 2.0, Alembic 1.18, PostgreSQL (Supabase), psycopg2 |
| Validation | Pydantic 2.12 + pydantic-settings |
| Auth / security | python-jose (JWT), passlib + bcrypt, slowapi (rate limiting) |
| Agent access | `mcp` 2.1.1 (Model Context Protocol SDK), jsonschema |
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
| [docs/MCP.md](docs/MCP.md) | The API as MCP tools for agents: HTTP door / stdio bridge, JWT auth, page → tool program map, naming rules, DRAFT guarantees, result shapes, troubleshooting |
| [docs/RBAC_PERMISSIONS.md](docs/RBAC_PERMISSIONS.md) | The 8-role permission model |
| [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md) | All config and secrets |
| [docs/EXCEL_MIGRATION_RUNBOOK.md](docs/EXCEL_MIGRATION_RUNBOOK.md) | Go-live migration off Excel: load order, dry-run discipline, rehearsals, cutover checklist |
| [docs/AI_ALWAYS_ON.md](docs/AI_ALWAYS_ON.md) | Always-on sensors, outcome capture, Action Inbox learning loop |
| [docs/AI_QUOTING_AGENT_RUNBOOK.md](docs/AI_QUOTING_AGENT_RUNBOOK.md) | Operating the Anthropic-powered RFQ/quoting feature |
| [docs/IMPLEMENTATION_NOTES_AI_QUOTING_AGENT.md](docs/IMPLEMENTATION_NOTES_AI_QUOTING_AGENT.md) | AI quoting design/implementation notes |
| [docs/CMMC_LEVEL_2_COMPLIANCE.md](docs/CMMC_LEVEL_2_COMPLIANCE.md) | CMMC L2 roadmap — **frozen 2026-07-28**, historical record only |
| [docs/AUDIT_LOG_RETENTION_RUNBOOK.md](docs/AUDIT_LOG_RETENTION_RUNBOOK.md) | Audit-log retention operations |
| [docs/DATABASE_BACKUP.md](docs/DATABASE_BACKUP.md) | Backup and restore procedures |
| [docs/onboarding/](docs/onboarding/README.md) | **Employee onboarding & training** — plain-language, role-by-role guides (Getting Started, Operator/Shop-Floor, Warehouse, Planner/Supervisor/Manager, Admin/IT) with screenshots and printable PDF handouts |
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | Developer onboarding & local setup |
| [docs/BROWSER_HARNESS.md](docs/BROWSER_HARNESS.md) | Safe headless-Chromium CLI for screenshots/snapshots/logs/PDFs |
| [docs/WALLBOARD.md](docs/WALLBOARD.md) | Shop-floor TV wallboard: display-token setup, kiosk-mode TVs, revocation |
| [docs/THERMAL_LABEL_PRINTING.md](docs/THERMAL_LABEL_PRINTING.md) | 4×6 thermal receiving labels: ProxyBox/WHTP203e setup, egress kill switch, manual reprint vs. auto-print, troubleshooting |
| [docs/SMOKE_TESTS.md](docs/SMOKE_TESTS.md) · [docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md) · [docs/PRODUCTION_CHECKLIST.md](docs/PRODUCTION_CHECKLIST.md) | Pre-launch verification |

## Compliance

Built for **AS9100D** and **ISO 9001**, on a secure-by-default multi-tenant foundation. The mechanisms below are enforced in code as correctness invariants:

- **Tenant isolation** — `company_id` scoping on all domain data; cross-tenant reads are defects.
- **Tamper-evident audit log** — append-only at the DB layer (migration `008`/`060` triggers refuse `UPDATE`/`DELETE`, independent of any setting), with a SHA-256 hash chain over create/update/delete/status-change events; never backfilled or edited out of band. **The hash chain is runtime-pausable** via `AUDIT_HASH_CHAIN_ENABLED` — it **defaults to on**, and pausing it is not fully reversible (rows written while paused can't be verified retroactively); see [docs/AUDIT_LOG_RETENTION_RUNBOOK.md](docs/AUDIT_LOG_RETENTION_RUNBOOK.md) → Pausing the hash chain. The interactive user-management endpoints (`app/api/endpoints/users.py` — create/update/activate/deactivate/role-change/password-reset) and work-center **update/deactivate** (`PUT`/`DELETE /work-centers/{id}` — deactivation also **refuses with a 409** while live operations still reference the machine) now write audit entries. **Remaining coverage gap:** interactive work-center **create** (`POST /work-centers/`) and the **status dropdown** (`POST /work-centers/{id}/status`) still emit **no** audit entries; do not represent those two actions as audited until they route through `AuditService`. (The bulk-import endpoints in both routers — `/users/import-csv`, `/work-centers/import-csv` — **do** audit every created row, tagged `source = "import"`.)
- **Soft delete** — `SoftDeleteMixin` (`is_deleted` / `deleted_at` / `deleted_by`); no physical deletes on traced data.
- **Traceability** — part/BOM revision control, critical-characteristic flags, and lot/serial genealogy; shipped data is preserved via new revisions rather than mutation.
- **RBAC + access control** — server-side role gating, account lockout (5 failed password attempts → 30-min lock, email/password login path), JWT session caps.

**CMMC Level 2 is not being pursued at this time** (deprioritized 2026-07-28). The controls above are unaffected — they protect tenant data and the quality system on their own merits, independent of any certification. See **[docs/CMMC_LEVEL_2_COMPLIANCE.md](docs/CMMC_LEVEL_2_COMPLIANCE.md)** for the frozen historical roadmap.

## Project structure

```
Werco-ERP-MES/
├── backend/                  # Python 3.11 / FastAPI
│   ├── app/
│   │   ├── api/
│   │   │   ├── endpoints/     # ~59 REST routers under /api/v1/
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
Built for Werco Manufacturing — AS9100D / ISO 9001.
