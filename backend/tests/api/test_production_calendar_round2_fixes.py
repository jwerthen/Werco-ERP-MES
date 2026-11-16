"""Regressions from the P1 round-2 review (2026-11-15) on the production calendar.

* **Long intervals vanished from every production day.** The attribution helpers selected
  rows by ``clock_in >= window start - 12 h``, but attribution (greatest shift overlap) can
  put a long interval on a LATER production day than that look-back reaches -- the start
  day's query read it and dropped it as another day's, and the attributed day's query never
  read it. A forgotten clock-out closed the next afternoon, or a breakdown over two nights,
  was counted on no day at all. The selection now reads every interval that OVERLAPS the
  range back to the attribution reach (``attribution_read_from``).
* **The working-calendar sync could store more than 24 hours a day** (parallel crews in one
  pattern), which the scheduling editor's own PUT then refused on every weekday.
* **A work center listed twice in one pattern** was refused as "in both P and P".

Werco's calendar (tests/production_calendar_helpers.py): day start 05:00 Central, weld
W1 05:00-15:30 / W2 15:30-02:00, rest of shop S1 07:30-16:00 / S2 15:30-00:00, Mon-Fri.
2026-11-16 is a Monday (CST, UTC-6).
"""

from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.production_calendar import ShiftDefinition
from app.models.user import UserRole
from app.models.working_calendar import WorkingCalendar
from app.services.oee_service import _attributed_downtime, _attributed_entries
from app.services.production_calendar_service import load_production_calendar
from app.services.working_calendar_service import save_working_calendar
from tests.lean_phase1_helpers import COMPANY_A, headers_for, make_downtime, make_entry, make_user, make_work_center
from tests.production_calendar_helpers import seed_werco_calendar

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/production-calendar"
SYNC_URL = f"{URL}/working-calendars/sync"
MONDAY = date(2026, 11, 16)
TUESDAY = date(2026, 11, 17)
WEDNESDAY = date(2026, 11, 18)


def central(year, month, day, hour, minute=0):
    return (
        datetime(year, month, day, hour, minute, tzinfo=CENTRAL_TIME_ZONE).astimezone(timezone.utc).replace(tzinfo=None)
    )


def _admin(db):
    return make_user(db, role=UserRole.ADMIN)


def _employee_time(client, admin, start: date, end: date):
    resp = client.get(
        "/api/v1/reports/employee-time",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        headers=headers_for(admin),
    )
    assert resp.status_code == 200, resp.text
    return [(e["date"], e["shift_code"]) for employee in resp.json() for e in employee["entries"]]


def _oee_entry_ids(db, work_center_id, day):
    cal = load_production_calendar(db, COMPANY_A)
    return [row.id for row in _attributed_entries(db, COMPANY_A, work_center_id, day, cal, None)]


def _oee_downtime_ids(db, work_center_id, day):
    cal = load_production_calendar(db, COMPANY_A)
    return [row.id for row in _attributed_downtime(db, COMPANY_A, work_center_id, day, cal, None)]


