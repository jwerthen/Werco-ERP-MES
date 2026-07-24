"""Coverage for the maintenance background jobs on branch qa/full-pass-2026-06-04.

Two behaviours are locked in here:

1. ``cleanup_old_logs_task`` no longer purges ``audit_logs``. It deletes only
   *ephemeral* operational records (old COMPLETED ``Job`` rows and old
   ``NotificationLog`` rows). The audit row MUST survive — deleting audit rows
   would break the tamper-evident hash chain (CMMC AU-3.3.8). This is the
   regression guard for the removed delete.

2. ``archive_aged_audit_logs_task`` delegates to
   ``AuditArchivalService.archive_all`` and honours ``dry_run``.

Both tasks are ``async`` and open their own ``SessionLocal``. Following the
pattern in tests/test_startup_seed.py we monkeypatch the module-level
``SessionLocal`` so the job runs against the same in-test session/connection as
the fixture, which makes its committed effects directly assertable.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock

import pytest
from sqlalchemy.orm import Session

import app.jobs.maintenance_jobs as maintenance_jobs
import app.services.audit_service as audit_service_module
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.governance import ExportEvent
from app.models.job import Job, JobStatus
from app.models.notification import Notification, NotificationLog
from app.models.user import User, UserRole
from app.services.audit_archival_service import ARCHIVE_EXPORT_TYPE, ARCHIVE_RECORD_TYPE
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.requires_db]

# Each test gets a unique user natural key so reruns under the same worker DB
# don't collide on the email/employee_id unique constraints.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


@contextmanager
def _frozen_utcnow(when: datetime):
    """Make ``AuditService.log`` write a row whose hashed timestamp is ``when``.

    Backdating the timestamp column after the fact would invalidate the integrity
    hash (the timestamp is hashed), so we move the clock the writer reads instead.
    """

    class _FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return when

    with mock.patch.object(audit_service_module, "datetime", _FrozenDateTime):
        yield


def _make_user(db: Session, *, is_active: bool = True) -> User:
    n = _next()
    user = User(
        email=f"maint-user-{n}@werco.test",
        employee_id=f"MNT-{n:05d}",
        first_name="Maint",
        last_name="User",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.OPERATOR,
        is_active=is_active,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_in_app_notification(db: Session, user_id: int, *, is_read: bool, age_days: int) -> Notification:
    """Seed a canonical in-app Notification row, backdating created_at to ``age_days``."""
    notif = Notification(
        company_id=1,
        user_id=user_id,
        event_key="wo.late",
        severity="warning",
        title="Notice",
        is_read=is_read,
    )
    db.add(notif)
    db.flush()
    # created_at uses a server default; backdate explicitly so age is deterministic.
    notif.created_at = datetime.utcnow() - timedelta(days=age_days)
    db.add(notif)
    db.commit()
    return notif


def _seed_old_audit_row(db: Session, *, age_days: int = 400) -> AuditLog:
    """Write a real (hash-chained) audit row dated ``age_days`` in the past.

    The clock is moved at write time so the older timestamp is the one folded
    into the integrity hash, keeping the row verifiable by the archival service.
    """
    with _frozen_utcnow(datetime.utcnow() - timedelta(days=age_days)):
        row = AuditService(db, user=None, company_id=1).log(
            action="LOGIN",
            resource_type="authentication",
            resource_identifier="old@werco.test",
        )
    assert row is not None
    db.commit()
    return row


def _seed_old_completed_job(db: Session) -> Job:
    job = Job(
        job_id=f"job-old-completed-{_next()}",
        job_type="send_email",
        queue="default",
        status=JobStatus.COMPLETED,
        completed_at=datetime.utcnow() - timedelta(days=120),
        company_id=1,
    )
    db.add(job)
    db.commit()
    return job


def _seed_recent_pending_job(db: Session) -> Job:
    job = Job(
        job_id=f"job-recent-pending-{_next()}",
        job_type="send_email",
        queue="default",
        status=JobStatus.PENDING,
        completed_at=None,
        company_id=1,
    )
    db.add(job)
    db.commit()
    return job


def _seed_old_notification(db: Session, user_id: int) -> NotificationLog:
    notif = NotificationLog(
        user_id=user_id,
        event_type="WO_LATE",
        channel="email",
        subject="Late work order",
        sent=True,
        company_id=1,
    )
    db.add(notif)
    db.flush()
    # sent_at uses a server default; backdate explicitly so it is "old".
    notif.sent_at = datetime.utcnow() - timedelta(days=120)
    db.add(notif)
    db.commit()
    return notif


# ---------------------------------------------------------------------------
# cleanup_old_logs_task — audit rows survive; ephemeral rows are purged
# ---------------------------------------------------------------------------


async def test_cleanup_old_logs_preserves_audit_but_purges_ephemeral(db_session: Session, monkeypatch):
    """The headline regression: cleanup deletes the old Job + NotificationLog + the
    retention-eligible in-app notifications while the old audit row SURVIVES (audit
    logs are never purged).

    The retention extension (§5) adds two counts to the result: read notifications
    older than the 90-day window, and unread rows belonging to deactivated users.
    """
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", lambda: db_session)

    user = _make_user(db_session)
    deactivated = _make_user(db_session, is_active=False)
    audit_row = _seed_old_audit_row(db_session)
    old_job = _seed_old_completed_job(db_session)
    recent_job = _seed_recent_pending_job(db_session)
    old_notif = _seed_old_notification(db_session, user_id=user.id)

    # In-app inbox rows exercising the retention rules:
    old_read = _seed_in_app_notification(db_session, user.id, is_read=True, age_days=120)  # pruned (aged read)
    recent_read = _seed_in_app_notification(db_session, user.id, is_read=True, age_days=5)  # kept (recent)
    active_unread = _seed_in_app_notification(db_session, user.id, is_read=False, age_days=200)  # kept (unread/active)
    deactivated_unread = _seed_in_app_notification(  # pruned (unread of a deactivated user)
        db_session, deactivated.id, is_read=False, age_days=1
    )

    # Capture primary keys as plain ints up front: the task closes the session in
    # its finally block, detaching ORM instances, so we query by id afterwards.
    audit_id, old_job_id, recent_job_id, old_notif_id = (
        audit_row.id,
        old_job.id,
        recent_job.id,
        old_notif.id,
    )
    old_read_id, recent_read_id, active_unread_id, deactivated_unread_id = (
        old_read.id,
        recent_read.id,
        active_unread.id,
        deactivated_unread.id,
    )

    result = await maintenance_jobs.cleanup_old_logs_task(days_to_keep=90)

    assert result["jobs_deleted"] == 1
    assert result["notification_logs_deleted"] == 1
    assert result["notifications_read_deleted"] == 1
    assert result["notifications_deactivated_deleted"] == 1

    # Audit row is untouched — this is the whole point.
    assert db_session.query(AuditLog).filter(AuditLog.id == audit_id).count() == 1

    # Ephemeral records were purged / retained as expected.
    assert db_session.query(Job).filter(Job.id == old_job_id).count() == 0
    assert db_session.query(Job).filter(Job.id == recent_job_id).count() == 1
    assert db_session.query(NotificationLog).filter(NotificationLog.id == old_notif_id).count() == 0

    # In-app notification retention: aged-read and deactivated-user-unread pruned;
    # recent-read and active-user-unread retained.
    assert db_session.query(Notification).filter(Notification.id == old_read_id).count() == 0
    assert db_session.query(Notification).filter(Notification.id == deactivated_unread_id).count() == 0
    assert db_session.query(Notification).filter(Notification.id == recent_read_id).count() == 1
    assert db_session.query(Notification).filter(Notification.id == active_unread_id).count() == 1


async def test_cleanup_old_logs_keeps_recent_completed_job(db_session: Session, monkeypatch):
    """A COMPLETED job inside the retention window is kept."""
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", lambda: db_session)

    recent_completed = Job(
        job_id=f"job-recent-completed-{_next()}",
        job_type="run_mrp",
        queue="default",
        status=JobStatus.COMPLETED,
        completed_at=datetime.utcnow() - timedelta(days=5),
        company_id=1,
    )
    db_session.add(recent_completed)
    db_session.commit()
    recent_id = recent_completed.id

    result = await maintenance_jobs.cleanup_old_logs_task(days_to_keep=90)

    assert result["jobs_deleted"] == 0
    assert db_session.query(Job).filter(Job.id == recent_id).count() == 1


# ---------------------------------------------------------------------------
# archive_aged_audit_logs_task — delegates to the archival service
# ---------------------------------------------------------------------------


async def test_archive_task_archives_via_service(db_session: Session, monkeypatch, tmp_path):
    """The archive task runs AuditArchivalService.archive_all and (with aged
    rows) produces an archival ExportEvent without deleting audit rows."""
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_ENABLED", True, raising=False)
    # Short retention so the backdated row (400 days old) is past the window.
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS_DEFAULT", 90, raising=False)

    audit_row = _seed_old_audit_row(db_session)
    audit_id = audit_row.id

    result = await maintenance_jobs.archive_aged_audit_logs_task(dry_run=False)

    assert result["status"] == "completed"
    assert result["total_archived"] >= 1

    # The aged audit row still exists (archival is non-destructive).
    assert db_session.query(AuditLog).filter(AuditLog.id == audit_id).count() == 1
    # An archival ExportEvent was written for company 1.
    assert (
        db_session.query(ExportEvent)
        .filter(
            ExportEvent.company_id == 1,
            ExportEvent.record_type == ARCHIVE_RECORD_TYPE,
            ExportEvent.export_type == ARCHIVE_EXPORT_TYPE,
        )
        .count()
        == 1
    )


async def test_archive_task_dry_run_writes_no_ledger(db_session: Session, monkeypatch, tmp_path):
    """dry_run reports but writes no ExportEvent."""
    monkeypatch.setattr(maintenance_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "AUDIT_ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS_DEFAULT", 90, raising=False)

    _seed_old_audit_row(db_session)

    result = await maintenance_jobs.archive_aged_audit_logs_task(dry_run=True)

    assert result["dry_run"] is True
    assert (
        db_session.query(ExportEvent)
        .filter(
            ExportEvent.company_id == 1,
            ExportEvent.export_type == ARCHIVE_EXPORT_TYPE,
        )
        .count()
        == 0
    )
