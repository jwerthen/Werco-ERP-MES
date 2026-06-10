# Shop-Floor TV Wallboard

Read-only, full-screen status board (`/wallboard`) for unattended shop TVs (A0.5): per-work-center
live jobs (who's on it, WO/op, elapsed time, qty done/target), queue depth, blocker and downtime
status, plus late and blocked work-order tickers.

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
