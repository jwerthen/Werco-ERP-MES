# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5), built to the
high-fidelity **"Foundry"** TV design (2026-07-22 redesign — near-black instrument panel,
JetBrains Mono, tabular numerals, authored at 1920×1080). Four fixed zones: a **HUD command bar**
(DOWN / BLOCKED / LATE alert chips, sync status, Central wall clock), a fixed **4×3 grid of
work-order cards** — row 1 pinned to the four most severe jobs, rows 2–3 cycling through the rest
on a 22s dwell — over a strip carrying the overflow copy and a segmented page bar, a **430px right
rail** (SHIP TODAY, LATE — OLDEST FIRST, BLOCKED·DOWN, OPEN NCRS + ON HOLD), and a **TODAY KPI
footer**. Nothing on the board scrolls or requires interaction — every zone has a fixed capacity,
a `+N more` overflow, and a designed empty state, and every panel keeps its slot at all data
values so a habitual glance lands on a memorized coordinate. The one thing that changes place is
the *content* of grid rows 2–3, and only there (see "Z2 — Work-order grid").

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
   vs amber (LATE) discrimination. The board also requests a Screen Wake Lock on load (see
   `docs/KIOSK.md` → Screen wake lock) but treat it as best-effort: smart-TV browsers often
   lack the API, so keep device sleep off regardless.

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

> **Scope of this rule.** It governs the **wallboard payload**. `docs/API.md` and `docs/KIOSK.md`
> cite `wallboard_service`'s privacy docstring as the precedent for what an unattended shop screen
> should show, and it still is — but it is not a repo-wide guarantee about station principals. The
> kiosk carries **one recorded exception** (owner decision, 2026-08-14): the crew station's queue
> read sends the job's five office-authored free-text guidance fields to a shared-PIN station. See
> [docs/KIOSK.md](KIOSK.md) → "Disclosure: this free text does reach a crew station" for the
> reasoning and the cost — the `show_customer_names` mechanism below is the pattern that decision
> considered and deliberately deferred. **Nothing about the wallboard payload changed.**

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

### Unit # — ungated

`jobs[].unit_number` (migration `083`) is the **Unit #** a one-unit-per-work-order job builds — the
weld assemblies. It is **not gated** and is populated for every principal, public boards included.
That is not a second exception to the rule above — but the reason is **category parity, not
length**. `String(50)` bounds how much text arrives, not what kind: fifty characters is ample for a
customer name, and nothing validates the content. What makes it showable is that it is the same
*category* as fields this board already renders publicly — `part_number`, and the operation and
work-center names — all office-entered and purpose-labeled. `notes` is withheld because it is
unbounded text of an **unconstrained** category, not merely because it is long. Making the number
showable on the wall is the reason the column exists rather than the note being surfaced.

**The residual, stated plainly.** The field is not validated, so an office user who types a customer
name into Unit # puts it on the public TV. The mitigation is convention plus disclosure-at-entry —
the work-order create form names the destinations under the field — and **not** enforcement. Do not
wave a future field onto this payload on the strength of "it's bounded"; ask whether it belongs to
one of the categories already here.

`customer_name` remains the **one** gated field on the payload. On a card, the two never compete for
space: the unit takes **row 2**'s large slot (the part number steps down beneath it), while the
customer name — when authorized — replaces **row 3**'s op line. The unit renders in the board's
**cyan** accent, deliberately none of the five status colors, so a build number can never be
misread as a state from across the shop.

A work order that tracks no unit sends `null` and its card renders exactly as it did before, so no
existing board changed. Set the number on the work order (create form, or the work-order detail
page); there is no per-display setting for it.

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

A **fixed 4×3 grid — always 12 card slots** — rendering the server-sorted `jobs` (the server
sends ≤24). Population: every **RELEASED / IN_PROGRESS / ON_HOLD** WO; DRAFT and terminal
statuses are off the board as everywhere else. **ON_HOLD joined the wall 2026-08-19** (owner
decision): a count on the Z3 rail is not a tile — a held WO used to vanish from the only surface
that says *which* job stopped and where — but it **sorts to the BACK** (below), so it can never
crowd actionable work off the top of the board.

