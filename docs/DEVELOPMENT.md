# Development Guide

This guide covers development practices, testing, and contribution guidelines for Werco ERP.

## Environment Setup

### Prerequisites
- Python 3.11 (supported production and CI runtime)
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
   python -m pip install --require-hashes -r requirements-dev.lock
   ```

3. **Frontend Setup**
   ```bash
   cd frontend
   npm ci
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

### Dependency updates

`backend/requirements.txt` and `requirements-dev.txt` are editable dependency inputs.
Development includes the runtime input; install the committed **hash locks** rather
than resolving those inputs during routine setup:

```bash
cd backend
python -m pip install --require-hashes -r requirements-dev.lock  # development / CI
# Runtime-only environments use: python -m pip install --require-hashes -r requirements.lock

# uv is included in the development lock. Bootstrap the compiler if needed:
python -m pip install uv==0.13.6
python scripts/lock_dependencies.py
python scripts/lock_dependencies.py --check
# For a targeted transitive dependency upgrade:
python scripts/lock_dependencies.py --upgrade-package PACKAGE_NAME
```

Edit direct requirements in the `.txt` inputs first, regenerate both locks, and
review the resulting version/hash changes. The helper requires exactly uv 0.13.6,
prefers the compiler beside the active Python interpreter, and retains existing
locked versions unless the inputs or `--upgrade-package` require a change. It resolves
both outputs in a temporary directory before publishing either; `--check` fails on
drift without changing files and may require registry access. Development resolution is
constrained to the runtime lock. Universal resolution includes OS/architecture markers
for Python >=3.11; it does not expand the supported production/CI runtime beyond 3.11.

CI installs `requirements-dev.lock` and checks regeneration. Runtime containers and
the E2E API install `requirements.lock`, all with `--require-hashes`. Do not delete
lockfiles to fix an environment: reinstall from them. Frontend and landing dependencies
likewise use their separate `package-lock.json` files and `npm ci`.

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
black --config=.black app tests
isort --settings-path=.isort.cfg app tests

# Lint code
flake8 app --max-line-length=120

# Type checking
mypy app --config-file=mypy.ini

# Run security checks
bandit -r app -s B101 -ll

# Run tests
pytest tests/ -v
pytest tests/ --cov=app --cov-report=html
```

MyPy's legacy global suppressions remain, but `app.schemas.*`,
`app.db.tenant_filter`, and `app.services.pdf_text` enforce `arg-type`,
`return-value`, `union-attr`, `call-arg`, and `attr-defined`. Expand this boundary
ratchet after reviewing each module; do not silence new findings in covered modules.

**Frontend**
```bash
cd frontend

# Format code
npm run format

# Verify the real ESLint config, then lint source with CI's warning cap
npm run lint -- --max-warnings=0
npm run lint:fix

# Type checking (application and tests)
npm run type-check

# Run tests
npm test
npm run test:coverage
```

`frontend/eslint.config.js` is the only ESLint configuration; the obsolete CRA
`.eslintrc.json` and package `eslintConfig` are removed. `rules-of-hooks` is an error
throughout `src`. `exhaustive-deps` is an error in `src/components/ui`; the broader
effect-dependency backlog needs individual behavior review before extending that
scope. `npm run test:lint-config` independently exercises the actual configuration
with accepted and rejected Hook examples, and is included in `npm run lint`.

**Landing site**
```bash
cd landing
npm ci
npm run type-check
npm run build
npm run audit:ci
npm run dev
```
Vite serves the existing static `index.html` at **http://localhost:3001**. The optional
React source under `src/` is type-checked separately; it is not mounted by the shipped
HTML entry. See [the landing README](../landing/README.md).

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

#### Why the tests run on SQLite

**Production is Supabase Postgres. The test suite runs on in-memory SQLite, and that is
a deliberate owner decision (2026-08-13), not an accident of configuration.** Write it
down here rather than discover it again: this was ambiguous for a long time, and the
ambiguity was expensive.

`backend/tests/conftest.py` gives every xdist worker its own in-memory SQLite database
(`sqlite:///file:werco_test_<worker>?mode=memory&cache=shared&uri=true`) and sets
`DATABASE_URL` / `TEST_DATABASE_URL` to it **before** `app.main` is imported. Since
`pytest.ini` carries `-n auto` unconditionally, there is always at least one worker, so
the `master` branch of that selection — the only one that reads an inherited
`TEST_DATABASE_URL` — never runs in practice.

Until 2026-08-13 the CI job also provisioned a `postgres:15-alpine` service and exported
a Postgres `DATABASE_URL`, which **no test ever connected to** (`create_engine` is lazy,
so nothing even dialed the socket). It cost ~21s per run and, worse, read as though the
backend were tested against Postgres. Both the service and those env vars are gone from
that job.

**`e2e.yml` still provisions Postgres, and should.** The Playwright suite boots the real
application against a real database, so it is the one place in CI where this system is
exercised on the engine it actually deploys to. Nothing above applies to it.

Two consequences worth knowing before writing a test:

- **SQLite is not Postgres.** Server-side defaults, `JSONB`, arrays, partial-index
  predicates and some `CHECK` semantics differ or are unenforced. Fidelity to production
  is bought deliberately elsewhere: migration tests compile against a `postgresql`
  dialect (see `tests/test_migration_*.py`), and the Alembic migrations themselves run
  against real Postgres. If a behavior you are testing depends on the database engine,
  assert it by dialect-compiling the SQL, not by executing it.
