# Operator Kiosk (`/kiosk`)

The operator kiosk (A0.3) is a touch-first, full-screen station screen for fixed shop-floor
terminals. It renders outside the normal app `Layout` and handles its own auth: an
unauthenticated visitor gets the badge-login screen, never a redirect to `/login`. It is
deliberately minimal — badge in, clock in to a queued job in two taps, report
production / complete / hold on the active job, and record process-sheet step data (see
[Process steps](#process-steps-process-sheets-capture)). No supervisor verbs (inspection,
labor approval, resume-from-hold, or any override) exist on this screen.

`/kiosk` now serves **two coexisting modes**, dispatched by URL param:

- `?work_center_id=N` — the **single-operator kiosk** documented in the sections below
  (one badge login bound to the terminal, one active job). Unchanged.
- `?station=<id>` — the **crew station** (multi-operator terminal): a shared-PIN station
  unlock, a live per-job crew roster, and per-badge JOIN/LEAVE/report/complete/hold/steps. See
  [Crew station mode](#crew-station-mode-kioskkiosk1stationid) at the end of this doc.

**Foundry redesign (2026-07-23).** Both modes render in a kiosk-scoped Foundry
instrument-panel theme: the `fd-*` Tailwind tokens are CSS-var-backed and a `.fd-scope-kiosk`
class on every kiosk root swaps in a darker palette — zero visual change anywhere else in the
app. The single-operator mode is fully redesigned: a **split running-job + queue hero** at
≥ 1100 px (stacked in portrait), a tabbed **GOOD | SCRAP** report overlay, hold and complete
overlay modals, a restyled two-column badge sign-in, and a bottom compliance strip
(`AS9100D · ISO 9001 · ITAR · SYNC` — SYNC reads OK when online, `—` when offline). The header
clock and all timers run on **server-corrected time** (`server_time` rides both operator
reads). The crew station keeps its own layout and gains the palette, the restyled shared
components, and the badge-gated document viewer (see
[Drawing / nest viewer](#drawing--nest-viewer)). The badge-screen fallback is now also
enforced at the HTTP layer: the app's global 401 interceptor is kiosk-aware — on `/kiosk`
paths a dead session clears client-side and the request rejects **without navigating to
`/login`**, so the kiosk lands on its badge screen (this closed the old gap where an
unauthenticated nest-PDF preview bounced the terminal to the office login form).

Frontend: `frontend/src/pages/OperatorKiosk.tsx` and `frontend/src/pages/CrewStationKiosk.tsx`
(+ `frontend/src/components/kiosk/`, `frontend/src/utils/kiosk.ts`,
`frontend/src/hooks/useKioskIdleLogout.ts`, `frontend/src/hooks/useWakeLock.ts`,
`frontend/src/services/kioskStationClient.ts`).

## Station URL and parameters

Each physical terminal is identified by its URL — there is no server-side station record:

```
/kiosk?kiosk=1&work_center_id=12&work_center_code=LASER1
```

| Param | Required | Meaning |
| --- | --- | --- |
| `work_center_id` | **Yes** | Numeric work-center id; drives the station queue (`GET /shop-floor/work-center-queue/{id}`). Without it the kiosk shows a "Station not configured" screen and does nothing. |
| `station` | — | **Routes to crew station mode instead** (server-side `kiosk_stations` id; see the crew-station section below). When present, none of the single-operator params in this table apply. |
| `kiosk=1` | Recommended | Arms the app-wide kiosk mode (persisted in `localStorage`; `kiosk=0` clears it). `/kiosk` is a kiosk-eligible path alongside `/shop-floor` and `/login`. |
| `work_center_code` | No | Display fallback for the station header until the work-center name resolves. |
| `dept` | No | Department tag read by the shared kiosk-mode helpers (`getKioskDept`); not used by the `/kiosk` screen itself. |
| `idle_logout_s` | No | Idle auto-logout override in seconds. Clamped to **30–600**; default **240** (4 minutes). Non-numeric values fall back to the default. |

**Lockdown recommendation.** Run each terminal in a dedicated kiosk-browser app (or the
OS kiosk/single-app mode) pinned to its station URL, e.g.
`/kiosk?kiosk=1&work_center_id=N`. The screen never navigates away on its own — login,
logout, and idle timeout all land back on the badge screen at the same URL, so a pinned
URL is all the station setup there is.

## Badge login

- Authentication is `POST /auth/employee-login` (employee ID or 4-digit badge ID → standard
  JWT access + refresh tokens). Successes, failures, and locked/disabled-account blocks are
  written to the audit trail (`EMPLOYEE_LOGIN_SUCCESS` / `EMPLOYEE_LOGIN_FAILED` /
  `EMPLOYEE_LOGIN_BLOCKED`); a locked or disabled account gets a 403.
- A keyboard-wedge badge scanner "types" the employee id and sends Enter — captured at the
  window level, so no input field has to be focused first (gloved operators never tap a
  field). Manual entry uses the on-screen number pad. The capture ignores keystrokes aimed at a
  real `<input>` / `<textarea>` / `<select>` / contenteditable, along with modifier chords and
  in-progress IME composition. No badge screen carries a text field today (they all key through the
  on-screen pad), so this changes nothing on them — it is there so that arming a capture on a screen
  that *does* carry one can never turn the operator's typing into a badge buffer and their Enter
  into a token mint.
- Badge = identity: one operator per login, no shared accounts. Backend error details
  (invalid ID, locked account, ambiguous badge → 409) are shown verbatim on the badge screen.
- Rate limit: **10/minute per IP** (raised from 3/minute for the Foundry redesign — a shift
  change cycles several badges through one shared station within a minute; still tight enough
  to keep online employee-id guessing impractical).

## Idle auto-logout

- Default **240 s** of no activity (tap / key / scan / wheel), overridable per station via
  `?idle_logout_s=N` (clamped 30–600). The ceiling is intentionally below the global
  15-minute app idle redirect so the kiosk's badge screen — not a hard `/login` redirect —
  always wins.
- A countdown banner appears for the final 30 s; any touch resets the timer.
- Logout is a **client-side token clear** (same as tapping LOG OUT). A server-side audit
  event for the idle logout itself is a known gap, tracked separately.
- **The timeout banks before it clears.** It blanks the screen state, then posts any pending
  [one-tap `+1 PIECE`](#one-tap-1-piece) count, and drops the credential only once that request has
  settled. The order is load-bearing: clearing the session first sends the flush out against a dead
  token, so an idle timeout would quietly destroy pieces the operator had already tapped and
  committed to. Nothing an operator can act through is on screen by then, so holding the token for one
  request costs nothing the logout was protecting — and the wait is **explicitly bounded** by a short
  timeout in the page, because the shared axios client sets no default one and an unanswered request
  would otherwise hold the session open indefinitely on an unattended tablet.

## What operators can do

1. **Queue → confirm → clock in (2 taps).** The station queue lists the work center's
   operations; tapping a job shows a confirm card; CLOCK IN calls
   `POST /shop-floor/clock-in` (entry type `run`). When the operation carries
   process-sheet steps, the confirm card adds a **REVIEW STEPS 2/6** button so the
   operator can read the steps before starting (see
   [Process steps](#process-steps-process-sheets-capture)).
2. **Running-job panel** (the split hero's left column while clocked in): WO number, part +
   **REV** chip (the new `part_revision` payload), quantity + progress bar, the laser-nest
   strip with its **VIEW NEST** button, a process-steps row (**PROCESS STEPS · 2/6 RECORDED**,
   when the operation carries steps) beside a **SCRAP / NCR** shortcut straight to the report
   overlay's scrap tab, and four telemetry tiles — **LAST REPORT** (time + good/scrap deltas
   of the operation's most recent report, from the new `last_report` payload), **AVG PER PC**
   (this session's pieces vs server-corrected elapsed time), **EST OP FINISH** (remaining ×
   average; "—" when unknowable), and **DOWNTIME** (the operation's blocker minutes, from
   `downtime_minutes`; amber when > 0). Its three verbs:
   - **REPORT PRODUCTION** — `POST /shop-floor/operations/{id}/production` with good/scrap
     deltas, entered in a tabbed **GOOD PCS | SCRAP / NCR** overlay. The GOOD tab leads with the
     [one-tap **+1 PIECE** lane](#one-tap-1-piece) — tap once per finished part and the piece
     records itself — above the numpad, the quick-adds `+5 +25` plus **FULL NEST n** when the
     operation's `component_quantity` is > 1, and a reported-so-far totals bar. `+1` is absent from
     that quick-add row wherever the lane renders and present where it does not (the lane's section
     explains why one `+1` may mean only one thing on a screen). The lane is GOOD-tab only — scrap
     takes an explicit reason and a deliberate entry, so a control that commits on a timer has no
     business there. Any scrap quantity **requires** an explicit reason picked
     from the scrap grid (no default; see "Scrap reason picker" below for what the grid
     contains and what is sent). This is no longer a kiosk-only guardrail: the server rejects
     a positive scrap delta with no reason (and the same rule on clock-out) with **422**, so
     reasonless scrap can't be posted around the UI. The scrap tab also carries an **OPEN
     NCR** toggle (pre-selected when the chosen scrap code is material/supplier-category —
     the operator can flip it either way): confirming sends `open_ncr` and the server files an
     **IN_PROCESS NCR** for exactly this report's scrap **in the same transaction** — no hold,
     no blocker, the machine keeps running (deliberate contrast with the process-step
     out-of-tolerance quality-hold, which does hold the job) — and the success toast quotes
     the real NCR number from the response. See `docs/API.md` → Shop Floor → "Scrap → NCR on
     the production report".
   - **COMPLETE** — clock-out first (`POST /shop-floor/clock-out/{id}` with final counts and,
     when any scrap is entered, the same scrap-grid reason), then
     `POST /shop-floor/operations/{id}/complete` at the target quantity. The verb opens a
     summary modal: GOOD / SCRAP tiles (naming any NCR filed this session), sheet runs and
     run time, a steps banner, a **ROUTES TO** row from the new `next_operation` payload
     (the next routing step; omitted on the last operation), and — when the operation has
     tied material — the [material deduction notice](#material-deduction-notice). Final-entry
     good defaults to the remaining quantity and carries the same **`+1 +5 +25`** (plus **FULL
     NEST n**) quick-add row as the report overlay — `components/kiosk/quantityQuickAdds.ts` is
     the single definition behind both, so the two rows can't drift. Two differences are
     load-bearing rather than cosmetic: the row applies to **GOOD only** (never scrap — it is
     captioned, each button names its target, and a tap re-points an open scrap numpad at good
     so the next digit can't land in the field the operator just left), and it is bounded by
     what clock-out will accept. Clock-out refuses **400 "Quantity produced exceeds quantity
     ordered"** once `operation.quantity_complete + quantity_produced` clears the operation
     target, so the row tops out at the remaining quantity and goes **disabled** once the field
     is there. Because good pre-fills at exactly that ceiling, the row arrives disabled on a
     normal complete (captioned `QUICK ADD TO GOOD · MAX n`, or `· OPERATION IS ALREADY AT ITS
     TARGET` when nothing remains): it is there for rebuilding a partial count after clearing
     the field, and recording MORE than the target is an office over-count, not a tap here.
     When the queue holds a next job the CTA chains it — after a
     successful complete the kiosk attempts clock-in to that job, and a refusal is surfaced
     verbatim (non-optimistic) with the operator landing on the queue. If the clock-out
     lands but the completion is refused, the kiosk says so — labor is closed either way.
     A completion refused with **409 `STEPS_INCOMPLETE`** (required process-sheet steps
     missing conforming records) opens the steps view with the outstanding steps rendered
     inline (see [Process steps](#process-steps-process-sheets-capture)).
   - **HOLD** — a required blocker-category grid (material missing, machine down, tooling
     missing, quality hold, …; two-line reason tiles) plus an **optional note** field (sent
     as the blocker note whenever non-empty, any category), then
     `PUT /shop-floor/operations/{id}/hold` at `medium`
     severity. A kiosk hold files the same structured `WorkOrderBlocker` a supervisor would.
     What lifts it again is the **ON HOLD** queue section below.

   Below the three primary verbs the panel carries a **lower-emphasis** fourth action —
   deliberately styled apart so it can't be tapped by mistake:
   - **CORRECT OVER-COUNT** — opens a touch `KioskCorrectionScreen` (digits-only keypad, **no
     minus key**, plus a **required** correction-reason tile from a set **distinct from the scrap
     grid** — "Double-counted", "Scanned twice", "Wrong qty entered", "Mis-key / typo", "Wrong
     job", "Other"). Confirm posts `POST /shop-floor/operations/{id}/reduce-production` with
     `{quantity_delta, reason, source: "kiosk"}`. This **removes good pieces the operator
     OVER-reported** on the job they are actively working — a **miscount fix, not scrap** (nothing
     moves to scrap). What it can/can't do is enforced server-side, not in the UI: it only walks
     back **the operator's OWN unapproved labor** on the operation — their open clock-in first,
     then their own **earlier unapproved sessions**, newest-first (still never another operator's
     count, still never **approved** labor — a signed-off count needs a supervisor), only
     **before** the operation/WO is complete (a complete op/terminal WO is refused **409** "ask a
     supervisor"), and only up to what they recorded on the operation (**400** otherwise). Every
     walk-back is tamper-evidently audited with its reason. Like every kiosk verb it is
     **non-optimistic** — the count never moves locally; the screen keeps the entered quantity and
     renders the server's refusal verbatim as a prominent **inline alert** on the correction screen
     itself (not a toast), so the operator reads exactly why it was refused (see `docs/API.md` →
     Shop Floor → "Over-count correction").

**Scrap reason picker — company codes vs. legacy grid (Lean Phase 1).** What the required
scrap grid contains depends on whether the company has **active scrap reason codes** (the
tenant vocabulary behind `GET /quality/scrap-reason-codes` — see `docs/API.md` → Quality):
- **Codes mode** (one or more active codes): the grid renders the company's codes as
  "CODE — Name" tiles in display order; the tapped code is sent as the write's
  **`scrap_reason_code_id`**, plus an **optional** free-text "Detail" line sent as
  `scrap_reason` (narrative alongside the code).
- **Legacy fallback** (no active codes): the grid is the old hardcoded shop-standard reason
  list (no free text), sent as `scrap_reason` — companies that haven't adopted codes keep
  the pre-existing flow unchanged.

The single-operator kiosk fetches the codes at badge login (`GET /quality/scrap-reason-codes`,
**fail-soft**: a fetch error falls back to the legacy grid rather than ever blocking scrap
entry on the floor); the crew station gets them off its queue payload instead (see
[Crew station mode](#crew-station-mode-kioskkiosk1stationid)). Either a code or non-blank
text satisfies the server's scrap-requires-a-reason rule.

**What operators cannot do.** No overrides: backend gating (operation sequence /
predecessor not complete, on-hold, optimistic-lock 409s, qualification warnings,
no-labor-recorded) is
surfaced **verbatim** in the error toast and never suppressed or retried around. There is
no resume-from-hold, inspection, or labor-approval verb on the kiosk.

**Completing needs somebody to have clocked in.** `POST /shop-floor/operations/{id}/complete`
refuses an operation carrying **no time entry at all** with **400** — *"Clock in to this
operation before completing it — no one has clocked in to it yet."* Neither kiosk flow can
trip this in normal use, and that is by design rather than luck: the gate asks whether the
**operation** has any labor on it, open **or closed**, not whether the caller is clocked in
right now. The single-operator COMPLETE clocks out first and completes second (its entry is
closed by then), and the crew station's signing badge often holds no entry of its own while
it auto-closes the crew's. What the gate stops is an operation booked complete at full
quantity that nobody ever worked — reachable from the operations list when a job's items all
sit READY together (see "Work-center pools at clock-in" below). The scanner resolver reports
the same blocker, so a scanned traveler and the endpoint say the same thing. The office verb
`POST /work-orders/operations/{id}/complete` is deliberately exempt — desk cleanup of
never-clocked work stays a supervisor/quality call. See `docs/API.md` → Shop Floor →
"Shop-floor completion requires a labor record".

**Queue order and the RUN chip.** Both kiosk modes list the work center's queue in the order the
server returns it — the kiosk never re-sorts client-side. That order is:

1. jobs a manager ranked on the **Dispatch Board** (`/dispatch`), in rank order — these carry a
   `run_order` and render a **`RUN n`** chip (`KioskRunOrderChip`) on the queue card and the crew
   job card;
2. then everything unranked, by work-order priority, then due date, then operation sequence.

Unranked jobs therefore always sort **after** every ranked job, and carry no chip. The rank is
**advisory**: it tells the operator what the shop wants run next, but **any** queued job can still
be started. The rank itself never gates a clock-in; the existing backend gating (sequence /
predecessor, on-hold, locks) is unchanged and stays the only thing that refuses one, still surfaced
verbatim as described above. Moving an operation to a different machine clears its rank; it lands
unranked at the tail of the new work center's queue until a manager re-ranks it there. This order
is not kiosk-only: the desktop shop-floor pages (`/shop-floor` "Time Clock" and
`/shop-floor/operations` "Operations") render the same server order verbatim and reuse the same
chip (`KioskRunOrderChip`, compact `size="sm"` — one implementation, do not fork), so an operator
sees one queue order everywhere: kiosk, crew station, desktop, and the manager's Dispatch Board.
Full ordering rule and the endpoint contract: `docs/API.md` → Shop Floor → "Dispatch run order" /
"Queue ordering" / "Desktop parity".

**Work-center pools at clock-in.** Operations of one work order that share a **work center** no
longer block each other for READY promotion, so they all appear on the queue together instead of one
at a time — a batch work order carrying ~18 press-brake items as one operation each now shows all 18,
where the queue previously showed one and hid the rest (the queue surfaces READY work only). Nothing
about what an operator may clock into changed: the floor's gate has always allowed same-work-center
operations in any order, so this makes already-legal work visible rather than granting anything.
Operations at a **different, downstream** work center still wait for their predecessors as before.

**If a job still shows one item of many, open the work order in the office app once.** Promotion runs
at work-order release, at each operation completion, and on the reconciling office/shop-floor reads —
**not** on the kiosk queue read. A work order that was released *before* this rule shipped can no
longer be released again, so it keeps the old one-at-a-time promotion until something reconciles it:
loading it on the Work Orders page (list or detail) or on the desktop `/shop-floor` dashboard /
operations list repairs it permanently, and the kiosk shows the full pool on the operator's next poll.
Refreshing the kiosk alone will not fix it. Nothing else is needed — no re-release, no re-import, no
admin action — and it only ever has to be done once per work order.

One consequence worth knowing on the floor: **holding one item stops its siblings being started.**
An **ON_HOLD** operation blocks from any work center, its own included, so while item 3 of a batch is
held the other 17 cannot be *started* — a quality or material stop is meant to stop the shop
building past the problem, not leave the pool running around it. Resuming the held operation puts
them back. This is the one place a non-laser pool and a laser pool differ (see the next paragraph):
a held **nest** does *not* block its siblings.

The hold gates **starting**, not **finishing**. An operator already clocked in when the hold lands
can still clock out, and a clock-out that reaches the target quantity completes the operation and
consumes its tied material — `POST /shop-floor/clock-out/{id}` carries no predecessor or ON_HOLD
check. So a hold stops the *next* item, not the one on the machine. Don't read the containment as
stronger than that when a problem is found mid-run.

**Laser nests at clock-in.** A laser-cutting WO is a **dispatch pool**: every nest operation is
created READY, so all of a package's nests appear on their work center's queue at once, and
operators may run them in **any order** — laser WOs are exempt from the sequence/predecessor gate,
even when a package's nests are spread across different lasers (see `docs/API.md` → Laser Nests →
"Laser WOs are dispatch pools"). A manager can still rank nests on the Dispatch Board; as
everywhere else, that only orders the queue and shows a `RUN n` chip — it never gates which nest an
operator picks up. For laser-cutting operations the kiosk surfaces the active nest at
all three touch points so the operator can confirm the right sheet before cutting: the queue card
(`KioskQueueCard` — CNC#/nest name, `completed`/`planned` runs, material•thickness, and a **PDF**
chip when a reference PDF is attached — tapping the chip opens the full-screen
[drawing / nest viewer](#drawing--nest-viewer) on the NEST tab without also firing the card's
clock-in confirm), the clock-in confirm card, and the running panel's nest strip (CNC#/nest,
material · thickness · sheet size, sheet-run count, and the **VIEW NEST** button into the same
viewer). The data is the `laser_nest` object that
`GET /shop-floor/work-center-queue/{id}` puts on each queue row and `GET /shop-floor/my-active-job`
puts on the active job — `null` for non-laser operations, and a soft-deleted manual nest never
appears (see `docs/API.md` → Shop Floor → "Laser-nest payload on operator reads" for the full
shape). The optional reference PDF is fetched **inline** through the fence-safe
`GET /shop-floor/documents/{id}/inline` on kiosk surfaces (the old `GET /laser-nests/{id}/document`
route remains for desktop callers); there is no approval workflow, and it never gates clock-in.

## Held work and RESUME

A held operation **stays visible on both kiosks**, in its own **ON HOLD** section below the
startable jobs. Before this it was invisible: the queue renders `QUEUE_OPERATION_STATUSES`
(READY/IN_PROGRESS) only, so an operator who mis-tapped HOLD watched the job disappear from every
screen the kiosk offers, and `resumeOperation` had exactly one call site in the whole app — a
desktop page. Recovery meant leaving the machine.

**Held work arrives on its own list.** `GET /shop-floor/work-center-queue/{id}` returns `held`
beside `queue`, plus `held_truncated` when the server's cap (`MAX_HELD_OPERATIONS`, 25) dropped
older holds — both kiosks say so rather than showing a silent subset. `queue` stays byte-identical
to what it always carried. **The list boundary is the safety property, not a flag inside the
rows**, so nothing client-side re-merges them and no code path that renders a startable job card
can reach a held row. Held rows additionally carry `startable: false` (stated, with no `true` twin
on queue rows — whether a *queued* operation may actually start is decided by the server gates at
the moment of the action, which a poll cannot honestly predict) and `run_order: null`.

The held card is deliberately **not** a `KioskQueueCard`: it is inert markup with exactly one
interactive element, **RESUME**. A held job must never be startable, and the operator has to lift
the hold first — which is also what the server enforces.

**It shows why it stopped**, from the row's nested `hold` block:

```
hold: { held_at, held_by_user_id, held_by_name, blocker: {…} | null }
```

`hold.blocker` carries the reason — category (labelled from the same `HOLD_REASONS` vocabulary the
hold tiles use), severity, and — **on the single-operator kiosk only** — the title and the
operator's note verbatim. `hold.held_by_name` / `hold.held_at` carry who stopped the job and when.
That is what lets somebody tell their own mis-tap from a real quality or material stop placed by
someone else; without it, RESUME is a control that silently clears another person's genuine hold.

> **The blocker's free text does NOT reach the crew station.** A crew station authenticates its
> 10–15s poll with a 24-hour shared-PIN **station** token: an unattended, PIN-unlocked tablet with
> no operator identity and **no idle station logout**, so whatever the board renders is readable by
> anyone walking past it. On a station response the server omits `title` and `note` **entirely** —
> the keys are absent, not blanked, because a render-side gate would still ship the text to the
> device — and sets `free_text_withheld: true` alongside `has_note`. The single-operator kiosk runs
> on the operator's **own user session**, so it is an identified caller and keeps the full block.
>
> **This is the wallboard's rule, applied to the same class of screen.** `wallboard_service`'s
> module docstring already states it for unattended shop displays — *no customer names, no ship-to
> addresses, no dollar figures, **no NCR titles/descriptions*** — and the wallboard's blocked-work
> rail has `title` and `note` in hand and deliberately emits only `wo_number` / `category` /
> `age_hours`. A held card carrying the note would disclose to that audience exactly what the
> wallboard withholds. `title` travels with `note` because it is equally unconstrained: `POST
> /work-order-blockers` takes a caller-supplied title, and the server-composed
> `_blocker_default_title` is only the fallback for a kiosk-placed hold.
>
> **It costs the feature nothing, and the card says so rather than going quiet.** The motivating
> accident is a **bare** hold that carries no note at all, and a deliberate categorized hold is
> still identifiable from category + severity + who placed it. Where free text *does* exist, the
> crew card and the confirm overlay render *"A written note was recorded — not shown on a shared
> station. Ask a supervisor before resuming."* Silence there would read as "no reason given" and
> invite a Resume over somebody's real stop.
>
> **`has_note` covers BOTH withheld fields, not just `note`.** It is a boolean, never the text, and
> it is true when a human wrote *either* a note **or** a title that differs from what
> `blocker_default_title` would compose. It used to key on `note` alone, reasoning that
> `work_order_blockers.title` is `nullable=False` and therefore always server-composed. That was
> wrong in the one direction that matters: the title is composed only when the **caller supplies
> none**, and an office-filed blocker routinely carries its whole explanation there with an empty
> note. Those reported `has_note: false`, the card drew a bare category, and the silence read as
> "nobody gave a reason" over a hold somebody had deliberately written up. A caller who types the
> exact composed string still reads as server-composed — the safe way to be wrong, since the
> withheld text would then say nothing the category does not.
>
> The note is not fetched back post-badge either: the crew confirm overlay renders **before** the
> badge scan, and adding a post-badge re-read for one line of text would buy an extra round trip on
> the floor for something the desktop and the single-operator kiosk already show.
>
> **The resume RESPONSE is gated the same way — the device, not just the poll.** `PUT
> /shop-floor/operations/{id}/resume` returns the blockers it did not resolve, and
> `KioskBlockerStillOpenScreen` renders them on a view built to persist (an explicit tap to leave,
> bounded only by the 90s idle reset). Those titles are **not** "server-composed": `title` is the
> same caller-supplied free text withheld above, so it is omitted from `open_blockers` whenever the
> caller presents a badge-minted crew-station token (`_token_scope == "kiosk"`), with `has_note` /
> `free_text_withheld` in its place and the category as the rendered fallback. The single-operator
> kiosk runs on a normal session (no `scope` claim) and keeps the title, as does the desktop.
>
> **One seam holds by client convention rather than server construction.** A crew station holds two
> credentials at once — the 24h station token and, briefly, a badge-minted operator token — and an
> operator token on the *queue read* resolves to `principal.kind == "user"`, which would return the
> free text. The server cannot tell that request from the single-operator kiosk's. So the queue read
> must always be sent with the **station** token; `kioskStationClient.getQueue` takes no token
> parameter and reads it from storage itself, and `kioskStationClient.queueToken.test.ts` pins that
> it still does so while an operator token is live. The resume *write* has no such exposure: it must
> carry the operator token for audit attribution, and that token is exactly what the gate keys on.

> **Reason and attribution are INDEPENDENT — never gate one on the other.** There is no
> `held_by` / `held_at` column on `work_order_operations`; the server reconstructs provenance from
> whichever record the hold path wrote, and the paths differ. A hold **with** a note or a non-OTHER
> category files a `WorkOrderBlocker`, so `blocker` is populated. A **bare** hold — no note,
> category OTHER, which is exactly the accidental fat-finger case this feature exists for — emits
> an `operation_hold` event and files **no blocker at all**, so `blocker` is null while
> `held_by_name` / `held_at` still name who pressed it. Reading the attribution off the blocker
> would leave precisely that case anonymous *and* reasonless — the one case that most needs to read
> as an accident. When both records are absent the card says "no hold reason recorded": a real
> state, since the server reports what was recorded and never infers a holder from
> `operation.updated_at`.
>
> The confirm overlay's copy forks on the same fact. With a blocker it warns the hold stays
> recorded and to ask a supervisor to clear it; for a **bare** hold it says there is nothing left
> open, because there genuinely is not — sending that operator after a record that does not exist
> would be its own small betrayal.

Both surfaces confirm first, in a `KioskModal` (**not** the shared `<ConfirmDialog>`, which portals
outside `.fd-scope-kiosk` and would paint office chrome on a shop tablet). The overlay restates the
job and states that the hold stays recorded. Then:

- **Single-operator kiosk** — confirm fires `PUT /shop-floor/operations/{id}/resume` on the
  operator's own session.
- **Crew station** — confirm hands off to a full-screen **badge signature** (`crew-resume`
  `BadgeScanPanel`), then `kioskStationClient.resumeOperation(operatorToken, id)`. The station
  token is honored only by the queue read and the badge mint, so the badge-identified operator is
  the audit actor. Two steps rather than a badge-in-modal like COMPLETE: a keypad inside the
  overlay pushes its own bottom row under the fold at 768x1024.

Server-gated ⇒ **non-optimistic**. A refusal renders **verbatim**: a toast on the single-operator
kiosk, inline on the crew sign screen, and nothing in the UI moves. Three refusals exist —
400 *"Operation is not on hold"* (a supervisor got there first), 404 on a cross-tenant id, and
**409 *"This nest was cancelled; its operation cannot be resumed."*** (below).

> **RESUME restores; it does not release, and it does not resurrect.** Both guards live on the
> **write**, so the desktop `ShopFloorSimple` page inherits them too — they are not kiosk-only UI
> rules.
>
> **A cancelled nest's operation is a tombstone, not a hold → 409.**
> `laser_nest_service.soft_delete_laser_nest` parks a soft-deleted nest's operation in `ON_HOLD`
> (`OperationStatus` has no operation-level CANCELLED) and never hard-deletes the nest, precisely so
> traceability and the package's run history survive. Resuming one would undo a soft delete from the
> front end and put a laser operation with no live nest, no CNC file and a quantity its parent no
> longer counts back on the board. `dispatch_service.cancelled_nest_exists` is the shared predicate:
> it keeps the tombstone off the kiosk's `held` list, off `GET /shop-floor/operations` (the desktop
> list where Resume is offered), and refuses the resume itself.
>
> **A resume never performs a release.** `PUT …/hold` refuses only COMPLETE, so a `PENDING`
> operation can be held — and the old *"`IN_PROGRESS` if `actual_start` else `READY`"* rule then let
> one tap promote it onto the dispatch board and the kiosk queue, on a `DRAFT` work order included.
> Release is the authorization step and the record of who authorized production. Resume now floors
> at `PENDING` and delegates any lift to `promote_ready_operations` — the one promotion rule shared
> by WO release, operation completion and the read-path heal — so it can only reach a state the next
> board poll would have reached anyway. In practice: started before the hold → `IN_PROGRESS`;
> `READY` before the hold on a live released WO → straight back to `READY`, net zero; `PENDING`
> before the hold, or a `DRAFT`/terminal parent, or an incomplete cross-work-center predecessor →
> `PENDING`, and the job does **not** rejoin the queue. Both kiosks then show an *info* toast
> (*"… hold lifted — still waiting on release or an earlier step."*) rather than a green *resumed*
> that would send the operator hunting a card that is never going to appear.
>
> An exact pre-hold status is not recoverable today: there is no `held_from_status` column, and the
> two records the hold paths write (the `operation_hold` event, the blocker) are **best-effort**
> emits — not a state source a transition may depend on. Recomputing from the promotion authority
> needs no schema change; a column that records the pre-hold status exactly would be a separate,
> scoped migration.

**Resume audits as a `STATUS_CHANGE`**, carrying old→new status plus
`extra_data.transition = "resume_operation"` (the verb the generic action no longer names) and the
ids of any blocker still open. It was a prose `audit.log` row with no before/after states, which was
tolerable while one desktop page was the only caller and stopped being tolerable when resume became
a shop-wide floor verb.

**Resuming does not resolve the blocker.** The endpoint returns `open_blockers` precisely so
operation status and blocker status cannot silently diverge, and the kiosk gives that list its own
screen with an explicit exit (like the OOT `KioskNcrFiledScreen`) rather than a toast the 15s poll
would yank away. The server's blocker `title` is rendered verbatim **where it is sent** — on a
crew-station response it is withheld (see the disclosure box above) and each row falls back to its
category label, with the category dropped from the line underneath so the panel does not stutter it
twice, plus one line saying a written reason exists.

> **Known gap — the kiosk cannot CLEAR a blocker, only resume past one.** For an accidental hold
> the better outcome is resolving the blocker: that resumes the operation as a side effect
> (`_resume_operation_if_no_open_blockers`) **and** closes the record, leaving nothing diverging —
> which is how the owner actually recovered from the incident that motivated this feature. Resuming
> alone leaves a phantom blocker on the dashboard and the WO Blockers panel for someone to chase.
> The kiosk ships resume-only because `POST /work-order-blockers/{id}/resolve` is unreachable from
> **both** surfaces behind two independent gates: it requires **ADMIN/MANAGER/SUPERVISOR** (an
> OPERATOR on the single-operator kiosk, which runs on their own session, gets 403), and
> `/api/v1/work-order-blockers` sits **outside `KIOSK_TOKEN_PATH_PREFIXES`**, so a badge-minted
> crew-station token is 403 there *whatever role the badge holds*. Widening either is an RBAC /
> security decision, not a frontend one. Until then the copy tells the operator the record stays
> open and to ask a supervisor to clear it. Revisit if a shop-floor-fenced resolve ever lands.

**Fold (2026-08-12, measured on the real pages; worst case — long blocker note AND the truncation
banner showing):** every control clears both tablet orientations. The **crew** rows are now
conservative rather than worst-case: a station never receives the note, so its card renders the
one-line *"a written note was recorded"* replacement instead of a long free-text block. Re-measure
only if a crew card grows a line.

| Control | 1024x768 | 768x1024 |
|---|---|---|
| Crew keypad bottom row (the standing rule's subject) | 619px, **+149px** | 619px, **+405px** |
| Crew resume-confirm CTA | 662px, **+106px** | 790px, **+234px** |
| Crew held-card RESUME | 740px, **+28px** | 740px, **+284px** |
| Crew held card (bottom edge) | 761px, **+7px** | 761px, **+263px** |
| Operator held-card RESUME | 642px, **+126px** | 642px, **+382px** |
| Operator resume-confirm CTA | 662px, **+106px** | 790px, **+234px** |

The **+7px** is worth understanding rather than treating as pass/fail. It is the bottom of the
*first* held card at landscape, with the truncation banner above it — i.e. one startable job plus a
banner that only appears past 25 concurrent holds. The board is a scrolling list, so a second held
card is below the fold the same way a fifth queue card always has been; that is inherent to a list
and not what the rule guards. The rule's actual subject is the fixed-height **badge screens**, where
the keypad's bottom row must be reachable without scrolling — those sit at +149px / +405px. If the
held card grows another line, re-measure before shipping it.

## One-tap `+1 PIECE`

Both REPORT PRODUCTION surfaces — the single-operator kiosk's overlay and the crew station's
quantity screen — lead with a **one-tap lane**: a `+1 PIECE` button tapped once per finished part,
which records itself. One implementation serves both
(`components/kiosk/useOneTapPieces.ts` + `components/kiosk/KioskOneTapLane.tsx`), and the state
machine is owned at **page** level rather than inside the screen that renders the lane — a tapped
count sitting in a subtree that Cancel, the crew station's 90 s idle flow-reset, its ghost-guard or
the single-operator idle logout is about to unmount would be silently lost production. Owned a level
up, every one of those teardowns banks the count instead.

Nothing about the write changed: it is the same
`POST /shop-floor/operations/{id}/production`, the same body, the same `source: "kiosk"` telemetry,
the same audit row, made under the same credential the surface already held.

**The tap is the commit; the window is only a way out of it.** A tap adds to a pending count and
(re-)arms a **5-second** window — a depleting bar, a seconds digit, and an **UNDO −1** control. Each
further tap re-arms it, so a run of parts coming off a machine posts as **one** additive report
(`+3`) rather than three racing requests, which is also what keeps the undo honest: whatever is
still on screen is still undoable. When the window elapses, the accumulated taps post.

**The pending count is banked whenever it CAN be, and never silently dropped when it cannot.** It
posts on the window elapsing, on leaving the
report screen (Cancel, the crew station's idle flow-reset, its ghost-guard, **Lock station**), on
the single-operator kiosk's idle auto-logout, and on page unload. The unload flush differs by
surface, deliberately: the crew station posts through its isolated fetch helper and sets
`keepalive`, so the request outlives the document; the single-operator kiosk runs on a normal
session through the shared axios client (ETag caching + the refresh-token interceptor), which cannot
set `keepalive`, and reaching around the client with a raw `fetch` would post outside the
interceptor that keeps the station's session alive. So on **that** surface the unload flush is
**best-effort** — a tablet closed mid-window may lose it. The guarantees there are carried by the
flushes that run while the document is still alive (the overlay closing, and the idle logout).

Two cases cannot be banked at all, and neither is allowed to vanish quietly:

- **It may already have landed.** This endpoint is purely additive with **no idempotency key**, so
  anything that re-sends a request whose fate is unknown counts the pieces twice — on a quality
  record, and through to FPY/OTD and tied-material consumption at completion. Only a **4xx, excluding
  408 and 425**, is treated as definitive: the server decided and wrote nothing. Everything else is
  ambiguous — no status at all (a dropped connection, an aborted `keepalive`), and **every 5xx**. A
  502/503 can come from a proxy that never reached the app, and a **504 is the canonical case where
  the write may well have committed and only the answer was lost**. Ambiguous failures are barred
  from every automatic path; only a human tapping **RETRY** may send one again.
- **Nobody can post it truthfully.** A delta whose `(operator, operation)` pair is gone — the badge
  expired and the operator never came back, or the tab closed while offline — is written to a
  sessionStorage **notice**: the count, the operator, the job, and a plain statement that the pieces
  are *not* on the job and must be recorded in the office. It is written from `pagehide` and from
  `visibilitychange`, **not only from an unmount** — React does not run effect cleanups on page
  unload, and a locked shop tablet never navigates in-SPA, so an unmount-only write would miss the
  reload that is the floor's usual recovery. A notice written defensively is reconciled away if the
  pieces are later banked under their own pair.

  A count still **held in memory** — parked by a dead token or a dropped connection — is surfaced the
  same way, on the **board**, naming whose it is. Both are visible off the report screen precisely
  because the 90-second idle reset used to hide the one thing somebody needed to act on.

  Nothing about either notice posts. **Writing pieces off is confirmed**, restating the count and who
  made them, on both routes (the lane's WRITE OFF and the board notice's DISMISS) — a single
  unconfirmed tap destroying the only remaining record of real production is the silent drop with a
  button on it. This is deliberately **not** a retry queue, for the same two reasons above.

**Three states an operator must be able to tell apart without reading a word**, because they commit
differently:

- **PENDING** — amber, dashed border, depleting bar, UNDO available. Tapped, not sent.
- **RECORDED** — green, solid, a check, and **no undo control anywhere**. The kiosk has no undo for
  a posted report; CORRECT OVER-COUNT (its own screen, its own reason, its own signature) is the
  only path after a post, so nothing in this state may imply otherwise.
- **NOT SAVED** — red, `role="alert"`, the server's `detail` verbatim, and a **RETRY**. A refused
  post puts the pieces back on the undoable pile exactly where the operator left them and **stops**;
  it is never auto-retried, because a retry loop against a server saying no is how one part becomes
  four reports.

Both controls in the lane are always present at the same size, `+1 PIECE` beside a dimmed `UNDO −1`
when there is nothing to take back. That is a safety property, not tidiness: measured on the tablet,
rendering UNDO only while something was pending let `+1 PIECE` grow back to full width the instant
the window closed, so a thumb already travelling toward UNDO landed on `+1` and recorded a piece
instead of removing one — the precise accident the window exists to prevent.

**`+1` leaves the quick-add row wherever the lane renders.** The two controls commit differently —
the lane's `+1` posts itself after the window, while a row `+1 / +5 / +25 / FULL NEST n` only fills
the GOOD field for a later confirm — and two controls reading `+1` side by side with those two
meanings is exactly how an operator stops knowing whether their part was counted. So
`components/kiosk/quantityQuickAdds.ts` takes an `omitSingle` option and the row drops its `+1`
there. The rule the module protects is **same appearance ⇒ same behaviour**: a screen may move a tap
to a different-looking control with different semantics; it may never keep the old chrome over new
semantics. Screens with no lane (the COMPLETE modals, the crew station's LEAVE and COMPLETE
quantity screens) keep the row exactly as documented under
[Quantity entry on the crew station](#crew-station-mode-kioskkiosk1stationid).

**The lane is opt-in per screen, and the opt-in is the ceiling** — the same convention as the
quick-add row. It renders only where the surface can work out what the server will accept: the
operation target, less what is recorded, less what the lane has tapped but not yet banked (leaving
the pending taps in would let it count past the target and key a guaranteed refusal). At the ceiling
the tap goes **disabled** (the lane says *"operation is already at its target"*) rather than posting
a guaranteed `400 "Quantity (N) cannot exceed quantity ordered (T)"` — the repo's non-optimistic
rule. On the single-operator kiosk that means an operator clocked onto a
job at **another** work center resolves no queue row, so no ceiling, so **no lane**, and the overlay
renders exactly as it did before (`+1` back in the row).

**Exactly one mechanism owns the count at a time.** While the lane holds un-banked pieces the
screen's confirm is disabled and reads *"Recording N pcs…"* — on **both** tabs of the single-operator
overlay, since a scrap confirm writes the same operation row. The lane always commits first, so a
confirm can never race a pending auto-post into two reports for one run of parts.

**Offline, the tap goes dark** like every other kiosk mutation control, and an armed window does not
fire while the post cannot land — it re-arms when the connection returns, so a delta stranded by a
dropped connection banks itself rather than burning on a request that was never going to arrive.
A window that reaches zero while blocked says so (*"Not saved yet — waiting for the connection"*)
rather than stalling at a countdown that did nothing.

## Material deduction notice

When the operation carries tied material, both completion screens — the single-operator COMPLETE
modal and the crew station's badge-signed confirm — show an informational notice above the confirm
action. It is **never a gate**: a shortage warns, it does not block the job, and the notice never
blocks a badge signature.

**What it says, and why it is worded around the OPERATION.** The heading is
*"Material — deducts when you complete this operation on WO-####"*, and the footer is *"Estimate —
this leaves stock when the operation completes, not as each run is reported."* That phrasing is
load-bearing, not cautious hedging, and it **changed with the trigger**: through PR 2 the same notice
said *"deducts when WO-#### finishes"*, which was correct then — every consumption call site was
gated on **work-order** completion, and since a laser WO carries one operation per nest, finishing
nest 1 of 3 moved no stock at all. Consumption now fires when the **operation** completes, so the
operator's next tap genuinely is what takes nest 1's sheet out of stock. The work-order number
survives only as **context**, never as the trigger — a crew station confirms a badge scan against a
job label, so "on WO-####" is what makes the sentence checkable against the paperwork in front of the
operator. What the copy must still never say is *per run*: reporting 3 of 6 runs on a nest whose
operation is still open deducts **nothing**. (See `docs/API.md` → Shop Floor → "Completion also
consumes tied material" and `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Capability vs. wiring".)

Two more facts the copy carries because both otherwise read as bugs:

- **The GOOD keypad does not move the number.** `/complete` asserts
  `quantity_complete = quantity_ordered` regardless of what was keyed, so the prediction is computed
  from the **ordered** quantity.
- **SCRAP raises it.** A scrapped run physically used its sheet (posted as `ISSUE`, not `SCRAP`, so
  lot genealogy keeps it), so keying 2 scrap predicts 2 **extra** sheets. An explicit line says so —
  *"Includes +2 for the 2 scrap you entered — a scrapped run still used its material"* — because
  "why did my scrap raise the material?" is otherwise a support ticket.

A shortage adds an amber line naming the short quantity and ending *"This never blocks the job; tell
your supervisor."* Everything is labelled an **estimate**: consumption is reconcile-to-target, the
operation's quantities can still move before it closes, and `qty_consumed` is a cache (the inventory
ledger is authoritative).

**The data rides the queue payload; the fence is not widened.** Ties come from the `material_ties`
array the station's own `GET /shop-floor/work-center-queue/{id}` already returns (and from
`GET /shop-floor/my-active-job` in single-operator mode) — **not** from
`/work-orders/{id}/material-allocations`, which sits outside the `/shop-floor` path fence and which a
badge-minted kiosk token gets a 403 from. This is the identical rationale already recorded for the
**scrap reason codes** under [Crew station mode](#crew-station-mode-kioskkiosk1stationid): carry the
data on a read the fence permits rather than widen the fence, so the station token keeps being
honored by exactly two things. The kiosk gets no
tie *mutation* verb at all — tying material is an office act (Admin / Manager / Supervisor).

The prediction arithmetic and every string above live in one place, `frontend/src/utils/materialTie.ts`,
shared by both kiosk modes and the Dispatch Board chip, so the operator's line and the manager's chip
cannot drift apart or drift from the engine. Note that it reads **`operation_quantity_scrapped`** (the
operation's scrap total), not `quantity_scrapped` (this time entry's session count) — the two differ on
any job worked across shifts, and the session figure would under-state the deduction.

## Drawing / nest viewer

The Foundry redesign adds a **full-screen document viewer** (`KioskDocViewer`) shared by both
kiosk modes: segmented **DRAWING | NEST** tabs (a tab hides when that document doesn't exist),
pdf.js canvas rendering (lazy-loaded so the kiosk bundle stays lean) with zoom 50–300% in 25%
steps, **FIT** (fit-to-width), and a page pager for multi-page documents — falling back to a
plain `<object>` embed if pdf.js fails — plus a right rail and a permanent
**CONTROLLED COPY · UNCONTROLLED IF PRINTED · ITAR** watermark strip. The right rail shows the
DOCUMENT key-values (part, revision, released date, nest material where applicable) and a
**CRITICAL DIMS** list — the part's critical SPC characteristics with their tolerance limits
(omitted when the part has none).

Data comes from two shop-floor-fenced reads (both open to any authenticated user, tenant-scoped,
read-only; badge-minted kiosk tokens reach them with zero fence changes — see `docs/API.md` →
Shop Floor → "Kiosk doc viewer"):

- `GET /shop-floor/operations/{id}/documents` — discovery: the newest approved/released part
  **drawing**, the operation's live **nest** reference PDF, the nest **material**, and the
  **critical dims**.
- `GET /shop-floor/documents/{id}/inline` — the guarded byte-serving route: only a DRAWING-type
  document or a live nest's reference PDF in the caller's tenant is served; anything else is a
  uniform **404**.

**Entry points.** Single-operator kiosk: the running panel's **VIEW NEST** button and the queue
card's **PDF** chip (both land on the NEST tab; the DRAWING tab is available when a released
drawing exists). Crew station: a **View nest / drawing** button on the job screen that is
**badge-gated** exactly like steps — the document reads need an operator (badge) token, so the
button opens a "scan badge to view documents" gate first, mints the 5-minute operator token, and
opens the viewer bound to it; a mid-view 401 (expired badge token) renders "Badge session
expired — rescan to view" **inline**. The crew job card's inline nest preview is deliberately
info-only now (no embedded PDF fetch pre-badge), so a nest preview can never 401 its way toward
`/login`. Every viewer failure renders inline with a retry — the component never navigates.

## Process steps (Process Sheets capture)

Operations whose routing carried a released process sheet get an immutable step snapshot at
WO creation (`docs/PROCESS_SHEETS_SCOPE.md`); both kiosk modes capture the per-step objective
evidence against that snapshot through the same shared panel (`KioskStepsPanel`). Every step
endpoint lives under `/shop-floor` on purpose — badge-minted kiosk-scoped tokens are
path-fenced to that prefix, so the crew station reaches them with zero fence changes.

**The steps chip.** Queue and job cards render **"Steps 2/6"** — required (non-INSTRUCTION)
steps vs. those with live conforming records, carried on every
`GET /shop-floor/work-center-queue/{id}` row (`steps_recorded`/`steps_total`) so no extra
round-trip. On a serialized WO a step counts only once records cover **every** serial. The
chip is hidden when the operation has no gating steps (0/0) and turns green when everything
required is recorded.

**Entry points.** Single-operator kiosk: the confirm card's **REVIEW STEPS** button and the
active-job banner's **PROCESS STEPS** button. Crew station: a steps verb on the job screen
that is **badge-gated** — "scan badge to open steps" mints the 5-minute operator token, the
panel banners *Recording as {name}*, and every record is attributed to that badge identity.
A 401 mid-flow (the ≤5-minute badge token expired) returns to the badge scan with a
"Badge session expired" notice — scan again to keep recording.

**Recording (typed, server-authoritative, non-optimistic).** The panel lists the snapshot
steps in sequence with type chips, instruction text, and each step's append-only record
trail. It is readable in **any** operation/WO state (held and completed jobs keep their
trail visible); inputs appear only while the operation is IN_PROGRESS and the station is
online. Writes go to `POST /shop-floor/operations/{id}/steps/{step_id}/records`; the view
refetches after every success and refusals surface verbatim. Per type:

- **MEASUREMENT** — the value pad shows the LSL/NOM/USL limits and a live tolerance preview
  that rounds exactly like the server (`config.decimals`), labeled *"Preview only — the
  server verdict is final."* An out-of-tolerance value is refused server-side with
  **409 `OUT_OF_TOLERANCE`** (`{measured, lsl, usl}`) and **no record row is written**; the
  kiosk renders an inline danger strip ("Out of tolerance — not recorded") telling the
  operator to re-measure — or, if the part really is out, to hold the job and file an NCR
  right from the strip (one-tap flow below).
- **CHECKBOX** — the kiosk records only the affirmative ("Mark done"); an unchecked box is
  simply not recorded.
- **LIST / VALUE** — touch option grid / free-text value.
- **PHOTO / FILE** — evidence capture, below.
- **INSTRUCTION** — display-only ("Read and follow"); never takes a record, never gates.

**Gauge capture (`requires_gauge` measurement steps).** The value pad grows a mandatory gauge
field: the operator **scans or types the gauge's marked identifier** (`equipment_code` —
`Equipment.equipment_id`, the human-readable/barcode code on the gauge). That's the kiosk path
by design: badge-minted operator tokens are fenced away from `/equipment`, so the kiosk cannot
browse gauges — the server resolves the scanned code tenant-scoped (`equipment_id` by PK stays
available to desktop callers; one or the other, never both → 400). The calibration check runs
**before** the tolerance evaluation on purpose — a measurement taken with an out-of-cal gauge
is untrustworthy in both directions, so it must be refused before it can either pass the gate
or trigger the OOT/NCR path — and it is **fail-closed**: the gauge must be ACTIVE **with** a
`next_calibration_date` on or after today; a gauge with **no due date is refused too** (not
demonstrably current). Refusals write no record row: an unknown code 404s ("No gauge with
identifier …"), and a stale/inactive gauge 409s **`GAUGE_OUT_OF_CAL`** — the kiosk renders a
"Gauge refused — not recorded" strip showing the gauge's **status and calibration due date**;
changing the code (i.e. re-scanning) clears the strip. On success the record echoes the
resolved gauge (`gauge: {equipment_id, equipment_code, name}`): the panel confirms
"✓ {name} ({code})" beside the field, prints the gauge on each record's trail line, and
pre-fills the code for the next slot on the same step so serial-after-serial measuring doesn't
force a re-scan (the server revalidates every time regardless).

**Out-of-tolerance → one-tap Hold + file NCR.** The OOT danger strip carries a **HOLD + FILE
NCR** button that opens a confirm sub-state inside the strip (an optional "notes for quality"
field, plus a warning that open labor entries will be clocked out). Confirming posts
`POST /shop-floor/operations/{id}/steps/{step_id}/quality-hold` (in-fence for kiosk operator
tokens), which atomically: creates an **IN_PROCESS NCR** pre-filled from the snapshot step
config (`specification`/`required_value` from LSL/NOM/USL, `actual_value` = the refused
measurement, part/lot/serial from the WO), files a **QUALITY_HOLD `WorkOrderBlocker`**
carrying the new `ncr_id`, flips the operation **ON_HOLD** through the existing blocker hold
pathway, and closes any open time entries (same as `PUT .../hold`) — all audited. The hold
body takes **no gauge field**; the kiosk prepends the refused attempt's gauge context to the
notes ("Measured with gauge {code} — {name}.") so the NCR keeps gauge traceability. On success
both kiosks show a dedicated full-screen **NCR-filed view** (`KioskNcrFiledScreen`) — the NCR
number rendered large enough to tag the part with, plus how many labor entries were closed —
whose single exit lands exactly where the standard HOLD verb lands (single-operator: back to
queue; crew station: back to the board). Non-optimistic throughout: the UI reflects only what
the server returned, and the whole flow is hard-disabled offline like every other kiosk
mutation.

**Per-serial capture.** On a serialized WO the panel carries a serial chip strip ("steps are
recorded per unit"); each record posts with its `serial_number`, and completeness is tracked
per step **per serial** — a serial's chip gets a check once every required step is covered
for that unit.

**Photo / file evidence (two-step, in-fence).** PHOTO opens the tablet's rear camera
(`capture="environment"`, images only); FILE also accepts a PDF. The file uploads **first**
to `POST /shop-floor/operations/{id}/steps/{step_id}/attachment` — which stores it as a
QUALITY_RECORD Document on the WO and exists precisely because the kiosk-token path fence
blocks `/documents/upload` — then the record create references the returned `document_id`
as `attachment_document_id` (the server rejects any document that isn't a QUALITY_RECORD
belonging to that WO). 10 MB cap, checked client-side before the upload and again
server-side.

**Corrections supersede — never edit.** The **Correct** button on an existing record opens a
modal requiring a **reason** plus the replacement value; the replacement runs the full
capture ladder (including the gauge and out-of-tolerance refusals) via
`POST .../records/{record_id}/supersede` and inherits the original's serial. The original
stays on file marked superseded — append-only evidence, per the AS9100D posture.

**Completion gating on the kiosk.**

- **COMPLETE refused.** The complete endpoint 409s with `STEPS_INCOMPLETE` when required
  steps lack conforming records; the kiosk opens the steps view with a "Cannot complete"
  strip listing each missing step (and its outstanding serials) with a jump-to-step button.
  On the crew station, if final production was posted before the refused complete, the
  toast says "Saved production, but completing failed: …" — the production landed.
- **Clock-out at target.** The TimeEntry **always closes normally** with its full
  quantities — labor truth is never trapped behind the steps gate. The operation
  deliberately stays IN_PROGRESS and the clock-out response carries a `steps_incomplete`
  warning block; the kiosk shows an **info** (never error) toast ("Clocked out — N step
  records still needed…") and opens the steps view with the outstanding steps inline.
  Completion then happens via COMPLETE once the records exist.

## Scanning (QR travelers & badges — A0.4)

**What's printed.** Work-order travelers (`/print/traveler/{id}`) carry **URL QR codes** — phone
cameras open the app, while wedge guns type the same URL into the resolver below: **one** header QR
encoding the job-page URL (`https://{host}/work-orders/{id}`; the WO number prints beneath it as
text) and one QR per routing step encoding a shop-floor deep link
(`https://{host}/shop-floor/operations?scan=OP%3A{operation_id}` — a phone scan opens the shop
floor focused on that operation). The earlier separate `WO:{work_order_number}` header QR is gone,
but bare `WO:{number}` / `OP:{id}` text remains valid resolver input. Employee badges
(`/print/badges`, opened from the Users page via multi-select) are CR80 cards (3.375in × 2.125in,
dashed cut lines) whose QR encodes the user's `employee_id` verbatim — the same payload
`POST /auth/employee-login` and the resolver below accept. QR was chosen over Code128 deliberately:
the floor's wedge scanners are 2D imagers that read both, and QR reuses the traveler's existing
`qrcode` dependency (zero new dependencies).

**What scanning does TODAY (resolve/lookup only).** A scanned code is plumbed through
`POST /api/v1/scanner/resolve-action`, which accepts the bare `OP:` / `WO:` / badge codes **and the
traveler URL forms above** (host deliberately not validated — tenancy comes from the authenticated
caller, never the code) and returns what the code *is* (operation / work order / employee /
unknown) and — for an operation — which shop-floor actions the calling user could legally take
right now, with display-ready blocker reasons (see `docs/API.md` → Scanner). It is read-only: no
audit rows, no events, no auth side effects (a badge scan is a lookup; badge login stays on
`POST /auth/employee-login`). The shop-floor operations screen's **Scan box** resolves through
`resolve-action` first — an operation hit filters to the WO, spotlights the row, and opens its
details with the legal actions in the toast; a work-order hit filters to the WO — and falls back to
the legacy `POST /scanner/lookup` for codes the resolver doesn't claim (badge, supplier-part,
part-number). A `?scan=` URL param on `/shop-floor/operations` (a phone-scanned traveler op QR)
runs the same flow **once on load, kiosk mode included**, then strips itself from the URL so
reloads don't re-scan. **Scan-to-act — scan a traveler step and land directly in clock-in / report
/ complete — arrives in Phase 1**; today the `/kiosk` station screen's only scan-driven behavior
remains badge login.

**Wedge-scanner notes.** Stations need **2D imagers** (the codes are QR — a 1D laser scanner will
not read them), configured in **keyboard-wedge mode** with an Enter/CR suffix, the same setup the
badge-login screen already expects. Traveler scan codes print at ≥ 0.6 in so handheld imagers read
them at arm's length; the URL payloads are denser than the old bare `OP:{id}` codes at the same
printed size, so the QR error-correction level stays at the default (M) — don't lower it.

**Traveler print control.** Every traveler prints with a control footer: **UNCONTROLLED WHEN
PRINTED**, the part revision, the printed-at timestamp, and printed-by (from the printing user's
session). The routing revision is *not* on the footer because work orders do not record which
routing revision generated their operations — the footer says so and points at the released
routing. Uncontrolled-when-printed is the standard AS9100D default stance for printed copies; the
footer copy is intended to become configurable **pending the quality manager's controlled-copy
decision**. Staleness signal: `resolve-action` on a traveler's operation QR (the `OP:` code it
embeds) returns
`warning: "routing_revision_changed"` (a documented timestamp proxy, not an exact revision check)
when the part's released routing changed after the WO was released/created.

## Telemetry

Every kiosk mutation — clock-in, clock-out, production report, complete, hold — sends
`source: "kiosk"` (the A0.1 adoption-telemetry channel; see `docs/API.md` → Shop Floor).
Kiosk activity is therefore fully distinguishable from desktop, scanner, import, and
backfill writes on the adoption dashboard. On the server these labor endpoints resolve the
`source` under a trust model: a **kiosk-scoped operator token** (the crew station's
badge-minted `scope="kiosk"` token) is authoritative and now **forces `kiosk`** regardless
of the client hint — previously these endpoints trusted the reported hint — while `import`
is **rejected (422)** as reserved for the bulk-migration loaders; the remaining channels
(kiosk/desktop/scanner/backfill) are stored as declared, or NULL when omitted.

**Process-step records follow the same model.** Step writes (record, correct/supersede,
quality-hold) share the labor endpoints' trust model (the labor endpoints' `import`-rejection
guard aside — a step write stores any declared channel verbatim): the client-reported `source`
hint is stored verbatim — or NULL when omitted; the server never guesses a channel — EXCEPT where
the credential is authoritative: a badge-minted `scope="kiosk"` operator token (crew
station) **always records `kiosk`** regardless of any hint. The single-operator kiosk runs
on a normal employee-login session, so it sends `source: "kiosk"` on every step write,
exactly like clock-in; the crew station sends no hint at all (its badge credential decides).
Either way, kiosk step records count as `kiosk` on the adoption dashboard.

## Offline behavior

- The kiosk polls queue + active job every **15 s**. When a poll or mutation fails, an
  **OFFLINE** banner appears (*"OFFLINE — actions are disabled until the connection is
  restored. Reconnecting…"*); last-known data and any typed values (quantities, selected
  reasons) are kept on screen — nothing the operator has entered is discarded.
- **Mutations are blocked while offline**, not just flagged. Every mutation control —
  clock-in/out confirm, report production, complete, hold, and scrap-reason selection — is
  hard-disabled until the connection is restored, so a tap against a dead connection cannot
  silently drop the record. The offline banner is the accessible explanation for the disabled
  buttons (referenced via `aria-describedby`); disabled action buttons read **Offline**.
- **Process steps follow the same rule**: the steps panel stays readable from its last load,
  and Record / Save evidence / Correct are hard-disabled (buttons read **Offline**) — no
  queued or optimistic step writes.
- There is **no offline write queue**: because mutations are disabled rather than queued, the
  operator retries them once the banner clears. Error toasts linger 12 s so they are readable
  from arm's length.
- The [one-tap **+1 PIECE**](#one-tap-1-piece) lane obeys that rule rather than excepting itself
  from it: the tap is hard-disabled offline, so nothing can be entered against a dead connection.
  What it does hold is a delta tapped while **online** whose window elapsed after the connection
  dropped — it stays on screen saying it is not saved, and posts when the connection returns. That
  is one in-memory delta on one screen, not a queue: it survives no reload and accumulates no
  backlog.
- The [drawing / nest viewer](#drawing--nest-viewer) is a pure read surface and follows the
  same posture: a failed document load renders an **inline** error with a retry (never a
  navigation, never a toastless blank), and nothing is queued.

## Screen wake lock

The kiosk (single-operator and crew-station mode), the wallboard, and the visitor sign-in
tablet request a browser **Screen Wake Lock** on mount (`frontend/src/hooks/useWakeLock.ts`),
so an unattended station's display does not sleep between touches. The browser auto-releases the lock when
the tab is hidden; the hook re-acquires it when the tab becomes visible again, and the
browser may decline the request outright (e.g. low battery) — both are tolerated silently.
The Wake Lock API only exists in **secure contexts** (HTTPS or localhost), so on a
plain-HTTP LAN deployment the hook is a deliberate no-op and the screen staying awake
depends on the device's own display/power settings — that is by design, not a bug.

## Crew station mode (`/kiosk?kiosk=1&station=<id>`)

The crew station is the multi-operator variant of the kiosk, for work centers where several
people work the **same** operation at once (three welders on one weldment). The backend labor
model already supports this — one `TimeEntry` per operator per clock-in window, hour rollups sum
across operators, and `uq_open_time_entry` allows different users on the same operation — so the
crew station changes only the terminal UX and its auth model. It coexists with the
single-operator mode: `?station=<id>` selects crew mode, `?work_center_id=N` keeps the
single-operator kiosk unchanged.

Frontend: `frontend/src/pages/CrewStationKiosk.tsx` +
`frontend/src/services/kioskStationClient.ts` (the isolated fetch helper — it never touches the
global axios client, whose 401→`/login` interceptor would be fatal on an unattended terminal).

### Station PIN model and admin setup

Each crew terminal is a server-side **`kiosk_stations`** record: a label, a **bound work
center** (non-null — the station may only read its own work center's queue), and a shared
numeric **PIN** (4–8 digits, bcrypt-hashed, never echoed back). This is the work-center-bound
twin of the visitor sign-in tablet's `signin_stations` model.

Admin setup (**Admin / Manager**): Work Centers page → **Kiosk Stations** button → the
management modal (list / create / reset-PIN / revoke), which also shows each station's pinned
terminal URL to copy:

```
/kiosk?kiosk=1&station=<id>
```

Pin the terminal's kiosk browser to that URL (same lockdown recommendation as the
single-operator mode). Station lifecycle endpoints live under
`/shop-floor/kiosk-stations` (see `docs/API.md` → Shop Floor); create, PIN reset, revoke, and
every station-login failure write tamper-evident audit rows.

### Two-tier auth

- **Station tier.** Entering the PIN calls the public, rate-limited
  `POST /shop-floor/kiosk-stations/station-login`, which mints a **24 h scoped `type="kiosk"`
  JWT** (sessionStorage only). That token is honored by exactly **two** things: the
  roster-enriched queue read (`GET /shop-floor/work-center-queue/{id}`, its own work center
  only — any other work center is **403**) and the badge-token mint below. Every other endpoint
  rejects it with **401** (`verify_token` accepts only `type="access"` JWTs), so the station can
  never act as a user. The `kiosk_stations` DB row is authoritative on every request — company
  scope comes from the row (never the JWT's `cid`), and the `revoked` flag is re-checked each
  call.
- **Operator tier.** Each badge scan calls `POST /auth/kiosk-badge-token` (station-token-gated),
  exchanging the badge for a **5-minute `scope="kiosk"` access token** with **no refresh
  token** — a shared terminal never holds a long-lived personal credential. The token lives in
  memory only (never persisted) and is **path-fenced in `get_current_user`** to
  `/api/v1/shop-floor/*` plus `/api/v1/auth/employee-logout`; any other path returns **403**.
  Three carve-outs inside the shop-floor prefix are **denied** to kiosk-scoped tokens even for
  MANAGER/ADMIN badges: the station lifecycle endpoints (`/shop-floor/kiosk-stations/*` — a
  scanned manager badge must not be able to reset a station PIN from the shared terminal), the
  labor-approval pair (`/shop-floor/time-entries/{id}/approve|unapprove` — G5-A approval is
  a desktop supervisor workflow), and the manager dispatch tools
  (`GET /shop-floor/dispatch-board` and `PUT /shop-floor/work-centers/{id}/run-order` — reading
  the whole shop's board, or dictating what every machine runs next, is a desk workflow). The
  crew station keeps its own work-center queue read, so operators still see the `RUN n` chips.
  Badge lookup is fenced to the station's company; unknown / inactive / locked / foreign-tenant
  badges are a uniform **401 "Invalid badge"**. Mints and failures are audited
  (`KIOSK_BADGE_TOKEN_ISSUED` / `KIOSK_BADGE_TOKEN_FAILED`).

All labor mutations then hit the **existing** shop-floor endpoints with the operator token, so
the badge-identified **operator — never the station — is the audit actor**, and tenant scoping,
optimistic locking, qualification warnings (G5-B), and `source: "kiosk"` telemetry all apply
unchanged.

### What the crew sees and does

The unlocked station shows the work center's **crew board**: one card per queued operation with
the operation-level tally and a roster chip strip of everyone clocked in, each with a live
per-person timer (computed against the server clock via the queue's `server_time`, so a
fast/slow tablet can't lie). The queue polls every **10 s** and refetches immediately after
every successful action.

Scrap entry on every crew flow (LEAVE clock-out, REPORT PRODUCTION, COMPLETE) uses the same
codes-or-legacy picker as the single-operator mode (see "Scrap reason picker" under
[What operators can do](#what-operators-can-do)) — but the station gets the company's active
codes off the **queue payload itself** (the top-level `scrap_reason_codes` array on
`GET /shop-floor/work-center-queue/{id}`), not a separate read. Deliberately so: badge-minted
kiosk tokens are path-fenced to `/shop-floor` and cannot call
`GET /quality/scrap-reason-codes`, and the station token stays honored by exactly the same two
things (its own queue read + the badge mint) — the two-capability invariant is intact and no
token scope was widened. An empty array means no active codes → the legacy grid.

**Quantity entry on the crew station** is one shared screen (`components/kiosk/KioskQuantityScreen.tsx`)
with a GOOD field, a SCRAP field and one big keypad between them, used by all three quantity flows
below. Alongside the keypad it carries the same **`+1 +5 +25`** (plus **FULL NEST n**) quick-add row
as the single-operator overlays — `components/kiosk/quantityQuickAdds.ts` is the single definition
behind every copy, so the amounts, order, labels and clamp cannot drift between the two terminals.
Five things about it are load-bearing rather than cosmetic:

- It applies to **GOOD only**, on all three flows. There is no scrap quick-add — scrap takes a
  reason and a deliberate entry — and because both fields are on screen at once the row is
  captioned, each button names its target, and a tap **re-points an open scrap keypad at good** so
  the operator's next digit can't land in the field they just steered away from.
- It is bounded by what the **server** will take: the operation target less what is already
  recorded (`quantity_ordered - quantity_complete` on the queue row, and that `quantity_ordered`
  is already the operation target — `component_quantity` when the operation carries one). Both
  writers behind this screen refuse over-target good quantity before any mutation —
  `POST /shop-floor/operations/{id}/production` with 400 *"Quantity (N) cannot exceed quantity
  ordered (T)"* and `POST /shop-floor/clock-out/{id}` with 400 *"Quantity produced exceeds quantity
  ordered"* — so the row clamps at that ceiling and goes **disabled** once the field reaches it
  (captioned `QUICK ADD TO GOOD · MAX n`, or `· OPERATION IS ALREADY AT ITS TARGET` when nothing
  remains). The **keypad is not bounded**: an operator can still key any figure and take the
  server's answer. Only the convenience is clamped.
- It is **opt-in per screen, and the opt-in is the ceiling itself**. LEAVE reached from the
  operator sheet for a job **outside this station's queue** has no queue row, so no ceiling, so
  **no row** (the crew tally banner is absent there for the same reason) — never an unbounded one.
  On COMPLETE the good field pre-fills at exactly that ceiling, so the row arrives disabled and
  exists for rebuilding a cleared count; recording more than the operation target is an office
  over-count, not a tap here. The over-count **CORRECTION** screen has no quick adds at all — it
  removes pieces.
- It sits **below the keypad** here, where the narrow single-operator overlays stack it above
  theirs. Measured, not styled: this screen's stack is taller, and at **1024x768** (landscape iPad)
  a row above the keypad pushed the keypad's CLEAR / 0 / backspace row 49px under the fold. Below
  it, the keypad stays whole at both tablet orientations. Anything added above the keypad on this
  screen owes the same measurement.
- On **REPORT PRODUCTION** the row is `+5 +25` (plus **FULL NEST n**): `+1` moves to the
  [one-tap **+1 PIECE** lane](#one-tap-1-piece) that renders beside the fields on that flow, so `+1`
  on a screen means exactly one thing. LEAVE and COMPLETE carry no lane and keep the full
  `+1 +5 +25` row. The lane's tapped-but-unbanked count also comes **out of** this row's ceiling, so
  a pending delta plus a keyed entry can't together key a refusal.

#### The fold measurement the one-tap lane owed

The lane is the control an operator taps once per finished part, so it has to be reachable without
a scroll — but stacked above the fields and the pad it pushed the keypad's CLEAR / 0 / backspace
row to **y=941 on a 768px-tall viewport, 173px under the fold**, against the 49px that got the
quick-add row moved below the pad in the first place. Margins cannot recover a ~190px block, so the
lane **moves sideways rather than shrinking**: on REPORT PRODUCTION the screen widens to `max-w-4xl`
and splits into two columns at the `lg` breakpoint — lane + fields on the left, keypad + quick adds
on the right — spending the ~350px of horizontal room a 1024px-wide landscape iPad was leaving
unused at `max-w-2xl`.

Measured after the change, keypad bottom row:

| Orientation | Layout | Keypad bottom row | Clearance | Page |
| --- | --- | --- | --- | --- |
| 1024x768 landscape | two-column | y=519 | **249px** | fits entirely, `scrollHeight` 768, **no scroll**; CONFIRM ends at y=699, above the fold — which it never was before |
| 768x1024 portrait | single column (below `lg`) | y=875 | **149px** | CONFIRM ends at y=1055, the same ~30px under the fold portrait has always had |

No horizontal overflow at either size. Anything added to **either column** owes the same
measurement.

- **JOIN / LEAVE (badge decides).** Tap a job → "scan badge to join or leave". If the badge's
  user is already on the roster, it's a **LEAVE**: the quantity screen closes their own entry
  (`POST /shop-floor/clock-out/{their time_entry_id}`; 0/0 allowed, scrap requires a structured
  reason). Otherwise it's a **JOIN** (`POST /shop-floor/clock-in`, entry type **Run** by default
  with a **Setup** toggle). Joining while clocked in elsewhere is allowed — the kiosk shows an
  informational "also clocked in at …" toast, never a block. A stale-roster double join gets the
  server's 400 ("already clocked in") as an info toast plus a refresh. **Badge-first** also
  works: scanning a badge at the board opens that operator's sheet — their open entries (tap to
  clock out) and the joinable jobs at this station.
- **REPORT PRODUCTION (badge-first).** The verb opens a **badge scan**, and the scan opens a
  quantity screen bound to that operator; everything recorded there posts under that badge's token
  (`POST /shop-floor/operations/{id}/production`) with no second signature screen. This is the shape
  **STEPS** and **DOCS** already had on this station — badge-gate *entry*, then write N records under
  the token — and process-step records are quality records, so the precedent is not a lesser one.
  **Attribution is unchanged**: nothing is written without a badge-minted operator token, and the
  audit actor is still the scanned operator, never the station. What changed is *when* the operator
  learns whose name they are recording under — before entering numbers rather than after — which is
  also what makes one tap per finished part possible at all: a signature after the fact is, by
  construction, a second action per piece. Two ways to record on that screen, deliberately unalike:
  the [one-tap **+1 PIECE** lane](#one-tap-1-piece) posts itself after its window, while the keypad
  and the `+5 / +25 / FULL NEST n` row fill the GOOD field and post on **RECORD**.
  - **The 5-minute badge token can expire mid-run, and nothing is re-keyed when it does.** A keyed
    entry refused **401** carries its good/scrap/reason back to the scan screen (*"Saving: 7
    good…"*), and the re-scan saves it and returns the operator to the quantity screen. A one-tap
    delta refused the same way **parks**: the lane stops posting against the dead credential, the
    scan screen states what is held (*"N tapped pcs still waiting to be saved"*), and a re-scan **by
    the same operator, on the same operation** un-parks it.

    A scan proves a credential is **valid**. It says nothing about **whose** the held pieces are, and
    the two must not be confused: every delta is stamped with its `(operator, operation)` pair at tap
    time and may only ever post while that same pair is bound. A scan by anyone else, or on any other
    job, does **not** adopt the held count — it goes to **ORPHANED**, which names the operator and
    the job on the lane, refuses further taps, and offers no "save anyway". Only the original pair
    returning can bank it; otherwise the pieces belong in an office entry. This is not tidiness: the
    endpoint credits the posting token's **TimeEntry** and moves stock against the operation in the
    URL, so a count posted under the wrong pair mis-attributes labour *and* consumes material on
    another work order's part and lot (invariant 6), permanently and indistinguishably from a real
    report.
- **CORRECT OVER-COUNT.** The same `KioskCorrectionScreen` as the single-operator mode (quantity to
  remove + a **required** correction-reason tile, distinct from the scrap grid), then a
  **badge-signature scan** saves the walk-back as that operator
  (`POST /shop-floor/operations/{id}/reduce-production`). The signing badge **must have an open
  clock-in on this op**, and the server bounds the walk-back to **their own unapproved** recorded
  evidence — their open clock-in first, then their own earlier unapproved sessions on this op
  (crew-safe: one welder can't erase another's pieces; **approved** labor is excluded — a
  signed-off count needs a supervisor). It **removes good pieces
  over-reported by mistake — not scrap**, and is refused once the op/WO is complete (**409** "ask a
  supervisor"). The success toast quotes the corrected crew total; a verbatim refusal (e.g. "you can
  only remove up to the N piece(s) you recorded on this operation") renders **inline on the
  correction screen** and keeps the quantity + reason so the **right** badge can re-scan.
- **COMPLETE (crew-wide, confirmed).** Completion auto-closes **every** operator's open entry on
  the operation, so the confirm dialog names who else gets clocked out, with their running
  durations, re-derived live from queue state. A badge scan inside the dialog signs it; if final
  new pieces were entered, the kiosk posts `production` first, then `complete` — if the
  production lands but completion is refused, it says so ("Saved production, but completing
  failed: …"). A concurrent 409 is surfaced verbatim and the board refreshes. The success toast
  names everyone auto-clocked-out (the complete response's `closed_time_entries`).
- **HOLD.** The same required blocker-category grid as the single-operator kiosk, then a
  badge-signature scan (`PUT /shop-floor/operations/{id}/hold`).
- **RESUME.** Held jobs sit in an **ON HOLD** section under the joinable board, each card
  carrying why it stopped — category, severity and who held it, but **not** the blocker's
  free-text note, which the server does not send to a shared station — and a single RESUME
  verb (never a join target). Confirm overlay → badge signature →
  `PUT /shop-floor/operations/{id}/resume`. Until this existed the station
  could place a hold but not lift one — `kioskStationClient` carried `holdOperation` and no
  twin — so a mis-tap on the floor needed a desktop to undo. See
  [Held work and RESUME](#held-work-and-resume), including why the kiosk resumes past a
  blocker but cannot clear it, why the note stops at the station boundary, and why a resume
  restores rather than releases.
- **STEPS (badge-gated).** The job screen's steps verb ("Steps 2/6", present when the
  operation carries process-sheet steps) opens a badge scan — step records are made in the
  scanned operator's name — then the shared steps panel bound to that badge-minted token
  (see [Process steps](#process-steps-process-sheets-capture) for the capture flow). A
  clock-out that reaches target with required steps outstanding, or a COMPLETE refused with
  `STEPS_INCOMPLETE`, lands the signing operator in the same steps view with the missing
  steps inline. Like every other flow, the 90 s idle reset abandons a half-entered steps
  screen back to the board; a mid-flow 401 (expired badge token) returns to the badge scan.
- **DOCS (badge-gated).** The job screen's **View nest / drawing** button (present when the
  operation's nest carries a reference PDF) opens a badge scan — "drawings and nests are
  controlled documents" — then the shared full-screen viewer bound to that badge-minted token
  (see [Drawing / nest viewer](#drawing--nest-viewer)). The job card's inline nest panel is
  info-only (no pre-badge PDF fetch); an expired badge token mid-view renders "Badge session
  expired — rescan to view" inline, never a navigation.

Every verb is server-gated and therefore **non-optimistic** — the kiosk shows a loading state,
reflects only what the server returns, and surfaces rejections verbatim.

### Shared tally — the double-count guard

Quantities are **additive server-side** with no crew de-duplication, so the guard against two
welders both reporting the same 10 pieces is the prominently displayed operation-level tally:
every quantity screen carries the banner **"CREW TOTAL SO FAR: 37 of 50 · 2 scrap — enter only
NEW pieces"**, and the production success toast quotes the new crew total. Train crews to enter
only pieces not yet counted; the tally (`quantity_complete` / `quantity_scrapped` on the queue
row) is server-derived, so all terminals and desktop views agree.

### Idle = flow reset, not logout

After **90 s** of inactivity on any screen other than the crew board, a half-entered flow
(quantities, badge prompt, hold reason) is abandoned back to the board so a walked-away operator
can't block the crew — but the **station stays unlocked**. There is one deliberate exception to
"abandoned": a [one-tap **+1 PIECE**](#one-tap-1-piece) count that is still inside its undo window
is **posted**, not dropped. Those pieces are not a half-entered flow — the tap was the commit, and
the window only ever offered a way out of it that walking away is not. The same holds for Cancel,
the ghost-guard, and **Lock station**. There is no idle station logout: the
station locks only via the explicit **Lock station** button or when a station-authed read gets a
**401** (revoked/expired), which drops the token and returns to the PIN screen. The reset never
fires mid-request. This differs deliberately from the single-operator mode's idle **logout**:
that mode binds a personal login to the terminal; the crew station holds no personal credential
between actions (operator tokens die in ≤5 minutes on their own).

### Revocation runbook

1. Work Centers → Kiosk Stations → **Revoke** on the station (or
   `POST /shop-floor/kiosk-stations/{id}/revoke`, Admin/Manager). Revocation is an idempotent,
   audited status flip — the row is kept as the issuance record, never deleted.
2. The station's DB row is re-checked on **every** queue read and badge mint, so the tablet
   locks to the PIN screen on its next poll (≤10 s) even though its JWT is still
   signature-valid.
3. Outstanding badge-minted operator tokens are not individually revocable but expire on their
   own in **≤5 minutes**.
4. There is no un-revoke: to bring the terminal back, create a new station (new id → new pinned
   URL) or, for a suspected-PIN-leak only, use **Reset PIN** on a still-active station
   (PIN reset does not invalidate the already-minted 24 h station token — revoke for that).

### Known residual: WebSocket auth is not path-fenced

The kiosk-scope path fence lives in `get_current_user`, which sees the HTTP request path. The
WebSocket endpoints authenticate via `get_current_user_from_token` (`app/core/security.py`),
which has no request path — so a `scope="kiosk"` operator token **can** open the `/ws/*`
channels during its ≤5-minute life. Accepted residual: those channels are read-only,
tenant-scoped broadcast streams (no mutations), and the token identifies a real operator of the
same tenant. The crew station itself does not use WebSockets (v1 is poll-only, 10 s).

### Rate limits

| Path | Limit | Note |
|------|-------|------|
| `POST /shop-floor/kiosk-stations/station-login` | 5/minute per IP | Same posture as the visitor tablet's PIN unlock |
| `POST /auth/kiosk-badge-token` | 30/minute per IP | Generous — a whole crew taps one terminal — but safe: the endpoint is station-token-gated, not public |

The public `POST /auth/employee-login` (10/minute — see Badge login above) is not used by the
crew station at all.