- **Server-side priority sort** (the client never re-sorts): **ACTIVE work first** — blocked/down,
  then late (worst `days_late` first), then running, then everything else by promise date
  ascending (no promise sorts last), WO number breaking every tie — and **HELD work strictly
  last**. The alarm classes are therefore a **contiguous prefix** of the order, which is exactly
  what lets the TV pin `jobs[0:4]` while the rest of the grid rotates. Fewer than 12 jobs →
  trailing cells stay plain background; the grid geometry never changes. There are no density
  tiers, no tier hysteresis, and no grid math — the 4×3 shape is constant.
- **Anchor row + rotating field (2026-08-19)** — the grid is no longer "the first 12". **Row 1
  (slots 1–4) is the ANCHOR**: `jobs.slice(0, 4)`, re-derived live from the freshest payload every
  render, never paged. **Rows 2–3 (slots 5–12) are the FIELD**: an 8-wide window over
  `jobs.slice(4)` that flips on a **22-second dwell**, so every delivered work order reaches the
  wall instead of only the first twelve. Anchor + field page 0 is exactly `jobs[0..11]` — today's
  board, card for card — so a job can never be shown twice or silently skipped. The math is pure
  and unit-tested in `frontend/src/utils/wallboardLayout.ts` (`planFieldPages` / `fieldWindow` /
  `safeMod` / `stripCopy`); the policy is `components/wallboard/useWoCycle.ts`.
  - **The flip is a hard swap, not motion**: no fade, slide, transform, CSS transition or new
    `@keyframes`. It has to be — the global `prefers-reduced-motion` block in
    `styles/accessibility.css` forces `animation-duration: 0.01ms !important` on `*`, so a
    CSS-driven carousel would freeze on page 0 forever on any TV reporting reduced motion. Only
    the 8 field cards unmount/mount; the 4 anchor cards keep their React key and their DOM nodes,
    so a DOWN card's 1.6s `fdPulse` never resets phase.
  - **Static band — the board moves if and only if something would otherwise be hidden.**
    `pages = 1` exactly when the field fits, i.e. for every delivered count ≤ 12, and a one-page
    board is byte-identical to its pre-cycle self. From **13** delivered jobs up it cycles. An
    earlier cut held still until 16, refusing to page when a flip would displace 8 cards to reveal
    fewer than 4; **the owner overruled that on 2026-08-19** — a job the floor cannot see is the
    failure this feature exists to fix, whatever the motion economics.
    **The accepted cost lives at 13–15 and is worth saying out loud:** `starts` is flush-clamped so
    both pages stay FULL (never a row of holes), which at `F = 9..11` leaves the two windows
    overlapping by 5–7 of their 8 cards — the flip reads as the field **shifting by one to three
    slots** rather than turning a clean page. That is the least legible flip the board can produce,
    and it is still the best option at those counts: a short page would blank 4–7 cells for a whole
    dwell, and disjoint full pages are arithmetically impossible when `F` is barely over 8. From 16
    delivered jobs the stride is a clean 4 — exactly one grid row.
  - **Cadence and phase.** `slot = floor(now / 22_000)` is derived from the board's existing 1s
    clock tick — **no new timer** — so every TV in the building stays in phase and a throttled or
    occluded tab self-corrects on resume. 22s is chosen against the 30s poll (LCM 330s): a 20s or
    30s dwell would coincide with the poll once a minute and teach viewers to attribute every data
    change to the flip. Worst-case wait for a specific job is `(pages − 1) × 22s` — 22s at 16
    delivered, 44s at the 24 cap.
  - **Stability.** The page plan freezes `wo_number` **strings** only and resolves them through the
    freshest payload on every render, so elapsed clocks keep ticking and a card never renders
    RUNNING while the HUD chip beside it says DOWN. The plan key is the delivered **set**, order-
    insensitive **within each half of the board** (`anchor|field`): a reorder *inside* the frozen
    field or *inside* the anchor row — the `running` flag flips across the whole shop at every lunch
    and shift change — rebuilds **nothing**, cards recolor in place and none move. A reorder that
    carries a job **across** the anchor/field boundary is a different matter and deliberately does
    rebuild (at the next boundary, phase preserved): the anchor is live while the field is frozen,
    so the job lifted *into* row 1 is nulled out of its frozen field slot while the job it
    *displaced* is in neither half — under a whole-list key that job would be rendered on **no
    cell at all**, indefinitely, because the delivered set never changed. A set change rebuilds at
    the **next** dwell boundary, preserving the cycle position — except on a **single-page** plan,
    which rebuilds immediately (there is no cycle position to protect, and deferring would leave the
    strip claiming `ALL OPEN WORK ORDERS ON BOARD` for a dwell while the 13th job was on no screen)
    and when the page currently on screen resolves to **nothing at all** (a page of blanks reads as
    broken; the cold start and a wholesale population replacement are special cases). A newly
    **blocked or down** WO rebuilds immediately and snaps the field to page 0 — **edge-triggered**,
    so a machine down for three hours fires once (LATE is excluded from the snap: `days_late` steps
    in a batch at Central midnight; **HELD work orders are excluded too** — the server sorts them
    strictly last, so snapping to page 0 for one would lurch every TV in the plant to show something
    two pages away). A frozen `wo_number` that has left the payload renders a **plain cell in
    place** — survivors do not reflow — and heals at the next boundary.
  - **Degradation.** `nightDim` **freezes** the cycle at page 0 (the board is declaring nobody is
    looking, and page 0 is the board people already know). **Offline keeps cycling** on last-known-
    good data: paging is not a freshness claim, the staged `SYNC OK → STALE → LOST` chip is the
    disclosure, and freezing would hide two-thirds of the population with no visible cause.
    `?dept=` boards page against **their own** delivered count, so **a department TV under 13 jobs
    never cycles while the plant TV beside it does**. That is correct — a board with nothing hidden
    should not move — but two screens behaving differently in one building *will* be reported as a
    bug, as will the related fact that per-dept `jobs_total` values do **not** sum to the plant
    total (a WO whose current op is `None` drops off every dept board).
