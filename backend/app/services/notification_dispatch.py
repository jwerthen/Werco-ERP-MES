"""Notification dispatch core — the single fan-out path for every channel.

Two entry points funnel into ``_fan_out``:

* ``dispatch_for_event(db, event)`` — the transactional-outbox path. Runs in the ARQ
  ``dispatch_notification_job`` from a committed ``OperationalEvent``; derives
  title/body/link/recipients from the event + catalog. Does NOT commit (the job commits
  the notification rows + the ``notified_at`` marker in one transaction).
* ``dispatch_direct(db, ...)`` — for crons / MRP / scheduling that already resolved the
  triggering entities and recipients in worker context. Commits its own writes.

Compliance (``NOTIFICATIONS_PLAN.md`` §8, ``PR1_DESIGN_SPEC.md`` §C/§D/§K):
* every recipient-resolution source is filtered by the event's ``company_id`` and
  ``User.is_active``;
* every written row (``Notification``, ``NotificationLog``, ``DigestQueue``) stamps
  ``company_id`` from the event — never derived-from-nothing;
* the acting user is never notified of their own action (actor exclusion);
* preferences are resolved in memory with NO row auto-create (§9.8);
* mandatory-channel events force their catalog-named channel on regardless of prefs;
* the SMS leg (§3.4) sends only bodies built by ``sms_content.build_sms_body`` from the
  catalog label + a sanitized record identifier + at most one vetted closed-vocabulary
  classifier — never the caller-composed title/body — and only for opted-in, SMS-eligible
  events to users with a phone on file; the ``allow_sms_egress`` kill switch is enforced
  fail-closed in ``sms_service``.

Content rules (revised 2026-07-29, after CMMC L2 was descoped — the boundary decision of
record is ``docs/NOTIFICATIONS.md`` §11.1; read it before widening either allowlist):
* EMAIL/in-app bodies carry the catalog description plus a detail line composed from the
  ``_DETAIL_KEYS`` payload allowlist (statuses, quantities, day counts, short reasons).
  Composition reads the PAYLOAD only — no DB re-query — so part numbers and customer names
  stay absent. That is a scope/N+1 decision, not a security boundary.
* SMS is relaxed far less, because an SMS renders on a locked screen: one classifier from
  the ``_SMS_DETAIL_KEYS`` FIELD allowlist, vetted again by ``safe_detail`` as a VALUE.
  Two fences, both required. ``reason`` is in the email allowlist and NOT the SMS one.

Runs only in the ARQ worker (a running event loop always exists), so emails/SMS are
enqueued with ``await enqueue_job(...)``; never ``enqueue_job_best_effort`` (which
``asyncio.run``s and would RuntimeError inside the loop).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.queue import enqueue_job, get_redis_pool
from app.models.notification import DigestQueue, Notification, NotificationLog, NotificationPreference
from app.models.user import User
from app.services.notification_catalog import (
    ALL_CHANNELS,
    CHANNEL_DIGEST,
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNEL_SMS,
    CatalogEntry,
    entry_for_event_type,
    get_entry,
    should_fire,
)
from app.services.sms_content import build_sms_body
from app.services.sms_service import SMS_COLLAPSE_DELAY_SECONDS, SMS_HOURLY_CAP_PER_USER, reserve_sms_quota

logger = logging.getLogger(__name__)

# Per-recipient/per-channel Redis dedup window (seconds). Guards retry re-enqueue,
# the enqueue-vs-sweeper race, and multiple emits within one flow. Best-effort: if
# Redis is down we skip dedup (the notified_at marker still bounds duplicates).
_DEDUP_WINDOW_SECONDS = 300

# Delay applied to the SMS send job so the NotificationLog row the dispatcher creates
# for it is committed before the job reads it (see _dispatch_sms).
_SMS_ENQUEUE_DEFER_SECONDS = 2

_IDENTIFIER_KEYS = (
    "work_order_number",
    "ncr_number",
    "receipt_number",
    "po_number",
    "fai_number",
    "car_number",
    "shipment_number",
    "quote_number",
    "equipment_id",
    "blocker_id",
)

# Payload keys whose value may appear as the SMS classifier. This is the FIRST of
# two fences (``sms_content.safe_detail`` is the second): it decides which FIELD is
# eligible, so a field carrying operator-typed text can never be considered at all.
# Every key here must hold a str-backed ENUM value, never free text -- deliberately
# excluding "title", "note", "reason", "scrap_reason", "defect_type" and "step_label",
# all of which are operator-typed and routinely contain customer and part detail.
_SMS_DETAIL_KEYS = (
    "category",
    "planned_type",
    "source",
)

# Payload keys allowed into the EMAIL detail line, with their display labels. Email
# is a mailbox rather than a lock screen, so this is broader than the SMS allowlist
# (quantities and short reasons are permitted -- see the boundary decision recorded
# in docs/NOTIFICATIONS.md section 11.1). It is still an allowlist rather than "dump
# the payload": that keeps a future emit site from silently widening what is mailed.
_DETAIL_KEYS = (
    ("status", "Status"),
    ("old_status", "From"),
    ("new_status", "To"),
    ("quantity_complete", "Qty complete"),
    ("quantity_scrapped", "Qty scrapped"),
    ("quantity_affected", "Qty affected"),
    ("quantity_received", "Qty received"),
    ("quantity_accepted", "Qty accepted"),
    ("quantity_rejected", "Qty rejected"),
    ("old_priority", "Priority was"),
    ("new_priority", "Priority now"),
    ("days_late", "Days late"),
    ("days_until_expiry", "Days to expiry"),
    ("disposition", "Disposition"),
    ("category", "Category"),
    ("source", "Source"),
    ("inspection_method", "Inspection"),
    ("reason", "Reason"),
)

# Detail values are truncated so a long operator-typed reason cannot turn an email
# subject line into a wall of text. Email is far more permissive than SMS, but not
# unbounded.
_EMAIL_DETAIL_VALUE_MAX = 120


# ---------------------------------------------------------------------------
# Content + link builders (identifier + event + allowlisted payload detail, §11.1)
# ---------------------------------------------------------------------------


def _payload_identifier(payload: Dict) -> Optional[str]:
    for key in _IDENTIFIER_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _payload_sms_detail(payload: Dict) -> Optional[str]:
    """First fence for the SMS classifier: pick the first allowlisted ENUM field.

    ``sms_content.safe_detail`` is the second fence and rejects the value if it is
    not a single enum-shaped token. Both are required.
    """
    for key in _SMS_DETAIL_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _payload_detail_line(payload: Dict) -> Optional[str]:
    """Compose the email detail line from the ``_DETAIL_KEYS`` allowlist.

    Returns e.g. ``"Qty complete: 40 | Qty scrapped: 2 | Reason: material"`` or
    ``None`` when the payload carries none of the allowlisted keys.
    """
    parts = []
    for key, label in _DETAIL_KEYS:
        value = payload.get(key)
        if value is None or value == "":
            continue
        text = " ".join(str(value).split())
        if len(text) > _EMAIL_DETAIL_VALUE_MAX:
            text = text[: _EMAIL_DETAIL_VALUE_MAX - 3].rstrip() + "..."
        parts.append(f"{label}: {text}")
    return " | ".join(parts) if parts else None


def _content_for_event(entry: CatalogEntry, event) -> tuple[str, str]:
    """Title/body for an outbox event.

    Title is the catalog label plus the record identifier. Body is the catalog
    description plus a detail line composed from the ``_DETAIL_KEYS`` payload
    allowlist -- quantities, statuses, transitions and short reasons, so the email
    is triageable without logging in (boundary decision of record:
    docs/NOTIFICATIONS.md section 11.1, 2026-07-29).

    Reads the PAYLOAD only. The dispatcher deliberately does not query the database
    to resolve ``part_id`` into a part number, so part numbers and customer names
    stay absent -- that is a scope/N+1 decision, not a security boundary.

    ``entry.description`` stays a static string because it is also served to the
    preferences matrix by ``GET /notifications/catalog``; the composition happens
    here instead of mutating the catalog.
    """
    payload = event.event_payload or {}
    identifier = _payload_identifier(payload)
    title = f"{entry.label}: {identifier}" if identifier else entry.label
    detail = _payload_detail_line(payload)
    body = f"{entry.description}\n\n{detail}" if detail else entry.description
    return title, body


def _link_for_event(event) -> Optional[str]:
    """Best-effort relative SPA route for an outbox event's deep link."""
    if event.work_order_id:
        return f"/work-orders/{event.work_order_id}"
    payload = event.event_payload or {}
    entity_type = (event.entity_type or "").lower()
    entity_id = event.entity_id
    if entity_type == "ncr" and entity_id:
        return f"/quality/ncr/{entity_id}"
    if entity_type == "fai" and entity_id:
        return f"/quality/fai/{entity_id}"
    if entity_type == "shipment" and entity_id:
        return f"/shipping/{entity_id}"
    if entity_type == "po_receipt" and payload.get("po_id"):
        return f"/purchasing/{payload['po_id']}"
    return None


