# Cycle counting in the warehouse

Warehouse → Inventory → Cycle Counts provides assigned sessions, physical count
entry, reload recovery, and manager review before stock adjustments. A session
enrolls the existing active, nonzero inventory rows in one location, warehouse, or
part scope, including negative balances. It does not create new inventory lots.

Managers, supervisors and admins schedule and assign sessions. Every working role
can start a session and record quantities; viewers can read the history. Starting
a session assigns it to the person who starts it. Managers can reassign it explicitly.
Assignments always resolve to an active user in the current company.

The count form accepts zero and positive finite quantities, preserves notes, and
offers Save & next for the next uncounted row. Each save is durable and audited;
it does not change stock. The browser submits the last observed `counted_at` value,
so another counter's newer observation produces a 409 instead of being overwritten.
Session selection, status, owner filter and pagination are URL state.

## Reviewed adjustments

The warehouse UI uses `POST /inventory/cycle-counts/{id}/review` and
`POST /inventory/cycle-counts/{id}/post-reviewed`. All rows must be counted and
available before review. Review shows the enrollment balance, current on-hand,
physical quantity and the adjustment that will actually post. Stock movements
since enrollment are called out, and the reviewer explicitly acknowledges the
quantities. Recount before posting if an observation no longer describes the shelf.

A ten-minute signed review binds company, reviewer, session, count observations,
and stock quantities/costs. Posting locks the count and stock rows, checks the same
snapshot, then calls the existing audited stock ledger completion path. A changed
count or stock row refuses posting with 409 and requires a new review. Even a
zero enrollment variance is compared to current stock on this reviewed path.
Completion is terminal; replay cannot append another adjustment.

The older `/complete` API remains compatible, including its optional
`apply_adjustments=false` mode and legacy enrollment-variance selection. New UI
callers always use the stricter reviewed path. No new table or environment variable
is needed for cycle counting.

## Verification

- Backend tests cover assignment and tenant boundaries, operator versus reviewer
  permissions, zero counts, incomplete reviews, competing observations, stale stock,
  current-basis posting and duplicate completion.
- UI tests cover operator entry, viewer controls, stale review recovery and scheduling.
- The browser acceptance test creates real synthetic stock, saves a mobile count,
  reloads, reviews the difference, posts once, reloads again and verifies the ledger.
- [Mobile count entry](screenshots/cycle-count-mobile.png)
- [Adjustment review](screenshots/cycle-count-review-desktop.png)
