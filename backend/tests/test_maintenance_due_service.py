"""``app.services.maintenance_due_service.maintenance_due`` -- the ONE rule for due and overdue PM.

The Maintenance dashboard's four tiles and the daily ``check_maintenance_due_job`` alerts both read it,
so these pin the rule itself: the window (overdue before today; due today through today + window,
inclusive), which work orders cover a schedule (scheduled / overdue / in progress -- never on hold),
which work orders are due by their own date (not started), the ordering, and the tenant scope. The
last class holds the dashboard to the same lists.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.maintenance import (
    COVERING_MAINTENANCE_STATUSES,
    DUE_WORK_ORDER_STATUSES,
    OPEN_MAINTENANCE_STATUSES,
    MaintenanceStatus,
    MaintenanceWorkOrder,
)
from app.models.user import UserRole
from app.services.company_time import company_today
from app.services.maintenance_due_service import DUE_WINDOW_DAYS, maintenance_due
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_pm_roll_forward import make_schedule
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_user

pytestmark = [pytest.mark.requires_db]

TODAY = date(2026, 12, 9)
S = MaintenanceStatus


def wo(db: Session, due, *, status=S.SCHEDULED, schedule=None, company_id=1) -> MaintenanceWorkOrder:
    _ensure_company(db, company_id)
    row = MaintenanceWorkOrder(
        company_id=company_id,
        schedule_id=schedule.id if schedule is not None else None,
        work_center_id=schedule.work_center_id if schedule is not None else make_wc(db, company_id=company_id).id,
        wo_number=f"MWO-DUE-{_next():05d}",
        title="Synthetic maintenance",
        status=status,
        due_date=due,
        requires_shutdown=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ids(rows):
    return [row.id for row in rows]


def test_the_status_sets():
    assert DUE_WINDOW_DAYS == 7
    assert COVERING_MAINTENANCE_STATUSES == (S.SCHEDULED, S.OVERDUE, S.IN_PROGRESS)
    assert DUE_WORK_ORDER_STATUSES == (S.SCHEDULED, S.OVERDUE)
    # A held work order is open (it blocks a second one) but covers nothing: its PM is not done.
    assert set(COVERING_MAINTENANCE_STATUSES) == set(OPEN_MAINTENANCE_STATUSES) - {S.ON_HOLD}


class TestTheWindow:
    def test_schedules_overdue_before_today_due_today_through_seven_days(self, db_session):
        by_offset = {
            offset: make_schedule(db_session, next_due=TODAY + timedelta(days=offset))
            for offset in (-30, -1, 0, 1, 7, 8, 60)
        }
        due = maintenance_due(db_session, 1, TODAY)
        assert ids(due.schedules_overdue) == [by_offset[-30].id, by_offset[-1].id]
        assert ids(due.schedules_due) == [by_offset[0].id, by_offset[1].id, by_offset[7].id]
        assert (due.today, due.window_end) == (TODAY, TODAY + timedelta(days=7))

    def test_work_orders_by_their_own_due_date(self, db_session):
        by_offset = {offset: wo(db_session, TODAY + timedelta(days=offset)) for offset in (-2, -1, 0, 7, 8)}
        due = maintenance_due(db_session, 1, TODAY)
        assert ids(due.work_orders_overdue) == [by_offset[-2].id, by_offset[-1].id]
        assert ids(due.work_orders_due) == [by_offset[0].id, by_offset[7].id]

    def test_a_narrower_window(self, db_session):
        three = make_schedule(db_session, next_due=TODAY + timedelta(days=3))
        make_schedule(db_session, next_due=TODAY + timedelta(days=4))
        due = maintenance_due(db_session, 1, TODAY, window_days=3)
        assert ids(due.schedules_due) == [three.id] and due.window_end == TODAY + timedelta(days=3)

    def test_ordered_by_date_then_id(self, db_session):
        later = make_schedule(db_session, next_due=TODAY + timedelta(days=5))
        first_same = make_schedule(db_session, next_due=TODAY + timedelta(days=2))
        second_same = make_schedule(db_session, next_due=TODAY + timedelta(days=2))
        later_wo = wo(db_session, TODAY - timedelta(days=1))
        earlier_wo = wo(db_session, TODAY - timedelta(days=9))
        due = maintenance_due(db_session, 1, TODAY)
        assert ids(due.schedules_due) == [first_same.id, second_same.id, later.id]
        assert ids(due.work_orders_overdue) == [earlier_wo.id, later_wo.id]

    def test_undated_inactive_and_nothing_at_all(self, db_session):
        _ensure_company(db_session, 1)
        assert maintenance_due(db_session, 1, TODAY).empty
        make_schedule(db_session, next_due=None)
        make_schedule(db_session, next_due=TODAY - timedelta(days=1), is_active=False)
        wo(db_session, None)
        assert maintenance_due(db_session, 1, TODAY).empty
        make_schedule(db_session, next_due=TODAY)
        assert not maintenance_due(db_session, 1, TODAY).empty


class TestCoverage:
    @pytest.mark.parametrize("status_", list(MaintenanceStatus), ids=lambda s: s.name)
    def test_which_work_orders_cover_a_schedule_and_which_are_due(self, db_session, status_):
        pm = make_schedule(db_session, next_due=TODAY - timedelta(days=2))
        row = wo(db_session, TODAY - timedelta(days=2), status=status_, schedule=pm)
        due = maintenance_due(db_session, 1, TODAY)
        covered = status_ in (S.SCHEDULED, S.OVERDUE, S.IN_PROGRESS)
        assert ids(due.schedules_overdue) == ([] if covered else [pm.id])
        assert ids(due.work_orders_overdue) == ([row.id] if status_ in (S.SCHEDULED, S.OVERDUE) else [])

    def test_another_companys_row_never_covers_or_appears(self, db_session):
        """``company_id`` is part of no FK: a legacy company-2 work order may name company 1's schedule.
        It covers nothing in company 1, and company 2's own items never reach company 1's lists."""
        pm = make_schedule(db_session, next_due=TODAY - timedelta(days=1))
        wo(db_session, TODAY - timedelta(days=1), status=S.IN_PROGRESS, schedule=pm, company_id=2)
        make_schedule(db_session, company_id=2, next_due=TODAY)
        wo(db_session, TODAY, company_id=2)

        mine = maintenance_due(db_session, 1, TODAY)
        assert ids(mine.schedules_overdue) == [pm.id]
        assert mine.schedules_due == [] and mine.work_orders_due == [] and mine.work_orders_overdue == []
        theirs = maintenance_due(db_session, 2, TODAY)
        assert {row.company_id for row in theirs.schedules_due + theirs.work_orders_due} == {2}
        assert pm.id not in ids(theirs.schedules_overdue)


class TestTheDashboardReadsTheSameRule:
    def test_the_four_tiles_are_the_lengths_of_the_four_lists(self, client, db_session):
        headers = headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=1))
        today = company_today(db_session, 1)
        for offset in (-3, -1, 0, 4, 7, 8):
            make_schedule(db_session, next_due=today + timedelta(days=offset))
        covered = make_schedule(db_session, next_due=today - timedelta(days=5))
        wo(db_session, today - timedelta(days=5), schedule=covered)
        held = make_schedule(db_session, next_due=today + timedelta(days=2))
        wo(db_session, today + timedelta(days=30), status=S.ON_HOLD, schedule=held)
        for offset in (-2, 0, 6, 9):
            wo(db_session, today + timedelta(days=offset))
        wo(db_session, today - timedelta(days=1), status=S.IN_PROGRESS)

        body = client.get("/api/v1/maintenance/dashboard", headers=headers).json()
        due = maintenance_due(db_session, 1, today)

        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (2, 4)
        assert (body["overdue_count"], body["due_this_week"]) == (2, 2)
        assert (len(due.schedules_overdue), len(due.schedules_due)) == (2, 4)
        assert (len(due.work_orders_overdue), len(due.work_orders_due)) == (2, 2)
