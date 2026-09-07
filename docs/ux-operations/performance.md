# Actual browser performance

Admin Settings → App Performance shows real signed-in browser measurements. LCP
measures loading, INP measures input response and CLS measures layout stability.
Results distinguish page loads from supported within-app navigations and separate
mobile, tablet and desktop viewport sizes. Each group includes its exact nearest-rank
75th percentile, sample count, good-sample percentage and artifact release.

The small-sample label appears below 20 measurements; an empty result never appears
as a perfect score. This is a diagnostic view, not a count of unique users or visits.
Different metrics may have different sample counts because browser support and
interaction behavior differ. Summaries return at most 200 groups per page; paging
never truncates the samples used to calculate each group. Changing company clears
the report and ignores responses from the previous session.

## Collection and privacy

The client uses Google's `web-vitals` 6.2.1 normal build, without the attribution build.
It initializes one set of observers at app boot. The payload contains only a fresh
random metric receipt ID, a source-controlled route template, coarse viewport bucket,
metric value/name, navigation type, sequence and release. Queries, hashes, customer
identifiers, form contents, DOM targets, performance entries and raw URLs never enter
the payload. Nothing is sent to an external analytics service.

Collection requires an authenticated user access token. Public sign-in, TV/station
principals and expired sessions do not submit data. Company changes clear pending
measurements. The upload uses its captured bearer without a cross-company refresh
retry; the server derives the tenant from that verified bearer. Kiosk access is limited
to collection config and sample ingestion; a kiosk administrator cannot administer
performance history or settings.

Do Not Track and Global Privacy Control disable initialization. The company admin
can pause collection; the server enforces that setting even for an older open tab.
Clear measurements affects only the active company's anonymous history. Pause and
clear requests capture their original bearer and never automatically refresh/replay;
a delayed 401 after a company switch cannot mutate the newly selected company. No browser
cookie, local-storage analytics identity or durable upload queue is created. Failed
uploads are dropped so analytics cannot burden a production workflow.

Payloads have a 10-sample limit and strict finite-number/enumeration bounds. A shared
database lock serializes receipt updates, the daily quota and the admin off switch.
The company limit is 5000 new metric samples per UTC day. Repeat reports update the
same metric only if their sequence advances, including a lower final value; older
responses cannot overwrite newer values. Metric identity cannot change on replay.
The existing rate-limit middleware caps ingestion at 60 batches/minute per shop NAT.

Samples older than 30 days are excluded from all reports, pruned during ingestion,
and deleted by the daily `cleanup_runtime_metrics_job`. Deployments using an explicit
`WORKER_CRON_JOBS` allowlist must include that job; subtraction lists follow new jobs
automatically. New tables use RLS and revoke browser/Data API role access. FastAPI's
custom JWT and explicit tenant queries remain the authorization boundary.

## Browser interpretation

Where the browser supports soft-navigation measurements, the library's own
`navigationURL` identifies the correct route even when a report arrives after another
screen opens. Other browsers retain document-lifetime measurements attributed to the
screen first loaded. These groups are labeled separately. A login or company switch
does not reattribute earlier document measurements to the new session.

The frontend release is compiled from Vercel's source commit or Railway's artifact
receipt. Local development is labeled `development`; it is never mistaken for a
production commit.

## Validation

- API regressions cover replay ordering, exact percentile cohorts, quota, admin and
  kiosk boundaries, company isolation, forbidden payload data and retention.
- Browser-unit regressions cover route redaction, static/detail route distinction,
  once-only observer registration, original-navigation attribution, company changes,
  privacy signals and disabled collection.
- Admin UI tests cover units, sample warnings, filters, pause, confirmed clearing,
  error retry, bounded paging, company changes and empty results.
- Migration 098 is checked up/down on SQLite and for PostgreSQL RLS/grant SQL.
- The PostgreSQL E2E preflight executes migrations 095–098 in a disposable schema and
  verifies p75 for cohorts of 1, 4, 5 and 8. Both migration cycles check table/sequence
  privilege denial against simulated inherited PUBLIC/anon/authenticated grants. Explicit `ceil(n*0.75)` avoids PostgreSQL's
  rounding numeric-to-integer cast, which differs from SQLite.
- Real-browser E2E passed: authentic LCP upload returned 202 and excluded private query text. Desktop and 390px mobile screenshots passed scoped WCAG A/AA checks with zero violations and no page overflow.

Primary implementation references: [web-vitals](https://github.com/GoogleChrome/web-vitals),
[Core Web Vitals](https://web.dev/articles/vitals),
[Supabase grants and RLS](https://supabase.com/docs/guides/api/securing-your-api).
