"""The daily preventive-maintenance alerts (``check_maintenance_due_task``, owner decisions 2026-11-20).

* ``maintenance.overdue`` for every overdue PM schedule and maintenance work order -- recurring, so a
  recipient with an unread alert for an item is not alerted again until they read it;
* ``maintenance.due`` ONCE per due cycle per item, when it comes within 7 days (an item that entered
  the window while the job was not running is alerted on the next run);
* what counts is ``maintenance_due_service`` -- uncovered active schedules (a held work order's
  schedule included) and unstarted work orders -- on the COMPANY's day;
* recipients are the company's active Maintenance-department users plus its active Admins, once each;
* tenant-scoped end to end, and one tenant's failure never stops the next.

The task runs against the test session (``SessionLocal`` patched) with the REAL dispatcher, so the
assertions read the stored ``Notification`` rows; only the Redis dedup reservation is stubbed. Every
clock the task and the maintenance endpoints read is frozen, and so are the timestamps the
once-per-cycle rule compares: each notification's ``created_at`` and each delivery log's ``sent_at``
(server defaults otherwise), and each schedule's and work order's ``updated_at`` (``datetime.utcnow``
otherwise) -- a change to the record starts a new cycle.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

import app.api.endpoints.maintenance as maintenance_endpoints
import app.core.time_utils as time_utils
import app.jobs.notification_jobs as notification_jobs
import app.services.maintenance_due_service as due_service
import app.services.notification_dispatch as dispatch
from app.models.company import Company
from app.models.maintenance import MaintenanceFrequency, MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.notification import Notification, NotificationLog, NotificationPreference
from app.models.user import User, UserRole
from app.services import notification_links
from tests.api.test_maintenance_assets import make_asset, make_wc
from tests.api.test_maintenance_company_day import FrozenDate, FrozenDatetime, _FrozenClock
from tests.api.test_maintenance_pm_roll_forward import make_schedule
from tests.api.test_receiving_compliance import _next, headers_for, make_user

pytestmark = [pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
DUE, OVERDUE = "maintenance.due", "maintenance.overdue"
# The morning the cron runs: hour 12, minute 15 in the worker container's zone -- 12:15 UTC while TZ is
# unset, which is 6:15 AM Central, the same calendar day.
D = date(2026, 12, 9)


# --------------------------------------------------------------------------- fixtures


class Clock:
    """Every clock the task and the maintenance endpoints read, and the timestamps rows get."""

    def at(self, instant: datetime) -> None:
        _FrozenClock.instant = instant

    def day(self, day: date, hour: int = 12, minute: int = 15) -> None:
        """``day`` at ``hour``:``minute`` UTC -- 12:15 is the cron's run; 18:00 is noon Central, when the
        floor completes or edits a PM, the same company day."""
        self.at(datetime.combine(day, time(hour, minute), tzinfo=timezone.utc))


def _frozen_utc() -> datetime:
    return _FrozenClock.instant.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def clock(monkeypatch, db_session):
    monkeypatch.setattr(_FrozenClock, "instant", datetime.combine(D, time(12, 15), tzinfo=timezone.utc))
    monkeypatch.setattr(time_utils, "datetime", FrozenDatetime)
    monkeypatch.setattr(maintenance_endpoints, "datetime", FrozenDatetime)
    monkeypatch.setattr(maintenance_endpoints, "date", FrozenDate)

    def stamp_notification(mapper, connection, target):
        target.created_at = _frozen_utc()

    def stamp_log(mapper, connection, target):
        target.sent_at = _frozen_utc()

    def stamp_new_record(mapper, connection, target):
        target.updated_at = _frozen_utc()

    def stamp_changed_record(mapper, connection, target):
        # Only a real change: ``onupdate`` fires only when an UPDATE is issued.
        if object_session(target).is_modified(target, include_collections=False):
            target.updated_at = _frozen_utc()

    listeners = [
        (Notification, "before_insert", stamp_notification),
        (NotificationLog, "before_insert", stamp_log),
        (MaintenanceSchedule, "before_insert", stamp_new_record),
        (MaintenanceSchedule, "before_update", stamp_changed_record),
        (MaintenanceWorkOrder, "before_insert", stamp_new_record),
        (MaintenanceWorkOrder, "before_update", stamp_changed_record),
    ]
    for target, name, listener in listeners:
        event.listen(target, name, listener)
    try:
        yield Clock()
    finally:
        for target, name, listener in listeners:
            event.remove(target, name, listener)


class _KeepOpen:
    """The test session, handed to the task with ``close()`` disarmed: the task closes its session when
    it ends, which would detach every row the test still reads (the real dispatcher commits, so they
    are expired too)."""

    def __init__(self, session: Session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@pytest.fixture
def run(monkeypatch, db_session, clock):
    """Run the task once against the test session; returns its totals."""
    monkeypatch.setattr(notification_jobs, "SessionLocal", lambda: _KeepOpen(db_session))
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(dispatch, "_enqueue_email", AsyncMock(return_value=None))

    def _run():
        return asyncio.run(notification_jobs.check_maintenance_due_task())

    return _run


def ensure_company(db: Session, company_id: int) -> None:
    """The company, active, on Central time (a test may move it to another zone afterwards)."""
    company = db.get(Company, company_id)
    if company is None:
        company = Company(id=company_id, name=f"Co {company_id}", slug=f"pm-alerts-{company_id}", is_active=True)
        company.timezone = "America/Chicago"
        db.add(company)
        db.commit()


def make_person(db: Session, *, company_id=COMPANY_A, role=UserRole.OPERATOR, department=None, active=True) -> User:
    ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"pm-alert-{n}@co{company_id}.test",
        employee_id=f"PMA-{n:05d}",
        first_name="Pm",
        last_name=f"Person{n}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        department=department,
        is_active=active,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def tech(db: Session, company_id=COMPANY_A) -> User:
    """A Maintenance-department technician: the core recipient."""
    return make_person(db, company_id=company_id, department="Maintenance")


def schedule(db: Session, due: date, *, company_id=COMPANY_A, active=True, **kwargs) -> MaintenanceSchedule:
    ensure_company(db, company_id)
    return make_schedule(db, company_id=company_id, next_due=due, is_active=active, **kwargs)


def work_order(
    db: Session,
    due: date,
    *,
    company_id=COMPANY_A,
    status=MaintenanceStatus.SCHEDULED,
    schedule_row=None,
    title="Replace hydraulic filter",
) -> MaintenanceWorkOrder:
    ensure_company(db, company_id)
    wo = MaintenanceWorkOrder(
        company_id=company_id,
        schedule_id=schedule_row.id if schedule_row is not None else None,
        work_center_id=(
            schedule_row.work_center_id if schedule_row is not None else make_wc(db, company_id=company_id).id
        ),
        wo_number=f"MWO-ALERT-{_next():05d}",
        title=title,
        status=status,
        due_date=due,
        requires_shutdown=False,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def alerts(db: Session, **filters):
    db.expire_all()
    query = db.query(Notification).filter(Notification.event_key.in_([DUE, OVERDUE]))
    for key, value in filters.items():
        query = query.filter(getattr(Notification, key) == value)
    return query.order_by(Notification.id).all()


def keyed(db: Session, **filters):
    """``{(event_key, related_type, related_id, user_id): count}`` -- what the inbox holds."""
    counts = {}
    for row in alerts(db, **filters):
        key = (row.event_key, row.related_type, row.related_id, row.user_id)
        counts[key] = counts.get(key, 0) + 1
    return counts


# --------------------------------------------------------------------------- what alerts


class TestWhatAlerts:
    def test_overdue_and_due_schedules_and_work_orders_each_alert_once(self, db_session, run):
        person = tech(db_session)
        late_pm = schedule(db_session, D - timedelta(days=3))
        soon_pm = schedule(db_session, D + timedelta(days=3))
        late_wo = work_order(db_session, D - timedelta(days=1))
        soon_wo = work_order(db_session, D + timedelta(days=7), status=MaintenanceStatus.OVERDUE)

        totals = run()

        assert totals == {"due": 2, "overdue": 2, "companies": 1}
        assert keyed(db_session) == {
            (OVERDUE, "MaintenanceSchedule", late_pm.id, person.id): 1,
            (OVERDUE, "MaintenanceWorkOrder", late_wo.id, person.id): 1,
            (DUE, "MaintenanceSchedule", soon_pm.id, person.id): 1,
            (DUE, "MaintenanceWorkOrder", soon_wo.id, person.id): 1,
        }
        links = {(row.related_type, row.link) for row in alerts(db_session)}
        assert links == {
            ("MaintenanceSchedule", notification_links.MAINTENANCE_SCHEDULES),
            ("MaintenanceWorkOrder", notification_links.MAINTENANCE_WORK_ORDERS),
        }
        assert {row.company_id for row in alerts(db_session)} == {COMPANY_A}

    def test_the_window_is_today_through_seven_days_and_nothing_further(self, db_session, run):
        tech(db_session)
        today = schedule(db_session, D)
        last = schedule(db_session, D + timedelta(days=7))
        beyond = schedule(db_session, D + timedelta(days=8))
        far = work_order(db_session, D + timedelta(days=30))

        run()

        alerted = {(row.event_key, row.related_id) for row in alerts(db_session)}
        assert alerted == {(DUE, today.id), (DUE, last.id)}
        assert beyond.id not in {row.related_id for row in alerts(db_session)}
        assert far.id not in {row.related_id for row in alerts(db_session, related_type="MaintenanceWorkOrder")}

    @pytest.mark.parametrize(
        "covering,expected",
        [
            (MaintenanceStatus.SCHEDULED, "work order"),
            (MaintenanceStatus.OVERDUE, "work order"),
            (MaintenanceStatus.IN_PROGRESS, "nothing"),
        ],
    )
    def test_a_covered_schedule_alerts_only_through_its_work_order(self, db_session, run, covering, expected):
        """A scheduled or overdue work order represents its PM (alerted by its own due date); one in
        progress is being worked. Either way the schedule is not alerted a second time."""
        tech(db_session)
        pm = schedule(db_session, D - timedelta(days=3))
        wo = work_order(db_session, D - timedelta(days=3), status=covering, schedule_row=pm)

        run()

        related = {(row.related_type, row.related_id) for row in alerts(db_session)}
        if expected == "work order":
            assert related == {("MaintenanceWorkOrder", wo.id)}
        else:
            assert related == set()

    def test_a_held_work_orders_schedule_alerts_as_an_overdue_schedule(self, db_session, run):
        """A held PM is not done (owner decision 2026-11-17): it alerts by the SCHEDULE's own date."""
        person = tech(db_session)
        pm = schedule(db_session, D - timedelta(days=2))
        held = work_order(db_session, D + timedelta(days=20), status=MaintenanceStatus.ON_HOLD, schedule_row=pm)

        run()

        assert keyed(db_session) == {(OVERDUE, "MaintenanceSchedule", pm.id, person.id): 1}
        assert held.id not in {row.related_id for row in alerts(db_session, related_type="MaintenanceWorkOrder")}

    def test_inactive_schedules_and_finished_work_orders_never_alert(self, db_session, run):
        tech(db_session)
        schedule(db_session, D - timedelta(days=5), active=False)
        schedule(db_session, D + timedelta(days=2), active=False)
        for status_ in (MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED, MaintenanceStatus.IN_PROGRESS):
            work_order(db_session, D - timedelta(days=4), status=status_)
            work_order(db_session, D + timedelta(days=2), status=status_)
        schedule(db_session, None)  # no due date at all

        totals = run()

        assert alerts(db_session) == []
        assert totals == {"due": 0, "overdue": 0, "companies": 0}

    def test_nothing_is_sent_without_a_recipient(self, db_session, run):
        make_person(db_session, role=UserRole.SUPERVISOR)  # not a recipient
        schedule(db_session, D - timedelta(days=1))
        assert run() == {"due": 0, "overdue": 0, "companies": 0}
        assert alerts(db_session) == []