- **Two concurrent pytest runs in `backend/` no longer collide on disk** — the databases
  are per-process and in memory, so there are no `test_gw*.db` files to stomp. (This was
  a genuine hazard when they were files; a stray parallel run produced thousands of bogus
  "table X already exists" errors on a healthy branch.)

The in-memory URI's **shared cache is load-bearing** and must not be simplified to a bare
`sqlite://`. Three things open a second engine against that URL — the app's own
`app/db/database.py` engine, `tests/api/export_audit_helpers.py` (which reads through a
separate engine *precisely* so it sees only committed rows, per the audit invariant), and
the duplicate `tests.conftest` copy imported by two completion tests. Under a bare
in-memory URL each would get its own empty database.

`tests/conftest.py`'s `db_session` fixture is the **single owner of the schema**: it runs
`create_all`/`drop_all` around every test. `app/main.py`'s lifespan deliberately skips its
own `create_all` when `ENVIRONMENT=test` so there is never a second connection issuing
schema DDL concurrently.

#### Tenant-scope guards (`tests/test_route_tenant_scope_guard.py`)

CLAUDE.md invariant 1 (every tenant query scoped to the active company from
`get_current_company_id`) is held structurally for new code by two guards in this file. They
are pure introspection (no database, no requests), carry the `unit` marker, and run in CI's
Backend Tests job with the rest of the suite.

- **Route guard.** Every HTTP route under `/api/v1` whose dependency tree authenticates must
  also reach `deps.get_current_company_id`. "Authenticates" means the tree reaches
  `deps.oauth2_scheme`: `get_current_user`, `require_role`, `get_audit_service`,
  `require_platform_admin` and the four station/display resolvers all do. The scope dependency
  can sit anywhere in the tree, so a router-level or wrapping dependency counts. A route that
  doesn't reach it must be named in one of two lists:
  - `GLOBAL_ROUTES`, with a reason. It is only for routes that really don't read tenant data
    by the active company: self-only routes, static catalogs, `require_platform_admin` routes,
    and the station/display principal routes, whose company comes from the principal's own row.
  - `TRACKED_DEBT`, for known offenders. Each entry names the branch that fixes it, or has
    `fixed_by=None` for an unowned follow-up whose reason names the fix (today: the dead
    write branch of `POST /admin/settings/seed-database`).

  The same file also fails when:
  - a `GLOBAL_ROUTES` reason cites a resolver or `require_platform_admin` that the route no
    longer reaches;
  - a `/api/v1/platform/` route doesn't depend on `require_platform_admin`;
  - `deps.py` gains a bearer-reading dependency beyond the five known doors;
  - something under the prefix is a `Mount` or plain `Route` that the guard can't see into.

  A `(METHOD, path)` registered twice is judged by its worse twin.
- **Constructor guard.** Code under `backend/app/` may construct a tenant model only if the
  call passes `company_id=` (or a `**{...}` dict literal with a `"company_id"` key), or binds
  the row to a name and assigns `<name>.company_id` in the same function. A `**` splat of any
  other mapping (`**parsed`, `**payload.model_dump()`) counts for nothing: the guard can't see
  what it holds, and that is the shape that hid `CustomFieldValue(..., **parsed)`. Tenant
  models are `TenantMixin` subclasses plus mapped classes that declare their own NOT NULL
  `company_id`, such as `User`. Any other construction must be named in
  `UNGUARDED_CONSTRUCTIONS`, which excuses exactly `occurrences` calls per (file, function,
  model) key.
- **Stale entries fail too.** The guard fails on an entry whose route now reaches
  `get_current_company_id`, is gone, or is no longer authenticated. It also fails on a
  construction that now sets `company_id`, is gone, or occurs fewer times. The failure message
  says to delete the entry, so a PR that fixes a listed offender removes its entry in the same
  PR. Don't add a new route to `TRACKED_DEBT` just to make CI green. Add
  `company_id: int = Depends(get_current_company_id)` and scope the route's queries with it.

What the guards do **not** prove:
- The route guard checks that the dependency is present, not that it is used. A route that
  takes `get_current_company_id` and then runs an unfiltered query still passes. Per-router
  tests (`tests/api/*tenant_isolation*`) cover use.
- WebSocket routes authenticate inside the handler through `services/access_identity.py` and
  are out of scope.
- Unauthenticated routes (`PUBLIC_OPERATIONS` in `tests/test_mcp_coverage.py`) are out of
  scope by construction. A public route that derives a company from its own credential (a
  carrier HMAC webhook, a station login, a display-token claim) must scope by that credential,
  and is reviewed by hand.
- `company_id=` is checked for presence, not provenance. A value taken from request input
  (`company_id=payload.company_id`) or `company_id=None` still passes.
- The constructor guard can't see a model reached dynamically (`cls(...)`, a class held in a
  variable). It also can't see `company_id` set through another object or a later helper. The
  `company=` relationship form isn't recognized either; it fails loudly, so write
  `company_id=company.id`.

The lists were seeded from the 2026-11-05 audit: 25 unscoped routes (complaint/RMA by-id
verbs and the complaints dashboard, custom-field value routes, calibration history, the tool
crib) and seven live constructor calls that omit `company_id` (five verbs plus both
custom-field value writes). The 2026-11 security/integrity PRs fixed every one of them, so the
only entries left are the `seed-database` pair, a dead write branch filed as an unowned
follow-up. The file is the current list, not this paragraph.

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

