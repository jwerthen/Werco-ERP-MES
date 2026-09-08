"""Calendar changes affect real planning and committed loads, without changing old centers."""

from datetime import date, datetime

import pytest

from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.working_calendar import WorkingCalendar
from app.services.scheduling_projection import _project_work_order_schedule
from app.services.working_calendar_service import load_working_calendars
from tests.api.kiosk_test_helpers import make_user, user_headers
from tests.api.test_scheduling_impact import APPLY, fixture_job, preview


def calendar_url(center):
    return f"/api/v1/scheduling/work-centers/{center.id}/calendar"


def save_calendar(client, headers, center, **changes):
    payload = {
        "expected_version": 0,
        "weekly_hours": [8, 8, 8, 8, 4, 0, 0],
        "overrides": [{"date": "2026-09-14", "hours": 0, "reason": "Plant shutdown"}],
        **changes,
    }
    result = client.put(calendar_url(center), headers=headers, json=payload)
    assert result.status_code == 200, result.text
    return result.json()


def test_calendar_version_permissions_tenant_and_audit(client, auth_headers, db_session):
    _, _, center = fixture_job(db_session)
    before = client.get(calendar_url(center), headers=auth_headers).json()
    assert before["version"] == 0 and before["weekly_hours"] == [8] * 7
    saved = save_calendar(client, auth_headers, center)
    assert saved["version"] == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "working_calendar").count() == 1
    refused = client.put(
        calendar_url(center),
        headers=auth_headers,
        json={"expected_version": 0, "weekly_hours": [8] * 7, "overrides": []},
    )
    assert refused.status_code == 409
    assert client.get(calendar_url(center), headers=auth_headers).json() == saved
    operator = make_user(db_session)
    assert (
        client.put(
            calendar_url(center),
            headers=user_headers(operator),
            json={"expected_version": 1, "weekly_hours": [8] * 7, "overrides": []},
        ).status_code
        == 403
    )
    foreign = make_user(db_session, company_id=2, role=UserRole.ADMIN)
    assert client.get(calendar_url(center), headers=user_headers(foreign)).status_code == 404
    assert (
        client.put(
            calendar_url(center),
            headers=user_headers(foreign),
            json={"expected_version": 0, "weekly_hours": [8] * 7, "overrides": []},
        ).status_code
        == 404
    )


def test_shift_preview_commit_and_heatmap_agree_across_short_shift_weekend_and_shutdown(
    client, auth_headers, db_session
):
    wo, op, center = fixture_job(db_session, start=datetime(2026, 9, 10), hours=12)
    save_calendar(client, auth_headers, center)
    plan = preview(client, auth_headers, [wo.id], shift_days=1)
    change = plan["jobs"][0]["operations"][0]
    assert change["after_start"][:10] == "2026-09-11"  # Friday: only four hours
    assert change["after_end"][:10] == "2026-09-15"  # Weekend + Monday shutdown
    db_session.expire_all()
    assert op.scheduled_start == datetime(2026, 9, 10)  # preview wrote nothing
    assert client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]}).status_code == 200
    db_session.expire_all()
    assert op.scheduled_end == datetime(2026, 9, 15)
    heatmap = client.get(
        '/api/v1/scheduling/capacity-heatmap?start_date=2026-09-11&end_date=2026-09-15', headers=auth_headers
    ).json()
    days = next(row for row in heatmap['work_centers'] if row['work_center_id'] == center.id)['days']
    assert [(row['scheduled_hours'], row['capacity_hours']) for row in days] == [(4, 4), (0, 0), (0, 0), (0, 0), (8, 8)]
    summary = client.get(
        '/api/v1/scheduling/capacity?start_date=2026-09-11&end_date=2026-09-15', headers=auth_headers
    ).json()
    assert next(row for row in summary if row['work_center_id'] == center.id)['available_hours'] == 12


