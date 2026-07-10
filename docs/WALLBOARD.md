# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5): a trailing-30-day
KPI strip, per-work-center live jobs (who's on it, WO/op, elapsed time, qty done/target), queue
depth, blocker and downtime status, plus late and blocked work-order tickers.

This is **not** the interactive operator kiosk (`/shop-floor/operations?kiosk=1`, badge login —
see `docs/onboarding/02-operator-shop-floor.md`). The wallboard takes no input and writes nothing;
it authenticates with a **scoped display token** instead of a user session.

## Setting up a TV

1. **Issue a token** — Admin Settings → **Wallboard Displays** tab → New display. Give it a label
   naming the physical screen ("North wall TV") and a lifetime (default 90 days, max 365). The UI
   is on the Admin Settings page (admin-gated); the API also allows Manager
   (`require_role([ADMIN, MANAGER])`).
2. **Copy the one-time URL.** The token JWT and a ready-made
   `https://<your-host>/wallboard?token=<jwt>` URL are shown **exactly once**, with copy buttons.
   The server never returns the token again — if you lose it, revoke and issue a new one.
3. **Open the URL on the TV's browser.** On first load the page moves the token from the URL into
   `sessionStorage` and scrubs it from the address bar (so it doesn't linger in screenshots or
   over-the-shoulder photos). Because it lives in `sessionStorage`, closing the browser drops it —
   bookmark/relaunch with the full `?token=` URL, or keep the browser session alive.
4. **Kiosk mode.** Run the TV's browser in kiosk/full-screen mode with sleep disabled (e.g.
   `chromium --kiosk 'https://<host>/wallboard?token=<jwt>'`, or the smart-TV browser's full-screen
   setting). The page is self-contained — no app chrome, no login screen.

### One TV per department

Append `&dept=<work_center_type>` to show only that department's work centers (case-insensitive
match on the work-center type), e.g. `/wallboard?token=<jwt>&dept=machining`.

## Behavior

- **Refresh:** polls every **30 seconds** (no WebSocket in v1 — reliability first).
- **Offline:** if a poll fails, the board shows an **OFFLINE** banner and keeps displaying the last
  good data; it recovers automatically on the next successful poll.
- **No token:** without a valid token (or signed-in session) the page shows guidance instead of
  data — it never redirects to login.
- **Privacy:** operator names are truncated server-side to "First L." — the payload is built for a
  public screen. A signed-in user can also open `/wallboard` (scoped to their active company).

## KPI strip (Lean Phase 1)

A row of floor-visible KPIs across the top of the board, from the optional `kpi_strip` block on the
wallboard payload:

| Tile | Field | Meaning |
|------|-------|---------|
| OTD 30d | `otd_ship_pct_30d` | Ship-based on-time delivery — full ordered quantity shipped on/before promise |
| FPY 30d | `fpy_pct_30d` | Overall first-pass yield across completed operations |
| Scrap 30d | `scrap_pct_30d` | Scrapped ÷ (complete + scrapped) across completed operations |
| Open WOs | `open_wip_count` | Open released WOs (released / in-progress / on-hold) — live, not windowed |
| Avg WIP Age | `avg_wip_age_days` | Mean days since release of those open WOs — live, not windowed |

- **Trailing 30 days.** The three percentages cover the last 30 days ending today; the two WIP
  figures are a live snapshot.
- **Nulls render as "—".** A percentage is `null` when the window has insufficient data (empty
  denominator) — the board shows an em dash, never a fake 0% or 100%.
- **~5-minute staleness is by design.** The strip is server-side cached per company (~5-min TTL) so
  the 30 s poll doesn't recompute analytics; trailing-30-day numbers don't move faster than that.
  The live board panels are unaffected — only the strip is cached.
- **Company-wide, not per-department.** `&dept=` narrows the work-center cards but **not** the
  strip — every TV shows the same plant-level KPIs.
- **Best-effort.** Aggregate numbers only (nothing operator-identifying). If the KPI computation
  fails, `kpi_strip` is `null` on that poll and the board renders without the strip — an analytics
  error never takes down the live board (the block is also optional on the payload, so a board
  pointed at an older backend renders unchanged). The endpoint stays a zero-write read and the
  display-token model below is unchanged.

## Security — treat the token like a password

A display token can **only** read the wallboard endpoint — it is rejected (401) everywhere else,
carries no user identity, and can write nothing. Still:

- The URL containing `?token=` grants wallboard access to whoever has it — share it only with
  whoever mounts the TV, and don't post it in chat/tickets.
- **If a TV is lost, stolen, or replaced, revoke its token** from Admin Settings → Wallboard
  Displays. Revocation is checked server-side on every request (the DB row, not the JWT, is
  authoritative), so the screen goes dark within one ~30s poll.
- Issuance and revocation are tamper-evidently audit-logged; label tokens clearly so the audit
  trail names the physical screen.
- Tokens expire (≤365 days). Re-issue and re-point the TV before expiry — expired tokens are
  rejected the same way as revoked ones.

## Reference

- Endpoints and threat model: `docs/API.md` → Authentication → Display tokens, and Shop Floor →
  wallboard callout.
- Role gating: `docs/RBAC_PERMISSIONS.md` → Admin.
- Implementation: `backend/app/api/deps.py` (`get_display_or_user`),
  `backend/app/services/wallboard_service.py`, `frontend/src/pages/Wallboard.tsx`,
  `frontend/src/services/wallboardClient.ts`.