# ---------------------------------------------------------------------------
# Recipient resolution (outbox path)
# ---------------------------------------------------------------------------


def _recipients_for_entry(db: Session, entry: CatalogEntry, event, company_id: int) -> List[User]:
    """Resolve the recipient set for a catalog entry, tenant-scoped & active-filtered.

    (roles ∪ departments ∪ entity-derived resolver) − actor. De-duplicated by user id.
    Every source filters by ``company_id`` and ``User.is_active`` (§8).
    """
    from sqlalchemy import or_

    by_id: Dict[int, User] = {}

    conditions = []
    if entry.roles:
        conditions.append(User.role.in_(list(entry.roles)))
    if entry.departments:
        conditions.append(User.department.in_(list(entry.departments)))
    if conditions:
        for user in (
            db.query(User).filter(User.company_id == company_id, User.is_active.is_(True), or_(*conditions)).all()
        ):
            by_id[user.id] = user

    if entry.resolver is not None:
        for user in entry.resolver(db, event, company_id) or []:
            if user is not None and getattr(user, "is_active", False):
                by_id[user.id] = user

    return list(by_id.values())


# ---------------------------------------------------------------------------
# Preference resolution (in memory, NO row auto-create) + dedup + suppression
# ---------------------------------------------------------------------------


def get_preference_row(db: Session, user_id: int) -> Optional[NotificationPreference]:
    """Fetch a user's saved preference row, or ``None``.

    Read-only by design: it NEVER constructs a row (today's auto-create omits
    ``company_id``, a non-null TenantMixin column — §9.8). A row exists only after an
    explicit save through ``PUT /users/me/notification-preferences``.
    """
    return db.query(NotificationPreference).filter(NotificationPreference.user_id == user_id).first()