# --------------------------------------------------------------------------- the company's day


class TestTheCompanysDay:
    def test_at_8_pm_central_today_is_still_today(self, db_session, run, clock):
        """2026-12-02 02:00 UTC is 8 PM on Dec 1 in Chicago. A PM due Dec 1 is DUE TODAY there, not
        overdue as the UTC day (Dec 2) would make it; one due Nov 30 is one day overdue."""
        clock.at(datetime(2026, 12, 2, 2, 0, tzinfo=timezone.utc))
        tech(db_session)
        today_pm = schedule(db_session, date(2026, 12, 1))
        yesterday_pm = schedule(db_session, date(2026, 11, 30))
        today_wo = work_order(db_session, date(2026, 12, 1))

        run()

        by_item = {(row.related_type, row.related_id): row for row in alerts(db_session)}
        assert by_item[("MaintenanceSchedule", today_pm.id)].event_key == DUE
        assert by_item[("MaintenanceSchedule", today_pm.id)].title == f"PM due today: {today_pm.name}"
        assert by_item[("MaintenanceWorkOrder", today_wo.id)].event_key == DUE
        assert by_item[("MaintenanceWorkOrder", today_wo.id)].title == (
            f"Maintenance work order due today: {today_wo.wo_number}"
        )
        overdue = by_item[("MaintenanceSchedule", yesterday_pm.id)]
        assert overdue.event_key == OVERDUE
        assert overdue.body.startswith("Was due Nov 30, 2026 (1 day ago)")

    def test_a_company_east_of_utc_is_judged_on_its_own_later_day(self, db_session, run, clock):
        """20:00 UTC on Dec 1 is already Dec 2 in Tokyo: a PM due Dec 1 is overdue THERE."""
        clock.at(datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc))
        tech(db_session)
        db_session.get(Company, COMPANY_A).timezone = "Asia/Tokyo"
        db_session.commit()
        pm = schedule(db_session, date(2026, 12, 1))

        run()

        [row] = alerts(db_session)
        assert (row.event_key, row.related_id) == (OVERDUE, pm.id)


