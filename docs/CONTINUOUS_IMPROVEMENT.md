# Continuous Improvement

Open **Production → Continuous Improvement** or search for it with the command
palette. The section tracks lean manufacturing suggestions from submission to
implementation. Everyone signed into the company's ERP can view the register;
managers and above can submit, assign, edit, change status, and add comments.

## Submit a suggestion

Describe one concrete problem and one proposed improvement. Select a category,
set a priority, and identify the affected area. Add the expected benefit, an
accountable owner, and a target date when known. New submissions enter **New**.

Categories cover Poka-yoke / mistake proofing, 5S / workplace organization,
standard work, flow and layout, quality, safety and ergonomics, setup reduction,
equipment, inventory, and other improvements.

For example, a Poka-yoke suggestion might describe a fixture that accepts a part
in either orientation, propose an asymmetric locating pin, and state the intended
benefit as eliminating reversed-part assembly. In the expected benefit, record
the current defect count, observation period, and target when available.

## Review and follow through

| Status | Meaning |
|---|---|
| New | Submitted and awaiting initial review |
| Under Review | Evaluating the problem, benefit, feasibility, or trial |
| Approved | Accepted and ready to schedule |
| In Progress | Implementation or a trial is underway |
| Implemented | Change is in use and implementation results are recorded |
| On Hold | Waiting on a stated dependency or decision |
| Declined | Closed with a documented reason |

Status counts make the review backlog and completed work visible. Use search and
the status, category, priority, and owner filters to focus a meeting or follow up
on an area. Owners and target dates make responsibility explicit.

Changing to **Implemented** requires implementation notes. Record what changed,
when it was put into use, what was measured before and after, and any process-sheet
or training updates. A completion note is evidence recorded by the manager; the
system does not independently verify claimed savings or effectiveness.

Putting a suggestion **On Hold**, declining it, or reopening an implemented item
requires a reason. Returning an existing suggestion to **New** is disallowed so
the initial submission queue keeps its meaning. Managers can otherwise move
between statuses as needed for small improvements and larger trials.

For a useful operating cadence, review new items weekly, assign an owner to each
accepted idea, and use comments for trial observations and follow-up checks. If
an implemented change is ineffective, reopen it with an explanation; the previous
implementation remains visible in its activity history.

## Timestamps and history

The server timestamps submission, updates, first review, implementation, and each
activity entry. The interface displays date and time in the ERP's Central time
zone. Target dates are calendar dates rather than times of day.

History records who submitted or changed an item, old and new field values, status
changes, and comments. There is no delete or history-edit action. Mutations also
write to the central audit log in the same transaction. Stale edits are rejected
so simultaneous reviewers cannot silently overwrite one another.

Company boundaries apply to lists, detail records, activity, status counts, and
owner choices. Owners must be active managers or above in that company. Platform
read-only company contexts cannot modify records, and kiosk credentials cannot
access this section.

## API and deployment

The authenticated API prefix is `/api/v1/continuous-improvement`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/metadata` | Category, status, priority, owner choices and write capability |
| GET | `/` | Filtered, paginated suggestions and company-wide status counts |
| POST | `/` | Submit a new suggestion |
| GET | `/{id}` | Suggestion and activity history |
| PATCH | `/{id}` | Update fields or status with `expected_version` |
| POST | `/{id}/comments` | Append a comment with `expected_version` |

Apply Alembic revision `111_continuous_improvement` with the normal backend
deployment (`alembic upgrade head`) before serving the new frontend. It adds the
suggestion and activity tables, indexes, constraints, and Postgres RLS/grant
hardening. The app's authenticated backend owns database access; the new tables
are not exposed through Supabase's public Data API.

No email alerts, automatic reminders, attachments, or automatic changes to work
orders/process sheets are performed by this module.