def channels_from_pref(pref: Optional[NotificationPreference], entry: CatalogEntry) -> set:
    """Pure resolution of enabled channels from a (possibly absent) preference row.

    Catalog defaults unless the user saved an explicit entry for this event; the
    mandatory channel is always forced on afterwards, so a mandatory-critical event
    can never be fully muted (§8.9).
    """
    channels: set = set(entry.default_channels)
    if pref is not None and isinstance(pref.preferences, dict):
        raw = pref.preferences.get(entry.event_key)
        if isinstance(raw, dict):
            channels = {channel for channel in ALL_CHANNELS if raw.get(channel)}
    if entry.mandatory_channel:
        channels.add(entry.mandatory_channel)
    return channels


def resolve_channels(db: Session, user: User, entry: CatalogEntry) -> set:
    """Enabled channels for this user+event (loads the preference row, then resolves).

    Public because the self-service preferences API renders the SAME resolution the
    dispatcher applies — one source of truth for "what will actually be sent". Callers
    resolving many events for one user should load the row once with
    :func:`get_preference_row` and call :func:`channels_from_pref` per entry."""
    return channels_from_pref(get_preference_row(db, user.id), entry)


def _has_unread(db: Session, *, company_id: int, user_id: int, entry: CatalogEntry, related_type, related_id) -> bool:
    return (
        db.query(Notification.id)
        .filter(
            Notification.company_id == company_id,
            Notification.user_id == user_id,
            Notification.event_key == entry.event_key,
            Notification.related_type == related_type,
            Notification.related_id == related_id,
            Notification.is_read.is_(False),
        )
        .first()
        is not None
    )


