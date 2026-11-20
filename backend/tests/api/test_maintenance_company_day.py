"""Maintenance "today" is the COMPANY's calendar day, never the server's UTC day.

Production runs on UTC, so from 6 PM Central (7 PM in summer) until midnight the server's
``date.today()`` / ``datetime.utcnow().date()`` is already TOMORROW. Before the fix a PM
completed at 8 PM on Dec 1 was stamped completed Dec 2 and rolled forward from Dec 2, and the
dashboard called a PM due Dec 1 overdue while it was still Dec 1 on the shop floor.

There is no freezegun in this repo, so :func:`frozen_clock` pins every clock the endpoint can
read to one instant -- 2026-12-02 02:00 UTC, i.e. 2026-12-01 20:00 CST:

* ``app.api.endpoints.maintenance.datetime`` -- ``utcnow()`` (``completed_at``, and the UTC
  day the old completion stamped);
* ``app.api.endpoints.maintenance.date`` -- ``today()``, the UTC day the old dashboard, overdue
  label and create defaults read (a UTC server's ``date.today()``);
* ``app.core.time_utils.datetime`` -- ``now(tz)``, which ``company_today`` -> ``local_today``
  reads.

Every assertion is chosen so the pre-fix code (UTC day, Dec 2) fails it and the company-day code
(Dec 1) passes. The patched classes return REAL ``datetime`` / ``date`` instances and keep
``isinstance`` / ``fromisoformat`` / arithmetic working for everything else in those modules.
"""

from datetime import date as real_date
from datetime import datetime as real_datetime
from datetime import timedelta, timezone

import pytest
from sqlalchemy.orm import Session

import app.api.endpoints.maintenance as maintenance_endpoints
import app.core.time_utils as time_utils
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.maintenance import MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_pm_roll_forward import make_raw_wo, make_schedule
from tests.api.test_receiving_compliance import _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

SCHEDULES = "/api/v1/maintenance/schedules"
WORK_ORDERS = "/api/v1/maintenance/work-orders"
DASHBOARD = "/api/v1/maintenance/dashboard"

# 8 PM on Dec 1 in Chicago (CST, UTC-6) is already 2 AM on Dec 2 in UTC.
EVENING_UTC = real_datetime(2026, 12, 2, 2, 0, tzinfo=timezone.utc)
LOCAL_DAY = real_date(2026, 12, 1)
UTC_DAY = real_date(2026, 12, 2)


class _FrozenClock:
    """The instant every patched clock reads; a test may move it before the first request."""

    instant = EVENING_UTC


class _RealDatetimeMeta(type):
    """``isinstance(x, <patched datetime>)`` keeps meaning "is a datetime" for real instances."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, real_datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, real_datetime)


class _RealDateMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, real_date)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, real_date)


class FrozenDatetime(real_datetime, metaclass=_RealDatetimeMeta):
    """``datetime`` whose clock reads :class:`_FrozenClock` -- as a UTC server's would."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FrozenClock.instant.replace(tzinfo=None)  # a UTC server's naive wall clock
        return _FrozenClock.instant.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return _FrozenClock.instant.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()


class FrozenDate(real_date, metaclass=_RealDateMeta):
    """``date`` whose ``today()`` is the UTC day at :class:`_FrozenClock` -- what the pre-fix
    endpoint read through a UTC server's ``date.today()``."""

    @classmethod
    def today(cls):
        return _FrozenClock.instant.astimezone(timezone.utc).date()


@pytest.fixture
def frozen_clock(monkeypatch, db_session):
    """Pin the maintenance endpoint's clocks to :data:`EVENING_UTC`; company 1 is in Chicago."""
    company = db_session.get(Company, 1)
    company.timezone = "America/Chicago"
    db_session.commit()
    monkeypatch.setattr(_FrozenClock, "instant", EVENING_UTC)
    monkeypatch.setattr(maintenance_endpoints, "datetime", FrozenDatetime)
    monkeypatch.setattr(maintenance_endpoints, "date", FrozenDate)
    monkeypatch.setattr(time_utils, "datetime", FrozenDatetime)
    return _FrozenClock


