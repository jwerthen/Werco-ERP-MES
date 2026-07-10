"""OEE service extraction + nightly auto-calc cron (Lean Phase 1, issue #88).

``services/oee_service.compute_oee_for_work_center`` is the former inline
``POST /oee/calculate/{wc}`` math (staffed-time availability, derived ideal
cycle, real scrap); the endpoint is now a thin delegate and the nightly ARQ
cron reuses the identical code. Locked here:

  * exact A/P/Q/OEE numbers for a seeded day via the endpoint (the extracted
    math on the OEE-1/4/5/7 conventions) + ``calculation_source='manual'``,
  * the migration-063 uniqueness rule: a second (company, WC, date, shift)
    record is a clean 409 on create, update-into-collision, and the NULL-vs-''
    shift key collision (COALESCE key: no shift == blank shift),
  * cron skip policy: manual records are authoritative (any shift), idle WCs
    are skipped, 'auto' rows are recomputed idempotently (still one row),
    ``calculation_source='auto'`` + created_by NULL, tenant confinement,
  * worker wiring: job registered and scheduled at 02:30.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.oee import CalculationSource, OEERecord
from app.models.time_entry import TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_downtime,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

RECORD_DATE = date(2026, 6, 4)
DAY_8AM = datetime(2026, 6, 4, 8, 0)


def _seed_measured_day(db: Session, *, company_id: int = COMPANY_A):
    """One WC-day whose OEE is hand-computable.

    staffed = RUN 6h + INSPECTION 2h = 480 min; unplanned downtime 60 min
    -> productive run 300 -> availability 300/480 = 62.5%.
    100 pieces cycled (80 good + 20 scrap) x 0.05 h/pc standard = 300 ideal min
    over 300 productive -> performance 100%. quality 80/100 = 80%.
    OEE = 0.625 x 1.0 x 0.8 = 50.0%.
    """
    user = make_user(db, role=UserRole.MANAGER, company_id=company_id)
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    wo = make_wo(db, part, company_id=company_id, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db, wo, wc, company_id=company_id, status_=OperationStatus.IN_PROGRESS, run_time_per_piece=0.05)
    make_entry(
        db,
        user,
        wo,
        op,
        wc,
        company_id=company_id,
        entry_type=TimeEntryType.RUN,
        clock_in=DAY_8AM,
        duration_hours=6,
        quantity_produced=80,
        quantity_scrapped=20,
    )
    make_entry(
        db,
        user,
        wo,
        op,
        wc,
        company_id=company_id,
        entry_type=TimeEntryType.INSPECTION,
        clock_in=DAY_8AM,
        duration_hours=2,
    )
    make_downtime(db, user, wc, company_id=company_id, start_time=DAY_8AM + timedelta(hours=1), duration_minutes=60)
    return user, wc


# ---------------------------------------------------------------------------
# Extracted math via the (now thin) endpoint
# ---------------------------------------------------------------------------


def test_auto_calculate_exact_apq_and_manual_source(client: TestClient, db_session: Session):
    user, wc = _seed_measured_day(db_session)

    resp = client.post(
        f"/api/v1/oee/calculate/{wc.id}",
        params={"record_date": RECORD_DATE.isoformat()},
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["availability_pct"] == pytest.approx(62.5)
    assert body["performance_pct"] == pytest.approx(100.0)
    assert body["quality_pct"] == pytest.approx(80.0)
    assert body["oee_pct"] == pytest.approx(50.0)
    assert body["planned_production_time_minutes"] == pytest.approx(480.0)
    assert body["actual_run_time_minutes"] == pytest.approx(300.0)
    assert body["downtime_minutes"] == pytest.approx(60.0)
    assert body["good_parts"] == 80
    assert body["total_parts"] == 100
    assert body["defect_parts"] == 20
    assert body["ideal_cycle_time_seconds"] == pytest.approx(180.0)
    # The on-demand trigger is a human action -> stamped 'manual'.
    assert body["calculation_source"] == CalculationSource.MANUAL.value

    record = db_session.query(OEERecord).filter(OEERecord.work_center_id == wc.id).one()
    assert record.calculation_source == "manual"
    assert record.created_by == user.id


# ---------------------------------------------------------------------------
# uq_oee_company_wc_date_shift -> 409, not 500
# ---------------------------------------------------------------------------


def _record_payload(wc_id: int, shift):
    return {
        "work_center_id": wc_id,
        "record_date": RECORD_DATE.isoformat(),
        "shift": shift,
        "planned_production_time_minutes": 480,
        "actual_run_time_minutes": 400,
        "total_parts_produced": 10,
        "ideal_cycle_time_seconds": 60,
        "actual_operating_time_minutes": 400,
        "good_parts": 10,
        "total_parts": 10,
    }


def test_duplicate_record_create_is_409(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)

    first = client.post("/api/v1/oee/records", json=_record_payload(wc.id, "Day"), headers=headers_for(user))
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post("/api/v1/oee/records", json=_record_payload(wc.id, "Day"), headers=headers_for(user))
    assert second.status_code == status.HTTP_409_CONFLICT, second.text
    assert "already exists" in second.json()["detail"]
    # Exactly one row survived.
    assert db_session.query(OEERecord).filter(OEERecord.work_center_id == wc.id).count() == 1


def test_null_shift_and_blank_shift_are_the_same_record_key(client: TestClient, db_session: Session):
    """COALESCE(shift, '') in the unique key: no shift and blank shift collide."""
    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)

    first = client.post("/api/v1/oee/records", json=_record_payload(wc.id, None), headers=headers_for(user))
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post("/api/v1/oee/records", json=_record_payload(wc.id, ""), headers=headers_for(user))
    assert second.status_code == status.HTTP_409_CONFLICT, second.text


def test_update_into_shift_collision_is_409(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)

    client.post("/api/v1/oee/records", json=_record_payload(wc.id, "A"), headers=headers_for(user))
    created_b = client.post("/api/v1/oee/records", json=_record_payload(wc.id, "B"), headers=headers_for(user))
    assert created_b.status_code == status.HTTP_200_OK, created_b.text

    resp = client.put(f"/api/v1/oee/records/{created_b.json()['id']}", json={"shift": "A"}, headers=headers_for(user))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text


def test_same_key_in_another_company_does_not_collide(db_session: Session):
    """The unique key leads with company_id: tenant B may hold the same WC-less
    (date, shift) shape without tripping tenant A's records."""
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    for cid, wc in ((COMPANY_A, wc_a), (COMPANY_B, wc_b)):
        record = OEERecord(work_center_id=wc.id, record_date=RECORD_DATE, shift=None)
        record.company_id = cid
        db_session.add(record)
    db_session.commit()  # no IntegrityError
    assert db_session.query(OEERecord).count() == 2