**Test files are type-checked (since 2026-08-03)**

`npm run type-check` runs two `tsc --noEmit` programs: `tsconfig.json` (application
source, which *excludes* tests) and `tsconfig.test.json` (the `*.test.*` / `*.spec.*`
files, `src/test-utils`, `src/setupTests.ts`). The retired quote-nesting Node worker is no longer built.
CI runs the same npm script.

This matters because the test runner does **not** type-check: `jest.config.js` hands
ts-jest an inline tsconfig inheriting `"isolatedModules": true`, so ts-jest is
transpile-only. Before this second program existed, 229 test files were checked by
neither gate — the mechanism that let `SPC.tsx` stay green while being non-functional
in production, because its test mocked the API client as resolving `{ data: ... }`,
the shape the page wrongly expected rather than what the real client returns.

What it forbids:

- **Wrong-shaped mocks.** `mockResolvedValue(...)` must match the real function's
  return type. Mocking an envelope the client never returns is a compile error.
- **Fixtures that don't satisfy their annotation.** A `const c: SPCCharacteristic = {...}`
  missing required fields, or carrying fields the type doesn't declare, fails.

Fix the mock or fixture to match the real contract. Do **not** silence it with `as any`
or `@ts-expect-error` — that reopens the exact hole. (~223 pre-existing `as any` casts,
173 of them on `mockResolvedValue`/`mockReturnValue` lines, still defeat the gate at
those call sites; they are known debt, not a pattern to copy.)

Do not "fix" this by setting `isolatedModules: false` in `jest.config.js`: that makes
ts-jest type-check per file at runtime, measured at 30 unrelated suites failing to
compile and the test run going 30s → 430s. The separate whole-program pass costs ~16s
and does not touch the test runtime.

#### Dashboard edit mode (drag and drop) in jsdom

jsdom has no layout engine, no Web Animations API, no `inert` enforcement and no real blur when
React moves a focused node, so the dashboard editor (`components/dashboard/editor/`,
`WidgetCell.tsx`; behavior in `docs/DASHBOARD.md` → Customizing the dashboard) is tested in
layers, each with its own helper in `frontend/src/test-utils/`:

- **Placement rules are pure.** `editor/dragPlanning.test.ts` feeds the planner rects from
  **`gridFlow.ts`**, a test-only simulator of the two dashboard grids (each width's span per
  breakpoint, sparse row-major placement, no dense packing) that produces real-shaped rects for any
  order, spans and heights; `gridFlow.test.ts` holds its span table to `PANEL_SIZE_CLASSES`. A rule
  about where a dragged widget lands is tested here, not through the DOM.
- **Editor behavior without the page** — the edit bar, the Move menu, the gallery, and
  `useLayoutEditor`'s focus, announcements and server verbs — runs on **`layoutEditorHarness.tsx`**:
  `createFakeLayout()` stands in for `useDashboardLayout` (its `save` / `reset` / `reload` are
  `jest.fn`s that behave like the server by default; `set()`, inside `act`, changes what it shows)
  and `<EditorHarness>` renders a miniature dashboard of real `WidgetCell`s with stub widgets, wired
  to the real hook.
- **Page and component suites never load dnd-kit.** A suite that enters edit mode on the page mocks
  the lazy loader with **`fakeDragLayer.tsx`**:

  ```ts
  jest.mock('../components/dashboard/editor/loadDragLayer', () =>
    jest.requireActual('../test-utils/fakeDragLayer').createFakeLoadDragLayerModule()
  );
  ```

  The fake is typed against the real layer's props (`editor/dragLayerTypes.ts`), renders nothing and
  records its props in `dragHarness`, whose `start` / `reorder` / `end` / `cancel` call the page's
  drag callbacks inside `act` (`start` is a no-op while the layer is disabled, as a disabled dnd-kit
  draggable starts nothing). Call `dragHarness.reset()` in `beforeEach`. The mocked
  `loadDashboardDragLayer` is a `jest.fn`, so `mockRejectedValueOnce` simulates the editor chunk
  failing to load.
- **One suite uses the real layer:** `editor/DashboardDragLayer.test.tsx` drives real dnd-kit
  sensors. A test about the sensors and the layer itself — activation thresholds, the touch
  long-press, the reorder gates, the resize guard, the ghost, the drop animation, click suppression
  — belongs there. Its tools are in **`dndDom.ts`**: `stubRects` (a scoped `getBoundingClientRect`
  stub keyed by `data-widget-cell`, then `data-testid`, changeable mid-test; `restore()` it after
  each test), `mouseDown` / `mouseMove` / `mouseUp` / `mouseDrag` (the press on the activator, then
  moves and the release on `document`, where the MouseSensor listens), `touchStart` / `touchMove` /
  `touchEnd` (real `TouchEvent`s carrying plain-object `touches`, dispatched on the target; pair with
  `jest.useFakeTimers()` for the long-press delay), and `installFakeAnimate` (records every
  `Element.prototype.animate` call, for the FLIP tests).

Use the fake layer whenever the subject is what the page or a component does with a drag; use the
real one only to prove the dnd-kit wiring. What none of these can prove — real grid geometry,
`inert`, touch in a browser — is the e2e spec's job (below). Focus after a keyboard move LATER, the
case where React re-inserts the focused cell, is asserted only in jsdom (`Dashboard.editMode.test.tsx`
→ "keyboard moves on the Move button"); the e2e spec checks focus after a move earlier and after a
pointer drop, not that case.

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

