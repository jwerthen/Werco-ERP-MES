# Recorded-piece planning validation

September 9, 2026. This records local acceptance evidence for the conditional
nesting/material-planning increment. Production promotion is a separate gate.
Pricing integration, reservations, inventory consumption and automatic remnant
registration are outside this increment.

## Geometry and source foundation

Wave A is commit `e7378b9c1060b4969e235bfb699e04a39934cc01`. The immutable domain
profile is `werco-remnant-domain-v1`, SHA256
`114171806c36fee380801a72b2beb346a41fa1af83884097f9604f417cdd445b`.
The existing compensated profile remains unchanged. Canonical nanoinch source
containment precedes coordinate translation. Final acceptance uses vector
containment, nominal clearances and compensated part collision checks; candidate
approximations never bypass these constraints.

The Wave A library/picker sweep passed 401 tests; subsequent standalone-domain
import refusal checks passed 33 tests across four suites. The larger synthetic
geometry case improved from 10.033 seconds to 0.859 seconds with identical complete
Nest JSON hash `ea2541798e9bd3e7e9b22c08891385a50d6e608ead84262d4cf2f4f7c9b9505d`.
These timings are a local comparison, not a production latency promise.

## Staged integration

- Backend related regression: 378 passed, two PostgreSQL-only skips. Both skipped
  checks passed separately on disposable PostgreSQL17. The final strict protocol
  ordinal/instance-index suite passed 30 tests.
- PostgreSQL evidence: 37 cases, including actual Node child execution, persisted
  stages/reports, omitted/null/object JSON selection detection, raw hashes,
  historical replay, revoked evidence permissions before claim/checkpoint, and
  observation withdrawal blocked by the save/start shared header lock until commit.
  The database contained synthetic fixtures and was removed after verification.
- Frontend broad gate: 545 of 547 tests passed on its initial 75-suite run. The two
  failures were legacy test fixtures (synchronous save timing and a v6 literal);
  the corrected affected suites passed all 15 tests, preserving their behavioral
  assertions. Four independent saved-preview race/tenant/receipt cases also passed.
- Root staged solver/packaged Node tests: 13 passed, including mixed thicknesses,
  the 36-stage aggregate cap, exact residual originals, all-fit zero residuals,
  nonfitting/invalid source fallback and isolation from input/consumer mutations.
- Independent image-oracle tests: 18 new and 31 existing tests passed. Mutations
  of source outer/hole coordinates, missing-corner/hole placements, part overlap,
  counts, maps, source IDs, credit, summary and packaged runtime/profile identity
  were refused.
- All three TypeScript programs, scoped frontend lint, whole-backend MyPy438
  files, Flake8 and Bandit, configured Python formatters and diff checks passed.
  Named compliance review found no material issue. No migration, new environment
  variable or extra cron schedule was introduced.

## Actual browser and production packages

The real MaterialNesting route and shadow-root presentation were exercised in a
local headless Chromium session. Only API responses were synthetic; the actual
browser worker, geometry engine, React controls, preview and export code ran.
This was not an authenticated production-browser smoke.

The synthetic L-piece has a missing upper-right corner, a physical hole and a
negative one-nanoinch source origin. For ten 2-by-2-inch parts, the full-sheet
baseline used two 8-by-8-inch sheets. The conditional layout placed eight original
instances on the piece and the exact remaining originals 8 and 9 on one full sheet.
Both local and saved previews retained the actual outline/hole. The JSON review
and saved report downloads preserved the instance partition and zero-credit ledger.

SVG export round-trips as XML and includes physical dimensions in inches while
retaining the internal SVG viewBox. The 820-pixel viewport check kept the new panel
within its viewport. Changing quantity hid stale geometry and disabled its review
buttons. New and reload returned to zero designs and no selection. No browser page
errors occurred in the completed flows. Temporary harness files, browser sessions,
local server and database containers were removed.

Both production Docker images built successfully. The common required
`smoke_nesting_worker.py` gate ran the exact worker image as its non-root user with
no network, a read-only filesystem, dropped capabilities and bounded resources.
It checked ordinary protocol1, exclusions, legacy refusal and staged protocol2.
The actual L-shaped material and hole have independent rectangle-distance oracles;
the smoke also ties counts, residual originals, v4 ledger and both profiles to the
packaged bundle. Packaged Node was `v22.23.2`; bundle SHA256 was
`7da19aab9c9d6006cf58ec073325dcb852699908761ad7639abc3f801f8cafa9`.
The frontend image served `/nest` and all five referenced startup assets over its
internal HTTP listener as UID1001 with no external network.

Vite completed its production build with existing CSS import-order/property and
large-chunk warnings. Those warnings do not replace the browser/layout checks.
Exact release CI, public release identity and active-worker verification remain
required before this increment is described as live.
