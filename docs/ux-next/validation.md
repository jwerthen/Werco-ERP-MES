# Frontend validation — workflow improvement packages

Run September 7, 2026 in `Werco-ERP-MES-next`. All business mutations in these checks target fresh synthetic local data. No production service was used.

## Full frontend gates

| Gate | Result |
| --- | --- |
| `CI=true TZ=UTC npm test -- --coverage --runInBand` | 337 suites / 3,615 tests passed, 160.461 seconds. |
| Coverage | Statements 65.19%, branches 55.53%, functions 50.29%, lines 65.60%; all configured thresholds passed. |
| `npm run type-check` | Product and test TypeScript passed. |
| `npm run lint` | Full frontend ESLint passed. |
| `npm run build` | Production Vite build passed. Vite reports its standard warning for chunks above 500 KB; no build error. |

Full coverage was run at the package freeze. The final PDF viewer, delivery-composer integration, and Purchasing email-permission additions then passed **20 targeted tests**. Product/test TypeScript, full ESLint, and the production Vite build were repeated successfully after those final additions froze. The build keeps PDF rendering in a separate 431.65 KB raw /128.67 KB gzip chunk.

Full coverage log: `/tmp/werco-next-final-jest.log`. Final frozen-source logs: `/tmp/werco-next-final-{types,lint,build}-rerun.log`.

## Full browser suite

The suite uses `CI=true TZ=UTC`, Chromium, one worker, zero retries, and failure traces. Its local API on port 8004 and frontend on port 5177 are separate from the preview on ports 8003/5176. Both passes begin with a newly created SQLite database from `python -m scripts.seed_data`, which creates metadata and seeds the standard company/users/parts/work centers/work orders. This local execution does not establish PostgreSQL migration or concurrency behavior.

The API process uses an isolated environment (`env -i`), explicit test keys, disabled test rate limits, CORS limited to localhost:5177, and empty SMTP host/user/password/from settings. No inherited delivery credentials reach the test process.

First pass: **68 passed, 1 expected station-credentials skip, 3 failed** across 72 tests in 3.0 minutes. The three failures were traced to pre-existing test assumptions or external test dependencies:

1. The user-info test clicked the actual Sign out control and then expected it to remain visible. The screenshot correctly showed the login page. It now checks the authenticated user's displayed name without signing out; the separate logout-control assertion is mandatory rather than optional.
2. The work-order operations test clicked a visible Suspense skeleton row. The trace identified `<tr class="animate-pulse">`. It now waits for a real data row, opens its record link, and asserts the specific Operations / Routing heading. The existing separate row-click test still checks that affordance.
3. Quote recovery never reached its test body: the login navigation exhausted its setup timeout, with an unanswered Google Fonts request in the trace. The local Vite server also emitted page reloads when the concurrent coverage report wrote HTML files. The final browser run starts after coverage completes, and the shared workflow fixture blocks only Google Fonts endpoints, using the application's fallback fonts. ERP API requests and UI assertions remain live. This suite does not assess web-font rendering.

All four affected/focused checks passed after the narrow test changes. Source changes for these corrections are limited to [fixtures](../../frontend/e2e/fixtures.ts), [navigation](../../frontend/e2e/navigation.spec.ts), and [work orders](../../frontend/e2e/work-orders.spec.ts).

First-pass evidence remains in `/tmp/werco-next-final-e2e.log`, `/tmp/werco-next-playwright-results`, and `/tmp/werco-next-playwright-report`. Focused results: `/tmp/werco-next-final-e2e-focused.log`. The first-pass database `/tmp/werco-next-e2e-fresh.db` is preserved. The full rerun used a second fresh database, `/tmp/werco-next-e2e-final.db`, including [workspace recovery](../../frontend/e2e/workspace-recovery.spec.ts).

## Final result

**71 browser tests passed, 1 expected station-credentials test skipped, 0 failures, 0 retries, in 1.8 minutes.** The full process-sheet author/release/serialized-work-order/kiosk/completion journey and all four workspace-recovery flows ran successfully.

Final browser log: `/tmp/werco-next-final-e2e-rerun.log`. Final report: `/tmp/werco-next-playwright-report-final`. Artifact directory: `/tmp/werco-next-playwright-results-final`. Both fresh databases and first-pass failure traces are preserved for review.

The frontend validation package is frozen. No product source changed to correct the first-pass browser failures; the fixture/spec changes make their original workflow assertions deterministic and more specific.