#### Dashboard edit mode spec — run it by hand before merging

`e2e/dashboard-edit-mode.spec.ts` proves what the jsdom suites cannot: the real CSS grid
reflowing under the pointer, with the landing slot exactly where the panel lands (at 1440,
1100 and 390 px wide); the order persisting across a reload; a pointer held still never
reordering; Escape mid-drag; focus on the Move button after a keyboard move earlier and after a
pointer drop; `inert` widget content; no remount across edit mode; axe with the Move menu and the
gallery open; and touch long-press versus swipe. It runs in `e2e.yml`, but that workflow is
**non-blocking**, so a green PR proves none of this. **Run the spec locally before merging any
change to the dashboard editor** (`components/dashboard/editor/`, `WidgetCell.tsx`, the edit-mode parts of
`pages/Dashboard.tsx`). With the backend seeded and started with `RATE_LIMIT_ENABLED=false`
(steps 1–2 above) and the Vite dev server running, from `frontend/`:

```bash
CI=1 E2E_BASE_URL=http://localhost:<frontend port> \
  E2E_ADMIN_EMAIL=admin@werco.com E2E_ADMIN_SECRET=<the seed admin's password> \
  npx playwright test e2e/dashboard-edit-mode.spec.ts
```

`CI=1` turns off the config's own `webServer` (which starts or reuses a dev server on port
5173), so Playwright tests the server at `E2E_BASE_URL`; it also means one worker and up to
two retries per failing test, so treat a test the report marks **flaky** as a failure to
investigate, not a pass. The spec signs in as the admin only, runs its tests serially, and
resets that admin's own saved dashboard layout to the default through the UI before and after
every test — don't point it at a database where that layout matters.

Its helpers encode real-browser pitfalls; keep them when you add a test:

- **Compare boxes in document coordinates** (`documentBox`). A viewport box is valid only at the
  scroll position it was read at, so the slot read before the release and the panel read after it
  are compared in the frame that does not scroll. The drop's own focus does not scroll the page (it
  is requested with `preventScroll`); the helper keeps the comparison valid if anything else
  scrolls between the two reads.
- **Wait for the glides** (`glidesSettled`). Every cell a reorder displaces — the landing slot
  included — runs a 200 ms FLIP on the cell element, and a box read mid-glide is neither where it
  was nor where it is going. Count only animations whose target carries `data-widget-cell`:
  widget content runs infinite CSS animations (status pulses) that never settle.
- **Wait out dnd-kit's click guard** (`dragClickGuardLapsed`). After any drag ends, dnd-kit keeps
  a capture-phase `click` blocker on `document` for 50 ms, so a click sent straight after a drop
  is swallowed.
- **Scan a settled dialog with axe.** The Modal fades and scales in, and axe samples rendered
  colours, so a scan taken mid-animation reports low contrast; the gallery scan first waits until
  the dialog has no running finite animation.
- **Keep the pointer out of the bottom auto-scroll band.** dnd-kit scrolls the page while the
  pointer is within 20% of the viewport's top or bottom, and a page scrolling under a held pointer
  reorders on its own after the slot was measured; the landing-slot and touch tests use a tall
  viewport (1600 px and 1400 px) and keep the pointer path above 80% of its height — they bound the
  bottom band only. A grab inside the top band is safe when the drag then moves only downward:
  dnd-kit 6.3.1 scrolls toward an edge only after the pointer has moved toward that edge during the
  drag (its scroll intent), which is why the touch test's long press, about 210 px from the top,
  does not scroll — provided the page is still. Scroll that is still running when the press lands
  counts as movement toward the top (the quick swipe before it leaves a fling, measured at 7 px)
  and latches upward intent, and the page then auto-scrolls up under the finger. So
  `scrollSettled` waits for scrollY to stay unchanged for 200 ms of animation frames (two
  back-to-back reads can land inside one frame mid-fling), and `panelToTop` settles before it
  measures. The swipe moves the finger down (the page scrolls up), because the page may already
  sit at its maximum scroll.
