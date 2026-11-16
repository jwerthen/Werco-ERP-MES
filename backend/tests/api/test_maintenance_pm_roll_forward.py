"""A completed PM moves its schedule forward, and the dashboard counts PMs nobody has started.

The PM loop the Maintenance page now drives end to end:

  schedule --(Start PM: POST /work-orders with schedule_id, no target)--> work order
           --(start, complete)--> schedule.last_completed_date = today,
                                  schedule.next_due_date = today + interval

Pinned here: the work order inherits the schedule's target; completing it advances the
schedule (and says so in the response and in a ``maintenance_schedule`` audit row); a
completion that omits ``checklist_results`` keeps the checklist the work order was opened
with; the schedule edit (``PUT /schedules/{id}``) is a planning-role write that never moves the
target; and ``GET /maintenance/dashboard`` reports overdue / due-this-week SCHEDULES that have
no open work order beside its (unchanged) work-order counts -- tenant-scoped, active only, and
never counting one PM twice.
"""

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import (
    MaintenanceFrequency,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceWorkOrder,
)
from app.models.user import UserRole
from tests.api.test_maintenance_assets import make_asset, make_wc
from tests.api.test_receiving_compliance import _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

SCHEDULES = "/api/v1/maintenance/schedules"
WORK_ORDERS = "/api/v1/maintenance/work-orders"
DASHBOARD = "/api/v1/maintenance/dashboard"


@pytest.fixture
def planner_headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.SUPERVISOR, company_id=1))


@pytest.fixture
def operator_headers(db_session):
    return headers_for(make_user(db_session, role=UserRole.OPERATOR, company_id=1))


