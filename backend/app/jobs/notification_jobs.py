import logging
from datetime import datetime, timedelta

from app.db.session import SessionLocal
from app.models.company import Company
from app.services.notification_service import NotificationEvent, NotificationService, get_notification_recipients

logger = logging.getLogger(__name__)


def _active_company_ids(db):
    """Return the ids of every active tenant.

    Notification digests fan out one isolated pass per company so a single cron
    run never surfaces (or emails) one tenant's overdue work, low stock, due
    calibrations or expiring quotes to another tenant (invariant #1).
    """
    return [row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()]


async def check_calibrations_task():
    """Check for calibrations due soon and send notifications (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.calibration import CalibrationStatus, Equipment

        notification_service = NotificationService(db)

        today = datetime.utcnow().date()
        due_7day = today + timedelta(days=7)
        due_1day = today + timedelta(days=1)

        total_7day = 0
        total_1day = 0

        for cid in _active_company_ids(db):
            try:
                # Equipment due in 7 days (tenant-scoped)
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

                # Equipment due in 1 day (tenant-scoped)
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

                # Get this tenant's quality team
                quality_users = get_notification_recipients(db, department="Quality", company_id=cid)

                # Send notifications for 7-day warnings
                for cal in calibrations_7day:
                    await notification_service.send_notification(
                        event_type=NotificationEvent.CALIBRATION_DUE,
                        users=quality_users,
                        subject=f"Calibration Due Soon: {cal.name}",
                        context={
                            "calibration": cal,
                            "days_until_due": (cal.next_calibration_date - today).days,
                        },
                        template="calibration_due",
                        related_type="Equipment",
                        related_id=cal.id,
                    )

                # Send urgent notifications for 1-day warnings
                for cal in calibrations_1day:
                    await notification_service.send_notification(
                        event_type=NotificationEvent.CALIBRATION_DUE,
                        users=quality_users,
                        subject=f"URGENT: Calibration Due Tomorrow: {cal.name}",
                        context={"calibration": cal, "days_until_due": 1, "urgent": True},
                        template="calibration_due",
                        related_type="Equipment",
                        related_id=cal.id,
                    )

                total_7day += len(calibrations_7day)
                total_1day += len(calibrations_1day)
            except Exception as e:
                # One tenant's digest failure must not abort the rest.
                logger.error(f"Calibration digest failed for company {cid}: {e}")

        logger.info(f"Checked calibrations: {total_7day} due in 7 days, {total_1day} due tomorrow")
        return {"calibrations_7day": total_7day, "calibrations_1day": total_1day}

    except Exception as e:
        logger.error(f"Calibration check job failed: {e}")
        raise
    finally:
        db.close()


async def check_late_work_orders_task():
    """Check for late work orders and send notifications (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.work_order import WorkOrder

        notification_service = NotificationService(db)

        today = datetime.utcnow().date()
        total_late = 0

        for cid in _active_company_ids(db):
            try:
                # Late work orders (tenant-scoped, soft-delete aware)
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

                # Get this tenant's supervisors and managers
                supervisors = get_notification_recipients(db, role="supervisor", company_id=cid)
                managers = get_notification_recipients(db, role="manager", company_id=cid)

                for wo in late_wos:
                    days_late = (today - wo.due_date).days
                    is_critical = days_late > 7

                    await notification_service.send_notification(
                        event_type=NotificationEvent.WO_LATE,
                        users=supervisors + managers,
                        subject=(
                            f"{'CRITICAL: ' if is_critical else ''}"
                            f"Work Order {wo.work_order_number} is {days_late} days late"
                        ),
                        context={"work_order": wo, "days_late": days_late, "critical": is_critical},
                        template="work_order_late",
                        related_type="WorkOrder",
                        related_id=wo.id,
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
    """Check for low stock items and send notifications (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.inventory import InventoryItem
        from app.models.part import Part

        notification_service = NotificationService(db)

        total_low_stock = 0

        for cid in _active_company_ids(db):
            try:
                # Inventory below its part's reorder point (tenant-scoped on both
                # tables; the reorder threshold lives on Part).
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

                # Get this tenant's purchasing and inventory users
                purchasing_users = get_notification_recipients(db, department="Purchasing", company_id=cid)
                inventory_users = get_notification_recipients(db, department="Inventory", company_id=cid)

                await notification_service.send_notification(
                    event_type=NotificationEvent.LOW_STOCK,
                    users=purchasing_users + inventory_users,
                    subject=f"Low Stock Alert: {len(low_stock)} items below reorder point",
                    context={"items": low_stock, "count": len(low_stock)},
                    template="low_stock",
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
    """Check for quotes expiring soon (per tenant)."""
    db = SessionLocal()
    try:
        from app.models.quote import Quote

        notification_service = NotificationService(db)

        now = datetime.utcnow()
        expiring_cutoff = now + timedelta(days=7)
        total_expiring = 0

        for cid in _active_company_ids(db):
            try:
                # Quotes expiring within 7 days (tenant-scoped)
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

                # Get this tenant's sales users
                sales_users = get_notification_recipients(db, department="Sales", company_id=cid)

                for quote in expiring_quotes:
                    days_until_expiry = (quote.valid_until - now.date()).days

                    await notification_service.send_notification(
                        event_type=NotificationEvent.QUOTE_EXPIRING,
                        users=sales_users,
                        subject=f"Quote {quote.quote_number} expires in {days_until_expiry} days",
                        context={"quote": quote, "days_until_expiry": days_until_expiry},
                        template="quote_expiring",
                        related_type="Quote",
                        related_id=quote.id,
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