- **One CDP session per touch gesture** (`touchGesture`). Chrome tracks the active touch per
  session, so a `touchMove` sent from a fresh session is refused ("Must send a TouchStart
  first").

### Test coverage gates

- Backend: **78%** floor from `backend/pytest.ini`; do not override it with a lower CLI value. Use `--no-cov` for focused subsets.
- Frontend: `frontend/jest.config.js` enforces statements **52%**, branches **43%**, functions **38%**, and lines **52%** when coverage runs. These are ratchets, not estimates of current coverage.

The fast deployment-hygiene tests inspect Compose configuration. To also run the
seven Docker integrations locally:

```bash
cd backend
RUN_DOCKER_HYGIENE_TESTS=1 pytest tests/test_deployment_hygiene.py --no-cov
```

They build five synthetic `FROM scratch` contexts using Docker's real ignore matcher
and run two Redis memory-pressure checks. They never copy the repository's real
`.env`, database, upload, or credential data into Docker. A running Docker daemon is
required when enabled; otherwise these integrations skip locally. CI's Backend Tests
sets this flag explicitly.

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
│   ├── requirements.txt      # Runtime dependency inputs
│   ├── requirements-dev.txt  # Development dependency inputs (includes runtime)
│   ├── requirements.lock     # Hashed runtime dependency tree
│   └── requirements-dev.lock # Hashed development/CI dependency tree
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

> Postgres-targeted. Local SQLite normally gets its schema from `create_all` (see "Create
> Schema" above); `python -m scripts.reset_dev_db` rebuilds the dev file through the same
> bootstrap below, where the Postgres-only steps no-op.

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

> **Every migration after the `058` stamp must be idempotent against a `create_all`-built
> schema.** Step 3 *runs* those revisions over tables, columns and indexes the models already
> built, so each one guards every object it creates — table, column, index, unique constraint,
> FK — **independently** (the `088_api_tokens` `_has_table` / `_has_index` style); a table guard
> alone still lets the `create_index` after it fail. Inside a `batch_alter_table`, guard every op:
> on SQLite a constraint or FK op makes the batch rebuild the table, which is where `089`'s
> re-added column surfaced. Postgres security statements stay **unconditional** and idempotent:
> `ENABLE ROW LEVEL SECURITY`, `REVOKE`, and trigger DDL (`CREATE OR REPLACE FUNCTION`,
> `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`, since model `after_create` listeners may already
> have built the trigger). The reason is not the bootstrap itself — there, `059`'s `pg_tables`
> sweep enables RLS on every table `create_all` built — but that `create_all` can build a table
> *before its migration runs* on an existing database: every non-production boot calls it
> (`app/main.py`), as does `scripts/seed_data.py`, and it enables RLS only where a model's
> `after_create` listener does. A security statement nested under a table-exists guard would then
> never fire, and that table would stay RLS-off. The one guard such a statement may need is a
> **role**-existence check. Any SQL naming Supabase's `anon`, `authenticated` or `service_role`
> runs under `DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '<role>') THEN …
> END IF; END $$` (or a Python check like `059`'s `_role_exists`). That means a `REVOKE` / `GRANT`
> grantee, and equally a role membership grant, a policy's `TO`, `ALTER DEFAULT PRIVILEGES … FOR
> ROLE` or `has_table_privilege('anon', …)`. Those roles exist only on Supabase. On CI, local Docker
> or a self-hosted Postgres, a statement naming a missing role aborts the migration, and the chain
> with it. Model `after_create` DDL that names one carries the `pg_roles` check in its own SQL:
> `create_all` executes it as written, where no Python guard can reach. `PUBLIC` always exists and
> needs no guard.
>
> **What enforces this.** `backend/tests/test_migration_fresh_bootstrap.py`, on SQLite in the
> blocking Backend Tests job: (1) the full bootstrap on a temp file reaches head with a schema
> matching a pure `create_all`, object for object, and a re-run moves nothing; (2) a
> **round-trip** — `create_all` → `stamp heads` → `downgrade 088_api_tokens` → `upgrade head` —
> must rebuild that same schema, FK names included. On (1) every `089`+ create is skipped by its own
> guard, so a deleted create or a `create_table` drifting from its model is invisible there; the
> round-trip makes them execute. Its floor is `088`, not `058`: a `058` round-trip surfaces 34
> known, accepted model-vs-migration differences in `059`–`088` (as of `113`), and an allowlist
> would hide the next real one. It proves each downgrade *completes*, not that it removes
> everything — an object a downgrade leaves behind is found again by its upgrade's guard, so that
> create stays unexercised;
> (3) static checks:
>
> - **Table-existence guards.** `test_rls_and_revoke_never_run_under_a_table_existence_guard`
>   fails any `ENABLE`/`FORCE ROW LEVEL SECURITY` / `REVOKE` reachable from `upgrade()` under a
>   table-existence guard, in every revision the path runs.
> - **Supabase roles in migrations.**
>   `test_sql_naming_a_supabase_role_runs_only_under_a_role_existence_check` fails any executable
>   SQL literal reachable from `upgrade()` that names `anon` / `authenticated` / `service_role`
>   outside a role-existence check. SQL comments are stripped first, and `rolname` comparisons and
>   `PUBLIC` are exempt. A role interpolated into a grantee, membership, `FOR ROLE` or policy `TO`
>   position counts too.
> - **Supabase roles in models.**
>   `test_model_ddl_naming_a_supabase_role_carries_its_own_role_check` requires every model DDL
>   literal naming one of those roles to carry its own `pg_roles` check.
>
> They are regression **tripwires**, not proofs. They check where a statement sits, not what it does.
> An in-SQL `pg_roles` test anywhere in the same literal counts, with no proof that its `IF` encloses
> the statement. A Python guard's polarity is not checked. A role interpolated anywhere else, or into
> model DDL, is not seen. The module docstring lists every blind spot.
>
> No `postgresql` branch executes on SQLite. These statements run on real Postgres only in the
> `scripts/verify_*_postgres.py` steps of the **non-blocking** `e2e.yml`, which also exercise the
> trigger rule. That rule is still not a blocking gate. The steps run directly after `Install backend
> dependencies`, ahead of Node, `npm ci` and the Playwright browser install, because they need only
> the backend and the Postgres service. They used to run after `npx playwright install`, so when that
> install broke on the runner image in November 2026 every run died first and silently skipped all
> of them. `tests/test_ci_workflow_gates.py` (`TestE2EPostgresVerifiersRunFirst`) pins the order.
> The steps run in this order:
>
> - **`verify_operations_postgres` and `verify_fabrication_quoting_postgres`** are per-migration.
>   - They load selected migration modules and re-run `095`–`098`, `106` and `107` over their own
>     objects.
>   - They build the `101`–`103` and `106`/`107` tables from the **models** first (their
>     `after_create` listeners create RLS, revokes and triggers) and run the migrations over them.
>     They then assert RLS, `anon`/`authenticated` denial under simulated inherited grants, the
>     immutability guards and an unchanged trigger set.
>   - They run `091`–`094` + `113` in a scratch database. The first pass is on a cluster without the
>     Supabase roles (CI's has none). The next passes are under simulated `ALTER DEFAULT PRIVILEGES …
>     GRANT ALL … TO PUBLIC, anon, authenticated`, and they assert that every table and sequence
>     stays private, including after `113`'s downgrade.
>   - They create `anon`/`authenticated` as transaction-scoped temporary roles, so they are the only
>     place in CI where the role guards' **roles-present** branch executes.
> - **`verify_fresh_bootstrap_postgres`** runs next, before the seed. It is the only real-Postgres
>   run of the actual chain. In a scratch database, dropped afterwards, it checks four things:
>   - `create_all` → `stamp 058_process_sheets` → `upgrade head` runs every revision from `059` to
>     head over a `create_all` schema and ends at the single head.
>   - A re-stamp + `upgrade head` leaves the schema unchanged.
>   - Every `public` table has RLS on, `alembic_version` included.
>   - `downgrade 088_api_tokens` → `upgrade head` restores the identical schema. That executes every
>     `089`+ Postgres downgrade branch.
>
>   Its snapshot covers catalog state SQLite cannot hold: ACLs, RLS flags, trigger and function
>   definitions, policies and enum labels. It compares runs with each other, not with a pure
>   `create_all`; the SQLite test above does that. On CI's role-less cluster it exercises the role
>   guards' **skip** branch.
> - **`verify_audit_deadlock_postgres`** runs after it, also before the seed, in its own scratch
>   database. It runs no migration. It builds a deadlock on the global audit-chain lock and checks
>   that the victim request answers the app-wide 409, saves nothing, and succeeds on retry, on
>   UPDATE routes, an INSERT route and `POST /work-orders/` (whose creation audit must roll back
>   with the work order). It covers both `AuditService.log`'s re-raise and the COMMIT guard in
>   `app/db/database.py`, neither of which SQLite can reproduce. It refuses to run when
>   `AUDIT_HASH_CHAIN_ENABLED` is off.
>
> *2026-11-02:* `089`–`098`, `106` and `107` shipped unguarded, so this path failed at `089`
> (`python -m scripts.reset_dev_db` included) until guards were retrofitted to those twelve
> files. Production was unaffected (it migrated incrementally from `088`), and CI never noticed:
> E2E builds the app's schema with `create_all` + seed, its Postgres verifiers run selected
> migration modules only in an empty schema, and nothing exercised the create_all → stamp →
> upgrade chain. The fix is the guards, **not** a later stamp point — stamping past those
> revisions would skip their Postgres RLS / `REVOKE` statements, the stamped-over hazard below.
> Retrofitting existence guards — `_has_*` checks, `IF [NOT] EXISTS` DDL, or a `pg_roles`
> role-existence check — is the one sanctioned edit to an applied migration (precedent
> `1897d457`, `a0dd1b09`, `4e5fe4e1`; for a role check, `093` on 2026-11-03). Object guards no-op on
> any database that already ran the revision, and on one that hasn't they emit the same DDL — plus,
> in `106`/`107`, twelve `DROP TRIGGER IF EXISTS` that no-op there — so the resulting schema is
> identical. A role check qualifies because a statement naming a missing role aborts the
> migration: any database with the revision recorded had the role, so the guarded form runs the
> identical statements there. Anything a retrofit would *add* is a new effect and goes in a new
> revision.
>
> *2026-11-03:* four more gaps between the incremental path and this one. (1) `100` creates four
> FKs by name (`fk_receipt_certificate_document`, `fk_receipt_delivery_batch`,
> `fk_po_supplier_acknowledged_by`, `fk_po_follow_up_owner`) and its downgrade drops them by name,
> but `app/models/purchasing.py` declared them unnamed — prod carried the names, every
> `create_all` database auto-generated others. The model now names them (the lock-step convention);
> `100` is unchanged, since it guards by column and still no-ops on the fresh path, and
> `test_migration_100_receiving_supplier_followup.py` pins both sides. Dev databases built by
> `create_all` before the fix keep auto-generated names: harmless (`100`'s downgrade still
> completes), but different from prod until rebuilt. (2) The chain test could not see (1), which is
> what the round-trip above closes. (3) `093` revoked with a bare `FROM anon, authenticated`, so on
> any Postgres without those roles this path died at `093` (`role "anon" does not exist`; reproduced
> on a local postgres:15) — nothing in CI ran `093` on Postgres. It now carries the role checks
> `091`/`092`/`094`–`098` already had. The static role checks now catch the next one, and
> `verify_fresh_bootstrap_postgres` now runs the chain on a role-less Postgres in `e2e.yml`. (4) `093`
> never revoked `FROM PUBLIC`, and `091` never revoked `user_workspace_records_id_seq` at all. Those
> are new effects, so they went into a new revision, `113_data_api_revoke_parity`, whose downgrade is
> a deliberate no-op (`docs/SUPABASE_SECURITY.md` → Data API revoke parity).

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
> widens it (to `varchar(128)`), but on this path `014b` sits below the `058` stamp baseline, so it
> is *stamped over*, never *run*: the column `alembic stamp` creates stays `varchar(32)`, and a
> revision id longer than 32 chars fails to record (`value too long for type character
> varying(32)`). So keep revision ids to ≤ 32 chars (e.g. `038_optimistic_lock_backfill`,
> `039_uq_open_time_entry`). The limit applies to the `revision`
> id, not the filename (`099_recoverable_import_batches.py` is 33 characters; its id is 30).
> Every id from `059` on is within it. SQLite does not enforce `VARCHAR` length, so the bootstrap
> chain test alone could not catch an overlong one; `test_migration_fresh_bootstrap.py` asserts
> the limit directly for every revision from the `058` stamp up.

> **A business key on a tenant table is unique per company, never globally.** A number, code or
> user-entered ID on a `TenantMixin` table is declared as a named
> `UniqueConstraint("company_id", "<col>", name="uq_<table>_company_<col>")` in `__table_args__`,
> with the column itself `index=True` but **not** `unique=True`. On a tenant table, `unique=True`
> makes `create_all` build a global unique index `ix_<table>_<col>`. That is the bug class `115`
> and `116` fixed. With a per-company generator, a second company draws a number the first already
> holds and the insert returns 500 (`116`'s NCR, CAR and FAI numbers). With a user-entered value,
> the refusal tells one company which values another company uses (`115`'s customer names and
> codes; `116`'s equipment IDs, tool IDs and downtime reason codes).
>
> To re-key an existing column, use `115_customers_unique_per_company` and
> `116_business_keys_per_company` as templates. Rebuild `ix_<table>_<col>` as a plain index under
> the same name, add the `uq_` constraint, guard each object independently, and mirror the result
> in the model in the same PR. The upgrade needs no dedup, because adding `company_id` only loosens
> the key. The downgrade tightens it, so it counts cross-row duplicates first and refuses before
> any DDL, reporting each `table.column` and a count but never the values.
>
> Seven tenant tables still carry a global single-column unique on a business number. They are a
> follow-up, not a pattern to copy. None returns a 500 the way NCRs did, because none is numbered
> per company. Where the number comes from an install-wide sequence, it discloses how many records
> other companies created:
>
> - `documents.document_number`, `engineering_change_orders.eco_number`,
>   `shipments.shipment_number`, `mrp_runs.run_number` and `maintenance_work_orders.wo_number`
>   come from a scan across every company. Only the document and shipment scans run under an
>   install-wide lock, so two concurrent ECO, MRP-run or maintenance creates can still race to
>   one number.
> - `quotes.quote_number` is minted `FAB-<company_id>-<fabrication quote id>-R<revision>`, which
>   embeds a global primary key.
> - `rfq_packages.rfq_number` has no writer in `app/` today.

### Running the PostgreSQL verifiers locally

The verifiers create and drop scratch schemas and databases (`DROP DATABASE … WITH (FORCE)`), so
`verify_operations_postgres`, `verify_fabrication_quoting_postgres`,
`verify_fresh_bootstrap_postgres` and `verify_audit_deadlock_postgres` all refuse anything but a
local, disposable test Postgres.
They share one guard, `scripts/_pg_verify.py::require_local_test_postgres`, unit-tested by
`tests/test_pg_verify_guard.py`. The guard requires all of these:

- `ENVIRONMENT=test`;
- a `postgresql` URL with an explicit host in `localhost` / `127.0.0.1` / `::1` / `postgres`;
- query parameters only from an allowlist (`sslmode`, `application_name`, `connect_timeout`,
  `options`, `client_encoding`);
- no `PGHOSTADDR` or `PGSERVICE` in the environment.

The query and environment rules exist because SQLAlchemy hands query parameters to libpq, which
honours them over the URL's own host: `…@localhost/db?host=<remote>` connects to the remote, and
so do `hostaddr`, `service` and `dsn` (psycopg2 merges `?dsn=hostaddr%3D<remote>` beside
`host=localhost`). The query rule is an allowlist because a denylist of those keys missed `dsn`.

`verify_fresh_bootstrap_postgres` also re-checks, in each child process, the URL the app's
`Settings` actually resolves, and requires it to name the scratch database. `Settings` prefers a
Supabase URL from the environment or `backend/.env` over `DATABASE_URL`, so a Supabase URL there
makes this verifier refuse.

The five older race/API verifiers (`verify_nesting_runs_postgres`,
`verify_nesting_spacing_postgres`, `verify_nesting_sources_postgres`,
`verify_stock_piece_postgres`, `verify_stock_piece_api_postgres`) still check only `url.host`. CI
reaches them only through `verify_operations_postgres`, after its guard, so run them that way.

To run them, start a throwaway `postgres:15` container. Its `POSTGRES_USER` is a superuser, which
the verifiers need to create databases and temporary roles. Then run each script from `backend/` as
`e2e.yml` does:

```bash
docker run --rm -d --name werco-pg-verify -p 55432:5432 \
  -e POSTGRES_USER=werco_user -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=werco_verify postgres:15

cd backend
export DATABASE_URL=postgresql://werco_user:verify@127.0.0.1:55432/werco_verify
export ENVIRONMENT=test
export SECRET_KEY=<any 32+ characters> REFRESH_TOKEN_SECRET_KEY=<another 32+ characters>
python -m scripts.verify_operations_postgres
python scripts/verify_fabrication_quoting_postgres.py --database-url "$DATABASE_URL"
python -m scripts.verify_fresh_bootstrap_postgres
python -m scripts.verify_audit_deadlock_postgres

docker rm -f werco-pg-verify
```

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
4. **Create API endpoint** in `app/api/endpoints/`. An authenticated route must depend on `get_current_company_id`, and a tenant row must be built with `company_id`. CI enforces both (see Testing → Tenant-scope guards).
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

## Operation numbers (`operation_number`)

`WorkOrderOperation.operation_number` and `RoutingOperation.operation_number` are `String(20)`
**identifier** columns, not display labels — and they are **user-editable free text**, so the office
may hold `10`, `OP-10A`, or `100-B` in them. Three rules:

**1. Never mint an `Op ` prefix into the column.** Every write site stores the bare identifier
(`str(sequence)` when it has to invent one): `work_orders.py` (the routing → WO copy plus the
assembly and component operation builders), `routing.py` (`POST /routing/create-from-generation`,
`POST /routing/{id}/operations`, `POST /routing/{id}/operations/reorder`),
`routing_import_service.py`, and the `WorkOrderNew` create form. The prefix belongs to the **view**,
and two helpers in `frontend/src/utils/operationLabel.ts` are the only things that decide how it
reads:

- `formatOperationLabel(v)` → `Op 10`. Adds the prefix, **absorbs a legacy one** (`Op 10`, `OP10`,
  `op-10`, `Operation 10` all collapse to a single `Op `), is idempotent, and renders `Op —` for a
  blank value.
- `operationNumberText(v)` → `10`. The bare identifier, implemented by **peeling
  `formatOperationLabel`'s output** so the two definitions of "what counts as a prefix" cannot
  drift. Blank in, empty string out, so each call site keeps its own fallback via a plain `||`.

The rule for choosing between them: **a column header supplies the word "Op"; a free-standing value
does not.** Under an `Op #` header render `operationNumberText`; in a sentence, a chip, or a toast
render `formatOperationLabel`. Neither is for a numeric `sequence`/counter — `Op {sequence}` and
`Op {n}/{total}` render an integer and are correct as they stand.

**2. The fix is forward-only, and there is deliberately no backfill.** Rows written before it keep
`Op 10`, so the column is permanently mixed. That is by design, not debt:

- A backfill `UPDATE` mutates a **user-editable field on released quality plans**. A raw migration
  writes no `AuditService` row, and manufacturing one afterwards is precisely the out-of-band audit
  write compliance invariant 2 forbids.
- It could not tell a minted `Op 10` from an office-typed `OP-10A` that a customer's numbering
  requires, so it would silently destroy real data.
- The column's format was never stable anyway — a planner retyping `10` as `OP-10A` produces the
  same variation with no deploy involved. Convergence therefore belongs at the display layer, which
  is what `formatOperationLabel` is.

The one place a stored value **is** rewritten is `POST /routing/{id}/operations/reorder`, which
re-derives `operation_number` from the new `sequence` for every operation it moves. Pre-existing
behavior, deliberately left: it normalizes a draft routing's legacy values as a side effect, and it
will overwrite a hand-typed `OP-10A`. See [docs/API.md](API.md) → Routing.

**3. Only two consumers parse the column, and both extract digits** — so both are indifferent to the
spelling: SPC critical-dimension matching (`app/api/endpoints/shop_floor.py::get_operation_documents`,
which compares against the *integer* `SPCCharacteristic.operation_number`) and
`_normalized_operation_number` behind the completion-evidence reconcile key
(`app/services/work_order_state_service.py`). Nothing else slices, compares, sorts, or filters on it.
A third parser must extract digits too — never slice a fixed prefix length, and never compare two
stored values for string equality.

Audit rows carry `operation_number` as their `resource_identifier`, so the same operation can read
`Op 10` before this change and `10` after. That is not a defect — `resource_type` + `resource_id`
(the operation PK) is the real key, and the hash chain verifies each row by recomputation from its
own stored value — but the audit search box is an `ILIKE`, so **search the digits**. See
[docs/API.md](API.md) → Audit Log.

Deliberate non-transforms: laser nest operations keep their own `Nest 1` / `Nest 2` numbering (a
correct label, not a doubled prefix); `work_order_duplicate_service` copies a legacy value verbatim;
and a routing's caller-supplied `operation_number` crosses to its work order untouched — the mint is
only ever a fallback.

Regression coverage: `backend/tests/api/test_operation_number_identifier.py`,
`frontend/src/utils/operationLabel.test.ts`,
`frontend/src/pages/WorkOrderNew.operationNumber.test.tsx`, and
`frontend/src/pages/operationNumberIdentifier.crossPage.test.tsx`.

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
- Reinstall the committed tree with `npm ci`; keep `package-lock.json` intact.
- Check TypeScript errors with `npm run type-check`

### Docker issues
- Stop all containers: `docker-compose down`
- Remove volumes: `docker-compose down -v`
- Rebuild: `docker-compose build --no-cache`

## CI/CD Pipeline

The project uses GitHub Actions for CI/CD:
- Runs on every push and pull request
- Executes tests, linting, and type checking
- Verifies Python hash-lock regeneration and the landing site's separate type/build checks
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
backend/vite logs upload on failure. The job pins its runner to `ubuntu-24.04` rather than
`ubuntu-latest`, because Playwright's browser install supports only the images on its own
support matrix; move the pin only together with a Playwright bump. Details in
`docs/CI_CD_SETUP.md`.

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