def make_schedule(
    db: Session,
    *,
    company_id: int = 1,
    next_due: date,
    frequency: MaintenanceFrequency = MaintenanceFrequency.MONTHLY,
    frequency_days: int = None,
    is_active: bool = True,
    work_center_id: int = None,
    asset_id: int = None,
    checklist: str = None,
) -> MaintenanceSchedule:
    if work_center_id is None and asset_id is None:
        work_center_id = make_wc(db, company_id=company_id).id
    schedule = MaintenanceSchedule(
        company_id=company_id,
        work_center_id=work_center_id,
        asset_id=asset_id,
        name=f"Synthetic PM {_next()}",
        frequency=frequency,
        frequency_days=frequency_days,
        next_due_date=next_due,
        is_active=is_active,
        checklist=checklist,
        requires_shutdown=False,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def dashboard(client, headers) -> dict:
    resp = client.get(DASHBOARD, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def schedule_audit_rows(db: Session, schedule_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "maintenance_schedule", AuditLog.resource_id == schedule_id)
        .order_by(AuditLog.id)
        .all()
    )


class TestScheduleToWorkOrderToCompletion:
    def test_completing_a_pm_work_order_rolls_its_schedule_forward(self, client, db_session, planner_headers):
        overdue_since = date.today() - timedelta(days=3)
        checklist = json.dumps(["Lubricate ways", "Check coolant"])
        schedule = make_schedule(db_session, next_due=overdue_since, checklist=checklist)

        # Start PM: the page sends schedule_id and NO target, plus the schedule's checklist.
        checklist_results = json.dumps(
            [{"item": "Lubricate ways", "done": False}, {"item": "Check coolant", "done": False}]
        )
        created = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={
                "schedule_id": schedule.id,
                "title": schedule.name,
                "maintenance_type": "preventive",
                "due_date": overdue_since.isoformat(),
                "checklist_results": checklist_results,
            },
        )
        assert created.status_code == 200, created.text
        wo = created.json()
        assert wo["schedule_id"] == schedule.id
        assert (wo["work_center_id"], wo["asset_id"]) == (schedule.work_center_id, None)
        assert wo["checklist_results"] == checklist_results

        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=planner_headers).status_code == 200
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={"notes": "ok"})
        assert done.status_code == 200, done.text

        today = datetime.utcnow().date()
        expected_next = today + timedelta(days=30)
        assert done.json()["schedule_next_due_date"] == expected_next.isoformat()
        # Omitted checklist_results keeps the checklist the work order was opened with.
        assert done.json()["checklist_results"] == checklist_results

        db_session.expire_all()
        refreshed = db_session.get(MaintenanceSchedule, schedule.id)
        assert refreshed.last_completed_date == today
        assert refreshed.next_due_date == expected_next

        rows = schedule_audit_rows(db_session, schedule.id)
        assert len(rows) == 1
        assert "advanced by completing maintenance work order" in rows[0].description
        assert rows[0].extra_data and rows[0].extra_data.get("maintenance_work_order_id") == wo["id"]

    @pytest.mark.parametrize(
        "frequency,frequency_days,interval",
        [
            (MaintenanceFrequency.ANNUAL, None, 365),
            (MaintenanceFrequency.CUSTOM, 45, 45),
        ],
    )
    def test_the_interval_follows_the_schedules_frequency(
        self, client, db_session, planner_headers, frequency, frequency_days, interval
    ):
        asset = make_asset(db_session)
        schedule = make_schedule(
            db_session,
            next_due=date.today(),
            frequency=frequency,
            frequency_days=frequency_days,
            asset_id=asset.id,
        )
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"schedule_id": schedule.id, "title": "PM"}).json()
        assert wo["asset_id"] == asset.id
        # Completing straight from scheduled (no start) advances it too.
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={})
        assert done.status_code == 200, done.text
        expected = datetime.utcnow().date() + timedelta(days=interval)
        assert done.json()["schedule_next_due_date"] == expected.isoformat()

    def test_a_work_order_without_a_schedule_advances_nothing(self, client, db_session, planner_headers):
        wc = make_wc(db_session)
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"work_center_id": wc.id, "title": "Fix"}).json()
        done = client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={})
        assert done.status_code == 200 and done.json()["schedule_next_due_date"] is None

    def test_an_explicit_checklist_replaces_and_an_explicit_null_clears(self, client, db_session, planner_headers):
        schedule = make_schedule(db_session, next_due=date.today())
        opened = json.dumps([{"item": "Grease", "done": False}])
        ticked = json.dumps([{"item": "Grease", "done": True}])
        first = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"schedule_id": schedule.id, "title": "PM", "checklist_results": opened},
        ).json()
        done = client.post(
            f"{WORK_ORDERS}/{first['id']}/complete", headers=planner_headers, json={"checklist_results": ticked}
        )
        assert done.json()["checklist_results"] == ticked

        second = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"schedule_id": schedule.id, "title": "PM", "checklist_results": opened},
        ).json()
        cleared = client.post(
            f"{WORK_ORDERS}/{second['id']}/complete", headers=planner_headers, json={"checklist_results": None}
        )
        assert cleared.json()["checklist_results"] is None

    def test_another_target_than_the_schedules_is_still_409(self, client, db_session, planner_headers):
        schedule = make_schedule(db_session, next_due=date.today())
        other = make_wc(db_session)
        resp = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"schedule_id": schedule.id, "work_center_id": other.id, "title": "PM"},
        )
        assert resp.status_code == 409, resp.text


