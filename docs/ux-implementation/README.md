# Werco ERP UX implementation

Implementation of the 25 approved packages from the September 6, 2026 audit. Work is isolated on `codex/ux-audit-improvements`, based on main at `0a383b8c63926dd02e2a37c5153b43a3b095631b`. The original checkout and production application were not changed by these local implementation steps.

The work prioritizes correct business outcomes, recovery from failures, readable record context, and access to the next action. It keeps the existing ERP design and component system.

## Approved-package checklist

| Package | Implemented result | Evidence |
|---|---|---|
| 01 Scheduling selection | Bulk actions use exactly the reviewed visible jobs; hidden selections are disclosed. Partial results name each failed work order and retry only failures. | [Production](production.md) |
| 02 Work-order preview | Deleted routing operations stay deleted. Obsolete part/readiness responses cannot replace the selected part; failed checks are explicit. | [Production](production.md) |
| 03 Quote conversion | Each selected production line creates a separately linked draft WO with its own quantity. Custom items require acknowledgement; remaining production lines stay actionable. | [Quoting](quoting-reports.md) |
| 04 Partial shipping | Pending allocations, dispatched quantities, cancellation and remaining quantities reconcile. A partial dispatch leaves the remainder available. Final fulfillment closes the WO once. | [Operations](operations.md) |
| 05 Navigation context | Receiving nested views retain their parent URL. BOM/Routing catalog searches and selection URLs, plus Reports tab/period URLs, support history and bookmarks. | [Production](production.md), [Operations](operations.md), [Quoting/reporting](quoting-reports.md) |
| 06 Truthful actions | MRP uses Mark reviewed and explains manual supply follow-up. Quote and PO actions say Mark as sent and disclose that they do not send a customer/vendor message. | Domain reports |
| 07 Quote workflow | Calculation snapshots carry quantity, price, lead time and breakdown into review. Changed inputs disable stale promotion. Quotes expose full detail and draft corrections; RFQ approval opens its quote. | [Quoting](quoting-reports.md) |
| 08 Traceability | Serial and lot searches load the matching detail type, expose loading/no-match/error states, and retain navigable investigation context. | [Operations](operations.md) |
| 09 Quality resolution | NCR/CAR records have full detail, investigation, disposition and authorized closure forms, stable URLs and local validation. | [Operations](operations.md) |
| 10 Complete history | Quality and quote lists retrieve the full requested history. Converted/expired quotes are discoverable. Employee Time exposes entries beyond the first ten. | Operations and reporting reports |
| 11 BOM detail | Both explosion views read the actual response envelope. Draft component edits preserve identity and metadata; failure keeps entered values. | [Production](production.md) |
| 12 Trustworthy state | Overview, setup, work-order support panels and maintenance distinguish unavailable, stale and verified data. Calibration totals retain their documented scope. Shell status reflects the update connection. | Domain reports |
| 13 Accessible controls | Pickers stay above their modal and close before it on Escape. Dialog naming/focus, hidden navigation, tab relationships and receipt validation are improved. | [Foundations](foundations.md), [Operations](operations.md) |
| 14 Account/session recovery | Administrator-assisted recovery preserves account identity. Sign-in restores permitted destinations. The real session warning follows a stable inactivity deadline. | [Foundations](foundations.md) |
| 15 Pending/recovery | Scoped pending guards and durable errors cover audited actions. Scheduling exposes partial success. Quote creation supports replay recovery after an ambiguous response; draft edits reject stale snapshots. | Domain reports |
| 16 Entity selection | Searchable human-readable selectors replace raw IDs for audited supplier, employee, work-center, work-order and engineering-change forms. Inventory requires a valid part. | [Operations](operations.md) |
| 17 PO review | Row activation opens full PO detail and a stable URL. Authorized draft correction, explicit Print, and truthful sending remain distinct actions. | [Operations](operations.md) |
| 18 Unsaved work | Shared navigation protection covers SPA Links, breadcrumbs and history. Successful saves mark forms clean before navigation. | [Foundations](foundations.md) |
| 19 Role/help discovery | Routes, shortcuts, search and navigation share permission rules. Help is available on mobile; tours and dismissals are scoped to the current user/workspace. | [Foundations](foundations.md) |
| 20 Documents/audits | Document previews and revision lineage preserve prior files and historical part identity. Supplier findings are fully readable. | [Operations](operations.md) |
| 21 Dispatch freshness | Visible boards refresh every 30 seconds and on focus; updates defer during interaction. Refresh age and errors are visible. | [Production](production.md) |
| 22 Global search | Query ownership prevents obsolete results or recents from replacing current input. Keyboard quick actions, visible selection and retry states are coherent. | [Foundations](foundations.md) |
| 23 Notifications | Bell/inbox share unread state and write invalidation. Bulk-read wording/counts use the stated global scope. Superseded queries cannot restore older rows. | [Foundations](foundations.md) |
| 24 Mobile/readability | New WO actions fit at 390 px. Time Clock uses compact selection with 24 stations. Priority labels are shared, and audited identifiers use readable dark-surface colors. | [Browser evidence](browser-acceptance.md), domain reports |
| 25 Scoped refresh | MRP, Tools and OEE retain selected scope and reject obsolete results. Mutations refresh the active dataset; errors do not masquerade as current data. | Production and operations reports |

