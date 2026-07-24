# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5), built to the
high-fidelity **"Foundry"** TV design (2026-07-22 redesign — near-black instrument panel,
JetBrains Mono, tabular numerals, authored at 1920×1080). Four fixed zones: a **HUD command bar**
(DOWN / BLOCKED / LATE alert chips, sync status, Central wall clock), a fixed **4×3 grid of
work-order cards** with a `+N MORE WORK ORDERS IN QUEUE` overflow strip, a **430px right rail**
(SHIP TODAY, LATE — OLDEST FIRST, BLOCKED·DOWN, OPEN NCRS + ON HOLD), and a **TODAY KPI footer**.
Nothing on the board scrolls, rotates, or requires interaction — every zone has a fixed capacity,
a `+N more` overflow, and a designed empty state, and every panel keeps its slot at all data
values so a habitual glance lands on a memorized coordinate.

This is **not** the interactive operator kiosk (`/shop-floor/operations?kiosk=1`, badge login —
see `docs/onboarding/02-operator-shop-floor.md`). The wallboard takes no input and writes nothing;
it authenticates with a **scoped display token** instead of a user session.

## Setting up a TV

1. **Issue a display** — Admin Settings → **Wallboard Displays** tab → New display. Give it a
   label naming the physical screen ("North wall TV"), a lifetime (default 90 days, max 365),
   — for a one-department TV — an optional **department preset** (a work-center type, e.g.
   `machining`), and the **Show customer names** opt-in (default **OFF** = public-safe; turn it on
   only for a trusted executive-office screen — see "Customer names — gated" below). The UI is on
   the Admin Settings page (admin-gated); the API also allows Manager
   (`require_role([ADMIN, MANAGER])`).
2. **Copy the 8-char setup code.** Issuance shows a one-time **setup code** (grouped `XXXX-XXXX`;
   valid **15 minutes**, **single use**) alongside the fallback `#token=` URL (below). Both are
   shown **exactly once** — but unlike the URL, a lost or expired code is cheap to replace (step
   4).
