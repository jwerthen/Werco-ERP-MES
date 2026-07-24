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
* mandatory-channel events force their catalog-named channel on regardless of prefs.

Runs only in the ARQ worker (a running event loop always exists), so emails are enqueued
with ``await enqueue_job(...)``; never ``enqueue_job_best_effort`` (which ``asyncio.run``s
and would RuntimeError inside the loop).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
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

logger = logging.getLogger(__name__)

# Per-recipient/per-channel Redis dedup window (seconds). Guards retry re-enqueue,
# the enqueue-vs-sweeper race, and multiple emits within one flow. Best-effort: if
# Redis is down we skip dedup (the notified_at marker still bounds duplicates).
_DEDUP_WINDOW_SECONDS = 300

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


# ---------------------------------------------------------------------------
# Content + link builders (CUI-safe: identifiers + event only, §11.1)
# ---------------------------------------------------------------------------


def _payload_identifier(payload: Dict) -> Optional[str]:
    for key in _IDENTIFIER_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _content_for_event(entry: CatalogEntry, event) -> tuple[str, str]:
    """Title/body for an outbox event. Record identifier + catalog label only — no
    CUI field detail (no part descriptions, customer names, or quantities)."""
    payload = event.event_payload or {}
    identifier = _payload_identifier(payload)
    title = f"{entry.label}: {identifier}" if identifier else entry.label
    return title, entry.description


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


def _resolve_channels(db: Session, user: User, entry: CatalogEntry) -> set:
    """Enabled channels for this user+event. Catalog defaults unless the user has an
    explicit saved preference row; the mandatory channel is always forced on. NEVER
    constructs a NotificationPreference (today's auto-create omits company_id, §9.8)."""
    channels: set = set(entry.default_channels)
    pref = db.query(NotificationPreference).filter(NotificationPreference.user_id == user.id).first()
    if pref and isinstance(pref.preferences, dict):
        raw = pref.preferences.get(entry.event_key)
        if isinstance(raw, dict):
            channels = {channel for channel in ALL_CHANNELS if raw.get(channel)}
    if entry.mandatory_channel:
        channels.add(entry.mandatory_channel)
    return channels


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
        channels = _resolve_channels(db, user, entry)
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

        # SMS is resolved but a no-op stub in PR 1 (Twilio arrives in PR 4).
        if CHANNEL_SMS in channels and entry.sms_eligible and not suppress_push:
            logger.debug("SMS channel resolved for %s but not sent (PR 4)", entry.event_key)

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
    commit: bool = True,
) -> int:
    """Direct path: fan out to an already-resolved recipient set (crons / MRP / scheduling).

    Callers resolve their entities + recipients in worker context and pass them in. Commits
    its own writes unless ``commit=False``."""
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
    )
    if commit:
        db.commit()
    return created