# --------------------------------------------------------------------------- recipients


class TestRecipients:
    def test_the_maintenance_department_and_the_admins_once_each(self, db_session, run):
        in_dept = tech(db_session)
        admin = make_person(db_session, role=UserRole.ADMIN, department="Office")
        admin_in_dept = make_person(db_session, role=UserRole.ADMIN, department="Maintenance")
        make_person(db_session, department="Maintenance", active=False)
        make_person(db_session, role=UserRole.ADMIN, active=False)
        make_person(db_session, role=UserRole.SUPERVISOR)
        make_person(db_session, role=UserRole.OPERATOR, department="Production")
        make_person(db_session, role=UserRole.MANAGER)
        make_person(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        make_person(db_session, company_id=COMPANY_B, department="Maintenance")
        pm = schedule(db_session, D - timedelta(days=1))

        run()

        rows = alerts(db_session, related_id=pm.id)
        assert sorted(row.user_id for row in rows) == sorted([in_dept.id, admin.id, admin_in_dept.id])
        assert {row.company_id for row in rows} == {COMPANY_A}

    def test_maintenance_alert_recipients_dedupes_and_stays_in_tenant(self, db_session):
        in_dept = tech(db_session)
        admin_in_dept = make_person(db_session, role=UserRole.ADMIN, department="Maintenance")
        make_person(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        recipients = notification_jobs.maintenance_alert_recipients(db_session, COMPANY_A)
        assert sorted(user.id for user in recipients) == sorted([in_dept.id, admin_in_dept.id])


# --------------------------------------------------------------------------- once per cycle / recurring


class TestDueIsOncePerCycle:
    def test_the_same_day_and_the_next_day_add_nothing(self, db_session, run, clock):
        people = [tech(db_session), make_person(db_session, role=UserRole.ADMIN)]
        pm = schedule(db_session, D + timedelta(days=3))
        wo = work_order(db_session, D + timedelta(days=5))

        first = run()
        again = run()
        clock.day(D + timedelta(days=1))
        next_day = run()

        assert (first["due"], again["due"], next_day["due"]) == (2, 0, 0)
        assert keyed(db_session) == {
            **{(DUE, "MaintenanceSchedule", pm.id, person.id): 1 for person in people},
            **{(DUE, "MaintenanceWorkOrder", wo.id, person.id): 1 for person in people},
        }

    def test_a_late_run_alerts_an_item_already_inside_the_window(self, db_session, run):
        """The item came within 7 days at T-7; the job did not run until T-3. It is alerted now."""
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=3))
        run()
        [row] = alerts(db_session)
        assert (row.event_key, row.related_id, row.user_id) == (DUE, pm.id, person.id)
        assert row.title == f"PM due in 3 days: {pm.name}"

    def test_a_new_due_date_starts_a_new_cycle(self, db_session, run, clock):
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=3))
        run()

        # Completed and rolled forward a month: outside the window for a while, then inside again.
        pm.next_due_date = D + timedelta(days=33)
        db_session.commit()
        clock.day(D + timedelta(days=20))
        assert run()["due"] == 0
        clock.day(D + timedelta(days=27))
        assert run()["due"] == 1
        clock.day(D + timedelta(days=28))
        assert run()["due"] == 0

        rows = alerts(db_session, related_id=pm.id)
        assert [row.title for row in rows] == [f"PM due in 3 days: {pm.name}", f"PM due in 6 days: {pm.name}"]
        assert {row.user_id for row in rows} == {person.id}

    def test_an_item_that_turns_overdue_gets_the_overdue_alert(self, db_session, run, clock):
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=1))
        run()
        clock.day(D + timedelta(days=2))
        run()
        assert keyed(db_session) == {
            (DUE, "MaintenanceSchedule", pm.id, person.id): 1,
            (OVERDUE, "MaintenanceSchedule", pm.id, person.id): 1,
        }