## Verification

The combined local backend run passed **6,998 tests** with **85.86% coverage**; the subsequent GitHub backend gate, including FK-name alignment and additional release-order tests, passed **7,011 tests** at the same coverage. The final local frontend run passed **326 suites / 3,556 tests** with CI coverage enabled under UTC, both TypeScript checks, zero-warning lint and the production build. The complete local Playwright suite passed **67 tests**, with one expected station-credential skip. Fresh-browser scenarios and measured mobile/color checks passed using synthetic local records.

Final combined results and browser measurements are recorded in [validation.md](validation.md) and [browser-acceptance.md](browser-acceptance.md). Domain reports retain targeted regression details; their overlapping counts must not be added together.

The browser checks use synthetic records in an isolated local database. They include read-only review and local fixture mutations, with no production business writes or outbound customer/vendor messages. The audit's production screenshots remain historical evidence, separate from implementation screenshots.

## Database and release notes

Apply migration `089_quote_line_conversion` before `090_document_revision_chain`. They add per-line work-order associations and optional quote creation request identity, followed by document predecessor identity. Existing records remain; historical quote links are retained without inventing per-line associations. New application code requires these columns.

Quote replay protection is optional for older clients; the updated UI always supplies a stable request key during retries. Quote and PO reviewed edits use timestamps to detect stale forms. PostgreSQL row locks protect allocation/conversion paths; repository tests use SQLite plus PostgreSQL SQL compilation, so their results do not claim PostgreSQL contention testing.

The validation above was completed locally before remote publication. The user subsequently authorized merging and pushing this work to main. Main triggers the repository's production pipeline; the Railway API image runs `alembic upgrade head` before starting the server, so migrations 089 and 090 ship with the backend.

The Railway release ordering checks ensure a combined backend/frontend change uses the full pipeline, which verifies the new backend release before uploading the Railway frontend. Its standalone frontend path remains available for frontend-only changes and explicit manual runs. GitHub's main ruleset requires a pull request and five passing CI contexts before merge. The release-order regression set passed 111 tests, with YAML and embedded-shell syntax validation.

The user-facing `wercomfg.app` domain is hosted by the existing Vercel project, separately from the Railway frontend. Railway release markers verify only Railway services; the Vercel domain must be checked independently after publication, as documented in [the deployment runbook](../DEPLOYMENT_RUNBOOK.md). This change does not alter that project's hosting settings or domain association.

MRP supply creation and customer/vendor delivery remain manual steps, now named accurately. Account recovery uses administrator assistance because the app has no verified self-service recovery endpoint. These are explicit product behaviors, not hidden successful side effects.

The app's existing large-bundle warning remains visible in the production build. No speed-up, full assistive-technology conformance, or exhaustive device/role coverage is claimed from these checks.