def _one_shift_payload(days, *, expected_version=0):
    """A default pattern with one 07:00-15:00 shift on ``days``; day start 05:00."""
    return {
        "expected_version": expected_version,
        "production_day_start": "05:00",
        "patterns": [
            {
                "name": "Only shift",
                "is_default": True,
                "work_center_ids": [],
                "shifts": [
                    {
                        "code": "X1",
                        "name": "Day",
                        "start": "07:00",
                        "end": "15:00",
                        "days": days,
                        "planned_minutes": 450,
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------------------
# Finding 1: a long interval is counted on the production day it is attributed to
# --------------------------------------------------------------------------------------


def test_a_24_hour_weld_entry_counts_on_tuesday_in_oee_and_the_reports(client: TestClient, db_session: Session):
    """Mon 16:00 -> Tue 16:00 at a weld bay: W1 Tuesday (10.5 h) beats W2 Monday (10 h)."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    entry = make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 16, 16, 0), duration_hours=24)

    assert _oee_entry_ids(db_session, weld.id, TUESDAY) == [entry.id]
    assert _oee_entry_ids(db_session, weld.id, MONDAY) == []

    assert _employee_time(client, admin, TUESDAY, TUESDAY) == [("2026-11-17", "W1")]
    assert _employee_time(client, admin, MONDAY, MONDAY) == []
    # A range that STARTS on the attributed day reads it too.
    assert _employee_time(client, admin, TUESDAY, WEDNESDAY) == [("2026-11-17", "W1")]


def test_a_42_hour_breakdown_counts_against_tuesday_oee(client: TestClient, db_session: Session):
    """Mon 16:00 -> Wed 10:00 at a rest-of-shop machine: S1 Tuesday (8.5 h) beats S2 Monday (8 h)."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    machine = make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    event = make_downtime(db_session, admin, machine, start_time=central(2026, 11, 16, 16, 0), duration_minutes=42 * 60)

    assert _oee_downtime_ids(db_session, machine.id, TUESDAY) == [event.id]
    assert _oee_downtime_ids(db_session, machine.id, MONDAY) == []
    assert _oee_downtime_ids(db_session, machine.id, WEDNESDAY) == []


def test_a_downtime_row_with_only_a_duration_is_still_read_by_its_attributed_day(
    client: TestClient, db_session: Session
):
    """``end_time`` NULL + ``duration_minutes``: the end is derived in Python, so SQL must not filter it out."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    machine = make_work_center(db_session)
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    event = make_downtime(db_session, admin, machine, start_time=central(2026, 11, 16, 16, 0), duration_minutes=42 * 60)
    event.end_time = None
    db_session.commit()

    assert _oee_downtime_ids(db_session, machine.id, TUESDAY) == [event.id]


def test_a_single_shift_calendar_keeps_a_forgotten_clock_out(client: TestClient, db_session: Session):
    """07:00-15:00 shop: Mon 10:00 -> Tue 14:00 overlaps Tuesday 7 h, Monday 5 h -> Tuesday."""
    admin = _admin(db_session)
    machine = make_work_center(db_session)
    saved = client.put(URL, json=_one_shift_payload([0, 1, 2, 3, 4]), headers=headers_for(admin))
    assert saved.status_code == 200, saved.text
    entry = make_entry(db_session, admin, None, None, machine, clock_in=central(2026, 11, 16, 10, 0), duration_hours=28)

    assert _oee_entry_ids(db_session, machine.id, TUESDAY) == [entry.id]
    assert _oee_entry_ids(db_session, machine.id, MONDAY) == []
    assert _employee_time(client, admin, TUESDAY, TUESDAY) == [("2026-11-17", "X1")]


def test_an_interval_attributed_two_days_after_its_start_is_read(client: TestClient, db_session: Session):
    """A Wednesday-only shift takes a Mon 06:00 -> Wed 16:00 entry: the reach, not a look-back, bounds the read."""
    admin = _admin(db_session)
    machine = make_work_center(db_session)
    saved = client.put(URL, json=_one_shift_payload([2]), headers=headers_for(admin))
    assert saved.status_code == 200, saved.text
    entry = make_entry(db_session, admin, None, None, machine, clock_in=central(2026, 11, 16, 6, 0), duration_hours=58)

    assert _oee_entry_ids(db_session, machine.id, WEDNESDAY) == [entry.id]
    assert _oee_entry_ids(db_session, machine.id, MONDAY) == []
    assert _oee_entry_ids(db_session, machine.id, TUESDAY) == []


def test_short_entries_are_unchanged(client: TestClient, db_session: Session):
    """The ordinary cases still land where they did: the 04:55 early weld clock-in and a plain shift."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    early = make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 17, 4, 55), duration_hours=2)
    night = make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 17, 16, 0), duration_hours=9)
    before = make_entry(db_session, admin, None, None, weld, clock_in=central(2026, 11, 16, 6, 0), duration_hours=8)

    assert sorted(_oee_entry_ids(db_session, weld.id, TUESDAY)) == sorted([early.id, night.id])
    assert _oee_entry_ids(db_session, weld.id, MONDAY) == [before.id]


# --------------------------------------------------------------------------------------
# Finding 2: a working calendar never gets more than 24 hours a day from the sync
# --------------------------------------------------------------------------------------


def _parallel_crews_payload(work_center_id, *, b1_active=True, b1_planned=600):
    weekdays = [0, 1, 2, 3, 4]
    return {
        "expected_version": 0,
        "production_day_start": "05:00",
        "patterns": [
            {
                "name": "Two crews",
                "is_default": False,
                "work_center_ids": [work_center_id],
                "shifts": [
                    {"code": "A1", "name": "A 1st", "start": "05:00", "end": "15:30", "days": weekdays},
                    {"code": "A2", "name": "A 2nd", "start": "15:30", "end": "02:00", "days": weekdays},
                    {
                        "code": "B1",
                        "name": "B 1st",
                        "start": "05:00",
                        "end": "15:30",
                        "days": weekdays,
                        "planned_minutes": b1_planned,
                        "is_active": b1_active,
                    },
                ],
            }
        ],
    }


def _with_planned(payload, minutes=600):
    for shift in payload["patterns"][0]["shifts"]:
        shift.setdefault("planned_minutes", minutes)
    return payload


def test_put_refuses_active_shifts_planning_more_than_a_day(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")

    resp = client.put(URL, json=_with_planned(_parallel_crews_payload(weld.id)), headers=headers_for(admin))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "Shift pattern 'Two crews' plans 1800 minutes of active shifts starting on Monday; the shifts starting on "
        "one day can plan at most 1440 minutes (24 hours), the most a work center's working calendar holds for a day."
    )


def test_exactly_a_day_of_planned_time_and_retired_shifts_are_accepted(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    # 600 + 600 + 240 = 1440: the limit itself is allowed.
    exact = _with_planned(_parallel_crews_payload(weld.id, b1_planned=240))
    resp = client.put(URL, json=exact, headers=headers_for(admin))
    assert resp.status_code == 200, resp.text

    # A retired shift plans nothing.
    retired = _with_planned(_parallel_crews_payload(weld.id, b1_active=False))
    retired["expected_version"] = resp.json()["version"]
    for pattern, saved_pattern in zip(retired["patterns"], resp.json()["patterns"]):
        pattern["id"] = saved_pattern["id"]
        for shift, saved_shift in zip(pattern["shifts"], saved_pattern["shifts"]):
            shift["id"] = saved_shift["id"]
    assert client.put(URL, json=retired, headers=headers_for(admin)).status_code == 200

    commit = client.post(SYNC_URL, params={"dry_run": "false"}, headers=headers_for(admin))
    assert commit.status_code == 200, commit.text
    assert commit.json()["rows"][0]["new_weekly_hours"] == [20, 20, 20, 20, 20, 0, 0]


def test_sync_refuses_a_stored_calendar_over_a_day_and_writes_nothing(client: TestClient, db_session: Session):
    """A calendar stored around ``_validate`` (here: re-activating a shift in the database)."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    saved = client.put(
        URL, json=_with_planned(_parallel_crews_payload(weld.id, b1_active=False)), headers=headers_for(admin)
    )
    assert saved.status_code == 200, saved.text
    db_session.query(ShiftDefinition).filter(ShiftDefinition.code == "B1").update({"is_active": True})
    db_session.commit()

    detail = (
        f"The production calendar plans more than 24 hours on one day for work center(s) {weld.code} ('Two crews'). "
        "Lower the planned minutes of the active shifts that start that day, save the calendar, then sync again."
    )
    dry = client.post(SYNC_URL, headers=headers_for(admin))
    assert (dry.status_code, dry.json()["detail"]) == (422, detail)
    commit = client.post(SYNC_URL, params={"dry_run": "false"}, headers=headers_for(admin))
    assert (commit.status_code, commit.json()["detail"]) == (422, detail)

    db_session.expire_all()
    assert db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == weld.id).first() is None


def test_the_shared_writer_refuses_hours_outside_a_day(db_session: Session):
    """``save_working_calendar`` is the one writer: it refuses for every caller, not only the scheduling PUT."""
    weld = make_work_center(db_session, work_center_type="welding")
    for bad in ([30.0] * 7, [8.0] * 6, [-1.0] + [8.0] * 6, [float("nan")] + [8.0] * 6):
        with pytest.raises(HTTPException) as refused:
            save_working_calendar(
                db_session, COMPANY_A, weld, weekly_hours=bad, overrides=[], expected_version=0, user_id=1
            )
        assert refused.value.status_code == 422
    assert db_session.query(WorkingCalendar).filter(WorkingCalendar.work_center_id == weld.id).first() is None


def test_synced_hours_round_trip_through_the_scheduling_editor(client: TestClient, db_session: Session):
    """The editor's read-modify-write (GET then PUT the same weekly_hours) accepts whatever the sync wrote."""
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    seed_werco_calendar(db_session, COMPANY_A, admin.id, [weld.id])
    assert client.post(SYNC_URL, params={"dry_run": "false"}, headers=headers_for(admin)).status_code == 200

    current = client.get(f"/api/v1/scheduling/work-centers/{weld.id}/calendar", headers=headers_for(admin))
    assert current.status_code == 200, current.text
    body = current.json()
    resp = client.put(
        f"/api/v1/scheduling/work-centers/{weld.id}/calendar",
        json={"expected_version": body["version"], "weekly_hours": body["weekly_hours"], "overrides": []},
        headers=headers_for(admin),
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------------------
# Nit: a work center listed twice in the same pattern
# --------------------------------------------------------------------------------------


def test_a_work_center_listed_twice_in_one_pattern_is_one_membership(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    payload = _with_planned(_parallel_crews_payload(weld.id, b1_active=False))
    payload["patterns"][0]["work_center_ids"] = [weld.id, weld.id]

    resp = client.put(URL, json=payload, headers=headers_for(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json()["patterns"][0]["work_center_ids"] == [weld.id]


def test_a_work_center_in_two_patterns_is_still_refused(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    weld = make_work_center(db_session, work_center_type="welding")
    payload = _with_planned(_parallel_crews_payload(weld.id, b1_active=False))
    second = _one_shift_payload([0])["patterns"][0]
    second["work_center_ids"] = [weld.id]
    second["is_default"] = False
    payload["patterns"].append(second)

    resp = client.put(URL, json=payload, headers=headers_for(admin))

    assert resp.status_code == 422
    assert resp.json()["detail"].startswith(f"Work center {weld.id} is in both 'Two crews' and 'Only shift'")