# ---------------------------------------------------------------------------
# Nightly cron (run_oee_auto_calc_task)
# ---------------------------------------------------------------------------


@pytest.fixture
def _cron_session(db_session, monkeypatch):
    """Point the job's SessionLocal at the test session (test_maintenance_jobs idiom)."""
    import app.jobs.oee_jobs as oee_jobs

    monkeypatch.setattr(oee_jobs, "SessionLocal", lambda: db_session)
    return db_session


async def test_cron_computes_auto_records_and_reruns_idempotently(_cron_session):
    from app.jobs.oee_jobs import run_oee_auto_calc_task

    db = _cron_session
    user, wc = _seed_measured_day(db)
    user_id, wc_id = user.id, wc.id

    result = await run_oee_auto_calc_task(company_id=COMPANY_A, record_date=RECORD_DATE)
    assert result["computed"] == 1
    assert result["errors"] == 0

    record = db.query(OEERecord).filter(OEERecord.work_center_id == wc_id).one()
    assert record.calculation_source == CalculationSource.AUTO.value
    assert record.shift is None  # whole-day row
    assert record.created_by is None  # system actor
    assert record.oee_pct == pytest.approx(50.0)
    first_id = record.id

    # New evidence lands, the cron re-runs: same row recomputed, not a duplicate.
    # (db.close() in the task detached the fixture objects; re-fetch them.)
    from app.models.user import User
    from app.models.work_center import WorkCenter

    user = db.get(User, user_id)
    wc = db.get(WorkCenter, wc_id)
    make_entry(
        db,
        user,
        None,
        None,
        wc,
        entry_type=TimeEntryType.RUN,
        clock_in=DAY_8AM + timedelta(hours=7),
        duration_hours=1,
        quantity_produced=0,
    )
    rerun = await run_oee_auto_calc_task(company_id=COMPANY_A, record_date=RECORD_DATE)
    assert rerun["computed"] == 1

    db.expire_all()
    records = db.query(OEERecord).filter(OEERecord.work_center_id == wc_id).all()
    assert len(records) == 1
    assert records[0].id == first_id
    assert records[0].planned_production_time_minutes == pytest.approx(540.0)  # 480 + 60 staffed

    # The system write is audited on the tamper-evident chain.
    audit = db.query(AuditLog).filter(AuditLog.resource_type == "oee_record", AuditLog.resource_id == first_id).all()
    assert len(audit) >= 2  # create + recompute update
    assert all(row.company_id == COMPANY_A for row in audit)


