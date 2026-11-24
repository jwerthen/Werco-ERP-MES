"""Scheduled alerts report the right things to the right people (plan alerts_health/2).

Each job runs against the in-test session with ``dispatch_direct`` recorded (no Redis), the way
``test_notification_jobs_tenant_isolation.py`` does it; the company's day is pinned through
``company_time.company_today`` so a run near midnight UTC cannot move it.

Calibration (step 1):
  * an overdue gauge raises ``calibration.overdue`` (it used to go silent once overdue);
  * a gauge due TODAY raises ``calibration.due`` (it fell through both old passes);
  * one alert per gauge per run (the old 7-day and 1-day passes overlapped);
  * out-of-service, retired and inactive gauges raise nothing;
  * recipients come from the catalog entry: a Quality-ROLE user whose Department says something
    else is reached, as is a Quality-department user of another role; nobody else is;
  * the day is the company's (``company_today``), not the UTC date.

Low stock (step 2): the cron's count, ``GET /inventory/low-stock``, the inbox ``low_stock``
source and the AI inventory sensor all name the SAME parts -- a part with no stock row at all
(out of stock) and one exactly at its point are low; one above it, one with reorder point 0 (even
with a safety stock and an empty bin) and a deleted one are not. The alert counts parts, names
none, and links to the filtered Inventory list.

Arrival (step 3): ``stock.low`` has an in-app default so it reaches the bell (``quote.expiring``
is deliberately unchanged, digest-only). The daily digest mails every active, deliverable user
with queued items -- no saved preference row means the defaults -- honours a saved opt-out, and
renders title, body and link for any catalog key.

Late jobs (step 4): one ``wo.late`` summary per company day for the Supervisor and Manager roles
-- total, the ten most overdue (work order number and days late only) and a link to the overdue
list. Late means ``must_ship_by``, else ``due_date``, before ``company_today``, for a released,
in-progress or held job; a second run the same day sends nothing.
"""

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as notification_jobs
import app.services.company_time as company_time
from app.models.calibration import CalibrationStatus, Equipment
from app.models.user import UserRole
from app.services import notification_links as links
from app.services.ai_sensors.inventory import run_inventory_risk_sensor
from app.services.low_stock_service import low_stock_count
from tests.api.test_backflush_lot_policy import make_user
from tests.api.test_owned_property_planning import (
    _inbox_low_stock,
    _lot,
    _low_stock_alerts,
    _reorder_part,
    _risk_recs,
)
from tests.test_notification_jobs_tenant_isolation import COMPANY_A, _make_user, _next

pytestmark = [pytest.mark.requires_db]

TODAY = date(2031, 3, 12)  # a fixed company-local day, far from the runner's own clock


def _pin_today(monkeypatch, today: date = TODAY) -> None:
    monkeypatch.setattr(company_time, "company_today", lambda db, company_id: today)


class _UnclosableSession:
    """The test session as the job's ``SessionLocal()``: its ``finally: db.close()`` must not
    detach the fixtures the assertions read afterwards."""

    def __init__(self, db: Session):
        self._db = db

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


def _record(monkeypatch, db_session: Session) -> list:
    monkeypatch.setattr(notification_jobs, "SessionLocal", lambda: _UnclosableSession(db_session))
    calls: list = []

    async def _fake_dispatch_direct(db, *, event_key, company_id, recipients, **kwargs):
        calls.append({"event_key": event_key, "company_id": company_id, "recipients": list(recipients), **kwargs})
        return 0

    monkeypatch.setattr(notification_jobs, "dispatch_direct", _fake_dispatch_direct)
    return calls


