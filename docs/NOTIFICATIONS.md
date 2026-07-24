# Notifications

Operational runbook for the Werco notification pipeline. This documents **PR 1 (Foundation +
in-app inbox)** — the transactional outbox, the event catalog, the in-app and email channels, and
the compliance invariants. Later PRs extend this file (see [Deferred / roadmap](#deferred--roadmap)
at the end). The authoritative design spec is [NOTIFICATIONS_PLAN.md](NOTIFICATIONS_PLAN.md); this
runbook describes what is actually implemented.

Convention: store UTC, serve UTC (`Z`), display Central — inbox timestamps follow the same rule as
the rest of the app.

---

## What PR 1 delivers

- One dispatcher, driven by a **transactional outbox** tee off `OperationalEventService.emit`.
- A single **event catalog** (`services/notification_catalog.py`) — the source of truth for every
  notifiable event, its category/severity, default + mandatory channels, and recipients.
- The **in-app inbox** (`notifications` table) behind the bell / popover / `/notifications` page,
  plus the API (see [API.md → Notifications](API.md#notifications-in-app-inbox)).
- The **email channel** fixed and live (retry layering, `Settings`-driven SMTP, 8 previously-missing
  templates, absolute deep links).
- The **SMS** kill switch column (`Company.allow_sms_egress`, default OFF) — **no SMS is sent** in
  PR 1 (Twilio arrives in PR 4).

---

## Architecture — the transactional outbox

Notifications are dispatched **after** the triggering transaction commits, keyed by the committed
`OperationalEvent` id. This is a deliberate outbox shape, not an incidental one:

```
domain code ──► OperationalEventService.emit(...)         (flush; id assigned)
                    │  if event_type is catalog-mapped, append event.id to
                    │  Session.info["pending_notification_event_ids"]
                    ▼
              [transaction commits]  ── after_commit ──►  enqueue dispatch_notification_job(event_id)
              [transaction rolls back] ─ after_rollback ─► drop the pending list  (NO ghost)
                    │
              ARQ worker: dispatch_notification_job(event_id)
                 1. load the committed OperationalEvent; return if notified_at is already set
                 2. resolve catalog entry from event.event_type; apply the transition gate
                 3. fan out (recipients ∩ prefs → in-app rows / email jobs / digest), tenant-scoped
                 4. set notified_at = utcnow() and commit rows + marker in ONE transaction
                    │
              relay sweeper cron (every 5 min): re-enqueue catalog-mapped events with
                 notified_at IS NULL older than 2 min (covers a Redis outage at enqueue time)
```

### Why post-commit, by committed event id

`emit` runs **before** the caller's commit (it only flushes), and rollbacks are a *designed* path
here — a stale write on the contended `WorkOrder` / `WorkOrderOperation` / `TimeEntry` paths raises
`StaleDataError`, translated to HTTP 409. Enqueuing at emit time would fire **ghost** notifications
for transitions that never committed, and the worker could race the commit and load a not-yet-visible
row. Post-commit enqueue by durable event id solves both; the `notified_at` marker + the sweeper make
delivery **at-least-once with idempotent re-dispatch**.

### The pieces

| Component | File | Role |
|---|---|---|
| Outbox marker | `services/operational_event_service.py` | `emit` appends the flushed event id to `Session.info["pending_notification_event_ids"]` iff `event_type` is in the catalog reverse index. Wrapped so the marker can never fail an emit. |
| Session listeners | `services/notification_outbox.py` | Module-level `after_commit` / `after_rollback` / `after_soft_rollback` listeners on the SQLAlchemy `Session`. `after_commit` routes the enqueue; the rollback listeners drop the pending list (ghost prevention). Imported at both API startup (`app.main`) and worker startup (`app.worker`) so the tee is active in every process that commits events. |
| Dispatch job | `jobs/notification_jobs.py::dispatch_notification_task` + `worker.py::dispatch_notification_job` | Loads the event, no-ops if missing or already dispatched (`notified_at` set), else fans out and commits rows + `notified_at` in one transaction. A crash before commit leaves `notified_at IS NULL` for the sweeper to re-pick. |
| Relay sweeper | `jobs/notification_jobs.py::relay_pending_notifications_task` + `relay_pending_notifications_job` cron | Every 5 min: bounded scan (LIMIT 500) of cataloged event types with `notified_at IS NULL` and `created_at < now − 2 min`; re-enqueues the dispatch job. |
| Fan-out core | `services/notification_dispatch.py` | `_fan_out` (shared), `dispatch_for_event` (outbox path), `dispatch_direct` (cron/MRP path). |

### Enqueue routing (the reason the tee exists)

`enqueue_job_best_effort` calls `asyncio.run()` and **RuntimeErrors inside a running loop**, so the
`after_commit` listener routes by context:

- **Async request handler** (a running loop exists) → `loop.create_task(enqueue_job(...))`, with a
  module-level task set holding a reference so the loop doesn't GC it mid-flight.
- **Sync `def` handler on a threadpool** (no running loop) → `enqueue_job_best_effort(...)`.

An enqueue failure is caught and logged — it must **never** fail the just-committed request; the
5-min sweeper is the backstop.

### Two fan-out entry points

- **`dispatch_for_event(db, event)`** — the outbox path. Derives title/body/link/recipients from the
  committed event + catalog. Does **not** commit (the job owns the atomic commit).
- **`dispatch_direct(db, *, event_key, company_id, recipients, ...)`** — for crons / MRP / scheduling
  that already resolved their entities + recipients in worker context. Commits its own writes.
- A worker-side bridge, **`dispatch_notification_direct_job`**, lets a **sync** request-path caller
  (which can't `await` the async dispatcher) hand recipient ids to the worker, which loads them
  tenant-scoped + active and runs `dispatch_direct`. Used by visitor check-in.

---

## Event catalog

`services/notification_catalog.py` is a frozen registry of `CatalogEntry` rows keyed by **`event_key`**
(dot notation, e.g. `wo.blocker_created`). The `event_key` is what lands in `notifications.event_key`,
the preference JSON keys, and the settings matrix — it is stable/frozen.

Each entry carries: `label`, `description`, `category`, `severity` (`info` | `warning` | `critical`),
`default_channels` (subset of `in_app` / `email` / `sms` / `digest`), `mandatory_channel` (the one
channel forced on, or `None`), `sms_eligible`, `recurring` (re-notify suppression), the recipient
spec (`roles` / `departments` / an optional entity-derived `resolver`), and `source_event_types` —
the emitted `OperationalEvent.event_type` strings that map to this key.

- **`SOURCE_EVENT_TYPE_TO_KEY`** is the reverse index the outbox tee consults. A source event type
  must map to exactly one key (a duplicate raises at import). **Emitted event types with no catalog
  entry are deliberately ignored** — future omissions are visible decisions, not silent drops.
- **Transition gates** (`TRANSITION_GATES`): some emits fire on a broad action, so a gate narrows
  them to the meaningful transition. In PR 1: `wo.blocker_resolved` (payload `status=resolved`),
  `ncr.closed` (`status=closed`), `fai.completed` (`status ∈ passed/failed/conditional`),
  `inspection.failed` (`quantity_rejected > 0`). When unsure, the gate does **not** fire.
- **`recurring` re-notify suppression**: for standing-condition detectors, a new in-app row (and the
  push channels) are **suppressed while an unread row for the same `(event_key, related_type,
  related_id, user_id)` exists** — the digest still accrues. So a WO late for two weeks is one inbox
  row + the digest, not 14 emails. Recurring in PR 1: `wo.late`, `stock.low`, `calibration.due`,
  `quote.expiring`, `cert.expiring`, `maintenance.overdue`.

### What actually fires in PR 1

The full v1 catalog is populated so the settings matrix (PR 3) and later PRs already have entries,
but **only entries whose source is wired today actually fire**; the rest are **dormant** catalog rows.

**Outbox-driven** (a committed `OperationalEvent` drives them):
`wo.blocker_created`, `wo.blocker_escalated`, `wo.blocker_resolved` (gated), `wo.released`,
`wo.started`, `wo.completed`, `wo.closed`, `wo.priority_changed` (off by default), `op.completed`
(off by default), `op.ready` (off by default), `production.reduced`, `ncr.created`, `ncr.closed`
(gated), `inspection.failed` (gated), `car.created`, `fai.created`, `fai.completed` (gated),
`po.sent`, `receipt.created`, `receipt.voided`, `receipt.corrected`, `shipment.shipped`,
`coc.generation_failed`, `downtime.started`, `downtime.resolved`.

**Direct-dispatch** (crons / MRP / scheduling call `dispatch_direct`):
`calibration.due`, `wo.late`, `stock.low`, `quote.expiring` (the four recurring crons in
`notification_jobs.py`); `mrp.completed`, `mrp.review_needed` (`mrp_jobs.py`), `mrp.expedite_required`
(`mrp_auto_service.py`), `capacity.overload` (`scheduling_jobs.py`).

**Direct bridge**: `visitor.check_in` — the visitor sign-in host notification. Sign-in is a sync
request path, so `visitor_log_service._notify_host_best_effort` hands off to
`dispatch_notification_direct_job`. The host now gets an **in-app row + a CUI-safe email** (the old
raw host-email is dropped — no double-email). The **visitor's name is intentionally omitted as CUI**;
the host clicks through to `/visitor-log` to see who arrived. (Its catalog entry has
`source_event_types=()` because it is driven by the direct bridge, not the outbox tee.)

**Dormant in PR 1** — catalog rows with `source_event_types=()` that are not yet wired:

- **`quality.hold` — intentionally dormant.** The only quality-hold path
  (`process_sheet_service.create_quality_hold`) already, in one transaction, emits **both**
  `ncr_created` (→ `ncr.created`, mandatory in-app to Quality) **and** `work_order_blocker_created`
  (→ `wo.blocker_created`, mandatory in-app to supervisors/managers). Every recipient a
  `quality.hold` notification would target is therefore already covered mandatorily by an event
  fired from the same action, so wiring it would double-notify. Revisit only if a quality-hold path
  appears that does **not** also raise an NCR + blocker.
- **PR 6 instrumentation** (the emitting domain isn't wired yet): `wo.deleted`, `scrap.recorded`,
  `ncr.voided`, `cert.expiring`, `cert.expired`, `complaint.received`, `complaint.status_changed`,
  `rma.approved`, `rma.received`, `po.deleted`, `vendor.deactivated`, `quote.sent`, `quote.accepted`,
  `shipment.delivery_exception`, `eco.submitted/approved/rejected/implemented`, `maintenance.due`,
  `maintenance.overdue`, `account.locked`, `import.completed`, `import.failed`.
- **PR 5 comments**: `comment.mention`, `comment.added`.

---

## Channels

`_fan_out` resolves each recipient's enabled channels (catalog defaults unless the user has an
explicit saved preference row; the `mandatory_channel` is always forced on) and dispatches per
channel. A per-recipient/per-channel **Redis dedup window** (~5 min, keyed
`(event_key, related_type, related_id, user_id, channel)`) guards retry re-enqueue, the
enqueue-vs-sweeper race, and multiple emits in one flow; it is best-effort (if Redis is down, dedup
is skipped — the `notified_at` marker still bounds duplicates).

- **In-app** — one `Notification` row (`company_id` stamped from the event). This is the canonical
  bell/popover/`/notifications` inbox state, distinct from `NotificationLog` (the per-channel
  delivery-attempt log). Indexed on `(user_id, is_read)`.
- **Email** — enqueues `send_email_job` and writes a `NotificationLog` row (`channel="email"`,
  linked to the in-app row via `notification_id` when one exists). Fixes shipped in PR 1:
  - **Retry layering** (`EmailService.send_email`): an unconfigured SMTP logs a skip and returns
    without raising (so dev doesn't spam ARQ retries), but a **real transport failure now propagates**
    so the job retries and records the terminal outcome (previously swallowed).
  - **`Settings`-driven SMTP**: reads `settings.SMTP_*` instead of dead `os.getenv`.
  - **8 new templates** (`calibration_due`, `low_stock`, `quote_expiring`, `wo_completed`,
    `scheduling_conflicts`, `mrp_complete`, `mrp_review_needed`, `expedite_required`) that used to
    drop silently, plus a generic `notification.html` used by the outbox email path.
  - **Deep links**: `base.html` renders an "Open in Werco" button + a "Manage notifications" footer
    built from `FRONTEND_BASE_URL` (see [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#email-smtp)).
    Empty `FRONTEND_BASE_URL` → the absolute link/footer are omitted.
- **Digest** — a `DigestQueue` row; the daily digest cron (8:00) is unchanged in PR 1.
- **SMS** — resolved but a **no-op stub** in PR 1. The `Company.allow_sms_egress` kill switch column
  exists (default OFF); Twilio, `User.phone` UI, storm caps, and the admin toggle are PR 4.

---

## Content rules (compliance)

### Graduated CUI-safe email rule (plan §11.1)

Email crosses the same external-SMTP boundary as SMS, so bodies are **CUI-safe**: they carry the
**record identifier + event + actor + deep link only** — **no** part descriptions, customer names,
or quantities. The detail lives behind the login. Outbox content is built from the event payload's
identifier keys (WO/NCR/receipt/PO/FAI/CAR/shipment/quote number, equipment/blocker id) + the catalog
label; the `OperationalEvent` payload is itself redaction-filtered at emit time. If your SMTP relay is
inside your assessed boundary (e.g. GCC-High / on-prem), this rule can be relaxed to rich templates —
record that boundary decision here first.

*(SMS terse-body rule is documented when PR 4 lands.)*

### Email deliverability checklist (SPF / DKIM / DMARC)

PR 1 makes email user-visible for the first time — 8 previously-dropped templates plus deep links
now actually send. Before enabling in production, verify DNS for the `SMTP_FROM` domain:

- [ ] **SPF** — a `TXT` record authorizing your SMTP relay's sending IPs (`v=spf1 include:... -all`).
- [ ] **DKIM** — the relay's DKIM public key published at its selector; signing enabled at the relay.
- [ ] **DMARC** — a `_dmarc` `TXT` policy (start `p=none` with `rua=` reporting, then tighten to
      `quarantine`/`reject`) aligned to the `SMTP_FROM` domain.
- [ ] Confirm `SMTP_FROM` / `SMTP_FROM_NAME` match the authenticated domain (no misaligned From).
- [ ] Send a test to a mailbox that reports auth results (e.g. Gmail "show original") and confirm
      SPF + DKIM + DMARC all pass.

---

## Compliance invariants (checklist)

The dispatcher runs in the **worker** with no request-scoped tenancy protection, so these are hard
requirements (enforced in `notification_dispatch.py` / `notification_catalog.py`):

- [ ] **Tenant-scoped rows + RLS** — `notifications` is `TenantMixin` (non-null `company_id` + index)
      and has `ENABLE ROW LEVEL SECURITY` in migration 072 (deny-by-default, app-layer tenancy is the
      enforcement).
- [ ] **Every recipient source filtered by `event.company_id`** — roles, departments, and the
      entity-derived resolvers all query under the triggering event's company.
- [ ] **Every written row stamps `company_id` from the event** — `Notification`, `NotificationLog`,
      `DigestQueue` — never derived-from-nothing.
- [ ] **`get_notification_recipients` requires `company_id`** — the `=None` all-tenants default is
      gone; all callers pass it.
- [ ] **No preference auto-create** — prefs are resolved in memory; an absent row means catalog
      defaults. `_fan_out` never constructs a `NotificationPreference` (the old auto-create omitted
      `company_id` → `IntegrityError` on Postgres, defect §9.8).
- [ ] **Actor exclusion** — the acting user (`event.user_id`) is never notified of their own action.
- [ ] **`is_active` filter** — deactivated users are excluded from every recipient source.
- [ ] **Mark-read is NOT audited** — read state is UI state, not domain state (no `audit_log` write).
- [ ] **Mandatory channels forced on** — a `mandatory_channel` entry can't be fully muted (e.g.
      `ncr.created` / `inspection.failed` force in-app to Quality; `account.locked` forces email).
- [ ] **SMS egress default-off** — `Company.allow_sms_egress` added, fail-closed; no Twilio calls in
      PR 1.

---

## Operational

### Cron / worker

`relay_pending_notifications_job` runs **every 5 minutes** (`worker.py` cron
`minute=set(range(0, 60, 5))`) as the outbox backstop. The new ARQ jobs are
`dispatch_notification_job`, `relay_pending_notifications_job`, and
`dispatch_notification_direct_job`. The four recurring detector crons (calibration 7:00, late-WO
8:00, low-stock 7:30, quote-expiring 9:00) and the MRP/scheduling jobs were repointed onto the new
dispatcher — the legacy blocker `_create_notification_logs` write and the completion-signal
notification leg were removed so events don't double-fire (the webhook leg stays).

### Retention

`cleanup_old_logs_task` (`jobs/maintenance_jobs.py`, Sunday 2 AM) prunes:

- `NotificationLog` rows older than the log window;
- **read** `Notification` rows older than `NOTIFICATION_RETENTION_DAYS` (90);
- **unread** `Notification` rows belonging to **deactivated** (`is_active == False`) users (they are
  excluded from unread counts anyway).

The tamper-evident `audit_log` is never purged by this job (archived separately).

### Migration 072 deploy ordering

`072_notifications_foundation` (`down_revision = 071_soft_delete_purchasing_ncr`) creates
`notifications` (+ RLS), adds `notification_logs.notification_id`, `operational_events.notified_at`
(+ its sweeper index), `users.phone`, `companies.allow_sms_egress`, and does a one-time idempotent
JSON normalization of `notification_preferences` to the 4-channel shape.

- **Run the migration BEFORE the app deploy** that reads/writes these columns — old code neither
  writes nor selects them, so the ordering is safe.
- Each `ADD COLUMN` is nullable-or-constant-default (metadata-only on PG 11+); the new table is empty.
  **`ix_operational_events_notified_at`** builds on `operational_events` (the append-only event
  stream) — if that table is materially large, build the index **`CONCURRENTLY` out-of-band** and let
  the guarded `create_index` no-op, to avoid the non-concurrent build's `SHARE` lock.
- **Historical backfill (prevents a go-live notification storm):** after adding `notified_at`, the
  migration backfills `notified_at = created_at` for every existing row. Production already emits the
  cataloged event types (`work_order_completed`, `ncr_created`, `purchase_order_received`, …), so
  without this the relay sweeper would re-dispatch the entire event history — in-app rows **and emails**
  for months-old events — on first deploy. The one-time `UPDATE` takes a brief write lock on
  `operational_events`; on a very large table run/batch it during the maintenance window. The sweeper
  additionally has a **24-hour lower bound** (`_RELAY_MAX_AGE_HOURS`) so no sustained backlog can ever
  produce a retroactive burst.

---

## Known limitations (carried to later PRs)

Surfaced by the PR-1 adversarial review; each is safe in PR 1 and has a designated home:

- **Delivery-record accuracy** — the email `NotificationLog` is written `sent=True` at *enqueue* time,
  not after confirmed SMTP delivery (the pre-existing pattern). Terminal-outcome write-back
  (`sent=False` + `error` on final ARQ-retry exhaustion) lands with the **admin delivery-failure view
  in PR 3**, which is the only consumer of a "failed" filter.
- **Recurring re-notify suppression is keyed on an unread in-app row** — a recipient who (via the PR-3
  preferences UI) turns *in-app off but email on* for a recurring event would escape suppression. Not
  reachable in PR 1 (no preference-write endpoint; `wo.late` defaults include in-app). **PR 3** must
  extend suppression to email/SMS-only recipients when it ships editable preferences.
- **Recurring-detector crons re-read preferences per (recipient × entity)** — a benign N×M of indexed
  point lookups in nightly jobs; batch per-company if these crons ever grow hot.

## Deferred / roadmap

PR 1 is the foundation; the remaining PRs (see [NOTIFICATIONS_PLAN.md §10](NOTIFICATIONS_PLAN.md)) extend this runbook:

- **PR 2 — Live push**: Redis pub/sub bridge (worker → API), company-aware `send_to_user`, the
  **kiosk WS fence** (reject `scope="kiosk"` tokens at WS connect), Layout `onMessage` → badge/toast.
- **PR 3 — Preferences & settings**: prefs/catalog APIs, My Settings page, admin defaults tab +
  admin-scoped delivery-failure view, digest fixes (30-min cron, WEEKLY, Central-time `digest_time`),
  one-click unsubscribe.
- **PR 4 — SMS**: `User.phone` UI + Twilio service/job + the `allow_sms_egress` admin toggle + storm
  caps; the terse CUI-safe SMS body rule is documented here when it lands.
- **PR 5 — Comments & mentions**: `comments` / `comment_mentions` / `entity_watchers`, `<CommentsPanel>`,
  the `comment.mention` / `comment.added` events, per-entity-type RBAC (documented in
  [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) when it lands), auto-watch.
- **PR 6 — Event coverage**: instrument the dormant domains (quotes, complaints/RMA, ECO,
  maintenance, delivery exceptions, account lockout, cert-expiry cron) so their catalog rows fire.
