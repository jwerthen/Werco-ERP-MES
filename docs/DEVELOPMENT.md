# Development Guide

This guide covers development practices, testing, and contribution guidelines for Werco ERP.

## Environment Setup

### Prerequisites
- Python 3.11+
- Node.js 22+ (LTS; see frontend/.nvmrc)
- PostgreSQL 15+ (only for the Postgres/Supabase path; local dev defaults to SQLite)
- Docker & Docker Compose (optional but recommended)
- Git

### Local Development Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd Werco-ERP
   ```

2. **Backend Setup**
   ```bash
   cd backend
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

3. **Frontend Setup**
   ```bash
   cd frontend
   npm install
   ```

4. **Environment Variables**
   ```bash
   copy backend\.env.example backend\.env
   # Edit backend\.env with your configuration
   ```

5. **Database Setup**

   You can develop against either local SQLite (default, zero-setup) or Postgres.

   - **Local SQLite (default):** `backend/.env.example` ships with
     `DATABASE_URL=sqlite:///./werco_dev.db` — no database server required.
     Tables are created by the seed script (next step); `alembic upgrade head`
     is **not** used on SQLite (the migrations are Postgres-targeted and query
     `information_schema` / `table_schema='public'`, so they fail on SQLite).
   - **Postgres / Supabase:** point `DATABASE_URL` at a Postgres instance and
     use Alembic for schema management (see the next step and "Database Migrations" below).
     ```bash
     # Optional local Postgres via Docker
     docker-compose up -d db
     ```

6. **Create Schema**
   - **Local SQLite:** run the seed script — it calls `Base.metadata.create_all`
     and then seeds demo data (the test suite bootstraps the same way via
     `tests/conftest.py`):
     ```bash
     cd backend
     python -m scripts.seed_data
     ```
     Seeds the demo company with login users — `admin@werco.com / admin123`
     (admin) and the remaining seeded users at `<email> / password123`
     (e.g. `jsmith@werco.com` manager, `bwilliams@werco.com` operator).
   - **Postgres / Supabase:** apply Alembic migrations instead of `create_all`:
     ```bash
     cd backend
     python -m alembic upgrade head
     # then optionally seed
     python -m scripts.seed_data
     # (or, inside the container) docker-compose exec backend python -m scripts.seed_data
     ```

   > **Seeding is dev/demo only.** `python -m scripts.seed_data` creates demo accounts with
   > well-known passwords, so it **refuses to run when `ENVIRONMENT=production` or when the
   > database target is the Supabase production Postgres** — the CLI seeder exits with an error (override for a throwaway sandbox with `SEED_ALLOW_PRODUCTION=1`;
   > see docs/ENVIRONMENT_VARIABLES.md → Seed Data). Production tenants are bootstrapped
   > through the company-onboarding flow (`POST /api/v1/companies/register`), which enforces
   > the password-strength policy.

## Development Workflow

### Running the Application

**Backend (Development)**
```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

**Frontend (Development)**
```bash
cd frontend
npm start
```
The Vite dev server runs on **http://localhost:5173**. It reads
`REACT_APP_*`-prefixed env vars (injected via `vite.config.ts` `define`);
set `REACT_APP_API_URL=http://localhost:8000/api/v1` in `frontend/.env`
(the app also defaults to that when unset — see `src/services/api.ts`).
The backend's default `CORS_ORIGINS` already includes `http://localhost:5173`,
so the SPA can call the API in local dev without extra config.

**Using Docker Compose**
```bash
docker-compose up
```

### Code Quality

**Backend**
```bash
cd backend

# Format code
black app/
isort app/

# Lint code
flake8 app/

# Type checking
mypy app

# Run security checks
bandit -r app

# Run tests
pytest tests/ -v
pytest tests/ --cov=app --cov-report=html
```

**Frontend**
```bash
cd frontend

# Format code
npm run format

# Lint code
npm run lint
npm run lint:fix

# Type checking
npm run type-check

# Run tests
npm test
npm run test:coverage
```

### Pre-commit Hooks

Pre-commit hooks are configured to run automatically before commits:
```bash
# Install hooks (first time only)
cd frontend
npm run prepare
cd ..
pre-commit install

# Run every hook against the whole tree (from the REPO ROOT, not backend/)
pre-commit run --all-files
```

The hooks are deliberately **validity and secrets only** — `check-yaml`,
`check-toml`, `check-json`, `check-added-large-files`, `check-merge-conflict`,
and `detect-private-key`. The whole run takes well under a second and modifies
nothing on a clean tree.

**There are intentionally no lint/format hooks.** Linting and formatting are
enforced in CI, blocking, at the pinned versions and from the correct working
directory — run them yourself from `backend/` (see CLAUDE.md → Commands).

