# Operational Action Inbox

The Action Inbox now starts with permission-scoped operational issues, shared owners and next actions. Existing AI recommendations, setup checks and master-data checks remain in their own section. Operational issues never use the browser's category-dismissal preference.

## Live scope

| Source | Included records | Resolve/review destination |
|---|---|---|
| Late work orders | Released, in progress or held; due before the active company's local day. Completed/cancelled/closed/deleted jobs are excluded. | The affected work order |
| Work-order blockers | Open or acknowledged blockers on active jobs, including operator-reported missing material. | The affected work order |
| Low stock | Active, nondeleted parts below their reorder point, using active on-hand inventory; held/rejected inventory is explicitly included, matching the existing stock signal. This is not an available-to-promise calculation. | Warehouse low-stock view |
| Quality | Open, under-review or pending-disposition NCRs; closed/void/deleted records are excluded. | The selected NCR investigation |
| Overdue purchasing | Sent/partially received orders with an open, unreceived line; line required date takes precedence, then expected/header required date. | The selected purchase order |
| Projected MRP shortage | Unprocessed order/manufacture/expedite recommendations in the latest completed company run. The card explicitly identifies a planning snapshot. Failed/new incomplete runs do not replace it. | Selected MRP run/action |

MRP supply drafts remain visible as follow-up work. A draft does not establish that material is available. Reviewed recommendations disappear through the existing MRP workflow; a new completed run supplies new issue identities.

## Ownership and triage

Assignment and a 500-character next-action note are shared across authorized users. Blocker and NCR owners use the existing source record's `assigned_to` field. Assignment changes and inbox state changes are audited; blocker acknowledgement reuses its existing service and audit behavior. Acknowledgement keeps the underlying issue in the active queue.

A 24-hour snooze is available in the UI, with a separate Snoozed view and Return to active action. The API accepts a bounded 0–168-hour duration. Snooze and acknowledgement apply only to a fingerprint of the current source occurrence; new source records and changed source facts return to attention without losing an existing owner's next-action plan. Source resolution removes the issue from the next live result. There is no generic Resolve or Dismiss action that hides a business problem.

The view supports Everyone, Mine, Unassigned, search and 20-item display pages. A visible, unedited inbox refreshes every minute. Each category is bounded at 1,000 records, with explicit partial-scope warnings if exceeded; counts describe loaded issues. The assignee roster is shared once per response rather than repeated on every item. Long source descriptions are excerpts; source links expose their full records.

## Authorization and persistence

Reads use the authenticated active-company dependency and both source and joined-record tenant predicates. Customized company role permissions are honored. Each source requires its module's view permission; triage also requires the matching write permission and the existing source's authorized management roles. Operator/viewer access does not confer assignment or acknowledgement authority. Read-only company contexts cannot mutate state. Eligible assignees must be active, in the same company and able to view that workflow; only minimal identity fields are returned.

PATCH checks both expected state version and source occurrence before saving. PostgreSQL transaction advisory locks serialize first-state creation and updates, and the company/source unique constraint is a final safeguard. These are implementation safeguards, not a claim that production concurrency has been load-tested.

[Migration 092](../../backend/alembic/versions/092_operational_inbox_state.py), following `091_user_workspaces`, creates the state table and indexes. It enables PostgreSQL RLS and revokes table/sequence privileges from PUBLIC and available `anon`/`authenticated` roles. The server uses the ERP's custom authentication; the migration does not invent an `auth.uid()` policy. Standard authenticated Data API roles have no access to the new table.

## Acceptance evidence

- [Backend source/permission/state tests](../../backend/tests/api/test_operations_inbox.py), [migration tests](../../backend/tests/test_operations_inbox_migration.py) and the existing blocker outcome slice: **42 passed**. This includes tenant isolation, foreign/inactive/ineligible owners, read-only context, custom permissions, stale writes, recurrence, snooze expiry, MRP snapshot/draft behavior and source resolution. SQLite upgrade/downgrade preserves preceding company/user rows; PostgreSQL DDL and RLS statements compile. A local PostgreSQL runtime was unavailable, so no live PostgreSQL migration claim is made.
- [Existing inbox tests](../../frontend/src/pages/ActionInbox.test.tsx) plus [operational interaction tests](../../frontend/src/pages/ActionInbox.operations.test.tsx): **14 passed**. Tests cover shared assignment, duplicate-submit protection, retained failed form input, stale-result controls, pagination, Mine/Unassigned, acknowledgement, snooze recovery and late responses after a company switch.
- TypeScript, targeted ESLint, mypy, Flake8, Black/isort and diff checks passed.
- Synthetic Chromium on localhost ports 8003/5176 verified assignment, Mine, acknowledgement remaining visible, snooze and return to active. At **390 × 844**, document width remained **390 px**, with no page runtime errors. [Assignment dialog](screenshots/inbox-mobile-assignment-viewport.jpg), [owned action](screenshots/inbox-mobile-owned-action.jpg). Captures use local synthetic records; no production records were changed.

The implementation lives in [ActionInbox.tsx](../../frontend/src/pages/ActionInbox.tsx), [operations_inbox_service.py](../../backend/app/services/operations_inbox_service.py), [endpoint](../../backend/app/api/endpoints/operations_inbox.py), [schema](../../backend/app/schemas/operations_inbox.py) and [state model](../../backend/app/models/operations_inbox.py).
