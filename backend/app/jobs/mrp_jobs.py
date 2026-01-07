from app.db.session import SessionLocal
from app.services.mrp_service import MRPService
from app.services.mrp_auto_service import MRPAutoService, MRPAutoMode
from app.services.notification_service import NotificationService, NotificationEvent, get_notification_recipients
import logging

logger = logging.getLogger(__name__)


async def run_mrp_task(mode: str = MRPAutoMode.REVIEW, planning_horizon_days: int = 90):
    """
    Background job to run MRP calculation

    Args:
        mode: Auto-processing mode (REVIEW, AUTO_DRAFT, AUTO_SUBMIT)
        planning_horizon_days: Planning horizon in days
    """
    db = SessionLocal()
    try:
        mrp_service = MRPService(db)
        auto_service = MRPAutoService(db)

        # Run MRP calculation
        logger.info(f"Starting MRP run with mode={mode}, horizon={planning_horizon_days} days")

        mrp_run = mrp_service.run_mrp(
            user_id=None,  # System user
            planning_horizon_days=planning_horizon_days,
            include_safety_stock=True,
            include_allocated=True
        )

        logger.info(f"MRP run {mrp_run.run_number} completed: "
                   f"{mrp_run.total_actions} actions, "
                   f"{mrp_run.total_requirements} requirements")

        # Auto-process actions based on mode
        if mode != MRPAutoMode.REVIEW and mrp_run.actions:
            results = auto_service.process_actions(
                actions=mrp_run.actions,
                mode=mode,
                user_id=None  # System user
            )

            logger.info(f"Auto-processed MRP actions: "
                       f"{results['pos_created']} POs, "
                       f"{results['wos_created']} WOs, "
                       f"{results['errors']} errors")

            # Send notification to planners
            notification_service = NotificationService(db)
            planners = get_notification_recipients(db, role="manager")

            if planners:
                await notification_service.send_notification(
                    event_type=NotificationEvent.CAPACITY_OVERLOAD,  # Reusing event
                    users=planners,
                    subject=f"MRP Run {mrp_run.run_number} Completed",
                    context={
                        "mrp_run": mrp_run,
                        "results": results,
                        "mode": mode
                    },
                    template="mrp_complete"
                )

            return {
                "mrp_run_id": mrp_run.id,
                "mrp_run_number": mrp_run.run_number,
                "total_actions": mrp_run.total_actions,
                "auto_processing": results
            }

        else:
            # Review mode - just notify about actions
            if mrp_run.total_actions > 0:
                notification_service = NotificationService(db)
                planners = get_notification_recipients(db, role="manager")

                if planners:
                    await notification_service.send_notification(
                        event_type=NotificationEvent.CAPACITY_OVERLOAD,
                        users=planners,
                        subject=f"MRP Run {mrp_run.run_number}: {mrp_run.total_actions} Actions Need Review",
                        context={
                            "mrp_run": mrp_run
                        },
                        template="mrp_review_needed"
                    )

            return {
                "mrp_run_id": mrp_run.id,
                "mrp_run_number": mrp_run.run_number,
                "total_actions": mrp_run.total_actions,
                "mode": "REVIEW"
            }

    except Exception as e:
        logger.error(f"MRP job failed: {e}")
        raise
    finally:
        db.close()
