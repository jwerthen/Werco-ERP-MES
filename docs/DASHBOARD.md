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
   manual refresh, **Customize** (hidden when the API predates the feature — see
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
   banner which happens to render nothing (no exception, no brief) is not "empty".
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
  data is trapped behind a nested scroll.

Panel widths are a fixed vocabulary, mapped to static Tailwind classes in one place
(`PANEL_SIZE_CLASSES`, `components/dashboard/widgetTypes.ts`):

| Width (`size`) | Dialog label | Classes |
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

**Customize** in the page header opens a dialog where a user chooses what their own dashboard shows.
Source: [`frontend/src/components/dashboard/`](../frontend/src/components/dashboard/) (registry,
layout logic, loaders, dialog, widgets) and [`frontend/src/pages/Dashboard.tsx`](../frontend/src/pages/Dashboard.tsx);
API contract in [`docs/API.md` → Personal dashboard layout](API.md#personal-dashboard-layout).

### What a user can do

- **Alerts & banners** — turn each banner on or off. Banners always render in the fixed order shown in
  [Layout](#layout); they cannot be reordered.
- **Key metrics** and **Panels** — each has an **On your dashboard** list (render order; **↑ / ↓**
  move, **Remove**) and an **Available** list grouped by category (Alerts, Shop floor, Work orders,
  Quality, Supply chain, Planning, Insights, People) with **Add**, which appends to the end. Panels
  also take a width: Third, Narrow, Half, Wide, Two-thirds or Full width (see
  [Responsive](#responsive)). There is no drag-and-drop; after each move, add or remove, focus stays
  on the same control (or the nearest one that still exists) and a polite live region announces the
  new position.
- **Save layout** — enabled only when the draft differs from what is saved and the session can save.
- **Reset to default** — returns to the default layout after a confirmation ("Reset dashboard to
  default?"); disabled when the user is already on the default.

Only widgets the viewer is [eligible](#eligibility-versus-link-gating) for are listed. Escape, the
backdrop, the close button and Cancel all go through the standard unsaved-changes guard
(`useUnsavedChanges`); while a save or reset is in flight the dialog cannot be dismissed.

### Persistence

A layout is stored **on the server, per user, per active company** (table `dashboard_layouts`, one
row per company + user, migration `118_dashboard_layouts`), so it follows the user across browsers
and devices, and a person who works in two companies (a platform admin switching context) keeps a
separate layout in each. Nothing is kept in browser storage. Every save and reset writes a required
audit row (resource type `dashboard_layout`), so layout changes appear in the Audit Log.

- **Who can save:** any role that can open `/`, but only from an **interactive person's session in a
  writable company**. In a read-only company view the page still renders the saved layout, and the
  dialog opens read-only with the notice "This session can't save a dashboard layout — read-only
  company view or an API/kiosk credential." An API token can read the layout (`can_edit: false`)
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

**Opt-in** (added from Customize; two core KPI tiles plus the panels in `EXTRA_WIDGETS`,
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
  dialog and rendered. It is a UX proxy that mirrors the endpoint's server gate as closely as the
  client can; the endpoint still enforces its own authorization, and a widget whose source answers
  **403** renders "Not available for your role" inside the widget (never in the page's error banner,
  and the source is not retried until the session changes).
- **Link gating** (`canOpen(href)`) decides whether something inside a visible widget is a link.
  `canOpen` is `canAccessPath(href, can)` (`utils/routeAccess.ts`), the exact predicate
  `PrivateRoute` enforces, so the dashboard never shows a link the router would send to
  `/unauthorized`. A link the viewer cannot open renders as plain text (KPI tiles render static,
  chips become text, row links become text); a panel's header action and the Needs attention
  **Review affected records** button are hidden.

Saved widgets the viewer is not eligible for right now are not rendered and not fetched, but a save
**keeps them**, in place, and the dialog says how many ("…they are kept and will reappear if access
returns"), so a temporary permission change never destroys a layout. A widget id this bundle does not
know (a layout saved by a newer or older SPA) is ignored when rendering and **dropped** on the next
save from this bundle.

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
- **Floors.** Background ticks (the 30 s poll and pushes) skip a source younger than its floor
  (table above) and never replace a request still in flight unless it is older than
  `max(floor, 30 s)`. A manual refresh (the header button, "Retry unavailable sections", a widget's
  Retry) bypasses the floors, except Delivery KPIs' 60 s hard minimum. After a failure a source is
  retried once `min(floor, 60 s)` has passed. All background ticks are skipped while the tab is
  hidden.
- **Cached sources.** Action Inbox (2 min), Maintenance (5 min) and Delivery KPIs (10 min) keep
  their last result in a module-level cache keyed by the session, so navigating away from `/` and
  back does not refetch them. A cached result is never passed off as fresh after a newer refresh
  of it failed — including one that fails after you left `/` or removed the widget, or one still
  in flight when you come back — so returning shows "could not refresh" and retries on the error
  cadence; a refusal (403) evicts the entry. The Notifications list has its own short cache, which
  the widget clears when it unmounts, so returning to `/` always shows a list that agrees with the
  unread count; a manual refresh or the widget's Retry always reaches the network, and a failed
  refresh of the list is never answered from that cache as a success.
- **Errors.** A failed source keeps its last verified data and is named in the page's widget-sources
  banner. A **403** is terminal for that source until the session scope changes and shows in the
  widget only.
- **Session changes.** Signing out, switching user or company, or entering a read-only company view
  re-reads the saved layout, closes the dialog and clears every source's state and cache.

### Loading the saved layout

The page reads `GET /dashboard/layout` on load and on every session change, and shows the loading
skeleton until it settles:

- **No answer within 3 s** (`LAYOUT_LOAD_TIMEOUT_MS`): the request is abandoned, the default layout
  renders, and the notice "Your saved dashboard layout couldn't be loaded; showing the default."
  offers **Reload layout**. A late answer is never swapped in under the user.
- **404 or 405**: the API predates this feature (the SPA can deploy before the API). The default
  renders, **Customize is hidden**, and no notice shows.
- **Any other failure** (or a response of the wrong shape): the default renders with the same
  notice. **Customize** then re-reads the layout first and opens the dialog only if that succeeds.
  A failed reload never replaces a layout already on screen.

Saving is **non-optimistic**: the dialog stays open with a spinner until the server answers, and
sends the `expected_company_id` / `expected_version` of the last server response. A **409** (the
layout changed in another tab, or the active company changed) shows the server's message in the
dialog with **Reload saved layout**, which replaces the draft with what is saved and says so
("Reloaded your saved layout. Your unsaved changes were discarded."). Success closes the dialog with
"Dashboard layout saved" (reset: "Dashboard reset to default").

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
- **Platform admins** no longer lose every permission check after a password login until the next
  reload (`utils/permissions.ts`: `platform_admin` ignores per-company role overrides, matching the
  backend, where it passes every gate).

### Adding a widget

1. **Define it.** Add an entry to `EXTRA_WIDGETS` (`components/dashboard/widgets/extra/index.ts`)
   for an opt-in panel, or to `CORE_WIDGETS` (`widgetRegistry.ts`) for a banner or KPI tile: a
   unique `id` matching the server's pattern `^[a-z][a-z0-9_.]{0,63}$`, `zone`, dialog `title` /
   `description`, `category`, `sources`, a `defaultSize` for a panel, and an `isEligible` that
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
