import logging
from datetime import datetime, timedelta
from typing import List, Optional

from app.core.queue import enqueue_job
from app.db.session import SessionLocal
from app.models.company import Company
from app.models.operational_event import OperationalEvent
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


async def check_calibrations_task():
    """Check for calibrations due soon and notify Quality (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.calibration import CalibrationStatus, Equipment

        today = datetime.utcnow().date()
        due_7day = today + timedelta(days=7)
        due_1day = today + timedelta(days=1)

        total_7day = 0
        total_1day = 0

        for cid in _active_company_ids(db):
            try:
                calibrations_7day = (
                    db.query(Equipment)
                    .filter(
                        Equipment.company_id == cid,
                        Equipment.next_calibration_date <= due_7day,
                        Equipment.next_calibration_date > today,
                        Equipment.status == CalibrationStatus.ACTIVE,
                    )
                    .all()
                )
                calibrations_1day = (
                    db.query(Equipment)
                    .filter(
                        Equipment.company_id == cid,
                        Equipment.next_calibration_date <= due_1day,
                        Equipment.next_calibration_date > today,
                        Equipment.status == CalibrationStatus.ACTIVE,
                    )
                    .all()
                )

                if not calibrations_7day and not calibrations_1day:
                    continue

                quality_users = get_notification_recipients(db, department="Quality", company_id=cid)

                for cal in calibrations_7day:
                    days = (cal.next_calibration_date - today).days
                    await dispatch_direct(
                        db,
                        event_key="calibration.due",
                        company_id=cid,
                        recipients=quality_users,
                        related_type="Equipment",
                        related_id=cal.id,
                        title=f"Calibration due soon: {cal.name}",
                        body=f"Calibration is due in {days} day(s).",
                        link=f"/calibration/{cal.id}",
                        template="calibration_due",
                        context={
                            "equipment_name": cal.name,
                            "days_until_due": days,
                            "link_path": f"/calibration/{cal.id}",
                        },
                    )

                for cal in calibrations_1day:
                    await dispatch_direct(
                        db,
                        event_key="calibration.due",
                        company_id=cid,
                        recipients=quality_users,
                        related_type="Equipment",
                        related_id=cal.id,
                        title=f"URGENT: Calibration due tomorrow: {cal.name}",
                        body="Calibration is due within 1 day.",
                        link=f"/calibration/{cal.id}",
                        template="calibration_due",
                        context={
                            "equipment_name": cal.name,
                            "days_until_due": 1,
                            "urgent": True,
                            "link_path": f"/calibration/{cal.id}",
                        },
                    )

                total_7day += len(calibrations_7day)
                total_1day += len(calibrations_1day)
            except Exception as e:
                logger.error(f"Calibration digest failed for company {cid}: {e}")

        logger.info(f"Checked calibrations: {total_7day} due in 7 days, {total_1day} due tomorrow")
        return {"calibrations_7day": total_7day, "calibrations_1day": total_1day}

    except Exception as e:
        logger.error(f"Calibration check job failed: {e}")
        raise
    finally:
        db.close()


async def check_late_work_orders_task():
    """Check for late work orders and notify supervisors/managers (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.work_order import WorkOrder

        today = datetime.utcnow().date()
        total_late = 0

        for cid in _active_company_ids(db):
            try:
                late_wos = (
                    db.query(WorkOrder)
                    .filter(
                        WorkOrder.company_id == cid,
                        WorkOrder.is_deleted == False,
                        WorkOrder.due_date < today,
                        WorkOrder.status.in_(["RELEASED", "IN_PROGRESS"]),
                    )
                    .all()
                )

                if not late_wos:
                    continue

                supervisors = get_notification_recipients(db, role="supervisor", company_id=cid)
                managers = get_notification_recipients(db, role="manager", company_id=cid)
                recipients = supervisors + managers

                for wo in late_wos:
                    days_late = (today - wo.due_date).days
                    is_critical = days_late > 7
                    await dispatch_direct(
                        db,
                        event_key="wo.late",
                        company_id=cid,
                        recipients=recipients,
                        related_type="WorkOrder",
                        related_id=wo.id,
                        title=(
                            f"{'CRITICAL: ' if is_critical else ''}"
                            f"Work Order {wo.work_order_number} is {days_late} days late"
                        ),
                        body="This work order is past its due date.",
                        link=f"/work-orders/{wo.id}",
                        template="work_order_late",
                        context={
                            "work_order_number": wo.work_order_number,
                            "days_late": days_late,
                            "critical": is_critical,
                            "link_path": f"/work-orders/{wo.id}",
                        },
                    )

                total_late += len(late_wos)
            except Exception as e:
                logger.error(f"Late work order digest failed for company {cid}: {e}")

        logger.info(f"Checked late work orders: {total_late} late")
        return {"late_work_orders": total_late}

    except Exception as e:
        logger.error(f"Late work order check job failed: {e}")
        raise
    finally:
        db.close()


async def check_low_stock_task():
    """Check for low stock items and notify Purchasing/Inventory (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.inventory import InventoryItem
        from app.models.part import Part

        total_low_stock = 0

        for cid in _active_company_ids(db):
            try:
                low_stock = (
                    db.query(InventoryItem)
                    .join(Part, InventoryItem.part_id == Part.id)
                    .filter(
                        InventoryItem.company_id == cid,
                        Part.company_id == cid,
                        Part.is_deleted == False,
                        InventoryItem.quantity_on_hand <= Part.reorder_point,
                    )
                    .all()
                )

                if not low_stock:
                    continue

                purchasing_users = get_notification_recipients(db, department="Purchasing", company_id=cid)
                inventory_users = get_notification_recipients(db, department="Inventory", company_id=cid)
                recipients = purchasing_users + inventory_users

                await dispatch_direct(
                    db,
                    event_key="stock.low",
                    company_id=cid,
                    recipients=recipients,
                    related_type=None,
                    related_id=None,
                    title=f"Low Stock Alert: {len(low_stock)} items below reorder point",
                    body=f"{len(low_stock)} item(s) are at or below their reorder point.",
                    link="/inventory",
                    template="low_stock",
                    context={"count": len(low_stock), "link_path": "/inventory"},
                )

                total_low_stock += len(low_stock)
            except Exception as e:
                logger.error(f"Low stock digest failed for company {cid}: {e}")

        logger.info(f"Low stock check: {total_low_stock} items")
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
                        link=f"/quotes/{quote.id}",
                        template="quote_expiring",
                        context={
                            "quote_number": quote.quote_number,
                            "days_until_expiry": days_until_expiry,
                            "link_path": f"/quotes/{quote.id}",
                        },
                    )

                total_expiring += len(expiring_quotes)
            except Exception as e:
                logger.error(f"Quote expiry digest failed for company {cid}: {e}")

        logger.info(f"Quote expiry check: {total_expiring} expiring soon")
        return {"expiring_quotes": total_expiring}

    except Exception as e:
        logger.error(f"Quote expiry check job failed: {e}")
        raise
    finally:
        db.close()