def _gauge(db: Session, due: date, *, status=CalibrationStatus.ACTIVE, is_active=True) -> Equipment:
    n = _next()
    gauge = Equipment(
        equipment_id=f"ALERT-EQ-{n}",
        name=f"Bore gauge {n}",
        next_calibration_date=due,
        status=status,
        is_active=is_active,
        company_id=COMPANY_A,
    )
    db.add(gauge)
    db.commit()
    db.refresh(gauge)
    return gauge


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_calibration_alerts_cover_overdue_and_due_today_once_each(db_session: Session, monkeypatch):
    _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)
    overdue = _gauge(db_session, TODAY - timedelta(days=4), status=CalibrationStatus.OVERDUE)
    due_today = _gauge(db_session, TODAY)
    tomorrow = _gauge(db_session, TODAY + timedelta(days=1))
    in_week = _gauge(db_session, TODAY + timedelta(days=7))
    beyond = _gauge(db_session, TODAY + timedelta(days=8))
    pulled = _gauge(db_session, TODAY - timedelta(days=2), status=CalibrationStatus.OUT_OF_SERVICE)
    retired = _gauge(db_session, TODAY, status=CalibrationStatus.RETIRED)
    inactive = _gauge(db_session, TODAY, is_active=False)
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)

    result = asyncio.run(notification_jobs.check_calibrations_task())

    by_gauge = {}
    for call in calls:
        assert call["related_type"] == "Equipment"
        assert call["related_id"] not in by_gauge, "one alert per gauge per run"
        by_gauge[call["related_id"]] = call
    for silent in (beyond, pulled, retired, inactive):
        assert silent.id not in by_gauge
    assert by_gauge[overdue.id]["event_key"] == "calibration.overdue"
    assert "4 days overdue" in by_gauge[overdue.id]["title"]
    assert by_gauge[due_today.id]["event_key"] == "calibration.due"
    assert by_gauge[due_today.id]["title"].startswith("URGENT: Calibration due today")
    assert by_gauge[tomorrow.id]["title"].startswith("URGENT: Calibration due tomorrow")
    assert by_gauge[in_week.id]["title"].startswith("Calibration due in 7 days")
    assert result["calibrations_overdue"] >= 1 and result["calibrations_due"] >= 3


def test_calibration_alerts_reach_the_quality_role_and_department_from_the_catalog(db_session: Session, monkeypatch):
    by_role = _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY, department="Inspection")
    by_department = _make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR, department="Quality")
    outsider = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER, department="Sales")
    gauge = _gauge(db_session, TODAY - timedelta(days=1))
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)

    asyncio.run(notification_jobs.check_calibrations_task())

    call = next(c for c in calls if c["related_id"] == gauge.id)
    ids = {u.id for u in call["recipients"]}
    assert by_role.id in ids, "the Quality ROLE is reached whatever the Department text says"
    assert by_department.id in ids
    assert outsider.id not in ids


def test_calibration_alerts_judge_the_company_day_not_the_utc_day(db_session: Session, monkeypatch):
    """The same gauge is due on one company day and overdue on the next: the job reads the day
    from ``company_today`` alone."""
    _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)
    gauge = _gauge(db_session, TODAY)

    _pin_today(monkeypatch, TODAY)
    calls = _record(monkeypatch, db_session)
    asyncio.run(notification_jobs.check_calibrations_task())
    assert next(c for c in calls if c["related_id"] == gauge.id)["event_key"] == "calibration.due"

    _pin_today(monkeypatch, TODAY + timedelta(days=1))
    calls = _record(monkeypatch, db_session)
    asyncio.run(notification_jobs.check_calibrations_task())
    assert next(c for c in calls if c["related_id"] == gauge.id)["event_key"] == "calibration.overdue"


# ---------------------------------------------------------------------------
# Low stock: one rule, four readers
# ---------------------------------------------------------------------------