class TestScheduleEdit:
    def test_planner_edits_the_due_date_cadence_and_checklist(self, client, db_session, planner_headers):
        schedule = make_schedule(db_session, next_due=date(2026, 12, 1))
        original_target = schedule.work_center_id
        other = make_wc(db_session)
        resp = client.put(
            f"{SCHEDULES}/{schedule.id}",
            headers=planner_headers,
            json={
                "next_due_date": "2026-12-15",
                "frequency": "custom",
                "frequency_days": 45,
                "priority": "high",
                "estimated_duration_hours": 2.5,
                "requires_shutdown": True,
                "checklist": json.dumps(["Drain tank", "Check belt"]),
                # Not a ScheduleUpdate field: the target never moves through PUT.
                "work_center_id": other.id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["next_due_date"] == "2026-12-15"
        assert (body["frequency"], body["frequency_days"]) == ("custom", 45)
        assert body["priority"] == "high" and body["estimated_duration_hours"] == 2.5
        assert body["requires_shutdown"] is True
        assert body["checklist_items"] == ["Drain tank", "Check belt"]
        assert body["work_center_id"] == original_target

        rows = schedule_audit_rows(db_session, schedule.id)
        assert len(rows) == 1 and rows[0].action == "UPDATE"

    def test_custom_without_days_is_422(self, client, db_session, planner_headers):
        schedule = make_schedule(db_session, next_due=date(2026, 12, 1))
        resp = client.put(f"{SCHEDULES}/{schedule.id}", headers=planner_headers, json={"frequency": "custom"})
        assert resp.status_code == 422, resp.text

    def test_an_operator_cannot_edit_a_schedule(self, client, db_session, operator_headers):
        schedule = make_schedule(db_session, next_due=date(2026, 12, 1))
        resp = client.put(f"{SCHEDULES}/{schedule.id}", headers=operator_headers, json={"next_due_date": "2027-01-01"})
        assert resp.status_code == 403, resp.text


class TestDashboardScheduleCounts:
    def test_unstarted_schedules_are_counted_beside_the_work_order_counts(self, client, db_session, planner_headers):
        today = date.today()
        overdue = make_schedule(db_session, next_due=today - timedelta(days=1))
        make_schedule(db_session, next_due=today)  # due today: due this week
        make_schedule(db_session, next_due=today + timedelta(days=7))  # last day of the week window
        make_schedule(db_session, next_due=today + timedelta(days=8))  # outside it
        make_schedule(db_session, next_due=today - timedelta(days=5), is_active=False)  # inactive: never
        make_schedule(db_session, company_id=2, next_due=today - timedelta(days=5))  # another tenant: never

        body = dashboard(client, planner_headers)
        assert (body["schedules_overdue"], body["schedules_due_this_week"]) == (1, 2)
        # The work-order counts are unchanged: there are no work orders.
        assert (body["overdue_count"], body["due_this_week"]) == (0, 0)

        # Opening a PM work order moves the PM from the schedule count to the work-order count.
        wo = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"schedule_id": overdue.id, "title": "PM", "due_date": overdue.next_due_date.isoformat()},
        ).json()
        body = dashboard(client, planner_headers)
        assert (body["schedules_overdue"], body["overdue_count"]) == (0, 1)

        # On hold is still open: the schedule stays represented by its work order.
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=planner_headers).status_code == 200
        hold = client.put(f"{WORK_ORDERS}/{wo['id']}", headers=planner_headers, json={"status": "on_hold"})
        assert hold.status_code == 200, hold.text
        assert dashboard(client, planner_headers)["schedules_overdue"] == 0

        # Completed: the schedule is no longer due at all (rolled forward 30 days).
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={}).status_code == 400
        resume = client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=planner_headers)
        assert resume.status_code == 200, resume.text
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={}).status_code == 200
        body = dashboard(client, planner_headers)
        assert (body["schedules_overdue"], body["overdue_count"], body["schedules_due_this_week"]) == (0, 0, 2)

    def test_a_cancelled_work_order_puts_the_schedule_back_in_the_count(self, client, db_session, planner_headers):
        schedule = make_schedule(db_session, next_due=date.today() - timedelta(days=2))
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"schedule_id": schedule.id, "title": "PM"}).json()
        assert dashboard(client, planner_headers)["schedules_overdue"] == 0
        client.put(f"{WORK_ORDERS}/{wo['id']}", headers=planner_headers, json={"status": "cancelled"})
        db_session.expire_all()
        assert db_session.get(MaintenanceWorkOrder, wo["id"]).status == MaintenanceStatus.CANCELLED
        assert dashboard(client, planner_headers)["schedules_overdue"] == 1
