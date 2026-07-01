# Visitor Sign-In Tablet (`/visitor-signin`)

A standalone, full-screen lobby tablet that lets visitors **self-serve sign in and sign out** at a
facility entrance. Every visit is recorded in the ERP — name, company, host, purpose, time in/out,
and a safety/NDA acknowledgment — and the log is viewable, searchable, and exportable by staff for
audit (AS9100D / CMMC visitor-control evidence).

This is **not** the interactive operator kiosk (`/kiosk`, badge login — see `docs/KIOSK.md`) and
**not** the read-only TV wallboard (`/wallboard` — see `docs/WALLBOARD.md`). Like both of those, the
tablet renders outside the normal app `Layout` (no app chrome, no `/login` redirect) and
authenticates with its own scoped credential rather than a user session.

Frontend: `frontend/src/pages/VisitorSignIn.tsx` (tablet), `frontend/src/pages/VisitorLog.tsx`
(admin log + station management), `frontend/src/services/signinClient.ts` (isolated fetch client),
`frontend/src/components/visitor/visitorConstants.tsx`.
Backend: `backend/app/api/endpoints/visitor_logs.py`, `backend/app/services/visitor_log_service.py`,
`backend/app/services/signin_station_service.py`, `backend/app/models/visitor_log.py`,
`backend/app/models/signin_station.py`, `backend/app/api/deps.py` (`get_signin_principal`),
`backend/app/core/security.py` (`create_signin_token` / `verify_signin_token`).

## Auth model — shared-PIN station

The tablet is unlocked once with a **per-company shared PIN** (not tied to a person). This is a
structural twin of the wallboard display-token mechanism, with two deliberate differences:

- The unlock is a **PIN** the tablet exchanges for a token, not a one-time URL link.
- The minted token authorizes exactly **two scoped writes** — visitor sign-in and sign-out — and
  nothing else.

| | Wallboard display token | Visitor sign-in station token |
| --- | --- | --- |
| JWT `type` claim | `"display"` | `"signin"` |
| How it's obtained | one-time `?token=<jwt>` URL, captured from the URL into `sessionStorage` | minted by **PIN**: `POST /visitor-logs/station-login {station_id, pin}` |
| Lifetime | ≤ 365 days (default 90) | **24 h** |
| Authorizes | the read-only `GET /shop-floor/wallboard` | the two writes `POST /visitor-logs/sign-in` and `/sign-out` |
| Auth dependency | `get_display_or_user` | `get_signin_principal` |
| Revocation anchor | `display_tokens` row | `signin_stations` row |

**The minting flow:**

1. An Admin/Manager creates a **SigninStation** (label + numeric PIN) from the Visitor Log admin
   page. The PIN is bcrypt-hashed at rest in `signin_stations.pin_hash`; the plaintext PIN is never
   stored and never echoed back.
2. The tablet opens at `/visitor-signin?station=<id>`. Staff (reception) enter the shared PIN once.
3. `POST /visitor-logs/station-login` verifies the PIN against `pin_hash` and that the station is
   not revoked, then mints a scoped JWT with `type="signin"` and claims `{sid, cid, label, exp,
   iat}`, **24 h** TTL. The token is returned exactly once. **The `signin_stations` row — not a
   per-token row — is the revocation anchor.**
4. The tablet holds the token in **`sessionStorage`** and attaches it only to calls made through the
   isolated `signinClient` (`Authorization: Bearer …`). The token **never** enters the global axios
   instance — that client's 401 interceptor force-redirects to `/login`, which would be fatal on an
   unattended tablet.

**Two-layer enforcement on the writes.** The two write endpoints depend on `get_signin_principal`,
which accepts **either** a normal staff access token **or** a `type="signin"` station token. For the
station path it runs the wallboard two-layer check, DB-authoritative throughout:

1. verify JWT signature / expiry / `type == "signin"` (`verify_signin_token`)
2. look up the `signin_stations` row by `sid` — it must exist and not be revoked
3. the JWT's `cid` claim must match the row's `company_id`

The active company comes from the **DB row**, never from the client's `cid` claim, so a forged or
stale `cid` can never widen tenant scope.

**Security fences (do not break):**

- `verify_token` still rejects any JWT whose `type != "access"`, so a signin token gets **401**
  everywhere except the two visitor writes. `get_signin_principal` and `get_display_or_user` are the
  only two dependencies that honor a non-`access` token type, and they stay separate — the read-only
  wallboard path is uncontaminated by the visitor-write path.
- Revocation is **two-layer and instant**: revoking the station flips `revoked=True` (the row is
  kept, never deleted), and *both* `station-login` and `get_signin_principal` re-check it on **every
  request** — so a revoked station can mint no new token and any token it already minted stops
  working on its next call.