def test_every_low_stock_reader_names_the_same_parts(client, db_session: Session, monkeypatch):
    buyer = make_user(db_session, role=UserRole.MANAGER)
    buyer.department = "Purchasing"
    db_session.commit()
    empty_bin = _reorder_part(db_session, reorder_point=10, safety_stock=2)  # no stock row at all
    at_point = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, at_point, 6)
    _lot(db_session, at_point, 4, status_="on_hold")
    above = _reorder_part(db_session, reorder_point=10)
    _lot(db_session, above, 11)
    safety_only = _reorder_part(db_session, reorder_point=0, safety_stock=5)  # empty, but no point
    deleted = _reorder_part(db_session, reorder_point=10)
    deleted.is_deleted = True
    db_session.commit()
    low = {empty_bin.id, at_point.id}
    never = {above.id, safety_only.id, deleted.id}

    alerts = _low_stock_alerts(client, buyer)
    assert low <= set(alerts) and not (never & set(alerts))
    assert alerts[empty_bin.id]["out_of_stock"] is True and alerts[empty_bin.id]["quantity_on_hand"] == 0
    assert alerts[at_point.id]["out_of_stock"] is False and alerts[at_point.id]["shortage"] == 0

    inbox = _inbox_low_stock(db_session, buyer)
    assert low <= set(inbox) and not (never & set(inbox))

    run_inventory_risk_sensor(db_session, COMPANY_A)
    db_session.commit()
    for part in (empty_bin, at_point):
        assert len(_risk_recs(db_session, part)) == 1
    for part in (above, safety_only, deleted):
        assert _risk_recs(db_session, part) == []

    calls = _record(monkeypatch, db_session)
    result = asyncio.run(notification_jobs.check_low_stock_task())
    count = low_stock_count(db_session, COMPANY_A)
    assert count == len(alerts) and result["low_stock_items"] == count
    (call,) = [c for c in calls if c["company_id"] == COMPANY_A]
    assert call["event_key"] == "stock.low"
    assert call["title"] == f"Low stock: {count} parts at or below reorder point"
    assert "out of stock" in call["body"]
    assert call["link"] == links.INVENTORY_LOW_STOCK
    assert buyer.id in {u.id for u in call["recipients"]}
    for part in (empty_bin, at_point):
        assert part.part_number not in call["title"] + call["body"], "section 11.1: the alert names no part"


# ---------------------------------------------------------------------------
# Low stock reaches the bell; the digest reaches the people it queued for
# ---------------------------------------------------------------------------


def test_stock_low_reaches_the_bell_and_quote_expiring_is_unchanged():
    from app.services.notification_catalog import CHANNEL_DIGEST, CHANNEL_IN_APP, get_entry

    assert get_entry("stock.low").default_channels == frozenset({CHANNEL_IN_APP, CHANNEL_DIGEST})
    assert get_entry("quote.expiring").default_channels == frozenset({CHANNEL_DIGEST})


