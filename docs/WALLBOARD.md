# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5) — the "Andon
Wall". Four fixed zones: a computed shop-state headline, a priority-sorted **job wall** of open
work-order tiles (each showing its current operation; the pre-redesign machine wall remains as
the old-backend fallback), a four-panel exception rail (SHIP / LATE / BLOCKED·DOWN / QUALITY),
and a TODAY band. Nothing on the board scrolls, rotates, or requires interaction —
every zone has a fixed capacity, a `+N more` overflow, and a designed empty state, and every panel
keeps its slot at all data values so a habitual glance lands on a memorized coordinate.

This is **not** the interactive operator kiosk (`/shop-floor/operations?kiosk=1`, badge login —
see `docs/onboarding/02-operator-shop-floor.md`). The wallboard takes no input and writes nothing;
it authenticates with a **scoped display token** instead of a user session.

## Setting up a TV

1. **Issue a display** — Admin Settings → **Wallboard Displays** tab → New display. Give it a
   label naming the physical screen ("North wall TV"), a lifetime (default 90 days, max 365), and
   — for a one-department TV — an optional **department preset** (a work-center type, e.g.
   `machining`). The UI is on the Admin Settings page (admin-gated); the API also allows Manager
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
   `rem` off a viewport-scaled root (1rem = 16px at 1080p, 32px at 4K — identical angular size),
   with a 2% safe-area pad for TV overscan; verify legibility on the actual hardware at viewing
   distance, especially the orange (BLOCKED) vs amber (LATE) discrimination.

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
the header chip renders it title-cased.

**What `dept` scopes — and what it never scopes:**

- **Dept-scoped:** the **job wall** — a work order belongs to a dept TV via its **current
  operation's** work-center type (a WO whose ops are all complete has no current op and drops off
  dept boards) — the work-center tiles (fallback wall), and the LATE and BLOCKED·DOWN rail panels
  — both their visible rows **and** their true totals (`late_total` / `blocked_total` /
  `down_total`) — and therefore the Z1 hero headline (a machining TV headlines machining's
  truth). Rail attribution rules: a WO is *late for a dept* when it has ≥1 open (non-complete)
  operation routed to a work center of that type; a blocker belongs to a dept via **its
  operation's** work center; a work center is *down for a dept* by its own type.
- **Consequences:** a late WO with open operations in two departments appears on **both** dept
  TVs' LATE rails, but its job tile shows only where its *current* op lives; a blocker filed
  without an operation (and a late WO with no routed open operations) cannot be dept-attributed
  and shows only on the **unfiltered** board. A job tile's BLOCKED band is **WO-level**
  (any unresolved blocker, wherever it's routed), so a dept TV can legitimately show a BLOCKED
  tile while its dept-scoped hero/rail count blockers routed elsewhere — the tile answers
  "can this job proceed?", the rail answers "what's stuck in *this* department?".
- **Always plant-wide:** the SHIP panel, the QUALITY panel, and the TODAY cells. The SHIP and
  QUALITY panels render a small `PLANT` tag whenever `dept` is set so a dept TV never mislabels
  company-wide numbers as departmental.

## Layout — the four zones

Fixed geography (1080p-relative): header 9% / body 82% (job wall 72.5% wide + exception rail
27.5%) / bottom band 9%. Layout never reflows on data — zero-value panels dim in place.

### Z1 — Header

- **Left:** `WERCO·FLOOR` wordmark + the title-cased dept chip (hidden when no `?dept=`).
- **Center — the hero headline:** one computed shop-state sentence with a status dot, from the
  true totals: red `● 1 DOWN · 3 BLOCKED · 12 LATE` when anything is down (zero-count segments
  omitted), orange/amber when only blocked/late, green `● ALL SYSTEMS NORMAL` when all three are
  zero, dim slate `● OFF SHIFT` when additionally `today.operators_on_clock` is 0 and no jobs are
  active. Dept-scoped totals drive it on a dept TV. Against an old backend (totals absent) the
  hero derives counts from `work_centers` + the capped lists — degraded but rendering.
- **Heartbeat:** the status dot pulses opacity on a 2s cycle while the board is live and
  **freezes when offline** — a stopped heartbeat is the staleness cue.
- **Right:** `Updated <time>` + a Central wall clock (1s tick), and the offline chip (below).

### Z2 — Job wall

A deterministic grid of **open work-order tiles** (owner feedback 2026-07-15: the main wall shows
work orders with their current operation, not machines). Population: every **RELEASED /
IN_PROGRESS** WO — **ON_HOLD is deliberately excluded** from the wall (the QUALITY rail panel
already counts holds); DRAFT and terminal statuses are off the board as everywhere else. Grid
shape always exactly fills: `rows = max(1, round(sqrt(N/1.6)))`, `cols = ceil(N/rows)`; trailing
empty cells render as plain background, bottom-right.

- **Server-side priority sort** (the client never re-sorts): blocked/down first, then late —
  worst `days_late` first — then running, then everything else by promise date ascending (no
  promise sorts last); WO number breaks every tie. The server caps the wall at **24** tiles;
  `jobs_total` carries the true uncapped count for the `+N more work orders` line.
- **Current operation** = the WO's lowest-sequence IN_PROGRESS op, else its lowest READY op, else
  its lowest PENDING op — none when all ops are complete.
- **Tile anatomy:** the header is a **solid filled color band** — the andon signal (state is
  never a hairline border) — carrying the WO number and a state word, strict precedence red
  DOWN > orange BLOCKED > amber LATE > green RUNNING > slate WAITING (black text on fills).
  DOWN = the current op's work center has an open downtime event (the body then leads with red
  `MACHINE DOWN · <work center>`); BLOCKED = any unresolved blocker on the WO, routed or not;
  RUNNING = the current op has open labor. Body: the part number leads (amber + `LATE Nd` chip
  when the WO is late), WO-level `done/ordered` qty right; the second line answers "what op is it
  on?" — `Op n/total · <op name> · <work center>` with a `First L. +N` crew suffix when someone
  is clocked in — with live elapsed time right while running (minutes tick client-side between
  polls); a thin WO-level progress bar underneath (green, amber when late). A tile newly entering
  DOWN/BLOCKED flashes its fill for ~10s, then settles steady.
