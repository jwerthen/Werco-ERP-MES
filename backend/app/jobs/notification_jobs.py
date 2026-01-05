from app.db.session import SessionLocal
from app.services.notification_service import NotificationService, NotificationEvent, get_notification_recipients
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


async def check_calibrations_task():
    """Check for calibrations due soon and send notifications"""
    db = SessionLocal()
    try:
        from app.models.calibration import Calibration

        notification_service = NotificationService(db)

        # Check calibrations due in 7 days
        due_soon = datetime.utcnow() + timedelta(days=7)
        calibrations_7day = db.query(Calibration).filter(
            Calibration.next_calibration_date <= due_soon,
            Calibration.next_calibration_date > datetime.utcnow(),
            Calibration.status == "ACTIVE"
        ).all()

        # Check calibrations due in 1 day
        due_tomorrow = datetime.utcnow() + timedelta(days=1)
        calibrations_1day = db.query(Calibration).filter(
            Calibration.next_calibration_date <= due_tomorrow,
            Calibration.next_calibration_date > datetime.utcnow(),
            Calibration.status == "ACTIVE"
        ).all()

        # Get quality team
        quality_users = get_notification_recipients(db, department="Quality")

        # Send notifications for 7-day warnings
        for cal in calibrations_7day:
            await notification_service.send_notification(
                event_type=NotificationEvent.CALIBRATION_DUE,
                users=quality_users,
                subject=f"Calibration Due Soon: {cal.equipment_name}",
                context={
                    "calibration": cal,
                    "days_until_due": (cal.next_calibration_date - datetime.utcnow().date()).days
                },
                template="calibration_due",
                related_type="Calibration",
                related_id=cal.id
            )

        # Send urgent notifications for 1-day warnings
        for cal in calibrations_1day:
            await notification_service.send_notification(
                event_type=NotificationEvent.CALIBRATION_DUE,
                users=quality_users,
                subject=f"URGENT: Calibration Due Tomorrow: {cal.equipment_name}",
                context={
                    "calibration": cal,
                    "days_until_due": 1,
                    "urgent": True
                },
                template="calibration_due",
                related_type="Calibration",
                related_id=cal.id
            )

        logger.info(f"Checked calibrations: {len(calibrations_7day)} due in 7 days, {len(calibrations_1day)} due tomorrow")
        return {"calibrations_7day": len(calibrations_7day), "calibrations_1day": len(calibrations_1day)}

    except Exception as e:
        logger.error(f"Calibration check job failed: {e}")
        raise
    finally:
        db.close()


async def check_late_work_orders_task():
    """Check for late work orders and send notifications"""
    db = SessionLocal()
    try:
        from app.models.work_order import WorkOrder

        notification_service = NotificationService(db)

        # Find late work orders
        today = datetime.utcnow().date()
        late_wos = db.query(WorkOrder).filter(
            WorkOrder.due_date < today,
            WorkOrder.status.in_(["RELEASED", "IN_PROGRESS"])
        ).all()

        if not late_wos:
            return {"late_work_orders": 0}

        # Get supervisors and managers
        supervisors = get_notification_recipients(db, role="supervisor")
        managers = get_notification_recipients(db, role="manager")

        # Send notifications
        for wo in late_wos:
            days_late = (today - wo.due_date).days
            is_critical = days_late > 7

            await notification_service.send_notification(
                event_type=NotificationEvent.WO_LATE,
                users=supervisors + managers,
                subject=f"{'CRITICAL: ' if is_critical else ''}Work Order {wo.wo_number} is {days_late} days late",
                context={
                    "work_order": wo,
                    "days_late": days_late,
                    "critical": is_critical
                },
                template="work_order_late",
                related_type="WorkOrder",
                related_id=wo.id
            )

        logger.info(f"Checked late work orders: {len(late_wos)} late")
        return {"late_work_orders": len(late_wos)}

    except Exception as e:
        logger.error(f"Late work order check job failed: {e}")
        raise
    finally:
        db.close()


async def check_low_stock_task():
    """Check for low stock items and send notifications"""
    db = SessionLocal()
    try:
        from app.models.inventory import InventoryItem

        notification_service = NotificationService(db)

        # Find low stock items
        low_stock = db.query(InventoryItem).filter(
            InventoryItem.quantity_on_hand <= InventoryItem.reorder_point
        ).all()

        if not low_stock:
            return {"low_stock_items": 0}

        # Get purchasing and inventory users
        purchasing_users = get_notification_recipients(db, department="Purchasing")
        inventory_users = get_notification_recipients(db, department="Inventory")

        # Send single notification with all low stock items
        await notification_service.send_notification(
            event_type=NotificationEvent.LOW_STOCK,
            users=purchasing_users + inventory_users,
            subject=f"Low Stock Alert: {len(low_stock)} items below reorder point",
            context={
                "items": low_stock,
                "count": len(low_stock)
            },
            template="low_stock"
        )

        logger.info(f"Low stock check: {len(low_stock)} items")
        return {"low_stock_items": len(low_stock)}

    except Exception as e:
        logger.error(f"Low stock check job failed: {e}")
        raise
    finally:
        db.close()


async def check_quote_expiring_task():
    """Check for quotes expiring soon"""
    db = SessionLocal()
    try:
        from app.models.quote import Quote

        notification_service = NotificationService(db)

        # Find quotes expiring in 7 days
        expiring_soon = datetime.utcnow() + timedelta(days=7)
        expiring_quotes = db.query(Quote).filter(
            Quote.valid_until <= expiring_soon,
            Quote.valid_until > datetime.utcnow(),
            Quote.status == "SENT"
        ).all()

        if not expiring_quotes:
            return {"expiring_quotes": 0}

        # Get sales users
        sales_users = get_notification_recipients(db, department="Sales")

        # Send notifications
        for quote in expiring_quotes:
            days_until_expiry = (quote.valid_until.date() - datetime.utcnow().date()).days

            await notification_service.send_notification(
                event_type=NotificationEvent.QUOTE_EXPIRING,
                users=sales_users,
                subject=f"Quote {quote.quote_number} expires in {days_until_expiry} days",
                context={
                    "quote": quote,
                    "days_until_expiry": days_until_expiry
                },
                template="quote_expiring",
                related_type="Quote",
                related_id=quote.id
            )

        logger.info(f"Quote expiry check: {len(expiring_quotes)} expiring soon")
        return {"expiring_quotes": len(expiring_quotes)}

    except Exception as e:
        logger.error(f"Quote expiry check job failed: {e}")
        raise
    finally:
        db.close()
