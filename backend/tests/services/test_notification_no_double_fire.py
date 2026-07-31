"""No-double-fire regression coverage (NOTIFICATIONS_PLAN.md §10 PR 1, PR1_DESIGN_SPEC.md §E).

The transactional-outbox tee now owns ``wo.blocker_created`` (via the emitted
``work_order_blocker_created`` / ``operation_hold`` events) and ``wo.completed`` (via
``work_order_completed``). The superseded legacy writes were removed so a single action
notifies each recipient exactly once:

* ``work_order_blocker_service.create_blocker`` no longer writes ``NotificationLog`` rows
  directly (the deleted ``_create_notification_logs``) -- it only emits the operational
  event (which the outbox turns into notifications) and keeps its AI recommendation.
* ``dispatch_work_order_completion_signals_task`` no longer sends the in-app/email
  notification leg -- only the webhook leg remains (asserted in
  tests/services/test_completion_signals_batch5.py).
"""

import inspect

import pytest
from sqlalchemy.orm import Session

from app.models.notification import NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.work_order_blocker import WorkOrderBlockerCreate
from app.services.work_order_blocker_service import WorkOrderBlockerService

pytestmark = [pytest.mark.requires_db]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(db: Session) -> User:
    n = _next()
    user = User(
        email=f"ndf-{n}@co1.test",
        employee_id=f"NDF-{n:05d}",
        first_name="NDF",
        last_name="User",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.SUPERVISOR,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_work_order(db: Session) -> WorkOrder:
    n = _next()
    part = Part(
        part_number=f"NDF-P-{n}",
        name=f"NDF part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    wo = WorkOrder(
        work_order_number=f"NDF-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status="draft",
        priority=3,
        company_id=1,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def test_create_blocker_writes_no_notification_log_directly(db_session: Session):
    """The blocker service emits ``work_order_blocker_created`` (the outbox owns the
    notification) but writes ZERO NotificationLog rows itself -- proving the deleted
    ``_create_notification_logs`` double-fire is gone."""
    user = _make_user(db_session)
    wo = _make_work_order(db_session)

    blocker = WorkOrderBlockerService(db_session).create_blocker(
        company_id=1,
        user=user,
        work_order_id=wo.id,
        data=WorkOrderBlockerCreate(put_operation_on_hold=False),
        audit=None,
    )
    db_session.commit()

    assert blocker.id is not None
    # No direct in-app/email delivery-log write from the service.
    assert db_session.query(NotificationLog).count() == 0

    # But it DID emit the operational event that the outbox tee turns into notifications.
    emitted = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == 1,
            OperationalEvent.event_type == "work_order_blocker_created",
            OperationalEvent.entity_id == blocker.id,
        )
        .first()
    )
    assert emitted is not None, "create_blocker must still emit work_order_blocker_created"


def test_blocker_service_has_no_legacy_notification_log_writer():
    """The deleted helper stays deleted (structural guard against re-introduction)."""
    assert not hasattr(WorkOrderBlockerService, "_create_notification_logs")
    source = inspect.getsource(WorkOrderBlockerService)
    # It may MENTION the removed write in a comment, but it must never CONSTRUCT one.
    assert "NotificationLog(" not in source, "the blocker service must not construct NotificationLog rows"


def test_completion_signal_job_has_no_notification_leg():
    """The completion-signal task keeps ONLY the webhook leg; the notification leg moved
    to the outbox. Guard against a re-introduced NotificationService call."""
    from app.jobs import completion_signal_jobs

    source = inspect.getsource(completion_signal_jobs)
    assert "NotificationService" not in source
    assert "send_notification" not in source
