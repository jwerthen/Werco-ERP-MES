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

Frontend: `frontend/src/pages/OperatorKiosk.tsx` and `frontend/src/pages/CrewStationKiosk.tsx`
(+ `frontend/src/components/kiosk/`, `frontend/src/utils/kiosk.ts`,
`frontend/src/hooks/useKioskIdleLogout.ts`, `frontend/src/services/kioskStationClient.ts`).

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
  field). Manual entry uses the on-screen number pad.
- Badge = identity: one operator per login, no shared accounts. Backend error details
  (invalid ID, locked account, ambiguous badge → 409) are shown verbatim on the badge screen.

## Idle auto-logout

- Default **240 s** of no activity (tap / key / scan / wheel), overridable per station via
  `?idle_logout_s=N` (clamped 30–600). The ceiling is intentionally below the global
  15-minute app idle redirect so the kiosk's badge screen — not a hard `/login` redirect —
  always wins.
- A countdown banner appears for the final 30 s; any touch resets the timer.
- Logout is a **client-side token clear** (same as tapping LOG OUT). A server-side audit
  event for the idle logout itself is a known gap, tracked separately.

## What operators can do

1. **Queue → confirm → clock in (2 taps).** The station queue lists the work center's
   operations; tapping a job shows a confirm card; CLOCK IN calls
   `POST /shop-floor/clock-in` (entry type `run`). When the operation carries
   process-sheet steps, the confirm card adds a **REVIEW STEPS 2/6** button so the
   operator can read the steps before starting (see
   [Process steps](#process-steps-process-sheets-capture)).
2. **Active-job banner** (pinned while clocked in), with three verbs — plus a
   **PROCESS STEPS · 2/6 RECORDED** button when the operation carries steps:
   - **REPORT PRODUCTION** — `POST /shop-floor/operations/{id}/production` with good/scrap
     deltas. Any scrap quantity **requires** an explicit reason picked from the scrap grid
     (no default; see "Scrap reason picker" below for what the grid contains and what is
     sent). This is no longer a kiosk-only guardrail: the server rejects a positive scrap
     delta with no reason (and the same rule on clock-out) with **422**, so reasonless scrap
     can't be posted around the UI.
   - **COMPLETE** — clock-out first (`POST /shop-floor/clock-out/{id}` with final counts and,
     when any scrap is entered, the same scrap-grid reason), then
     `POST /shop-floor/operations/{id}/complete` at the target quantity. If the clock-out
     lands but the completion is refused, the kiosk says so — labor is closed either way.
     A completion refused with **409 `STEPS_INCOMPLETE`** (required process-sheet steps
     missing conforming records) opens the steps view with the outstanding steps rendered
     inline (see [Process steps](#process-steps-process-sheets-capture)).
   - **HOLD** — a required blocker-category grid (material missing, machine down, tooling
     missing, quality hold, …), then `PUT /shop-floor/operations/{id}/hold` at `medium`
     severity. A kiosk hold files the same structured `WorkOrderBlocker` a supervisor would.

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
predecessor not complete, on-hold, optimistic-lock 409s, qualification warnings) is
surfaced **verbatim** in the error toast and never suppressed or retried around. There is
no resume-from-hold, inspection, or labor-approval verb on the kiosk.

**Laser nests at clock-in.** For laser-cutting operations the kiosk surfaces the active nest at
all three touch points so the operator can confirm the right sheet before cutting: the queue card
(`KioskQueueCard` — CNC#/nest name, `completed`/`planned` runs, material•thickness, and a "PDF" chip
when a reference PDF is attached), the clock-in confirm card, and the active-job banner (both
rendering `LaserNestOperatorPanel`, which previews the PDF inline). The data is the `laser_nest` object that
`GET /shop-floor/work-center-queue/{id}` puts on each queue row and `GET /shop-floor/my-active-job`
puts on the active job — `null` for non-laser operations, and a soft-deleted manual nest never
appears (see `docs/API.md` → Shop Floor → "Laser-nest payload on operator reads" for the full
shape). The optional reference PDF is fetched **inline** from `GET /laser-nests/{id}/document`
when the operator opens it (no approval workflow, and it never gates clock-in).

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
  Two carve-outs inside the shop-floor prefix are **denied** to kiosk-scoped tokens even for
  MANAGER/ADMIN badges: the station lifecycle endpoints (`/shop-floor/kiosk-stations/*` — a
  scanned manager badge must not be able to reset a station PIN from the shared terminal) and
  the labor-approval pair (`/shop-floor/time-entries/{id}/approve|unapprove` — G5-A approval is
  a desktop supervisor workflow).
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

- **JOIN / LEAVE (badge decides).** Tap a job → "scan badge to join or leave". If the badge's
  user is already on the roster, it's a **LEAVE**: the quantity screen closes their own entry
  (`POST /shop-floor/clock-out/{their time_entry_id}`; 0/0 allowed, scrap requires a structured
  reason). Otherwise it's a **JOIN** (`POST /shop-floor/clock-in`, entry type **Run** by default
  with a **Setup** toggle). Joining while clocked in elsewhere is allowed — the kiosk shows an
  informational "also clocked in at …" toast, never a block. A stale-roster double join gets the
  server's 400 ("already clocked in") as an info toast plus a refresh. **Badge-first** also
  works: scanning a badge at the board opens that operator's sheet — their open entries (tap to
  clock out) and the joinable jobs at this station.
- **REPORT PRODUCTION.** Quantities first, then a **badge-signature scan** saves the report as
  that operator (`POST /shop-floor/operations/{id}/production`).
- **COMPLETE (crew-wide, confirmed).** Completion auto-closes **every** operator's open entry on
  the operation, so the confirm dialog names who else gets clocked out, with their running
  durations, re-derived live from queue state. A badge scan inside the dialog signs it; if final
  new pieces were entered, the kiosk posts `production` first, then `complete` — if the
  production lands but completion is refused, it says so ("Saved production, but completing
  failed: …"). A concurrent 409 is surfaced verbatim and the board refreshes. The success toast
  names everyone auto-clocked-out (the complete response's `closed_time_entries`).
- **HOLD.** The same required blocker-category grid as the single-operator kiosk, then a
  badge-signature scan (`PUT /shop-floor/operations/{id}/hold`).
- **STEPS (badge-gated).** The job screen's steps verb ("Steps 2/6", present when the
  operation carries process-sheet steps) opens a badge scan — step records are made in the
  scanned operator's name — then the shared steps panel bound to that badge-minted token
  (see [Process steps](#process-steps-process-sheets-capture) for the capture flow). A
  clock-out that reaches target with required steps outstanding, or a COMPLETE refused with
  `STEPS_INCOMPLETE`, lands the signing operator in the same steps view with the missing
  steps inline. Like every other flow, the 90 s idle reset abandons a half-entered steps
  screen back to the board; a mid-flow 401 (expired badge token) returns to the badge scan.

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
can't block the crew — but the **station stays unlocked**. There is no idle station logout: the
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

The public `POST /auth/employee-login` (3/minute) is untouched — the crew station never uses it.
