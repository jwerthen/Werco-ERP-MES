# Scheduling impact preview — package 5

Implemented in the Scheduling date actions. **Preview Shift Dates** and **Preview Selected Earliest** use only selected rows that are currently visible. **Preview Visible Unscheduled** replaces the immediate all-jobs shortcut and uses the filtered visible queue. Requests above 50 jobs require a smaller selection; nothing is silently truncated.

The review shows the exact operation/center, before and after dates, status transitions, job completion and due-date estimates, named blocked/skipped jobs, and affected daily overloads with neighboring job numbers. Shift preserves the gaps and durations of all scheduled remaining operations. Completed operation history stays unchanged. Earliest planning simulates the selected jobs in queue order, accounting for earlier proposed jobs as well as existing load. The capacity and projection functions are shared with the existing scheduler.

## Apply and recovery contract

- Preview is read-only: no schedules, audit records, or preview records are written.
- A signed token expires after 10 minutes and is bound to the authenticated user and company. Both endpoints enforce planner roles. No migration is needed.
- Apply locks relevant company work orders, centers, and operations in consistent order, re-reads their state, and validates the fingerprint before writing. Dependencies include currently finished/hidden operations at those centers, because reopening them can consume capacity without changing their center assignment.
- Reviewed operation dates and audit records commit together. A changed job, operation, shared schedule, or capacity rejects the entire applicable plan with HTTP 409; it does not calculate and apply a replacement.
- Repeating the same token against its exact applied state returns `already_applied` without another shift or audit. An uncertain response offers **Retry reviewed plan** with the same token. A stale/expired plan requires an explicit new preview and review.
- Preview/apply refs guard duplicate events synchronously. The dialog cannot close during an in-flight request; apply stays pending through schedule reconciliation. Named results remain on the page. Applied selections clear; blocked/skipped and hidden selections survive.
- Capacity cache and the affected work-order, center, and dashboard realtime channels refresh after commit. Refresh failure remains visible through the existing last-good schedule error state.

## Validation

- New backend API regressions: **19 passed**. They cover read-only preview, exact downstream dates, completed history, selected-job capacity, due-date risk, affected neighboring jobs including unchanged total overload, malformed/oversized scope, stale dependencies with no partial apply, tenant/actor/role restrictions, expired/tampered tokens, exact replay, and blocked subsets.
- Existing scheduling API regressions: **17 passed**; scheduling service tenant regressions: **3 passed**.
- New Scheduling impact UI regressions: **6 passed**, plus existing scheduling run-order/selection/pending/calendar regressions: **9 passed**, under **UTC, America/Chicago, and Pacific/Auckland**. These assert exact date labels, visible selection scope, preview-before-write, pending duplicate guards through reconciliation, stale-plan explicit re-review, same-token uncertain retry, and preserved blocked/hidden selections.
- Frontend source and test TypeScript checks and scoped zero-warning ESLint passed. Backend scoped Black/isort checks and mypy passed.

## Practical limits

This retains the existing daily-hours scheduling model (including its operation-duration estimate), not a new shift, holiday, material, or staffing solver. Unknown remaining dates remain unknown rather than implying an on-time finish. Changes at a shared center can invalidate a preview conservatively. The tests use isolated SQLite fixtures; actual concurrent PostgreSQL lock behavior and browser layout are not claimed by this validation record. No production data was changed.
