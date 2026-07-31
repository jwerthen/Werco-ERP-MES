import logging
from datetime import datetime, timedelta

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Retention window for read in-app notifications (§5). Unread rows are retained (they are
# actionable) EXCEPT those belonging to deactivated users, which are pruned regardless.
NOTIFICATION_RETENTION_DAYS = 90


async def cleanup_old_logs_task(days_to_keep: int = 90):
    """
    Clean up old *ephemeral* operational logs (background-job tracking, notification
    delivery logs, and read in-app notifications).

    IMPORTANT: audit logs are deliberately NOT purged here. ``audit_logs`` is an
    immutable, tamper-evident, append-only compliance store (CMMC AU-3.3.8): the
    ``tr_audit_log_no_delete`` trigger rejects row DELETEs and removing rows would
    create sequence gaps that break hash-chain verification. Aged audit rows are
    exported to cold storage by ``archive_aged_audit_logs_task`` instead, and any
    physical removal from the online DB is a deliberate DBA partition-drop (see
    docs/AUDIT_LOG_RETENTION_RUNBOOK.md) — never an automated row delete.

    Args:
        days_to_keep: Number of days to keep ephemeral logs.
    """
    db = SessionLocal()
    try:
        from app.models.job import Job, JobStatus
        from app.models.notification import Notification, NotificationLog
        from app.models.user import User

        cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)
        notif_cutoff = datetime.utcnow() - timedelta(days=NOTIFICATION_RETENTION_DAYS)

        # Delete old completed background-job tracking records (ephemeral, not audit).
        jobs_deleted = db.query(Job).filter(Job.completed_at < cutoff_date, Job.status == JobStatus.COMPLETED).delete()

        # Delete old notification delivery logs (ephemeral, not audit).
        notification_logs_deleted = db.query(NotificationLog).filter(NotificationLog.sent_at < cutoff_date).delete()

        # Prune read in-app notifications older than the retention window.
        notifications_read_deleted = (
            db.query(Notification)
            .filter(Notification.is_read.is_(True), Notification.created_at < notif_cutoff)
            .delete(synchronize_session=False)
        )

        # Prune unread rows belonging to deactivated users (excluded from counts anyway).
        deactivated_user_ids = [row_id for (row_id,) in db.query(User.id).filter(User.is_active == False).all()]
        notifications_deactivated_deleted = 0
        if deactivated_user_ids:
            notifications_deactivated_deleted = (
                db.query(Notification)
                .filter(Notification.is_read.is_(False), Notification.user_id.in_(deactivated_user_ids))
                .delete(synchronize_session=False)
            )

        db.commit()

        logger.info(
            "Cleanup complete: %d jobs, %d notification logs, %d read notifications, "
            "%d deactivated-user notifications (audit logs are retained/archived, not purged)",
            jobs_deleted,
            notification_logs_deleted,
            notifications_read_deleted,
            notifications_deactivated_deleted,
        )

        return {
            "jobs_deleted": jobs_deleted,
            "notification_logs_deleted": notification_logs_deleted,
            "notifications_read_deleted": notifications_read_deleted,
            "notifications_deactivated_deleted": notifications_deactivated_deleted,
        }

    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()


async def archive_aged_audit_logs_task(dry_run: bool = False):
    """
    Export audit rows past their retention window to cold storage.

    Compliant replacement for deleting old audit logs: it preserves immutability
    and hash-chain verifiability (rows are exported, never removed) and honors the
    per-company ``security_audit_record`` retention policy. See
    :class:`app.services.audit_archival_service.AuditArchivalService`.

    Args:
        dry_run: If True, report what would be archived without writing any files
            or ledger rows.
    """
    from app.services.audit_archival_service import AuditArchivalService

    db = SessionLocal()
    try:
        result = AuditArchivalService(db).archive_all(dry_run=dry_run)
        logger.info(
            "Audit archival complete: status=%s total_archived=%d dry_run=%s",
            result.get("status"),
            result.get("total_archived", 0),
            dry_run,
        )
        return result
    except Exception as e:
        logger.error(f"Audit archival job failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()
