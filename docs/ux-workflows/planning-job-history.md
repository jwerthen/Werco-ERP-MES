# Material planning and job history

Scheduling and Work Order Detail now share a read-only material-readiness estimate. Completion and capacity forecasts use the existing work-center working calendars, including weekday hours and holiday/shutdown overrides. No customer promise, inventory reservation or purchase-order date is written by a forecast or preview.

## Scheduling and forecasts

The existing **Preview Visible Unscheduled**, **Preview Selected Earliest**, and **Preview Shift Dates** actions include each job's material-ready date, requirement coverage, source quantities and unresolved shortages. Earliest scheduling waits for both known material availability and center capacity. A shift that precedes known readiness is blocked. Unknown material coverage blocks the affected job and does not invent an arrival date. The reviewed plan keeps exact visible selection, per-job outcomes and explicit apply/recovery behavior.

Material demand follows the existing BOM/phantom/routing resolver. Open material ties replace the corresponding BOM demand, rather than adding it twice. Recorded ISSUE/RETURN ledger quantities reduce remaining demand; the `qty_consumed` allocation cache is not authoritative. All three work-order ledger reference forms are included through `work_order_ledger_filter`.

Coverage uses active, consumable, unexpired stock, subtracts existing allocated quantities, and clamps negative balances to zero usable supply. A pinned tie can use only its pinned lot. Incoming supply uses the outstanding quantity on issued/partially received, non-deleted PO lines, and only trusts a supplier date when the PO also has an acknowledgment timestamp. Requested dates and legacy estimates are not confirmed arrivals. Overdue confirmed arrivals, unconfirmed supply, incompatible units, unresolved definitions and insufficient quantities remain explicit unknowns. Stock expiring before a projected start is not accepted as coverage.

A shared virtual pool prevents the selected jobs from each claiming the same stock or incoming line quantity. This is a planning estimate in selected queue order, not a reservation; work outside the selected set may consume that supply. Jobs with incomplete coverage can conservatively retain partial coverage in the preview pool. General allocated stock is never automatically reclaimed as a job's own supply. Jobs without any recorded material requirement retain their existing capacity-only scheduling behavior with an explicit warning that material readiness has not been verified.

The signed scheduling token includes the material evidence, resolved requirements and existing calendar/schedule snapshot. Apply rechecks current permissions and tenant scope, locks the relevant work-order, operation, PO, BOM, part, allocation and inventory dependencies, and rejects changed evidence with 409 before writing a different plan. Existing exact-result retry behavior remains. Manual, earliest, operation and finite scheduling paths use the same readiness guard, preserving the legacy no-requirement case.

Work Order Detail's **Completion forecast** loads on demand for admins, managers and supervisors, matching the existing analytics endpoint. It shows a date only when the calendar, time estimates and material evidence support one, with assumptions beside the result. Queue delay remains a historical heuristic and excludes the job's own operations. Completed operation dates come from recorded actual dates. Missing estimates or an unavailable working calendar leave future dates unknown. The panel retains the last result with a stale warning after a failed refresh.

Capacity forecasting sums each actual calendar date in the forecast week. Unconfigured centers retain the established calendar default; there is no newly implied Monday–Friday policy. Remaining committed workload is still spread evenly over the forecast window, now stated in the UI. A closed week with committed work is explicitly overloaded rather than presented as spare capacity.

## Chronological job history

**Job timeline** loads only when opened and is available to the same authenticated company-scoped readers as Work Order Detail. It combines job creation, recorded operation starts/finishes, labor clock entries, immutable production receipt totals, inventory ledger movements, process-step evidence, blockers, NCR/FAI creation, and safely scoped audit changes. Production receipt values are labeled recorded totals; the panel does not reconstruct additive deltas from cumulative totals.

Business records, audit evidence and supplemental quality events are visibly distinguished. The supplemental NCR/FAI update events are best-effort telemetry, not substitutes for compliance audit records. Audit rendering exposes only the action and approved changed-field names. It omits raw old/new values, private payloads, labor rates, costs, IP addresses and authentication context. Historical foreign or unavailable actors remain unknown. Deleted jobs cannot be queried; deleted NCR detail is withheld while its scoped void audit can remain in the job's history.