class TestAnEmailOnlyRecipientIsAlsoAlertedOncePerCycle:
    """A recipient who turned in-app OFF for ``maintenance.due`` and kept email ON has no inbox row for
    the once-per-cycle check to find; the email delivery log counts too, or they would be emailed every
    day of the window."""

    def email_only(self, db: Session, user: User) -> None:
        db.add(
            NotificationPreference(
                user_id=user.id,
                company_id=user.company_id,
                preferences={DUE: {"in_app": False, "email": True, "sms": False, "digest": False}},
            )
        )
        db.commit()

    def email_logs(self, db: Session, user: User):
        db.expire_all()
        return (
            db.query(NotificationLog)
            .filter(NotificationLog.user_id == user.id, NotificationLog.event_type == DUE)
            .order_by(NotificationLog.id)
            .all()
        )

    def test_two_consecutive_days_send_one_email(self, db_session, run, clock):
        person = tech(db_session)
        self.email_only(db_session, person)
        pm = schedule(db_session, D + timedelta(days=4))

        first = run()
        clock.day(D + timedelta(days=1))
        second = run()

        assert (first["due"], second["due"]) == (1, 0)
        [log] = self.email_logs(db_session, person)
        assert (log.channel, log.related_type, log.related_id) == ("email", "MaintenanceSchedule", pm.id)
        assert alerts(db_session) == []  # in-app is off: no inbox row at all

    def test_a_new_cycle_emails_again(self, db_session, run, clock):
        person = tech(db_session)
        self.email_only(db_session, person)
        pm = schedule(db_session, D + timedelta(days=4))
        run()
        pm.next_due_date = D + timedelta(days=34)
        db_session.commit()
        clock.day(D + timedelta(days=30))
        run()
        clock.day(D + timedelta(days=31))
        run()
        assert len(self.email_logs(db_session, person)) == 2

    def test_beside_an_in_app_recipient_each_hears_once(self, db_session, run, clock):
        emailed = tech(db_session)
        self.email_only(db_session, emailed)
        inboxed = make_person(db_session, role=UserRole.ADMIN)
        pm = schedule(db_session, D + timedelta(days=2))

        run()
        clock.day(D + timedelta(days=1))
        run()

        assert len(self.email_logs(db_session, emailed)) == 1
        assert keyed(db_session) == {(DUE, "MaintenanceSchedule", pm.id, inboxed.id): 1}