3. **On the TV, open `https://<your-host>/tv` and type the code.** Codes are case-insensitive,
   dashes/spaces are ignored, and the alphabet excludes `0/O/1/I/L` so nothing is ambiguous read
   off a screen; `/tv/<code>` also works as a deep link. The page claims the code, stores the
   minted display token in `localStorage`, and lands on `/wallboard` — with `?dept=` applied
   automatically when the display carries a department preset. **TV reboots and browser restarts
   don't need re-pairing**: the credential persists on the device until the display is revoked or
   expires (or the browser's storage is wiped).
4. **Re-pairing after a repair or browser wipe:** click **New setup code** on the display's row,
   walk to the TV, open `/tv`, type the fresh code. Reissuing invalidates the previous code
   immediately (used or not) and doesn't touch the display's lifetime or revocation state. The
   action is disabled for revoked/expired displays — issue a new display for those.
5. **Kiosk mode.** Run the TV's browser in kiosk/full-screen mode with sleep disabled — `/tv` is
   safe as the browser homepage (an already-paired display bounces straight to the board), e.g.
   `chromium --kiosk 'https://<host>/tv'`, or the smart-TV browser's full-screen setting. The page
   is self-contained — no app chrome, no login screen. Everything is sized in
   `rem` off the viewport-scaled root `calc(100vh / 67.5)` (1rem = 16px at 1080p, 32px at 4K —
   identical angular size; the design is authored at 1920×1080 with a 22×24px page margin);
   verify legibility on the actual hardware at viewing distance, especially the orange (BLOCKED)
   vs amber (LATE) discrimination.

### Per-display settings (URL params)

Three optional display settings, all **off by default**, set via URL params (`1` on, `0` off) and
**persisted per device** to `localStorage['wallboard_display_settings']` whenever any of the three
appears in the URL — so you can open `/wallboard?clock24=1` once at pairing time and drop the
param afterwards; a `0` clears the stored setting the same way. Params the URL doesn't mention
load from the stored settings.

- `clock24=1` — 24-hour wall clock (the `UPDATED` time follows the same format).
- `seconds=1` — show seconds on the wall clock.
- `dim=1` — night-dim: a full-screen `rgba(0,0,0,0.38)` overlay (`pointer-events: none`) for
  unmanned/night shifts.

All times stay **Central** regardless of format.

### Fallback: the one-time `#token=` URL

Issuance still returns the raw display JWT and a ready-made
`https://<your-host>/wallboard#token=<jwt>` URL (shown once with the setup code; the server never
returns the token again — if it's lost, reissue a setup code or revoke and issue a new display).
The token rides in the URL **fragment** so it never leaves the browser in requests or server logs;
legacy `?token=<jwt>` query-param URLs from earlier issuances still work. On first load the page
moves the token from the URL into `sessionStorage` and scrubs it from the address bar (so it
doesn't linger in screenshots or over-the-shoulder photos). Because URL-pasted tokens live in
`sessionStorage`, closing the browser drops them — bookmark/relaunch with the full `#token=` URL,
or keep the browser session alive. Pairing via `/tv` has neither problem (the claimed token
persists in `localStorage` and never rides in a URL) — prefer the setup code for anything
permanent.

### One TV per department

Set the **department preset** on the display at issuance and a TV paired via `/tv` lands on
`/wallboard?dept=<type>` automatically. For URL-based setups, append `dept=<work_center_type>` as
a query param (before the `#token=` fragment) to narrow the board to one department, e.g.
`/wallboard?dept=machining#token=<jwt>`. `dept` matches the work-center type case-insensitively;
the HUD identity line renders it upper-cased (`LIVE WALLBOARD // MACHINING`).

**What `dept` scopes — and what it never scopes:**

- **Dept-scoped:** the **work-order grid** (`jobs`) — a work order belongs to a dept TV via its
  **current operation's** work-center type (a WO whose ops are all complete has no current op and
  drops off dept boards) — the `work_centers` block (the board's DOWN join source), and the LATE
  and BLOCKED·DOWN rail panels — both their visible rows **and** their true totals (`late_total`
  / `blocked_total` / `down_total`) — and therefore the HUD's DOWN / BLOCKED / LATE alert chips
  (a machining TV headlines machining's truth). Rail attribution rules: a WO is *late for a dept*
  when it has ≥1 open (non-complete) operation routed to a work center of that type; a blocker
  belongs to a dept via **its operation's** work center; a work center is *down for a dept* by
  its own type.
- **Consequences:** a late WO with open operations in two departments appears on **both** dept
  TVs' LATE rails, but its card shows only where its *current* op lives; a blocker filed
  without an operation (and a late WO with no routed open operations) cannot be dept-attributed
  and shows only on the **unfiltered** board. A card's BLOCKED state is **WO-level**
  (any unresolved blocker, wherever it's routed), so a dept TV can legitimately show a BLOCKED
  card while its dept-scoped chip/rail count blockers routed elsewhere — the card answers
  "can this job proceed?", the rail answers "what's stuck in *this* department?".
- **Always plant-wide:** the SHIP panel, the NCR/HOLD split row, and the TODAY cells — `dept`
  never scopes them server-side (semantics unchanged from the previous design). The Foundry board
  no longer renders the small `PLANT` tag the previous design put on the SHIP and QUALITY panels;
  the numbers remain plant-wide, and the HUD identity line (`LIVE WALLBOARD // <DEPT>`) is the
  only dept marker on screen.

### Customer names — gated (executive vs. public boards)

The board's long-standing posture is **no customer names on a public screen** — a CUI/AS9100D
privacy requirement. There is now **one gated exception**: an executive-office board can show the
work order's **customer name** on each card. It is off by default and enforced **server-side** in
`build_wallboard_payload` — a display can never widen its own scope past the gate.

A tile's `customer_name` is populated **only** when the requesting principal is authorized:

- a **display token** whose `show_customer_names` flag is `True` — the per-display **Show customer
  names** opt-in set at issuance (`display_tokens.show_customer_names`, `Boolean NOT NULL`, default
  `false`; migration `072_display_token_show_customer`), **or**
- a **signed-in user** whose role is **Platform Admin, Admin, or Manager**.

Every other principal — a public / un-flagged display token, or a signed-in Supervisor / Operator /
Quality / Shipping / Viewer previewing the board in-app — gets `customer_name = None` (redacted),
identical to a public TV.

On a redacted (public) board, card **Row 3** keeps its existing `OP n/total · <op name>` line, so
nothing is lost there. On an authorized (executive) board the customer name **replaces** that line
(and falls back to the op line for any WO with no customer set). Set the opt-in only on a screen
whose viewers are cleared to see customer identities.

Two operational notes:

- **The in-app board is not automatically public-safe.** A signed-in Platform Admin / Admin /
  Manager who opens `/wallboard` in their own session renders customer names regardless of any
  display token — so an office user who walks up to a shop-floor screen and signs in exposes them.
  This is that user's authenticated session (they already have customer-data access everywhere),
  not a new leak, but don't treat "open the board in the app" as equivalent to an un-flagged public
  display. For an always-public shop TV, pair it with a display token that has **Show customer
  names** OFF and leave it on the display credential, not a signed-in session.
- **The flag is fixed at issuance.** There is no edit endpoint for `show_customer_names`; to flip a
  display between public and executive, **revoke it and issue a new one** (or issue a fresh setup
  code from a new display). This is deliberate — every public↔executive transition is a fresh,
  audited issuance rather than a silent toggle.

## Layout — the four zones

Fixed geography, authored at 1920×1080: HUD bar (86px) / body (work-order grid + the fixed 430px
right rail) / TODAY KPI bar (102px), 22×24px page margin, 13–14px gaps. Every size on the board
is `rem` against the `calc(100vh / 67.5)` root — 1rem = 16px at 1080p, 32px at 4K — so the whole
board scales as one unit. Layout never reflows on data — zero-value chips and panels dim in
place. (**2026-07-23:** the board's text, label, and hairline colors were brightened for legibility
on a wall TV under office lighting; the near-black instrument-panel surfaces are unchanged — see
`wallboardTokens.ts`.)

### Z1 — HUD command bar

- **Left:** the white Werco logo, a hairline divider, and the board identity — `SHOP FLOOR` over
  `LIVE WALLBOARD // ALL WORK CENTERS` (or `// <DEPT>` under `?dept=`).
- **Center — the alert chips:** `N DOWN` (red), `N BLOCKED` (orange), `N LATE` (amber), driven by
  the true uncapped totals (`down_total` / `blocked_total` / `late_total`); against an old
  backend (totals absent) they fall back to the down work-center count and the capped list
  lengths — degraded but rendering. Dept-scoped totals drive them on a dept TV. A zero-count
  chip keeps its exact geometry and dims in place (slate text, hairline edge, no tinted fill, no
  glow). The DOWN chip's dot pulses opacity (1.6s ease-in-out) while `down > 0` — part of the
  only animation on the board.
- **Right:** the sync status — `SYNC OK` (green) / `SYNC STALE` (amber) / `SYNC LOST` (red), see
  Behavior — over `UPDATED h:mm` (the last successful poll), a divider, and the Central wall
  clock (1s tick; `h:mm` + AM/PM by default — see the display settings above).

### Z2 — Work-order grid

A **fixed 4×3 grid — always 12 card slots** — showing the first 12 of the server-sorted `jobs`
(the server sends ≤24). Population: every **RELEASED / IN_PROGRESS** WO — **ON_HOLD is
deliberately excluded** (the NCR/HOLD split row already counts holds); DRAFT and terminal
statuses are off the board as everywhere else.

- **Server-side priority sort** (the client never re-sorts): blocked/down first, then late —
  worst `days_late` first — then running, then everything else by promise date ascending (no
  promise sorts last); WO number breaks every tie. Overflow beyond the 12 visible cards goes to
  the full-width strip under the grid — `+N MORE WORK ORDERS IN QUEUE`, counted against the
  uncapped `jobs_total` (`ALL OPEN WORK ORDERS ON BOARD` when nothing is hidden). Fewer than 12
  jobs → trailing cells stay plain background; the grid geometry never changes. There are no
  density tiers, no tier hysteresis, and no grid math — the 4×3 shape is constant.
- **Card state** is classified client-side with strict precedence **DOWN > BLOCKED > LATE >
  RUNNING > WAITING** (`classifyJob`): DOWN = the current op's work center has an open downtime
  event; BLOCKED = any unresolved blocker on the WO, routed or not; RUNNING = the current op has
  open labor. The state drives the card's left status edge, its chip (the LATE chip carries the
  age: `LATE 14D`), its time value, its stop reason, and its progress-bar color. DOWN cards get
  a red-washed background; WAITING cards de-emphasize (grey edge, muted part number, no glow).
- **Current operation** = the WO's lowest-sequence IN_PROGRESS op, else its lowest READY op, else
  its lowest PENDING op — none when all ops are complete (the card then reads `ALL OPS
  COMPLETE`).
- **Card anatomy — five fixed rows:**
  1. WO number ←→ status chip (glowing dot + state word; only DOWN dots pulse);
  2. part number ←→ WO-level `done/ordered` qty;
  3. `OP n/total · <op name>` — **or the WO's customer name on an authorized (executive) board**
     (see "Customer names — gated"; public boards keep the op line, and an authorized board falls
     back to it for a WO with no customer) ←→ the state's **time value** — red downtime duration on
     DOWN, orange blocked age on BLOCKED, green elapsed cycle on RUNNING, muted elapsed on a LATE
     card that is also running (minutes tick client-side between polls);
  4. work center ←→ the **stop reason** — the downtime category on DOWN, the blocker category on
     BLOCKED, `IN QUEUE` on WAITING;
  5. a thin WO-level progress bar (`qty_complete / qty_ordered`) + percent.
- **Stoppage detail is joined client-side:** DOWN duration + category come from
  `work_centers[].down` via the current op's work-center code; BLOCKED age + category come from
  `blocked_wos[]` by WO number. When a join misses (e.g. a blocked WO that fell outside the
  capped `blocked_wos` list), the cell renders **blank** — a blank cell is part of the design,
  never an error.
- **Empty / degraded states:** nothing released or in progress → a calm full-zone `NO OPEN WORK
  ORDERS` panel, not an error. A payload with no `jobs` field at all (a backend predating the
  job wall) → a full-zone `BOARD DATA UNAVAILABLE — BACKEND UPDATE REQUIRED` state: **the
  machine-wall fallback is removed** — the Foundry board never renders work-center tiles. (The
  `work_centers` block still ships in full on every payload: old TV bundles render it as the
  pre-redesign machine wall, and the current board consumes its `down` blocks for the joins
  above.)

### Z3 — Right rail

A fixed **430px** column of four panels, each with a colored top accent bar. Rows are pinned
worst-first with a `+N MORE` count; every exception row leads with a fixed-width magnitude column
(`14D`, `38H`, `2H14M`) so severity scans vertically from across the shop. Zero-value panels keep
their slot and dim in place with a green zero-line (`ON TIME — NOTHING LATE`, `NOTHING BLOCKED OR
DOWN`) — the board visibly rewards a clean day without any layout reflow.

- **P1 SHIP TODAY** (blue top accent — brand, not status): the `shipped / due` fraction for **one
  population** — WOs *promised today* (promise = `must_ship_by || due_date`, the OTD precedence).
  Fraction color: **mute** when nothing is due, **green** once shipped ≥ due, **amber** when
  behind before 12:00 Central, **red** when still behind at/after noon. Up to 2 open due-today
  rows (`WO · part` ←→ `N LEFT` qty remaining, largest remaining first) + `+N MORE TODAY`; when
  nothing is promised today, `NEXT DUE <day> (N WOS)` instead of a bare zero. `THIS WEEK N`
  footer (promised in the next 7 days, not yet fully shipped). Plant-wide.
- **P2 LATE — OLDEST FIRST** (amber top accent; takes the rail's flexible height): the **true
  total** (`late_total`, never the capped list length) as the amber headline, then up to 6
  pinned rows worst-first: `14D  WO-0885  PLT-2093`. `+N MORE` footer against the true total.
  Dept-scoped under `?dept=`.
- **P3 BLOCKED / DOWN** (orange top accent): twin headline counts — `BLOCKED N` (orange) and
  `DOWN N` (red) — which dim to faint at zero but never disappear. Up to 4 rows, down work
  centers first (`2H14M  MILL-2  MAINTENANCE`, live minutes ticking between polls) then blocked
  WOs oldest-first (`38H  WO-1108  MATERIAL MISSING`), `+N MORE` against the true totals.
  Dept-scoped under `?dept=`.
- **P4 OPEN NCRS / ON HOLD:** a split row of two half panels — `OPEN NCRS` with a `NEWEST Nd AGO`
  sub-line (only when the age is known) and an amber count (dim at zero), and `ON HOLD` —
  **counts and ages only**, never NCR titles or free text (which can name customers/suppliers).
  Plant-wide.

### Z4 — TODAY KPI bar

A fixed-height footer panel: a lead cell — blue `TODAY` eyebrow over the live Central date — then
six equal hairline-divided cells: `OPS DONE`, `PIECES`, `ON CLOCK` (green), `LABOR HRS`
(1 decimal), `RECEIPTS`, `SCRAP EVENTS` (amber when > 0) — live, resetting at **Central
midnight**, from the `today` block. Semantics in the
payload section below. Missing block (old backend) → `—` values; the bar never disappears. (The
`PLANT 30d` KPI cluster that used to occupy the right ~40% of this band was removed on owner
feedback 2026-07-15 — see the KPI-strip deprecation note below.)

## Payload

`GET /shop-floor/wallboard` returns the whole board in one call. **Back-compat:** every block and
field added after A0.5 v1 is optional/defaulted — an old TV build ignores the new fields; the new
TV against an old backend renders `—` values and falls back to list lengths for the totals. All
blocks below share the payload's privacy posture: counts, ages, WO/part numbers and dates only —
no ship-to addresses, no dollar figures, no NCR text, operators as "First L.". Customer names are
the one **gated** exception (`jobs[].customer_name`, below) — populated only for an authorized
principal and redacted on every public board.

- **`jobs[]` / `jobs_total`** — the Z2 work-order grid. Population: open (**RELEASED /
  IN_PROGRESS**) WOs only — **ON_HOLD is deliberately excluded** (the NCR/HOLD split row counts
  holds). Server-side
  priority sort (blocked/down → most-late → running → promise date asc, WO number tiebreak),
  capped at **24**; `jobs_total` is the true uncapped count for `+N more`. Both are
  **dept-scoped** when `dept` is passed — a job belongs to a dept via its **current op's**
  work-center type. Each job carries `wo_number`, `part_number`, the **gated** `customer_name`
  (see the privacy note below), `status`, WO-level
  `qty_complete` / `qty_ordered`, `promise_date` (`must_ship_by || due_date`), `is_late` /
  `days_late` (the same shared lateness predicate as the rail — the card and the LATE panel
  cannot disagree), `blocked` (any unresolved blocker on the WO), `down` (current op's work
  center has an open downtime event), `running` (current op has ≥1 open labor entry),
  `ops_completed` / `ops_total`, and `current_op` — chosen by the IN_PROGRESS > READY > PENDING
  lowest-sequence precedence, `null` when all ops are complete — with `sequence`, `name`,
  `work_center_code` / `work_center_name`, `status`, `qty_done` / `qty_target`, `crew` (up to 3
  "First L." names), `crew_count` (true headcount), `elapsed_minutes` (earliest open clock-in).
  **Privacy:** a card carries WO/part/op identifiers, dates, quantities, and "First L." crew
  names only — never dollar figures or notes. `customer_name` is the ONE **gated** field:
  populated only for an authorized principal (a display token opted in via `show_customer_names`,
  or a signed-in Platform Admin / Admin / Manager), `None` on every public board — see "Customer
  names — gated". `jobs` is absent (`null`) only
  from a pre-job-wall backend, which makes the TV render the Z2 `BOARD DATA UNAVAILABLE` state
  (the machine-wall fallback is gone from the Foundry board).
- **`work_centers[].active_jobs[]`** (still shipped in full: old TV bundles render it as the
  pre-redesign machine wall, and the current board joins `work_centers[].down` for the card
  stop reasons/durations and the BLOCKED·DOWN rail rows) — **one row per
  operation** (crew-station grouping), not
  one per time entry: `crew` (up to 3 "First L." names), `crew_count` (true headcount for the
  `+N` suffix), `elapsed_minutes` (from the crew's earliest open clock-in), and server-computed
  `is_late`. `operator_name` is kept as a back-compat alias of `crew[0]`.
- **`late_wos[]` / `blocked_wos[]`** — server-ranked (late: worst-first; blocked: oldest-first),
  **capped at 12** (was 25 in the ticker era), and **dept-scoped** when `dept` is passed.
  `late_wos[].due_date` carries the **promise date** (`must_ship_by || due_date`) under the
  original field name for wire back-compat.
- **`late_total` / `blocked_total` / `down_total`** — true **uncapped** totals for the rail
  headlines and the HUD alert chips; dept-scoped with the lists. `None` (absent) from an old
  backend — never a fake 0.
- **"Late", everywhere on the board** = promise date (`coalesce(must_ship_by, due_date)`, the OTD
  precedence) strictly before today's **Central** date, on a live, non-terminal WO. One shared
  predicate drives the late list, `late_total`, and per-job `is_late`, so they cannot disagree.
- **`ship`** (plant-wide, Central-day window) — `due_today` = **all** WOs promised today (shipped
  or not); `shipped_today` = those already fully shipped (the analytics counted-shipment rules,
  cancelled WOs excluded) — one population, so the TV fraction is coherent; `due_this_week` =
  promised today..+6 days, not fully shipped; `due_today_rows` = top 2 open due-today WOs by
  quantity remaining (`wo_number`, `part_number`, `promise_date`, `qty_remaining` — deliberately
  nothing else); `next_due_date` / `next_due_count` when nothing is promised today.
- **`today`** (plant-wide, Central-midnight → now) — `ops_completed`; `pieces_completed`
  (RUN+REWORK quantity produced, **provenance-excluded**: backfill/import rows never masquerade
  as live capture); `wos_completed`; `operators_on_clock` (distinct users with an open time
  entry, **any** entry type); `hours_logged` (closed labor durations + open elapsed, attributed
  to the entry's start day); `receipts` (PO receipts); `scrap_events` (entries with scrap > 0,
  provenance-excluded). Aggregates only — nothing per-person.
- **`quality`** (plant-wide) — `open_ncr_count` (not closed/void), `newest_ncr_age_days`,
  `wos_on_hold`. Counts and ages only.
- **`kpi_strip`** — **deprecated, always `null`** (see the deprecation note below).
- **Best-effort blocks:** `ship` / `today` / `quality` are each computed independently; a failed
  block is `null` on that poll (and logged) — a broken panel never blanks the whole TV, and the
  endpoint stays a zero-write read. The `jobs` block is **core** like `work_centers` — computed
  inline, not best-effort.

## Behavior

- **Refresh:** polls every **30 seconds** (deliberately no WebSocket — reliability first).
  Numerals and progress bars update at poll boundaries; elapsed/downtime minute counters tick
  client-side between polls.
- **Offline (staged, never flashing):** the HUD sync status steps green `SYNC OK` → **steady
  amber `SYNC STALE`** after **1 failed poll** → **steady red `SYNC LOST`** after **4 consecutive
  failures** (~2 minutes), with `UPDATED h:mm` showing the last-good time throughout. The last
  good board stays on screen and recovers automatically on the next successful poll.
- **Motion budget:** the only things that move are the wall clock (1s), the minute counters
  between polls, and the **1.6s opacity pulse on DOWN dots** (the HUD chip's dot while
  `down > 0`, and DOWN card chip dots) — nothing else. The previous board's heartbeat,
  new-event flash, and payload-swap fade are gone (design rule: no ambient motion on data).
  Steady state never flashes — including offline. No marquees, no tickers, no rotation.
- **No scrolling, anywhere:** every list is capped server- or client-side worst-first with a
  `+N more` count, so anything hidden is by definition less severe than everything shown.
- **No token:** without a valid token (or signed-in session) the page shows guidance instead of
  data — it never redirects to login.
- **Revoked/expired token:** full-screen notice directing to a fresh setup code + `/tv`
  re-pairing; every stored display credential (the `sessionStorage` URL capture and the
  `localStorage` `/tv` claim) is dropped and polling stops.
- **Privacy:** operator names are truncated server-side to "First L." — the payload is built to be
  public-safe by default. The one gated exception is `customer_name` on each job tile (see "Customer
  names — gated"): redacted on every public / un-flagged display and for non-privileged signed-in
  roles; shown only to an opted-in executive display or a signed-in Platform Admin / Admin / Manager.
  A signed-in user can also open `/wallboard` (scoped to their active company).

## KPI strip — deprecated

The trailing-30-day floor KPI cluster (ship OTD / FPY / scrap / WIP count / WIP age — Lean
Phase 1, rendered as the `PLANT 30d` block in Z4) was **removed from the TV entirely** on owner
feedback (2026-07-15 job-wall redesign). The server no longer computes it — the compute path and
its ~5-minute per-company cache were deleted — and the payload's `kpi_strip` field is
**deprecated: always `null`**, kept only for wire back-compat (an old TV bundle renders its
em-dash empty cluster on `null`; the current board reads nothing from it). The underlying Lean
Phase 1 metric services are untouched — only the TV stopped consuming them.

## Security — treat the token like a password

A display token can **only** read the wallboard endpoint — it is rejected (401) everywhere else,
carries no user identity, and can write nothing. Still:

- The URL containing `#token=` grants wallboard access to whoever has it — share it only with
  whoever mounts the TV, and don't post it in chat/tickets.
- **If a TV is lost, stolen, or replaced, revoke its token** from Admin Settings → Wallboard
  Displays. Revocation is checked server-side on every request (the DB row, not the JWT, is
  authoritative), so the screen goes dark within one ~30s poll.
- **Treat setup codes like one-time passwords** — that is what they are: 8 chars of CSPRNG output
  (~40 bits over an unambiguous 31-symbol alphabet), **15-minute TTL, single use**, stored only as
  a SHA-256 hash (the plaintext is never persisted or logged). The claim endpoint
  (`POST /auth/display-token/claim`) is public by design (a pairing TV has no credential yet) but
  rate-limited (**10/minute per IP**) and answers **every** failure — unknown, used, or expired
  code, revoked or expired display — with the same generic 404, so it can't be probed as an
  oracle. Reissuing a code kills the previous one immediately. Same short-credential posture as
  the kiosk/visitor station PINs (`docs/KIOSK.md`, `docs/VISITOR_SIGNIN.md`): a short secret typed
  at the device mints a scoped token, and the DB row — never the JWT — stays the authority.
- **The claimed JWT is revocable exactly like before.** The claim re-mints the JWT from the
  `display_tokens` row (same `jti` / company / expiry as the issuance JWT), so the row remains the
  single revocation anchor — revoke the display and the TV goes dark within one ~30s poll no
  matter how it was paired.
- Issuance, revocation, setup-code reissue, and each successful claim are tamper-evidently
  audit-logged (the claim as a `CLAIM` event on the display's company, with the caller's
  IP/user-agent and no user identity — it's a TV, not a person; the code value and its hash never
  land on the audit chain). Label tokens clearly so the audit trail names the physical screen.
- Tokens expire (≤365 days). Re-issue and re-point the TV before expiry — expired tokens are
  rejected the same way as revoked ones.

## Reference

- Endpoints and threat model: `docs/API.md` → Authentication → Display tokens, and Shop Floor →
  wallboard callout.
- Role gating: `docs/RBAC_PERMISSIONS.md` → Admin.
- Implementation: `backend/app/api/deps.py` (`get_display_or_user`),
  `backend/app/services/display_token_service.py` (issue / setup-code reissue / public claim),
  `backend/app/services/wallboard_service.py` (payload builder + the shared lateness predicate),
  `frontend/src/pages/Wallboard.tsx`, `frontend/src/pages/TvPair.tsx` (the `/tv` pairing screen),
  `frontend/src/components/wallboard/` (zone components — `HudBar.tsx` (Z1), `WoGrid.tsx` /
  `WoCard.tsx` (Z2), `ShipTodayPanel.tsx` / `LatePanel.tsx` / `BlockedDownPanel.tsx` /
  `QualitySplitRow.tsx` (Z3), `TodayKpiBar.tsx` (Z4), and `wallboardTokens.ts` — the
  board-local Foundry palette, deliberately not the app shell's `--fd-*` variables),
  `frontend/src/utils/wallboardLayout.ts` (pure classification + formatting helpers only —
  `classifyJob`'s strict state precedence, duration/age/label formatting, dept title-casing;
  the grid math, machine-wall sort, and tier hysteresis were deleted with the old layout),
  `frontend/src/services/wallboardClient.ts`.
