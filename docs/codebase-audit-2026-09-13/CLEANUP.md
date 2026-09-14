# Cleanup and deployment hygiene — 2026-09-13

Implemented the first requested follow-up to the [codebase audit](README.md) on
`codex/cleanup-deployment-hygiene`, based on `a4a085a31ae3783f5be7d26e182b91951ab67693`.
This record describes the local implementation and verification. No application,
database, or managed Redis deployment was performed.

## Completed

| Area | Result |
|---|---|
| Workspace cleanup | Removed 236 untracked duplicate files only after comparing each with its original and verifying a recovery archive. All were byte-identical; 1,872,809 duplicate bytes removed. Refreshed frontend/landing installs with `npm ci` and the backend `.venv` from the development lock. |
| Python dependencies | Separated runtime and development inputs; removed duplicated Redis constraints and pytest from runtime. Added complete, versioned hash locks, a pinned compiler, and a read-only freshness check. Runtime and development resolve the same runtime package versions. Docker, CI, E2E, deployment verification, and the direct-host deployment script now install the appropriate lock with `--require-hashes`. |
| Dependency fixes | Updated pypdf to 6.16.1, React Router to the patched 7.18.3 lock resolution, affected npm transitive dependencies, and the lock compiler to uv 0.11.15. Retired the frontend audit exceptions after the locked tree became clean. Python retains only the existing documented ecdsa exception. |
| Frontend checks | Enabled Hook call-order rules throughout the frontend and effect-dependency checking in the already-clean shared UI directory. Added tests exercising the actual loaded ESLint configuration. Removed the obsolete ESLint file, ignored CRA configuration, and four unused direct tooling dependencies. |
| Python checks | Re-enabled five correctness checks for schemas, tenant filtering, and PDF text boundaries. Added real mypy probes that fail if these guards stop working. The only backend application edit explicitly narrows `None` before the existing thickness type validation; accepted/rejected values are unchanged. |
| Test reliability | Replaced collection-time wall-clock timestamps in nesting readiness tests with one fixed clock shared by test payloads and the service check. This preserves the stale/future/fresh checks when a long suite delays execution. Production readiness behavior is unchanged. |
| Landing maintenance | Added independent TypeScript, build, and dependency-audit gates; fixed dormant React source errors exposed by type-checking. The shipped static `landing/index.html` remains the entry. The direct-host frontend build now installs development build tools even on a production host. |
| Docker contexts | Excluded nested environment files, virtual environments, credentials, private keys, local databases, uploads, logs, and backups in all three effective contexts. Templates, locks, migrations, release markers, and worker build inputs remain available. |
| Redis retention | Both Compose files explicitly select `noeviction`, retaining AOF and persistent storage. Production retains 256 MiB within its 512 MiB container allocation. Real Redis tests verify queued payloads/indexes survive memory pressure. |
| Runbooks | Updated installation, dependency maintenance, development checks, CI, Docker, worker, and environment guidance. API routes and the role/permission contract are unaffected. |

The duplicate recovery archive and SHA-256 manifest are outside the repository at
`/Users/jonwerthen/.codex/backups/werco-cleanup-2026-09-13/`:
`duplicate-copies.tar.gz` and `manifest.json`. The previous backend environment's
installed-package list is saved there as `previous-backend-venv.txt`. The source of
the duplicate copying was not established; no sync configuration was changed.

## Verification

Final results are recorded in [cleanup-verification.json](evidence/cleanup-verification.json).
Captured output excerpts are in [cleanup-check-output.txt](evidence/cleanup-check-output.txt).
The original audit report/evidence remain an unchanged record of the earlier baseline.

- Frontend: all three TypeScript programs, production build, zero-warning lint,
  four ESLint configuration tests, and 438 Jest suites / 4,303 tests passed.
- Landing: TypeScript, production build, and dependency audit passed.
- Backend: Black, isort, Flake8, mypy, and Bandit at the CI threshold passed.
  The full SQLite sweep recorded **8,346 passed, 1 failed, 26 skipped**, with
  **86.74% coverage** against the unchanged 78% floor. Its sole failure was a
  test timestamp calculated three minutes into the future during collection:
  by execution, that timestamp correctly qualified as fresh. The test clock was
  made deterministic; all **22 affected tests passed**, both normally and with a
  simulated four-minute delay after collection (no sleeps). This is not a claim
  of a second clean full-suite run. Seven Docker integration cases skipped in the
  default sweep were exercised in the separate enabled run below.
- Dependency locks: clean hash installs, `pip check`, freshness verification,
  runtime/development consistency tests, 199 PDF-focused tests, and Linux x86_64
  wheel resolution passed. The runtime-only environment imports the API and worker
  with pytest absent.
- Deployment: five synthetic-context builds exercised Docker's real matcher;
  two isolated Redis tests exhausted memory and verified queued data survived;
  parsed Compose policy checks and the new CI gate checks passed (13 tests total).
  All 67 CI workflow tests also passed against the final files.
- The production API Dockerfile built successfully on Linux arm64. An isolated,
  network-disabled smoke check imported the API/worker as the non-root image user,
  verified PDF tools and migration files, and confirmed pytest and `/app/.env` were
  absent. It did not start the API lifespan or run migrations. The worker image was
  not rebuilt in this local pass; its context and Linux x86_64 Python wheel inputs
  were verified separately.
- Repository validity and private-key hooks passed for tracked files and new artifacts.
- Independent implementation, test-engineer, documentation, and code/compliance
  reviews covered the final changes.

## Rollout and remaining work

Merge/deploy through the existing coordinated release pipeline when this batch is
approved for release. Python installations must use the committed locks; see
[development dependency maintenance](../DEVELOPMENT.md). No dependency migration
or database migration was added.

Managed Redis needs its own provider configuration change to `maxmemory-policy=noeviction`;
application deployment does not set it. Monitor memory and rejected writes before
saturation: existing queued keys are retained, but new cache writes and job enqueue
operations can fail. Keep this retention policy when rolling back an application
image. See [Redis operational guidance](../DOCKER_PRODUCTION.md#redis-queue-retention-and-memory-pressure).

The audit's access-control, session isolation, MRP, transactional, and large-module
refactoring findings remain follow-up work. Broader effect-dependency enforcement
also remains: the diagnostic scan found 56 findings across 36 files that need
behavior review before dependencies are changed. Existing mypy suppressions remain
outside the explicitly tightened modules.

Browser E2E, live PostgreSQL checks, production performance measurements, and a full
production rollout were not part of this local cleanup verification. The landing
site's CDN-loaded script is outside npm audit; its certification claims and sample
testimonials require a separate content review before publication.