class TestOverdueIsRecurring:
    def test_one_unread_row_per_person_until_it_is_read(self, db_session, run, clock):
        people = [tech(db_session), make_person(db_session, role=UserRole.ADMIN)]
        pm = schedule(db_session, D - timedelta(days=4))
        wo = work_order(db_session, D - timedelta(days=2))

        run()
        run()
        clock.day(D + timedelta(days=1))
        run()
        expected = {
            **{(OVERDUE, "MaintenanceSchedule", pm.id, person.id): 1 for person in people},
            **{(OVERDUE, "MaintenanceWorkOrder", wo.id, person.id): 1 for person in people},
        }
        assert keyed(db_session) == expected

        # The first person reads the schedule's alert: the next run raises it again for them only.
        [read] = alerts(db_session, related_type="MaintenanceSchedule", user_id=people[0].id)
        read.is_read = True
        db_session.commit()
        run()
        expected[(OVERDUE, "MaintenanceSchedule", pm.id, people[0].id)] = 2
        assert keyed(db_session) == expected
        unread = alerts(db_session, related_type="MaintenanceSchedule", user_id=people[0].id, is_read=False)
        assert len(unread) == 1


# --------------------------------------------------------------------------- what an alert says


class TestWhatAnAlertSays:
    def test_titles_and_bodies_name_the_item_and_its_own_companys_target(self, db_session, run):
        tech(db_session)
        lathe = make_wc(db_session, code="CNC-77")
        forklift = make_asset(db_session, tag="FL-12")
        late_pm = schedule(db_session, D - timedelta(days=2), work_center_id=lathe.id)
        soon_pm = schedule(db_session, D + timedelta(days=1), work_center_id=None, asset_id=forklift.id)
        late_wo = work_order(db_session, D - timedelta(days=1), title="Grease the turret")

        run()

        by_item = {(row.related_type, row.related_id): row for row in alerts(db_session)}
        late = by_item[("MaintenanceSchedule", late_pm.id)]
        assert late.title == f"PM overdue: {late_pm.name}"
        assert late.body == "Was due Dec 7, 2026 (2 days ago) on CNC-77. No work order is under way for it."
        soon = by_item[("MaintenanceSchedule", soon_pm.id)]
        assert soon.title == f"PM due in 1 day: {soon_pm.name}"
        assert soon.body == "Due Dec 10, 2026 on FL-12."
        wo_row = by_item[("MaintenanceWorkOrder", late_wo.id)]
        assert wo_row.title == f"Maintenance work order overdue: {late_wo.wo_number}"
        assert wo_row.body == "Grease the turret: was due Dec 8, 2026 (1 day ago) and is not started."

    def test_another_companys_target_code_is_never_read_out(self, db_session, run):
        """A legacy schedule of company A may point at company B's machine (``company_id`` is part of no
        FK). Its alert names the schedule, never the foreign machine."""
        tech(db_session)
        foreign_wc = make_wc(db_session, company_id=COMPANY_B, code="ACME-SECRET-WC")
        legacy = MaintenanceSchedule(
            company_id=COMPANY_A,
            work_center_id=foreign_wc.id,
            name="Legacy PM",
            next_due_date=D - timedelta(days=1),
            is_active=True,
        )
        db_session.add(legacy)
        db_session.commit()

        run()

        [row] = alerts(db_session)
        assert row.related_id == legacy.id and row.title == "PM overdue: Legacy PM"
        assert "ACME-SECRET" not in (row.title + (row.body or ""))
        assert row.body == "Was due Dec 8, 2026 (1 day ago). No work order is under way for it."


# --------------------------------------------------------------------------- tenancy