async def _dedup_reserve(event_key: str, related_type, related_id, user_id: int, channel: str) -> bool:
    """Atomically reserve the per-recipient/channel dedup key. Returns True when this
    caller won the slot (proceed), False when a recent duplicate already holds it.
    Best-effort: any Redis error returns True (do not suppress on infra failure)."""
    try:
        redis = await get_redis_pool()
        key = f"werco:notify:dedup:{event_key}:{related_type}:{related_id}:{user_id}:{channel}"
        result = await redis.set(key, "1", ex=_DEDUP_WINDOW_SECONDS, nx=True)
        return bool(result)
    except Exception:  # pragma: no cover - dedup is best-effort
        logger.debug("notification dedup check failed (continuing without dedup)", exc_info=True)
        return True


# ---------------------------------------------------------------------------
# Shared fan-out
# ---------------------------------------------------------------------------


async def _fan_out(
    db: Session,
    *,
    entry: CatalogEntry,
    company_id: int,
    actor_user_id: Optional[int],
    candidates: Sequence[User],
    related_type: Optional[str],
    related_id: Optional[int],
    title: str,
    body: Optional[str],
    link: Optional[str],
    template: Optional[str],
    context: Optional[Dict],
    sms_identifier: Optional[str] = None,
    sms_detail: Optional[str] = None,
) -> int:
    """Fan out to every recipient/channel. Adds rows + enqueues emails; does NOT commit.

    Returns the number of in-app rows created (for logging/tests)."""
    severity = entry.severity
    created = 0

    # Actor exclusion + is_active + de-dup by id.
    recipients: Dict[int, User] = {}
    for user in candidates:
        if user is None:
            continue
        if actor_user_id is not None and user.id == actor_user_id:
            continue
        if not getattr(user, "is_active", False):
            continue
        recipients[user.id] = user

    for user in recipients.values():
        channels = resolve_channels(db, user, entry)
        if not channels:
            continue

        # Recurring re-notify suppression (§3.1): while an unread in-app row for the
        # same (event_key, entity, user) exists, suppress the push channels so a
        # standing condition (e.g. a WO late for two weeks) is ONE inbox row + the
        # digest, not 14 emails. The digest channel still accrues.
        # PR-3 FOLLOW-UP: this keys off an unread IN-APP row, so a recipient who (via the
        # PR-3 preferences UI) turns in_app OFF but keeps email ON for a recurring event
        # would never create a suppressing row and would get an email every cron cycle.
        # Unreachable in PR 1 (no preference-write endpoint yet; defaults for the only
        # recurring+email entry, wo.late, include in_app). When PR 3 lands editable prefs,
        # suppression must also consider email/SMS-only recipients (e.g. a per-(user,key,
        # entity) "last notified" marker independent of the in-app row).
        suppress_push = entry.recurring and _has_unread(
            db,
            company_id=company_id,
            user_id=user.id,
            entry=entry,
            related_type=related_type,
            related_id=related_id,
        )

        in_app_id: Optional[int] = None

        if CHANNEL_IN_APP in channels and not suppress_push:
            if await _dedup_reserve(entry.event_key, related_type, related_id, user.id, CHANNEL_IN_APP):
                notification = Notification(
                    company_id=company_id,
                    user_id=user.id,
                    event_key=entry.event_key,
                    severity=severity,
                    title=title,
                    body=body,
                    link=link,
                    related_type=related_type,
                    related_id=related_id,
                )
                db.add(notification)
                db.flush()  # assign id for NotificationLog linkage
                in_app_id = notification.id
                created += 1

        if CHANNEL_EMAIL in channels and not suppress_push and user.email:
            if await _dedup_reserve(entry.event_key, related_type, related_id, user.id, CHANNEL_EMAIL):
                await _enqueue_email(user=user, title=title, body=body, link=link, template=template, context=context)
                # sent=True records the ENQUEUE, not confirmed SMTP delivery. PR-3 FOLLOW-UP:
                # the admin delivery-failure view (PR 3) needs the terminal outcome, so
                # send_email_job should write back sent=False + error on final ARQ-retry
                # exhaustion (thread notification_log_id through the job). Deferred with that
                # consuming view; matches the pre-existing enqueue-time logging behavior.
                db.add(
                    NotificationLog(
                        company_id=company_id,
                        user_id=user.id,
                        event_type=entry.event_key,
                        channel=CHANNEL_EMAIL,
                        subject=title,
                        body=body,
                        sent=True,
                        related_type=related_type,
                        related_id=related_id,
                        notification_id=in_app_id,
                    )
                )

        # SMS leg (§3.4). Fires only when the user opted the SMS channel ON for an
        # SMS-ELIGIBLE event, the recurring-suppression window is clear, AND a phone is
        # on file -- an SMS toggle is inert without a phone. The per-company
        # ``allow_sms_egress`` kill switch is enforced fail-closed inside sms_service,
        # so nothing leaves the boundary even if this leg enqueues.
        if CHANNEL_SMS in channels and entry.sms_eligible and not suppress_push and getattr(user, "phone", None):
            if await _dedup_reserve(entry.event_key, related_type, related_id, user.id, CHANNEL_SMS):
                await _dispatch_sms(
                    db,
                    entry=entry,
                    company_id=company_id,
                    user=user,
                    related_type=related_type,
                    related_id=related_id,
                    title=title,
                    sms_identifier=sms_identifier,
                    sms_detail=sms_detail,
                    in_app_id=in_app_id,
                )

        if CHANNEL_DIGEST in channels:
            if await _dedup_reserve(entry.event_key, related_type, related_id, user.id, CHANNEL_DIGEST):
                db.add(
                    DigestQueue(
                        company_id=company_id,
                        user_id=user.id,
                        event_type=entry.event_key,
                        event_data={
                            "title": title,
                            "body": body,
                            "link": link,
                            "related_type": related_type,
                            "related_id": related_id,
                        },
                        digest_date=date.today(),
                    )
                )

    return created


