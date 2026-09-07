# Measured performance — package 6

Two concrete costs were removed from Work Orders: the unopened laser import workflow was part of the route's static JavaScript dependencies, and the desktop page mounted every hidden mobile card alongside its paginated table.

The laser workflow now loads when opened. Its loading dialog can be cancelled; a failed download stays within that dialog, with retry and then an explicit page reload if the browser caches the failure. The existing standalone preview → import → work-order navigation test still passes. Desktop and mobile now mount only their active list. The initial render reads the current viewport, and the 1024px breakpoint responds to resize without resetting the parent-owned filters or sorting. Desktop pagination can restart when remounted.

## Controlled measurements

The baseline is commit `0d89e19`. “Before” is a snapshot containing the new workflow controls before these two optimizations; “after” changes only the lazy import and mounted responsive views within that snapshot. The raw results and source hashes are in [performance-evidence](performance-evidence/).

| Measurement | Previous main | Before optimization | After optimization |
| --- | ---: | ---: | ---: |
| Work Orders incremental JS, gzip estimate | 39,991 B | 43,561 B | 29,495 B |
| Work Orders incremental JS, minified | 121,714 B | 131,065 B | 89,709 B |
| Complete initial JS graph, gzip estimate | 268,212 B | 272,121 B | 258,070 B |
| Desktop DOM nodes, 1,000 work orders | 39,392 | 39,403 | 1,399 |
| Search/filter completion, median | 537 ms | 531 ms | 345 ms |
| Navigation to populated first row, median | 1,587 ms | 1,603 ms | 1,575 ms |
| First import dialog ready, median | 91 ms | 89 ms | 379 ms |

Relative to the controlled before variant, the route downloads **32.3% less incremental gzip-estimated JavaScript**, mounts **96.4% fewer desktop DOM nodes**, and completes the tested search/filter interaction **35.0% sooner**. Initial row readiness changed by only 28ms; this is too small to treat as a reliable general load-time improvement. The import's first open costs about 290ms more in this lab because its code is now fetched on demand. Subsequent opens use the downloaded module.

These are local laboratory results, not field gains. Five runs per variant were interleaved in Chromium 145.0.7632.6 at 1440×1000, with 4× CPU throttling, 5Mbps network/20ms latency emulation, gzip-served static assets, and 1,000 synthetic work orders. API responses were intercepted, paginated at 500 records, and delayed by a fixed 30ms. No production backend or user data was used. Search measurement includes the existing debounce. All 15 measured journeys reported zero page errors. The dynamic wizard chunk was absent before opening in the optimized build.

The real-browser failure probe aborted the first wizard chunk download. Chromium retained the failed module across a simple retry; the dialog's explicit **Reload page** action recovered, and reopening the wizard succeeded. This probe caused no import or outbound request to a real backend.

## Repeatable checks

From a frontend checkout, create a manifest build and run:

```sh
npx vite build --manifest --outDir /tmp/werco-current-build
node scripts/measure-route-bundles.mjs /tmp/werco-current-build
LAB_SAMPLES=5 LAB_ROWS=1000 node scripts/measure-work-orders-lab.mjs /tmp/werco-current-build
LAB_SAMPLES=0 LAB_PROBE_CHUNK_FAILURE=1 node scripts/measure-work-orders-lab.mjs /tmp/werco-current-build
```

Both measurement scripts accept several build directories for interleaved comparisons. Build commit `0d89e19` in a separate exported checkout with the same dependency lock and runtime for the previous-main comparison. Keep full frontend source/configuration with each build; Vite needs the normal Tailwind and chunk configuration. The bundle script counts entry plus static route imports, deduplicates shared files, and excludes unopened dynamic features. Gzip sizes are computed per emitted JavaScript file and do not represent CDN Brotli, cache-hit behavior, CSS, or backend latency.

## Regression evidence

- **97 tests passed across 11 suites**: all Work Orders suites, the lazy loader's closed/open/callback/error-retry cases, and responsive switching tests. The existing full nest import flow waits for the actual loaded pick step rather than the temporary loading dialog; no workflow assertion was removed.
- Breakpoint tests prove the inactive tree never mounts initially on mobile and that controlled filtering/sorting survives both directions at 1023/1024px. Existing desktop action and role assertions remain green.
- Frontend source and test TypeScript checks and scoped zero-warning ESLint passed. Both Node measurement scripts pass syntax checks.
- The saved controls and search interaction were included in the current snapshot. This work does not claim mobile virtualization, production API performance, or improvements to every page. The mobile list still displays its full filtered set; that is a separate product decision from avoiding its hidden desktop copy.
