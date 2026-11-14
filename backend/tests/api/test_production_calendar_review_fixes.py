"""Regressions from the P1 review (2026-11-13) on the production calendar and its readers.

Werco's calendar (tests/production_calendar_helpers.py): day start 05:00 Central, weld
W1 05:00-15:30 / W2 15:30-02:00, rest of shop S1 07:30-16:00 / S2 15:30-00:00, Mon-Fri.
2026-11-17 is a Tuesday, 2026-11-18 a Wednesday (CST, UTC-6).
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.endpoints import production_calendar as calendar_router
from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.oee import OEERecord
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.models.working_calendar import WorkingCalendar
from tests.lean_phase1_helpers import (
    COMPANY_A,
    headers_for,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)
from tests.production_calendar_helpers import seed_werco_calendar, werco_payload

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/production-calendar"
TUESDAY = date(2026, 11, 17)
WEDNESDAY = date(2026, 11, 18)


def central(year, month, day, hour, minute=0):
    return (
        datetime(year, month, day, hour, minute, tzinfo=CENTRAL_TIME_ZONE).astimezone(timezone.utc).replace(tzinfo=None)
    )


def _admin(db):
    return make_user(db, role=UserRole.ADMIN)


def _welding(db):
    return make_work_center(db, work_center_type="welding")


def _employee_time(client, admin, day: date):
    resp = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": day.isoformat(), "end_date": day.isoformat()},
        headers=headers_for(admin),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------------------
# Finding 1: employee-time counted a straddling entry on two production days
# --------------------------------------------------------------------------------------


def test_early_weld_clock_in_is_counted_once_on_its_attributed_day(client: TestClient, db_session: Session):
    """04:55-15:30 Wednesday weld entry: Wednesday's W1 only -- never in Tuesday's report too."""
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, weld, status_=OperationStatus.IN_PROGRESS)
    make_entry(db_session, admin, wo, op, weld, clock_in=central(2026, 11, 18, 4, 55), duration_hours=10 + 35 / 60)

    tuesday = _employee_time(client, admin, TUESDAY)
    wednesday = _employee_time(client, admin, WEDNESDAY)

    assert tuesday == []
    [employee] = wednesday
    assert employee["total_hours"] == pytest.approx(10.58)
    assert [(e["date"], e["shift_code"]) for e in employee["entries"]] == [("2026-11-18", "W1")]


def test_a_range_report_counts_a_straddling_entry_once(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 18, 4, 55), duration_hours=2)
    make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 17, 16, 0), duration_hours=1)

    resp = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": TUESDAY.isoformat(), "end_date": WEDNESDAY.isoformat()},
        headers=headers_for(admin),
    )

    [employee] = resp.json()
    assert employee["total_hours"] == 3
    assert sorted(e["date"] for e in employee["entries"]) == ["2026-11-17", "2026-11-18"]


def test_unconfigured_midnight_crossing_entry_is_reported_on_its_clock_in_day_only(
    client: TestClient, db_session: Session
):
    """No calendar: 23:00 CST Jan 14 -> 01:00 CST Jan 15 belongs to Jan 14 (local day of clock-in)."""
    admin = _admin(db_session)
    make_entry(db_session, admin, None, None, None, clock_in=central(2026, 1, 14, 23, 0), duration_hours=2)

    jan14 = _employee_time(client, admin, date(2026, 1, 14))
    jan15 = _employee_time(client, admin, date(2026, 1, 15))

    assert [e["date"] for emp in jan14 for e in emp["entries"]] == ["2026-01-14"]
    assert jan15 == []


def test_employee_time_far_past_start_is_not_a_500(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    resp = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": "0001-01-01", "end_date": "0001-01-02"},
        headers=headers_for(admin),
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------------------
# Finding 3: a PUT that echoes a legacy OEE label back was refused
# --------------------------------------------------------------------------------------


def test_oee_put_echoing_the_records_own_legacy_label_is_accepted(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    legacy = OEERecord(work_center_id=weld.id, record_date=TUESDAY, shift="Day", calculation_source="manual")
    legacy.company_id = COMPANY_A
    db_session.add(legacy)
    db_session.commit()

    echoed = client.put(
        f"/api/v1/oee/records/{legacy.id}",
        json={"shift": "Day", "notes": "fix typo"},
        headers=headers_for(admin),
    )
    assert echoed.status_code == 200, echoed.text
    assert (echoed.json()["shift"], echoed.json()["shift_id"], echoed.json()["notes"]) == ("Day", None, "fix typo")

    # Explicit null shift_id with the same label is the same echo.
    nulled = client.put(
        f"/api/v1/oee/records/{legacy.id}",
        json={"shift": "Day", "shift_id": None, "notes": "again"},
        headers=headers_for(admin),
    )
    assert nulled.status_code == 200, nulled.text

    # A DIFFERENT free-text label is still a new selection, and still refused.
    changed = client.put(f"/api/v1/oee/records/{legacy.id}", json={"shift": "Night"}, headers=headers_for(admin))
    assert changed.status_code == 422 and "Unknown shift 'Night'" in changed.json()["detail"]


# --------------------------------------------------------------------------------------
# Compliance finding 4: out-of-range dates were 500s
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("at", ["9999-12-31T23:00:00Z", "0001-01-01T00:00:00Z"])
def test_resolve_at_the_ends_of_the_datetime_range_is_422(client: TestClient, db_session: Session, at):
    viewer = make_user(db_session, role=UserRole.OPERATOR)
    resp = client.get(f"{URL}/resolve", params={"at": at}, headers=headers_for(viewer))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "The instant is outside the supported range"


@pytest.mark.parametrize("record_date,status_code", [("9999-12-31", 422), ("0001-01-01", 200)])
def test_oee_calculate_at_the_ends_of_the_date_range_is_not_a_500(
    client: TestClient, db_session: Session, record_date, status_code
):
    admin = _admin(db_session)
    weld = _welding(db_session)
    resp = client.post(
        f"/api/v1/oee/calculate/{weld.id}", params={"record_date": record_date}, headers=headers_for(admin)
    )
    assert resp.status_code == status_code, resp.text


# --------------------------------------------------------------------------------------
# Finding 6: the working-calendar sync
# --------------------------------------------------------------------------------------


def test_sync_skips_a_pattern_with_no_active_shift_instead_of_zeroing_capacity(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    rest = make_work_center(db_session)
    payload = werco_payload([weld.id])
    # Retire every rest-of-shop shift: the default pattern now defines no hours.
    for shift in payload["patterns"][1]["shifts"]:
        shift["is_active"] = False
    saved = client.put(URL, json=payload, headers=headers_for(admin))
    assert saved.status_code == 200, saved.text

    dry = client.post(f"{URL}/working-calendars/sync", headers=headers_for(admin)).json()
    assert [row["work_center_id"] for row in dry["rows"]] == [weld.id]

    commit = client.post(
        f"{URL}/working-calendars/sync",
        params={"dry_run": "false", "expected_version": saved.json()["version"]},
        headers=headers_for(admin),
    )
    assert commit.status_code == 200, commit.text
    db_session.expire_all()
    assert db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == rest.id).first() is None
    weld_calendar = db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == weld.id).one()
    assert weld_calendar.weekly_hours == [20, 20, 20, 20, 20, 0, 0]


def test_sync_commit_takes_the_production_calendar_lock_and_a_dry_run_does_not(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])

    with patch("app.services.production_calendar_service.acquire_generator_lock") as lock:
        client.post(f"{URL}/working-calendars/sync", headers=headers_for(admin))
        assert lock.call_count == 0
        commit = client.post(f"{URL}/working-calendars/sync", params={"dry_run": "false"}, headers=headers_for(admin))
        assert commit.status_code == 200, commit.text
    lock.assert_called_once()
    assert lock.call_args.args[1:] == ("production_calendar", COMPANY_A)


# --------------------------------------------------------------------------------------
# Finding 9: the PUT's IntegrityError mapping and the reserved "~" prefix
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["pattern", "shift"])
def test_names_and_codes_starting_with_the_reserved_prefix_are_refused(client: TestClient, db_session: Session, where):
    admin = _admin(db_session)
    weld = _welding(db_session)
    payload = werco_payload([weld.id])
    if where == "pattern":
        payload["patterns"][0]["name"] = "~1"
    else:
        payload["patterns"][0]["shifts"][0]["code"] = "~W1"
    resp = client.put(URL, json=payload, headers=headers_for(admin))
    assert resp.status_code == 422 and "cannot start with '~'" in resp.json()["detail"]


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception(message))


@pytest.mark.parametrize(
    "message,detail",
    [
        (
            'duplicate key value violates unique constraint "uq_production_calendar_company"',
            calendar_router.STALE_VERSION_DETAIL,
        ),
        ("UNIQUE constraint failed: production_calendar_settings.company_id", calendar_router.STALE_VERSION_DETAIL),
        (
            'insert or update on table "oee_records" violates foreign key constraint "fk_oee_records_shift_id"',
            calendar_router.CALENDAR_CONFLICT_DETAIL,
        ),
    ],
)
def test_put_integrity_errors_map_to_the_right_409(client: TestClient, db_session: Session, message, detail):
    admin = _admin(db_session)
    weld = _welding(db_session)

    def _raise(*_args, **_kwargs):
        raise _integrity_error(message)

    with patch.object(calendar_router, "save_production_calendar", _raise):
        resp = client.put(URL, json=werco_payload([weld.id]), headers=headers_for(admin))

    assert resp.status_code == 409
    assert resp.json()["detail"] == detail


# --------------------------------------------------------------------------------------
# Finding 4: the OEE sparkline is the per-day headline value, on the same inputs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("approved_only", [False, True])
def test_oee_sparkline_is_the_per_day_headline_value(db_session: Session, approved_only):
    """Same inputs as the headline: closed entries, good + scrapped pieces, the approved filter."""
    from app.services import analytics_service as analytics_module
    from app.services.analytics_service import AnalyticsService

    admin = _admin(db_session)
    weld = _welding(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, weld, status_=OperationStatus.IN_PROGRESS, run_time_per_piece=0.05)
    days = [date(2026, 11, 16), TUESDAY, WEDNESDAY]
    for index, day in enumerate(days):
        start = central(day.year, day.month, day.day, 6, 0)
        approved = make_entry(
            db_session,
            admin,
            wo,
            op,
            weld,
            clock_in=start,
            duration_hours=2,
            quantity_produced=10 + index,
            quantity_scrapped=index,
        )
        approved.approved = start
        # An unapproved entry (dropped when approval is required) and an open one (never counted).
        make_entry(db_session, admin, wo, op, weld, clock_in=start, duration_hours=1, quantity_produced=4)
        make_entry(db_session, admin, wo, op, weld, clock_in=start, open_entry=True, quantity_produced=50)
    db_session.commit()

    with patch.object(analytics_module, "is_approved_labor_required", return_value=approved_only):
        service = AnalyticsService(db_session, COMPANY_A)
        sparkline = service._get_oee_sparkline(days[0], days[-1], weld.id)
        per_day = [
            round(value, 1) if value is not None else 0.0
            for value in (AnalyticsService(db_session, COMPANY_A)._get_oee_value(d, d, weld.id) for d in days)
        ]

    assert sparkline == per_day
    assert all(point > 0 for point in sparkline)
