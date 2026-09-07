# Warehouse, purchasing and planning workflows

This package extends the [production workflow improvements](../ux-operations/README.md)
with eight additions. Screenshots use a local environment with synthetic data.

| Improvement | User-visible behavior | Details |
| --- | --- | --- |
| Customer edit preservation | Editing one field preserves all untouched contact, address, payment and shipping data. | [Customers, search and imports](customer-search-imports.md) |
| Recoverable imports | Review a durable batch, resume partial progress, download and correct failed rows, and inspect created-record receipts. | [Customers, search and imports](customer-search-imports.md) |
| Complete search results | Exact identifiers and retired aliases rank before broad matches; filters, totals and pagination cover the full matching set. | [Customers, search and imports](customer-search-imports.md) |
| Cycle counting | Schedule and assign counts, enter physical quantities on mobile, resume saved work, review current-stock adjustments and post with the correct role. | [Cycle counting](cycle-counting.md) |
| Delivery receiving | Review multiple PO lines together, attach actual certificate files, and recover an uncertain submission using its original request ID. | [Receiving and supplier follow-up](receiving-supplier-followup.md) |
| Supplier follow-up | Preserve requested dates while recording acknowledgments, confirmed dates, owners and follow-up deadlines; overdue follow-ups enter the inbox. | [Receiving and supplier follow-up](receiving-supplier-followup.md) |
| Calendar and material planning | Forecast against work-center calendars and known material supply, with explicit unknowns instead of unsupported arrival promises. | [Planning and job history](planning-job-history.md) |
| Job history | Browse filtered, chronological business events and open their source records, including work-order-filtered stock movements. | [Planning and job history](planning-job-history.md) |

## Release requirements

Alembic migrations 099 and 100 follow 098. Apply database/API changes before the
website; see the [deployment runbook](../DEPLOYMENT_RUNBOOK.md). New import and
delivery tables are private to the backend, with PostgreSQL RLS and revoked Data API
table/sequence grants. PostgreSQL CI verifies migrations 095–100 through repeated
upgrade/downgrade cycles, including populated import and receiving records.

No new feature environment variables are required. Existing database, signing and
document-storage configuration is reused. Vercel setup and production deployment
remain pending separately; implementation and local screenshots do not indicate a
production release.
