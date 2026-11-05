"""``dispatch.changed`` -- the in-app notification for a changed machine run order.

Owner decision (2026-11-04): the floor banner plus the in-app inbox, no SMS. The catalog
entry fans a committed ``dispatch_order_changed`` event out to the operators CLOCKED IN at
that machine (``resolve_clocked_in_operators``). The dispatcher runs in the worker with no
request-scoped tenant protection, so the resolver's own filters are the whole fence:

* tenant isolation -- a foreign company's clocked-in user (or a foreign entry on this
  company's operation) receives nothing;
* active users only; the actor is never notified of their own change;
* LABOR entries only (setup / run / rework / inspection): an open BREAK or DOWNTIME row,
  or a closed entry, is not "clocked in";
* the machine is the operation's CURRENT work center, not the entry's stamped one;
* recurring: one unread row per (machine, operator) -- a planner dragging cards around
  does not stack a row per drag;
* content: titled with the machine code (``work_center_code`` is the LAST identifier
  key), a GENERIC body (no next-up / operator / part detail), and the record-less
  ``/shop-floor/operations`` link.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.models.notification import Notification, NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus
from app.services import notification_links as links
from app.services.notification_catalog import (
    CATALOG,
    CHANNEL_IN_APP,
    SOURCE_EVENT_TYPE_TO_KEY,
    resolve_clocked_in_operators,
)
from app.services.notification_dispatch import _IDENTIFIER_KEYS, _content_for_event, dispatch_for_event
from tests.api.dispatch_plan_helpers import make_plan_wo, ready
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, make_user, make_work_center

pytestmark = [pytest.mark.requires_db]

EVENT_KEY = "dispatch.changed"


def _patch_no_redis(monkeypatch):
    email_spy = AsyncMock()
    monkeypatch.setattr(dispatch, "enqueue_job", email_spy)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    return email_spy


def _event(work_center, *, actor_id=None, company_id: int = COMPANY_A) -> OperationalEvent:
    return OperationalEvent(
        company_id=company_id,
        event_type="dispatch_order_changed",
        source_module="dispatch",
        entity_type="work_center",
        entity_id=work_center.id,
        user_id=actor_id,
        severity="info",
        event_payload={
            "work_center_id": work_center.id,
            "work_center_code": work_center.code,
            "change_kind": "work_order_order",
            "actor_display": "Jon W.",
            "next_up_before": {"operation_id": 1, "operation_name": "P-123 - Bend", "part_number": "P-123"},
            "next_up_after": {"operation_id": 2, "operation_name": "P-456 - Bend", "part_number": "P-456"},
            "moved_up": [2],
            "queue_length": 2,
        },
    )


def _clock_in(db: Session, user, operation, *, entry_type=TimeEntryType.RUN, closed=False, company_id=None, wc_id=None):
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=wc_id if wc_id is not None else operation.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow(),
        clock_out=datetime.utcnow() if closed else None,
        company_id=company_id if company_id is not None else operation.company_id,
    )
    db.add(entry)
    db.commit()
    return entry


def _rows(db: Session, user_id: int):
    return db.query(Notification).filter(Notification.user_id == user_id, Notification.event_key == EVENT_KEY).all()


# --------------------------------------------------------------------------
# The catalog entry
# --------------------------------------------------------------------------


def test_catalog_entry_matches_the_owner_decision():
    entry = CATALOG[EVENT_KEY]
    assert SOURCE_EVENT_TYPE_TO_KEY["dispatch_order_changed"] == EVENT_KEY
    assert entry.label == "Dispatch order changed"
    assert entry.category == "Production"
    assert entry.severity == "info"
    assert entry.default_channels == frozenset({CHANNEL_IN_APP})
    assert entry.mandatory_channel is None
    assert entry.sms_eligible is False
    assert entry.recurring is True
    assert entry.resolver is resolve_clocked_in_operators
    assert entry.roles == () and entry.departments == ()


def test_work_center_code_is_the_last_identifier_key():
    """Appended LAST so no existing event's title can change (NOTIFICATIONS.md 11.1)."""
    assert _IDENTIFIER_KEYS[-1] == "work_center_code"
    assert _IDENTIFIER_KEYS.index("work_center_code") > _IDENTIFIER_KEYS.index("work_order_number")