Categories, a Central-time start date, and actor selection filter the timeline. Filters are preserved in the URL. Each underlying source query is bounded to one page plus one row, and stable timestamp/source/ID cursors page older events without loading the complete history. Source links open the relevant job operation, quality record or work-order-filtered stock movement panel. The stock-movement filter is visible and can be cleared from both UI and URL. Refresh failures retain visible last-good events with a stale warning.

The history is a view of existing records and their available timestamps. It is not a complete immutable snapshot of every historical field edit. Source IDs and evidence labels remain visible so users can distinguish that limitation.

## API and permissions

| Endpoint | Contract |
| --- | --- |
| `GET /api/v1/work-orders/{id}/timeline` | Existing authenticated job-reader and active-company scope. Optional `category` (`job`, `production`, `labor`, `material`, `quality`, `blocker`, `audit`), `actor_id`, `start_at`, `end_at`, opaque `cursor` (max 1000 characters), `limit` 1–100 (default 30). Returns `items`, `next_cursor`, `coverage`. Foreign/deleted jobs return 404; invalid filters/cursors return 422. |
| `GET /api/v1/analytics/predict/delivery/{id}` | Existing admin/manager/supervisor scope. Adds `materials`, `warnings`, `basis`; future completion/operation dates and on-time probability can be null when unknown. |
| `GET /api/v1/analytics/predict/capacity` | Existing admin/manager/supervisor scope and shape; available hours now follow working calendars. |
| `POST /api/v1/scheduling/impact-preview` | Existing scheduling writer role gate. Read-only signed review; per-job `materials` augments the existing response. |
| `POST /api/v1/scheduling/impact-apply` | Existing role, company, pending/replay and stale-plan rules; material dependency changes also invalidate review. |

No new tables, migration, environment variables or permission strings are required for these two features.

## Validation

- 225 focused backend tests passed across scheduling, sequential operations, dispatch run order, working calendars, signed impacts, finite scheduler tenancy, prediction tenancy, material planning and timeline. Cases cover confirmed arrivals, shared quantities, held/expired/negative/allocated stock, unknown supply, tie/ledger precedence, stale material evidence, calendar shutdowns, direct-path guards, all three ledger reference shapes, actor/tenant privacy and stable pagination.
- 201 frontend tests across 23 suites passed for the new panels and existing Scheduling, Work Order Detail, Analytics and stock movements. Source and test TypeScript checks and scoped zero-warning ESLint passed. The final mobile nest-action wrapping adjustment passed all 165 Work Order Detail tests and lint again.
- Black check passed on all 14 owned Python source/test files; scoped isort, Flake8 and mypy checks passed during implementation.
- Local Chromium uses synthetic records on API8007/frontend5180. Browser screenshots and final integrated checks are recorded below and in the package validation. No production data or outbound email is involved.
- Isolated service/API tests exercise SQLite. These results do not establish PostgreSQL lock-contention behavior; the parent integration preflight separately checks PostgreSQL migrations. Material coverage is not a reservation guarantee.

### Browser evidence

Synthetic `WF-PLAN-READY` has ten required blanks, an authoritative issue of one,
four usable blanks and six supplier-confirmed incoming blanks dated September 11.
Its center works four hours Friday, closes on weekends, and has a September 14
shutdown. The review schedules its twelve-hour operation September 11–15 and calls
out the four-day delivery risk. `WF-PLAN-UNKNOWN` has no confirmed supplier arrival
and is blocked with no proposed finish. The screenshot was captured before apply;
no schedule was written during this browser check.

The separate completion forecast includes another queued job and therefore shows
September 16 with its queue-based assumption. The unknown-supply forecast remains
unknown. Mobile timeline width and scroll width both measured 356px within the
390px viewport. The panels were visually inspected; this is not a claim that all
older Work Order Detail content has been redesigned. The existing nest actions
now wrap at that width. The material history link opened the exact work-order
Stock Movements URL and displayed its active filter. No browser page errors were
observed.

- [Material-aware scheduling review](screenshots/material-aware-schedule-preview.png)
- [Job history and forecast desktop](screenshots/job-planning-timeline-desktop.png)
- [Job timeline mobile](screenshots/job-timeline-mobile.png)
- [Completion forecast mobile](screenshots/job-planning-mobile.png)
- [Unknown supply mobile](screenshots/job-planning-unknown-mobile.png)