async def test_cron_never_overwrites_a_manual_record_any_shift(_cron_session):
    from app.jobs.oee_jobs import run_oee_auto_calc_task

    db = _cron_session
    user, wc = _seed_measured_day(db)
    # A hand-entered per-shift record exists for the same WC/day.
    manual = OEERecord(
        work_center_id=wc.id,
        record_date=RECORD_DATE,
        shift="Day",
        calculation_source=CalculationSource.MANUAL.value,
        oee_pct=42.0,
    )
    manual.company_id = COMPANY_A
    db.add(manual)
    db.commit()

    result = await run_oee_auto_calc_task(company_id=COMPANY_A, record_date=RECORD_DATE)
    assert result["skipped_manual"] == 1
    assert result["computed"] == 0

    db.expire_all()
    records = db.query(OEERecord).filter(OEERecord.work_center_id == wc.id).all()
    assert len(records) == 1  # no auto sibling row was added
    assert records[0].oee_pct == pytest.approx(42.0)  # untouched


async def test_cron_skips_idle_work_centers_and_stays_in_tenant(_cron_session):
    from app.jobs.oee_jobs import run_oee_auto_calc_task

    db = _cron_session
    # Company A: one idle WC (no entries, no downtime).
    idle_wc = make_work_center(db, company_id=COMPANY_A)
    # Company B: a fully measured day that must NOT be touched by an A-scoped run.
    _seed_measured_day(db, company_id=COMPANY_B)

    result = await run_oee_auto_calc_task(company_id=COMPANY_A, record_date=RECORD_DATE)
    assert result["skipped_idle"] == 1
    assert result["computed"] == 0
    assert db.query(OEERecord).count() == 0  # neither tenant got a row from this run

    assert db.query(OEERecord).filter(OEERecord.work_center_id == idle_wc.id).count() == 0


# ---------------------------------------------------------------------------
# Worker wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_oee_job_registered_and_scheduled_at_0230():
    from app.worker import WorkerSettings, run_oee_auto_calc_job

    assert run_oee_auto_calc_job in WorkerSettings.functions

    entry = next(cj for cj in WorkerSettings.cron_jobs if cj.coroutine is run_oee_auto_calc_job)
    assert entry.hour in (2, {2})
    assert entry.minute in (30, {30})


@pytest.mark.unit
def test_oee_job_wrapper_parses_iso_record_date(monkeypatch):
    import asyncio

    import app.jobs.oee_jobs as oee_jobs
    import app.worker as worker

    captured = {}

    async def fake_task(company_id=None, record_date=None):
        captured["company_id"] = company_id
        captured["record_date"] = record_date
        return {"ok": True}

    monkeypatch.setattr(oee_jobs, "run_oee_auto_calc_task", fake_task)

    asyncio.run(worker.run_oee_auto_calc_job({"job_id": "t"}, company_id=7, record_date="2026-06-04"))
    assert captured["company_id"] == 7
    assert captured["record_date"] == date(2026, 6, 4)

    asyncio.run(worker.run_oee_auto_calc_job({"job_id": "t"}))
    assert captured["record_date"] is None
