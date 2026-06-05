import logging
from datetime import datetime, timedelta

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def cleanup_old_logs_task(days_to_keep: int = 90):
    """
    Clean up old *ephemeral* operational logs (background-job tracking and
    notification logs).

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
        from app.models.notification import NotificationLog

        cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)

        # Delete old completed background-job tracking records (ephemeral, not audit).
        jobs_deleted = db.query(Job).filter(Job.completed_at < cutoff_date, Job.status == JobStatus.COMPLETED).delete()

        # Delete old notification logs (ephemeral, not audit).
        notifications_deleted = db.query(NotificationLog).filter(NotificationLog.sent_at < cutoff_date).delete()

        db.commit()

        logger.info(
            "Cleanup complete: %d jobs, %d notifications (audit logs are retained/archived, not purged)",
            jobs_deleted,
            notifications_deleted,
        )

        return {
            "jobs_deleted": jobs_deleted,
            "notifications_deleted": notifications_deleted,
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
