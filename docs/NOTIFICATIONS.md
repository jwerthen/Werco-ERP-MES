# Notifications

Operational runbook for the Werco notification pipeline. This documents **PR 1 (Foundation +
in-app inbox)** — the transactional outbox, the event catalog, the in-app and email channels, and
the compliance invariants — and **PR 4 (SMS / Twilio)**, the deliberately terse SMS channel (see
[SMS channel](#sms-channel-twilio)). The email and SMS **content rules were revised 2026-07-29**
after CMMC L2 was descoped — read
[Content rules](#content-rules-compliance) before changing what a notification says. Later PRs
extend this file (see
[Deferred / roadmap](#deferred--roadmap) at the end). The authoritative design spec is
[NOTIFICATIONS_PLAN.md](NOTIFICATIONS_PLAN.md); this runbook describes what is actually
implemented.

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

## What PR 4 adds

- The **SMS channel**, live over Twilio (`services/sms_service.py` + `jobs/sms_jobs.py`), gated by
  **two** default-off switches — see [SMS channel](#sms-channel-twilio).
- The single **SMS body builder** (`services/sms_content.py`) and its standing content rule
  (below, alongside the email rule).
- **`User.phone`** made real (E.164, validated with `phonenumbers`), self-service at
  `PUT /users/me/phone` and on the admin user create/update paths — where `phone` had been a phantom
  schema field that was silently dropped.
- **Self-service preferences** for the SMS channel (`GET`/`PUT /users/me/notification-preferences`)
  plus `POST /users/me/test-sms`, and the ADMIN-only `PUT /companies/me/sms-egress` kill-switch
  toggle. UI: **My Settings** (`/settings`, all roles) and **Admin Settings → SMS Privacy**
  (`/admin/settings?tab=smsprivacy`).
- **`NotificationLog.provider_message_id` / `provider_status`** — the Twilio message SID + provider
  status on each delivery row.

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
`coc.generation_failed`, `downtime.started`, `downtime.resolved`, `material.allocation_shortage`,
`material.backflush_shortage`, `material.allocation_consumption_failed`, `material.backflush_failed`,
`material.backflush_demand_refused`.

**Direct-dispatch** (crons / MRP / scheduling call `dispatch_direct`):
`calibration.due`, `wo.late`, `stock.low`, `quote.expiring` (the four recurring crons in
`notification_jobs.py`); `mrp.completed`, `mrp.review_needed` (`mrp_jobs.py`), `mrp.expedite_required`
(`mrp_auto_service.py`), `capacity.overload` (`scheduling_jobs.py`).

> **`material.allocation_shortage` — added with the material-consumption engine.** Not part of the
> original notification build; it is registered by the same change that introduced the tied-material
> consumption path, so no release exists in which the shortage fires with no catalog entry.
>
> | Field | Value |
> |-------|-------|
> | `event_key` | `material.allocation_shortage` |
> | `label` | Tied material shortage |
> | `category` | **Purchasing** |
> | `severity` | **warning** |
> | `default_channels` | `in_app` + `email` |
> | `mandatory_channel` | none |
> | `sms_eligible` | `false` |
> | `recurring` | `false` |
> | Recipients | departments **Purchasing** + **Inventory** (no role spec) |
> | `source_event_types` | `("material_allocation_shortage",)` — outbox-driven |
>
> **What emits it.** `services/material_consumption_service.py::_record_allocation_shortage`, at the
> moment a work order's completion consumes material tied to it (a
> `work_order_material_allocations` row) and the source lot cannot cover the demand. The lot is driven
> negative, a tamper-evident `ALLOCATION_SHORTAGE` `audit_log` row is written (that row, not the
> notification, is the compliance record), and the event is emitted **best-effort** — a signal failure
> can never fail an in-flight completion. The shortage itself never blocks production.
>
> Deliberately **distinct from `stock.low`**, which is cron-driven off reorder points and `recurring`;
> this one fires at the moment of consumption and is not re-notify-suppressed, because each shortage
> is a discrete event on a specific work order. It is also distinct from the older
> **`backflush_shortage`** event — ~~which has **no catalog entry** and therefore still notifies
> nobody~~ **wired up in PR 4.4; see the next entry.**

> **`material.backflush_shortage` — added in PR 4.4 (material consumption, backflush lot policy).**
> **What this closed is a silent drop, not a missing feature.** `BACKFLUSH_SHORTAGE_EVENT_TYPE =
> "backflush_shortage"` has been **emitted since Batch 6** with **no catalog row**, and
> `SOURCE_EVENT_TYPE_TO_KEY` ignores uncataloged event types by design — so a BOM/routing backflush
> shortage was written to the tamper-evident audit chain and then notified to **nobody**, for four
> PRs. This entry is the whole wiring; **the emit site is unchanged**.
>
> | Field | Value |
> |-------|-------|
> | `event_key` | `material.backflush_shortage` |
> | `label` | Backflush material shortage |
> | `category` | **Purchasing** |
> | `severity` | **warning** |
> | `default_channels` | `in_app` + `email` |
> | `mandatory_channel` | none |
> | `sms_eligible` | `false` |
> | `recurring` | `false` |
> | Recipients | departments **Purchasing** + **Inventory** (no role spec) |
> | `source_event_types` | `("backflush_shortage",)` — outbox-driven |
>
> Placed immediately after `material.allocation_shortage` and mirroring it exactly. **Two keys rather
> than one, deliberately:** same moment (consumption), different engine — that one fires from the
> operation-scoped tie engine (`material_consumption_service`), this one from the work-order
> completion backflush (`completion_inventory_service`). Distinct keys let an operator tell a
> tied-material shortage from a BOM shortage at a glance, and let the settings matrix gate them
> independently. The audit action strings (`ALLOCATION_SHORTAGE` / `BACKFLUSH_SHORTAGE`) were already
> split for the same reason.
>
> **What emits it.** `services/completion_inventory_service.py::_record_backflush_shortage`, when a
> completing work order's component demand cannot be covered by stock. The lot is driven negative, a
> tamper-evident `BACKFLUSH_SHORTAGE` `audit_log` row is written — **that row, not the notification,
> is the compliance record** — and the event is emitted **best-effort**, so a signal failure can never
> fail an in-flight completion. The shortage never blocks production.
>
> **Two PR 4.4 changes affect what the payload means.** (1) The shortfall is now computed against the
> lots the draw **actually walked**, closing a defect where a multi-lot component could be driven
> deeply negative with **no** shortage row and **no** event at all — so this key firing *more* often
> than the old code would have is the fix working, not a regression. (2) The audit row now discloses
> **why the rest of the stock was not drawn**: on an unpinned draw, the segregated stock the predicate
> passed over (`held_quantity_skipped` / `held_lot_numbers`), so a shortage is never reported bare
> against material physically on the rack; on a **pinned** draw, the pin itself (`pinned_lot`), because
> there the pin — not any lot's status — is the constraint. The two clauses are mutually exclusive.
> Neither reaches the notification body (see the CUI rule below): the body is machine-composed from the
> catalog label plus one allowlisted identifier, so lot numbers never leave the audit row and the event
> payload.
>
> **Dormant in practice, and say so.** The BOM/routing half of that leg is gated on
> `Part.backflush_components`, which through PR 4.4 had **no writer anywhere in `app/`** — so this key
> could only fire from the **work-order-scoped material tie** half of the same leg. **PR 4.5 exposed the
> flag** (settable on `PUT /parts/{id}` / `PUT /materials/{id}` behind a 409 readiness gate, still
> default-off), so the BOM/routing half is now **reachable but unexercised**: no production part has
> opted in, and this key still has not fired from that half. See
> `docs/MATERIAL_CONSUMPTION_PLAN.md` → "Exposing the flag (PR 4.5)".

> **`material.allocation_consumption_failed` and `material.backflush_failed` — added in PR 4.4, and
> they exist because the SHORTAGE keys above would otherwise have been unreachable on some
> deployments.** These are the **degraded siblings** of the two shortage keys: same engines, same
> moment, but the draw **raised and was rolled back to its savepoint**, so *nothing moved at all*.
>
> | Field | `material.allocation_consumption_failed` | `material.backflush_failed` |
> |-------|------------------------------------------|------------------------------|
> | `label` | Tied material consumption failed | Backflush consumption failed |
> | `category` | **Purchasing** | **Purchasing** |
> | `severity` | **warning** | **warning** |
> | `default_channels` | `in_app` + `email` | `in_app` + `email` |
> | `sms_eligible` / `recurring` | `false` / `false` | `false` / `false` |
> | Recipients | departments **Purchasing** + **Inventory** | same |
> | `source_event_types` | `("material_allocation_consumption_failed",)` | `("backflush_component_failed",)` |
> | Emitted by | `material_consumption_service::_record_consumption_failed` | `completion_inventory_service::_record_backflush_component_failed` |
> | Audit twin | `ALLOCATION_CONSUMPTION_FAILED` (`success=false`) | `BACKFLUSH_COMPONENT_FAILED` (`success=false`) |
>
> **Why they are not optional tidying.** The audit rows alone made the degraded path strictly
> **quieter** than the lesser condition it degrades from — a shortage still moves stock and still
> notifies, while "the draw rolled back, so stock was never depleted" reached nobody. That is the worse
> material-trail gap, and it is not a corner case: on a database where
> `chk_inventory_items_quantity_non_negative` is live, **every** shortage arrives here instead, so
> `material.backflush_shortage` — PR 4.4's headline signal — would be exactly the key that never fires.
> Kept **separately keyed** from the shortage pair so an operator can tell "stock went negative" from
> "stock never moved" without opening the audit log, and so the settings matrix can gate them
> independently. Both emit **best-effort** on the post-rollback outer transaction, so a signal failure
> can never fail an in-flight completion, and the `audit_log` row — not the notification — remains the
> compliance record.

> **`material.backflush_demand_refused` — added in PR 4.5, the third member of that family.** Same
> category / severity / channels / recipients as the two above (Purchasing, warning, `in_app` + `email`,
> departments Purchasing + Inventory; `sms_eligible` and `recurring` both `false`).
> `source_event_types = ("backflush_demand_refused",)`, emitted by
> `completion_inventory_service::_emit_demand_refused_event`, audit twin `BACKFLUSH_DEMAND_REFUSED`.
>
> Where the other two mean "the draw was attempted and went wrong", this one means **the system judged
> the DEMAND itself untrustworthy and declined to issue it** — a BOM line with quantity 0, a
> unit-of-measure mismatch, a deleted component, a cycle, a routing/BOM disagreement. It is the **least
> self-correcting** of the three: the refusal fires at completion on a part that is *already* armed and
> nothing disarms it, so absent a notification the same component silently under-issues on every
> subsequent job while the BOM line the diagnostic names stays broken. Emitted **once per refused
> scope** (the first diagnostic naming a component, or the first structural blocker), not once per
> diagnostic — one component violating two rules is one notification — under its own `begin_nested()`
> savepoint, because this path is reachable from a reconcile-on-read GET that must never 500.

**Direct bridge**: `visitor.check_in` — the visitor sign-in host notification. Sign-in is a sync
request path, so `visitor_log_service._notify_host_best_effort` hands off to
`dispatch_notification_direct_job`. The host gets an **in-app row + an email** (the old raw
host-email is dropped — no double-email).

**The notification now names the visitor (changed 2026-07-29).** Title and body read
`Jane Smith checked in (Acme Corp) and named you as their host.`, and the email renders the
`visitor_check_in` template — which had existed with **no caller** until this change — with
`visitor_name`, `visitor_company`, `purpose` (plus the purpose note when present), `signed_in_at`
and `station_label`. The rule it replaces omitted the name as CUI, which left the host with an alert
they had to log in to act on; the basis for the change is the
[boundary decision of record](#content-rules-compliance). Every field is read off the `VisitorLog`
row already in scope (no extra query), the context carries **JSON-safe primitives only** (it rides
ARQ/Redis, so no ORM row and no raw `datetime` — `signed_in_at` is pre-formatted to Central because
an email has no client-side localizer), and `redact_event_payload` does **not** apply on this path:
direct dispatch has no stored event payload. (Its catalog entry has `source_event_types=()` because
it is driven by the direct bridge, not the outbox tee.)

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
    **Caveat — three of the template files still have no caller**: `wo_completed` (listed above),
    `wo_released` and `ncr_created` are never named by a `template=` argument anywhere, so adding
    the file did not by itself make anything send. `visitor_check_in` was in that same state until
    2026-07-29, when the host check-in path started rendering it (see [Direct
    bridge](#what-actually-fires-in-pr-1)). Wire a caller or drop the file; don't assume a template
    that exists is reachable.
  - **Deep links**: `base.html` renders an "Open in Werco" button + a "Manage notifications" footer
    built from `FRONTEND_BASE_URL` (see [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#email-smtp)).
    Empty `FRONTEND_BASE_URL` → the absolute link/footer are omitted.
- **Digest** — a `DigestQueue` row; the daily digest cron (8:00) is unchanged in PR 1.
- **SMS** — live over Twilio as of PR 4. The leg fires only when **all** of the following hold:
  the catalog entry is `sms_eligible`, the user has explicitly enabled `sms` for that event
  (no catalog entry ships `sms` in its defaults, so this is always an opt-in), recurring
  suppression is clear, and the user has a `phone` on file. It writes a `NotificationLog` row and
  enqueues `send_sms_job`; the `allow_sms_egress` kill switch is then re-checked fail-closed inside
  `sms_service` before anything leaves the boundary. Full detail in
  [SMS channel](#sms-channel-twilio).

---

## SMS channel (Twilio)

### Two default-off gates — SMS does nothing until both are on

Read this first when SMS "isn't working". Nothing is broken by default; **it is off by design, twice
over**:

1. **Per-company** — `Company.allow_sms_egress` (Boolean, non-null, **default OFF** for every
   tenant). Flipped only by an **ADMIN** via `PUT /companies/me/sms-egress` (UI: Admin Settings →
   SMS Privacy). Twilio sits outside the CUI boundary, so this is a kill switch in the same family
   as `allow_ai_egress` / `allow_carrier_egress` / `allow_print_egress`, and it is re-checked
   **fail-closed on every send** — turning it off also stops messages already queued in ARQ.
2. **Per-user** — an explicit opt-in **plus** a saved phone number. **No catalog entry ships `sms`
   in its `default_channels`**, so the SMS leg is unreachable until the user turns it on at
   `PUT /users/me/notification-preferences` (UI: My Settings), and a toggle without a phone on file
   is inert (`_fan_out` skips the leg when `user.phone` is empty).

On top of both: only events flagged **`sms_eligible`** in the catalog can ever send. Today that is
`wo.blocker_created`, `wo.blocker_escalated`, `ncr.created`, `inspection.failed`,
`downtime.started` — plus `quality.hold`, which is eligible but [deliberately
dormant](#what-actually-fires-in-pr-1). Enabling `sms` for a non-eligible event is refused with
**400**.

### How a message flows

```
dispatcher SMS leg (_dispatch_sms, notification_dispatch.py)
  1. build the body    → sms_content.build_sms_body(label, identifier)   (CUI-safe, see below)
  2. storm check       → reserve_sms_quota(user_id)      (Redis, 5/hour per user)
  3. write NotificationLog(channel="sms", sent=False, company_id from the EVENT)
       └─ over cap? record WHY on that row, arm the deferred collapse, STOP (no enqueue)
  4. flush (assign the log id), then enqueue send_sms_job(..., notification_log_id=<id>)
       with _defer_by = 2s  ── so the row is COMMITTED before a worker looks it up
                    │
  ARQ worker: send_sms_job → jobs/sms_jobs.send_sms_task
  5. re-resolve the recipient: User.id == user_id AND company_id AND is_active
  6. send_sms(db, company_id, to=user.phone, body)   → egress gate → Twilio
  7. UPDATE that same NotificationLog row: sent / error / provider_message_id / provider_status
```

Load-bearing details:

- **One attempt = one `NotificationLog` row, retries included.** The dispatcher creates the row
  (`sent=False`) and hands the job its id; `_record_delivery` **updates** that row rather than
  appending one per ARQ attempt. The lookup is tenant-scoped (`id` **and** `company_id`), so a job
  can never touch another tenant's log row. If the id is absent (the storm-collapse message), a
  fresh row is inserted.
- **The 2-second send deferral** (`_SMS_ENQUEUE_DEFER_SECONDS`) exists so the pre-created row is
  committed before a (possibly different) worker process reads it. Without it a fast pickup would
  miss the uncommitted row and insert a second one. Delivery is correct either way; the defer is
  what keeps the delivery log one-row-per-attempt.
- **The job takes `user_id`, never a phone number.** PII stays out of the Redis job payload, and the
  recipient is re-resolved **tenant-scoped + `is_active`** at send time — so a user deactivated
  between dispatch and delivery is not messaged (recorded as `recipient is inactive or has no phone
  number on file`).
- The `company_id` on every log row is stamped **from the triggering event**, never from the
  recipient.

### Storm control

`SMS_HOURLY_CAP_PER_USER = 5` messages per user per hour — a Redis counter over a fixed
`SMS_QUOTA_WINDOW_SECONDS = 3600` window that starts at the user's first SMS and expires with it.
SMS here is critical-events-only and opt-in, so this is a storm valve, not a quota.

- Messages **1–5** in the window send normally.
- **Over cap**, the individual message is suppressed — but *visibly*: the `NotificationLog` row is
  still written with `error = "suppressed: per-user SMS cap (5/hour) reached"`, so **"why didn't I
  get an SMS?" is answerable from the delivery log** rather than being a silent drop.
- The **first** overflow in a window arms **one deferred collapse message**
  (`send_sms_overflow_job`, deferred `SMS_COLLAPSE_DELAY_SECONDS = 120`s so the count reflects the
  whole burst): `Werco: 7 more alerts - check the app. Log in to view.` It carries **no identifiers
  at all**. The collapse **bypasses the per-user cap** — it *is* the cap's safety valve — but still
  passes through the egress gate like any other send.
- The collapse arms **at most once per quota window**, so the true per-user ceiling is
  **cap + 1 = 6 messages/hour**. The arm key's TTL is `SMS_QUOTA_WINDOW_SECONDS`, deliberately
  *longer* than the collapse delay and deliberately **not** cleared by `settle_sms_overflow`. (If
  the arm expired when the collapse fired — as it would if both used `SMS_COLLAPSE_DELAY_SECONDS` —
  the next suppressed alert would re-arm it, and a sustained storm would yield one collapse every
  two minutes, ~35/hour, defeating the cap in exactly the scenario it exists for.)
- The collapse job **reads** the overflow counter, sends, and only then **settles** it
  (`decrby`, not delete) — so a retried collapse never loses alerts, and messages suppressed while
  the collapse was in flight roll into the next one.

### Test-send budget

`POST /users/me/test-sms` is bounded **per identity**, not only per IP:
`SMS_TEST_HOURLY_CAP_PER_USER = 3` per hour over the same `SMS_QUOTA_WINDOW_SECONDS` window, via
`reserve_test_sms_quota`. The per-IP limiter in `main.py` keys on address alone, so one account can
multiply it by rotating egress IPs, and it is disabled outright wherever `RATE_LIMIT_ENABLED=false`
(the documented E2E config) — leaving carrier spend unbounded without this second control.

The budget is **separate** from `SMS_HOURLY_CAP_PER_USER`, so testing the button never eats into the
critical-alert allowance. The reservation is **tri-state**, and the two refusals are distinguished
on purpose:

| Outcome | HTTP | Why distinct |
|---|---|---|
| `TEST_QUOTA_ALLOWED` | 200 | — |
| `TEST_QUOTA_CAPPED` | **429** | The user really did hit 3/hour. |
| `TEST_QUOTA_UNAVAILABLE` | **503** | Redis is down. Refuse (see below), but do **not** claim a limit they never hit — they would keep retrying against a false explanation. |

### The two opposite failure postures (the subtlest thing here)

Two Redis/DB-dependent controls sit next to each other in this path and fail in **opposite**
directions. This is deliberate; do not "make them consistent".

| Control | Where | On failure | Why |
|---|---|---|---|
| **Egress gate** (`allow_sms_egress`) | `sms_service._sms_egress_allowed` | **FAILS CLOSED** — deny | It protects the **CUI boundary**. A control that cannot verify "allowed" must deny. Unknown tenant, missing company row, or a DB exception all return `False`, and `_sms_egress_allowed(db, None)` is `False` — **deliberately stricter than `llm_client._ai_egress_allowed`**, which tolerates the no-tenant edge. Every SMS caller has a tenant (the dispatcher stamps it from the event; the API path takes it from `get_current_company_id`), so a missing one is a bug, and a bug must not egress. |
| **Storm cap** (`reserve_sms_quota`) | `sms_service` | **FAILS OPEN** — send + `WARNING` | If Redis is down the counter cannot be read. SMS is critical-only, opt-in, and kill-switch-gated, so **an extra message beats dropping a critical alert** (an AS9100D awareness control). The per-recipient/channel dedup window (also fail-open) still applies, and the cap resumes the moment Redis returns. |
| **Test-send cap** (`reserve_test_sms_quota`) | `sms_service` | **FAILS CLOSED** — 503 | The mirror image of the storm cap directly above, and for the reason that distinguishes them: a test SMS is **not** a critical alert. Nobody is deprived of safety information by a button that says "try again shortly", whereas failing open restores the unbounded-spend hole this cap exists to close. Volume controls follow what they protect — the alert budget protects *awareness*, this one protects *spend*. |

### Twilio configuration

Credentials come **exclusively** from `Settings`/environment — nothing is hardcoded. See
[ENVIRONMENT_VARIABLES.md → SMS (Twilio)](ENVIRONMENT_VARIABLES.md#sms-twilio).

- **Auth mode 1 (preferred)** — `TWILIO_ACCOUNT_SID` + `TWILIO_API_KEY_SID` (an `SK…` key) +
  `TWILIO_API_KEY_SECRET`. Revocable per key without rotating the account credential; used whenever
  **both** API-key values are set.
- **Auth mode 2 (legacy fallback)** — `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`.
- **Sender resolution** — `TWILIO_MESSAGING_SERVICE_SID` wins over `TWILIO_FROM_NUMBER`.
- **Unconfigured is a soft skip, not a failure.** With no credentials (or no sender), `send_sms`
  logs and returns `SMSResult(status="skipped")` **without raising** — the same posture as the
  unconfigured-SMTP path, so a dev/test environment never spams ARQ retries. Credentials present but
  the `twilio` package missing is likewise a soft skip (`reason="library_missing"`), logged as an
  error because it is a deployment defect.
- The client is a lazily-built module-level singleton (`get_twilio_client`); `reset_twilio_client()`
  drops it for credential rotation or tests.
- New backend dependencies (`backend/requirements.txt`): `twilio` and `phonenumbers` (E.164
  parsing/validation). Both are pinned; rebuild the **worker** image too, or its SMS jobs soft-skip
  with `library_missing`.

### Error handling

| Outcome | Raised by `send_sms` | Job behavior |
|---|---|---|
| Egress disabled / unresolvable tenant | `SMSEgressDisabledError` | Terminal — record `SMS egress is disabled for this company`, no retry |
| Unparseable stored number | `InvalidPhoneNumberError` | Terminal — record `invalid phone number on file: …` |
| **Twilio 4xx other than 429** (bad number, opted-out recipient, unpermitted region) | `SMSPermanentError` (carries `status`/`code`) | **Terminal — recorded with `provider_status`, no retry burned** |
| **Twilio 5xx / 429 / network** | the original exception propagates | **Re-raised → ARQ retries** (the attempt is recorded first as `transport failure (will retry): …`) |
| Twilio unconfigured / SDK missing | — (returns `skipped`) | Recorded as `skipped: not_configured` / `skipped: library_missing` |

Phone numbers are never logged in full — `mask_phone()` renders them as `***1234`.

### Phone is field-minimized

`User.phone` serializes in **exactly one schema**: the local `UserResponse` in
`api/endpoints/users.py`, used only by the self-profile routes (`GET /users/me`, the
`/users/me/*` self-service routes) and the ADMIN/MANAGER user-management routes. General user
serialization goes through `app.schemas.user.UserResponse` (auth/token/platform browse) and the
per-domain `UserSummary`-style schemas — **none of which expose a phone number**, and neither will
PR 5's mention-search. Keep it that way when adding user-shaped responses.

---

## Content rules (compliance)

### Email content rule (plan §11.1) — **relaxed 2026-07-29**

> #### Boundary decision of record — 2026-07-29
>
> This section is the register the original rule named ("record that boundary decision here
> first"), so the decision is written here before the code that depends on it.
>
> **What changed.** CMMC Level 2 was deprioritized on 2026-07-28 (PR #163). The original rule
> was derived from a CUI-boundary analysis: email leaves for an external SMTP relay, therefore
> bodies carry no field detail. With no CUI programme, that rationale no longer governs.
>
> **Be precise about the basis.** We are **not** invoking the original escape hatch. That clause
> allowed rich templates when the relay sits *inside an assessed boundary* — there is no
> assessment, so that condition is not met and should not be claimed. The actual basis is
> weaker and worth stating plainly: this is now an ordinary business judgement that the
> operational value of a useful email outweighs the privacy cost of shop record detail sitting
> in employees' mailboxes and on the relay. **If CMMC is ever revived, this decision must be
> revisited — it is not an assessed control.**
>
> **What email bodies MAY now carry:** the record identifier and event label (as before), plus
> the operational detail already present in the event payload — quantities, statuses,
> transitions, day counts, and short reasons/notes — and, on the visitor check-in email, the
> visitor's name, company and purpose.
>
> **What they still may NOT carry:** anything `redact_event_payload` strips at emit time
> (credentials, tokens, `raw_text` / `document_text` / `drawing_text`, `cui`-named keys) — that
> filter is unchanged and remains the backstop. Enrichment also reads the **payload only**; the
> dispatcher does not re-query the database to resolve `part_id` into a part number, so part
> numbers and customer names remain absent. That is a scope and N+1 decision, not a security
> one — a future change may add them deliberately.

Outbox content is built from the event payload's identifier keys (WO/NCR/receipt/PO/FAI/CAR/
shipment/quote number, equipment/blocker id) + the catalog label, plus a composed detail line
from a curated payload allowlist (see `_DETAIL_KEYS` in `services/notification_dispatch.py`).
The `OperationalEvent` payload is itself redaction-filtered at emit time.

`CatalogEntry.description` is **not** the email body builder — it is also served to the
notification-preferences matrix by `GET /notifications/catalog`, so it stays a static string.
Body composition lives in `_content_for_event`.

### Terse SMS body rule (plan §3.4 / §11.1) — **narrowly relaxed 2026-07-29**

Still a **standing rule**, not a per-call judgement — and deliberately relaxed **much less than
email**, for a reason that has nothing to do with CMMC:

> An SMS renders on a **locked phone screen**. Anyone who can see the phone can read it, without
> unlocking it and without authenticating to anything. That exposure is real whether or not a CUI
> programme exists, so descoping CMMC does not license putting shop detail on the wire here the
> way it does for a mailbox. Twilio also bills **per segment**, so body length is a cost input,
> not just a style choice.

A body may carry:

1. the record **identifier** (e.g. `WO-1042`, `NCR-2026-014`),
2. the catalog **event label** (e.g. "Work order blocked / on hold"),
3. **one short classifier** from a closed vocabulary (e.g. `machine down`, `material`) — new, and
4. the **"log in to view"** pointer.

```
Werco: {identifier} - {catalog label} ({detail}). Log in to view.
Werco: {identifier} - {catalog label}. Log in to view.     # no safe detail present
Werco: {catalog label}. Log in to view.                    # no safe identifier either
```

**Still never**: customer names, part numbers or descriptions, quantities, prices, operator names,
or **any operator-typed free text** — blocker notes, scrap reasons, defect descriptions, step
labels, and caller-composed titles all stay off SMS. The classifier comes from a fixed
`_SMS_DETAIL_KEYS` allowlist of enum-shaped payload fields and is sanitized by `safe_detail()`,
which refuses anything that does not look like a short closed-vocabulary token. The full detail
still lives behind the login.

`services/sms_content.py` is the **only** place an SMS body is built, and it enforces the rule
structurally:

- **It deliberately does not accept the caller-composed `title` / `body`.** Those are composed
  freely by crons and direct dispatchers and legitimately carry equipment names, quote numbers, and
  day counts. The body is assembled from the catalog `label` + a sanitized payload identifier only,
  so a future payload key carrying free text can never reach an SMS.
- **`safe_identifier()`** accepts only record-number shapes — must start alphanumeric, then only
  `A-Z a-z 0-9 . _ / # -` and spaces, ≤ 40 chars. Anything free-text-like is **dropped** and the
  body degrades to the label alone. The identifier itself comes from the fixed
  `_IDENTIFIER_KEYS` allowlist on the event payload (work-order / NCR / receipt / PO / FAI / CAR /
  shipment / quote number, equipment id, blocker id).
- **160-char cap** (`SMS_MAX_LENGTH`) = one GSM-7 segment, so a storm cannot silently multiply into
  per-segment billing. The **label truncates before the "log in" pointer is dropped** — every
  message keeps its pointer to the app.
- The storm-collapse body (`build_overflow_sms_body`) and the test-SMS body
  (`build_test_sms_body`) carry no identifiers at all, so they are CUI-safe by construction.

Everything in that module is pure and side-effect free, so the content rule is unit-testable and
auditable in isolation.

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
- [ ] **SMS egress default-off, fail-closed** — `Company.allow_sms_egress` is re-resolved before
      **every** Twilio call in `sms_service._sms_egress_allowed`; unknown tenant, missing company
      row, `company_id is None`, or any exception all **deny**. No phone number and no body leave
      the boundary on a denial.
- [ ] **SMS bodies built only by `sms_content.build_sms_body`** — catalog label + sanitized
      identifier + at most one vetted classifier, **never the caller-composed title/body** (that
      refusal is unchanged; see the terse-body rule above). The classifier clears **two independent
      fences**: `_SMS_DETAIL_KEYS` in `notification_dispatch` picks the eligible payload **field**
      (enum-valued keys only — no operator-typed field is listed), and `safe_detail()` in
      `sms_content` vets the **value** (single whitespace-free token, letters/`_`/`-` only, ≤ 24
      chars, ≤ 3 words). Neither alone is sufficient. Adding another SMS call site means routing it
      through that builder; adding a key to `_SMS_DETAIL_KEYS` means confirming that field can only
      ever hold an enum value.
- [ ] **SMS is doubly opt-in** — per-company kill switch **and** per-user opt-in + phone; only
      `sms_eligible` catalog events are offerable, and the API rejects enabling `sms` on a
      non-eligible event with **400**.
- [ ] **Phone changes are audited** — self-service `PUT /users/me/phone` and the admin user
      create/update paths write `audit_log` rows (a silently redirected alert channel would be an
      audit gap). The **egress toggle is double-audited** (field update **and**
      `sms_egress_enabled` / `sms_egress_disabled` status change).
- [ ] **Phone is field-minimized** — it serializes only in the self-profile / admin
      user-management `UserResponse`, never in general user lists (see above).

---

## Operational

### Cron / worker

`relay_pending_notifications_job` runs **every 5 minutes** (`worker.py` cron
`minute=set(range(0, 60, 5))`) as the outbox backstop. The new ARQ jobs are
`dispatch_notification_job`, `relay_pending_notifications_job`,
`dispatch_notification_direct_job`, and — as of PR 4 — `send_sms_job` and
`send_sms_overflow_job` (both registered in `worker.py::WorkerSettings.functions`; a worker deployed
without them will leave SMS `NotificationLog` rows stuck at `sent=False`).
The four recurring detector crons (calibration 7:00, late-WO
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

### Turning SMS on (end-to-end)

Four steps, in order. Skipping any one leaves SMS silently inert.

1. **Ops — set the Twilio env vars** on the **Railway backend service** (and the worker, if it runs
   as a separate service): `TWILIO_ACCOUNT_SID` + `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET`
   (or the legacy `TWILIO_AUTH_TOKEN`), plus `TWILIO_MESSAGING_SERVICE_SID` **or**
   `TWILIO_FROM_NUMBER`. Names and combinations in
   [ENVIRONMENT_VARIABLES.md → SMS (Twilio)](ENVIRONMENT_VARIABLES.md#sms-twilio). Never commit
   values. Redeploy so the settings are picked up.
2. **Admin — enable company egress**: Admin Settings → **SMS Privacy**
   (`/admin/settings?tab=smsprivacy`) → turn on `allow_sms_egress` (confirm-on-enable). This is a
   **CUI-boundary decision** and lands on the tamper-evident audit trail twice.
3. **User — save a phone + opt in**: My Settings (`/settings`) → enter the phone number (stored
   E.164; a number that can't be parsed is rejected with **400**) → enable **SMS** on the
   SMS-eligible events they want.
4. **User — click "Send test SMS"** (`POST /users/me/test-sms`, rate-limited **3/minute**). It
   targets the caller's own number only, goes through the same `sms_service` path as real alerts,
   and writes a `notification_logs` row (`event_type = "sms.test"`).

### Diagnosing "no SMS arrived"

Walk these in order — the first four are configuration, not bugs:

1. **Company egress** — is `allow_sms_egress` ON for that tenant (Admin Settings → SMS Privacy)?
   A denial is recorded as `SMS egress is disabled for this company` on the delivery row.
2. **Phone on file** — `GET /users/me/notification-preferences` returns `phone`,
   `sms_egress_enabled`, and `sms_configured`, which is exactly what the UI uses to explain an
   inert toggle. No phone ⇒ the dispatcher skips the SMS leg entirely (no row is written).
3. **Event eligibility** — is the event `sms_eligible` in the catalog
   (`GET /notifications/catalog`)? Non-eligible events cannot be enabled (400).
4. **User opt-in** — SMS is never in a catalog default; confirm the per-event `sms` toggle is on.
5. **Storm cap** — look for `suppressed: per-user SMS cap (5/hour) reached` on the
   `notification_logs` row, and for the collapse message (`event_type = "sms.storm_collapse"`).
6. **Delivery rows** — query the `notification_logs` **table** for `channel = "sms"` and the user:
   `sent`, `error`, `provider_message_id` (Twilio SID), `provider_status` (`queued` / `accepted` /
   …). `GET /notifications/logs` shows `sent` / `error` / `event_type` but **does not serialize the
   two provider columns** today (PR 3's admin delivery view is their intended surface), so the SID
   needs DB access. A Twilio-side delivery failure *after* acceptance is not visible here at all —
   the delivery-receipt webhook is deferred (see
   [Known limitations](#known-limitations-carried-to-later-prs)); take the SID to the Twilio console.
7. **Worker** — `send_sms_job` / `send_sms_overflow_job` must be registered in the running worker;
   rows stuck at `sent=False` with no `error` mean the job never ran.
8. **Twilio configured** — `sms_configured` false ⇒ rows read `skipped: not_configured`; the env
   vars are missing on that service.

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

### PR 4 migration (SMS delivery provenance)

PR 4 adds two **nullable, additive** columns to `notification_logs` with **no backfill**:

| Column | Type | Meaning |
|---|---|---|
| `provider_message_id` | `String(64)` | The Twilio message SID for an SMS row |
| `provider_status` | `String(40)` | The provider-reported status at send (`queued` / `accepted` / …) |

Both are channel-agnostic: the email channel leaves them `NULL` today, and a future ESP with message
ids can reuse them. Because they are nullable with no default, the migration is metadata-only and
old code neither reads nor writes them — so the usual ordering (**run the migration before the app
deploy**) is safe, and a rollback is a plain drop. The revision lands as the next Alembic version
after `072_notifications_foundation` (authored alongside this PR; confirm the exact revision id in
`backend/alembic/versions/` before deploying).

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
- **SMS delivery receipts and inbound STOP are not wired** (PR 4, deliberate — plan §3.4 defers
  both). `provider_status` records only the status Twilio returned *at send* (`queued`/`accepted`);
  a later carrier-side failure is visible in the Twilio console, not in `notification_logs`.
  Opt-out relies on **Twilio's own STOP handling**, which covers the US compliance requirement — a
  recipient who texts STOP stops receiving messages, but Werco's per-user `sms` toggles still read
  as ON, so the delivery log will show provider rejections rather than an opt-out state.
- **`dispatch_direct` callers pass no `sms_identifier`** — an SMS-eligible direct dispatch (crons /
  MRP / scheduling) would send the label-only body. Harmless today: no currently-wired direct caller
  targets an SMS-eligible event. Pass `sms_identifier` when one does.
  (`dispatch_direct` forwards `sms_detail` to `_fan_out` alongside `sms_identifier`; both reach
  `build_sms_body` and both are re-vetted there, so a direct caller can pass a classifier and have
  it behave exactly as on the outbox path.)

## Deferred / roadmap

PR 1 is the foundation; the remaining PRs (see [NOTIFICATIONS_PLAN.md §10](NOTIFICATIONS_PLAN.md)) extend this runbook:

- **PR 2 — Live push**: Redis pub/sub bridge (worker → API), company-aware `send_to_user`, the
  **kiosk WS fence** (reject `scope="kiosk"` tokens at WS connect), Layout `onMessage` → badge/toast.
- **PR 3 — Preferences & settings**: prefs/catalog APIs, My Settings page, admin defaults tab +
  admin-scoped delivery-failure view, digest fixes (30-min cron, WEEKLY, Central-time `digest_time`),
  one-click unsubscribe. **Partially pre-landed by PR 4**: the `/settings` page and
  `GET`/`PUT /users/me/notification-preferences` exist, but the write path owns the **`sms` channel
  only** (the request model `forbid`s extra keys, so a PR-3-shaped payload fails loudly with 422
  rather than silently dropping channels). PR 3 widens the same rows — the persisted JSON already
  keeps the full `{in_app, email, sms, digest}` shape per event, so no migration is needed.
- **PR 4 — SMS**: ✅ **shipped** — `User.phone` + My Settings UI, the Twilio service/job, the
  `allow_sms_egress` admin toggle, and storm caps. Documented above:
  [SMS channel](#sms-channel-twilio) and the terse body rule under
  [Content rules](#content-rules-compliance) (narrowly relaxed 2026-07-29 to allow one vetted
  closed-vocabulary classifier). Still deferred from §3.4: the Twilio inbound
  STOP/opt-out webhook and the delivery-receipt webhook.
- **PR 5 — Comments & mentions**: `comments` / `comment_mentions` / `entity_watchers`, `<CommentsPanel>`,
  the `comment.mention` / `comment.added` events, per-entity-type RBAC (documented in
  [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) when it lands), auto-watch.
- **PR 6 — Event coverage**: instrument the dormant domains (quotes, complaints/RMA, ECO,
  maintenance, delivery exceptions, account lockout, cert-expiry cron) so their catalog rows fire.
