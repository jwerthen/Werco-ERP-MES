# Combined validation

Validation was performed locally on September 6–7, 2026 in the isolated `codex/ux-audit-improvements` worktree. These results cover the repository test scope and the synthetic browser scenarios linked below; they are not a production deployment result.

## Backend

| Gate | Result |
|---|---|
| Full default pytest suite, four workers | **6,998 passed**, 27 warnings, 587.05 seconds |
| Coverage | **85.86%**, above the repository's 78% threshold |
| Mypy | No issues in 364 source files |
| Black | 710 files unchanged |
| isort / Flake8 | Passed |
| Bandit | Passed at the repository's `-ll` severity threshold; no medium/high findings. The scan metrics include 52 low-severity findings below that gate. |

The worktree uses Python 3.11 and the repository's exact runtime/development requirements. Tests use the existing per-worker, in-memory SQLite fixtures. The default `not evals` marker remains in force; live AI evals were not run. Existing warnings were recorded rather than changing the repository warning policy to hide them.

Reproduction from `backend/` with its isolated virtual environment:

```sh
.venv/bin/pytest tests/ -q -n 4 --cov=app --cov-report=xml --cov-report=term
.venv/bin/black --check --diff --config=.black app tests
.venv/bin/isort --check-only --diff --settings-path=.isort.cfg app tests
.venv/bin/flake8 app --max-line-length=120
.venv/bin/mypy app --config-file=mypy.ini
.venv/bin/bandit -r app -s B101 -ll
```

The migration regressions cover preservation, upgrade/downgrade behavior and PostgreSQL SQL compilation for migrations 089 and 090. The migration graph has one head, `090_document_revision_chain`, following `089_quote_line_conversion` and `088_api_tokens`. Applying both additions to an isolated pre-change schema produced no Alembic differences from model metadata for quotes, quote lines or documents; both additions also downgraded successfully.

SQLite does not validate production row-lock contention; a PostgreSQL staging exercise remains part of release verification. No database credentials or production configuration were copied into this worktree.

After the full backend run, the model's existing quote-line FK was given the same explicit constraint name as migration 089, allowing metadata-created schemas to follow the same downgrade path. This metadata-only correction passed 17 quote-conversion/migration tests plus model type/format checks. The full backend suite was not repeated for the constraint-name alignment.

PR #265 subsequently passed the full GitHub backend gate at commit `4888c517a0c81680b63345d012ee79524be8d0bf`: **7,011 tests**, 27 warnings, **85.86% coverage**, 1,147.62 seconds. This includes the FK-name alignment and the additional release-order regressions.

## Frontend

| Gate | Result |
|---|---|
| Combined Jest suite with CI coverage and UTC timezone | **326 suites / 3,552 tests passed**, 54.926 seconds |
| Frontend coverage | 64.30% statements, 54.63% branches, 49.42% functions, 64.67% lines; repository thresholds passed |
| Source and test TypeScript | Passed |
| ESLint with zero warnings | Passed |
| Production Vite build | Passed, 3.45 seconds |

The completed gate table includes the final Receiving history race, StrictMode calculator restoration, Scheduling duplicate-drop protection, work-order independent support loading and retained stale-content corrections. The Receiving and calculator regressions reproduced their observed browser failures before the corresponding fixes.

PR CI exposed a Scheduling calendar bug under UTC: host-local week-start calculation could move Monday's Central date into Sunday and omit its jobs. The calendar now preserves its noon-UTC anchors when calculating week starts and moving days. Both affected tests use a fixed Monday and check the actual dated cell and header while retaining their pending-state and partial-failure assertions. All nine Scheduling regressions passed with coverage in UTC, America/Chicago and Pacific/Kiritimati (UTC+14), followed by the full coverage run and fresh type, lint and build checks reported above.

The full browser run also exposed successful process-sheet creation being blocked by the unsaved-navigation guard. Sheet creation and update now mark the form saved only after a successful API response, before opening its detail. A regression using the real data router and guard reproduced the blocked URL before the fix; its companion test checks that failed creation remains dirty and retains the entered title. The unchanged complete process-sheet browser journey passes with the fix.

Reproduction from `frontend/`:

```sh
npm ci
CI=true TZ=UTC npm test -- --coverage --watchAll=false --maxWorkers=2
npm run type-check
npm run lint -- --max-warnings=0
npm run build
```

The existing build warning about a JavaScript chunk exceeding 500 kB remains. Build completion is not evidence of a measured load-time improvement. Targeted run counts in the domain reports overlap the full suite and must not be added together.

## Browser and review evidence

[Browser acceptance](browser-acceptance.md) records actual local workflows, screenshots, viewport measurements and fixture outcomes. The production screenshots in the [original audit](../ux-audit-2026-09-06/README.md) are historical evidence and are clearly separate from the implementation captures.

The review covers all 25 approved packages through source review, regression tests and selected browser workflows. It does not claim that every possible role, dataset, browser, device or assistive technology was exercised. Deployment requires the two additive migrations described in the [implementation report](README.md).
