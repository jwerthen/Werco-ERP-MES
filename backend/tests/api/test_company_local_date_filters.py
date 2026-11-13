"""Calendar-date filters on the plugin's read surfaces select company-LOCAL days (P1.1 sweep).

The same UTC-midnight bug the exports had (``datetime.combine(d, datetime.min/max.time())``
against a naive-UTC column) lived in downtime, job costs, the employee-time and
daily-output reports, and the analytics services. Each now filters local midnight to
the EXCLUSIVE next local midnight in ``Company.timezone`` (default America/Chicago).

Every boundary test seeds four rows -- one microsecond before the local day, exactly at
local midnight, one microsecond before the next local midnight, and exactly at it --
and expects only the middle two, at the CST and CDT boundaries and both DST days.
``tests/test_date_filter_combine_guard.py`` keeps the old pattern out of ``app/``.
"""

from datetime import date, datetime, timedelta
from typing import List

import pytest
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE, local_date_range_to_utc, local_today
from app.models.company import Company
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.job_costing import JobCost
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.adoption_metrics_service import get_adoption_metrics
from app.services.analytics_service import AnalyticsService, get_date_range
from app.services.company_time import company_today, company_zone
from app.services.flow_metrics_service import get_flow_metrics
from app.services.quality_yield_service import get_fpy_rty

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BOUNDARY_DAYS = [
    pytest.param(date(2026, 1, 15), datetime(2026, 1, 15, 6), datetime(2026, 1, 16, 6), id="cst"),
    pytest.param(date(2026, 7, 15), datetime(2026, 7, 15, 5), datetime(2026, 7, 16, 5), id="cdt"),
    pytest.param(date(2026, 3, 8), datetime(2026, 3, 8, 6), datetime(2026, 3, 9, 5), id="spring-forward-23h"),
    pytest.param(date(2026, 11, 1), datetime(2026, 11, 1, 5), datetime(2026, 11, 2, 6), id="fall-back-25h"),
]
ONE = timedelta(microseconds=1)


def _stamps(start: datetime, end: datetime) -> List[datetime]:
    """Before the day, at its start, its last instant, the next day's start."""
    return [start - ONE, start, end - ONE, end]


@pytest.fixture
def part(db_session: Session) -> Part:
    row = Part(
        part_number="TZ-SWEEP-PART",
        name="Sweep part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def work_center(db_session: Session) -> WorkCenter:
    row = WorkCenter(code="TZ-WC", name="Sweep WC", work_center_type="welding", is_active=True, company_id=1)
    db_session.add(row)
    db_session.commit()
    return row


def _work_order(db: Session, part: Part, number: str, **fields) -> WorkOrder:
    wo = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=fields.pop("status", WorkOrderStatus.IN_PROGRESS),
        company_id=1,
        **fields,
    )
    db.add(wo)
    db.flush()
    return wo


# ---------------------------------------------------------------------------
# Downtime: list, summary, by-work-center
# ---------------------------------------------------------------------------


def _seed_downtime(db: Session, user: User, wc: WorkCenter, stamps: List[datetime]) -> List[int]:
    ids = []
    for index, start_time in enumerate(stamps):
        event = DowntimeEvent(
            work_center_id=wc.id,
            start_time=start_time,
            end_time=start_time + timedelta(minutes=10 + index),
            duration_minutes=10 + index,
            category=DowntimeCategory.MECHANICAL,
            planned_type=DowntimePlannedType.UNPLANNED,
            reported_by=user.id,
            company_id=1,
        )
        db.add(event)
        db.flush()
        ids.append(event.id)
    db.commit()
    return ids


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_downtime_reads_select_the_central_day(
    client, auth_headers, test_user, work_center, db_session, day, start, end
):
    ids = _seed_downtime(db_session, test_user, work_center, _stamps(start, end))
    params = {"date_from": day.isoformat(), "date_to": day.isoformat()}

    listed = client.get("/api/v1/downtime/", headers=auth_headers, params=params)
    summary = client.get("/api/v1/downtime/summary", headers=auth_headers, params=params)
    by_wc = client.get("/api/v1/downtime/by-work-center", headers=auth_headers, params=params)

    assert listed.status_code == 200, listed.text
    assert sorted(row["id"] for row in listed.json()) == sorted(ids[1:3])
    assert summary.json()["event_count"] == 2
    # 11 + 12 minutes: only the middle two events' durations.
    assert summary.json()["total_downtime_hours"] == round(23 / 60, 2)
    assert [row["event_count"] for row in by_wc.json()] == [2]


@pytest.mark.parametrize("path", ["/api/v1/downtime/", "/api/v1/downtime/summary", "/api/v1/downtime/by-work-center"])
def test_downtime_bad_ranges_are_422(client, auth_headers, path):
    reversed_range = client.get(path, headers=auth_headers, params={"date_from": "2026-01-16", "date_to": "2026-01-15"})
    out_of_range = client.get(path, headers=auth_headers, params={"date_to": date.max.isoformat()})

    assert reversed_range.status_code == 422
    assert reversed_range.json()["detail"] == "Start date must be on or before end date"
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"] == "End date is outside the supported range"


