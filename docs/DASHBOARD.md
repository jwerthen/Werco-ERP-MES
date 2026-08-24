# Manager Dashboard (command cockpit)

The authenticated landing page (`/`) — a single-screen "command cockpit" giving managers a
holistic, low-scroll view of live shop activity, staffing, and job progress. Source:
[`frontend/src/pages/Dashboard.tsx`](../frontend/src/pages/Dashboard.tsx).

This is the interactive manager view. It is **not** the unattended TV board (`/wallboard`, see
[`docs/WALLBOARD.md`](WALLBOARD.md)) or the operator station (`/shop-floor/operations?kiosk=1`, see
[`docs/KIOSK.md`](KIOSK.md)).

## Layout

Top to bottom, all in one tight vertical stack:

1. **Header** — title, live "Updated" pulse, manual refresh, Shop Floor link.
2. **Setup nudge** (`SetupNudge`) — **admins only**, dismissible "Finish setup — N% complete" banner
   that deep-links to `/setup`. Reads `GET /setup/health` for the live progress; hidden once setup is
   100% complete or the admin dismisses it (persisted in `localStorage`). Renders for no other role.
3. **Alert chips** — up to four clickable chips (overdue WOs, open NCRs, calibration due, low stock).
   Hidden when there are none.
4. **KPI strip** — one compact 10-tile row (Active WO, Signed In, Checked In, Due Today, Overdue,
   Idle, Calibration Due, Low Stock, Done Today, Open NCRs).
5. **Cockpit grid** — the four live panels, co-visible at once. On wide screens they sit in a 12-col
   grid as two rows (Capacity + Live Activity, then Work Center Status + Presence); each panel caps
   its height and scrolls internally so the page doesn't grow:
   - **Capacity Overview** — per-work-center rows: utilization bar + a 7-day load heatmap (cells link
     to `/scheduling`).
   - **Live Shop Activity** — one compact two-line row per active time-clock assignment, grouped
     by work center. Line 1 is the operator — the short `display_name` ("First L.") the backend
     supplies on the `active_assignments` user payload (fallback: derived from the full name, then
     the employee id), role badge, elapsed time; line 2 is the job — WO link, part · op, progress
     bar + qty. That progress figure is **operation-scoped**, not work-order-scoped — see below.
   - **Work Center Status** — one row per station (status, active/queue counts, People count).
   - **Signed In Right Now** — live presence (on-the-job users as chips, idle users as rows).
6. **Recent Completions** — latest completed operations.

## Operator de-duplication (deliberate)

The same clocked-in operator could appear in three panels. To keep the cockpit compact **without
losing data**, each operator's live job is rendered **once** — in **Live Shop Activity** (the
canonical view, carrying WO, operation, progress, elapsed). The other panels reference it instead of
repeating it:

- **Work Center Status** shows only the **People count** (no nested per-person list). The count is a
  button that scrolls to that station's group in Live Shop Activity.
- **Signed In Right Now** shows on-the-job users as **name chips** (each scrolls to that operator's
  assignment row); only **idle** users (signed in, not clocked into work) render as full rows. An
  operator can hold **several** open assignments at once (crew-station multi-job clock-in), so
  repeated clicks on their chip **cycle through each assignment** in turn, and the chip tooltip
  gains "· N jobs".

Cross-links are keyed on **stable ids** — `work_center.id` (`#wc-live-<id>`) and `time_entry_id`
(`#assign-<id>`) — never on display names. Detail fields that don't fit the compact row (full name +
employee id, entry type, started time, due date, customer, priority, part name) are preserved in the
row's hover tooltip.

Regression coverage: [`frontend/src/pages/Dashboard.dedup.test.tsx`](../frontend/src/pages/Dashboard.dedup.test.tsx).

## Progress is operation-scoped (deliberate)

A Live Shop Activity row **is** one assignment to one operation, so **both halves** of its
`complete/ordered` fraction come from that **operation** — never from the work order. A running
laser nest reads **"0/2"**: its own sheet runs, the same fraction Dispatch, the WO routing table and
Shop Floor Operations already print for it. Not "0/102", which is the sheet total of all 21 nests on
the job.