def _queue(db: Session, user, event_key: str, *, title: str, body: str, link: str):
    from datetime import datetime

    from app.models.notification import DigestQueue

    db.add(
        DigestQueue(
            company_id=user.company_id,
            user_id=user.id,
            event_type=event_key,
            event_data={"title": title, "body": body, "link": link, "related_type": None, "related_id": None},
            processed=False,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()


def test_the_digest_mails_everyone_with_queued_items_and_renders_any_catalog_key(db_session: Session, monkeypatch):
    import app.jobs.email_jobs as email_jobs
    from app.core.config import settings
    from app.models.notification import NotificationPreference
    from app.services.email_service import email_service

    no_row = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    opted_out = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    db_session.add(
        NotificationPreference(company_id=COMPANY_A, user_id=opted_out.id, preferences={}, digest_enabled=False)
    )
    inactive = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    inactive.is_active = False
    badge_only = _make_user(db_session, company_id=COMPANY_A, department="Purchasing")
    badge_only.email = f"badge-{_next()}@users.werco.com"
    db_session.commit()
    for user in (no_row, opted_out, inactive, badge_only):
        _queue(
            db_session,
            user,
            "stock.low",
            title="Low stock: 3 parts at or below reorder point",
            body="3 parts are at or below the reorder point; 1 out of stock.",
            link=links.INVENTORY_LOW_STOCK,
        )
    _queue(
        db_session,
        no_row,
        "calibration.overdue",
        title="OVERDUE: Calibration of Bore gauge 1 is 2 days overdue",
        body="Was due Mar 10, 2031.",
        link=links.CALIBRATION_LIST,
    )
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://erp.example.test")
    monkeypatch.setattr(email_jobs, "SessionLocal", lambda: _UnclosableSession(db_session))
    sent = []

    async def _smtp(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(email_jobs.email_service, "send_email", _smtp)

    result = asyncio.run(email_jobs.send_daily_digest_task())

    assert [call["to"] for call in sent] == [no_row.email]
    assert result["digests_sent"] == 1
    html = email_service._render_template("daily_digest", sent[0]["context"])
    assert "Low stock (1)" in html and "Calibration overdue (1)" in html
    assert "Low stock: 3 parts at or below reorder point" in html
    assert "1 out of stock" in html
    assert f'href="https://erp.example.test{links.INVENTORY_LOW_STOCK}"'.replace("&", "&amp;") in html


# ---------------------------------------------------------------------------
# Late jobs: one summary a day
# ---------------------------------------------------------------------------


def _wo(db: Session, *, due=None, ship_by=None, status=None, number=None):
    from app.models.work_order import WorkOrder, WorkOrderStatus
    from tests.test_notification_jobs_tenant_isolation import _make_part

    n = _next()
    wo = WorkOrder(
        work_order_number=number or f"LATE-{n:05d}",
        part_id=_make_part(db, company_id=COMPANY_A).id,
        customer_name="Fictional Aero Customer",
        quantity_ordered=1,
        status=status or WorkOrderStatus.RELEASED,
        priority=5,
        due_date=due,
        must_ship_by=ship_by,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def test_the_late_summary_is_one_daily_notice_judged_on_the_promise_date(db_session: Session, monkeypatch):
    from app.models.work_order import WorkOrderStatus

    supervisor = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    manager = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    operator = _make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    late_by_ship = _wo(db_session, due=TODAY + timedelta(days=5), ship_by=TODAY - timedelta(days=3))
    not_late_by_ship = _wo(db_session, due=TODAY - timedelta(days=9), ship_by=TODAY)  # ship-by wins
    held = _wo(db_session, due=TODAY - timedelta(days=1), status=WorkOrderStatus.ON_HOLD)
    draft = _wo(db_session, due=TODAY - timedelta(days=30), status=WorkOrderStatus.DRAFT)
    done = _wo(db_session, due=TODAY - timedelta(days=30), status=WorkOrderStatus.COMPLETE)
    many = [_wo(db_session, due=TODAY - timedelta(days=10 + i)) for i in range(10)]
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)

    result = asyncio.run(notification_jobs.check_late_work_orders_task())

    (call,) = [c for c in calls if c["company_id"] == COMPANY_A]
    assert call["event_key"] == "wo.late"
    assert call["related_type"] == "LateWorkOrderSummary" and call["related_id"] == 20310312
    total = 12  # late_by_ship, held and the ten in `many`
    assert result["late_work_orders"] >= total
    assert call["title"] == f"Late work orders: {total}"
    lines = call["body"].split("\n")
    assert lines[0].startswith(f"{total} work orders are past the promise date")
    listed = [line.split(":")[0] for line in lines[1:11]]
    assert listed == [wo.work_order_number for wo in reversed(many)], "the ten most overdue, worst first"
    assert lines[11] == "And 2 more."
    assert call["context"]["total"] == total and len(call["context"]["jobs"]) == 10
    assert call["link"] == links.WORK_ORDERS_OVERDUE
    ids = {u.id for u in call["recipients"]}
    assert {supervisor.id, manager.id} <= ids and operator.id not in ids
    assert "Fictional Aero Customer" not in call["title"] + call["body"]
    body_and_title = call["title"] + call["body"]
    for quiet in (not_late_by_ship, draft, done):
        assert quiet.work_order_number not in body_and_title
    assert late_by_ship.work_order_number not in listed  # 3 days late: outside the worst ten here
    assert held.work_order_number not in listed


def test_a_second_run_the_same_day_sends_no_second_summary(db_session: Session, monkeypatch):
    from app.models.notification import Notification

    user = _make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    _wo(db_session, due=TODAY - timedelta(days=2))
    db_session.add(
        Notification(
            company_id=COMPANY_A,
            user_id=user.id,
            event_key="wo.late",
            severity="warning",
            title="Late work orders: 1",
            related_type="LateWorkOrderSummary",
            related_id=notification_jobs.late_summary_related_id(TODAY),
        )
    )
    db_session.commit()
    _pin_today(monkeypatch)
    calls = _record(monkeypatch, db_session)
    asyncio.run(notification_jobs.check_late_work_orders_task())
    assert [c for c in calls if c["company_id"] == COMPANY_A] == []

    # The next company day is a new summary.
    _pin_today(monkeypatch, TODAY + timedelta(days=1))
    calls = _record(monkeypatch, db_session)
    asyncio.run(notification_jobs.check_late_work_orders_task())
    assert [c["related_id"] for c in calls if c["company_id"] == COMPANY_A] == [20310313]