def test_downtime_honors_company_timezone(client, auth_headers, test_user, work_center, db_session):
    db_session.get(Company, 1).timezone = "America/New_York"
    db_session.commit()
    ids = _seed_downtime(
        db_session, test_user, work_center, _stamps(datetime(2026, 1, 15, 5), datetime(2026, 1, 16, 5))
    )

    listed = client.get(
        "/api/v1/downtime/", headers=auth_headers, params={"date_from": "2026-01-15", "date_to": "2026-01-15"}
    )

    assert sorted(row["id"] for row in listed.json()) == sorted(ids[1:3])


# ---------------------------------------------------------------------------
# Job costs list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_job_cost_list_selects_the_central_day(client, auth_headers, part, db_session, day, start, end):
    ids = []
    for index, created_at in enumerate(_stamps(start, end)):
        wo = _work_order(db_session, part, f"WO-JC-{index}")
        job_cost = JobCost(work_order_id=wo.id, company_id=1, created_at=created_at, updated_at=created_at)
        db_session.add(job_cost)
        db_session.flush()
        ids.append(job_cost.id)
    db_session.commit()

    response = client.get(
        "/api/v1/job-costs/", headers=auth_headers, params={"date_from": day.isoformat(), "date_to": day.isoformat()}
    )

    assert response.status_code == 200, response.text
    assert sorted(row["id"] for row in response.json()) == sorted(ids[1:3])
    # The response model now carries the UTC offset.
    assert all(row["created_at"].endswith("Z") for row in response.json())


def test_job_cost_reversed_range_is_422(client, auth_headers):
    response = client.get(
        "/api/v1/job-costs/", headers=auth_headers, params={"date_from": "2026-01-16", "date_to": "2026-01-15"}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Reports: employee-time and daily-output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_employee_time_selects_the_central_day(client, auth_headers, operator_user, db_session, day, start, end):
    for clock_in in _stamps(start, end):
        db_session.add(
            TimeEntry(
                user_id=operator_user.id,
                entry_type=TimeEntryType.RUN,
                clock_in=clock_in,
                clock_out=None,
                duration_hours=1,
                company_id=1,
            )
        )
    db_session.commit()

    response = client.get(
        "/api/v1/reports/employee-time",
        headers=auth_headers,
        params={"start_date": day.isoformat(), "end_date": day.isoformat(), "user_id": operator_user.id},
    )

    assert response.status_code == 200, response.text
    [employee] = response.json()
    assert employee["total_hours"] == 2
    # Each entry's ``date`` is its Central clock-in day -- the day it was selected by.
    assert {entry["date"] for entry in employee["entries"]} == {day.isoformat()}


def test_employee_time_second_shift_lands_on_its_own_day(client, auth_headers, operator_user, db_session):
    """7 PM CST Jan 15 (01:00Z Jan 16): on Jan 15's report, dated Jan 15."""
    db_session.add(
        TimeEntry(
            user_id=operator_user.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime(2026, 1, 16, 1, 0),
            clock_out=datetime(2026, 1, 16, 3, 0),
            duration_hours=2,
            company_id=1,
        )
    )
    db_session.commit()

    jan15 = client.get(
        "/api/v1/reports/employee-time",
        headers=auth_headers,
        params={"start_date": "2026-01-15", "end_date": "2026-01-15"},
    ).json()
    jan16 = client.get(
        "/api/v1/reports/employee-time",
        headers=auth_headers,
        params={"start_date": "2026-01-16", "end_date": "2026-01-16"},
    ).json()

    assert [entry["date"] for entry in jan15[0]["entries"]] == ["2026-01-15"]
    assert jan15[0]["entries"][0]["clock_in"] == "2026-01-16T01:00:00Z"
    assert jan16 == []


def test_employee_time_reversed_range_is_422(client, auth_headers):
    response = client.get(
        "/api/v1/reports/employee-time",
        headers=auth_headers,
        params={"start_date": "2026-01-16", "end_date": "2026-01-15"},
    )

    assert response.status_code == 422


def test_daily_output_buckets_are_central_days_ending_central_today(
    client, auth_headers, part, work_center, db_session
):
    today = local_today(CENTRAL_TIME_ZONE)
    today_start, _ = local_date_range_to_utc(today, today, CENTRAL_TIME_ZONE)
    wo = _work_order(db_session, part, "WO-DAILY-TZ")
    for index, (actual_end, quantity) in enumerate(((today_start - ONE, 3), (today_start, 5))):
        db_session.add(
            WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=work_center.id,
                sequence=10 * (index + 1),
                name=f"Daily op {index}",
                status=OperationStatus.COMPLETE,
                quantity_complete=quantity,
                actual_end=actual_end,
                company_id=1,
            )
        )
    db_session.commit()

    buckets = client.get("/api/v1/reports/daily-output", headers=auth_headers, params={"days": 2}).json()

    assert [row["date"] for row in buckets] == [(today - timedelta(days=1)).isoformat(), today.isoformat()]
    assert [row["completed"] for row in buckets] == [3, 5]