@pytest.fixture
def planner_headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=1))


def dashboard(client, headers) -> dict:
    resp = client.get(DASHBOARD, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_completed_wo(db: Session, *, completed_at: real_datetime, total_cost: float) -> MaintenanceWorkOrder:
    wo = MaintenanceWorkOrder(
        company_id=1,
        work_center_id=make_wc(db).id,
        wo_number=f"MWO-DAY-{_next():05d}",
        title="Synthetic completed maintenance",
        status=MaintenanceStatus.COMPLETED,
        completed_at=completed_at,
        total_cost=total_cost,
        requires_shutdown=False,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


class TestTheFrozenClockItself:
    def test_the_patched_classes_still_behave_as_datetime_and_date(self, frozen_clock):
        """The patch replaces module globals the endpoint uses for more than reading the clock,
        so the classes must stay drop-in: real instances still pass ``isinstance``, parsing and
        arithmetic still work, and the clocks read the frozen instant."""
        assert isinstance(real_datetime(2026, 1, 1), maintenance_endpoints.datetime)
        assert isinstance(real_date(2026, 1, 1), maintenance_endpoints.date)
        assert maintenance_endpoints.datetime.fromisoformat("2026-12-01T20:00:00") == real_datetime(2026, 12, 1, 20)
        assert maintenance_endpoints.date.fromisoformat("2026-12-01") + timedelta(days=1) == UTC_DAY
        assert maintenance_endpoints.datetime.utcnow() == real_datetime(2026, 12, 2, 2, 0)
        assert maintenance_endpoints.date.today() == UTC_DAY
        assert time_utils.local_today(time_utils.CENTRAL_TIME_ZONE) == LOCAL_DAY


class TestCompletionStampsTheCompanyDay:
    def test_an_evening_completion_is_stamped_and_rolled_forward_from_the_local_day(
        self, client, db_session, planner_headers, frozen_clock
    ):
        schedule = make_schedule(db_session, next_due=LOCAL_DAY)  # monthly: one calendar month
        created = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"schedule_id": schedule.id, "title": "PM", "due_date": LOCAL_DAY.isoformat()},
        )
        assert created.status_code == 200, created.text
        wo_id = created.json()["id"]
        assert client.post(f"{WORK_ORDERS}/{wo_id}/start", headers=planner_headers).status_code == 200

        done = client.post(f"{WORK_ORDERS}/{wo_id}/complete", headers=planner_headers, json={})
        assert done.status_code == 200, done.text

        # One calendar month from the LOCAL day (Dec 1 -> Jan 1); the UTC day would give Jan 2.
        expected_next = real_date(2027, 1, 1)
        assert done.json()["schedule_next_due_date"] == expected_next.isoformat() == "2027-01-01"
        # completed_at is still the UTC instant; only the calendar DAY is the company's.
        assert done.json()["completed_at"] == "2026-12-02T02:00:00Z"

        db_session.expire_all()
        stored = db_session.get(MaintenanceSchedule, schedule.id)
        assert stored.last_completed_date == LOCAL_DAY  # not Dec 2, the server's UTC day
        assert stored.next_due_date == expected_next

        [row] = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "maintenance_schedule", AuditLog.resource_id == schedule.id)
            .all()
        )
        assert row.new_values == {"last_completed_date": "2026-12-01", "next_due_date": "2027-01-01"}

    def test_a_company_east_of_utc_is_stamped_its_own_later_day(
        self, client, db_session, planner_headers, frozen_clock
    ):
        """The day comes from ``Company.timezone``, not a hard-coded Central: at 20:00 UTC on
        Dec 1 it is already Dec 2 in Tokyo, so the UTC day would be a day EARLY there."""
        company = db_session.get(Company, 1)
        company.timezone = "Asia/Tokyo"
        db_session.commit()
        frozen_clock.instant = real_datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc)

        schedule = make_schedule(db_session, next_due=real_date(2026, 12, 1))
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"schedule_id": schedule.id, "title": "PM"})
        assert wo.status_code == 200, wo.text
        done = client.post(f"{WORK_ORDERS}/{wo.json()['id']}/complete", headers=planner_headers, json={})
        assert done.status_code == 200, done.text

        db_session.expire_all()
        stored = db_session.get(MaintenanceSchedule, schedule.id)
        assert stored.last_completed_date == real_date(2026, 12, 2)
        # One calendar month from Tokyo's Dec 2; the UTC day (Dec 1) would give Jan 1.
        assert stored.next_due_date == real_date(2027, 1, 2)


