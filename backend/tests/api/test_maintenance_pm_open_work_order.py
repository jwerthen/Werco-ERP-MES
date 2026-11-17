"""A PM schedule takes ONE open maintenance work order at a time, and a held PM is still due.

``POST /maintenance/work-orders`` with a ``schedule_id`` that already has an open work order
(scheduled / overdue / in progress / on hold) is **409** naming it: completing either of two
would advance the schedule, leaving the other to be performed -- or reported overdue -- against a
due date it no longer has. A completed or cancelled work order does not count; the check is
tenant-scoped (another company's row naming this schedule id never blocks it, and never shows up
in the refusal), a foreign ``schedule_id`` is still the flat 404, and it runs after the
target-mismatch 409.

``GET /maintenance/dashboard``: a PM whose only open work order is ON HOLD counts in
``schedules_overdue`` / ``schedules_due_this_week`` by the SCHEDULE's due date (owner decision
2026-11-17) -- an on-hold work order is on no work-order tile, so without this the PM would be
on none. A scheduled, overdue or in-progress work order still represents its PM, so nothing is
counted twice.
"""

from datetime import timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import MaintenanceStatus, MaintenanceWorkOrder
from app.models.user import UserRole
from app.services.company_time import company_today
from tests.api.test_maintenance_assets import make_wc
from tests.api.test_maintenance_pm_roll_forward import make_raw_wo, make_schedule
from tests.api.test_receiving_compliance import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/maintenance/work-orders"
DASHBOARD = "/api/v1/maintenance/dashboard"

OPEN_STATUSES = [
    (MaintenanceStatus.SCHEDULED, "scheduled"),
    (MaintenanceStatus.OVERDUE, "overdue"),
    (MaintenanceStatus.IN_PROGRESS, "in progress"),
    (MaintenanceStatus.ON_HOLD, "on hold"),
]


@pytest.fixture
def headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=1))


def start_pm(client, headers, schedule_id: int, **body):
    return client.post(WORK_ORDERS, headers=headers, json={"schedule_id": schedule_id, "title": "PM", **body})


def work_order_count(db: Session) -> int:
    db.expire_all()
    return db.query(MaintenanceWorkOrder).count()