> **History worth knowing (fixed 2026-07-28).** `.pre-commit-config.yaml` was
> invalid YAML from its introduction — `exclude:^.env.example$` was missing the
> space after the colon — so `pre-commit` could not load the config and *every*
> hook was silently dead, including `detect-private-key`. `check-yaml` could not
> catch it, because check-yaml is configured by the very file that failed to
> parse. `backend/tests/test_precommit_config.py` now guards against a
> recurrence, and CI runs `detect-private-key` directly in the `Secret Scanning`
> job so the control no longer depends on each developer having run
> `pre-commit install`.
>
> Reviving the config also revealed that its black/isort/flake8/bandit hooks
> could never have worked: they pinned black 23 / isort 5 / flake8 7.0 against
> the repo's black 26 / isort 7 / flake8 7.3, flake8 never found `backend/.flake8`
> (pre-commit runs from the repo root), `--extend-ignore=E203,W503` split on its
> comma so `W503` was passed as a filename, black/isort would have rewritten 35
> and 49 **applied** Alembic migrations respectively (their exclude settings do
> not apply to explicitly-passed files), and bandit pointed at a nonexistent
> `backend/pyproject.toml`. They were removed rather than repaired — CI already
> enforces all of them correctly.
>
> Scope honesty: `detect-private-key` finds PRIVATE KEY blocks, not API tokens
> or passwords, and Trivy's non-blocking fs secret scan covers only `backend/`
> and `frontend/`. Broader secret scanning (GitHub secret scanning or gitleaks)
> remains an open decision.

## Testing

### Backend Testing

**Run all tests**
```bash
cd backend
pytest tests/ -v
```

**Run with coverage**
```bash
pytest tests/ --cov=app --cov-report=html --cov-report=term
```

**Run specific test file**
```bash
pytest tests/api/test_work_orders.py -v
```

**Run specific test**
```bash
pytest tests/api/test_work_orders.py::TestWorkOrdersAPI::test_create_work_order -v
```

**Run by marker**
```bash
pytest tests/ -m unit  # Unit tests only
pytest tests/ -m api   # API tests only
pytest tests/ -m integration  # Integration tests only
```

### Frontend Testing

**Run all tests**
```bash
cd frontend
npm test
```

**Run tests in watch mode**
```bash
npm run test:watch
```

**Run tests with coverage**
```bash
npm run test:coverage
```

### E2E Testing (Playwright)

The Playwright suite (`frontend/e2e/*.spec.ts`) drives a real browser against a running
full stack — it needs a **seeded backend**, not mocks:

```bash
# 1. Seed the database (creates the schema + default company, users, work centers,
#    parts, and work orders the specs expect)
cd backend
python -m scripts.seed_data

# 2. Start the API with the rate limiter OFF. Nearly every spec logs in through the
#    UI and /api/v1/auth/login is limited to 5/min — with limits on, the suite 429s.
RATE_LIMIT_ENABLED=false uvicorn app.main:app --port 8000

# 3. Run the suite (locally, playwright.config.ts starts the Vite dev server itself)
cd frontend
npm run test:e2e             # or: npx playwright test
npx playwright show-report   # open the HTML report
```

Credentials come from `E2E_*` env vars, which must be **exported in the shell that runs
Playwright** — nothing loads `frontend/.env` into the test-runner process
(`frontend/.env.example` documents the canonical values; see also
`docs/ENVIRONMENT_VARIABLES.md` → E2E Testing). **Subtlety:
the fixture defaults in `frontend/e2e/fixtures.ts` are `manager@werco.com` /
`operator@werco.com`, which the seed does NOT create.** The email overrides are required
— `E2E_ADMIN_EMAIL=admin@werco.com` / `E2E_ADMIN_SECRET=admin123`,
`E2E_MANAGER_EMAIL=jsmith@werco.com` and `E2E_OPERATOR_EMAIL=bwilliams@werco.com` (both
`password123`) — to match the users `scripts/seed_data.py` actually creates.

The crew-station kiosk tests (`e2e/crew-station-kiosk.spec.ts`) self-skip their
station flows unless `E2E_KIOSK_STATION_ID` / `E2E_KIOSK_PIN` / `E2E_BADGE_A` /
`E2E_BADGE_B` point at a provisioned station and badges — the seed does not create one
yet (known follow-up); the admin-modal test still runs.

In CI the same suite runs via `.github/workflows/e2e.yml` — see the CI/CD Pipeline
section below.

### Test Coverage Targets

- Backend: 70% minimum coverage
- Frontend: 70% minimum coverage

## Project Structure

```
Werco-ERP/
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI endpoints
│   │   ├── core/             # Configuration, security, cache
│   │   ├── db/               # Database setup and connection
│   │   ├── models/           # SQLAlchemy ORM models
│   │   ├── schemas/          # Pydantic schemas for validation
│   │   ├── services/         # Business logic
│   │   └── main.py           # FastAPI application entry point
│   ├── tests/                # Backend tests
│   │   ├── api/              # API endpoint tests
│   │   ├── conftest.py       # Pytest fixtures
│   │   └── ...
│   ├── alembic/              # Database migrations
│   ├── scripts/              # Utility scripts
│   ├── requirements.txt      # Production dependencies
│   └── requirements-dev.txt  # Development dependencies
├── frontend/
│   ├── src/
│   │   ├── components/       # Reusable React components
│   │   ├── pages/            # Page-level components
│   │   ├── services/         # API client and data fetching
│   │   ├── context/          # React context providers
│   │   └── types/            # TypeScript type definitions
│   ├── public/               # Static assets
│   ├── jest.config.js        # Jest configuration
│   ├── tsconfig.json         # TypeScript configuration
│   └── package.json          # Node dependencies
└── docs/                     # Documentation
```