- **The strip under the grid** keeps its exact height, chrome and slot. Left: walk-up copy. Right:
  a **segmented page bar** (one segment per page, current filled `FD.ink`, the rest `FD.faint`)
  rendered **only while the board is cycling** — non-text on purpose, so from 5 m a viewer sees how
  many pages exist and which is lit without resolving a glyph. The **unlit** segments are
  `FD.faint`, not the `FD.line` hairline: at 1.48:1 on the panel the track was not resolvable at TV
  distance, which left a lone lit dash — a change *flash* rather than a page indicator, and no way
  to count the pages at all; it changes state once per dwell, so it costs the motion budget
  nothing. There is deliberately **no countdown/progress bar** (a bar whose whole semantic is "the
  view changes in N seconds" is ambient motion on data). Five copy states:
  1. no jobs → no strip at all (the `NO OPEN WORK ORDERS` zone owns the space);
  2. static, nothing hidden → `ALL OPEN WORK ORDERS ON BOARD`;
  3. static, some hidden → `+N MORE WORK ORDERS IN QUEUE` — and since the board now cycles the
     moment anything would be off-screen, a single-page board is single-page *because everything
     fits*, so this `+N` can only ever be the tail the **server** truncated at its 24-job cap;
  4. cycling, nothing truncated → `TOP 4 PINNED · PAGE i/N · n OPEN WORK ORDERS`;
  5. cycling, past the 24 cap → `TOP 4 PINNED · PAGE i/N · n OF total OPEN WORK ORDERS · +R NOT ON
     BOARD`.
  Two of those are correctness, not taste: **`+N MORE … IN QUEUE` is never emitted while cycling**,
  so the phrase keeps exactly one meaning across the screen — "permanently hidden and strictly less
  severe" — and `+R NOT ON BOARD` is deliberately different wording for the genuinely truncated
  tail; and the word is **`PINNED`, never `HELD`**, now that ON_HOLD work orders are on the wall.
- **Card state** is classified client-side with strict precedence **HELD > DOWN > BLOCKED > LATE >
  RUNNING > WAITING** (`classifyJob`): HELD = the WO's `status` is `on_hold` (there is **no `held`
  boolean on the wire** — the tile carries the hold on the existing `status` field); DOWN = the
  current op's work center has an open downtime
  event; BLOCKED = any unresolved blocker on the WO, routed or not; RUNNING = the current op has
  open labor. The state drives the card's left status edge, its chip (the LATE chip carries the
  age: `LATE 14D`), its time value, its stop reason, and its progress-bar color. DOWN cards get
  a red-washed background; WAITING **and HELD** cards de-emphasize (grey edge, muted part number,
  no glow). **HELD leads the precedence on purpose**: a held WO that is also down, blocked, late or
  running still reads `HELD`, greys out, and **never pulses or takes the DOWN red wash** — it is
  deliberately stopped and somebody already knows, so it must not spend the alarm channel. The
  cross-zone reading that follows is intended, not a bug: a held-and-blocked WO still counts in the
  HUD `BLOCKED` chip and still rides the Z3 blocked rail, so a viewer can read `BLOCKED 3` and find
  two orange cards — the third is the grey `HELD` card at the back of the board, and the rail is the
  disclosure. **The same holds for LATE, and it is the sharper case**: `_late_wo_filters` excludes
  only terminal statuses, so a held-and-late WO keeps its `late_total` count and its *dated* row on
  the Z3 LATE rail (`WO-1120 · 14D`) while its card reads `HELD` with **no age at all** — the rail
  is the disclosure, the card is the stop, and spending the LATE chip on a known stop is exactly
  what the precedence refuses.
