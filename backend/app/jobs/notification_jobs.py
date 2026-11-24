import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.core.queue import enqueue_job
from app.db.session import SessionLocal, reset_session_after_failure
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.services import notification_links as links
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.notification_dispatch import dispatch_direct, dispatch_for_event
from app.services.notification_service import get_notification_recipients

logger = logging.getLogger(__name__)

# Relay sweeper bounds (§3.1): re-dispatch catalog-mapped events that never got their
# after_commit enqueue (e.g. a Redis outage). Only events older than the grace window are
# swept so a normally-enqueued event is not double-dispatched by both paths.
_RELAY_GRACE_MINUTES = 2
_RELAY_BATCH_LIMIT = 500
# Lower bound (defense-in-depth): never retroactively dispatch events older than this.
# Migration 072 backfills notified_at on all pre-existing rows so history is already
# excluded; this additionally caps a flood if the worker/Redis is down for a long stretch
# and a post-deploy backlog of undispatched events accumulates -- a notification pending
# for over a day is stale enough to skip rather than surface as a burst of old alerts.
_RELAY_MAX_AGE_HOURS = 24


def _active_company_ids(db):
    """Return the ids of every active tenant.

    Notification digests fan out one isolated pass per company so a single cron
    run never surfaces (or emails) one tenant's overdue work, low stock, due
    calibrations or expiring quotes to another tenant (invariant #1).
    """
    return [row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()]


# ---------------------------------------------------------------------------
# Transactional outbox: dispatch + relay sweeper
# ---------------------------------------------------------------------------


async def dispatch_notification_task(event_id: int) -> dict:
    """Fan out notifications for one committed OperationalEvent (outbox, §3.1).

    Idempotent + crash-safe: returns early if the event was already dispatched
    (``notified_at`` set); otherwise fans out and commits the notification rows + the
    ``notified_at`` marker in ONE transaction, so a crash before commit leaves
    ``notified_at IS NULL`` and the sweeper re-picks it. The per-recipient Redis dedup
    window in ``_fan_out`` bounds duplicates across the enqueue-vs-sweeper race.
    """
    db = SessionLocal()
    try:
        event = db.query(OperationalEvent).filter(OperationalEvent.id == event_id).first()
        if event is None:
            return {"dispatched": False, "reason": "event_not_found"}
        if event.notified_at is not None:
            return {"dispatched": False, "reason": "already_dispatched"}

        created = await dispatch_for_event(db, event)
        event.notified_at = datetime.utcnow()
        db.commit()
        return {"dispatched": True, "in_app_created": created, "event_id": event_id}
    except Exception:
        db.rollback()
        logger.exception("dispatch_notification_task failed for event %s", event_id)
        raise
    finally:
        db.close()


async def relay_pending_notifications_task(limit: int = _RELAY_BATCH_LIMIT) -> dict:
    """Re-enqueue catalog-mapped OperationalEvents that never got dispatched.

    Covers a Redis outage at after_commit-enqueue time. Bounded scan: only cataloged
    event types (uncataloged events never get ``notified_at`` set, so filtering by type
    keeps this from scanning the whole append-only table) with ``notified_at IS NULL``,
    older than the grace window, and NEWER than the max-age floor (so a long outage /
    the pre-072 history backfilled by the migration can never trigger a retroactive burst).
    """
    if not SOURCE_EVENT_TYPE_TO_KEY:
        return {"scanned": 0, "enqueued": 0}

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=_RELAY_GRACE_MINUTES)
        floor = now - timedelta(hours=_RELAY_MAX_AGE_HOURS)
        rows = (
            db.query(OperationalEvent.id)
            .filter(
                OperationalEvent.event_type.in_(list(SOURCE_EVENT_TYPE_TO_KEY.keys())),
                OperationalEvent.notified_at.is_(None),
                OperationalEvent.created_at < cutoff,
                OperationalEvent.created_at >= floor,
            )
            .order_by(OperationalEvent.id.asc())
            .limit(limit)
            .all()
        )
        enqueued = 0
        for (event_id,) in rows:
            try:
                await enqueue_job("dispatch_notification_job", event_id=event_id)
                enqueued += 1
            except Exception:  # pragma: no cover - a single enqueue failure must not abort the sweep
                logger.warning("relay sweeper: failed to re-enqueue event %s", event_id, exc_info=True)
        if enqueued:
            logger.info("relay sweeper re-enqueued %d pending notification events", enqueued)
        return {"scanned": len(rows), "enqueued": enqueued}
    finally:
        db.close()