def test_detail_keys_carry_none_of_the_notice_detail():
    detail_keys = {key for key, _ in dispatch._DETAIL_KEYS}
    for key in ("next_up_before", "next_up_after", "moved_up", "actor_display", "change_kind", "queue_length"):
        assert key not in detail_keys
        assert key not in dispatch._SMS_DETAIL_KEYS


def test_content_is_the_machine_code_and_a_generic_body(db_session: Session):
    wc = make_work_center(db_session)
    title, body = _content_for_event(CATALOG[EVENT_KEY], _event(wc))
    assert title == f"Dispatch order changed: {wc.code}"
    assert body == CATALOG[EVENT_KEY].description
    for leaked in ("P-123", "P-456", "Jon W.", "Bend"):
        assert leaked not in title and leaked not in body


def test_link_is_the_record_less_shop_floor_operations_page(db_session: Session):
    wc = make_work_center(db_session)
    assert dispatch._link_for_event(_event(wc)) == links.SHOP_FLOOR_OPERATIONS == "/shop-floor/operations"


# --------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------


def test_resolver_finds_exactly_the_operators_clocked_in_at_the_machine(db_session: Session):
    brake, saw = make_work_center(db_session), make_work_center(db_session)
    _, (at_brake,) = make_plan_wo(db_session, [(brake, OperationStatus.IN_PROGRESS, 10)])
    _, (also_brake,) = make_plan_wo(db_session, [(brake, OperationStatus.IN_PROGRESS, 10)])
    _, (at_saw,) = make_plan_wo(db_session, [(saw, OperationStatus.IN_PROGRESS, 10)])

    runner = make_user(db_session)
    setter = make_user(db_session)
    double = make_user(db_session)  # two open entries at the brake -> still one recipient
    sawyer = make_user(db_session)
    _clock_in(db_session, runner, at_brake)
    _clock_in(db_session, setter, also_brake, entry_type=TimeEntryType.SETUP)
    _clock_in(db_session, double, at_brake)
    _clock_in(db_session, double, also_brake, entry_type=TimeEntryType.INSPECTION)
    _clock_in(db_session, sawyer, at_saw)

    recipients = resolve_clocked_in_operators(db_session, _event(brake), COMPANY_A)
    assert sorted(user.id for user in recipients) == sorted([runner.id, setter.id, double.id])


@pytest.mark.parametrize("entry_type", [TimeEntryType.BREAK, TimeEntryType.DOWNTIME])
def test_non_labor_entries_are_not_clocked_in(db_session: Session, entry_type):
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [ready(wc)])
    user = make_user(db_session)
    _clock_in(db_session, user, op, entry_type=entry_type)
    assert resolve_clocked_in_operators(db_session, _event(wc), COMPANY_A) == []


def test_a_closed_entry_is_not_clocked_in(db_session: Session):
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [ready(wc)])
    user = make_user(db_session)
    _clock_in(db_session, user, op, closed=True)
    assert resolve_clocked_in_operators(db_session, _event(wc), COMPANY_A) == []


def test_the_machine_is_the_operations_current_work_center(db_session: Session):
    old_wc, new_wc = make_work_center(db_session), make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [(new_wc, OperationStatus.IN_PROGRESS, 10)])
    user = make_user(db_session)
    _clock_in(db_session, user, op, wc_id=old_wc.id)  # the entry still names the old machine
    assert [u.id for u in resolve_clocked_in_operators(db_session, _event(new_wc), COMPANY_A)] == [user.id]
    assert resolve_clocked_in_operators(db_session, _event(old_wc), COMPANY_A) == []


def test_an_inactive_user_is_not_a_recipient(db_session: Session):
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [ready(wc)])
    gone = make_user(db_session, is_active=False)
    _clock_in(db_session, gone, op)
    assert resolve_clocked_in_operators(db_session, _event(wc), COMPANY_A) == []


