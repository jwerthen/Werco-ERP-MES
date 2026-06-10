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
