# Production workflow reliability and the next eight improvements

This round follows PR 266. Its eight packages are distinct from the earlier saved-draft,
review-before-posting, operational-inbox and document-delivery work.

| Package | Result | Implementation and evidence |
| --- | --- | --- |
| 1. Safe kiosk retries | A stable request ID and durable production receipt let an uncertain report be retried without adding its quantities twice. Original operator and payload bindings are retained. | [Production recovery and calendars](production.md) |
| 2. Coordinated releases | Vercel builds production artifacts without assigning the public domain. The protected production pipeline verifies the API, then promotes the exact matching website artifact. | [Release coordination](release-coordination.md) |
| 3. Background email alerts | One TLS negotiation; visible queued, accepted, failed, skipped and uncertain outcomes; ambiguous submissions are not automatically resent. | [Background email reliability](background-email-reliability.md) |
| 4. MRP purchases by supplier | Select recommendations, review individual quantities/dates/costs, and create one draft PO per supplier with links back to every source recommendation. | [MRP purchase batches](mrp-purchase-batches.md) |
| 5. Mobile work-order browsing | Bounded server queries, accurate filtered totals and progressive mobile loading replace downloading and rendering the full list. | [Work-order browsing](work-order-browsing.md) |
| 6. Working calendars | Per-center weekday hours and dated shutdown/capacity overrides feed the scheduling review and committed projection. | [Production recovery and calendars](production.md) |
| 7. Shared team views | Manager-controlled table configurations can be reused by the team; private drafts stay private. Inventory, parts and shipping gain workspace controls. | [Team workspaces](team-workspaces.md) |
| 8. Actual user performance | First-party LCP, INP and CLS measurements grouped by route template, viewport size, navigation type and release. Admins can pause collection or clear measurements. | [Performance measurements](performance.md) |

## Validation and release status

All eight implementations are present. Local validation includes:

- Frontend: 3,680 tests across 349 suites, including the final captured-token
  performance transport, session behavior and scheduling date regression.
- Backend: all 333 migration tests, 96 registry/release-gate tests and 26 notification
  link tests pass after integration fixes. Full-suite results are recorded in the
  pull request and CI checks.
- Full browser suite: 72 passed with no retries, plus one existing station-credential
  skip, on a fresh synthetic database. Separate acceptance covers the new kiosk,
  calendar, supplier-batch, shared-view and performance workflows.
- Source/test TypeScript, zero-warning ESLint, backend Black/isort/Flake8/mypy and
  production build pass. Bandit reports no medium/high severity findings.
- PostgreSQL CI executes two migration cycles, verifies private table/sequence grants
  against inherited Data API permissions, and checks exact percentile cohorts.

Merging requires every protected check to pass and the release setup below to be
complete. Screenshots and package-specific limitations are linked above.

The isolated browser environment uses synthetic data on ports 8005/5178. No live
customer documents or email recipients are used. Screenshots in this directory show
synthetic fixtures or browser measurements of that local environment.

The coordinated Vercel release needs a project-scoped credential in the GitHub
production environment and auto-assignment disabled on the existing ERP project.
The three project/team/public-domain environment variables are configured.
The Vercel CLI's OAuth session cannot create that credential (`403: Cannot create
tokens for this app`). The account browser session is required to complete setup.
Production auto-assignment remains unchanged until the credential and gate are ready.