async def _dispatch_sms(
    db: Session,
    *,
    entry: CatalogEntry,
    company_id: int,
    user: User,
    related_type: Optional[str],
    related_id: Optional[int],
    title: Optional[str],
    sms_identifier: Optional[str],
    sms_detail: Optional[str] = None,
    in_app_id: Optional[int],
) -> None:
    """Storm-check, log, and enqueue one notification SMS.

    The body is built ONLY by ``sms_content.build_sms_body``, from the catalog label, a
    sanitized record identifier, and at most one closed-vocabulary classifier — never from
    the caller-composed ``title``/``body``, which is written freely and may carry names and
    other detail that must not land on a locked phone screen (§3.4 / §11.1). ``title`` is
    stored on the ``NotificationLog`` row below for the in-app/admin delivery view; it is
    not an input to the body.

    The classifier reaches here already field-allowlisted (``_SMS_DETAIL_KEYS``) and is
    vetted again by ``safe_detail`` inside the builder; an unsafe value is dropped and the
    body degrades rather than raising.

    A ``NotificationLog`` row is created here (``company_id`` stamped from the EVENT,
    never from the recipient) and its id handed to the job, which updates that same row
    with the Twilio SID + status. One row per delivery, retries included.

    Storm control runs before the enqueue: over-cap messages are recorded as suppressed
    (visible in the delivery log rather than silently vanishing) and the first overflow
    schedules the deferred collapse message.
    """
    body = build_sms_body(label=entry.label, identifier=sms_identifier, detail=sms_detail)
    decision = await reserve_sms_quota(user.id)

    log = NotificationLog(
        company_id=company_id,
        user_id=user.id,
        event_type=entry.event_key,
        channel=CHANNEL_SMS,
        subject=title,
        body=body,
        sent=False,
        related_type=related_type,
        related_id=related_id,
        notification_id=in_app_id,
    )
    db.add(log)

    if not decision.send:
        log.error = f"suppressed: per-user SMS cap ({SMS_HOURLY_CAP_PER_USER}/hour) reached"
        if decision.arm_collapse:
            await enqueue_job(
                "send_sms_overflow_job",
                company_id=company_id,
                user_id=user.id,
                _defer_by=timedelta(seconds=SMS_COLLAPSE_DELAY_SECONDS),
            )
        return

    db.flush()  # assign the log id so the job can settle THIS row
    await enqueue_job(
        "send_sms_job",
        company_id=company_id,
        user_id=user.id,
        body=body,
        notification_log_id=log.id,
        event_type=entry.event_key,
        # Small defer so the row above is COMMITTED before a (possibly different)
        # worker looks it up. Without it a fast pickup would miss the uncommitted row
        # and insert a second one. Delivery-irrelevant at this scale; the job is
        # correct either way, this just keeps one attempt = one log row.
        _defer_by=timedelta(seconds=_SMS_ENQUEUE_DEFER_SECONDS),
    )