- **Current operation** = the WO's lowest-sequence IN_PROGRESS op, else its lowest READY op, else
  its lowest PENDING op, else its lowest **ON_HOLD** op — none when all ops are complete (the card
  then reads `ALL OPS COMPLETE`). ON_HOLD is strictly last (an actually-runnable op always wins)
  but it *is* in the chain: with ON_HOLD work orders on the wall, the common shape is a WO held
  *because* its operation was held, and leaving that shape with no current op made the tile read
  `ALL OPS COMPLETE` beside a `HELD` chip **and** dropped the job off every `?dept=` board.
- **Card anatomy — five fixed rows:**
  1. WO number ←→ status chip (glowing dot + state word; only DOWN dots pulse);
  2. part number ←→ `done/ordered` qty (see **Order totals on a pool WO** below) — on a
     **unit-tracked** WO the left slot instead leads with `UNIT <n>` and steps the part number
     down beneath it at a smaller weight (see **Unit # — ungated** above); the qty side is
     unchanged, and a WO with no unit renders this row byte-identically to its pre-`083` self;
  3. `OP n/total · <op name>` — **or the WO's customer name on an authorized (executive) board**
     (see "Customer names — gated"; public boards keep the op line, and an authorized board falls
     back to it for a WO with no customer) ←→ the state's **time value** — red downtime duration on
     DOWN, orange blocked age on BLOCKED, green elapsed cycle on RUNNING, muted elapsed on a LATE
     card that is also running (minutes tick client-side between polls);
  4. work center ←→ the **stop reason** — the downtime category on DOWN, the blocker category on
     BLOCKED, the bare words `ON HOLD` on HELD (**no hold reason, NCR title or free text — ever**;
     that text can name customers and suppliers, and keeping it off the tile is what makes ON_HOLD
     on the wall a *population* change rather than a disclosure-category change), `IN QUEUE` on
     WAITING;
  5. a thin progress bar (`qty_complete / qty_ordered`) + percent.
- **Which cell yields — the card's width contract (2026-08-20).** Every row is two cells competing
  for one content box: **314px at 1080p** (the 347px grid cell, less the `0.25rem` status edge, the
  `0.0625rem` right hairline, and 2 × `0.875rem` of padding). The card renders in a **monospace**
  stack with a `0.6em` advance, so width is exactly `chars × (0.6 × fontSize + tracking)` — every
  budget here is arithmetic, not a measurement, which is why the board needs no fit-text loop,
  `ResizeObserver`, or any other layout-measuring JS (all three are forbidden: this display runs
  unattended for weeks). The rule for which cell gives way is **not** "whichever sits on the right":

  > A cell may take its full max-content width (`shrink-0`) only when truncating it would make it
  > **lie**.

  The status chip (`LATE 12D`), the qty (`40/120`) and the time value (`2H14M`) are exactly that
  class — a clipped number reads as a different, plausible, *wrong* number — and all three are
  small and bounded (≤128px), so they stay rigid and their rows are paid for out of tracking.
  **The stop reason was the one rigid cell that was neither.** At 211px `ENGINEERING QUESTION` is
  the widest cell on the card (69% of the box), and being `shrink-0` it took all of it, leaving the
  work-center name **87px — eight characters**. That is how `DEBURR BENCH 1` reached the TV as
  `DEBURR B…`. **Severity was inverted:** DOWN and BLOCKED cards, where knowing *which machine*
  matters most, were the only two that hid it, while a calm LATE card (empty right cell) gave the
  same name 298px.

  Row 4 now inverts the priority: the **work center is rigid** up to a `12.5rem` cap and the
  **reason absorbs the deficit**. The asymmetry is in how the two degrade — the reason is a closed
  vocabulary read from the **front** (each `DowntimeCategory` member, and each
  `WorkOrderBlockerCategory` member, is unique within 8 characters **of its own vocabulary**, so
  `ENGINEERING…` still names the blocker), while a work-center name is free text disambiguated at
  the **end** (`DEBURR BENCH 1` vs `… 2`), and truncating it destroys the identity outright.
  Per-vocabulary is the correct frame, not a hedge: a card draws this cell from exactly one of the
  two enums and which one is settled by the status chip and the status edge before the text is read.
  Across the union the claim is false — `OTHER` belongs to both enums, and `MATERIAL` (downtime)
  shares eight characters with `MATERIAL MISSING` (blocker) — which is why
  `WoCard.test.tsx` checks the two vocabularies **separately**. The cap keeps the inversion from merely flipping the unfairness:
  it reserves the reason `314 − 12 − 200 = 102px ≈ 10 characters`, and it is applied **only when a
  reason exists**, so a LATE/RUNNING card still hands the machine name the whole row. Both cells
  carry `min-w-0`, which is load-bearing rather than tidy — without it a flex item's automatic
  minimum size is its longest **word** (`ENGINEERING`, 111px), the reason refuses to shrink far
  enough, and the **row overflows the card**: a geometry break, not a truncation, against this
  board's central rule that every panel keeps its slot at all data values.

  **Tracking is now a label affordance only.** The chip and the `UNIT ` prefix keep theirs, the stop
  reason keeps a reduced `0.03em`, and *data* strings — WO number, part number, unit number, op
  line, customer, work center — run at `0`. On this face tracking is the cheapest width available
  (0.8px per character at `1rem`, against 0.6px for a whole `0.0625rem` font step) and it costs no
  glyph height, which is what actually carries at 3–6m. **Row 4 was not fixable by typography at any
  size**: the realistic worst pair overruns by 110.8px while every non-font lever combined yields
  46.4px, and even `0.875rem` with zero tracking on both sides is still 17.6px short. Only the qty
  changed size (`1.1875rem` → `1.0625rem`) — it is the cell that starves the part number while being
  forbidden to truncate, so size was the only lever it had, and row 5 restates the same progress as
  a percent directly beneath it. **The part/unit headline keeps its `1.9375rem`.**

  Row 4's gap is the one that **grew** (`0.5rem` → `0.75rem`) while every other row's shrank. Before,
  the machine name almost always truncated, so an **ellipsis** separated the two cells; now it almost
  always renders complete and ends on a real glyph, and two same-size monospace strings 6px apart
  read as one run-on at 5m. Row 3 keeps the tighter gap precisely because its left cell *does* still
  truncate and supplies its own ellipsis.

- **Machine identity — an open owner question, not a layout decision.** `WorkCenter` carries two
  identities and `wallboard_service` sets both from the same object, so the card's
  `work_center_name ?? work_center_code` fallback never fires: `name` is `String(100)` free text with
  no uniqueness constraint, `code` is `String(20)` and **unique per company**. Rendering the **code**
  instead would bound this row by construction and was seriously considered. It was **not** taken,
  because it changes *what the floor reads* and the evidence does not support it: in `seed_data.py`
  the CNC cells pair `CNC-01/02/03` with **`Haas VF-2` / `Haas VF-4` / `Haas ST-20`**, where the code
  carries no machine identity at all and an operator says "the VF-4". The layout fix renders **every
  work-center name in the seed data whole** (the longest, `Powder Coating Line`, is 19 characters
  against a 20-character cap), so the swap would trade a working prose name for a code that is
  uninformative on the shop's most valuable machines. **Ask before revisiting:** when someone points
  at a stopped machine, do they say `PWD-01` or "the powder line"? If it is the code, the swap
  becomes correct and `OperatorKiosk` / `CrewStationKiosk` / `Dashboard` / `FlowAnalytics` already
  render code-first.

- **What still truncates on the card, on purpose.** Verified in a real engine (headless Chromium at
  1920×1080, 2560×1440 and 3840×2160 — identical results at all three, so the `rem` budget is
  genuinely scale-invariant) against an adversarial 12-card fixture built from the real enum
  vocabularies, the seed-data work-center names and production-format `WO-YYYYMMDD-NNN` numbers.
  Clipped spans went **21 → 8** counting the single-style spans, or **23 → 10** counting the two
  mixed-style unit lines as well (they carry a nested `UNIT ` prefix, so they are measured by
  `scrollWidth` rather than by an offscreen probe). The composition changed more than the count:
  work-order numbers 8 → 0, part numbers 2 → 0, work-center names 4 → 1. What remains:
  - a work-center name **over 20 characters** when a reason is present — in practice exactly one
    machine, the 30-char `Ermaksan Fiber Laser 6KW Bay 2`, which shows 20 characters (it was showing
    **eight**). No split of a 302px row fits a 30-char name beside a 20-char reason; something must
    lose. It renders whole on LATE/RUNNING cards;
  - the **stop reason**, whenever the machine name is long — down to its ~10-character floor. This is
    the deliberate trade and the one genuinely new truncation this change introduces;
  - **row 3's op line.** `OP 12/12 · FIRST ARTICLE INSPECTION` improved from 95px over to 57px, but
    the `OP n/total · ` prefix alone eats 9–11 of ~28 characters. Closing it means shortening the
    prefix, which changes what the row **says** — a content decision, deliberately not taken here.
    The executive customer-name variant clips the same slot (75px → 37px);
  - **row 2 beside a large quantity.** The deficit is driven by the **qty digit count**, not the part
    number: the same 14-char `COVER-PNT-1120` was 5.7px short beside `0/24` and 85.5px beside
    `12500/25000`. It now fits through `40/120`; `100/250` and up still clip. Closing the 4-digit
    case needs the headline at ~1.6rem, which is not worth it;
  - the **unit card's part sub-line**, which is unfixable by width alone: the unit line and the part
    sub-line share one `flex-col` wrapper, so both children clamp to whatever the larger claimed.
    There is no negotiation to fix — it is a pure width shortage.
  - **row 1 sits exactly one character from its next cliff, and that one is DATE-DRIVEN — it will
    arrive silently.** `WO-YYYYMMDD-NNN` is 15 characters and now fits beside every chip, but the
    bound on a LATE card *is* 15. The day a daily sequence reaches four digits
    (`WO-20260819-1004`, 16 characters) the work-order number clips again, on LATE cards only.
    `_generate_work_order_number` mints `NNN` per day, so this needs 1000 work orders released in
    one day — remote for this shop, but nothing refuses it and nothing warns. Recorded because a
    regression that depends on the calendar rather than on a code change is the kind nobody
    connects back to this commit. The fix if it ever lands is the chip's tracking (`0.08em`, with
    room to go to `0.05em`), not the WO number's size.
