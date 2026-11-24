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
"""

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

import app.jobs.notification_jobs as notification_jobs
import app.services.company_time as company_time
from app.models.calibration import CalibrationStatus, Equipment
from app.models.user import UserRole
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
