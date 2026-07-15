# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5) — the "Andon
Wall". Four fixed zones: a computed shop-state headline, an alarm-first grid of work-center tiles
with an idle strip, a four-panel exception rail (SHIP / LATE / BLOCKED·DOWN / QUALITY), and a
TODAY + trailing-30-day band. Nothing on the board scrolls, rotates, or requires interaction —
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

- **Dept-scoped:** the work-center tiles, the LATE and BLOCKED·DOWN rail panels — both their
  visible rows **and** their true totals (`late_total` / `blocked_total` / `down_total`) — and
  therefore the Z1 hero headline (a machining TV headlines machining's truth). Attribution rules:
  a WO is *late for a dept* when it has ≥1 open (non-complete) operation routed to a work center
  of that type; a blocker belongs to a dept via **its operation's** work center; a work center is
  *down for a dept* by its own type.
- **Consequences:** a late WO with open operations in two departments appears on **both** dept
  TVs; a blocker filed without an operation (and a late WO with no routed open operations) cannot
  be dept-attributed and shows only on the **unfiltered** board.
- **Always plant-wide:** the SHIP panel, the QUALITY panel, the TODAY cells, and the 30-day KPI
  cluster. The SHIP and QUALITY panels render a small `PLANT` tag whenever `dept` is set so a
  dept TV never mislabels company-wide numbers as departmental.

## Layout — the four zones

Fixed geography (1080p-relative): header 9% / body 82% (floor wall 72.5% wide + exception rail
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

### Z2 — Floor wall

A deterministic grid of **non-idle** work centers (idle = no active jobs, no downtime, nothing
blocked). Grid shape always exactly fills: `rows = max(1, round(sqrt(N/1.6)))`,
`cols = ceil(N/rows)`; trailing empty cells render as plain background, bottom-right.

- **Alarm-first sort:** DOWN → BLOCKED → RUNNING-LATE → RUNNING, alphabetical within class. A
  tile only moves when its state *class* changes (instant reposition at a poll boundary).
- **Tile anatomy:** the header is a **solid filled color band** — the andon signal (green
  RUNNING, red DOWN, orange `N BLOCKED`, amber LATE; black text on fills; state is never a
  hairline border) — with a queue chip that escalates (hidden at 0, plain 1–4, amber-filled ≥5,
  red-outline ≥10). A down tile shows its blocker category + live downtime duration (minutes tick
  client-side between polls). Job rows are **one row per operation** (server crew-grouped): part
  number leads (amber + `LATE Nd` chip when the WO is late), qty done/target right; secondary
  line `WO · op · First L. +N` crew label with elapsed time right; a thin progress bar underneath
  (green, amber when late).
- **Density tiers** (per-tile job-row budget by active-tile count): ≤6 → 3 rows, 7–12 → 2 rows,
  13–20 → 1 row; the rest collapse into `+N more`. Tier switches are damped with 2-poll
  hysteresis so a center flapping in/out of idle can't thrash the wall. Above 20 active centers
  the documented answer is one `?dept=` TV per department (the spec's last-resort pagination is
  deliberately not implemented).
- **Idle strip** (bottom of the wall, hidden when nothing is idle): idle centers collapse into
  dim slate chips with their queue counts — `IDLE 3 — SAW-2 Q1 · DEBURR Q0 …`, capped + `+N more`.
- **All centers idle:** the wall renders a single large dim `FLOOR IDLE` panel (with the hero
  reading `OFF SHIFT` when nobody is on the clock). A dark, calm screen is the designed reward.
- **Zero work centers** (bad `dept` value): full-zone "No active work centers" empty state.

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

### Z4 — TODAY / 30-day band

- **Left ~60% — `TODAY`** (live, resets at **Central midnight**, from the `today` block): six
  cells — `OPS DONE`, `PIECES`, `ON CLOCK`, `HRS`, `RECEIPTS`, `SCRAP EVT`. Semantics in the
  payload section below. Missing block (old backend) → `—` values; the band never disappears.
- **Right ~40% — `PLANT 30d`** behind a heavier double rule and an explicit scope label (it stays
  company-wide even on a dept TV): the trailing-30-day OTD / FPY / Scrap percentages with small
  threshold swatches beside each value (color banding lives on the swatch, never the value), plus
  `WIP N · N.Nd avg`. Same `kpi_strip` block and null discipline as before (see KPI strip below).

## Payload

`GET /shop-floor/wallboard` returns the whole board in one call. **Back-compat:** every block and
field added after A0.5 v1 is optional/defaulted — an old TV build ignores the new fields; the new
TV against an old backend renders `—` values and falls back to list lengths for the totals. All
blocks below share the payload's privacy posture: counts, ages, WO/part numbers and dates only —
no customer names, no ship-to addresses, no dollar figures, no NCR text, operators as "First L.".

- **`work_centers[].active_jobs[]`** — now **one row per operation** (crew-station grouping), not
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
- **`kpi_strip`** — unchanged (see below).
- **Best-effort blocks:** `ship` / `today` / `quality` / `kpi_strip` are each computed
  independently; a failed block is `null` on that poll (and logged) — a broken panel never blanks
  the whole TV, and the endpoint stays a zero-write read.

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

## KPI strip (Lean Phase 1)

The trailing-30-day floor KPIs, from the optional `kpi_strip` block — now rendered as the
**PLANT 30d** cluster on the right of the Z4 band (v1 rendered it as a strip across the top):

| Tile | Field | Meaning |
|------|-------|---------|
| OTD | `otd_ship_pct_30d` | Ship-based on-time delivery — full ordered quantity shipped on/before promise |
| FPY | `fpy_pct_30d` | Overall first-pass yield across completed operations |
| Scrap | `scrap_pct_30d` | Scrapped ÷ (complete + scrapped) across completed operations |
| WIP | `open_wip_count` | Open released WOs (released / in-progress / on-hold) — live, not windowed |
| avg | `avg_wip_age_days` | Mean days since release of those open WOs — live, not windowed |

- **Trailing 30 days.** The three percentages cover the last 30 days ending today; the two WIP
  figures are a live snapshot.
- **Nulls render as "—".** A percentage is `null` when the window has insufficient data (empty
  denominator) — the board shows an em dash, never a fake 0% or 100%.
- **~5-minute staleness is by design.** The strip is server-side cached per company (~5-min TTL) so
  the 30 s poll doesn't recompute analytics; trailing-30-day numbers don't move faster than that.
  The live board panels are unaffected — only the strip is cached.
- **Company-wide, not per-department.** `&dept=` narrows the work-center tiles and the late/blocked
  rail but **not** this cluster — every TV shows the same plant-level KPIs, labeled `PLANT 30d`.
- **Best-effort.** Aggregate numbers only (nothing operator-identifying). If the KPI computation
  fails, `kpi_strip` is `null` on that poll and the board renders `—` values — an analytics
  error never takes down the live board (the block is also optional on the payload, so a board
  pointed at an older backend renders unchanged). The endpoint stays a zero-write read and the
  display-token model below is unchanged.

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
  `frontend/src/components/wallboard/` (zone components),
  `frontend/src/utils/wallboardLayout.ts` (grid math / sort / tier hysteresis),
  `frontend/src/hooks/useNewEventFlash.ts`, `frontend/src/services/wallboardClient.ts`.