## Station setup (admin)

On the **Visitor Log** page (`/visitor-log`, ADMIN/MANAGER/SUPERVISOR), the **Stations** button
opens the station-management modal:

1. **Create a station.** Give it a label naming the physical tablet ("Lobby Tablet") and a numeric
   **PIN**. The label becomes the audit actor string for every visit that tablet records.
2. **Point the tablet at its URL.** The modal shows `/<your-host>/visitor-signin?station=<id>` for
   each station with a copy button. Open that URL on the tablet's browser (kiosk/full-screen mode
   recommended, sleep disabled). On first use, reception enters the PIN once to start the session.
3. **Reset PIN** re-hashes the shared PIN in place (the station id / URL is unchanged); existing
   tokens keep working until they expire or the station is revoked.
4. **Revoke** kills the station: it can mint no new token, and the tablet loses access on its next
   request. Revocation is a status flip (idempotent), not a delete, so the issuance trail survives.

Create / reset-PIN / revoke are all **ADMIN/MANAGER** (Supervisors can view the log but not manage
stations); each is tamper-evidently audit-logged.

## Visitor flow (on the tablet)

1. **PIN unlock.** With no/expired token the tablet shows a numeric keypad. The PIN is 4–8 digits
   (see the security note below for the recommended length); a bad PIN shows the server's rejection
   and clears the field.
2. **Welcome.** Two large touch targets: **Sign In** / **Sign Out**.
3. **Sign in** — a touch-first form: name (required), company, phone, "who are you here to see?"
   (host), **purpose** chosen from structured tiles (Meeting · Delivery · Contractor · Interview ·
   Audit · Other), a free-text **note required only when Other** is chosen, and a **safety/NDA
   acknowledgment checkbox that must be checked to submit**. Submit is **non-optimistic**: it shows a
   loading state, reflects only the server's response, and surfaces the server's verbatim error
   `detail` via a toast on failure (the form and its data are kept intact). On success the tablet
   confirms ("Signed in, <name>") and notes when the host has been notified.
4. **Sign out** — the visitor enters their **name**; the server looks up the open visit. On exactly
   one match it signs them out. If **more than one** open visit shares the name, the server returns a
   **409** with a minimal disambiguation list (`{id, visitor_company, signed_in_at}`) and the tablet
   shows a picker; tapping the right visit re-submits by `visitor_log_id`. No open match returns
   **404** ("No open visitor record found for that name").

### Idle behavior

- After **120 s** of no interaction the form **resets to the welcome screen and discards any
  half-entered data** (privacy) — a countdown banner appears for the final stretch, and any touch
  resets the timer. Idle reset uses the shared `useKioskIdleLogout` hook.
- Idle **keeps the session token** so the next visitor can keep self-serving. The 24 h token TTL is
  the real session backstop.
- A **"Lock station"** button (always visible in the header) drops the token and returns to the PIN
  screen — use it at end of day or when handing the tablet off.

### Offline behavior

Sign-in and sign-out are **hard-disabled while offline** (an `online`/`offline` listener), never
queued — firing a write against a dead connection would silently drop the record. An OFFLINE banner
appears and is the accessible explanation for the disabled submit buttons (referenced via
`aria-describedby`); disabled action buttons read **Offline**. There is **no offline write queue**.

## Host email notification (on sign-in)

On a sign-in, the service does a **best-effort host match**: an *active* user **in the same company**
whose full name case-insensitively equals the typed host name. The match succeeds only on **exactly
one** hit (0 or >1 → no match), and it is **company-scoped only — never cross-tenant** (host names
are CUI).

- If a host is matched **and has an email**, an internal check-in email is **enqueued best-effort**
  to that host (ARQ `send_email_job`, `visitor_check_in.html` template), honoring the host's
  `VISITOR_CHECK_IN` notification preference. This uses the existing internal **SMTP** path.
- Free-text host with no match → **no email** (only `host_name` is stored).
- The notification **never blocks or fails the sign-in** — a notification error is swallowed and
  logged. Visitor and host names are CUI: this is internal SMTP to the company's **own** employee
  only, never to an external boundary.

## Captured fields

| Field | Required | Notes |
| --- | --- | --- |
| Visitor name | **Yes** | CUI PII |
| Company | No | |
| Phone | No | |
| Host name | No | free text; best-effort matched to an internal user for notification |
| Purpose | **Yes** | one of `meeting` · `delivery` · `contractor` · `interview` · `audit` · `other` |
| Purpose note | **When `other`** | server- and client-validated as required when purpose is `other` |
| Safety/NDA acknowledgment | **Yes** | must be `true` to sign in (server-validated) |
| Time in / time out | auto | `signed_in_at` set on sign-in; `signed_out_at` set on sign-out (NULL = still on-site) |
| Station | auto | `station_label` denormalized at sign-in (the audit actor); `signin_station_id` NULL if staff-created |