def test_calendar_edit_invalidates_reviewed_plan_and_retains_original_dates(client, auth_headers, db_session):
    wo, op, center = fixture_job(db_session, start=datetime(2026, 9, 10))
    plan = preview(client, auth_headers, [wo.id], shift_days=1)
    save_calendar(client, auth_headers, center)
    result = client.post(APPLY, headers=auth_headers, json={"plan_token": plan["plan_token"]})
    assert result.status_code == 409
    db_session.expire_all()
    assert op.scheduled_start == datetime(2026, 9, 10)


def test_manual_schedule_rolls_to_open_date_and_date_preview_uses_same_calendar(client, auth_headers, db_session):
    wo, op, center = fixture_job(db_session, hours=12)
    save_calendar(client, auth_headers, center)
    result = client.put(
        f'/api/v1/scheduling/work-orders/{wo.id}/schedule',
        headers=auth_headers,
        json={"scheduled_start": "2026-09-12", "forward_schedule": True},
    )
    assert result.status_code == 200, result.text
    db_session.expire_all()
    assert (op.scheduled_start, op.scheduled_end) == (datetime(2026, 9, 15), datetime(2026, 9, 16))
    result = client.post(
        '/api/v1/scheduling/capacity-for-date',
        headers=auth_headers,
        json={
            "work_center_id": center.id,
            "target_date": "2026-09-14",
            "work_order_id": wo.id,
            "forward_schedule": True,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()['capacity_hours'] == 0 and result.json()['projected_hours'] == 0


def test_shutdown_retains_existing_work_as_overload(client, auth_headers, db_session):
    _, _, center = fixture_job(db_session, start=datetime(2026, 9, 14))
    save_calendar(client, auth_headers, center)
    result = client.get(
        '/api/v1/scheduling/capacity-heatmap?start_date=2026-09-14&end_date=2026-09-14', headers=auth_headers
    ).json()
    day = next(row for row in result['work_centers'] if row['work_center_id'] == center.id)['days'][0]
    assert day['capacity_hours'] == 0 and day['scheduled_hours'] == 8 and day['overloaded'] is True


@pytest.mark.parametrize(
    'payload',
    [
        {'weekly_hours': [8] * 6},
        {'weekly_hours': [25] * 7},
        {'overrides': [{'date': '2026-09-14', 'hours': 0, 'reason': '  '}]},
        {
            'overrides': [
                {'date': '2026-09-14', 'hours': 0, 'reason': 'A'},
                {'date': '2026-09-14', 'hours': 4, 'reason': 'B'},
            ]
        },
    ],
)
def test_invalid_calendar_refused_without_changes(client, auth_headers, db_session, payload):
    _, _, center = fixture_job(db_session)
    result = client.put(
        calendar_url(center),
        headers=auth_headers,
        json={'expected_version': 0, 'weekly_hours': [8] * 7, 'overrides': [], **payload},
    )
    assert result.status_code == 422
    assert db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == center.id).count() == 0


def test_multi_shift_duration_and_legacy_fallback(db_session):
    _, op, center = fixture_job(db_session, hours=16)
    center.capacity_hours_per_day = 16
    db_session.commit()
    # Merely reading default settings never changes legacy 8h date projection.
    book = load_working_calendars(db_session, 1)
    assert _project_work_order_schedule([op], op, date(2026, 9, 11), calendars=book)[0]['scheduled_end'] == date(
        2026, 9, 12
    )
    book[center.id].update(version=1, weekly_hours=[16, 16, 16, 16, 16, 0, 0])
    assert _project_work_order_schedule([op], op, date(2026, 9, 11), calendars=book)[0]['scheduled_end'] == date(
        2026, 9, 11
    )


def test_finite_scheduler_uses_configured_multi_day_capacity(client, auth_headers, db_session):
    from datetime import timedelta

    from app.services.scheduling_service import SchedulingService

    wo, op, center = fixture_job(db_session, hours=12)
    today = date.today()
    save_calendar(
        client,
        auth_headers,
        center,
        weekly_hours=[4] * 7,
        overrides=[{'date': today.isoformat(), 'hours': 0, 'reason': 'Shutdown'}],
    )
    result = SchedulingService(db_session, 1).run_scheduling(work_center_ids=[center.id], work_order_ids=[wo.id])
    assert result['scheduled_count'] == 1, result
    db_session.expire_all()
    assert op.scheduled_start.date() == today + timedelta(days=1)
    assert op.scheduled_end.date() == today + timedelta(days=3)


def test_shift_preserves_precedence_when_configured_center_precedes_legacy_center(client, auth_headers, db_session):
    from app.models.work_order import OperationStatus, WorkOrderOperation
    from tests.api.kiosk_test_helpers import make_work_center

    wo, first, center = fixture_job(db_session, start=datetime(2026, 9, 11), hours=8)
    legacy_center = make_work_center(db_session)
    later = WorkOrderOperation(
        company_id=1,
        work_order_id=wo.id,
        work_center_id=legacy_center.id,
        operation_number='20',
        sequence=20,
        name='Legacy downstream',
        status=OperationStatus.PENDING,
        scheduled_start=datetime(2026, 9, 14),
        scheduled_end=datetime(2026, 9, 15),
        run_time_hours=8,
        setup_time_hours=0,
    )
    db_session.add(later)
    db_session.commit()
    save_calendar(client, auth_headers, center)
    plan = preview(client, auth_headers, [wo.id], shift_days=1)
    changes = {row['operation_id']: row for row in plan['jobs'][0]['operations']}
    assert changes[first.id]['after_end'][:10] == '2026-09-15'
    assert changes[later.id]['after_start'][:10] == '2026-09-16'
    assert changes[later.id]['after_end'][:10] == '2026-09-17'  # original two-day span retained


def test_earliest_search_rejects_opening_beyond_requested_horizon(client, auth_headers, db_session):
    from datetime import timedelta

    wo, _, center = fixture_job(db_session)
    save_calendar(
        client,
        auth_headers,
        center,
        weekly_hours=[0] * 7,
        overrides=[{'date': (date.today() + timedelta(days=100)).isoformat(), 'hours': 8, 'reason': 'Reopening'}],
    )
    result = client.post(
        f'/api/v1/scheduling/work-orders/{wo.id}/schedule-earliest',
        headers=auth_headers,
        json={'horizon_days': 1, 'forward_schedule': True},
    )
    assert result.status_code == 409, result.text


def test_configured_finite_scheduler_reserves_minimum_hour_for_each_zero_estimate(client, auth_headers, db_session):
    from datetime import timedelta

    from app.services.scheduling_service import SchedulingService

    wo1, op1, center = fixture_job(db_session, hours=0)
    wo2, op2, _ = fixture_job(db_session, center=center, hours=0)
    save_calendar(client, auth_headers, center, weekly_hours=[1] * 7, overrides=[])
    service = SchedulingService(db_session, 1)
    result = service.run_scheduling(work_center_ids=[center.id], work_order_ids=[wo1.id, wo2.id])
    assert result['scheduled_count'] == 2
    db_session.expire_all()
    assert [op1.scheduled_start.date(), op2.scheduled_start.date()] == [date.today(), date.today() + timedelta(days=1)]
    assert service.capacity_map[center.id].daily_load[date.today()] == 1
    assert op1.run_time_hours == 0 and op2.run_time_hours == 0
    fresh = SchedulingService(db_session, 1)
    fresh._initialize_capacity([center], 90)
    assert fresh.capacity_map[center.id].daily_load[date.today()] == 1


def test_configured_finite_scheduler_keeps_positive_fractional_estimates(client, auth_headers, db_session):
    from app.services.scheduling_service import SchedulingService

    wo1, op1, center = fixture_job(db_session, hours=0.5)
    wo2, op2, _ = fixture_job(db_session, center=center, hours=0.5)
    save_calendar(client, auth_headers, center, weekly_hours=[1] * 7, overrides=[])
    service = SchedulingService(db_session, 1)
    result = service.run_scheduling(work_center_ids=[center.id], work_order_ids=[wo1.id, wo2.id])
    assert result['scheduled_count'] == 2
    db_session.expire_all()
    assert op1.scheduled_start.date() == op2.scheduled_start.date() == date.today()
    assert service.capacity_map[center.id].daily_load[date.today()] == 1
