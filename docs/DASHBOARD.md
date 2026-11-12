# Manager Dashboard (command cockpit)

The authenticated landing page (`/`) — a single-screen "command cockpit" giving managers a
holistic, low-scroll view of live shop activity, staffing, and job progress. Source:
[`frontend/src/pages/Dashboard.tsx`](../frontend/src/pages/Dashboard.tsx).

This is the interactive manager view. It is **not** the unattended TV board (`/wallboard`, see
[`docs/WALLBOARD.md`](WALLBOARD.md)) or the operator station (`/shop-floor/operations?kiosk=1`, see
[`docs/KIOSK.md`](KIOSK.md)). An `operator` who opens `/` is redirected to that station
(`App.tsx`), so operators never see this page.

Each user can choose which sections appear, their order and (for panels) their width — see
[Customizing the dashboard](#customizing-the-dashboard). Everything in **Layout** below is the
**default** ("classic") layout, which is what every role sees until they save a layout of their own.

## Layout

Top to bottom, all in one tight vertical stack. Items 1–3 are page chrome and cannot be hidden;
items 4–9 are widgets (their widget ids are in the [catalog](#widget-catalog)).

1. **Header** — title, "Updated" pulse and "Checked HH:MM" / "Refreshing dashboard…" metadata,
   manual refresh, **Customize**, which turns the page into [edit mode](#customizing-the-dashboard)
   (hidden when the API predates the feature — see
   [Loading the saved layout](#loading-the-saved-layout)), and the **Shop Floor** link (shown only
   when the viewer can open `/shop-floor`).
2. **System banners** — not widgets, always evaluated:
   - **Shop-data error** (`role="alert"`, with **Retry**) when the `GET /shop-floor/dashboard`
     heartbeat fails but earlier data is on screen ("Could not refresh shop data…" or, for an ETag
     cache fallback, "Connection interrupted. Showing last verified shop data."). With no data at
     all the page shows the full **Error loading dashboard** state with Retry instead.
   - **Widget sources** (`role="status"`) — one line per data source a *visible* widget needs that
     failed to refresh, by its label: "Quality: Could not refresh. No verified data is available."
     (or "Showing last verified data." when an earlier result is kept), plus a **Retry unavailable
     sections** button. A source that answered **403** is never listed here; its widget says "Not
     available for your role" instead.
   - **Saved layout** (`role="status"`) — "Your saved dashboard layout couldn't be loaded; showing the
     default." with a **Reload layout** button.
3. **Empty dashboard** — when the viewer's layout resolves to no widget at all (they removed
   everything, or none of their saved widgets is available to them), an empty state "Your dashboard
   is empty" with a **Customize dashboard** action replaces items 4–9. A layout that still holds a
   banner which happens to render nothing (no exception, no brief) is not "empty": with banners but
   no KPI tile or panel, the page shows a standing note "Only alerts and banners are on your
   dashboard. They appear here when something needs attention." with its own **Customize dashboard**
   button. Both actions enter edit mode, exactly like the header button.
4. **Needs attention** (`attention`) — the first exception, in priority order overdue work orders →
   open NCRs → calibration due → low stock, with a **Review affected records** button (hidden when
   the viewer cannot open its target). Its subtitle reads "Based on last verified shop data" while the
   shop-data error banner is up. Renders nothing when there is no exception.
5. **Setup nudge** (`setup_nudge`, `SetupNudge`) — **admins only**, dismissible "Finish setup — N%
   complete" banner that deep-links to `/setup`. Reads `GET /setup/health` for the live progress;
   hidden once setup is 100% complete or the admin dismisses it (persisted in `localStorage`).
   Renders for no other role.
6. **Morning brief** (`morning_brief`, `MorningBriefBanner`) — today's AI morning brief when one is
   pending in the Action Inbox (reads `GET /ai/recommendations?status=pending&limit=20` itself);
   renders nothing otherwise.
7. **Alert chips** (`alert_chips`) — up to four chips (overdue WOs, open NCRs, calibration due, low
   stock), the same list the Needs attention banner takes its first item from. Hidden when there are
   none. A chip whose target the viewer cannot open renders as plain text.
8. **KPI strip** — one compact 10-tile row, in this order: "Overdue"; "Signed In, Idle"; "Active
   Work Orders"; "Signed In Now"; "Checked In Now"; "Due Today"; "Calibration Due"; "Low Stock
   Items"; "Operations Completed Today"; "Open NCRs". Overdue, Active Work Orders, Checked In Now,
   Due Today, Calibration Due, Low Stock Items and Open NCRs link to their list pages when the viewer
   can open them; Signed In, Idle and Operations Completed Today scroll to a panel on the page.
9. **Cockpit grid** — the five panels in one 12-column grid. On wide screens: Capacity Overview
   (7) + Live Shop Activity (5), then Work Center Status (7) + Signed In Right Now (5), then Recent
   Completions across the full width. Each panel caps its height and scrolls internally so the page
   doesn't grow:
   - **Capacity Overview** — per-work-center rows: utilization bar + a 7-day load heatmap (cells link
     to `/scheduling`).
   - **Live Shop Activity** — one compact two-line row per active time-clock assignment, grouped
     by work center. Line 1 is the operator — the short `display_name` ("First L.") the backend
     supplies on the `active_assignments` user payload (fallback: derived from the full name, then
     the employee id), role badge, elapsed time; line 2 is the job — WO link, part · op, progress
     bar + qty. That progress figure is **operation-scoped**, not work-order-scoped — see below.
     A row with an operation carries a **View operation →** button that opens the
     **operation-details modal** (`DashboardOperationDetails`, reads
     `GET /shop-floor/operations/{id}`).
   - **Work Center Status** — one row per station (status, active/queue counts, People count). Its
     **View All** link (to `/work-centers`) shows only for viewers holding `admin:settings`.
   - **Signed In Right Now** — live presence (on-the-job users as chips, idle users as rows).
   - **Recent Completions** — latest completed operations; keeps its own card and body cap (see
     Responsive).

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

Because each panel can now be hidden, every in-page cross-link checks that its target widget is on
screen and otherwise renders as plain, non-interactive text (see
[Cross-links degrade](#cross-links-degrade-when-their-target-is-hidden)).

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

- **≥1280px (xl)** — 12-column grid; the default layout is 7+5 / 7+5 with Recent Completions across
  all 12; panel bodies cap at `clamp(280px, 38vh, 440px)` and scroll internally (Recent Completions
  keeps its own `clamp(200px, 28vh, 360px)`).
- **1024–1279px (lg)** — two columns, same internal caps. `two_thirds` and `full` panels span both
  columns; every other width takes one.
- **<1024px / mobile** — single column; height caps are **dropped** so panels grow naturally and no
  data is trapped behind a nested scroll. In [edit mode](#what-edit-mode-shows) only, each panel body
  previews at 12rem (192 px) with a fade, so the page stays short enough to rearrange; placement
  depends only on order and width, so the preview still shows where each panel will sit.

Panel widths are a fixed vocabulary, mapped to static Tailwind classes in one place
(`PANEL_SIZE_CLASSES`, `components/dashboard/widgetTypes.ts`). The label is what the Move menu's
**Width** radios show:

| Width (`size`) | Width label | Classes |
|---|---|---|
| `third` | Third | `xl:col-span-4` |
| `narrow` | Narrow | `xl:col-span-5` |
| `half` | Half | `xl:col-span-6` |
| `wide` | Wide | `xl:col-span-7` |
| `two_thirds` | Two-thirds | `lg:col-span-2 xl:col-span-8` |
| `full` | Full width | `lg:col-span-2 xl:col-span-12` |

The grid places panels in saved order with no dense packing: a panel that does not fit in what is
left of a row starts the next row, leaving a gap, rather than an earlier gap being back-filled.

## Data and refresh

The default layout is driven by the ETag-cached `GET /shop-floor/dashboard` (summary, work centers,
active assignments, signed-in users, recent completions) plus `GET /scheduling/capacity-heatmap`,
with the alert widgets reading `/quality/summary`, `/calibration/equipment/due-soon`, and
`/inventory/low-stock`. Which of those are fetched depends on the widgets on screen — see
[Data fetching](#data-fetching). Data refreshes on a 30s poll and on WebSocket pushes
(`dashboard_update` / `work_order_update` / `shop_floor_update` / `quality_alert` / `notification`,
debounced 750 ms; pushes ship off — `REALTIME_BROADCAST_ENABLED` in
[`docs/ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md)). Both skip a hidden browser tab and
catch up with one load when it becomes visible again. See the `/shop-floor/dashboard` caching +
bounded-reconcile notes in [`docs/API.md`](API.md). The 2026-06 redesign was **presentation-only** —
the endpoint payloads are unchanged. Two later additive payload changes: (1) the
`active_assignments` **user** object also carries `display_name` — the "First L." short form (the
wallboard's `operator_display_name` helper) — which Live Shop Activity rows display instead of the
employee id (full name + employee id stay in the payload and the row tooltip); (2) each
assignment's **operation** object also carries `quantity_ordered` (the server-resolved
`operation_target_quantity`; `null` when the time entry has no operation) and `component_quantity` —
see "Progress is operation-scoped" above.

## Customizing the dashboard

**Customize** in the page header turns the live page into an in-place **edit mode**, in the style of
iOS / macOS widgets: the user drags KPI tiles and panels into a new order (or moves them from the
keyboard), removes them, adds widgets from a gallery and picks panel widths, then saves the whole
session with **Done**. Source: [`frontend/src/components/dashboard/editor/`](../frontend/src/components/dashboard/editor/)
(the editor), [`WidgetCell.tsx`](../frontend/src/components/dashboard/WidgetCell.tsx) (one tile or
panel, in both modes) and [`frontend/src/pages/Dashboard.tsx`](../frontend/src/pages/Dashboard.tsx);
the registry, layout logic, loaders and widgets are in
[`frontend/src/components/dashboard/`](../frontend/src/components/dashboard/); API contract in
[`docs/API.md` → Personal dashboard layout](API.md#personal-dashboard-layout).

### Entering edit mode

- **Entry points.** The header **Customize** button, the empty state's **Customize dashboard** action
  and the banners-only note's **Customize dashboard** button run the same handler. Pointing at or
  focusing the header button starts loading the editor's code ahead of the press.
- **When it is refused.** Customize is hidden when the API predates the feature, and disabled (its
  handler does nothing) while the saved layout loads, while the page body is the loading skeleton or
  the full **Error loading dashboard** state — edit mode arranges the cells, so it opens only over
  them — while a layout reload runs, while an entry is already under way, and while editing.
- **Read-only sessions** (`can_edit: false` — see [Persistence](#persistence)). Customize stays
  visible, described to screen readers as "Read-only session. The layout can't be changed." Pressing
  it shows the info toast "This session can't save a dashboard layout — read-only company view or an
  API/kiosk credential." and does not enter edit mode. A session that becomes read-only while
  editing (a re-read answers `can_edit: false`) leaves edit mode with the same toast.
- **A fresh read on every entry.** Customize re-reads `GET /dashboard/layout` before every entry (its
  icon spins and it is `aria-busy` meanwhile): a copy cached since the page loaded can be behind a
  save from another tab or device, and an edit session seeded from it would end in a **409** that
  discards its edits. The re-read has the same 3 s cap as the first load
  ([`LAYOUT_LOAD_TIMEOUT_MS`](#loading-the-saved-layout)), so the re-read can take up to 3 s (plus
  loading the editor's code the first time, which has no deadline of its own and which pointing at
  or focusing Customize starts early). If the re-read fails while a saved layout is on screen, edit
  mode opens on that layout; if the first load had failed too, Customize refuses with "Your saved dashboard layout couldn't be loaded. Try again in a
  moment." A re-read that answers `can_edit: false` gets the read-only toast.
- **The editor's code loads on demand.** If that chunk cannot be fetched (typically a 404 just after
  a deploy), the toast "The dashboard editor couldn't load. Reload the page and try again." appears,
  the page stays in view mode with no automatic reload, focus returns to Customize, and the next
  press tries the import again. If the session changed while it loaded, nothing happens.
- **On entry** the draft is seeded once from the saved layout (polls and pushes never reset it), focus
  moves to the edit bar's heading "Customizing dashboard", and the page announces "Customizing your
  dashboard. Drag widgets to rearrange them, or use each widget's Move button. Press Done to save."
  The header Customize button is disabled while editing; manual refresh and **Shop Floor** stay
  available.

### What edit mode shows

- **The edit bar**, fixed to the bottom of the viewport (above the bottom navigation below lg, right
  of the sidebar at lg+) but next after the page header in the tab order: the heading, an **Unsaved
  changes** tag while the draft differs from the saved layout, and **Add widgets · Undo · Reset to
  default · Cancel · Done**. Below sm, Undo shows its icon only and Reset reads "Reset" (the
  accessible names are unchanged). The bar also holds the failure alert, an always-mounted status
  line and the [notes about widgets it cannot show](#widgets-edit-mode-cannot-show).
- **Every KPI tile and panel** gets a dashed hairline outline, a square **−** (Remove) badge on its
  top-left corner and a **Move** button (a six-dot grip) inside its top-right corner — 24 px, 32 px
  on a coarse pointer. The chrome sits over the cell, so the grids keep their view-mode gaps and
  spans; in a KPI tile the label leaves room for the Move button. The KPI strip and the panel grid
  become lists ("Key metrics on your dashboard", "Panels on your dashboard").
- **Widget content stays mounted and live** — its polls and pushes continue — but is `inert`: no
  link, button, in-page cross-link or operation-details modal inside it can be focused or fired, and
  an invisible drag surface covers it. The banner zone is inert too; banners get no chrome.
- **Below lg** each panel body previews at 12rem (192 px) with a fade at the bottom (see
  [Responsive](#responsive)).
- **An empty zone** shows a dashed placeholder — "No key metrics on your dashboard." / "No panels on
  your dashboard." — whose **Add key metrics** / **Add panels** button opens the gallery at that
  section. The empty state and the banners-only note are not shown while editing.
- **Nothing remounts.** Each tile and panel renders through `WidgetCell` in both modes; edit mode
  adds its chrome beside the widget and toggles attributes and classes, so entering or leaving never
  remounts a widget: a panel keeps its scroll position, the Notifications list keeps its cache, and
  Setup progress and Morning brief do not refetch.
- **Focus scrolling stays clear of the chrome.** While editing, `scroll-padding` keeps a focused
  control out from under the sticky header (4rem) and the edit bar (measured from the bar's top
  edge, plus 8 px; 7rem until it is first measured, and re-measured when the bar or window resizes),
  the page gets matching bottom padding so its last row can scroll clear of the bar (less whatever
  its containers already pad below it: Layout's `<main>` reserves BottomNav's height below lg, and
  counting it twice left about 80 px of dead space under the last row on a phone), and scroll
  anchoring is off so a live reorder cannot make the browser jump the page. The previous values come
  back on exit.

### Dragging with a mouse or a finger

- **Lift.** With a mouse, pen or trackpad, press on a tile or panel (its body, or its Move button)
  and move 6 px. On touch, press and hold for 250 ms without moving more than 8 px; a quicker swipe
  scrolls the page as usual. Where the device supports it, a touch pickup gives a short vibration.
- **Live reflow.** The widget's own cell stays in the grid as the landing slot (a blue dashed
  outline, its content hidden but still holding its height), and a compact ghost card — the title
  and the width ("Key metric" for a tile), at most 480 × 280 px, keeping the point you grabbed —
  follows the pointer. When the pointer passes the midline of another widget the page renders the
  new order, so the **real CSS grid** reflows and the slot shows exactly where the widget will land;
  the widgets it displaces glide to their new places. Over a gutter or the empty end of a row, the
  widget goes after the last widget on that row to the left of the pointer. After each reorder the
  pointer must move 8 px before the next one, and a pointer held still never keeps reordering. KPI
  tiles move only within the KPI strip, panels only within the panel grid.
- **Auto-scroll.** The page scrolls while the pointer is within 20% of the viewport's top or bottom
  edge.
- **Drop.** Releasing commits the order the slot showed as **one Undo step** (releasing outside the
  grid keeps the last order shown); the ghost glides into the slot and fades out (180 ms), and focus
  moves to the widget's Move button.
- **Cancel.** Escape, switching away from the browser tab, or a window resize that crosses a Tailwind
  breakpoint (640 / 768 / 1024 / 1280 px) cancels the drag, and the widgets glide back to the order
  at pickup. A resize that stays inside one breakpoint — the iOS toolbar collapsing mid-drag — does
  not cancel. A release that the drag library drops before it has registered the pickup is reported
  as a cancel, so the page is never left stuck mid-drag.
- A press while widgets are still gliding finishes the glides first, and lifts only when it lands on
  the pressed widget's settled position.

### Keyboard and the Move menu

- **Tab order:** header, edit bar, the page's system banners (their Retry / Reload buttons), then
  each KPI tile's and each panel's **Remove** and **Move** buttons, in order.
- **On a Move button** (named "Move {title}", or "Move or resize {title}" for a panel): ArrowLeft /
  ArrowUp moves the widget one place earlier in reading order, ArrowRight / ArrowDown one place
  later, Home / End to the first / last place. An arrow with a modifier key is ignored. Each press is
  one Undo step; focus stays on the same Move button, and its widget is scrolled into view. At an end
  nothing moves and "{title} is already first." / "{title} is already last." is announced.
- **Enter or Space** (or a click or tap without dragging) opens the **Move menu**: **Move to start ·
  Move earlier · Move later · Move to end** (at the first place the first two are disabled, at the
  last place the last two), and for a panel a **Width** group of six radios, each with a small 12-column glyph of its span. Opening
  focuses the first enabled item. A move item moves the widget, closes the menu and returns focus to
  the Move button. A width applies at once — the menu stays open with focus on the radio — and is
  announced ("{title} width: Half.") 500 ms after the last change. **All widths chosen in one visit
  to the menu are one Undo step**, and arriving back at the width the visit started with leaves no
  step. Escape closes the menu and returns focus to the Move button; a press outside it, or focus
  moving outside it, closes it. One menu is open at a time, and starting a drag closes it. It opens
  below the button, opens above instead when it would run under the edit bar or off the bottom of
  the viewport and fits under the sticky header, and otherwise scrolls itself into view; it grows
  leftward from the cell's right edge, or rightward when that would cross the content area's left
  edge.
- **Escape never leaves edit mode.** It cancels a drag, closes the Move menu or closes a dialog.
  There are no global shortcuts — no Ctrl/Cmd+Z, no Escape-to-exit.

### Zones and widths

Every widget belongs to one of three zones, and nothing moves between them:

- **Banners** (Needs attention, Setup progress, Morning brief, Alert chips) are switched on or off,
  only from the gallery's **Alerts & banners** section. They always render in the fixed order shown
  in [Layout](#layout) and cannot be moved.
- **KPI tiles** are ordered and have one size.
- **Panels** are ordered and take one of six widths from the Move menu: Third, Narrow, Half, Wide,
  Two-thirds or Full width — 4, 5, 6, 7, 8 or 12 of the 12 columns at xl (see
  [Responsive](#responsive)).

### Remove, Add widgets and Undo

- **Remove (−)** takes the widget off the draft, and the widgets after it glide into the space.
  Focus moves to the next widget's Remove button in that zone, else the previous one's, else **Add
  widgets**, and "{title} removed from your dashboard. Undo is available." is announced.
- **Add widgets** opens the gallery (a dialog at every screen size): **Alerts & banners** (a checkbox
  per banner), then **Key metrics** and **Panels** — the widgets the viewer is
  [eligible](#eligibility-versus-link-gating) for that are not on the dashboard, grouped by category,
  each with its description, a panel's default width and an **Add** button. **Add appends the widget
  to the end of its zone** (a panel at its default width, else Half). The gallery stays open, the
  row leaves the list, focus moves to the next **Add** (else the previous one, else the gallery's
  **Done**), and the gallery's own live region announces "{title} added at position n of n." Escape,
  the backdrop, ✕ and **Done** close it without asking — every addition is already on the grid
  behind it. If something was added, focus then moves to the last added widget's Move button, which
  is scrolled into view and ringed for 1.2 s. Opened from an empty zone's placeholder, the gallery
  lands on that section's heading; from **Add widgets**, at the top. The gallery shows static
  descriptions only: it renders no widget and fetches nothing.
- **Undo** (a bar button) reverts one step at a time: a drop, a keyboard or menu move, a remove, an
  add, one Move-menu visit's widths, a banner switch. History holds 50 steps, and reloading the
  saved layout after a **409** clears it. It announces "Undone: {step}." — for example "Undone:
  moved Capacity Overview." With nothing to undo the button is `aria-disabled` but stays focusable.
  There is no Ctrl/Cmd+Z.

### Done, Cancel and Reset to default

- **Done** closes any open Move menu, then:
  - **nothing changed** (including a draft changed and changed back): leaves edit mode without a
    request and announces "Finished customizing. Nothing changed.";
  - **otherwise** it sends **one** `PUT /dashboard/layout` — the whole session in a single save —
    through `useDashboardLayout`'s compare-and-swap, with the `expected_company_id` /
    `expected_version` of the last server response (normally the entry re-read). It is
    non-optimistic: Done shows a spinner but keeps keyboard focus, the bar's status line says "Saving
    your dashboard layout…", and Add widgets, Undo, Reset, Cancel, dragging and every Remove and
    Move button are locked until the server answers. Success shows the toast "Dashboard layout
    saved" and leaves edit mode;
  - **409** (the layout changed in another tab, or the active company changed): the bar's alert shows
    the server's message with a focused **Reload saved layout**. Done, Reset, Undo, Add widgets,
    dragging and the widget chrome stay locked until the reload succeeds; Cancel stays available. The
    reload replaces the draft with the saved layout, clears Undo, says "Reloaded your saved layout.
    Your unsaved changes were discarded." and focuses the heading; a reload that fails says "Your
    saved layout could not be reloaded. Try again." and keeps focus on the button;
  - **any other failure** (for example the **503** when the audit row cannot be written): the alert
    shows the server's message (or "Your dashboard layout was not saved. Try again."), the draft is
    kept and focus returns to Done.
- **Cancel** is disabled only while a request is in flight. With unsaved changes it asks first,
  through the browser's confirm ("You have unsaved changes. Discard them?" — `useUnsavedChanges`'s
  `confirmDiscard`); declining keeps editing. It sends nothing, the widgets glide back to the saved
  layout, and "Customizing cancelled. Your saved layout is showing." is announced. After a **409**,
  Cancel also re-reads the saved layout, so the page and the next edit session start from the
  server's current version.
- **Reset to default** is disabled when no layout is saved (the server answers `layout: null` —
  tooltip "You are already using the default layout") and while the editor is locked. It asks first
  ("Reset dashboard to default?" — "Your saved layout will be replaced by the standard dashboard."),
  and the confirmation cannot be dismissed while it runs. Confirming sends
  `POST /dashboard/layout/reset` (the same compare-and-swap), shows "Dashboard reset to default" and
  leaves edit mode, discarding the draft. A **409** closes the confirmation and focuses **Reload
  saved layout**; any other failure closes it, shows the alert and focuses Reset.
- **Leaving the page with unsaved changes** asks "Leave with unsaved changes?" for in-app
  navigation, and a refresh or tab close gets the browser's own prompt. **While Done's save is in
  flight that guard is off**, because leaving then discards nothing: the PUT still lands, and its
  outcome is shown as a toast after you have left — "Dashboard layout saved", or "Your dashboard
  layout changes were not saved. Your previous layout is unchanged." A reset that settles after you
  left reports the same way ("Dashboard reset to default" / "Your dashboard was not reset. Your
  saved layout is unchanged.").
- **A session change** (sign-out, another user, another company, a read-only view) ends edit mode
  in the very render it happens, so the old draft is never shown, fetched for or saved. A draft
  with unsaved changes gets the warning toast "Your session changed, so unsaved dashboard edits were
  discarded."; focus that was lost goes to Customize (or the main content area while Customize is
  disabled); a save, reset or reload still in flight is ignored when it answers.

### Widgets edit mode cannot show

- **Widgets the viewer cannot see right now.** Saved widgets the viewer is not
  [eligible](#eligibility-versus-link-gating) for are not in the draft and not offered in the gallery,
  and a save **keeps them in place**. The bar counts the ones the viewer chose: "N widgets in your
  saved layout are not available to you right now; they are kept and will reappear if access
  returns." A widget the stock default includes is never counted (`countCarriedWidgets`), so it is
  kept without a note — today that is only Setup progress, which non-admins never see.
- **Widgets this version of the app does not know.** Ids saved by a newer version of the SPA (a
  fresh tab after a deploy) are not shown and are **dropped** by a save from this version. The bar
  warns before Done: "N widgets from a newer version of the app aren't shown here and will be
  removed if you save. Refresh the page to keep them."

### Refreshes, motion and screen readers while editing

- **Data keeps refreshing.** The 30 s poll and WebSocket pushes continue in edit mode, and keyboard
  moves never pause them. **Only while a pointer or touch drag is in progress** are poll and push
  refreshes held, together with any background request a widget makes itself — a refresh that
  changed heights would move the grid under the pointer. When the drag ends or is cancelled (or edit
  mode ends mid-drag), **one** catch-up refresh runs if anything was held: a full one, heartbeat
  included, which still waits for a hidden tab to become visible. A manual refresh is never held.
  What edit mode fetches is in [Data fetching](#data-fetching).
- **Animation.** Every edit — a live reorder, a move, a remove, an add, a width, a banner switch,
  Undo, a cancelled drag, Cancel, a reload or a reset — animates the tiles and panels it moves as one
  FLIP: a widget that moved glides to its new place (200 ms) and a new one fades in (180 ms). Data
  refreshes never animate. With `prefers-reduced-motion` there is no FLIP and no drop animation, and
  scrolling a moved widget into view jumps instead of gliding.
- **Names and descriptions.** Each Move button is described by one shared hint ("Use the arrow keys
  to move this widget one place. Home and End move it to the first or last place. Press Enter for
  more options.") followed by its position ("Position n of m."); each Remove button is named "Remove
  {title}". The drag surface and the ghost are hidden from assistive technology; the Move button is
  their equivalent.
- **Announcements** go to the page's polite live region:

  | Event | Announcement |
  |---|---|
  | Entering | "Customizing your dashboard. Drag widgets to rearrange them, or use each widget's Move button. Press Done to save." |
  | Keyboard or menu move | "{title}, position n of m." (spoken once — see below) |
  | Move at an end | "{title} is already first." / "{title} is already last." |
  | Width | "{title} width: {width}." (500 ms after the last change) |
  | Remove | "{title} removed from your dashboard. Undo is available." |
  | Undo | "Undone: {step}." |
  | Drag pickup | "Picked up {title}, position n of m." |
  | Live reorder during a drag | "{title}, position n of m." (500 ms after the last reorder) |
  | Drop | "{title} dropped at position n of m." / "{title} stays at position n of m." |
  | Drag cancelled | "Move cancelled. {title} is back at position n of m." |
  | Done with nothing changed | "Finished customizing. Nothing changed." |
  | Cancel | "Customizing cancelled. Your saved layout is showing." |

  The gallery's "{title} added at position n of n." goes to the gallery's own live region, because
  an open dialog hides the page's region from VoiceOver. The bar's status line speaks "Saving your
  dashboard layout…", "Reloading your saved layout…" and the reload notice. The drag library's own
  announcements are switched off. A keyboard or menu move's position is spoken **once**: when the
  move puts focus back on the Move button (a move later, or any move from the menu), the screen
  reader re-reads the button's description, which ends with the new position, so the live region
  stays quiet; only a move that leaves focus undisturbed (a keyboard move earlier) uses the live
  region. A screen reader set not to read descriptions therefore does not hear the new position after
  a move later or a menu move.
- **Focus.** Every outcome names a target, so focus never drops to the page body:

  | After | Focus goes to |
  |---|---|
  | Entering | The edit bar's heading |
  | An arrow, Home or End; a Move-menu move | The same widget's Move button (its widget scrolled into view) |
  | A width radio | Stays on the radio |
  | Escape in the Move menu | The Move button |
  | A drop or a cancelled drag | The dragged widget's Move button |
  | Remove | The next widget's Remove button in the zone, else the previous one's, else **Add widgets** |
  | Undo | Stays on Undo |
  | Add in the gallery | The next **Add**, else the previous one, else the gallery's **Done** |
  | Closing the gallery | The last added widget's Move button; with nothing added, the button that opened it |
  | Done, Cancel, a successful reset | The header Customize button |
  | A **409** on save or reset | **Reload saved layout** |
  | Reload succeeded / failed | The edit bar's heading / stays on **Reload saved layout** |
  | Any other save / reset failure | Done / Reset |
  | A refused entry (read-only re-read, failed re-read, the editor's code failing to load) | The header Customize button |

### How edit mode is built

- **One widget tree for both modes.** `WidgetCell` renders each KPI tile and panel in view and edit
  mode alike — the widget's body is the same element in both, with edit mode adding the chrome and
  the drag surface beside it and toggling `role`, `inert` and classes — and the widget renders inside
  a memoized body with its own widget-level error boundary, so a cell re-render never re-renders the
  widget and a crashed widget can still be moved or removed.
- **Editor state** lives in `useLayoutEditor` (main bundle): a pure reducer (`editorState.ts`: the
  draft, the Undo history and the drag session), the server verbs through the unchanged
  `useDashboardLayout`, focus, announcements and the unsaved-changes guard. Edit mode renders the
  draft run through `buildSavePayload` and `resolveLayout` — exactly what Done would store, resolved
  as view mode will resolve it; a drag's live order is render-only until the drop commits it.
  `editorCopy.ts` holds the copy more than one module must agree on — every message
  `useLayoutEditor` announces or toasts, the Move / Remove names and zone-list labels, and every
  element id the hook moves focus to — so the hook, the components and their tests import one copy.
  Text only one component renders stays in that component (the bar's heading and buttons, the Move
  menu's items, the gallery's text, a cell's "Position N of M."), as does the gallery's
  section-heading id, which the gallery focuses itself; the page's "Your saved dashboard layout
  couldn't be loaded…" refusal is in `Dashboard.tsx`.
- **The drag layer is a lazy chunk.** `@dnd-kit/core` (pinned exactly, 6.3.1) is imported by one
  module, `DashboardDragLayer.tsx`, which is reached only through `loadDragLayer.ts`'s dynamic
  `import()` — so nothing of dnd-kit is in the entry bundle, and the layer mounts only while editing.
  It is a **sibling** of the widget tree (mounting it never remounts a widget) and binds to the
  page's own cells through `dragBridge.ts`; the cells forward their mouse-down / touch-start to it.
  Where a dragged widget lands is decided by pure functions in `dragPlanning.ts` from the boxes the
  browser laid out; the animation is `useFlipLayout.ts` (Web Animations API).
- **No server change.** Storage, API, schema, permissions and audit are what PR #326 shipped: Done is
  the existing `PUT /dashboard/layout` and Reset the existing `POST /dashboard/layout/reset`, each
  writing one required audit row per real change.

### Decisions open to change

These were decided for the first version of edit mode and can be revisited:

1. **In-place edit mode with a bottom bar, saved by Done.** The whole session is one save; there is
   no autosave per drag, because each save writes an audit row and bumps the version. The button
   could read "Save layout" instead of "Done".
2. **A read-only session gets a toast** explaining why it cannot customize, instead of a disabled
   editor (the first release opened a read-only dialog).
3. **New widgets come from the Add widgets gallery and land at the end of their section**; the user
   then drags them into place. Dragging straight out of the gallery is not built.
4. **Widths are the six named sizes, chosen in each panel's Move menu.** There are no drag-to-resize
   handles, KPI tiles have one size, banners are on/off in a fixed order, and widgets cannot cross
   between the KPI strip and the panel grid.
5. **The instrument-panel look rather than Apple mimicry:** no jiggle, square "−" badges, dashed
   hairline outlines.
6. **Undo is a bar button, with no Ctrl/Cmd+Z**, and Cancel discards the whole session, asking first
   when there are changes.

### Persistence

A layout is stored **on the server, per user, per active company** (table `dashboard_layouts`, one
row per company + user, migration `118_dashboard_layouts`), so it follows the user across browsers
and devices, and a person who works in two companies (a platform admin switching context) keeps a
separate layout in each. Nothing is kept in browser storage. Every save and reset writes a required
audit row (resource type `dashboard_layout`), so layout changes appear in the Audit Log.

- **Who can save:** any role that can open `/`, but only from an **interactive person's session in a
  writable company**. In a read-only company view the page still renders the saved layout, but
  **Customize** does not enter edit mode: it shows the info toast "This session can't save a
  dashboard layout — read-only company view or an API/kiosk credential." (see
  [Entering edit mode](#entering-edit-mode)). An API token can read the layout (`can_edit: false`)
  but not save it; a kiosk badge token is refused on every verb by its path fence. See
  [`docs/RBAC_PERMISSIONS.md`](RBAC_PERMISSIONS.md#dashboard).
- **The server checks shape, not membership.** It accepts any well-formed widget id and width; the
  widget catalog belongs to the frontend, because the SPA and the API deploy independently. A stored
  id grants nothing: it names a component this bundle may or may not render, and every endpoint a
  widget reads enforces its own authorization.

### The default

With no saved layout (or after **Reset to default**) the server answers `layout: null` and the page
renders `CLASSIC_LAYOUT` (`widgetRegistry.ts`): the exact cockpit described in [Layout](#layout), for
**every role**, with the original spans (capacity and work-center status 7 columns, live activity and
presence 5, recent completions the full width). None of the opt-in widgets are on it. An empty widget
list is different: it is a deliberately empty dashboard, and reset is the only way back to the
default.

### Widget catalog

"Heartbeat" is `GET /shop-floor/dashboard`, which the page always fetches (see
[Data fetching](#data-fetching)). Core sources (`quality`, `equipment`, `stock`, `capacity`) have no
refresh floor: they refetch on every poll, push and manual refresh, as before customization. The
permission names are frontend permissions (`frontend/src/utils/permissions.ts`), evaluated with the
company's role overrides; which default roles hold each is in
[`docs/RBAC_PERMISSIONS.md`](RBAC_PERMISSIONS.md#dashboard).

**On the default layout** (core widgets, `CORE_WIDGETS`):

| Id | Title | Zone | Data | Shown to |
|---|---|---|---|---|
| `attention` | Needs attention | banner | Heartbeat + `quality`, `equipment`, `stock` | Everyone |
| `setup_nudge` | Setup progress | banner | Fetches `GET /setup/health` itself | Admins (`admin` role or superuser) |
| `morning_brief` | Morning brief | banner | Fetches `GET /ai/recommendations` itself | Everyone |
| `alert_chips` | Alert chips | banner | Heartbeat + `quality`, `equipment`, `stock` | Everyone |
| `kpi.overdue` | Overdue | KPI | Heartbeat `summary.overdue` | Everyone |
| `kpi.idle_operators` | Signed In, Idle | KPI | Heartbeat | Everyone |
| `kpi.active_work_orders` | Active Work Orders | KPI | Heartbeat | Everyone |
| `kpi.signed_in` | Signed In Now | KPI | Heartbeat | Everyone |
| `kpi.checked_in` | Checked In Now | KPI | Heartbeat | Everyone |
| `kpi.due_today` | Due Today | KPI | Heartbeat | Everyone |
| `kpi.calibration_due` | Calibration Due | KPI | `equipment` → `GET /calibration/equipment/due-soon?days=30` | Everyone |
| `kpi.low_stock` | Low Stock Items | KPI | `stock` → `GET /inventory/low-stock` | Everyone |
| `kpi.completed_today` | Operations Completed Today | KPI | Heartbeat | Everyone |
| `kpi.open_ncrs` | Open NCRs | KPI | `quality` → `GET /quality/summary` | Everyone |
| `capacity_overview` | Capacity Overview | panel, `wide` | `capacity` → `GET /scheduling/capacity-heatmap` (Central today + 6 days) | Everyone |
| `live_activity` | Live Shop Activity | panel, `narrow` | Heartbeat | Everyone |
| `work_center_status` | Work Center Status | panel, `wide` | Heartbeat | Everyone |
| `presence` | Signed In Right Now | panel, `narrow` | Heartbeat | Everyone |
| `recent_completions` | Recent Completions | panel, `full` | Heartbeat | Everyone |

**Opt-in** (added from Customize → **Add widgets**; two core KPI tiles plus the panels in `EXTRA_WIDGETS`,
`widgets/extra/index.ts`). Every one reads an endpoint that already existed:

| Id | Title | Zone (default width) | Source → endpoint | Shown to | Refresh floor |
|---|---|---|---|---|---|
| `kpi.open_cars` | Open CARs | KPI | `quality` (shared) | `quality:view` | none (core) |
| `kpi.pending_fai` | Pending FAIs | KPI | `quality` (shared) | `quality:view` | none (core) |
| `action_inbox` | Action Inbox | panel (`half`) | `actionInbox` → `GET /operations-inbox/` | Everyone (the server filters items by permission) | 2 min, cached |
| `notifications` | Notifications | panel (`third`) | Unread count from the store the header bell already polls; list `notifications` → `GET /notifications?unread=true&page_size=5` | Everyone | List: 2 min, or at once when the unread count changes |
| `work_orders_overdue` | Overdue Work Orders | panel (`half`) | `workOrdersOverdue` → `GET /work-orders/browse?scope=overdue&sort=due_date&direction=asc&limit=8&hide_cots=false` | `work_orders:view` | 60 s |
| `work_orders_due_today` | Work Orders Due Today | panel (`half`) | `workOrdersDueToday` → same, `scope=due_today` | `work_orders:view` | 60 s |
| `late_jobs` | Late Jobs | panel (`half`) | `wallboard` → `GET /shop-floor/wallboard` | `work_orders:view` | 60 s |
| `blocked_jobs` | Blocked Jobs | panel (`third`) | `wallboard` | `work_orders:view` | 60 s |
| `todays_output` | Today's Output | panel (`half`) | `wallboard` | `work_orders:view` | 60 s |
| `shipping_outlook` | Shipping Outlook | panel (`third`) | `wallboard` | `shipping:view` | 60 s |
| `active_downtime` | Active Downtime | panel (`half`) | `downtime` → `GET /downtime/active` | `work_orders:view` | 60 s |
| `maintenance_due` | Maintenance Due | panel (`third`) | `maintenance` → `GET /maintenance/dashboard` | `work_orders:view` | 5 min, cached |
| `quality_pulse` | Quality Pulse | panel (`half`) | `quality` (shared) | `quality:view` | none (core) |
| `inspection_queue` | Inspection Queue | panel (`half`) | `inspectionStats` → `GET /receiving/stats?days=30` and `inspectionQueue` → `GET /receiving/inspection-queue` | `receiving:view` | 2 min |
| `delivery_kpis` | Delivery & Yield KPIs | panel (`half`) | `deliveryKpis` → `GET /analytics/kpis?period=30d` | Admin, Manager or Supervisor role (superuser and platform admin pass) | 10 min, cached; a manual refresh at most every 60 s |
| `visitors_on_site` | Visitors On Site | panel (`third`) | `visitors` → `GET /visitor-logs/?on_site_only=true&limit=10` | Admin, Manager or Supervisor role (superuser and platform admin pass) | 2 min |

The four `wallboard` panels share **one** request. The work-order lists send `hide_cots=false` so
their totals agree with the Overdue and Due Today tiles, which count COTS work orders too. Quality
Pulse's "New NCRs this month by current disposition" counts by the server's calendar month in UTC,
not the Central day used elsewhere, and says so in its tooltip. The two role-gated panels mirror
their endpoints' `require_role([ADMIN, MANAGER, SUPERVISOR])` rather than a permission, so a
company's permission overrides cannot offer them to a role the server refuses.

### Eligibility versus link gating

Two separate checks, both built on the same `usePermissions().can` that `PrivateRoute` and the
sidebar use (`components/dashboard/useWidgetEligibility.ts`):

- **Eligibility** (`isEligible` on a widget definition) decides whether a widget is offered in the
  Add widgets gallery and rendered. It is a UX proxy that mirrors the endpoint's server gate as closely as the
  client can; the endpoint still enforces its own authorization, and a widget whose source answers
  **403** renders "Not available for your role" inside the widget (never in the page's error banner,
  and no poll or manual refresh asks again while the page stays open — see Errors below).
- **Link gating** (`canOpen(href)`) decides whether something inside a visible widget is a link.
  `canOpen` is `canAccessPath(href, can)` (`utils/routeAccess.ts`), the exact predicate
  `PrivateRoute` enforces, so the dashboard never shows a link the router would send to
  `/unauthorized`. A link the viewer cannot open renders as plain text (KPI tiles render static,
  chips become text, row links become text); a panel's header action and the Needs attention
  **Review affected records** button are hidden.

Saved widgets the viewer is not eligible for right now are not rendered and not fetched, but a save
**keeps them**, in place, and the edit bar says how many ("…they are kept and will reappear if access
returns" — a widget the stock default includes, today only Setup progress, is kept without a note), so
a temporary permission change never destroys a layout. A widget id this bundle does not know (a layout
saved by a newer or older SPA) is ignored when rendering and **dropped** on the next save from this
bundle; the edit bar warns before Done (see
[Widgets edit mode cannot show](#widgets-edit-mode-cannot-show)).

### Cross-links degrade when their target is hidden

In-page jumps work only when the widget they jump to is on screen; otherwise the control is plain,
non-interactive text:

| Control | Needs |
|---|---|
| "Signed In, Idle" tile (scrolls to the idle list) | `presence` |
| Live Shop Activity's empty-state "View idle operators" action | `presence` |
| "Operations Completed Today" tile (scrolls to Recent Completions) | `recent_completions` |
| Work Center Status "N ppl" count | `live_activity` |
| Signed In Right Now on-the-job chips | `live_activity` |

### Data fetching

- **The heartbeat is always fetched.** `GET /shop-floor/dashboard` (ETag-cached) feeds the
  summary tiles and the core panels and keeps its own request guard: on load, every 30 s, on
  WebSocket pushes and on a manual refresh, whatever the layout. Its poll and push reloads skip a
  hidden tab and run once when the tab becomes visible.
- **Only visible widgets' sources are fetched.** The page fetches the union of the `sources` of the
  widgets it renders, through one keyed loader (`useDashboardSources.ts`) with its own state and
  request sequence per source, so one widget's slow request can never discard another's. Nothing is
  fetched for widgets until the saved layout has settled (at most 3 s). A source that becomes needed
  is fetched at once; one that stops being needed has its state dropped and any late answer ignored.
- **In edit mode** the page fetches the saved layout's sources **plus** the draft's, the draft's
  only once its membership has been unchanged for 400 ms (`EDIT_SOURCE_SETTLE_MS` in `Dashboard.tsx`):
  an added widget's source is fetched 400 ms after the add, and an add undone inside that window
  sends nothing. A saved widget removed in the draft keeps its source — and that source's state, a
  403 included — while you edit, so Cancel puts it back with its data at once. Leaving edit mode
  drops draft-only sources immediately. Only the current edit session's draft counts (a settled value
  left over from a cancelled session is never fetched), a drag never changes membership, and the
  gallery never fetches. Background refreshes pause only during a drag — see
  [Refreshes, motion and screen readers while editing](#refreshes-motion-and-screen-readers-while-editing).
- **Floors.** Background ticks (the 30 s poll and pushes) skip a source younger than its floor
  (table above) and never replace a request still in flight unless it is older than
  `max(floor, 30 s)`. A manual refresh (the header button, "Retry unavailable sections", a widget's
  Retry) bypasses the floors, except Delivery KPIs' 60 s hard minimum. After a failure a source is
  retried once `min(floor, 60 s)` has passed. All background ticks are skipped while the tab is
  hidden.
- **Cached sources.** Action Inbox (2 min), Maintenance (5 min) and Delivery KPIs (10 min) keep
  their last result in a module-level cache keyed by the session, so navigating away from `/` and
  back inside that window does not refetch them. Every answer is recorded when it arrives, even
  after you have left `/`, and the newest attempt wins whatever order they arrive in. A request
  still in flight when you come back (or re-add the widget) is adopted while it is younger than
  the in-flight grace (`max(floor, 30 s)`): it is not sent again, its start counts for the
  floors, and when it answers its result, failure or refusal is shown. Older than that it is
  treated as hung, as a poll would treat it: with cached data on screen its answer is still
  shown if it arrives before the page's own next request replaces it; with none, the source is
  fetched again at once. So a cached result is
  never passed off as fresh after a newer refresh of it failed: coming back while the result is
  still inside its window shows "could not refresh" and retries on the error cadence. With no
  result inside its window and no request young enough to adopt, the source is fetched again. A
  refusal (403) newer than the cached result evicts it. The Notifications list has its own short cache, which the widget clears
  when it unmounts, so returning to `/` always shows a list that agrees with the unread count; a
  manual refresh or the widget's Retry always reaches the network, and a failed refresh of the list
  is never answered from that cache as a success.
- **Errors.** A failed source keeps its last verified data and is named in the page's widget-sources
  banner. A **403** shows in the widget only and is terminal for that source while the page stays
  open: no poll or manual refresh asks again. Leaving `/` and coming back, saving the layout without
  the widget and adding it back later, or a session change asks the server again.
- **Session changes.** Signing out, switching user or company, or entering a read-only company view
  re-reads the saved layout, ends edit mode (see
  [Done, Cancel and Reset to default](#done-cancel-and-reset-to-default)) and clears every source's
  state and cache.

### Loading the saved layout

The page reads `GET /dashboard/layout` on load and on every session change, and shows the loading
skeleton until it settles:

- **No answer within 3 s** (`LAYOUT_LOAD_TIMEOUT_MS`): the request is abandoned, the default layout
  renders, and the notice "Your saved dashboard layout couldn't be loaded; showing the default."
  offers **Reload layout**. A late answer is never swapped in under the user.
- **404 or 405**: the API predates this feature (the SPA can deploy before the API). The default
  renders, **Customize is hidden**, and no notice shows.
- **Any other failure** (or a response of the wrong shape): the default renders with the same
  notice. **Customize** re-reads the layout before every entry and, after a failed load, enters
  edit mode only if that re-read succeeds (see [Entering edit mode](#entering-edit-mode)). A failed
  reload never replaces a layout already on screen.

Saves and resets happen only from edit mode, non-optimistically, with the `expected_company_id` /
`expected_version` of the last server response — see
[Done, Cancel and Reset to default](#done-cancel-and-reset-to-default) for the **409** and
**Reload saved layout** path.

### Which "overdue" each surface uses

Several surfaces flag late work, and they do not all mean the same thing:

| Surface | Rule | Source |
|---|---|---|
| Overdue tile, the overdue alert chip and Needs attention | `due_date` before Central today on a non-deleted work order that is not COMPLETE, CLOSED or CANCELLED — DRAFT, RELEASED, IN_PROGRESS and ON_HOLD all count, COTS included | Heartbeat `summary.overdue` |
| Overdue Work Orders panel (and its "View all" link) | The same rule, so its total matches the tile | `GET /work-orders/browse?scope=overdue` |
| Late Jobs panel | **Promise date** — `must_ship_by`, else `due_date` — before Central today on a non-deleted, non-terminal work order (DRAFT included); labelled "by promise date" | `GET /shop-floor/wallboard` |
| Action Inbox panel (late work-order items) | `due_date` before today in the company's timezone (default America/Chicago) on RELEASED, IN_PROGRESS or ON_HOLD work orders — **DRAFT excluded** | `GET /operations-inbox/` |
| Live Shop Activity row's red dot | The assignment's work-order `due_date` before Central today | Heartbeat |

### What changed for existing users

The default layout renders the same sections, in the same order, at the same widths. The deliberate
visible differences:

- **Links the viewer cannot open are no longer links.** Before, they led to `/unauthorized`. With the
  default role permissions: Work Center Status **View All** shows only for Admin (`/work-centers`
  needs `admin:settings`); the Calibration Due tile and the calibration chip are plain text for
  Supervisor, Shipping and Viewer (`/calibration` needs `quality:calibration`); the Open NCRs tile and
  the NCR chip are plain text for Shipping (`/quality` needs `quality:view`); and the Needs attention
  **Review affected records** button is hidden whenever its first exception is one of those. The
  **Shop Floor** header link hides only for a company whose role overrides remove
  `work_orders:view`.
- **The page waits up to 3 s for the saved layout** before showing widgets (skeleton meanwhile).
- **Recent Completions sits inside the panel grid** as a full-width cell instead of a separate card
  below it; its markup and height cap are unchanged.
- **Background refreshes pause in a hidden tab** (poll and pushes), with one catch-up load on return.
- **Each KPI tile has its own widget error boundary**, as each panel already had: a tile that throws
  while rendering shows the error in its own slot.
- **Customize edits the page in place.** The first release of customization (PR #326) opened a
  Customize dialog with ↑ / ↓ buttons; it was replaced by [edit mode](#customizing-the-dashboard),
  and a read-only session now gets a toast instead of a read-only dialog.
- **Platform admins** no longer lose every permission check after a password login until the next
  reload (`utils/permissions.ts`: `platform_admin` ignores per-company role overrides, matching the
  backend, where it passes every gate).

### Adding a widget

1. **Define it.** Add an entry to `EXTRA_WIDGETS` (`components/dashboard/widgets/extra/index.ts`)
   for an opt-in panel, or to `CORE_WIDGETS` (`widgetRegistry.ts`) for a banner or KPI tile: a
   unique `id` matching the server's pattern `^[a-z][a-z0-9_.]{0,63}$`, `zone`, the `title` (edit
   mode's names and the gallery) and gallery `description`, `category`, `sources`, a `defaultSize` for a panel, and an `isEligible` that
   mirrors the endpoint's server gate (`can('<route permission>')`, or `hasRole([...])` where the
   endpoint uses `require_role`). Omitting `isEligible` offers it to everyone who can open `/`.
2. **Give it data.** Reuse an existing source key when another widget already reads the endpoint
   (one request is shared). Otherwise add a key to `ExtraSourceKey` (`widgetTypes.ts`) and a
   `SourceSpec` in `sources/extraSources.ts`: a `label` for the error banner, a `fetch` through the
   `services/api.ts` client, a `minIntervalMs` floor, and `hardMinIntervalMs` / `cacheTtlMs` for an
   expensive endpoint. Widgets read existing endpoints; the layout routes never carry widget data, so
   no backend change or migration is needed.
3. **Render it.** A zero-prop component that reads `useDashboardSource(key)` and
   `useDashboardWidgetEnv()` (`canOpen`, `isVisible`, `nowMs`, `refresh`), registered in
   `EXTRA_WIDGET_RENDERERS` (core: `CORE_WIDGET_RENDERERS` in `widgets/core/index.ts`). A panel
   renders a `CockpitPanel` with no column-span classes (the page's cell carries the width). Every
   link goes through `canOpen`: use `WidgetLink` / `PanelAction` from `widgets/extra/shared.tsx`, and
   render a 403 with `ForbiddenNotice`.
4. **Test it.** One widget test using `renderWithDashboardContext` and `readySource`
   (`frontend/src/test-utils/dashboardWidgets.tsx`) for data, empty, error and forbidden. The catalog
   guards will need updating on purpose: `widgets/extra/index.test.ts` pins the number of opt-in
   panels, their eligibility and source keys; `dashboardLayout.test.ts` checks every id against the
   server pattern and for a renderer; `useDashboardSources.test.tsx` checks every source key has a
   spec.
5. **Leave the default alone.** Do not add the widget to `CLASSIC_LAYOUT` unless the owner decides
   to change everyone's dashboard: `layout: null` means "this bundle's classic layout", so editing it
   changes the page for every user who never customized, in every role. Renaming or removing an id
   drops that widget from saved layouts the next time each user saves.
6. **Document it** in the catalog table above and the eligibility list in
   [`docs/RBAC_PERMISSIONS.md`](RBAC_PERMISSIONS.md#dashboard).