async def dispatch_notification_direct_task(
    *,
    event_key: str,
    company_id: int,
    recipient_ids: List[int],
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
    title: str,
    body: Optional[str] = None,
    link: Optional[str] = None,
    template: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Worker-side bridge that runs ``dispatch_direct`` for a SYNC request-path caller.

    A sync ``def`` handler cannot ``await dispatch_direct`` (it awaits the Redis pool /
    ``enqueue_job``), so such callers resolve their recipient ids server-side and enqueue
    THIS job via ``enqueue_job_best_effort``. The worker (a running loop) loads the
    recipients tenant-scoped + active-filtered and dispatches in the correct async
    context. Used by the visitor check-in host notification (§9.6 defect #6).
    """
    if not recipient_ids:
        return {"dispatched": 0, "reason": "no_recipients"}

    db = SessionLocal()
    try:
        from app.models.user import User

        users = (
            db.query(User)
            .filter(User.id.in_(recipient_ids), User.company_id == company_id, User.is_active.is_(True))
            .all()
        )
        if not users:
            return {"dispatched": 0, "reason": "no_active_recipients"}

        created = await dispatch_direct(
            db,
            event_key=event_key,
            company_id=company_id,
            recipients=users,
            related_type=related_type,
            related_id=related_id,
            title=title,
            body=body,
            link=link,
            template=template,
            context=context,
        )
        return {"dispatched": created}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Recurring detector crons (repointed onto the new dispatcher, §E)
# ---------------------------------------------------------------------------


CALIBRATION_DUE_EVENT = "calibration.due"
CALIBRATION_OVERDUE_EVENT = "calibration.overdue"
CALIBRATION_DUE_WINDOW_DAYS = 7


def calibration_alert_recipients(db, company_id: int) -> list:
    """Who hears about due and overdue gauges: the catalog entry's audience -- the Quality ROLE
    plus the Quality department -- not only users whose free-text Department reads "Quality"."""
    from app.services.notification_dispatch import catalog_audience_for

    return catalog_audience_for(db, CALIBRATION_DUE_EVENT, company_id)


async def check_calibrations_task():
    """Alert Quality about gauges coming due and gauges already overdue, per tenant, on the
    company's own calendar day (``company_today``).

    In service means ``is_active`` and a status other than out-of-service / retired (both are
    deliberate states a person set; the stored ACTIVE / DUE / OVERDUE status is a date-derived
    label that may be stale, so the date decides). Per gauge, at most one alert a run:

    * ``calibration.overdue`` -- next calibration date before today. Recurring: one unread inbox
      row per person while it stays overdue, re-raised once read.
    * ``calibration.due`` -- due today or within the next 7 days; "URGENT" when due today or
      tomorrow. A gauge due today used to fall through both passes (each required a date after
      today), and one overdue gauge was never mentioned again.
    """
    db = SessionLocal()
    try:
        from app.models.calibration import CalibrationStatus, Equipment
        from app.services.company_time import company_today

        in_service = (CalibrationStatus.ACTIVE, CalibrationStatus.DUE, CalibrationStatus.OVERDUE)
        totals = {"calibrations_due": 0, "calibrations_overdue": 0}

        for cid in _active_company_ids(db):
            try:
                today = company_today(db, cid)
                gauges = (
                    db.query(Equipment)
                    .filter(
                        Equipment.company_id == cid,
                        Equipment.is_active.is_(True),
                        Equipment.status.in_(in_service),
                        Equipment.next_calibration_date.isnot(None),
                        Equipment.next_calibration_date <= today + timedelta(days=CALIBRATION_DUE_WINDOW_DAYS),
                    )
                    .order_by(Equipment.next_calibration_date, Equipment.id)
                    .all()
                )
                if not gauges:
                    continue

                # Dispatched even to an empty audience: dispatch_direct also reaches any people an
                # Admin added under Email Recipients for the event.
                recipients = calibration_alert_recipients(db, cid)

                for cal in gauges:
                    days = (cal.next_calibration_date - today).days
                    if days < 0:
                        await dispatch_direct(
                            db,
                            event_key=CALIBRATION_OVERDUE_EVENT,
                            company_id=cid,
                            recipients=recipients,
                            related_type="Equipment",
                            related_id=cal.id,
                            title=f"OVERDUE: Calibration of {cal.name} is {_days_word(-days)} overdue",
                            body=(
                                f"Was due {_alert_date(cal.next_calibration_date)}. Take the gauge out of use "
                                "until it is calibrated."
                            ),
                            link=links.CALIBRATION_LIST,
                            template="calibration_due",
                            context={
                                "equipment_name": cal.name,
                                "days_overdue": -days,
                                "overdue": True,
                                "urgent": True,
                                "link_path": links.CALIBRATION_LIST,
                            },
                        )
                        totals["calibrations_overdue"] += 1
                        continue

                    urgent = days <= 1
                    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {_days_word(days)}"
                    await dispatch_direct(
                        db,
                        event_key=CALIBRATION_DUE_EVENT,
                        company_id=cid,
                        recipients=recipients,
                        related_type="Equipment",
                        related_id=cal.id,
                        title=f"{'URGENT: ' if urgent else ''}Calibration due {when}: {cal.name}",
                        body=f"Calibration is due {_alert_date(cal.next_calibration_date)}.",
                        link=links.CALIBRATION_LIST,
                        template="calibration_due",
                        context={
                            "equipment_name": cal.name,
                            "days_until_due": days,
                            "urgent": urgent,
                            "link_path": links.CALIBRATION_LIST,
                        },
                    )
                    totals["calibrations_due"] += 1
            except Exception as e:
                logger.error(f"Calibration alerts failed for company {cid}: {e}")
                # Reset the shared session, or every later tenant inherits this one's failed transaction.
                reset_session_after_failure(db, f"the calibration alerts for company {cid}")

        logger.info(
            f"Checked calibrations: {totals['calibrations_overdue']} overdue, {totals['calibrations_due']} due "
            f"within {CALIBRATION_DUE_WINDOW_DAYS} days"
        )
        return totals

    except Exception as e:
        logger.error(f"Calibration check job failed: {e}")
        raise
    finally:
        db.close()


LATE_SUMMARY_EVENT = "wo.late"
LATE_SUMMARY_RELATED_TYPE = "LateWorkOrderSummary"
LATE_SUMMARY_LIST_LIMIT = 10
LATE_CRITICAL_DAYS = 7


def late_summary_related_id(day) -> int:
    """The summary's dedupe key: the company-local day as ``YYYYMMDD``. Together with
    ``LATE_SUMMARY_RELATED_TYPE`` and the recipient it makes one summary per person per day."""
    return day.year * 10000 + day.month * 100 + day.day


def _late_summary_already_sent(db, company_id: int, related_id: int) -> bool:
    """A summary for this company day already exists (in-app row or email log): a second run of
    the job the same day sends nothing."""
    from app.models.notification import Notification, NotificationLog

    for model, key_column in ((Notification, Notification.event_key), (NotificationLog, NotificationLog.event_type)):
        hit = (
            db.query(model.id)
            .filter(
                model.company_id == company_id,
                key_column == LATE_SUMMARY_EVENT,
                model.related_type == LATE_SUMMARY_RELATED_TYPE,
                model.related_id == related_id,
            )
            .first()
        )
        if hit is not None:
            return True
    return False


async def check_late_work_orders_task():
    """One daily late-jobs summary per Supervisor and Manager, per tenant (plan alerts_health/2 step 4).

    Late means past the PROMISE date -- ``must_ship_by`` when set, else ``due_date`` -- against the
    company's own day (``company_today``), for a released, in-progress or held work order that is
    not deleted. Replaces the old per-job dispatch (one in-app row and one email per late job to
    every supervisor and manager, judged on the due date and the UTC day).

    The summary carries the total, the ten most overdue jobs (work order number and days late --
    no part numbers or customer names, docs/NOTIFICATIONS.md section 11.1) and a link to the Work
    Orders list's late view (``scope=late``, the same predicate). Recipients are the ``wo.late`` catalog audience (the Supervisor and
    Manager roles) plus anyone an Admin selected under Email Recipients. Dedupe key: the company
    day per recipient; a second run the same day sends nothing."""
    db = SessionLocal()
    try:
        from app.models.work_order import WorkOrder
        from app.services.company_time import company_today
        from app.services.notification_dispatch import catalog_audience_for
        from app.services.work_order_browse_service import late_promise_date, late_work_order_clause

        total_late = 0
        summaries = 0

        for cid in _active_company_ids(db):
            try:
                today = company_today(db, cid)
                promise = late_promise_date()
                late = db.query(WorkOrder).filter(
                    WorkOrder.company_id == cid,
                    WorkOrder.is_deleted == False,  # noqa: E712
                    late_work_order_clause(today),
                )
                count = late.count()
                if not count:
                    continue
                total_late += count

                related_id = late_summary_related_id(today)
                if _late_summary_already_sent(db, cid, related_id):
                    continue

                worst = late.order_by(promise.asc(), WorkOrder.work_order_number.asc()).limit(LATE_SUMMARY_LIST_LIMIT)
                jobs = []
                for wo in worst.all():
                    promised = wo.must_ship_by or wo.due_date
                    days_late = (today - promised).days
                    jobs.append(
                        {
                            "work_order_number": wo.work_order_number,
                            "days_late": days_late,
                            "critical": days_late > LATE_CRITICAL_DAYS,
                        }
                    )
                more = count - len(jobs)
                noun = "work order is" if count == 1 else "work orders are"
                lines = [f"{count} {noun} past the promise date (ship-by date, else due date)."]
                lines += [f"{job['work_order_number']}: {_days_word(job['days_late'])} late" for job in jobs]
                if more > 0:
                    lines.append(f"And {more} more.")

                await dispatch_direct(
                    db,
                    event_key=LATE_SUMMARY_EVENT,
                    company_id=cid,
                    recipients=catalog_audience_for(db, LATE_SUMMARY_EVENT, cid),
                    related_type=LATE_SUMMARY_RELATED_TYPE,
                    related_id=related_id,
                    title=f"Late work orders: {count}",
                    body="\n".join(lines),
                    link=links.WORK_ORDERS_LATE,
                    template="work_order_late",
                    context={
                        "total": count,
                        "jobs": jobs,
                        "more": more,
                        "critical": any(job["critical"] for job in jobs),
                        "link_path": links.WORK_ORDERS_LATE,
                    },
                )
                summaries += 1
            except Exception as e:
                logger.error(f"Late work order summary failed for company {cid}: {e}")
                # Reset the shared session, or every later tenant inherits this one's failed transaction.
                reset_session_after_failure(db, f"the late work order summary for company {cid}")

        logger.info(f"Checked late work orders: {total_late} late, {summaries} summary dispatch(es)")
        return {"late_work_orders": total_late, "summaries": summaries}

    except Exception as e:
        logger.error(f"Late work order check job failed: {e}")
        raise
    finally:
        db.close()


async def check_low_stock_task():
    """Tell Purchasing and Inventory how many parts are low, once a day per tenant.

    The count is the one low-stock rule (``low_stock_service``): parts, not stock rows, at or
    below a reorder point above zero, company-owned stock only -- so it matches the Inventory
    page's low-stock list, which the alert links to. Recipients are the ``stock.low`` catalog
    audience. The body names no part (docs/NOTIFICATIONS.md section 11.1): a count, how many are
    out of stock, and how many are at or below safety stock."""
    db = SessionLocal()
    try:
        from app.services.low_stock_service import low_stock_parts
        from app.services.notification_dispatch import catalog_audience_for

        total_low_stock = 0

        for cid in _active_company_ids(db):
            try:
                low = low_stock_parts(db, cid)
                if not low:
                    continue

                recipients = catalog_audience_for(db, "stock.low", cid)

                count = len(low)
                out = sum(1 for row in low if row.out_of_stock)
                critical = sum(1 for row in low if row.is_critical)
                noun = "part is" if count == 1 else "parts are"
                body = f"{count} {noun} at or below the reorder point"
                if out:
                    body += f"; {out} out of stock"
                if critical:
                    body += f"; {critical} at or below safety stock"
                await dispatch_direct(
                    db,
                    event_key="stock.low",
                    company_id=cid,
                    recipients=recipients,
                    related_type=None,
                    related_id=None,
                    title=f"Low stock: {count} {'part' if count == 1 else 'parts'} at or below reorder point",
                    body=body + ".",
                    link=links.INVENTORY_LOW_STOCK,
                    template="low_stock",
                    context={
                        "count": count,
                        "out_of_stock": out,
                        "critical": critical,
                        "link_path": links.INVENTORY_LOW_STOCK,
                    },
                )

                total_low_stock += count
            except Exception as e:
                logger.error(f"Low stock digest failed for company {cid}: {e}")
                # Reset the shared session, or every later tenant inherits this one's failed transaction.
                reset_session_after_failure(db, f"the low stock digest for company {cid}")

        logger.info(f"Low stock check: {total_low_stock} parts")
        return {"low_stock_items": total_low_stock}

    except Exception as e:
        logger.error(f"Low stock check job failed: {e}")
        raise
    finally:
        db.close()


async def check_quote_expiring_task():
    """Check for quotes expiring soon and notify Sales (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.quote import Quote

        now = datetime.utcnow()
        expiring_cutoff = now + timedelta(days=7)
        total_expiring = 0

        for cid in _active_company_ids(db):
            try:
                expiring_quotes = (
                    db.query(Quote)
                    .filter(
                        Quote.company_id == cid,
                        Quote.valid_until <= expiring_cutoff,
                        Quote.valid_until > now,
                        Quote.status == "SENT",
                    )
                    .all()
                )

                if not expiring_quotes:
                    continue

                sales_users = get_notification_recipients(db, department="Sales", company_id=cid)

                for quote in expiring_quotes:
                    days_until_expiry = (quote.valid_until - now.date()).days
                    await dispatch_direct(
                        db,
                        event_key="quote.expiring",
                        company_id=cid,
                        recipients=sales_users,
                        related_type="Quote",
                        related_id=quote.id,
                        title=f"Quote {quote.quote_number} expires in {days_until_expiry} days",
                        body="This quote is about to expire.",
                        link=links.quote(quote.id),
                        template="quote_expiring",
                        context={
                            "quote_number": quote.quote_number,
                            "days_until_expiry": days_until_expiry,
                            "link_path": links.quote(quote.id),
                        },
                    )

                total_expiring += len(expiring_quotes)
            except Exception as e:
                logger.error(f"Quote expiry digest failed for company {cid}: {e}")
                # Reset the shared session, or every later tenant inherits this one's failed transaction.
                reset_session_after_failure(db, f"the quote expiry digest for company {cid}")

        logger.info(f"Quote expiry check: {total_expiring} expiring soon")
        return {"expiring_quotes": total_expiring}

    except Exception as e:
        logger.error(f"Quote expiry check job failed: {e}")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Preventive maintenance: due (T-7) and overdue alerts (owner decision 2026-11-20)
# ---------------------------------------------------------------------------

MAINTENANCE_DUE_EVENT = "maintenance.due"
CHANGE_CLOCK_TOLERANCE = timedelta(minutes=1)
MAINTENANCE_OVERDUE_EVENT = "maintenance.overdue"


def maintenance_alert_recipients(db, company_id: int) -> list:
    """Who hears about due and overdue PM: the company's active users in the catalog entry's departments
    (Maintenance) plus its active users in the entry's roles (Admin) -- owner decision 2026-11-20, held in
    ONE place, ``notification_catalog`` -- each once."""
    from app.services.notification_dispatch import catalog_audience_for

    return catalog_audience_for(db, MAINTENANCE_DUE_EVENT, company_id)


def _alert_date(value) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _days_word(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _due_phrase(due_date, today) -> str:
    days = (due_date - today).days
    if days == 0:
        return "due today"
    return f"due in {_days_word(days)}"


def _target_code(row, company_id: int):
    """The record's machine or asset code -- only when it belongs to the same company (a legacy row
    may point at another tenant's machine, whose code must not be read out)."""
    for related in (row.asset, row.work_center):
        if related is not None and related.company_id == company_id:
            return getattr(related, "asset_tag", None) or getattr(related, "code", None)
    return None


def _as_utc(value):
    """A naive-UTC column value (``updated_at``) as an aware UTC instant; ``None`` stays ``None``."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _due_alert_already_sent(db, company_id: int, refs) -> bool:
    """Whether a ``maintenance.due`` alert already went out for this cycle of a PM item.

    ``refs`` are ``(related_type, related_id, since)``: an alert on that record on or after ``since``
    counts. ``since`` is the later of the cycle's start (the company-local start of the day the item
    came within the window) and the record's last change (``updated_at``) -- so a completion that rolls
    a schedule forward, or an edit that moves a due date, starts a new cycle even inside the window,
    and an alert from an earlier cycle never stands in for this one. Both records count: the in-app
    row and the email / SMS log row."""
    from sqlalchemy import and_, or_

    from app.models.notification import Notification, NotificationLog

    if not refs:
        return False
    in_app = (
        db.query(Notification.id)
        .filter(
            Notification.company_id == company_id,
            Notification.event_key == MAINTENANCE_DUE_EVENT,
            or_(
                *(
                    and_(
                        Notification.related_type == related_type,
                        Notification.related_id == related_id,
                        Notification.created_at >= since,
                    )
                    for related_type, related_id, since in refs
                )
            ),
        )
        .first()
    )
    if in_app is not None:
        return True
    delivered = (
        db.query(NotificationLog.id)
        .filter(
            NotificationLog.company_id == company_id,
            NotificationLog.event_type == MAINTENANCE_DUE_EVENT,
            or_(
                *(
                    and_(
                        NotificationLog.related_type == related_type,
                        NotificationLog.related_id == related_id,
                        NotificationLog.sent_at >= since,
                    )
                    for related_type, related_id, since in refs
                )
            ),
        )
        .first()
    )
    return delivered is not None


async def check_maintenance_due_task():
    """Alert the Maintenance department and the Admins about due and overdue preventive maintenance,
    per tenant, on the company's own calendar day (owner decision 2026-11-20).

    * ``maintenance.overdue`` -- every PM schedule and maintenance work order past its due date (what
      the dashboard's overdue tiles count). Recurring: a recipient with an unread alert for the item
      is not alerted again until they read it, so a standing overdue PM is one inbox row, not one a
      day.
    * ``maintenance.due`` -- each item once per due cycle, when it first comes within 7 days of its
      due date (T-7; the dashboard's due-this-week tiles). An item that entered the window while the
      job was not running is alerted on the next run. A completion that rolls a schedule forward, or
      an edit that moves a due date, starts a new cycle; a work order opened from a schedule already
      announced this cycle is not announced again.

    In-app only: neither catalog entry is email- or SMS-eligible today.

    What counts comes from ``app.services.maintenance_due_service`` -- the dashboard's own rule -- so
    a tile and an alert never disagree. Links land on the Maintenance page's Schedules or Work Orders
    tab. Fires only when ``check_maintenance_due_job`` is enabled in the worker
    (``WORKER_CRON_JOBS``). A recurring overdue alert is keyed on the schedule, so a recipient who
    never read last cycle's overdue alert for a schedule is not re-alerted for the next cycle's until
    they read it (the catalog's recurring rule)."""
    db = SessionLocal()
    try:
        from app.services.company_time import company_today, company_window
        from app.services.maintenance_due_service import DUE_WINDOW_DAYS, maintenance_due

        totals = {"due": 0, "overdue": 0, "companies": 0}
        for cid in _active_company_ids(db):
            try:
                today = company_today(db, cid)
                due = maintenance_due(db, cid, today, window_days=DUE_WINDOW_DAYS)
                if due.empty:
                    continue
                recipients = maintenance_alert_recipients(db, cid)
                if not recipients:
                    continue
                sent_before = totals["due"] + totals["overdue"]

                def cycle_start(due_date):
                    start, _ = company_window(db, cid, due_date - timedelta(days=DUE_WINDOW_DAYS), due_date)
                    # Aware UTC: ``created_at`` / ``sent_at`` are timestamptz, and a naive parameter
                    # would be read in the database session's time zone.
                    return start.replace(tzinfo=timezone.utc)

                for schedule in due.schedules_overdue:
                    days = (today - schedule.next_due_date).days
                    code = _target_code(schedule, cid)
                    await dispatch_direct(
                        db,
                        event_key=MAINTENANCE_OVERDUE_EVENT,
                        company_id=cid,
                        recipients=recipients,
                        related_type="MaintenanceSchedule",
                        related_id=schedule.id,
                        title=f"PM overdue: {schedule.name}",
                        body=(
                            f"Was due {_alert_date(schedule.next_due_date)} ({_days_word(days)} ago)"
                            + (f" on {code}" if code else "")
                            + ". No work order is under way for it."
                        ),
                        link=links.MAINTENANCE_SCHEDULES,
                    )
                    totals["overdue"] += 1
                for wo in due.work_orders_overdue:
                    days = (today - wo.due_date).days
                    await dispatch_direct(
                        db,
                        event_key=MAINTENANCE_OVERDUE_EVENT,
                        company_id=cid,
                        recipients=recipients,
                        related_type="MaintenanceWorkOrder",
                        related_id=wo.id,
                        title=f"Maintenance work order overdue: {wo.wo_number}",
                        body=f"{wo.title}: was due {_alert_date(wo.due_date)} ({_days_word(days)} ago) and is not started.",
                        link=links.MAINTENANCE_WORK_ORDERS,
                    )
                    totals["overdue"] += 1

                def since(due_date, record):
                    start = cycle_start(due_date)
                    changed = _as_utc(record.updated_at)
                    if changed is None:
                        return start
                    # ``updated_at`` is the app's clock, ``created_at`` / ``sent_at`` the database's
                    # (second precision on SQLite): a minute's tolerance keeps an alert written right
                    # after a change from reading as older than it.
                    return max(start, changed - CHANGE_CLOCK_TOLERANCE)

                for schedule in due.schedules_due:
                    if _due_alert_already_sent(
                        db, cid, [("MaintenanceSchedule", schedule.id, since(schedule.next_due_date, schedule))]
                    ):
                        continue
                    code = _target_code(schedule, cid)
                    await dispatch_direct(
                        db,
                        event_key=MAINTENANCE_DUE_EVENT,
                        company_id=cid,
                        recipients=recipients,
                        related_type="MaintenanceSchedule",
                        related_id=schedule.id,
                        title=f"PM {_due_phrase(schedule.next_due_date, today)}: {schedule.name}",
                        body=f"Due {_alert_date(schedule.next_due_date)}" + (f" on {code}" if code else "") + ".",
                        link=links.MAINTENANCE_SCHEDULES,
                    )
                    totals["due"] += 1
                for wo in due.work_orders_due:
                    refs = [("MaintenanceWorkOrder", wo.id, since(wo.due_date, wo))]
                    schedule = wo.schedule if wo.schedule_id is not None else None
                    if schedule is not None and schedule.company_id == cid:
                        # Opened from a schedule already announced this cycle (Start PM copies the
                        # schedule's due date): the PM was announced; don't announce it twice.
                        refs.append(("MaintenanceSchedule", schedule.id, since(wo.due_date, schedule)))
                    if _due_alert_already_sent(db, cid, refs):
                        continue
                    await dispatch_direct(
                        db,
                        event_key=MAINTENANCE_DUE_EVENT,
                        company_id=cid,
                        recipients=recipients,
                        related_type="MaintenanceWorkOrder",
                        related_id=wo.id,
                        title=f"Maintenance work order {_due_phrase(wo.due_date, today)}: {wo.wo_number}",
                        body=f"{wo.title}: due {_alert_date(wo.due_date)}.",
                        link=links.MAINTENANCE_WORK_ORDERS,
                    )
                    totals["due"] += 1
                if totals["due"] + totals["overdue"] > sent_before:
                    totals["companies"] += 1
            except Exception as e:
                logger.error(f"Maintenance due/overdue alerts failed for company {cid}: {e}")
                # Reset the shared session, or every later tenant inherits this one's failed transaction.
                reset_session_after_failure(db, f"the maintenance due/overdue alerts for company {cid}")

        logger.info(
            f"Checked preventive maintenance: {totals['overdue']} overdue and {totals['due']} due alert(s) "
            f"across {totals['companies']} company(ies)"
        )
        return totals
    except Exception as e:
        logger.error(f"Maintenance due check job failed: {e}")
        raise
    finally:
        db.close()