No drawn signature, no photo, no citizenship/escort fields are captured.

## Admin Visitor Log page (`/visitor-log`)

An authenticated admin page (inside the app `Layout`, listed under the **Admin** sidebar section,
role-gated ADMIN/MANAGER/SUPERVISOR via the `visitor_logs:view` permission). All its calls go through
the **normal `api` client**, not the tablet's `signinClient`.

- **Read / filter / search.** A `<DataTable>` of visits (visitor · company · host · purpose · signed
  in · signed out · station · status) with client sort, a status filter (all / on-site / signed
  out), a date-from/to range, and a debounced free-text search over visitor name / company / host.
  Status renders via `<StatusBadge>` — **signed_in → amber, signed_out → slate**. The header shows
  the live on-site count.
- **CSV export.** Built into the DataTable; the server also exposes `GET /visitor-logs/export.csv`
  (ADMIN/MANAGER), which **audits an `EXPORT` action**.
- **Staff sign-out.** ADMIN/MANAGER can sign out an on-site visitor directly from a row (`POST
  /sign-out` with `{visitor_log_id}`).
- **Soft-delete.** ADMIN/MANAGER can remove a row via a `<Modal>` confirm — the record is
  **soft-deleted** (it stays in the audit trail), never physically erased.
- **Station management** — the create/reset-PIN/revoke/URL surface described under *Station setup*.

## Compliance & security

- **Tenant isolation.** Every query filters `company_id` plus `is_deleted == False` on VisitorLog
  reads. For staff, the company comes from `get_current_company_id`; for the tablet, it comes from
  the **authoritative `signin_stations` row**, never the client's `cid`. The host match is
  company-scoped only.
- **Full audit trail.** Every state change is recorded through `AuditService` (after the
  PK-assigning flush, before the terminal commit, so the row and its tamper-evident audit entry
  commit atomically): sign-in → `log_create`, sign-out → `log_status_change`, delete → `log_delete`,
  CSV → an explicit `EXPORT` action, station create → `log_create`, revoke → `log_status_change`,
  reset-PIN → `log_update`, and a **failed PIN attempt** → a `LOGIN_FAILED` operational event. On the
  station path the audit row is written with `user=None` + the explicit `company_id`, and the
  **station label is recorded as the actor** (the tamper-evident chain is never written directly).
- **Soft-delete only** on VisitorLog (the attendance record survives for audit). Stations use the
  `revoked` flag, never a row delete, so the issuance trail survives.
- **RBAC, server-side and fail-closed.** Viewing the log is SUPERVISOR+ (`require_role`); export,
  delete, and all station administration are ADMIN/MANAGER; the two visitor writes accept a station
  token *or* any authenticated user. The frontend's `PermissionGate` / route gating is UX only.
- **No new unauthenticated write surface** beyond the PIN-gated `station-login` → scoped-token path.
  `verify_token` still fences `type != "access"` everywhere else.

> **Security note — provision 6–8 digit PINs in the interim.** The per-path auth rate limiter for
> `POST /visitor-logs/station-login` is **declared but not yet wired** (the limit is registered in
> `main.py`'s `AUTH_RATE_LIMITS` as `5/minute`, but the per-path middleware currently only *logs*
> sensitive auth paths — it does not enforce them; only the app-wide default limit applies). This is
> a known gap with a **tracked, separate fix**. Until it lands, **provision 6–8 digit station PINs**
> (the schema allows 4–8) so the unlock has more entropy against online guessing. Mitigations already
> in place: PINs are **bcrypt-hashed** at rest and never echoed, every **failed attempt is audited**
> (`LOGIN_FAILED`), and **revocation is instant** (DB-authoritative, re-checked every request). Treat
> a station PIN like a shared password — rotate it (Reset PIN) on staff turnover and revoke a lost or
> decommissioned tablet immediately.

## Reference

- Endpoints and request/response: `docs/API.md` → Visitor Logs.
- Role gating: `docs/RBAC_PERMISSIONS.md` → Visitor Logs and → Admin.
- Environment: no new variables — the signin token reuses the existing JWT signing keys
  (`SECRET_KEY` / `ALGORITHM`) and host email reuses the existing `SMTP_*` config
  (`docs/ENVIRONMENT_VARIABLES.md`).