- **Density tiers** (by tile count, header height only in job mode) are damped with the same
  2-poll hysteresis as before, so a WO flapping on/off the wall can't thrash the grid. **No idle
  strip in job mode** — idle machines surface only through the exception rail.
- **Empty state:** nothing released or in progress → a calm full-zone `No open work orders`
  panel, not an error.

**Machine-wall fallback (old backends).** When the payload has no `jobs` field (a backend that
predates the job wall), the page renders the previous machine wall unchanged — the alarm-first
grid of non-idle work-center tiles with per-operation crew-grouped job rows, queue chips, the
3/2/1 job-row density tiers, the idle-chip strip, and the `FLOOR IDLE` all-idle state. The
`work_centers` block still ships in full on every payload, so old TV bundles pointed at a new
backend keep rendering the machine wall too.

### Z3 — Exception rail

Four **fixed** panels (SHIP 22% / LATE 30% / BLOCKED·DOWN 28% / QUALITY 20% of the rail height),
replacing the v1 rotating ticker outright — nothing in the rail rotates. Rows are pinned
worst-first with a `+N more` count; every exception row leads with a fixed-width magnitude column
(`14d`, `38h`, `2h14m`) so severity scans vertically from across the shop. Zero-value panels keep
their slot and dim in place with a green zero-line (`LATE 0 — ON TIME`, `NOTHING BLOCKED OR
DOWN`); when late, blocked, and down are all zero the zero-lines render large — the board visibly
rewards a clean day without any layout reflow.

- **P1 SHIP** (navy header rule — brand, not status): the `shipped / due` fraction for **one
  population** — WOs *promised today* (promise = `must_ship_by || due_date`, the OTD precedence).
  Sub-line: amber `N TO GO` when behind, green `COMPLETE` when everything promised today has
  shipped, dim `NONE DUE` when nothing is promised today. The fraction turns **red when
  incomplete due-today WOs remain past 12:00 Central**. Also `THIS WEEK N` (promised in the next
  7 days, not yet fully shipped), then up to 2 open due-today rows (`WO · part`, largest
  remaining first) + `+N more today`. Empty state: `Next due: <day> (N WOs)` instead of a bare
  zero. Plant-wide; `PLANT` tag under `?dept=`.
- **P2 LATE:** the **true total** (`late_total`, never the capped list length) as the amber
  headline, then up to 6 pinned rows worst-first: `14d  WO-0991  4471-002`. `+N more` footer.
  Dept-scoped under `?dept=`.
- **P3 BLOCKED·DOWN:** twin headline counts — `BLOCKED N` (orange) and `DOWN N` (red; the DOWN
  half never disappears, it dims to slate at zero). Up to 5 rows, down centers first
  (`2h14m  MILL-2 · MAINTENANCE`) then blocked WOs oldest-first (`38h  WO-1077 · WAITING
  MATERIAL`), `+N more`. Dept-scoped under `?dept=`.
- **P4 QUALITY:** `OPEN NCRs N · newest Nd` and `ON HOLD N` — **counts and ages only**, never NCR
  titles or free text (which can name customers/suppliers). Amber-filled value chips when >0, dim
  slate zeros. Plant-wide; `PLANT` tag under `?dept=`.

### Z4 — TODAY band

Six full-width hairline-divided cells — `OPS DONE`, `PIECES`, `ON CLOCK`, `HRS`, `RECEIPTS`,
`SCRAP EVT` — live, resetting at **Central midnight**, from the `today` block. Semantics in the
payload section below. Missing block (old backend) → `—` values; the band never disappears. (The
`PLANT 30d` KPI cluster that used to occupy the right ~40% of this band was removed on owner
feedback 2026-07-15 — see the KPI-strip deprecation note below.)

## Payload