# ---------------------------------------------------------------------------
# Analytics: period "today", the service windows, and the router 422s
# ---------------------------------------------------------------------------


def test_get_date_range_today_is_the_company_wall_clock_date():
    today = local_today(CENTRAL_TIME_ZONE)
    assert get_date_range("today", zone=CENTRAL_TIME_ZONE) == (today, today)
    assert get_date_range("7d", zone=CENTRAL_TIME_ZONE) == (today - timedelta(days=7), today)
    # No zone given: the default company zone, never the server's date.today().
    assert get_date_range("today") == (today, today)


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_analytics_service_windows_are_central_days(db_session, operator_user, part, work_center, day, start, end):
    wo = _work_order(db_session, part, "WO-ANALYTICS-TZ")
    for clock_in in _stamps(start, end):
        db_session.add(
            TimeEntry(
                user_id=operator_user.id,
                work_order_id=wo.id,
                work_center_id=work_center.id,
                entry_type=TimeEntryType.RUN,
                clock_in=clock_in,
                clock_out=clock_in + timedelta(minutes=1),
                duration_hours=0.1,
                quantity_produced=10,
                company_id=1,
            )
        )
    db_session.commit()

    service = AnalyticsService(db_session, 1)

    assert service._get_good_units(day, day) == 20
    assert service._window(day, day) == (start, end)


def test_analytics_service_reads_the_company_zone(db_session):
    db_session.get(Company, 1).timezone = "America/Denver"
    db_session.commit()

    window = AnalyticsService(db_session, 1)._window(date(2026, 1, 15), date(2026, 1, 15))

    assert window == (datetime(2026, 1, 15, 7), datetime(2026, 1, 16, 7))


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/analytics/kpis",
        "/api/v1/analytics/quality-metrics",
        "/api/v1/analytics/flow",
        "/api/v1/analytics/fpy",
        "/api/v1/reports/ship-otd",
    ],
)
def test_analytics_custom_period_bad_ranges_are_422(client, admin_headers, path):
    reversed_range = client.get(
        path, headers=admin_headers, params={"period": "custom", "start_date": "2026-01-16", "end_date": "2026-01-15"}
    )
    out_of_range = client.get(
        path, headers=admin_headers, params={"period": "custom", "start_date": "2026-01-15", "end_date": "9999-12-31"}
    )

    assert reversed_range.status_code == 422
    assert out_of_range.status_code == 422


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_fpy_and_flow_windows_are_central_days(db_session, part, work_center, day, start, end):
    for index, stamp in enumerate(_stamps(start, end)):
        wo = _work_order(
            db_session,
            part,
            f"WO-FLOW-{index}",
            status=WorkOrderStatus.COMPLETE,
            released_at=stamp - timedelta(days=2),
            actual_end=stamp,
        )
        db_session.add(
            WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=work_center.id,
                sequence=10,
                name="FPY op",
                status=OperationStatus.COMPLETE,
                quantity_complete=4,
                actual_end=stamp,
                company_id=1,
            )
        )
    db_session.commit()

    fpy = get_fpy_rty(db_session, 1, day, day)
    flow = get_flow_metrics(db_session, 1, day, day)

    assert sum(group.operations for group in fpy.by_work_center) == 2
    assert sorted(row.work_order_number for row in flow.work_orders) == ["WO-FLOW-1", "WO-FLOW-2"]


@pytest.mark.parametrize("day,start,end", BOUNDARY_DAYS)
def test_adoption_event_window_uses_aware_central_bounds(db_session, day, start, end):
    """``OperationalEvent.occurred_at`` is a timezone-aware column: the same Central day applies."""
    for stamp in _stamps(start, end):
        db_session.add(
            OperationalEvent(
                company_id=1,
                event_type="operation_completed",
                source_module="shop_floor",
                event_payload={},
                occurred_at=stamp,
            )
        )
    db_session.commit()

    result = get_adoption_metrics(db_session, 1, day, day)

    assert result.live_completions + result.backfill_completions + result.unknown_completions == 2


def test_company_zone_reads_the_company_row_and_falls_back_to_central(db_session):
    company = db_session.get(Company, 1)
    assert company_zone(db_session, 1) == CENTRAL_TIME_ZONE  # model default
    company.timezone = "America/Phoenix"
    db_session.commit()
    assert str(company_zone(db_session, 1)) == "America/Phoenix"
    company.timezone = "Mars/Olympus_Mons"
    db_session.commit()
    assert company_zone(db_session, 1) == CENTRAL_TIME_ZONE
    # A missing company (or none) is Central too, never UTC.
    assert company_zone(db_session, 987654) == CENTRAL_TIME_ZONE
    assert company_zone(db_session, None) == CENTRAL_TIME_ZONE
    assert company_today(db_session, 1) == local_today(CENTRAL_TIME_ZONE)