The denominator is the server's one target rule (`operation_target_quantity` in
`backend/app/services/work_order_state_service.py`): an operation that declares its own
`component_quantity` is targeted at that — a laser nest's planned runs, a batch/pool line's piece
count — and only an operation without one inherits the work order's `quantity_ordered`.
`GET /shop-floor/dashboard` now resolves that rule per assignment and sends the answer as
`active_assignments[].operation.quantity_ordered`, with `component_quantity` beside it so the shared
client mirror (`frontend/src/utils/operationQuantity.ts`) can resolve the same rule, and for plain
payload parity with the three sibling operation payloads that already ship it.

The two keys are **optional on the client** because the SPA (Vercel) and the API (Railway) deploy
independently, so a frontend build that reads them can be live against a backend that does not send
them yet; the client mirror degrades that window to the work-order figure. That is the *only* way
they go missing — **not** the ETag cache, which is an in-memory `Map` starting empty on every page
load and so can never hold a payload older than the bundle reading it. Worth stating, because
"the cache can't hold a stale shape" is true and makes the fallback look like dead code: delete it
and the next frontend-ahead-of-backend deploy renders "0/102" again with every test still passing.

**Why this is written down:** the row used to divide the *operation's* `quantity_complete` by the
*work order's* `quantity_ordered`, and a nest sitting at 0 of 2 rendered as **"0/102 (0%)"** —
neither the nest's fraction nor the job's (38/102). Mixed scope was the defect, so the two halves
now fall back **together**: an assignment carrying no operation (indirect/setup labor) prints the
work order's own pair, never one half of each.

Two consequences worth keeping:

- The whole-job figure is still available, but only in the row's hover tooltip and **explicitly
  labelled** ("Work order total 38/102"), so it can never be misread as this operation's progress.
- The percentage is **unclamped** while the bar is clamped (the `ShopFloorSimple` convention).
  Over-completion is reachable at one nest's scale, and a clamped "3/2 (100%)" would contradict the
  raw fraction printed beside it.

## Reusable cockpit primitives

The compact building blocks introduced here are shared in
[`frontend/src/components/cockpit/`](../frontend/src/components/cockpit/) so other pages get the same
instrument-panel treatment without reinventing it:

- **`MiniStat`** — compact KPI tile (small icon chip + uppercase label + tabular value). Renders as a
  static tile, a `<Link>` (`href`), or a filter `<button>` (`onClick` + `active`). Replaces the bulky
  big-stat-icon KPI cards; wrap a row of them in **`MiniStatStrip`** (responsive 2/3/5-up by default).
- **`CockpitPanel`** — a `card-compact` panel with a tight header, a capped internally-scrolling body
  (caps apply at lg+ and drop on mobile), and an optional footer count, for laying capped panels
  side-by-side instead of stacking unbounded full-width sections.

These were rolled out to the KPI strips of the list/ops pages in a follow-on pass; the broader
page-by-page overhaul backlog (tiers + sequencing) lives in the project audit notes.

## Responsive

- **≥1280px (xl)** — 7+5 / 7+5 grid; all four panels on screen at once; panel bodies cap at
  `clamp(280px, 38vh, 440px)` and scroll internally.
- **1024–1279px (lg)** — two columns, same internal caps.
- **<1024px / mobile** — single column; height caps are **dropped** so panels grow naturally and no
  data is trapped behind a nested scroll.

## Data and refresh

The panels are driven by the ETag-cached `GET /shop-floor/dashboard` (summary, work centers, active
assignments, signed-in users, recent completions) plus `GET /scheduling/capacity-heatmap`, with the
alert widgets reading `/quality/summary`, `/calibration/equipment/due-soon`, and `/inventory/low-stock`.
Data refreshes on a 30s poll and on WebSocket pushes (`dashboard_update` / `shop_floor_update` /
`quality_alert` / etc.); live presence is WebSocket-driven. See the `/shop-floor/dashboard` caching +
bounded-reconcile notes in [`docs/API.md`](API.md). The 2026-06 redesign was **presentation-only** —
the endpoint payloads are unchanged. Two later additive payload changes: (1) the
`active_assignments` **user** object also carries `display_name` — the "First L." short form (the
wallboard's `operator_display_name` helper) — which Live Shop Activity rows display instead of the
employee id (full name + employee id stay in the payload and the row tooltip); (2) each
assignment's **operation** object also carries `quantity_ordered` (the server-resolved
`operation_target_quantity`; `null` when the time entry has no operation) and `component_quantity` —
see "Progress is operation-scoped" above.