class TestTenantIsolation:
    def test_each_companys_pm_alerts_only_its_own_people(self, db_session, run):
        person_a, person_b = tech(db_session, COMPANY_A), tech(db_session, COMPANY_B)
        admin_a = make_person(db_session, role=UserRole.ADMIN)
        admin_b = make_person(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        pm_a = schedule(db_session, D - timedelta(days=1))
        pm_b = schedule(db_session, D - timedelta(days=1), company_id=COMPANY_B)
        wo_a = work_order(db_session, D + timedelta(days=2))
        wo_b = work_order(db_session, D + timedelta(days=2), company_id=COMPANY_B)

        totals = run()

        assert totals == {"due": 2, "overdue": 2, "companies": 2}
        people = {COMPANY_A: {person_a.id, admin_a.id}, COMPANY_B: {person_b.id, admin_b.id}}
        items = {COMPANY_A: {pm_a.id, wo_a.id}, COMPANY_B: {pm_b.id, wo_b.id}}
        names = {COMPANY_A: (pm_a.name, wo_a.wo_number), COMPANY_B: (pm_b.name, wo_b.wo_number)}
        for row in alerts(db_session):
            assert row.user_id in people[row.company_id], row
            assert row.related_id in items[row.company_id], row
            other = COMPANY_B if row.company_id == COMPANY_A else COMPANY_A
            assert not any(name in row.title + (row.body or "") for name in names[other]), row
        assert len(alerts(db_session)) == 8

    def test_one_companys_failure_does_not_stop_the_next(self, db_session, run, monkeypatch):
        tech(db_session, COMPANY_A)
        person_b = tech(db_session, COMPANY_B)
        schedule(db_session, D - timedelta(days=1))
        pm_b = schedule(db_session, D - timedelta(days=1), company_id=COMPANY_B)
        real = due_service.maintenance_due
        attempted = []

        def failing_for_a(db, company_id, today, **kwargs):
            attempted.append(company_id)
            if company_id == COMPANY_A:
                raise RuntimeError("synthetic failure for one tenant")
            return real(db, company_id, today, **kwargs)

        monkeypatch.setattr(due_service, "maintenance_due", failing_for_a)

        totals = run()

        assert sorted(attempted) == [COMPANY_A, COMPANY_B]
        assert totals == {"due": 0, "overdue": 1, "companies": 1}
        assert keyed(db_session) == {(OVERDUE, "MaintenanceSchedule", pm_b.id, person_b.id): 1}


# --------------------------------------------------------------------------- the server's own created_at


def test_once_per_cycle_holds_with_the_real_created_at(db_session, monkeypatch):
    """Every other test here pins ``created_at``; this one keeps the server default (and the real
    clock), so the once-per-cycle comparison is exercised against the timestamp the database writes."""
    monkeypatch.setattr(notification_jobs, "SessionLocal", lambda: _KeepOpen(db_session))
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    person = tech(db_session)
    pm = schedule(db_session, company_today_of(db_session) + timedelta(days=3))

    first = asyncio.run(notification_jobs.check_maintenance_due_task())
    second = asyncio.run(notification_jobs.check_maintenance_due_task())

    assert (first["due"], second["due"]) == (1, 0)
    assert keyed(db_session) == {(DUE, "MaintenanceSchedule", pm.id, person.id): 1}


def company_today_of(db: Session):
    from app.services.company_time import company_today

    return company_today(db, COMPANY_A)


# --------------------------------------------------------------------------- the cron and the catalog


def test_the_cron_is_registered_once_and_selectable_alone():
    from app.worker import ALL_CRON_JOBS, WorkerSettings, check_maintenance_due_job, select_cron_jobs

    matches = [job for job in ALL_CRON_JOBS if job.coroutine is check_maintenance_due_job]
    assert len(matches) == 1
    # Hour 12, minute 15 in the worker container's zone (12:15 UTC while TZ is unset). The day it judges
    # is each company's own (company_today), whatever the hour.
    assert (matches[0].hour, matches[0].minute) == (12, 15)
    assert WorkerSettings.functions.count(check_maintenance_due_job) == 1

    selected = select_cron_jobs("check_maintenance_due_job")
    assert len(selected) == 1 and selected[0].coroutine is check_maintenance_due_job
    assert selected[0].name == "cron:check_maintenance_due_job"
    assert len(ALL_CRON_JOBS) > len(selected)


def test_the_worker_delegates_to_the_task(monkeypatch):
    from app.worker import check_maintenance_due_job

    receipt = {"due": 1, "overdue": 2, "companies": 1}
    calls = []

    async def fake_task():
        calls.append(True)
        return receipt

    monkeypatch.setattr(notification_jobs, "check_maintenance_due_task", fake_task)
    assert asyncio.run(check_maintenance_due_job({"job_id": "maintenance-due-test"})) is receipt
    assert calls == [True]


def test_the_catalog_entries():
    from app.services.notification_catalog import CHANNEL_IN_APP, get_entry

    due, overdue = get_entry(DUE), get_entry(OVERDUE)
    assert due.recurring is False and overdue.recurring is True
    for entry in (due, overdue):
        assert entry.roles == (UserRole.ADMIN,)
        assert entry.departments == ("Maintenance",)
        assert entry.default_channels == frozenset({CHANNEL_IN_APP})
        assert entry.source_event_types == ()
    assert notification_links.MAINTENANCE_SCHEDULES in notification_links.ALL_LINK_TEMPLATES
    assert notification_links.MAINTENANCE_WORK_ORDERS in notification_links.ALL_LINK_TEMPLATES


# --------------------------------------------------------------------------- a change starts a new cycle

WORK_ORDERS = "/api/v1/maintenance/work-orders"
SCHEDULES = "/api/v1/maintenance/schedules"


@pytest.fixture
def planner(db_session):
    """A supervisor's headers: plans and completes PM, and is not an alert recipient."""
    return headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_A))


def start_pm(client, headers, pm: MaintenanceSchedule, due: date = None) -> dict:
    body = {"schedule_id": pm.id, "title": "Synthetic PM"}
    if due is not None:
        body["due_date"] = due.isoformat()  # the Maintenance page's Start PM copies the schedule's date
    created = client.post(WORK_ORDERS, headers=headers, json=body)
    assert created.status_code == 200, created.text
    return created.json()


def complete_pm(client, headers, pm: MaintenanceSchedule) -> dict:
    wo = start_pm(client, headers, pm)
    done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=headers, json={})
    assert done.status_code == 200, done.text
    return done.json()


def due_titles(db: Session, related_type: str, related_id: int, user: User):
    return [
        row.title
        for row in alerts(db, event_key=DUE, related_type=related_type, related_id=related_id, user_id=user.id)
    ]


