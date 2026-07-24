"""ARQ outbox job coverage: dispatch idempotency + the relay sweeper's selection window
(NOTIFICATIONS_PLAN.md §3.1).

* ``dispatch_notification_task(event_id)`` is idempotent + crash-safe: it sets the
  ``notified_at`` marker and commits the notification rows in ONE transaction, and a second
  run for the same event does nothing (no double-write).
* ``relay_pending_notifications_task`` (the 5-min sweeper, the Redis-outage backstop) only
  re-enqueues catalog-mapped events whose ``notified_at IS NULL`` AND ``created_at`` is older
  than the grace window -- never already-dispatched, too-recent, or uncataloged events.

Redis is never touched: the dispatch email path and the sweeper's ``enqueue_job`` are stubbed.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as njobs
import app.services.notification_dispatch as dispatch
from app.models.company import Company
from app.models.notification import Notification
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole

pytestmark = [pytest.mark.requires_db]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int = 1) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"job-co-{company_id}", is_active=True))
        db.commit()


def _make_quality_user(db: Session, *, company_id: int = 1) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"job-{n}@co{company_id}.test",
        employee_id=f"JOB-{n:05d}",
        first_name="Job",
        last_name="User",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_event(
    db: Session,
    *,
    event_type: str = "ncr_created",
    company_id: int = 1,
    notified_at=None,
    created_at=None,
    entity_id: int = 900,
) -> OperationalEvent:
    event = OperationalEvent(
        company_id=company_id,
        event_type=event_type,
        source_module="quality",
        entity_type="ncr",
        entity_id=entity_id,
        severity="critical",
        event_payload={"ncr_number": f"NCR-{entity_id}"},
        notified_at=notified_at,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _point_jobs_at_session(monkeypatch, db_session):
    monkeypatch.setattr(njobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)


def _stub_dispatch_redis(monkeypatch):
    monkeypatch.setattr(dispatch, "enqueue_job", AsyncMock())
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))


# ---------------------------------------------------------------------------
# dispatch_notification_task idempotency
# ---------------------------------------------------------------------------


def test_dispatch_task_is_idempotent(db_session: Session, monkeypatch):
    _stub_dispatch_redis(monkeypatch)
    _point_jobs_at_session(monkeypatch, db_session)
    user = _make_quality_user(db_session, company_id=1)
    event = _make_event(db_session, company_id=1)

    first = asyncio.run(njobs.dispatch_notification_task(event.id))
    assert first["dispatched"] is True
    assert first["in_app_created"] >= 1
    count_after_first = db_session.query(Notification).filter(Notification.user_id == user.id).count()
    assert count_after_first == 1

    # notified_at marker is now set (idempotency + crash-safety).
    db_session.refresh(event)
    assert event.notified_at is not None

    # Second run is a no-op -- no double-write.
    second = asyncio.run(njobs.dispatch_notification_task(event.id))
    assert second == {"dispatched": False, "reason": "already_dispatched"}
    assert db_session.query(Notification).filter(Notification.user_id == user.id).count() == count_after_first


def test_dispatch_task_missing_event_is_safe_noop(db_session: Session, monkeypatch):
    _stub_dispatch_redis(monkeypatch)
    _point_jobs_at_session(monkeypatch, db_session)
    result = asyncio.run(njobs.dispatch_notification_task(999999))
    assert result == {"dispatched": False, "reason": "event_not_found"}


# ---------------------------------------------------------------------------
# relay sweeper selection window
# ---------------------------------------------------------------------------


def test_sweeper_only_reenqueues_stale_uncommitted_cataloged_events(db_session: Session, monkeypatch):
    _point_jobs_at_session(monkeypatch, db_session)

    now = datetime.utcnow()
    old = now - timedelta(minutes=10)

    ev_stale = _make_event(db_session, event_type="ncr_created", notified_at=None, created_at=old, entity_id=1)
    _make_event(db_session, event_type="ncr_created", notified_at=now, created_at=old, entity_id=2)  # already done
    _make_event(db_session, event_type="ncr_created", notified_at=None, created_at=now, entity_id=3)  # too recent
    _make_event(
        db_session, event_type="totally_uncataloged", notified_at=None, created_at=old, entity_id=4
    )  # not cataloged

    enqueued = []

    async def _fake_enqueue(job_name, *args, **kwargs):
        enqueued.append((job_name, kwargs.get("event_id")))

    monkeypatch.setattr(njobs, "enqueue_job", _fake_enqueue)

    result = asyncio.run(njobs.relay_pending_notifications_task())

    assert enqueued == [("dispatch_notification_job", ev_stale.id)]
    assert result == {"scanned": 1, "enqueued": 1}