def test_resolver_is_tenant_isolated(db_session: Session):
    wc_a = make_work_center(db_session)
    _, (op_a,) = make_plan_wo(db_session, [ready(wc_a)])
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    _, (op_b,) = make_plan_wo(db_session, [ready(wc_b)], company_id=COMPANY_B)

    home = make_user(db_session)
    foreign = make_user(db_session, company_id=COMPANY_B)
    _clock_in(db_session, home, op_a)
    _clock_in(db_session, foreign, op_b)
    # Corrupt cross-tenant rows must not leak either way: a company-B user's entry on a
    # company-A operation, and a company-B entry row on a company-A operation.
    _clock_in(db_session, foreign, op_a)
    _clock_in(db_session, home, op_a, company_id=COMPANY_B)

    assert [u.id for u in resolve_clocked_in_operators(db_session, _event(wc_a), COMPANY_A)] == [home.id]
    # The company argument (the event's company) is the scope, never the entity id.
    assert resolve_clocked_in_operators(db_session, _event(wc_b), COMPANY_A) == []
    assert [u.id for u in resolve_clocked_in_operators(db_session, _event(wc_b), COMPANY_B)] == [foreign.id]


def test_resolver_ignores_events_about_anything_but_a_work_center(db_session: Session):
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [ready(wc)])
    _clock_in(db_session, make_user(db_session), op)
    event = _event(wc)
    event.entity_type = "work_order"
    assert resolve_clocked_in_operators(db_session, event, COMPANY_A) == []


# --------------------------------------------------------------------------
# End to end through the dispatcher
# --------------------------------------------------------------------------


def test_dispatch_writes_one_in_app_row_per_clocked_in_operator_and_never_the_actor(db_session: Session, monkeypatch):
    email_spy = _patch_no_redis(monkeypatch)
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [(wc, OperationStatus.IN_PROGRESS, 10)])
    operator = make_user(db_session)
    planner = make_user(db_session, role=UserRole.SUPERVISOR)  # clocked in too, but the actor
    foreign = make_user(db_session, company_id=COMPANY_B)
    _clock_in(db_session, operator, op)
    _clock_in(db_session, planner, op)
    _clock_in(db_session, foreign, op, company_id=COMPANY_B)

    created = asyncio.run(dispatch_for_event(db_session, _event(wc, actor_id=planner.id)))
    db_session.flush()

    assert created == 1
    (row,) = _rows(db_session, operator.id)
    assert row.company_id == COMPANY_A
    assert row.title == f"Dispatch order changed: {wc.code}"
    assert row.link == "/shop-floor/operations"
    assert row.related_type == "work_center" and row.related_id == wc.id
    assert _rows(db_session, planner.id) == []
    assert _rows(db_session, foreign.id) == []
    # In-app only: no email leg, no SMS log row.
    assert email_spy.await_count == 0
    assert db_session.query(NotificationLog).filter(NotificationLog.user_id == operator.id).count() == 0


def test_recurring_suppresses_a_second_row_until_the_first_is_read(db_session: Session, monkeypatch):
    _patch_no_redis(monkeypatch)
    wc = make_work_center(db_session)
    _, (op,) = make_plan_wo(db_session, [ready(wc)])
    operator = make_user(db_session)
    _clock_in(db_session, operator, op)

    asyncio.run(dispatch_for_event(db_session, _event(wc)))
    asyncio.run(dispatch_for_event(db_session, _event(wc)))
    db_session.flush()
    assert len(_rows(db_session, operator.id)) == 1

    _rows(db_session, operator.id)[0].is_read = True
    db_session.commit()
    asyncio.run(dispatch_for_event(db_session, _event(wc)))
    db_session.flush()
    assert len(_rows(db_session, operator.id)) == 2


def test_a_notice_at_another_machine_is_a_separate_row(db_session: Session, monkeypatch):
    """Suppression keys on the machine: an unread BRAKE row does not swallow a SAW notice."""
    _patch_no_redis(monkeypatch)
    brake, saw = make_work_center(db_session), make_work_center(db_session)
    _, (at_brake,) = make_plan_wo(db_session, [ready(brake)])
    _, (at_saw,) = make_plan_wo(db_session, [ready(saw)])
    operator = make_user(db_session)
    _clock_in(db_session, operator, at_brake)
    _clock_in(db_session, operator, at_saw)

    asyncio.run(dispatch_for_event(db_session, _event(brake)))
    asyncio.run(dispatch_for_event(db_session, _event(saw)))
    db_session.flush()
    assert sorted(row.related_id for row in _rows(db_session, operator.id)) == sorted([brake.id, saw.id])