class TestAChangeToTheRecordStartsANewCycle:
    def test_a_weekly_pm_completed_on_its_due_day_is_announced_once_for_its_next_cycle(
        self, db_session, run, clock, client, planner
    ):
        person = tech(db_session)
        pm = schedule(db_session, D, frequency=MaintenanceFrequency.WEEKLY)
        assert run()["due"] == 1  # D: "due today"
        clock.day(D, 18, 0)
        assert complete_pm(client, planner, pm)["schedule_next_due_date"] == (D + timedelta(days=7)).isoformat()

        new_per_day = []
        for offset in range(1, 8):
            clock.day(D + timedelta(days=offset))
            new_per_day.append(run()["due"])

        # D+7 is inside the window from D+1 on: the next cycle is announced on D+1, and only then. (By
        # the cycle-start rule alone the alert from D would have stood in for it: no announcement.)
        assert new_per_day == [1, 0, 0, 0, 0, 0, 0]
        assert due_titles(db_session, "MaintenanceSchedule", pm.id, person) == [
            f"PM due today: {pm.name}",
            f"PM due in 6 days: {pm.name}",
        ]

    def test_a_daily_pm_is_announced_once_per_completion(self, db_session, run, clock, client, planner):
        person = tech(db_session)
        pm = schedule(db_session, D, frequency=MaintenanceFrequency.DAILY)

        per_day = []
        for offset in range(10):
            day = D + timedelta(days=offset)
            clock.day(day)
            per_day.append(run()["due"])
            clock.day(day, 18, 0)
            complete_pm(client, planner, pm)

        # One per cycle: the cycle-start rule alone would give [1, 0, 0, 0, 0, 0, 0, 0, 1, 0].
        assert per_day == [1] * 10
        assert due_titles(db_session, "MaintenanceSchedule", pm.id, person) == [f"PM due today: {pm.name}"] * 10

    def test_a_due_date_moved_earlier_is_announced_again_with_the_new_date(
        self, db_session, run, clock, client, planner
    ):
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=7))
        assert run()["due"] == 1
        clock.day(D, 18, 0)
        moved = client.put(
            f"{SCHEDULES}/{pm.id}", headers=planner, json={"next_due_date": (D + timedelta(days=2)).isoformat()}
        )
        assert moved.status_code == 200, moved.text

        clock.day(D + timedelta(days=1))
        assert run()["due"] == 1
        clock.day(D + timedelta(days=2))
        assert run()["due"] == 0

        assert due_titles(db_session, "MaintenanceSchedule", pm.id, person) == [
            f"PM due in 7 days: {pm.name}",
            f"PM due in 1 day: {pm.name}",
        ]
        newest = alerts(db_session, event_key=DUE, related_id=pm.id, user_id=person.id)[-1]
        assert newest.body.startswith("Due Dec 11, 2026")

    def test_any_schedule_edit_starts_a_new_cycle_even_one_that_leaves_the_due_date_alone(
        self, db_session, run, clock, client, planner
    ):
        """Accepted behavior (owner review 2026-11-20): the rule reads the record's last change, not
        which field changed, so an edit that moves nothing relevant still re-announces the PM once. A
        PUT that changes nothing at all writes no UPDATE and starts nothing."""
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=5))
        assert run()["due"] == 1

        clock.day(D, 18, 0)
        unchanged = client.put(
            f"{SCHEDULES}/{pm.id}", headers=planner, json={"next_due_date": (D + timedelta(days=5)).isoformat()}
        )
        assert unchanged.status_code == 200, unchanged.text
        clock.day(D + timedelta(days=1))
        assert run()["due"] == 0

        clock.day(D + timedelta(days=1), 18, 0)
        edited = client.put(f"{SCHEDULES}/{pm.id}", headers=planner, json={"estimated_duration_hours": 2.5})
        assert edited.status_code == 200, edited.text
        clock.day(D + timedelta(days=2))
        assert run()["due"] == 1
        clock.day(D + timedelta(days=3))
        assert run()["due"] == 0
        assert len(due_titles(db_session, "MaintenanceSchedule", pm.id, person)) == 2