class TestReadsUseTheCompanyDay:
    def test_a_pm_schedule_due_today_is_due_this_week_not_overdue_in_the_evening(
        self, client, db_session, planner_headers, frozen_clock
    ):
        make_schedule(db_session, next_due=LOCAL_DAY - timedelta(days=1))  # Nov 30: overdue either way
        make_schedule(db_session, next_due=LOCAL_DAY)  # Dec 1: due TODAY, not overdue
        make_schedule(db_session, next_due=LOCAL_DAY + timedelta(days=7))  # Dec 8: the window's last day
        make_schedule(db_session, next_due=LOCAL_DAY + timedelta(days=8))  # Dec 9: outside the window

        body = dashboard(client, planner_headers)
        # The UTC day (Dec 2) would read (2, 2): Dec 1 overdue, and Dec 9 inside the window.
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (1, 2)

    def test_a_work_order_due_today_is_not_overdue_on_the_dashboard_list_or_overdue_read(
        self, client, db_session, planner_headers, frozen_clock
    ):
        due_today = make_raw_wo(db_session, company_id=1, due=LOCAL_DAY)
        due_yesterday = make_raw_wo(db_session, company_id=1, due=LOCAL_DAY - timedelta(days=1))

        body = dashboard(client, planner_headers)
        assert (body["overdue_count"], body["due_this_week"]) == (1, 1)

        listed = client.get(WORK_ORDERS, headers=planner_headers)
        assert listed.status_code == 200, listed.text
        labels = {row["id"]: row["status"] for row in listed.json()}
        assert labels == {due_today.id: "scheduled", due_yesterday.id: "overdue"}

        overdue = client.get(f"{WORK_ORDERS}/overdue", headers=planner_headers)
        assert overdue.status_code == 200, overdue.text
        assert [row["id"] for row in overdue.json()] == [due_yesterday.id]

    def test_completed_this_month_starts_at_the_local_first_of_the_month(
        self, client, db_session, planner_headers, frozen_clock
    ):
        """``completed_at`` is a UTC instant. 03:00 UTC on Dec 1 is still 9 PM on Nov 30 in
        Chicago -- last month -- while 07:00 UTC on Dec 1 is 1 AM on Dec 1."""
        make_completed_wo(db_session, completed_at=real_datetime(2026, 12, 1, 3, 0), total_cost=100.0)
        make_completed_wo(db_session, completed_at=real_datetime(2026, 12, 1, 7, 0), total_cost=40.0)

        body = dashboard(client, planner_headers)
        assert body["completed_this_month"] == 1
        assert body["total_cost_month"] == 40.0

    def test_create_defaults_are_the_local_day(self, client, db_session, planner_headers, frozen_clock):
        wc = make_wc(db_session)
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"work_center_id": wc.id, "title": "Corrective"})
        assert wo.status_code == 200, wo.text
        assert (wo.json()["scheduled_date"], wo.json()["due_date"]) == ("2026-12-01", "2026-12-01")

        schedule = client.post(
            SCHEDULES,
            headers=planner_headers,
            json={"name": "Synthetic PM", "work_center_id": wc.id, "frequency": "monthly"},
        )
        assert schedule.status_code == 200, schedule.text
        assert schedule.json()["next_due_date"] == "2026-12-01"
