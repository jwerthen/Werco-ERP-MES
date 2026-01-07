from app.db.session import SessionLocal
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


async def cleanup_old_logs_task(days_to_keep: int = 90):
    """
    Clean up old audit logs and job records

    Args:
        days_to_keep: Number of days to keep logs
    """
    db = SessionLocal()
    try:
        from app.models.audit_log import AuditLog
        from app.models.job import Job, JobStatus
        from app.models.notification import NotificationLog

        cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)

        # Delete old audit logs
        audit_deleted = db.query(AuditLog).filter(
            AuditLog.timestamp < cutoff_date
        ).delete()

        # Delete old completed jobs
        jobs_deleted = db.query(Job).filter(
            Job.completed_at < cutoff_date,
            Job.status == JobStatus.COMPLETED
        ).delete()

        # Delete old notification logs
        notifications_deleted = db.query(NotificationLog).filter(
            NotificationLog.sent_at < cutoff_date
        ).delete()

        db.commit()

        logger.info(f"Cleanup complete: {audit_deleted} audit logs, "
                   f"{jobs_deleted} jobs, {notifications_deleted} notifications")

        return {
            "audit_logs_deleted": audit_deleted,
            "jobs_deleted": jobs_deleted,
            "notifications_deleted": notifications_deleted
        }

    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()
