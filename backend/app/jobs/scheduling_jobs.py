from app.db.session import SessionLocal
from app.services.scheduling_service import SchedulingService
from app.services.notification_service import NotificationService, NotificationEvent, get_notification_recipients
import logging

logger = logging.getLogger(__name__)


async def run_scheduling_task(
    work_center_ids: list = None,
    horizon_days: int = 90,
    optimize_setup: bool = False
):
    """
    Background job to run constraint-based scheduling

    Args:
        work_center_ids: List of work center IDs to schedule (None = all)
        horizon_days: Scheduling horizon in days
        optimize_setup: Optimize for setup time reduction
    """
    db = SessionLocal()
    try:
        scheduling_service = SchedulingService(db)

        logger.info(f"Starting scheduling job: horizon={horizon_days} days")

        # Run scheduling algorithm
        results = scheduling_service.run_scheduling(
            work_center_ids=work_center_ids,
            horizon_days=horizon_days,
            optimize_setup=optimize_setup
        )

        logger.info(f"Scheduling complete: {results['scheduled_count']} operations scheduled, "
                   f"{results['conflict_count']} conflicts")

        # Check for capacity conflicts
        conflicts = scheduling_service.detect_conflicts()

        if conflicts:
            logger.warning(f"Detected {len(conflicts)} capacity conflicts")

            # Send notification to production managers
            notification_service = NotificationService(db)
            managers = get_notification_recipients(db, role="manager")

            if managers:
                await notification_service.send_notification(
                    event_type=NotificationEvent.CAPACITY_OVERLOAD,
                    users=managers,
                    subject=f"Production Scheduling: {len(conflicts)} Capacity Conflicts Detected",
                    context={
                        "conflicts": conflicts,
                        "scheduled_count": results["scheduled_count"]
                    },
                    template="scheduling_conflicts"
                )

        return {
            "scheduled_count": results["scheduled_count"],
            "conflict_count": results["conflict_count"],
            "capacity_conflicts": len(conflicts),
            "conflicts": conflicts[:10]  # Return first 10 conflicts
        }

    except Exception as e:
        logger.error(f"Scheduling job failed: {e}")
        raise
    finally:
        db.close()