async def _enqueue_email(
    *,
    user: User,
    title: str,
    body: Optional[str],
    link: Optional[str],
    template: Optional[str],
    context: Optional[Dict],
) -> None:
    email_context = dict(context or {})
    email_context.setdefault("base_url", settings.FRONTEND_BASE_URL)
    email_context.setdefault("year", datetime.utcnow().year)
    email_context.setdefault("title", title)
    email_context.setdefault("body", body)
    if link:
        email_context.setdefault("notification_link", f"{settings.FRONTEND_BASE_URL}{link}")
    await enqueue_job(
        "send_email_job",
        to=user.email,
        subject=title,
        body=None,
        template=template or "notification",
        context=email_context,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def dispatch_for_event(db: Session, event) -> int:
    """Outbox path: fan out notifications for one committed OperationalEvent.

    Resolves the catalog entry from ``event.event_type``, applies the transition gate,
    then fans out tenant-scoped to ``event.company_id`` with the actor excluded. Does NOT
    commit — the caller (dispatch_notification_job) commits the rows + the notified_at
    marker atomically."""
    entry = entry_for_event_type(event.event_type)
    if entry is None:
        return 0
    if not should_fire(entry, event):
        return 0

    title, body = _content_for_event(entry, event)
    link = _link_for_event(event)
    related_type = event.entity_type
    related_id = event.entity_id
    candidates = _recipients_for_entry(db, entry, event, event.company_id)

    return await _fan_out(
        db,
        entry=entry,
        company_id=event.company_id,
        actor_user_id=event.user_id,
        candidates=candidates,
        related_type=related_type,
        related_id=related_id,
        title=title,
        body=body,
        link=link,
        template=None,  # outbox uses the generic notification template
        context={"title": title, "body": body},
        # The record number only -- the SMS body is built from this + the catalog
        # label, never from the payload at large (§3.4).
        sms_identifier=_payload_identifier(event.event_payload or {}),
        sms_detail=_payload_sms_detail(event.event_payload or {}),
    )


async def dispatch_direct(
    db: Session,
    *,
    event_key: str,
    company_id: int,
    recipients: Sequence[User],
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
    title: str,
    body: Optional[str] = None,
    link: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    template: Optional[str] = None,
    context: Optional[Dict] = None,
    sms_identifier: Optional[str] = None,
    sms_detail: Optional[str] = None,
    commit: bool = True,
) -> int:
    """Direct path: fan out to an already-resolved recipient set (crons / MRP / scheduling).

    Callers resolve their entities + recipients in worker context and pass them in. Commits
    its own writes unless ``commit=False``.

    ``sms_identifier`` is the record number to put in an SMS body (e.g. ``"WO-1042"``).
    Direct callers compose ``title``/``body`` freely, and that free text must never land on
    a locked phone screen — so an SMS-eligible direct dispatch that omits this simply sends
    the catalog label with no identifier rather than borrowing the title (§3.4). No
    currently-wired direct caller targets an SMS-eligible event.

    ``sms_detail`` mirrors the outbox path's closed-vocabulary classifier and is subject to
    the same two fences: the caller must source it from an enum-valued field, and
    ``sms_content.safe_detail`` vets the value again before it can reach a body."""
    entry = get_entry(event_key)
    if entry is None:  # pragma: no cover - programming error
        logger.error("dispatch_direct called with uncataloged event_key %r", event_key)
        return 0

    created = await _fan_out(
        db,
        entry=entry,
        company_id=company_id,
        actor_user_id=actor_user_id,
        candidates=recipients,
        related_type=related_type,
        related_id=related_id,
        title=title,
        body=body,
        link=link,
        template=template,
        context=context,
        sms_identifier=sms_identifier,
        sms_detail=sms_detail,
    )
    if commit:
        db.commit()
    return created