## Frontend API error handling

FastAPI **422** (validation) responses return `detail` as an **array** of
`{loc, msg, type}` objects. Rendering that array as a React child throws
"Objects are not valid as a React child" — and because a toast renders **above** the
router's page-level error boundary, that throw once unmounted the entire SPA to a
blank `#root` (the user lost all form input). Two layers prevent it now; keep them in
mind when adding forms:

1. **The Axios interceptor normalizes it for you.** `services/api.ts` calls
   `normalizeAxiosErrorDetail` (`utils/apiError.ts`) on every rejection, so by the time
   a `.catch` runs, `error.response.data.detail` is a **string** (the 422 items joined
   as `field: message; …`). The raw items are preserved under `response.data.detailItems`
   if you need field-level errors. Structured 409 refusals (OBJECT `detail` with a
   `code`, e.g. Process Sheets — see `utils/processSheetErrors.ts`) and plain-string
   details are left untouched. **You can safely `showToast('error', err.response.data.detail)`.**
   Never render a raw `detail` you fetched with bare `axios`/`fetch` (bypassing the
   interceptor) — run it through `toDisplayString()` first.

2. **A global error boundary is the backstop.** `App.tsx` wraps the whole tree —
   *outside every provider, including `ToastProvider`* — in `<ErrorBoundary level="global">`.
   Any render error that still slips through shows a friendly full-page reload screen
   instead of a blank page. Do not mount new providers outside this boundary.

Regression coverage: `utils/apiError.test.ts`, `components/ui/Toast.test.tsx`
(a raw 422 array in a toast renders a readable string and does not unmount), and
`components/ErrorBoundary/ErrorBoundary.test.tsx`.

## Database Migrations