class TestStartPmDoesNotAnnounceThePmTwice:
    def test_a_work_order_opened_after_its_schedules_alert_is_not_announced(
        self, db_session, run, clock, client, planner
    ):
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=5))
        assert run()["due"] == 1
        clock.day(D, 18, 0)
        wo = start_pm(client, planner, pm, due=D + timedelta(days=5))

        clock.day(D + timedelta(days=1))
        totals = run()

        # The schedule is now covered by its work order, and the work order borrows the schedule's
        # alert: by the work order's own record alone it would be announced here.
        assert totals["due"] == 0
        assert due_titles(db_session, "MaintenanceWorkOrder", wo["id"], person) == []
        assert len(due_titles(db_session, "MaintenanceSchedule", pm.id, person)) == 1

    def test_a_work_order_on_a_never_announced_schedule_is_announced(self, db_session, run, clock, client, planner):
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=3))
        clock.day(D, 11, 0)  # Start PM before the morning run: the schedule was never announced
        wo = start_pm(client, planner, pm, due=D + timedelta(days=3))

        clock.day(D)
        assert run()["due"] == 1
        assert due_titles(db_session, "MaintenanceWorkOrder", wo["id"], person) == [
            f"Maintenance work order due in 3 days: {wo['wo_number']}"
        ]
        assert due_titles(db_session, "MaintenanceSchedule", pm.id, person) == []

    def test_a_legacy_work_order_on_another_companys_schedule_does_not_borrow_its_alert(self, db_session, run, clock):
        person_a, person_b = tech(db_session, COMPANY_A), tech(db_session, COMPANY_B)
        foreign_pm = schedule(db_session, D + timedelta(days=3), company_id=COMPANY_B)
        run()
        assert len(due_titles(db_session, "MaintenanceSchedule", foreign_pm.id, person_b)) == 1

        legacy = work_order(db_session, D + timedelta(days=3), schedule_row=foreign_pm)  # company A's row
        clock.day(D + timedelta(days=1))
        run()

        [row] = alerts(db_session, event_key=DUE, related_type="MaintenanceWorkOrder", related_id=legacy.id)
        assert (row.company_id, row.user_id) == (COMPANY_A, person_a.id)
        assert foreign_pm.name not in row.title + (row.body or "")


class TestOverdueAcrossCycles:
    def test_an_unread_overdue_alert_holds_back_the_next_cycles_until_read(
        self, db_session, run, clock, client, planner
    ):
        """Documented (accepted) behavior: ``maintenance.overdue`` is recurring and keyed on the
        schedule, so a recipient who never read cycle 1's overdue alert is not alerted for cycle 2's
        until they read it."""
        person = tech(db_session)
        pm = schedule(db_session, D - timedelta(days=1), frequency=MaintenanceFrequency.WEEKLY)
        assert run()["overdue"] == 1
        clock.day(D, 18, 0)
        complete_pm(client, planner, pm)  # next due D+7

        clock.day(D + timedelta(days=8))  # overdue again: cycle 2
        assert run()["overdue"] == 1  # the task dispatched it...
        overdue_rows = alerts(db_session, event_key=OVERDUE, related_id=pm.id, user_id=person.id)
        assert len(overdue_rows) == 1  # ...and the unread cycle-1 row held back the inbox row

        overdue_rows[0].is_read = True
        db_session.commit()
        clock.day(D + timedelta(days=9))
        run()
        rows = alerts(db_session, event_key=OVERDUE, related_id=pm.id, user_id=person.id)
        assert len(rows) == 2 and rows[-1].is_read is False
        assert rows[-1].body.startswith("Was due Dec 16, 2026 (2 days ago)")


class TestTheChangeClockTolerance:
    @pytest.mark.parametrize(
        "alert_before_change,re_announced",
        [(timedelta(seconds=0.6), False), (timedelta(seconds=59), False), (timedelta(minutes=2), True)],
        ids=["same-second", "59s", "2min"],
    )
    def test_an_alert_stamped_up_to_a_minute_before_the_change_still_counts(
        self, db_session, run, clock, alert_before_change, re_announced
    ):
        """``updated_at`` is the app's clock (microseconds), an alert's ``created_at`` the database's
        (whole seconds on SQLite): an alert written right after a change can read as up to a second --
        allowed: a minute -- OLDER than the change. Within the minute it still counts; beyond it, it is
        an earlier cycle's alert."""
        person = tech(db_session)
        pm = schedule(db_session, D + timedelta(days=10))
        change = datetime.combine(D, time(12, 15, 0, 600_000), tzinfo=timezone.utc)
        clock.at(change)
        pm.next_due_date = D + timedelta(days=4)
        db_session.commit()
        db_session.refresh(pm)
        assert pm.updated_at == change.replace(tzinfo=None)

        clock.at(change - alert_before_change)
        assert run()["due"] == 1
        clock.day(D + timedelta(days=1))
        assert run()["due"] == (1 if re_announced else 0)
        assert len(due_titles(db_session, "MaintenanceSchedule", pm.id, person)) == (2 if re_announced else 1)


class TestTotalsAndRecipientsFromTheCatalog:
    def test_a_company_counts_only_when_it_got_an_alert(self, db_session, run, clock):
        tech(db_session)
        schedule(db_session, D + timedelta(days=3))
        assert run() == {"due": 1, "overdue": 0, "companies": 1}
        assert run() == {"due": 0, "overdue": 0, "companies": 0}

    def test_the_recipients_follow_the_catalog_entry(self, db_session, run, monkeypatch):
        """One place holds who hears: the catalog entry's departments and roles."""
        from dataclasses import replace

        from app.services import notification_catalog

        supervisor = make_person(db_session, role=UserRole.SUPERVISOR)
        planner_dept = make_person(db_session, department="Planning")
        tech(db_session)
        widened = replace(
            notification_catalog.CATALOG[DUE],
            roles=(UserRole.ADMIN, UserRole.SUPERVISOR),
            departments=("Maintenance", "Planning"),
        )
        monkeypatch.setitem(notification_catalog.CATALOG, DUE, widened)
        recipients = {user.id for user in notification_jobs.maintenance_alert_recipients(db_session, COMPANY_A)}
        assert {supervisor.id, planner_dept.id} <= recipients