- **Order totals on a pool WO:** rows 2 and 5 normally show the **work-order header**
  (`quantity_complete / quantity_ordered`) — correct for a conventional routing, where every
  operation processes the whole order. A **pool** work order is different: its operations are
  independent line items, each carrying its own target in `component_quantity` (a laser nest's
  planned_runs; a batch WO's piece count for that item). There the card shows the **SUM across
  those line operations** — total pieces done / total pieces on the order — because the header's
  non-pool rollup takes MAX over operations capped at `quantity_ordered`, which read `8/8 —
  100%` on an 18-item press-brake batch that was on item 2 (fixed 2026-08-13; laser cards were
  already right, since a laser WO's header IS the pooled SUM). One server-side helper:
  `per_item_operation_totals` (`work_order_state_service.py`) — a pure read that issues no
  queries of its own (guard 2 below reads the operation's nest, which the wallboard eager-loads).
  The `OP n/total` row and the ship panel's `N LEFT` (which counts shippable units, not line
  pieces) are unaffected, so a pool card can read `8/79` beside a rail reading `8 LEFT`.
  - **Telling a pool from a routing is the hard part**, because `component_quantity` with a NULL
    `component_part_id` is **overloaded**: `GET /work-orders/preview-operations` emits the
    *assembly's own* routing operations with exactly that shape, carrying the **whole order
    quantity restated once per operation**, and the New Work Order wizard posts it verbatim.
    Summing those would render a 10-piece job as `0/50` then `50/50`. Four guards, in order:
    **(1)** the WO's part has an active **BOM** → never sum (that impostor is emitted only for
    BOM'd parts, and conversely a pool WO's hand-set targets only survive on a part with no BOM,
    since `_reconcile_operation_component_quantities` overwrites them on every WO GET);
    **(2)** operations backing a **soft-deleted laser nest** are dropped (the tombstone keeps its
    `component_quantity` while the header is recomputed over live nests);
    **(3)** **all or nothing** — one operation without a target sends the whole WO back to the
    header, so a partly-targeted batch can never render "100%" with items still open;
    **(4)** if **every** line target equals `quantity_ordered`, that's the restated-order-quantity
    signature, not a pool — the backstop for an impostor whose part later *lost* its BOM.
    A WO that fails any guard shows the header, exactly as before.
  - **Two known costs, not oversights.** A pool whose every line carries the *same* target as the
    header (8 sets × 1 piece per line; a 3-line WO at `quantity_ordered = 1`) is indistinguishable
    from the impostor, so guard 4 leaves it reading `8/8 — 100%` — **one line with a different
    piece count is enough to make the card work**. And adding a whole-order operation to a pool WO
    (a final QC, a PACK step) leaves that op untargeted, which trips guard 3 and reverts the whole
    card to the header — so **don't add an untargeted operation to a pool WO** (nothing in the
    product explains the reversion). Both disappear the day work orders carry a **pool type**: one
    positive marker would replace all four guards. **`WorkOrder.sequential_operations` (migration
    `081`) is not that marker** — it is a per-WO routing/pool discriminator for READY *promotion*,
    and every work order that existed when `081` ran backfilled to `false` (= pooled) whether it is a
    batch of per-item lines or a conventional routing. Reading it as "this WO's operations are line
    items" would sum the restated-order-quantity impostor on the entire pre-`081` backlog. The four
    guards stay; a real pool type is still unbuilt. Until then the rule refuses rather than guesses,
    in the direction that never invents a bigger order than the header. It also assumes **one
    operation per line item** — two operations carrying the same item's count sum it twice, exactly
    as the laser pool rollup does.
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
  IN_PROGRESS / ON_HOLD**) WOs; DRAFT and terminal statuses are off the wall. Server-side
  priority sort — **active work first** (blocked/down → most-late → running → promise date asc, WO
  number tiebreak) and **held work strictly last**, so the alarm classes stay a contiguous prefix
  the TV's pinned anchor row can rely on — capped at **24**; `jobs_total` is the true uncapped
  count for `+N more` / `+N NOT ON BOARD`. Both are
  **dept-scoped** when `dept` is passed — a job belongs to a dept via its **current op's**
  work-center type. Each job carries `wo_number`, `unit_number` (the Unit # this WO builds, `null`
  when it tracks none — **ungated**, see "Unit # — ungated"), `part_number`, the **gated**
  `customer_name` (see the privacy note below), `status`,
  `qty_complete` / `qty_ordered` (order totals — WO header on a conventional routing, the SUM of
  per-item operation targets/progress on a pool WO; see "Order totals on a pool WO"),
  `promise_date` (`must_ship_by || due_date`), `is_late` /
  `days_late` (the same shared lateness predicate as the rail — the card and the LATE panel
  cannot disagree), `blocked` (any unresolved blocker on the WO), `down` (current op's work
  center has an open downtime event), `running` (current op has ≥1 open labor entry),
  `ops_completed` / `ops_total`, and `current_op` — chosen by the IN_PROGRESS > READY > PENDING
  lowest-sequence precedence, `null` when all ops are complete — with `sequence`, `name`,
  `work_center_code` / `work_center_name`, `status`, `qty_done` / `qty_target`, `crew` (up to 3
  "First L." names), `crew_count` (true headcount), `elapsed_minutes` (earliest open clock-in).
  **Privacy:** a card carries WO/part/op identifiers (the Unit # among them), dates, quantities,
  and "First L." crew
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
- **`late_wos[]` / `blocked_wos[]`** — server-ranked (late: worst-first; blocked: oldest-first)
  and **dept-scoped** when `dept` is passed. The two caps **differ on purpose**: `late_wos` keeps
  the 12-row ticker cap (was 25 in the ticker era), while `blocked_wos` is capped at the **job-wall
  limit, 24** (`_BLOCKED_JOIN_LIMIT`) — the Z2 cards join their BLOCKED age and stop reason from it
  by WO number, and the rotation now exposes ranks 13–24, which (being the least severe) are
  systematically the ones a 12-row cap dropped, so later field pages would have shown more blank
  reason cells than page 0. The gap is narrowed, not closed: the rows are one per **blocker**
  (a WO with three open blockers eats three) and ordered by **report time**, not by wall rank, so a
  shop carrying more than 24 open blockers can still miss a tile — the blank stop-reason cell above
  is the designed degrade. Neither list is a **count**: `late_total` / `blocked_total` ride
  uncapped and are the only correct source for one.
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
  Steady state never flashes — including offline. No marquees, no tickers. The **one** exception
  is Zone 2's field, which advances to the next page of work orders every 22s (see "Z2 — Work-order
  grid"): a discrete React state swap with no keyframes, transition or transform — paging, not
  motion — and inert entirely on a board of 12 or fewer delivered work orders (a property of
  `planFieldPages`, not a threshold: one page exactly when the field fits).
- **No scrolling, anywhere:** every list is capped server- or client-side worst-first with a
  `+N more` count, so anything hidden is by definition less severe than everything shown. Zone 2 is
  the one place where "hidden" is now temporary rather than permanent — its field rotates — which is
  why its cycling copy says `+N NOT ON BOARD` and never borrows the rail's `+N MORE … IN QUEUE`.
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
  `WoCard.tsx` / `useWoCycle.ts` (Z2 — the last is the anchor/field cycle POLICY: plan key,
  rebuild rules, alarm snap, mid-cycle resolution), `ShipTodayPanel.tsx` / `LatePanel.tsx` /
  `BlockedDownPanel.tsx` / `QualitySplitRow.tsx` (Z3), `TodayKpiBar.tsx` (Z4), and
  `wallboardTokens.ts` — the board-local Foundry palette, deliberately not the app shell's
  `--fd-*` variables),
  `frontend/src/utils/wallboardLayout.ts` (pure classification, PAGING MATH + formatting helpers —
  `classifyJob`'s strict state precedence, `planFieldPages` / `fieldWindow` / `safeMod` /
  `stripCopy`, duration/age/label formatting, dept title-casing; it still **never sorts** — the
  machine-wall sort and tier hysteresis were deleted with the old layout),
  `frontend/src/services/wallboardClient.ts`.
