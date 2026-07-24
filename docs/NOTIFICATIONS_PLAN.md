# Notifications & Messaging System — Implementation Plan

**Status:** READY TO IMPLEMENT (scope decisions approved 2026-07-24; adversarially reviewed against the codebase; PR #149 and #150 merged — branch off current `origin/main`)
**Owner decisions locked in:** Twilio SMS from day one (behind a CMMC kill switch) · in-app push now, browser web-push deferred · full comments + @mentions · per-user toggles with a small mandatory-critical set.

---

## 1. Goal

A single, robust notification pipeline so that every significant event in the platform — holds, blocks, completions, NCRs, failed inspections, receipts, late work orders, comments/mentions, and the rest — reaches the right people over the channels they choose: **in-app (bell + live toast), email, and SMS**, each toggleable per event type in a new My Settings page. Plus a first-class **comments + @mentions** layer so the platform carries the conversation, not just the data.

## 2. Current state (from 2026-07-24 codebase recon)

What already exists and is reused:

- `NotificationService` + `NotificationPreference` / `NotificationLog` / `DigestQueue` (all `TenantMixin`, migration 026). Email-only; per-event `{email, digest}` JSON prefs; daily-digest cron at 8:00. **Orphaned**: no preferences API or UI; `WO_RELEASED`, `WO_BLOCKED`, `PO_RECEIVED`, `INSPECTION_FAILED`, `NCR_CREATED` are declared but never fired; only `CAPACITY_OVERLOAD` (MRP) and the blocker service's in-app `NotificationLog` rows are live.
- Email: `EmailService` (aiosmtplib SMTP + Jinja2, `app/templates/email/`), sent via ARQ `send_email_job`.
- WebSocket: `ConnectionManager` (`core/websocket.py`) already indexes sockets per user and implements `send_to_user()` — **never called anywhere**. Frontend `useWebSocket` hook with reconnect/heartbeat/token-refresh exists; `Layout.tsx` already holds an open `/ws/updates` socket (presence only, no `onMessage`).
- Event seams: `OperationalEventService.emit/emit_best_effort` (~40 event types, append-only, tenant-validated, payload-redacting) and `AuditService.log_status_change` (43 call sites). No pub/sub bus. **`emit` flushes but does not commit** — the dispatch design in §3.1 is built around that fact.
- ARQ worker with 11 cron jobs (digest, WO-late, calibration, low-stock, quote-expiring, tracking poll…).

What does not exist: any SMS capability (no provider, no `User.phone` column — the `phone` in `api/endpoints/users.py` local schemas is a phantom that never persists), any user-facing settings page (only `/admin/settings`), a notification bell/unread state, web push/service worker, a Note/Comment model (notes are bare text columns on ~40 tables), a `FRONTEND_BASE_URL` setting (emails contain no links today).

Pre-existing defects this plan fixes en route: §9.

## 3. Architecture

### 3.1 One dispatcher, many channels — with a transactional outbox

`NotificationService` is rebuilt around a central **event catalog** and a single dispatch path:

```
domain code ──► OperationalEventService.emit(...)   (catalog-mapped event types marked on the Session)
                        │
                 [transaction commits]  ──► after_commit hook enqueues dispatch_notification_job(event_id)
                 [transaction rolls back] ─► marks discarded — NO notification (no ghosts)
                        │
                 ARQ worker: dispatch_notification_job
                   1. load the committed OperationalEvent row; set notified_at (idempotency marker)
                   2. resolve recipients (roles ∪ watchers ∪ direct − actor), ALL filtered by the event's company_id
                   3. resolve per-user prefs (catalog defaults; mandatory forces the catalog-named channel)
                   4. create `notifications` rows (in-app) · publish WS push · enqueue email/SMS jobs · queue digest
                   5. write NotificationLog per email/SMS delivery attempt (company_id stamped from the event)
                        │
                 relay sweeper cron (5 min): re-enqueues catalog-mapped events with notified_at IS NULL
                 older than ~2 min (covers a Redis outage at enqueue time)
```

- **Event catalog** (`services/notification_catalog.py`): one registry keyed by `event_key` → label, description, category, severity (`info|warning|critical`), default channels (`in_app/email/sms/digest`), `mandatory` flag **plus the channel it forces** (quality/mention events force in-app; `account.locked` forces email — the affected user can't see in-app), and a recipient resolver. It drives defaults, the settings-UI matrix (served via API — the frontend never hardcodes event lists), and validation. Adding an event = one catalog entry + one trigger. **Emitted-but-uncataloged event types are deliberately ignored by the tee** — future omissions are visible decisions, not silent drops.
- **Why the outbox shape**: `emit` runs before the caller's commit, and rollbacks are a *designed* path here (StaleDataError→409 on the contended WO/op/TimeEntry writes). Enqueuing at emit time would send phantom notifications for transitions that never happened, and the worker could race the commit. Post-commit enqueue by **committed event id** solves both; the `notified_at` marker + sweeper make delivery at-least-once with idempotent re-dispatch.
- **Enqueue context matrix** (the existing `enqueue_job_best_effort` calls `asyncio.run()` and breaks inside a running loop — do not use it from the tee): async request context → `await enqueue_job(...)` on the module Redis pool; sync threadpool handlers → the thread-safe best-effort helper; inside ARQ jobs → the job `ctx` redis. The after_commit hook routes to the right one; the sweeper is the safety net for all three.
- **Trigger coverage**: the tee instantly covers the ~40 already-emitted events. Domains that emit nothing today (ECO, quotes, complaints/RMA, maintenance, account lockout, cert expiry, low-stock) get proper `emit` instrumentation added — which the AI/analytics layers want anyway — so *everything* flows through the one outbox. No parallel mechanism.
- **Actor exclusion**: you are never notified of your own action.
- **Dedup**: a `(event_key, related_type, related_id, user)` Redis window (~5 min) guards against retry re-enqueue and multiple emits within one flow (it is *not* relied on to mask architectural duplicates — those are removed at the source, §10 PR 1).
- **Re-notify policy for recurring detectors** (wo.late, stock.low, calibration.due, quote.expiring, maintenance.overdue, cert.expiring): a new notification is **suppressed while an unread one for the same `(event_key, entity, user)` exists**; the daily digest carries the standing list. A WO late for two weeks = one inbox row + the digest, not 14 emails.

### 3.2 In-app channel (Phase 1)

- New **`notifications` table** — the canonical per-user inbox row (one per user per event): `company_id`, `user_id`, `event_key`, `severity`, `title`, `body`, `link` (relative route), `related_type/related_id`, `is_read`, `read_at`, `created_at`. Indexed on `(user_id, is_read)`. `NotificationLog` remains the per-channel delivery-attempt log (email/SMS rows link back via a new nullable `notification_id`).
- **Bell + popover** in the `Layout` top bar: unread badge, recent-20 popover, mark-read on click, mark-all-read, "View all" → new `/notifications` page (`DataTable`, server pagination, filters by category/severity/unread).
- **Live delivery**: `manager.send_to_user()` finally gets its caller. Dispatch runs in the worker while `ConnectionManager` lives in the API process, so add a **Redis pub/sub bridge**: the worker publishes `{user_id, company_id, payload}` on `werco:notify`; an API-side subscriber task (started in the existing `lifespan`, reconnect-with-backoff, cancelled at shutdown) forwards to the local manager. **Both sides use `get_redis_settings()`** (the ARQ source of truth — not `REDIS_URL`, which configures the cache) so publisher and subscriber can't silently target different Redis instances. Every API replica subscribing is the intended multi-instance fan-out fix. Delivery is **company-aware**: the subscriber only sends to sockets whose registered `connection_company` matches the payload's `company_id` (defense-in-depth for platform-admin context switching — same user_id, different active company).
- **Kiosk fence (required in PR 2)**: `/ws/updates` authenticates with bare `verify_token` today and would accept a badge-minted `scope="kiosk"` operator token — which would put personal notifications on a shared shop-floor screen. `_identity_from_token` rejects kiosk-scoped tokens at WS connect (mirroring the HTTP path fence), with a test asserting a badge token receives no notification frames.
- `Layout`'s existing `/ws/updates` socket gets an `onMessage` handler: bump badge, prepend popover cache, `showToast` for `critical`.
- **Fallback**: the bell polls `unread-count` every 60s; WS is an enhancement, not a dependency.
- ActionInbox drops its `NotificationLog` merge (its sole consumer — verified); it keeps setup-health + AI recommendations. `GET /notifications/logs` is retained as the admin delivery-failure view's data source (admin-scoped in PR 3).

### 3.3 Email channel (fixed, not new)

- Keep `send_email_job`, but **fix the retry layering**: `EmailService.send_email` currently swallows all exceptions and returns `False`, defeating ARQ's retry. It will raise on transport failure (the task already re-raises), with `NotificationLog` recording the terminal outcome.
- **Create the 8 missing templates** (calibration_due, low_stock, quote_expiring, wo_completed, scheduling_conflicts, mrp_complete, mrp_review_needed, expedite_required) — these emails silently drop today. Template content follows the email content rule decided in §11.1.
- Move SMTP config from module-level `os.getenv` to `Settings` (the `Settings.SMTP_*` fields exist but are dead).
- Add **`FRONTEND_BASE_URL`** setting; every email gets a deep-link button and a footer: "Manage notifications" (link to My Settings) + **one-click unsubscribe** for that event's email channel. Unsubscribe token spec (pinned): python-jose JWT with a **dedicated `type="unsub"` claim** (rejected by every other verifier, same discipline as display tokens); payload exactly `{user_id, event_key, channel:"email"}`; **expiry 60 days** (re-minted on every email, so short is fine); effect strictly limited to turning that one channel **off** (never on, never another channel); `POST /notifications/unsubscribe` is token-authenticated, rate-limited via `ENDPOINT_RATE_LIMITS`, and audited with `user=None`, the token's user as resource, and a description noting the token-authenticated action.
- Ops note for the runbook: SPF/DKIM/DMARC checklist for the From domain — 8 previously-dropped templates plus links start going out, so deliverability becomes user-visible for the first time.

### 3.4 SMS channel (Twilio, fail-closed)

- **`Company.allow_sms_egress`** — `Boolean, nullable=False, default=False, server_default="false"`, exactly the `allow_ai_egress` pattern: fail-closed gate in the new `sms_service` before any Twilio call; ADMIN-only double-audited `PUT /companies/me/sms-egress`; AIEgressTab-style toggle UI (confirm-on-enable) in a new AdminSettings tab.
- **`User.phone`** — real column (String(32)), stored E.164, validated/normalized via the `phonenumbers` lib; editable in My Settings (self-service, audited) and the admin Employees tab; the phantom `phone` in `users.py` local schemas becomes real. **Field minimization**: phone serializes only in the self-profile response and admin user-management responses — never in general user lists or mention-search. SMS toggles are inert until a phone is present; "Send test SMS" button.
- **`sms_service` + `send_sms_job`**: Twilio SDK, config in `Settings` (`TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER` or messaging service SID), `NotificationLog` rows with channel=`sms` + Twilio SID + status.
- **CUI-safe bodies**: SMS content is deliberately terse and generic — record type + number + event + "open Werco to view" (e.g. `Werco: WO-1042 placed on HOLD (material shortage). Log in to view.`). No customer names, no part descriptions, no quantities. Standing content rule, documented in the runbook.
- **Storm control**: per-user cap (default 5 SMS/hour); overflow collapses into one "…and N more — check the app" message. SMS is only *eligible* for `critical` catalog events, and per-user SMS toggles default **off** (opt-in) even where eligible.
- Deferred to a later phase: Twilio inbound STOP/opt-out webhook + delivery-receipt webhook (Twilio's own STOP handling covers US compliance meanwhile).

### 3.5 Digest

- Keep `DigestQueue`; digest becomes a per-event channel choice alongside in_app/email/sms.
- Fix scheduling: the cron runs every 30 minutes and sends to users whose `digest_time` falls in the window; `WEEKLY` (modeled today, never scheduled) fires on Mondays; `NONE` disables.
- **Timezone**: `digest_time` is interpreted in **America/Chicago** (the shop-local zone, per the store-UTC/display-Central convention), converted per-run with `zoneinfo` so DST is handled. A per-user timezone column is deliberately out of scope.

### 3.6 Comments + @mentions + watchers

- **`comments`** (SoftDeleteMixin + TenantMixin): `entity_type`, `entity_id`, `author_id`, `body`, `edited_at`; **`comment_mentions`** (comment_id, user_id). Entity types v1: work_order, purchase_order, ncr, part, quote, receipt, shipment, eco, vendor, customer.
- **`entity_watchers`** (TenantMixin): `entity_type`, `entity_id`, `user_id`. **Auto-watch rules**: you watch a record you created, commented on, or were mentioned in; manual Watch/Unwatch on detail pages.
- **Tenant validation of polymorphic refs (invariant)**: comment/watcher creation resolves the target `(entity_type, entity_id)` via `tenant_query()` against the active company and 404s otherwise (matching the existing IDOR posture). Entity ids are global integers — a bare id is never trusted.
- **RBAC — who may comment/watch where**: a per-entity-type permission map in the comments/watchers service mirrors each domain's *read* RBAC (e.g. quote/PO comment threads require the roles that can read quotes/POs; an operator can comment on WOs but not quotes). The same gate applies to watch/unwatch, to mention-search *in context*, and to **mention delivery** — mentioning a user who lacks access to the entity does not leak its title/body (the mention is refused at compose time with a clear message). Moderation: authors edit/delete their own comments; ADMIN/MANAGER may delete any (all audited). Documented in RBAC_PERMISSIONS.md.
- **`<CommentsPanel entityType entityId>`** shared component (instrument-panel styling, FormField/Button primitives): thread list + composer with @mention autocomplete (`GET /users/mention-search` — returns **only `{id, display_name}`** for active company users; no emails/roles/phones), edit/delete own, timestamps in Central. Mounted on WO detail, PO/Purchasing detail, NCR/Quality detail, Part detail, Quote detail, Receiving detail first.
- Notifications: `comment.mention` → mentioned user (mandatory in-app); `comment.added` → watchers minus mentioned/author.
- **Lifecycle**: recipient resolution filters `User.is_active` across all three sources (roles, watchers, direct); watcher rows survive soft-delete (restore keeps watchers) but resolution skips deleted entities; notification deep links to since-deleted records render a "record deleted" affordance rather than a dead link; deactivated users' unread rows are excluded from unread counts and pruned by retention.
- Comment create/edit/delete audited via `AuditService`.

### 3.7 Preferences & governance

- `NotificationPreference.preferences` JSON widens from `{email, digest}` to `{in_app, email, sms, digest}` per event. **No more row auto-creation**: an absent row means catalog defaults, resolved in memory at dispatch time; a row is persisted only when the user explicitly saves (this also removes today's mid-request commit inside pref resolution and the `IntegrityError` auto-create defect, §9.8). PR 1's migration normalizes existing rows to the new shape (one-time, idempotent).
- **My Settings page** (`/settings`, all roles — the first user-facing settings surface): notification matrix (rows = catalog events grouped by category; columns = In-app / Email / SMS / Digest toggles), digest frequency/time, phone number, test-send buttons. Saves via `PUT /users/me/notification-preferences` (audited `log_update`).
- **Mandatory criticals**: catalog-flagged events force their catalog-named channel on (locked toggle with tooltip); other channels remain user choice. V1 mandatory set: `ncr.created` (Quality role, in-app), `quality.hold` (in-app), `inspection.failed` (Quality, in-app), `wo.blocker_created` (supervisors/managers, in-app), `comment.mention` (mentioned user, in-app), `account.locked` (affected user, **email** — they can't see in-app). Code-defined in the catalog, not admin-editable, v1.
- **Admin defaults**: new AdminSettings "Notifications" tab — company-wide default matrix applied to users who haven't customized, the SMS egress kill switch, and a recent-delivery-failures view (from `NotificationLog`, admin-scoped).

## 4. Event catalog (v1)

Severity: ℹ info · ⚠ warning · 🔴 critical. Channels shown are **defaults** (user-changeable unless Mandatory). SMS eligibility only where marked; per-user SMS is opt-in everywhere. **(instrument)** = the trigger emits nothing today and gets an `emit` added.

### Production
| Event | Trigger | Default recipients | Sev | Defaults | SMS-elig | Mand. |
|---|---|---|---|---|---|---|
| wo.blocker_created (hold/block) | `work_order_blocker_service.create_blocker`, `put_operation_on_hold` | supervisors, managers, Purchasing/Inventory (by blocker type), watchers | 🔴 | in-app+email | ✔ | ✔ |
| wo.blocker_escalated | `escalate_blocker` (already emits) | managers + original recipients | 🔴 | in-app+email | ✔ | |
| wo.blocker_resolved | `update_blocker` resolve | same as created | ℹ | in-app | | |
| wo.released | `release_work_order` | supervisors, watchers | ℹ | in-app | | |
| wo.started | `start_work_order` | watchers | ℹ | in-app | | |
| wo.completed | completion signal (exists) | supervisors, managers, creator, watchers | ℹ | in-app+email | | |
| wo.closed | `mark_shipped` | managers, watchers | ℹ | in-app | | |
| wo.late | cron (exists; re-notify policy §3.1) | supervisors, managers | ⚠ | in-app+email | | |
| wo.deleted | WO soft-delete route | watchers, supervisors | ⚠ | in-app | | |
| wo.priority_changed | priority route | watchers | ℹ | off (available) | | |
| op.completed | operation complete | watchers | ℹ | off (available) | | |
| op.ready | `emit_operation_ready_event` | WC supervisors | ℹ | off (available) | | |
| scrap.recorded | `report_operation_production` scrapped>0 | Quality, supervisors | ⚠ | in-app | | |
| production.reduced | reduction service | supervisors | ⚠ | in-app | | |

### Quality
| Event | Trigger | Default recipients | Sev | Defaults | SMS-elig | Mand. |
|---|---|---|---|---|---|---|
| ncr.created | quality/receiving/shop-floor/process-sheet NCR paths | Quality dept, managers, WO watchers | 🔴 | in-app+email | ✔ | ✔ (Quality) |
| ncr.closed | `update_ncr` → CLOSED | Quality, watchers | ℹ | in-app | | |
| ncr.voided | `void_ncr` | Quality managers | ℹ | in-app | | |
| quality.hold | `raise_step_quality_hold` | Quality, supervisors | 🔴 | in-app+email | ✔ | ✔ |
| inspection.failed | `inspect_receipt` rejected>0 | Quality, Purchasing, managers | 🔴 | in-app+email | ✔ | ✔ (Quality) |
| car.created | CAR endpoints | Quality | ⚠ | in-app+email | | |
| fai.created / fai.completed | FAI endpoints | Quality | ℹ | in-app | | |
| calibration.due | cron (exists; re-notify §3.1) | Quality | ⚠ | in-app+digest | | |
| cert.expiring / cert.expired | new cron over `compute_cert_status` (**instrument**) | operator, supervisors, Quality | ⚠ | in-app+digest | | |
| complaint.received / complaint.status_changed | complaints endpoints (**instrument**) | Quality, sales managers | ⚠ | in-app+email | | |
| rma.approved / rma.received | RMA endpoints (**instrument**) | Quality | ⚠ | in-app | | |

### Purchasing & Inventory
| Event | Trigger | Default recipients | Sev | Defaults | SMS-elig | Mand. |
|---|---|---|---|---|---|---|
| po.sent | `send_purchase_order` | watchers | ℹ | in-app | | |
| po.deleted | PO soft-delete route | PO creator, watchers, Purchasing | ⚠ | in-app | | |
| receipt.created (po.received) | `receive_material` | PO creator, watchers | ℹ | in-app+email | | |
| receipt.voided / receipt.corrected | void/correct routes | Purchasing managers, Quality | ⚠ | in-app | | |
| vendor.deactivated | vendor delete/deactivate | Purchasing, Quality (ASL) | ⚠ | in-app | | |
| stock.low | cron (exists; re-notify §3.1) | Purchasing, Inventory | ⚠ | digest | | |
| mrp.expedite_required | mrp_auto (exists) | Purchasing | ⚠ | in-app+email | | |
| mrp.completed / mrp.review_needed | MRP jobs (exists) | planners/managers | ℹ | email | | |
| capacity.overload | scheduling jobs (exists) | managers | ⚠ | in-app+email | | |

### Sales, Shipping, Engineering, Maintenance, System
| Event | Trigger | Default recipients | Sev | Defaults | SMS-elig | Mand. |
|---|---|---|---|---|---|---|
| quote.sent / quote.accepted | quotes endpoints (**instrument**) | Sales, managers | ℹ | in-app | | |
| quote.expiring | cron (exists; re-notify §3.1) | Sales | ⚠ | digest | | |
| shipment.shipped | `mark_shipped` | Sales, watchers | ℹ | in-app+email | | |
| shipment.delivery_exception | `record_tracking_events` on transition into FAILURE/RETURNED (**instrument** — covers both webhook and poll paths) | Shipping, Sales | ⚠ | in-app+email | | |
| coc.generation_failed | exists | Shipping, Quality | ⚠ | in-app | | |
| eco.submitted | `submit_eco` (**instrument**) | approver roles | ⚠ | in-app+email | | |
| eco.approved / eco.rejected / eco.implemented | ECO routes (**instrument**) | submitter, watchers | ℹ | in-app+email | | |
| maintenance.due / maintenance.overdue | cron (**new**; re-notify §3.1) | Maintenance, supervisors | ⚠ | in-app | | |
| downtime.started | `create_downtime_event` | supervisors, managers | 🔴 | in-app | ✔ | |
| downtime.resolved | `resolve_downtime_event` | same | ℹ | in-app | | |
| comment.mention | comment create | mentioned user | ℹ | in-app+email | | ✔ |
| comment.added | comment create | watchers | ℹ | in-app | | |
| account.locked | login throttle (**instrument**) | affected user, admins | 🔴 | email (user) + in-app (admins) | | ✔ (email) |
| import.completed / import.failed | migration import | initiator | ℹ | in-app | | |
| visitor.check_in | exists | host | ℹ | in-app+email | | |

(AI recommendations stay in ActionInbox — out of scope here. WO/PO/vendor **restores** are deliberately not cataloged — the restore is visible in the record's history; revisit if asked.)

**Considered and deferred** (explicit no-for-now, cheap to promote later): process-sheet release/obsolete/new-revision → part/WO watchers (strongest promote candidate); kiosk/sign-in **station** PIN-lockout security events → admins (second candidate); time-entry approve/unapprove → operator; vendor ASL approval-status change; document uploaded to a watched record.

## 5. Data model changes

One Alembic migration **per DDL-bearing PR**. Current head on `main` is `071_soft_delete_purchasing_ncr` (note: two files share the `071_` filename prefix — `071_display_token_show_customer` chains into it; linear, single head). PR 1's migration takes prefix **072** with `down_revision = "071_soft_delete_purchasing_ncr"`. All new tables: non-null `company_id` + index + `ENABLE ROW LEVEL SECURITY`, idempotent, real downgrade.

**PR 1 migration:**
- `notifications` (§3.2)
- `notification_logs` + nullable `notification_id` FK, index
- `operational_events` + nullable `notified_at` (outbox idempotency marker, §3.1)
- `users` + `phone` (String(32), nullable)
- `companies` + `allow_sms_egress` (Boolean, non-null, server_default `false`)
- `notification_preferences`: one-time JSON normalization backfill to the `{in_app,email,sms,digest}` shape (no DDL)

**PR 5 migration:** `comments`, `comment_mentions`, `entity_watchers` (§3.6)

Retention: extend the existing `cleanup_old_logs_job` to prune read `notifications` > 90 days, unread rows of deactivated users, and processed `DigestQueue` rows (configurable).

## 6. API surface (new/changed)

| Endpoint | Notes |
|---|---|
| `GET /notifications` | paged inbox, filters (unread/category/severity); tenant + self-scoped |
| `GET /notifications/unread-count` | bell badge (cheap; also pushed over WS) |
| `POST /notifications/{id}/read` · `POST /notifications/read-all` | not audited (deliberate — UI state, not domain state) |
| `GET /notifications/catalog` | event catalog for the settings matrix |
| `GET/PUT /users/me/notification-preferences` | audited `log_update` |
| `PUT /users/me/profile` | self-service phone (audited); first self-profile route |
| `PUT /companies/me/sms-egress` | ADMIN-only, double-audited (log_update + log_status_change) |
| `POST /notifications/unsubscribe` | `type="unsub"` signed-token auth (§3.3), rate-limited, audited |
| `GET /notifications/logs` | retained; admin-scoped for the delivery-failure view |
| `GET/POST /comments` · `PATCH/DELETE /comments/{id}` | entity-scoped; tenant-validated target; per-entity-type RBAC (§3.6); soft delete; audited |
| `GET /users/mention-search?q=` | company-scoped active users; returns `{id, display_name}` only |
| `POST/DELETE /watchers` | watch/unwatch; same tenant + RBAC gates as comments |

## 7. Frontend work

- **Bell + popover + toasts** in `Layout` (WS `onMessage` on the existing `/ws/updates` socket; 60s poll fallback) — `critical` → toast via `useToast`.
- **`/notifications` page** — DataTable, server pagination, filters; deep links via `routeMeta`; "record deleted" affordance for dead links.
- **`/settings` My Settings page** — preference matrix from the catalog API, digest controls, phone, test sends. New nav item (all roles).
- **AdminSettings "Notifications" tab** — company defaults, `allow_sms_egress` toggle (AIEgressTab pattern), delivery-failure view.
- **`<CommentsPanel>`** on 6 detail pages with @mention autocomplete.
- Conventions: FormField/Button/Modal/StatusBadge/EmptyState primitives, statusColors, Central-time rendering, jsx-a11y green, non-optimistic for server-gated saves (preference saves may be optimistic — rarely rejected).

## 8. Compliance requirements (treat as invariants)

1. Every new table tenant-scoped (`TenantMixin` shape) + RLS in the migration; all request-path queries via `tenant_query()`/`get_current_company_id`.
2. **Dispatcher tenancy**: the dispatcher runs in the worker with no request-scoped protection — *every* recipient-resolution source (roles, watchers, mentions) filters by the triggering event's `company_id`, and *every* row it writes (`notifications`, `NotificationLog`, `DigestQueue`, any pref row) stamps `company_id` from the event, never derived-from-nothing. Tenant-isolation test: a foreign-company watcher row on the same `entity_id` receives nothing.
3. `get_notification_recipients` — `company_id` becomes **required** (today it defaults to `None` = all tenants).
4. **Polymorphic refs**: comment/watcher targets resolve via `tenant_query()` (404 on foreign/absent entities); per-entity-type RBAC map mirrors domain read access (§3.6).
5. `allow_sms_egress` default-off, fail-closed at the service layer, ADMIN-only double-audited toggle — identical to AI/carrier/print egress.
6. Channel content rules: SMS strictly terse/CUI-safe (§3.4); email per the §11.1 decision.
7. Audited: preference changes, phone changes, egress toggle, comment create/edit/delete, unsubscribe (actor `user=None`, token's user as resource). **Not** audited: mark-read.
8. Comments are soft-delete; no hard deletes.
9. Mandatory-critical events cannot be fully muted (catalog-named channel forced) — AS9100 awareness of quality holds/NCRs.
10. Kiosk fencing covers **both** surfaces: HTTP (existing deps fence) and the WS connect (`scope="kiosk"` rejected at `_identity_from_token`, §3.2).
11. WS pushes are company-aware (subscriber matches `connection_company`, §3.2).
12. PII minimization: mention-search returns id+name only; phone only in self-profile and admin responses.

## 9. Pre-existing defects fixed en route

1. 8 referenced email templates missing → emails silently dropped (§3.3).
2. `EmailService.send_email` swallows exceptions → ARQ never retries (§3.3).
3. `Settings.SMTP_*` dead config (email path reads `os.getenv`).
4. Phantom `phone` field in `users.py` local schemas (never persisted).
5. `WEEKLY` digest modeled but never scheduled (§3.5).
6. Visitor check-in bypasses `NotificationService` (no `NotificationLog`) — reroute through the dispatcher.
7. `get_notification_recipients(company_id=None)` cross-tenant default (§8.3).
8. **`_get_user_preference` auto-creates `NotificationPreference` without `company_id`** — a non-null `TenantMixin` column, so on Postgres the commit raises `IntegrityError` for any user without a pref row, silently failing today's only-live email paths (swallowed by the per-tenant try/except). Fixed by the no-auto-create design (§3.7). Same missing-`company_id` construction exists for `NotificationLog`/`DigestQueue` writes — the rebuilt dispatcher stamps all three (§8.2).

## 10. Phased PR plan

| PR | Scope | Key risk |
|---|---|---|
| **1. Foundation + in-app inbox** | PR 1 migration (§5); event catalog + dispatcher rebuild on the **post-commit outbox** (§3.1) incl. sweeper cron; wire all already-emitted events; **remove superseded legacy writes** — delete `work_order_blocker_service._create_notification_logs` and repoint the four `notification_jobs.py` cron tasks + MRP `CAPACITY_OVERLOAD` at the new dispatcher (no double-fire); re-notify policy; email fixes (templates, retry, Settings, FRONTEND_BASE_URL + deep links); bell/popover/`/notifications` page with 60s polling; ActionInbox de-dup | Biggest PR; outbox/transaction mechanics + migration |
| **2. Live push** | Redis pub/sub bridge (`get_redis_settings()` both sides, lifespan subscriber); company-aware `send_to_user`; **kiosk WS fence**; Layout `onMessage` → badge/toast | Worker→API process boundary; kiosk regression |
| **3. Preferences & settings** | Prefs/catalog APIs; My Settings page (+ self-profile route); admin defaults tab + admin-scoped delivery-failure view; digest fixes (30-min cron, WEEKLY, Central-time `digest_time`); unsubscribe endpoint | Prefs API/UI; digest timezone |
| **4. SMS** | `User.phone` UI + schema realization; Twilio service + job + kill switch + admin toggle; storm caps; test send | Egress compliance; Twilio account setup (user-side) |
| **5. Comments & mentions** | PR 5 migration; comments/mention/watcher APIs with tenant-validated targets + per-entity RBAC map; `<CommentsPanel>` on 6 pages; mention/comment events; auto-watch | Fan-out noise tuning; RBAC map completeness |
| **6. Event coverage completion** | Instrument silent domains (quotes, complaints/RMA, ECO, maintenance, delivery exceptions at `record_tracking_events`, account lockout, cert-expiry cron); full catalog wired; low-stock stays cron | Broad but shallow touch surface |

**Explicitly deferred** (designed-for, not built): browser web push (service worker + VAPID + pywebpush), Twilio STOP/delivery-receipt webhooks, quiet hours, Slack/Teams channel adapter, event-driven low-stock, admin-editable mandatory sets, per-user timezone, the §4 considered-and-deferred events.

Each PR runs the standing gates: **test-engineer** (pytest + Jest; outbox rollback/ghost tests, WS bridge, dispatcher tenant-isolation, kiosk-fence), **documentation-engineer** (API.md, ENVIRONMENT_VARIABLES.md, RBAC_PERMISSIONS.md, new `docs/NOTIFICATIONS.md` runbook incl. SMS/email content rules + SPF/DKIM checklist, CLAUDE.md), **compliance-auditor** (every PR touches data access/auth). Base: current `origin/main` (#149 and #150 merged 2026-07-24); suggested branch name `feat/notifications-foundation` for PR 1.

## 11. Open decisions (defaults chosen; flag if you disagree)

1. **Email content vs CUI boundary** (raised in review): SMS is terse because Twilio is outside the CUI boundary — but email over external SMTP is the same class of crossing, and rich templates would carry customer names/part detail. **Default chosen: graduated rule — email bodies carry record identifiers, event, actor, and deep link, but no CUI field detail** (no part descriptions, customer names, quantities); the detail lives behind the login. If your SMTP relay is within your assessed boundary (e.g. GCC-High / on-prem), say so and we relax this to rich templates and record the boundary decision in the runbook.
2. **Mandatory set** (§3.7): the 6 listed events — trim/extend?
3. **Operator reach**: operators get in-app only by default (kiosk-badge users rarely see the full app); their rows exist in the matrix if they log into the SPA.
4. **Retention**: 90-day prune of read notifications — adjust?
5. **Promote from considered-and-deferred?** Process-sheet release → watchers, and station PIN-lockout → admins, are the two strongest candidates.