def dashboard(client, headers) -> dict:
    resp = client.get(DASHBOARD, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestOneOpenWorkOrderPerSchedule:
    def test_a_second_start_pm_is_409_naming_the_open_one(self, client, db_session, headers):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        first = start_pm(client, headers, schedule.id)
        assert first.status_code == 200, first.text
        audit_rows = db_session.query(AuditLog).count()

        second = start_pm(client, headers, schedule.id)

        assert second.status_code == status.HTTP_409_CONFLICT, second.text
        detail = second.json()["detail"]
        assert isinstance(detail, str)  # a plain string the page shows as-is
        assert schedule.name in detail
        assert f"{first.json()['wo_number']} (scheduled)" in detail
        # Nothing written: no second work order, no audit row.
        assert work_order_count(db_session) == 1
        assert db_session.query(AuditLog).count() == audit_rows

    @pytest.mark.parametrize("open_status,label", OPEN_STATUSES)
    def test_every_open_status_blocks_and_is_named(self, client, db_session, headers, open_status, label):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        open_wo = make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=open_status)

        refused = start_pm(client, headers, schedule.id)

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert f"{open_wo.wo_number} ({label})" in refused.json()["detail"]
        assert work_order_count(db_session) == 1

    @pytest.mark.parametrize("ended", [MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED])
    def test_a_completed_or_cancelled_work_order_does_not_block(self, client, db_session, headers, ended):
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=ended)
        created = start_pm(client, headers, schedule.id)
        assert created.status_code == 200, created.text

    def test_the_next_pm_opens_once_the_open_one_ends(self, client, db_session, headers):
        """The page's own loop: Start PM, complete it, Start PM again; and cancel frees it too."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        first = start_pm(client, headers, schedule.id)
        assert first.status_code == 200, first.text
        completed = client.post(f"{WORK_ORDERS}/{first.json()['id']}/complete", headers=headers, json={})
        assert completed.status_code == 200, completed.text

        second = start_pm(client, headers, schedule.id)
        assert second.status_code == 200, second.text
        assert start_pm(client, headers, schedule.id).status_code == status.HTTP_409_CONFLICT
        cancelled = client.put(f"{WORK_ORDERS}/{second.json()['id']}", headers=headers, json={"status": "cancelled"})
        assert cancelled.status_code == 200, cancelled.text

        assert start_pm(client, headers, schedule.id).status_code == 200

    def test_another_schedule_and_work_orders_without_one_are_unaffected(self, client, db_session, headers):
        wc = make_wc(db_session)
        busy = make_schedule(db_session, next_due=company_today(db_session, 1), work_center_id=wc.id)
        other = make_schedule(db_session, next_due=company_today(db_session, 1), work_center_id=wc.id)
        assert start_pm(client, headers, busy.id).status_code == 200
        # A second schedule on the same machine is its own PM.
        assert start_pm(client, headers, other.id).status_code == 200
        # Corrective work orders carry no schedule: any number may be open on the machine.
        for _ in range(2):
            corrective = client.post(WORK_ORDERS, headers=headers, json={"work_center_id": wc.id, "title": "Fix"})
            assert corrective.status_code == 200, corrective.text

    def test_the_target_mismatch_409_is_answered_first(self, client, db_session, headers):
        """A request that is wrong twice gets the refusal about its own input, not the one about
        the schedule's state."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        open_wo = make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=MaintenanceStatus.ON_HOLD)
        elsewhere = make_wc(db_session)

        refused = start_pm(client, headers, schedule.id, work_center_id=elsewhere.id)

        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert "target must be its schedule's" in refused.json()["detail"]
        assert open_wo.wo_number not in refused.json()["detail"]


class TestTheOpenWorkOrderCheckStaysInTenant:
    def test_another_companys_row_naming_this_schedule_neither_blocks_nor_leaks(self, client, db_session, headers):
        """``company_id`` is part of no FK, so a legacy company-2 work order may name company 1's
        schedule id. Unscoped, the check would refuse company 1's own PM -- and print company 2's
        work-order number in the refusal."""
        schedule = make_schedule(db_session, next_due=company_today(db_session, 1))
        foreign = make_raw_wo(db_session, company_id=2, schedule_id=schedule.id, status=MaintenanceStatus.IN_PROGRESS)

        created = start_pm(client, headers, schedule.id)

        assert created.status_code == 200, created.text
        assert foreign.wo_number not in created.text

    def test_company_bs_open_pm_never_blocks_company_a(self, client, db_session, headers):
        schedule_b = make_schedule(db_session, company_id=2, next_due=company_today(db_session, 2))
        headers_b = headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=2))
        assert start_pm(client, headers_b, schedule_b.id).status_code == 200

        schedule_a = make_schedule(db_session, next_due=company_today(db_session, 1))
        assert start_pm(client, headers, schedule_a.id).status_code == 200

    def test_a_foreign_schedule_is_still_the_flat_404_not_a_409(self, client, db_session, headers):
        """Company 2's schedule HAS an open work order; company 1 asking for it must learn
        nothing -- the same 404 as an id that does not exist, never the 409 naming the work order."""
        schedule_b = make_schedule(db_session, company_id=2, next_due=company_today(db_session, 2))
        open_b = make_raw_wo(db_session, company_id=2, schedule_id=schedule_b.id, status=MaintenanceStatus.SCHEDULED)

        foreign = start_pm(client, headers, schedule_b.id)
        missing = start_pm(client, headers, schedule_b.id + 10_000)

        assert foreign.status_code == missing.status_code == status.HTTP_404_NOT_FOUND
        assert foreign.json() == missing.json()
        assert open_b.wo_number not in foreign.text and schedule_b.name not in foreign.text