`GET /shop-floor/wallboard` returns the whole board in one call. **Back-compat:** every block and
field added after A0.5 v1 is optional/defaulted — an old TV build ignores the new fields; the new
TV against an old backend renders `—` values and falls back to list lengths for the totals. All
blocks below share the payload's privacy posture: counts, ages, WO/part numbers and dates only —
no customer names, no ship-to addresses, no dollar figures, no NCR text, operators as "First L.".

- **`jobs[]` / `jobs_total`** — the Z2 job wall. Population: open (**RELEASED / IN_PROGRESS**)
  WOs only — **ON_HOLD is deliberately excluded** (the QUALITY rail counts holds). Server-side
  priority sort (blocked/down → most-late → running → promise date asc, WO number tiebreak),
  capped at **24**; `jobs_total` is the true uncapped count for `+N more`. Both are
  **dept-scoped** when `dept` is passed — a job belongs to a dept via its **current op's**
  work-center type. Each job carries `wo_number`, `part_number`, `status`, WO-level
  `qty_complete` / `qty_ordered`, `promise_date` (`must_ship_by || due_date`), `is_late` /
  `days_late` (the same shared lateness predicate as the rail — the tile and the LATE panel
  cannot disagree), `blocked` (any unresolved blocker on the WO), `down` (current op's work
  center has an open downtime event), `running` (current op has ≥1 open labor entry),
  `ops_completed` / `ops_total`, and `current_op` — chosen by the IN_PROGRESS > READY > PENDING
  lowest-sequence precedence, `null` when all ops are complete — with `sequence`, `name`,
  `work_center_code` / `work_center_name`, `status`, `qty_done` / `qty_target`, `crew` (up to 3
  "First L." names), `crew_count` (true headcount), `elapsed_minutes` (earliest open clock-in).
  **Privacy:** a job tile carries WO/part/op identifiers, dates, quantities, and "First L." crew
  names only — never customer names, dollar figures, or notes. `jobs` is absent (`null`) only
  from a pre-job-wall backend, which makes the TV fall back to the machine wall.
- **`work_centers[].active_jobs[]`** (the machine wall — still shipped in full: old TV bundles
  render it, and a new TV renders it as the fallback when `jobs` is absent) — **one row per
  operation** (crew-station grouping), not
  one per time entry: `crew` (up to 3 "First L." names), `crew_count` (true headcount for the
  `+N` suffix), `elapsed_minutes` (from the crew's earliest open clock-in), and server-computed
  `is_late`. `operator_name` is kept as a back-compat alias of `crew[0]`.
- **`late_wos[]` / `blocked_wos[]`** — server-ranked (late: worst-first; blocked: oldest-first),
  **capped at 12** (was 25 in the ticker era), and **dept-scoped** when `dept` is passed.
  `late_wos[].due_date` carries the **promise date** (`must_ship_by || due_date`) under the
  original field name for wire back-compat.
- **`late_total` / `blocked_total` / `down_total`** — true **uncapped** totals for the rail
  headlines and hero sentence; dept-scoped with the lists. `None` (absent) from an old backend —
  never a fake 0.
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
  endpoint stays a zero-write read. The job wall is **core** like `work_centers` — computed
  inline, not best-effort.

## Behavior

- **Refresh:** polls every **30 seconds** (deliberately no WebSocket — reliability first).
  Numerals and progress bars transition at poll boundaries; elapsed/downtime minute counters tick
  client-side between polls.
- **Offline (staged, never flashing):** after **1 failed poll** a **steady amber** chip appears —
  `OFFLINE — data as of <time>` — and the heartbeat dot freezes; after **4 consecutive failures**
  (~2 minutes) the chip escalates to a **steady red fill**. The last good board stays on screen
  throughout and recovers automatically on the next successful poll.
- **Motion budget:** the only things that move are the wall clock, the minute counters, the
  heartbeat, poll-boundary value transitions, and a ~10s **new-event flash** on a tile newly
  entering DOWN/BLOCKED or a rail row newly appearing (diffed by stable ids; suppressed on first
  paint and on `?dept=` change, so a fresh load never lights up the whole board). Steady state
  never flashes — including offline. No marquees, no tickers, no rotation.
- **No scrolling, anywhere:** every list is capped server- or client-side worst-first with a
  `+N more` count, so anything hidden is by definition less severe than everything shown.
- **No token:** without a valid token (or signed-in session) the page shows guidance instead of
  data — it never redirects to login.
- **Revoked/expired token:** full-screen notice directing to a fresh setup code + `/tv`
  re-pairing; every stored display credential (the `sessionStorage` URL capture and the
  `localStorage` `/tv` claim) is dropped and polling stops.
- **Privacy:** operator names are truncated server-side to "First L." — the payload is built for
  a public screen. A signed-in user can also open `/wallboard` (scoped to their active company).

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
  `frontend/src/components/wallboard/` (zone components — `JobWall.tsx` / `JobTile.tsx` for Z2,
  `FloorGrid.tsx` for the machine-wall fallback),
  `frontend/src/utils/wallboardLayout.ts` (grid math / job-state classification / machine-wall
  sort / tier hysteresis),
  `frontend/src/hooks/useNewEventFlash.ts`, `frontend/src/services/wallboardClient.ts`.
