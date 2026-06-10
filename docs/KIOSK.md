# Operator Kiosk (`/kiosk`)

The operator kiosk (A0.3) is a touch-first, full-screen station screen for fixed shop-floor
terminals. It renders outside the normal app `Layout` and handles its own auth: an
unauthenticated visitor gets the badge-login screen, never a redirect to `/login`. It is
deliberately minimal — badge in, clock in to a queued job in two taps, and report
production / complete / hold on the active job. No supervisor verbs (inspection, labor
approval, resume-from-hold, or any override) exist on this screen.

Frontend: `frontend/src/pages/OperatorKiosk.tsx` (+ `frontend/src/components/kiosk/`,
`frontend/src/utils/kiosk.ts`, `frontend/src/hooks/useKioskIdleLogout.ts`).

## Station URL and parameters

Each physical terminal is identified by its URL — there is no server-side station record:

```
/kiosk?kiosk=1&work_center_id=12&work_center_code=LASER1
```

| Param | Required | Meaning |
| --- | --- | --- |
| `work_center_id` | **Yes** | Numeric work-center id; drives the station queue (`GET /shop-floor/work-center-queue/{id}`). Without it the kiosk shows a "Station not configured" screen and does nothing. |
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
   `POST /shop-floor/clock-in` (entry type `run`).
2. **Active-job banner** (pinned while clocked in), with three verbs:
   - **REPORT PRODUCTION** — `POST /shop-floor/operations/{id}/production` with good/scrap
     deltas. Any scrap quantity **requires** a structured reason picked from the shop-standard
     grid (no default, no free text); the reason is sent as the endpoint's `scrap_reason`
     field and stored on the operator's time entry.
   - **COMPLETE** — clock-out first (`POST /shop-floor/clock-out/{id}` with final counts and
     any scrap reason), then `POST /shop-floor/operations/{id}/complete` at the target
     quantity. If the clock-out lands but the completion is refused, the kiosk says so —
     labor is closed either way.
   - **HOLD** — a required blocker-category grid (material missing, machine down, tooling
     missing, quality hold, …), then `PUT /shop-floor/operations/{id}/hold` at `medium`
     severity. A kiosk hold files the same structured `WorkOrderBlocker` a supervisor would.

**What operators cannot do.** No overrides: backend gating (operation sequence /
predecessor not complete, on-hold, optimistic-lock 409s, qualification warnings) is
surfaced **verbatim** in the error toast and never suppressed or retried around. There is
no resume-from-hold, inspection, or labor-approval verb on the kiosk.

## Scanning (QR travelers & badges — A0.4)

**What's printed.** Work-order travelers (`/print/traveler/{id}`) carry two kinds of scan code: a
`WO:{work_order_number}` QR in the header and one `OP:{operation_id}` QR per routing step in the
routing table. Employee badges (`/print/badges`, opened from the Users page via multi-select) are
CR80 cards (3.375in × 2.125in, dashed cut lines) whose QR encodes the user's `employee_id`
verbatim — the same payload `POST /auth/employee-login` and the resolver below accept. QR was
chosen over Code128 deliberately: the floor's wedge scanners are 2D imagers that read both, and QR
reuses the traveler's existing `qrcode` dependency (zero new dependencies).

**What scanning does TODAY (resolve/lookup only).** A scanned code is plumbed through
`POST /api/v1/scanner/resolve-action`, which returns what the code *is* (operation / work order /
employee / unknown) and — for an operation — which shop-floor actions the calling user could
legally take right now, with display-ready blocker reasons (see `docs/API.md` → Scanner). It is
read-only: no audit rows, no events, no auth side effects (a badge scan is a lookup; badge login
stays on `POST /auth/employee-login`). **Scan-to-act — scan a traveler step and land directly in
clock-in / report / complete — arrives in Phase 1**; today the kiosk's only scan-driven behavior
remains badge login.

**Wedge-scanner notes.** Stations need **2D imagers** (the codes are QR — a 1D laser scanner will
not read them), configured in **keyboard-wedge mode** with an Enter/CR suffix, the same setup the
badge-login screen already expects. Traveler scan codes print at ≥ 0.6 in so handheld imagers read
them at arm's length.

**Traveler print control.** Every traveler prints with a control footer: **UNCONTROLLED WHEN
PRINTED**, the part revision, the printed-at timestamp, and printed-by (from the printing user's
session). The routing revision is *not* on the footer because work orders do not record which
routing revision generated their operations — the footer says so and points at the released
routing. Uncontrolled-when-printed is the standard AS9100D default stance for printed copies; the
footer copy is intended to become configurable **pending the quality manager's controlled-copy
decision**. Staleness signal: `resolve-action` on a traveler's `OP:` code returns
`warning: "routing_revision_changed"` (a documented timestamp proxy, not an exact revision check)
when the part's released routing changed after the WO was released/created.

## Telemetry

Every kiosk mutation — clock-in, clock-out, production report, complete, hold — sends
`source: "kiosk"` (the A0.1 adoption-telemetry channel; see `docs/API.md` → Shop Floor).
Kiosk activity is therefore fully distinguishable from desktop, scanner, import, and
backfill writes on the adoption dashboard.

## Offline behavior

- The kiosk polls queue + active job every **15 s**. When a poll or mutation fails, an
  **OFFLINE** banner appears; last-known data and any typed values (quantities, selected
  reasons) are kept on screen — nothing the operator has entered is discarded.
- There is **no offline write queue**: a mutation that fails was not saved, and the
  operator retries it once the banner clears. Error toasts linger 12 s so they are readable
  from arm's length.