class TestAHeldPmIsStillDue:
    def test_an_on_hold_work_order_puts_its_overdue_schedule_back_in_the_count(self, client, db_session, headers):
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today - timedelta(days=2))
        make_raw_wo(
            db_session,
            company_id=1,
            schedule_id=schedule.id,
            status=MaintenanceStatus.ON_HOLD,
            due=today - timedelta(days=2),
        )

        body = dashboard(client, headers)
        assert body["schedules_overdue"] == 1
        # Counted once: an on-hold work order is on no work-order tile.
        assert (body["overdue_count"], body["due_this_week"], body["in_progress"]) == (0, 0, 0)
        assert body["schedules_due_this_week"] == 0

    def test_an_on_hold_work_order_puts_its_due_soon_schedule_back_in_the_week(self, client, db_session, headers):
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today + timedelta(days=3))
        make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=MaintenanceStatus.ON_HOLD)

        body = dashboard(client, headers)
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (0, 1)
        assert (body["overdue_count"], body["due_this_week"]) == (0, 0)

    def test_the_schedules_own_due_date_decides_not_the_work_orders(self, client, db_session, headers):
        """The held work order's due date (next week) is not what the tile reads: the schedule
        came due two days ago, so the PM is overdue."""
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today - timedelta(days=2))
        make_raw_wo(
            db_session,
            company_id=1,
            schedule_id=schedule.id,
            status=MaintenanceStatus.ON_HOLD,
            due=today + timedelta(days=5),
        )

        body = dashboard(client, headers)
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (1, 0)

    @pytest.mark.parametrize(
        "covering,tile",
        [
            (MaintenanceStatus.SCHEDULED, "overdue_count"),
            (MaintenanceStatus.OVERDUE, "overdue_count"),
            (MaintenanceStatus.IN_PROGRESS, "in_progress"),
        ],
    )
    def test_a_work_order_being_worked_still_represents_its_schedule(self, client, db_session, headers, covering, tile):
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today - timedelta(days=2))
        make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=covering, due=today - timedelta(days=2))

        body = dashboard(client, headers)
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (0, 0)
        assert body[tile] == 1  # the PM is on the work order's tile instead -- once

    def test_a_held_work_order_beside_one_being_worked_does_not_double_count(self, client, db_session, headers):
        """Legacy data can hold two open work orders on one schedule. If one is being worked the
        PM is represented by it; the held one must not ALSO put the schedule in the count."""
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today - timedelta(days=2))
        make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=MaintenanceStatus.ON_HOLD)
        make_raw_wo(db_session, company_id=1, schedule_id=schedule.id, status=MaintenanceStatus.IN_PROGRESS)

        body = dashboard(client, headers)
        assert (body["schedules_overdue"], body["in_progress"]) == (0, 1)

    def test_another_companys_held_work_order_changes_nothing_here(self, client, db_session, headers):
        """Company 2's in-progress row naming company 1's schedule never covers it (pinned in
        test_maintenance_pm_roll_forward); company 2's ON-HOLD row must not either, and company 1's
        held PM never reaches company 2's tiles."""
        today = company_today(db_session, 1)
        schedule = make_schedule(db_session, next_due=today - timedelta(days=1))
        make_raw_wo(db_session, company_id=2, schedule_id=schedule.id, status=MaintenanceStatus.ON_HOLD)
        held = make_schedule(db_session, next_due=today + timedelta(days=1))
        make_raw_wo(db_session, company_id=1, schedule_id=held.id, status=MaintenanceStatus.ON_HOLD)

        body = dashboard(client, headers)
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (1, 1)

        other = dashboard(client, headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=2)))
        assert (other["schedules_overdue"], other["schedules_due_this_week"]) == (0, 0)