> Postgres only. On local SQLite the schema comes from `create_all` (see "Create
> Schema" above); the migrations are Postgres-targeted and are not run on SQLite.

### Bootstrap order (new Postgres database)

A bare `alembic upgrade head` against an **empty** database is **not** the
supported path and will fail: the core tables are created by
`Base.metadata.create_all()` on first app boot (`app/main.py`), not by an
initial migration — `001` only adds indexes — and `002_add_laser_press_brake_types.py`
runs `ALTER TYPE workcentertype ...`, which errors if the enum type doesn't exist yet.

The supported bootstrap is:

```bash
# 1. Create the schema (first app boot, or explicitly):
python -m scripts.seed_data            # calls create_all (+ seeds demo data)

# 2. Mark the DB as already at the migration baseline:
alembic stamp 058_process_sheets       # NOT head — see the stamped-over-DDL warning below

# 3. Apply migrations newer than the baseline going forward:
alembic upgrade head
```

After bootstrap, normal incremental `alembic upgrade head` is the standard path.

> **`create_all` reproduces only what the models declare — everything else in a stamped-past
> migration is lost silently.** `Base.metadata.create_all()` emits exactly what SQLAlchemy
> metadata knows: the tables, columns, indexes, and constraints **declared on the models**.
> Anything a migration creates with no model mirror — raw-DDL trigger functions and RLS
> statements, but equally ordinary `op.create_index` / `op.create_check_constraint` /
> `op.create_foreign_key` calls — is **silently skipped** when the baseline is stamped past it.
> Prod has been bitten three times, all the same cause: the `008` audit-immutability triggers
> (found 2026-07-07; see `docs/SUPABASE_SECURITY.md`), ~42 read-path indexes that lived only in
> migrations `001`/`003`/`020`/`026`/`027` (found 2026-07-31 via a read-only prod `pg_indexes`
> audit; restored by `079_restore_stamped_over_idx`), and — same audit, same day, against
> `pg_constraint` — `003`'s named lineage foreign keys and value-range CHECK constraints
> (restored by `080_restore_stamped_over_con`). Both `079` and `080` also mirror every restored
> object into the owning model (`__table_args__` / `ForeignKey(...)`) so `create_all` reproduces
> them and a future stamp can never skip them again.
>
> On a fresh Supabase Postgres: after `create_all`, stamp at **`058_process_sheets`** so
> `alembic upgrade head` really runs `059`+ — `059` (dynamic RLS enablement + privilege
> revocations) and `060` (recreates `008`'s trigger functions *and* triggers) restore the raw
> DDL `create_all` cannot produce, and `079`/`080` no-op where `create_all` already built the
> mirrored indexes and constraints. All are idempotent on a `create_all` schema.
>
> **What such a bootstrap still lacks** (swept 2026-07-31: migration-only DDL at or below the
> stamp point with no model mirror — equally absent from any DB bootstrapped this way. The index
> drift AND `003`'s constraint drift were both audit-verified against prod — **zero** of `003`'s
> `chk_*` CHECKs and **zero** of its 22 named `fk_*` foreign keys existed there, though 8 of those
> columns did carry an equivalent *auto-named* FK, because their model column already declared a
> plain `ForeignKey()` that `create_all` emitted — and **both are now closed**, by `079` for the
> indexes and `080` for the constraints. Prod *does* have `039`'s `uq_open_time_entry` — its
> bootstrap predates `039`, which then ran incrementally; the losses below apply in full only to a
> fresh bootstrap):
>
> - ~~`003`'s 22 lineage foreign keys~~ — **closed by `080_restore_stamped_over_con`**, which adds
>   all 22 (`ON DELETE SET NULL`, no `ON UPDATE` — `003`'s `safe_create_fk` semantics) and mirrors
>   14 of them onto the model column as `ForeignKey(..., ondelete="SET NULL", name=<003's name>)`.
>   The other 8 are the equivalence bucket above: they stay **unnamed** on the model, and the
>   migration's `_equivalent_fk` probe skips `003`'s named twin rather than double-constraining the
>   column. `work_orders.current_operation_id` closes an FK cycle, so it is pinned `use_alter=True`
>   and gives `work_orders` ⇄ `work_order_operations` a **second FK path** — an onclause-less
>   `join(WorkOrder)` between them now raises `AmbiguousForeignKeysError` at query-compile time
>   (HTTP 500). Seven such call sites were pinned when `080` landed; a static sweep in
>   `tests/test_migration_080_restore_stamped_over_constraints.py` keeps new ones out.
> - ~~`003`'s 23 CHECK constraints~~ — **19 closed by `080`** (each mirrored into the owning model's
>   `__table_args__` with byte-identical predicate text). **Four are deliberately NOT restored** —
>   read the next block before "completing" the set.
> - `003`'s server-side `CURRENT_TIMESTAMP` column defaults (minor: the ORM supplies values).
> - `004`'s four `ix_<table>_version` indexes (negligible: optimistic-lock UPDATEs probe the PK).
> - `023`'s four QMS indexes (`ix_qms_standards_name`, `ix_qms_clauses_standard_id`,
>   `ix_qms_clauses_clause_number`, `ix_qms_clause_evidence_clause_id` — no `index=True` on the
>   model columns).
> - `039`'s `uq_open_time_entry` partial unique index — the DB-level one-open-clock-in guard that
>   `app/services/operation_action_gates.py` treats as authoritative; only the app-level guard
>   survives this bootstrap path.
>
> Everything else at or below `058` is covered: enum `ALTER TYPE` migrations (`create_all` builds
> each enum at its current model shape), data heals/backfills (vacuous on an empty DB), and the
> remaining index/constraint DDL (`006`, `016`, `022`, `026`/`027` tenant uniques + per-table
> `company_id`, `030`–`037`, `041`/`076`, `042`, `044`–`058`) is verified model-mirrored. The rule going
> forward: DDL the models *can* express must be declared on the model in the same PR as the
> migration (the `042`/`078`/`079`/`080` lock-step convention) — and never stamp past DDL they cannot.

> **The four `003` constraints `080` deliberately did NOT restore — do not "complete the set".**
> Three of them are load-bearing *absences*: shipped code writes exactly the rows they would
> refuse, so adding one converts a normal operation into an `IntegrityError`. Migration `080`'s
> header carries the full argument, and
> `backend/tests/test_migration_080_restore_stamped_over_constraints.py` →
> `test_the_four_exclusions_stay_excluded` fails if either the migration literals or a model grows
> one of them (a companion test inserts the rows on SQLite to prove they stay writable).
>
> | Not restored | Why it stays out |
> |---|---|
> | `chk_inventory_items_quantity_non_negative` (`quantity_on_hand >= 0`) | **Excluded by design, not drift.** The material-consumption shortage posture (CLAUDE.md invariant 6, `docs/MATERIAL_CONSUMPTION_PLAN.md`) deliberately lets a **short completion drive a lot negative** — record-and-alert, never a rolled-back write. This CHECK would make short completions fail at the DB. The 2026-07-31 `pg_constraint` audit confirms it is **not** live in prod, and `080` makes that a decided, test-enforced choice rather than an open question — reversing it is a product decision. (`chk_inventory_items_allocated_non_negative` on `quantity_allocated` **is** restored: that column is written exactly once, as 0 at item creation, and no code path decrements it.) |
> | `chk_po_receipts_quantity_received_positive` (`quantity_received > 0`) | **Shipped code legitimately writes 0.** `receiving.py::void_receipt` (PR #149) reconciles a voided receipt down to `quantity_received = 0` on the soft-deleted row — 0 is reserved for void, and the correction path is separately bounded `gt=0`. Restoring `003`'s predicate would make every receipt void fail. A future version must be void-aware (e.g. `quantity_received > 0 OR is_deleted`), as its own reviewed migration. The `quantity_accepted` / `quantity_rejected` siblings are unaffected by void and **are** restored. |
> | `chk_bom_items_quantity_positive` (`quantity > 0`) | **A non-positive BOM quantity is a designed, representable state.** The backflush readiness/refusal layer carries blocking `zero_bom_quantity` / `negative_bom_quantity` diagnostics whose whole job is to surface such a line, refuse the backflush opt-in, and have a human correct it — the same surface-don't-make-unrepresentable posture as the BOM-line UoM mismatches. The DB CHECK would make the bulk import fail on exactly the rows those diagnostics exist to catch, and turn the machinery into dead code. The real gate is the API bound (`schemas/bom.py`, `quantity` `gt=0`). (`chk_bom_items_scrap_factor_range` **is** restored — nothing writes `scrap_factor` outside `[0, 1]`.) |
> | `chk_work_centers_efficiency_range` | **Obsolete — the column never existed.** `work_centers` has carried `efficiency_factor` (1.0-scale) since the initial commit; a bare `efficiency` column appears nowhere, and `003`'s own `required_columns=['efficiency']` guard skipped this CHECK even on databases that really ran `003`. Not re-derived onto `efficiency_factor` either: the 2026-07-31 pre-flight could not probe that column's bounds and the Pydantic schema puts none on it, so out-of-range prod data cannot be ruled out. Re-derive only after a prod probe. |

> **Keep new revision ids ≤ 32 characters.** On the `create_all → stamp → upgrade` bootstrap path
> the `alembic_version.version_num` column is `varchar(32)`. Migration `014b_widen_alembic_version`
> widens it (to `varchar(128)`), but `014b` is *stamped over*, not *run*, when the stamped baseline
> is newer than it — so on a freshly bootstrapped DB the column stays `varchar(32)` and a revision id
> longer than 32 chars fails to record (`value too long for type character varying(32)`). Until the
> baseline is at or past `014b` on every target, keep revision ids to ≤ 32 chars (e.g.
> `038_optimistic_lock_backfill`, `039_uq_open_time_entry`).

### Create a new migration
```bash
cd backend
alembic revision --autogenerate -m "Description of changes"
```

> `--autogenerate` only sees tables that are registered on `Base.metadata`, which
> requires every model module to be imported in `app/models/__init__.py`
> (`alembic/env.py` does `from app.models import *`). When you add a new model
> file, wire it into `app/models/__init__.py` — otherwise autogenerate will miss
> its tables, or crash with `NoReferencedTableError` if another model references them.

### Apply migrations
```bash
alembic upgrade head
```

### Rollback migration
```bash
alembic downgrade -1
```

### View migration history
```bash
alembic history
```

### Concurrency-safety migrations (Batch 2 — completion-path hardening)

Two migrations back the work-order-completion concurrency fixes (see
`docs/WORK_ORDER_COMPLETION_REMEDIATION.md`, Rank 5 / Batch 2):

- **`038_optimistic_lock_backfill`** — makes the `version` column on `work_order_operations` and
  `time_entries` safe for the now-mapped `version_id_col` optimistic locking. It backfills
  `version = 1 WHERE version IS NULL` (no data destroyed) and re-asserts `NOT NULL` +
  `server_default '1'`. The column itself is owned by `004_add_optimistic_locking`; this migration
  only normalizes data, so its `downgrade` is a deliberate no-op (it does not drop the column).
  Idempotent and safe to re-run. Plain transactional DDL/DML — intentionally split from `039` so it
  can run inside a transaction. `069_work_order_version_guard` (2026-07-21) later applies the
  identical guard to `work_orders`, backing the `version_id_col` mapping on `WorkOrder` itself
  (header-level `PUT /work-orders/{id}` optimistic locking) — run it with or before that code deploy.

- **`039_uq_open_time_entry`** — adds the partial unique index
  `uq_open_time_entry ON time_entries (user_id, operation_id) WHERE clock_out IS NULL` (at most one
  open clock-in per user + operation). Before building the index it runs a **non-destructive
  pre-flight dedupe**: within each `(user_id, operation_id)` group of open rows it keeps the most
  recent (`clock_in DESC, id DESC`) and **closes** the older ones by setting `clock_out = clock_in`
  and `duration_hours = 0`. `quantity_produced` and the rows themselves are **preserved** (only the
  duplicated *time* is zeroed; the parts were really made). The closed-row ids are printed to the
  migration/deploy output (timestamped by the deploy) for AS9100D labor traceability — deliberately
  **not** written to the tamper-evident `audit_log`. The index is built with
  `CREATE INDEX CONCURRENTLY` inside an autocommit block (so it can't run in a transaction — hence
  the split from `038`), and the `downgrade` drops it `CONCURRENTLY` too. Idempotent
  (`IF NOT EXISTS` / inspector guard); Postgres-only (skipped on SQLite, where the app-level guard
  still applies).

### Completion-inventory migrations (Batch 6 — FG receipt + backflush)

Two migrations back the work-order-completion inventory side-effects (see
`docs/WORK_ORDER_COMPLETION_REMEDIATION.md`, Rank 9 / Batch 6), plus a later dialect-parity fix to the
second of them:

- **`040_add_part_backflush_flag`** — adds the opt-in flag the backflush logic keys off:
  `parts.backflush_components BOOLEAN NOT NULL DEFAULT false`. The `server_default='false'` backfills
  every existing row in the same `ALTER` (a metadata-only column add on PostgreSQL 11+ — brief
  `ACCESS EXCLUSIVE` lock, no table rewrite, no backfill pass). The model (`app/models/part.py`)
  declares the identical `server_default`, so the `create_all` bootstrap path produces the same column
  definition. Idempotent (inspector column-existence guard) and reversible (guarded `drop_column`).

- **`041_uq_wo_inventory_idempotency`** — DB-enforces "one finished-goods receipt and one
  backflush issue per work order" with **two partial UNIQUE indexes** on `inventory_transactions`:
  `uq_wo_inventory_receipt` on `(company_id, reference_type, reference_id, transaction_type)`
  `WHERE reference_type='work_order' AND transaction_type='RECEIVE'`, and `uq_wo_inventory_issue` on
  `(company_id, reference_type, reference_id, transaction_type, part_id)`
  `WHERE reference_type='work_order' AND transaction_type='ISSUE'`. The partial predicate scopes the
  constraint to work-order-referenced rows only, so PO/SO receipts, manual adjustments, transfers,
  scrap, ships and counts are unaffected. The predicate uses the **UPPERCASE** enum labels
  (`'RECEIVE'`/`'ISSUE'`) because the native Postgres `transactiontype` enum stores the member name —
  the model's `__table_args__` declares the identical indexes and the two must stay in lock-step.
  Both indexes are built with `CREATE UNIQUE INDEX CONCURRENTLY` inside an autocommit block (so they
  can't run in a transaction) to avoid blocking writes on the high-write `inventory_transactions`
  table; the `downgrade` drops them `CONCURRENTLY` too. **Pre-flight duplicate guard fails loudly:**
  inventory transactions are regulated traceability records, so if pre-existing duplicate work-order
  RECEIVE/ISSUE groups exist, the migration **lists the offending `(company_id, reference_id[,
  part_id])` groups and raises** rather than deleting any rows — an operator resolves them deliberately
  (keep the earliest min-id row), then re-runs. Idempotent (`IF NOT EXISTS` / inspector + `pg_indexes`
  guard) and reversible; the migration body itself is Postgres-only (skipped on SQLite — partial
  indexes + `CONCURRENTLY` were treated as Postgres features), with `create_all` emitting the indexes
  from the model on SQLite and the app-level guard applying either way.

  > **What `uq_wo_inventory_issue` guards TODAY (PR 4.4, 2026-07-27) — read this before assuming the
  > component backflush is index-protected.** Both indexes are **unchanged** and stay exactly as
  > defined here; PR 4.4 ships **no migration at all** (head stays `076_uq_wo_inv_sqlite_parity`) and
  > the lockstep tests in `tests/test_migration_075_*.py` / `tests/test_migration_076_*.py` pass
  > **unmodified**. What changed is which rows arrive under the predicate. Reconciled component
  > consumption now posts `reference_type='work_order_backflush'`, deliberately **outside** it, because
  > reconcile-to-target needs N rows per (work order, part) plus a later top-up row. So
  > `uq_wo_inventory_receipt` still guards the live finished-goods `RECEIVE`, while
  > `uq_wo_inventory_issue` now covers **only legacy (pre-4.4) component ISSUE rows** — no new row can
  > ever trip it, and it is retained as a permanent fence over that history rather than as a live
  > guard. The current leg's non-duplication is **arithmetic** (`delta = target − signed ledger net`,
  > serialized by the pre-existing `SELECT … FOR UPDATE` on `work_orders`), not schema-enforced. Adding
  > a `reference_type` value needed no migration: the column is unconstrained `String(50)` free text
  > with no CHECK, enum or domain — the same way PR 1 introduced `work_order_operation`. See
  > `docs/MATERIAL_CONSUMPTION_PLAN.md` → "The reconciling backflush and one lot policy (PR 4.4)".

- **`076_uq_wo_inv_sqlite_parity`** (file `076_uq_wo_inventory_sqlite_parity.py`) — **dialect-parity
  fix for the two `041` indexes.** They declared only `postgresql_where`, which SQLite ignores, so on
  SQLite — local dev and the entire pytest suite — they degraded into **full** unique indexes covering
  *every* `reference_type`. The test environment therefore enforced a constraint production does not:
  legitimate repeated movements under one reference (a second compensating `ADJUST` on one
  `po_receipt`, several `SHIP` rows for one shipment, the many-rows-per-operation material-consumption
  path) were rejected locally though Postgres accepts them. The model now declares `sqlite_where`
  alongside `postgresql_where` **from the same predicate constant** — correct on both dialects because
  SQLAlchemy binds the uppercase enum *member name* (`'RECEIVE'`/`'ISSUE'`) under Postgres **and**
  SQLite (verified at the bind-processor level, not assumed) — so `create_all` now builds them
  partial, and the migration rebuilds them partial on existing SQLite DBs. **On Postgres it is a
  deliberate no-op** (guarded on dialect, emits zero DDL): those indexes are already correct, and
  rebuilding a unique index on the hottest ledger table would take a real lock and briefly drop the
  live double-receive guard for no behavioral gain. Coverage for the rows the guards exist for
  (`reference_type='work_order'` RECEIVE/ISSUE) is **unchanged** on both dialects. Idempotent
  (reflects `dialect_options['sqlite_where']` and rebuilds only a genuinely-degraded index) and
  reversible — the `downgrade` really restores the full-index shape, and because that shape is
  *stricter* it pre-checks and **raises with the offending groups itemized rather than deleting any
  ledger row** (the `041` posture).

### Completion-path performance indexes (Batch 9 — reconcile/predecessor read paths)

One migration backs the work-order-completion read-path speedups (see
`docs/WORK_ORDER_COMPLETION_REMEDIATION.md`, Rank 12 / Batch 9):

- **`042_wo_completion_perf_indexes`** — adds **two non-unique btree indexes** that back the hot
  completion / reconcile query shapes (previously sequential scans on high-row tables):
  `ix_time_entries_operation_clock_out` on `time_entries(operation_id, clock_out)` (backs
  `reconcile_work_orders_from_completion_evidence`'s per-operation production/scrap rollups, the
  `clock_out IS NOT NULL` closed-only rollup, and the `ORDER BY operation_id, clock_out DESC`
  latest-entry scan), and `ix_woo_work_order_sequence` on `work_order_operations(work_order_id,
  sequence)` (backs `has_incomplete_predecessors` and `release_next_ready_operation`). Unlike `041`'s
  partial UNIQUE indexes these enforce **no invariant** — they are pure read-path speedups, so there is
  **no pre-flight duplicate guard** (nothing to validate; the build cannot fail on existing data). Both
  are built with `CREATE INDEX CONCURRENTLY` inside an autocommit block (so they can't run in a
  transaction) to avoid the `ACCESS EXCLUSIVE` lock a plain `CREATE INDEX` would take on the high-write
  `time_entries` / `work_order_operations` tables; the `downgrade` drops them `CONCURRENTLY` too. The
  models (`TimeEntry.__table_args__` in `app/models/time_entry.py`, `WorkOrderOperation.__table_args__`
  in `app/models/work_order.py`) declare the identical indexes so the `create_all` bootstrap path
  produces them byte-for-byte — keep the migration and the model declarations in lock-step. Idempotent
  (`_index_exists` / `if_not_exists`) and reversible; Postgres-only (skipped on SQLite, where
  `create_all` already emits both indexes from the model declarations).

## Work-order completion rollup (shared finalizer)

Completion is consolidated in **one** place: `finalize_operation_completion(db, wo, op)` in
`app/services/work_order_state_service.py` (Rank 6 / Batch 3 — see
`docs/WORK_ORDER_COMPLETION_REMEDIATION.md`). Every completion path **delegates** to it rather than
re-implementing the op → work-order rollup:

- both `/operations/{id}/complete` endpoints (office `work_orders.py` and shop-floor `shop_floor.py`),
- the additive verbs (`/shop-floor/clock-out/{id}`, `/shop-floor/operations/{id}/production`),
- the privileged `/work-orders/{id}/complete` override (it force-completes each still-open operation
  through the finalizer instead of blind-flipping the work order to COMPLETE).

The finalizer owns **only** the state transition — remaining-ops decision (reusing the loaded
`work_order.operations` relationship), the COMPLETE-vs-`RELEASED`→`IN_PROGRESS` branch, the
`max()`-guarded finished-quantity sync (floored at durable `TimeEntry` evidence, capped at target;
on a laser dispatch-pool WO the synced value is the pooled **sum** of per-nest progress via
`pooled_quantity_complete`, never snapped to `quantity_ordered` —
see `docs/WORK_ORDER_COMPLETION_REMEDIATION.md` → Rank 6 → Update 2026-07-22),
the `actual_start`/`actual_end` stamping (clamped so `actual_start ≤ actual_end`), the self-healing
next-`READY` release, and maintaining `current_operation_id` — and returns the set of affected
`work_center_id`s. The **caller** keeps auth, tenant lookup, row locks, audit, scheduling refresh and
broadcasts. The finalizer does not commit and does not flush the audit chain. When adding a new
completion entry point, call `finalize_operation_completion` rather than duplicating the rollup.

## API Documentation

Once the backend is running, access the interactive API documentation:
- Swagger UI: http://localhost:8000/api/docs
- ReDoc: http://localhost:8000/api/redoc
- OpenAPI JSON: http://localhost:8000/api/openapi.json

## Adding New Features

### Backend Feature

1. **Create/update model** in `app/models/`
2. **Create schema** in `app/schemas/`
3. **Implement service** in `app/services/` (business logic)
4. **Create API endpoint** in `app/api/endpoints/`
5. **Add tests** in `tests/`
6. **Create migration** if database changes needed

### Frontend Feature

1. **Update types** in `src/types/`
2. **Create/update service** in `src/services/`
3. **Create component** in `src/components/`
4. **Add route** in `src/App.tsx`
5. **Write tests** alongside component files

## Timezone handling

The platform rule is **store UTC, serve UTC (`Z`), display Central**:

- **Store UTC.** Persist naive/aware UTC datetimes as usual — nothing about storage changes.
- **Serve UTC with `Z`.** API responses serialize `datetime` fields as UTC ISO-8601 with a trailing
  `Z` (e.g. `2026-07-01T19:17:00Z`). New **response** schemas inherit `UTCModel`
  (`app/schemas/base.py`) rather than bare `BaseModel`; hand-built response dicts run datetime values
  through `app.core.time_utils.to_utc_iso(...)`. `date`-only fields stay `YYYY-MM-DD` (unaffected).
- **Display Central.** The frontend renders every timestamp in shop-local Central time
  (America/Chicago) via `frontend/src/utils/centralTime.ts` (`formatCentralDateTime` /
  `formatCentralDate` / `formatCentralTime`; `toDate` to parse — it treats zone-less strings as UTC;
  `getCentralTodayISODate` / `getCentralDateStamp` for date-only form defaults). Never hand-roll
  `new Date(x).toLocaleString()` for display — it renders in the viewer's timezone and mis-parses
  no-`Z` strings.

## Debugging

### Backend Debugging

You can run the backend with detailed logging:
```bash
export LOG_LEVEL=DEBUG
uvicorn app.main:app --reload --port 8000
```

### Frontend Debugging

- Use React DevTools extension
- Check browser console for errors
- Use VS Code debugger with configuration

## Performance Optimization

### Backend
- Use Redis caching for frequently accessed data
- Optimize database queries with proper indexes
- Use pagination for large datasets
- Implement async operations where possible

### Frontend
- Use React.memo for expensive components
- Implement code splitting and lazy loading
- Optimize bundle size
- Use virtual scrolling for long lists

## Common Issues

### Backend won't start
- Verify DATABASE_URL in .env (local default is SQLite; if using Postgres, confirm the server is running)
- For local SQLite, make sure the schema exists — run `python -m scripts.seed_data` (do not run `alembic upgrade head` against SQLite)
- Check port 8000 is not in use

### Frontend build errors
- Clear node_modules: `rm -rf node_modules package-lock.json && npm install`
- Check TypeScript errors with `npm run type-check`

### Docker issues
- Stop all containers: `docker-compose down`
- Remove volumes: `docker-compose down -v`
- Rebuild: `docker-compose build --no-cache`

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD:
- Runs on every push and pull request
- Executes tests, linting, and type checking
- Builds Docker images
- Runs security scans
- Deploys after successful runs

The Playwright E2E suite runs in a separate, **deliberately non-blocking** workflow
(`.github/workflows/e2e.yml`): on PRs to `main`/`develop` that touch `backend/` or
`frontend/`, nightly at 09:00 UTC, and on manual dispatch. It boots the full stack in the
runner (Postgres 15 service → `python -m scripts.seed_data` → uvicorn with
`ENVIRONMENT=test` and `RATE_LIMIT_ENABLED=false` → Vite dev server) and runs
`npx playwright test` with the `E2E_*` vars set inline to the throwaway dev-seed users —
no repo secrets. It is not a required status check until its flake behavior in CI is
known; the promotion path is adding the "Playwright E2E" job to branch protection. The
HTML report is always uploaded as the `playwright-report` artifact (14-day retention);
backend/vite logs upload on failure. Details in `docs/CI_CD_SETUP.md`.

## Security Considerations

- Never commit `.env` files
- Use strong, random SECRET_KEY in production
- Enable rate limiting
- Keep dependencies updated
- Run security audits regularly
- Use HTTPS in production

## Contributing

1. Create a feature branch from `main` or `develop`
2. Make changes with proper tests
3. Ensure all tests pass
4. Run code quality checks
5. Submit a pull request with clear description
6. Address review feedback

## Additional Resources

- FastAPI Documentation: https://fastapi.tiangolo.com/
- React Documentation: https://react.dev/
- SQLAlchemy Documentation: https://docs.sqlalchemy.org/
- Tailwind CSS: https://tailwindcss.com/docs
