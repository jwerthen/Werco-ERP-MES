"""Behavior locks for the Batch-8 OEE/OTD metric correctness fixes (Rank 11).

Covered findings:
- OEE-1: POST /oee/calculate/{wc} no longer 500s (it referenced TimeEntry.start_time/
  end_time which DO NOT EXIST); it now reads clock_in/clock_out + duration_hours and
  writes a sane OEERecord (200).
- OEE-4: availability is computed against STAFFED (clocked) time per WC, so reported
  downtime / un-clocked idle reduces it (no longer pinned ~1.0 against plant capacity).
- OEE-5: pieces/scrap are counted across the production-bearing entry types (RUN+REWORK),
  so scrap logged on a REWORK clock-out is not silently dropped.
- OEE-6: OTD returns None ("n/a") on an empty denominator (not 100.0), and a COMPLETE WO
  with a NULL actual_end is counted as NOT on-time.
- OEE-7: ideal cycle is DERIVED from WorkOrderOperation.run_time_per_piece and quality
  comes from real scrap, not an assumed all-good / hardcoded 60 s.

Tenant scoping (Batch 1) is preserved: every OEE/OTD query stays company-scoped. A
cross-tenant exclusion is asserted on the auto-calc path.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.oee import OEERecord
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.analytics_service import AnalyticsService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    n = _next()
    user = User(
        email=f"b8-{n}@co{company_id}.test",
        employee_id=f"B8-{n:05d}",
        first_name="B8",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"B8-P-{n}",
        name=f"Part {n}",
        description="batch8 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=0.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, company_id: int = COMPANY_A, *, capacity_hours_per_day: float = 8.0) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"B8-WC-{n}",
        code=f"B8-WC-{n}",
        work_center_type="machining",
        description="batch8 fixture work center",
        capacity_hours_per_day=capacity_hours_per_day,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    status_: WorkOrderStatus,
    company_id: int = COMPANY_A,
    quantity_ordered: float = 10,
    due_date=None,
    actual_end=None,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B8-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=due_date,
        actual_end=actual_end,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    status_: OperationStatus = OperationStatus.IN_PROGRESS,
    run_time_per_piece: float = 0.0,
    scheduled: bool = False,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        run_time_per_piece=run_time_per_piece,
        scheduled_start=datetime.utcnow() if scheduled else None,
        scheduled_end=datetime.utcnow() + timedelta(hours=2) if scheduled else None,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def make_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    entry_type: TimeEntryType,
    duration_hours: float,
    when: date,
    quantity_produced: float = 0.0,
    quantity_scrapped: float = 0.0,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    clock_in = datetime.combine(when, datetime.min.time()) + timedelta(hours=8)
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=clock_in,
        clock_out=clock_in + timedelta(hours=duration_hours),
        duration_hours=duration_hours,
        quantity_produced=quantity_produced,
        quantity_scrapped=quantity_scrapped,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_downtime(
    db: Session,
    user: User,
    wc: WorkCenter,
    *,
    duration_minutes: float,
    when: date,
    planned: bool = False,
    company_id: int = COMPANY_A,
) -> DowntimeEvent:
    start = datetime.combine(when, datetime.min.time()) + timedelta(hours=9)
    event = DowntimeEvent(
        work_center_id=wc.id,
        start_time=start,
        end_time=start + timedelta(minutes=duration_minutes),
        duration_minutes=duration_minutes,
        category=DowntimeCategory.MECHANICAL,
        planned_type=DowntimePlannedType.PLANNED if planned else DowntimePlannedType.UNPLANNED,
        reported_by=user.id,
        company_id=company_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ───────────────────────── OEE-1 / OEE-7: auto-calc endpoint ─────────────────────


def test_auto_calculate_oee_returns_200_and_writes_sane_record(client, db_session):
    """OEE-1: the endpoint that always 500'd (TimeEntry.start_time/end_time do not
    exist) now returns 200 and persists a valid OEERecord from clock_in/clock_out."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    # run_time_per_piece = 0.1 h/pc (6 min/pc) -> ideal cycle DERIVED, not hardcoded 60 s.
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    today = date.today()
    # 8 clocked hours: 6 h RUN producing 50 good + 5 scrap, 2 h DOWNTIME (idle).
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=6.0,
        when=today,
        quantity_produced=50,
        quantity_scrapped=5,
    )
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.DOWNTIME,
        duration_hours=2.0,
        when=today,
    )

    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    # Quality from real scrap (OEE-7): 50 good of 55 total -> ~90.9%, not 100%.
    assert body["good_parts"] == 50
    assert body["defect_parts"] == 5
    assert body["total_parts"] == 55
    assert 90.0 < body["quality_pct"] < 91.5
    # Ideal cycle DERIVED (0.1 h/pc -> 360 s/pc), not the old hardcoded 60 s.
    assert abs(body["ideal_cycle_time_seconds"] - 360.0) < 1.0
    # Availability against STAFFED time (8 h): productive run = 6 h RUN -> 0.75, well
    # below the old ~1.0 pinned value (OEE-4).
    assert 70.0 < body["availability_pct"] < 80.0
    assert 0.0 < body["oee_pct"] < 100.0

    # Record persisted and tenant-stamped.
    rec = db_session.query(OEERecord).filter(OEERecord.work_center_id == wc.id, OEERecord.company_id == COMPANY_A).one()
    assert rec.company_id == COMPANY_A
    assert rec.total_parts == 55


def test_auto_calculate_oee_unplanned_downtime_lowers_availability(client, db_session):
    """OEE-7: reported UNPLANNED DowntimeEvent time is subtracted from productive run,
    lowering availability further than idle alone."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.05)
    today = date.today()
    # 8 h fully clocked as RUN, but 2 h reported machine downtime within it.
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=8.0,
        when=today,
        quantity_produced=40,
        quantity_scrapped=0,
    )
    make_downtime(db_session, admin, wc, duration_minutes=120.0, when=today)

    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # productive run = 8 h RUN - 2 h downtime = 6 h; availability = 6/8 = 0.75.
    assert 70.0 < body["availability_pct"] < 80.0
    assert body["downtime_minutes"] == 120.0


def test_auto_calculate_oee_tenant_scoped(client, db_session):
    """Tenant scoping preserved: company B's entries never feed company A's record."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    admin_b = make_user(db_session, company_id=COMPANY_B)
    part_b = make_part(db_session, company_id=COMPANY_B)
    # Same WC id space but distinct companies.
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    wo_b = make_wo(db_session, part_b, status_=WorkOrderStatus.IN_PROGRESS, company_id=COMPANY_B)
    op_b = make_op(db_session, wo_b, wc_a, company_id=COMPANY_B)  # B entry on WC id of A
    make_entry(
        db_session,
        admin_b,
        wo_b,
        op_b,
        entry_type=TimeEntryType.RUN,
        duration_hours=5.0,
        when=date.today(),
        quantity_produced=99,
        company_id=COMPANY_B,
    )

    resp = client.post(f"/api/v1/oee/calculate/{wc_a.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # No company-A staffed time at this WC -> zero parts, B's 99 never leak in.
    assert resp.json()["total_parts"] == 0


# ───────────────────────── OEE-4/5: AnalyticsService OEE value ───────────────────


def test_oee_value_none_when_no_staffed_time(db_session):
    """OEE-4: a WC with no clocked time in the window is genuinely uncomputable -> None
    ("n/a"), never a misleading 0/100."""
    make_user(db_session)
    wc = make_work_center(db_session)
    svc = AnalyticsService(db_session, COMPANY_A)
    today = date.today()
    assert svc._get_oee_value(today, today, wc.id) is None


def test_oee_quality_counts_rework_scrap(db_session):
    """OEE-5: scrap logged on a REWORK clock-out is counted (production-bearing), not
    silently dropped from the quality leg."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    today = date.today()
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=4.0,
        when=today,
        quantity_produced=40,
        quantity_scrapped=0,
    )
    # 10 produced + 10 scrapped on a REWORK entry -> scrap must reduce quality.
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.REWORK,
        duration_hours=2.0,
        when=today,
        quantity_produced=10,
        quantity_scrapped=10,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    oee = svc._get_oee_value(today, today, wc.id)
    assert oee is not None
    # Pieces/scrap span RUN+REWORK (production-bearing): good = 50 (40 RUN + 10 REWORK
    # produced), scrap = 10 -> total = 60, quality = 50/60 ≈ 0.833. If the REWORK scrap
    # were dropped (the OEE-5 bug), total would be 50 and quality 1.0.
    # Productive run = RUN+SETUP = 4 h (the spec's productive portion; REWORK time is
    # staffed but not productive-run). availability = 4 h ÷ 6 h staffed ≈ 0.667; ideal =
    # 50*0.1 = 5 h, performance = min(5/4, 1) = 1.0. OEE ≈ 0.667 * 1.0 * 0.833 ≈ 55.6%.
    assert 52.0 < oee < 58.0  # quality clearly applied; would be ~67% if scrap dropped


# ───────────────────────────── OEE-6: OTD honesty ───────────────────────────────


def test_otd_none_on_empty_denominator(db_session):
    """OEE-6: no completed WO with a due date in the window -> OTD is None ("n/a"),
    NOT a misleading perfect 100.0."""
    make_user(db_session)
    svc = AnalyticsService(db_session, COMPANY_A)
    today = date.today()
    assert svc._get_otd_value(today - timedelta(days=7), today) is None


def test_otd_late_wo_with_null_actual_end_counts_as_late(db_session):
    """OEE-6: a COMPLETE WO with a NULL actual_end (completion never stamped it) is
    counted as NOT on-time rather than dropped from the denominator."""
    make_user(db_session)
    part = make_part(db_session)
    # updated_at is stamped in UTC; anchor the window on the UTC date and pad a day so
    # the just-now record is inside it regardless of local-vs-UTC date boundary.
    utc_today = datetime.utcnow().date()
    # COMPLETE, due 5 days ago, NULL actual_end -> updated_at (now) anchors it in window.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        due_date=utc_today - timedelta(days=5),
        actual_end=None,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    otd = svc._get_otd_value(utc_today - timedelta(days=1), utc_today + timedelta(days=1))
    # One WO in the denominator, zero on-time -> 0.0 (not None, not 100.0).
    assert otd == 0.0


def test_otd_on_time_wo_counts(db_session):
    """OEE-6: a COMPLETE WO finished on/before its due date counts as on-time."""
    make_user(db_session)
    part = make_part(db_session)
    utc_today = datetime.utcnow().date()
    end_dt = datetime.combine(utc_today, datetime.min.time()) + timedelta(hours=10)
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        due_date=utc_today + timedelta(days=2),
        actual_end=end_dt,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    otd = svc._get_otd_value(utc_today - timedelta(days=1), utc_today + timedelta(days=1))
    assert otd == 100.0


# ────────────────────── /kpis endpoint: n/a serializes cleanly ──────────────────


def test_kpi_dashboard_serializes_na_oee_and_otd(client, db_session):
    """OEE-4/OEE-6: the /analytics/kpis endpoint returns 200 with null ("n/a") OEE and
    OTD values when there's no data, not a misleading 0/100 and not a 500."""
    admin = make_user(db_session)
    resp = client.get("/api/v1/analytics/kpis?period=7d", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # No staffed time / no completed WOs in a fresh tenant window -> both are null.
    assert body["oee"]["value"] is None
    assert body["on_time_delivery"]["value"] is None


# ─────────────────────────── MS-5: capacity release ─────────────────────────────


def test_completing_operation_clears_schedule_reservation(client, db_session):
    """MS-5: a completed operation no longer reserves capacity — its scheduled_start/
    scheduled_end are cleared on completion, so capacity is freed by DATA rather than by
    every reader remembering the ``status != COMPLETE`` predicate."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    op = make_op(
        db_session,
        wo,
        wc,
        status_=OperationStatus.IN_PROGRESS,
        scheduled=True,  # carries scheduled_start/scheduled_end
    )
    assert op.scheduled_start is not None and op.scheduled_end is not None

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    assert op.status == OperationStatus.COMPLETE
    # Reservation freed by data (MS-5).
    assert op.scheduled_start is None
    assert op.scheduled_end is None


# ══════════════════════ Batch-8 matrix extension (test-engineer) ═════════════════
# Fills the gaps the original 10 locks left open:
#   * Availability decomposed exactly via NON-productive staffed time (no downtime).
#   * Performance leg isolated to the formula (only leg < 1.0).
#   * Quality leg isolated to RUN scrap (the original quality lock rode on REWORK).
#   * auto-OEE endpoint: performance leg from run_time_per_piece (a·p·q decomposed).
#   * OTD exact ratio on a mixed set incl. a STAMPED-late WO (actual_end > due_date),
#     distinct from the existing null-actual_end case.
#   * MS-5 on the RECONCILE-on-read path (the original only covered the live path).
#   * No regression: get_oee_details + OTD sparkline still serve floats with the
#     Optional KPIValue.value schema change.


# ───────────────────── Availability: exact proportion (OEE-4) ────────────────────


def test_oee_availability_exact_from_nonproductive_staffed_time(db_session):
    """OEE-4: staffed (clocked) time that is NOT productive run (e.g. INSPECTION) is in
    the availability denominator but not the numerator, so availability is the exact
    productive/staffed ratio — independent of any DowntimeEvent. Performance and quality
    are pinned at 1.0 so the only sub-1.0 leg is availability."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    # run_time_per_piece 0.2 h/pc; 30 pieces -> ideal = 6 h == productive run -> perf 1.0.
    op = make_op(db_session, wo, wc, run_time_per_piece=0.2)
    today = date.today()
    # 6 h RUN producing 30 good + 0 scrap (perf=1.0, quality=1.0) ...
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=6.0,
        when=today,
        quantity_produced=30,
        quantity_scrapped=0,
    )
    # ... plus 2 h INSPECTION: staffed but NOT productive-run and NOT production-bearing.
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.INSPECTION,
        duration_hours=2.0,
        when=today,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    oee = svc._get_oee_value(today, today, wc.id)
    assert oee is not None
    # staffed = 8 h, productive run = 6 h -> availability = 0.75 exactly; perf=quality=1.0.
    # OEE = 0.75 * 1.0 * 1.0 * 100 = 75.0. (If INSPECTION leaked into the numerator the
    # availability would wrongly read 1.0 / OEE 100; if it were dropped from the
    # denominator likewise.)
    assert abs(oee - 75.0) < 0.01


# ────────────────────── Performance: isolated leg (OEE-7) ────────────────────────


def test_oee_performance_exact_from_run_time_per_piece(db_session):
    """OEE-7: performance = ideal hours (Σ produced × run_time_per_piece) ÷ productive
    run, derived from the routing standard — NOT a hardcoded cycle. Availability and
    quality are pinned at 1.0 so performance is the only sub-1.0 leg and its value is
    asserted exactly."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    op = make_op(
        db_session, wo=make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS), wc=wc, run_time_per_piece=0.1
    )
    today = date.today()
    # All staffed time is productive RUN (availability 1.0); 40 good, 0 scrap (quality 1.0).
    make_entry(
        db_session,
        admin,
        op.work_order,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=12.0,
        when=today,
        quantity_produced=40,
        quantity_scrapped=0,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    oee = svc._get_oee_value(today, today, wc.id)
    assert oee is not None
    # ideal = 40 * 0.1 = 4 h; productive run = 12 h -> performance = 4/12 = 0.3333.
    # availability = 12/12 = 1.0; quality = 1.0. OEE = 0.3333 * 100 = 33.33%.
    assert abs(oee - (4.0 / 12.0) * 100.0) < 0.05  # ~33.33%


# ───────────────────── Quality: isolated RUN scrap (OEE-7) ───────────────────────


def test_oee_quality_exact_from_run_scrap(db_session):
    """OEE-7: quality = good ÷ (good + scrapped) from real scrap on a RUN clock-out, not
    an assumed all-good. Availability and performance are pinned at 1.0 so quality is the
    only sub-1.0 leg (complements the existing REWORK-scrap lock)."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    # AnalyticsService weights ideal hours by GOOD (quantity_produced) only:
    # ideal = 30 * 0.25 = 7.5 h. Size productive run to 7.5 h so performance = 1.0 and
    # quality is the only sub-1.0 leg.
    op = make_op(db_session, wo, wc, run_time_per_piece=0.25)
    today = date.today()
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=7.5,
        when=today,
        quantity_produced=30,
        quantity_scrapped=10,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    oee = svc._get_oee_value(today, today, wc.id)
    assert oee is not None
    # availability = 7.5/7.5 = 1.0. ideal = 30*0.25 = 7.5 h, productive run = 7.5 h ->
    # performance = min(7.5/7.5, 1) = 1.0. quality = 30/(30+10) = 0.75. OEE = 75.0.
    # If RUN scrap were dropped (assumed all-good), quality would be 1.0 / OEE 100.
    assert abs(oee - 75.0) < 0.01


# ─────────────── auto-OEE endpoint: A·P·Q decomposed with a perf loss ────────────


def test_auto_calculate_oee_performance_leg_from_routing(client, db_session):
    """OEE-1/OEE-7 on the revived endpoint: with availability and quality pinned at 1.0,
    the reported performance_pct equals the routing-derived ideal ÷ productive run, and
    oee_pct = availability·performance·quality decomposes to that performance value."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    today = date.today()
    # 10 h, ALL RUN (availability 1.0), 50 good + 0 scrap (quality 1.0). ideal = 50*0.1 = 5 h.
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=10.0,
        when=today,
        quantity_produced=50,
        quantity_scrapped=0,
    )
    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # availability = productive_run(10h) / staffed(10h) = 100%.
    assert abs(body["availability_pct"] - 100.0) < 0.5
    # quality = 50/50 = 100%.
    assert abs(body["quality_pct"] - 100.0) < 0.5
    # performance = ideal(5h) / productive_run(10h) = 50%.
    assert abs(body["performance_pct"] - 50.0) < 1.0
    # OEE = 1.0 * 0.5 * 1.0 = 50% (the perf loss flows straight through).
    assert abs(body["oee_pct"] - 50.0) < 1.0
    # ideal cycle DERIVED: total parts 50, ideal 5 h -> 360 s/pc, not the old 60 s.
    assert abs(body["ideal_cycle_time_seconds"] - 360.0) < 1.0


# ───────────────── OTD: exact ratio over a mixed set incl. stamped-late ──────────


def test_otd_exact_ratio_mixed_set(db_session):
    """OEE-6: over a known set of three COMPLETE WOs the OTD ratio is exactly 1/3:
      * on-time  (actual_end <= due_date)            -> counts on-time
      * stamped-late (actual_end > due_date)         -> counts late
      * NULL actual_end (no verifiable completion)   -> counts late
    No misleading 100, no dropped denominator member."""
    make_user(db_session)
    part = make_part(db_session)
    utc_today = datetime.utcnow().date()
    end_dt = datetime.combine(utc_today, datetime.min.time()) + timedelta(hours=10)

    # 1) On-time: finished today, due in 2 days.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        due_date=utc_today + timedelta(days=2),
        actual_end=end_dt,
    )
    # 2) Stamped-late: finished today, due 3 days ago (actual_end.date() > due_date).
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        due_date=utc_today - timedelta(days=3),
        actual_end=end_dt,
    )
    # 3) NULL actual_end, due 3 days ago -> updated_at(now) anchors it in-window, late.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        due_date=utc_today - timedelta(days=3),
        actual_end=None,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    otd = svc._get_otd_value(utc_today - timedelta(days=1), utc_today + timedelta(days=1))
    # 1 on-time of 3 in the denominator -> 33.33% exactly (not 100, not None).
    assert otd is not None
    assert abs(otd - (1.0 / 3.0) * 100.0) < 0.01


# ───────────────────── MS-5: reconcile-on-read clears reservation ────────────────


def test_reconcile_on_read_clears_schedule_reservation(client, db_session):
    """MS-5 on the RECONCILE path: an IN_PROGRESS op whose closed completion evidence
    (quantity_complete >= target with a closed TimeEntry) drives it to COMPLETE on a
    plain GET must ALSO have its schedule reservation freed — not just the live
    complete endpoint. Asserted via DB read after the read-triggered reconcile."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    # IN_PROGRESS op that still carries a schedule reservation; quantity already met.
    op = make_op(
        db_session,
        wo,
        wc,
        status_=OperationStatus.IN_PROGRESS,
        run_time_per_piece=0.1,
        scheduled=True,
    )
    op.quantity_complete = 5  # meets target (quantity_ordered=5)
    db_session.commit()
    # Closed completion evidence: a clocked-out RUN entry producing the full target qty.
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=4.0,
        when=date.today(),
        quantity_produced=5,
        quantity_scrapped=0,
    )
    assert op.scheduled_start is not None and op.scheduled_end is not None

    # A plain GET triggers reconcile-on-read, which drives the op to COMPLETE.
    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    assert op.status == OperationStatus.COMPLETE
    # Reconcile-driven completion freed the reservation too (MS-5, both paths).
    assert op.scheduled_start is None
    assert op.scheduled_end is None


# ─────────────── No regression: Optional value across analytics consumers ────────


def test_oee_details_serves_float_with_optional_value_schema(db_session):
    """No regression from KPIValue.value -> Optional[float]: get_oee_details coalesces an
    uncomputable window's None OEE to 0.0 for its float chart series (OEEComponents.oee /
    OEEDataPoint.oee stay float), so the schema change doesn't break that consumer."""
    make_user(db_session)
    wc = make_work_center(db_session)
    svc = AnalyticsService(db_session, COMPANY_A)
    today = date.today()
    details = svc.get_oee_details(today, today, wc.id)
    # Empty window -> _get_oee_value None coalesced to 0.0 float (not None) for the chart.
    assert isinstance(details.summary.oee, float)
    assert details.summary.oee == 0.0
    assert all(isinstance(dp.oee, float) for dp in details.time_series)
    assert all(isinstance(dp.oee, float) for dp in details.by_work_center)


def test_otd_sparkline_serves_floats_when_value_is_na(db_session):
    """No regression: the OTD sparkline is a List[float] glyph; even when the headline
    KPIValue.value is None ("n/a", empty denominator), the sparkline still yields floats
    (n/a weeks render 0.0) so the schema change doesn't leak None into the float list."""
    make_user(db_session)
    svc = AnalyticsService(db_session, COMPANY_A)
    today = date.today()
    sparkline = svc._get_otd_sparkline(today - timedelta(days=7), today)
    assert isinstance(sparkline, list)
    assert all(isinstance(v, float) for v in sparkline)


# ══════════════════ Batch-8 follow-up fixes (should-fix close-out) ════════════════
# Two review items closed here:
#   * Metric consistency: AnalyticsService._get_ideal_production_hours weighted ideal
#     hours by quantity_produced ONLY, while the auto-calc endpoint weighted by
#     quantity_produced + quantity_scrapped. With scrap present the same data yielded two
#     different OEE numbers (the /analytics/kpis headline vs. the persisted OEERecord).
#     _get_ideal_production_hours now weights by produced + scrapped to match the endpoint
#     and the standard OEE Performance convention (every piece run — incl. scrap — consumes
#     a standard cycle; scrap is discounted only in the Quality leg).
#   * RBAC: the OEE router gated only with get_current_user, so any authenticated user
#     (incl. OPERATOR/VIEWER) could create/overwrite OEE records and targets and re-run the
#     writable POST /calculate. The WRITE endpoints now carry the same role gate as the
#     sibling Analytics router (ADMIN/MANAGER/SUPERVISOR); the READ endpoints stay open.


# ─────────── Metric consistency: both OEE paths agree on a scrap dataset ──────────


def test_oee_paths_agree_with_scrap(client, db_session):
    """Fix 1: with scrap present, AnalyticsService._get_oee_value (the /analytics/kpis
    headline) and the persisted OEERecord.oee_pct from POST /oee/calculate must agree for
    identical data, because both now weight ideal hours by produced + scrapped.

    Dataset (one WC, one day, no downtime events) chosen so every OEE leg lands on a clean
    value: 10 h all-RUN (availability = productive_run/staffed = 1.0); run_time_per_piece
    0.1 h/pc; 40 good + 10 scrap -> 50 cycled. ideal = (40+10)*0.1 = 5 h -> performance =
    5/10 = 0.5; quality = 40/50 = 0.8. OEE = 1.0 * 0.5 * 0.8 = 40.0%.

    Regression guard: under the OLD analytics weighting (produced ONLY) ideal would be
    40*0.1 = 4 h -> performance 0.4 -> analytics OEE 32.0, diverging from the endpoint's
    40.0. The equality assertion below fails on that pre-fix behavior.
    """
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    today = date.today()
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=10.0,
        when=today,
        quantity_produced=40,
        quantity_scrapped=10,
    )

    # Path A: AnalyticsService (the /analytics/kpis OEE headline).
    svc = AnalyticsService(db_session, COMPANY_A)
    analytics_oee = svc._get_oee_value(today, today, wc.id)
    assert analytics_oee is not None
    # Clean dataset -> 40.0% exactly (and NOT the pre-fix 32.0).
    assert abs(analytics_oee - 40.0) < 0.01

    # Path B: persisted OEERecord written by the auto-calc endpoint.
    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    record_oee = resp.json()["oee_pct"]

    # The two paths now agree for identical data with scrap present (Fix 1). Tolerance
    # covers the endpoint's per-leg 2-decimal rounding in calculate_oee.
    assert abs(analytics_oee - record_oee) < 0.5


def test_ideal_production_hours_weights_produced_plus_scrapped(db_session):
    """Fix 1 (unit): _get_ideal_production_hours weights ideal hours by produced +
    scrapped, not produced alone. 40 good + 10 scrap at 0.1 h/pc -> (40+10)*0.1 = 5.0 h.
    Under the pre-fix produced-only weighting this would be 4.0 h."""
    admin = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    today = date.today()
    make_entry(
        db_session,
        admin,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=10.0,
        when=today,
        quantity_produced=40,
        quantity_scrapped=10,
    )
    svc = AnalyticsService(db_session, COMPANY_A)
    ideal = svc._get_ideal_production_hours(today, today, wc.id)
    assert abs(ideal - 5.0) < 1e-6  # would be 4.0 under produced-only weighting


# ───────────────────── RBAC: OEE write endpoints gated, reads open ───────────────


def test_oee_calculate_forbidden_for_operator(client, db_session):
    """Fix 2: a non-privileged role (OPERATOR) is rejected with 403 on the writable
    POST /oee/calculate (it persists/overwrites an OEERecord), and no record is written."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    # Gate fired before any write: nothing persisted for this WC.
    assert (
        db_session.query(OEERecord).filter(OEERecord.work_center_id == wc.id, OEERecord.company_id == COMPANY_A).count()
        == 0
    )


def test_oee_create_record_forbidden_for_operator(client, db_session):
    """Fix 2: OPERATOR cannot create an OEE record directly (POST /oee/records) -> 403."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    payload = {"work_center_id": wc.id, "record_date": date.today().isoformat()}
    resp = client.post("/api/v1/oee/records", json=payload, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_oee_create_target_forbidden_for_operator(client, db_session):
    """Fix 2: OPERATOR cannot create/overwrite an OEE target (POST /oee/targets) -> 403."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    payload = {"work_center_id": wc.id, "target_oee_pct": 80.0}
    resp = client.post("/api/v1/oee/targets", json=payload, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_oee_calculate_allowed_for_supervisor(client, db_session):
    """Fix 2: an authorized role (SUPERVISOR) still succeeds on POST /oee/calculate (the
    gate admits ADMIN/MANAGER/SUPERVISOR), so the write path is restricted, not broken."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, wc, run_time_per_piece=0.1)
    make_entry(
        db_session,
        supervisor,
        wo,
        op,
        entry_type=TimeEntryType.RUN,
        duration_hours=6.0,
        when=date.today(),
        quantity_produced=30,
        quantity_scrapped=0,
    )
    resp = client.post(f"/api/v1/oee/calculate/{wc.id}", headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_oee_read_endpoints_open_to_operator(client, db_session):
    """Fix 2 (no over-restriction): READ endpoints stay on get_current_user so the shop
    floor can still VIEW OEE dashboards. An OPERATOR gets 200 on the dashboard, trends,
    records list, and targets list."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    make_work_center(db_session)
    h = headers_for(operator)
    for path in (
        "/api/v1/oee/dashboard?period=7d",
        "/api/v1/oee/trends?period=7d",
        "/api/v1/oee/records",
        "/api/v1/oee/targets",
    ):
        resp = client.get(path, headers=h)
        assert resp.status_code == status.HTTP_200_OK, f"{path} -> {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Dashboard/trends date-range scoping: the From/To range must scope the plant
# strip / tiles / comparison / trend chart, not just the records table (the
# range was previously silently dropped by these two endpoints). An explicit
# date_from/date_to takes precedence over the period preset.
# ---------------------------------------------------------------------------


def make_oee_record(db: Session, wc: WorkCenter, record_date: date, oee_pct: float, company_id: int = COMPANY_A):
    """Insert a minimal OEERecord on a specific date with a given OEE (a/p/q mirror it)."""
    rec = OEERecord(
        company_id=company_id,
        work_center_id=wc.id,
        record_date=record_date,
        oee_pct=oee_pct,
        availability_pct=oee_pct,
        performance_pct=oee_pct,
        quality_pct=oee_pct,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_dashboard_honors_date_from_date_to_window(client, db_session):
    """The dashboard's current-OEE-per-WC follows the From/To range: the latest record WITHIN
    the window is used, and a window with no records yields null (renders '--'), not a stale value."""
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    make_oee_record(db_session, wc, date(2021, 1, 10), 40.0)
    make_oee_record(db_session, wc, date(2021, 2, 20), 80.0)
    h = headers_for(admin)

    # Window covering only the January record.
    resp = client.get("/api/v1/oee/dashboard?date_from=2021-01-01&date_to=2021-01-31", headers=h)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    wcs = {w["work_center_id"]: w for w in resp.json()["work_centers"]}
    assert wcs[wc.id]["current_oee_pct"] == 40.0
    assert wcs[wc.id]["record_date"] == "2021-01-10"

    # Window covering only the February record -> latest-in-window is the Feb value.
    resp = client.get("/api/v1/oee/dashboard?date_from=2021-02-01&date_to=2021-02-28", headers=h)
    wcs = {w["work_center_id"]: w for w in resp.json()["work_centers"]}
    assert wcs[wc.id]["current_oee_pct"] == 80.0
    assert wcs[wc.id]["record_date"] == "2021-02-20"

    # Window before both records -> no data in window -> null, not a fabricated/stale value.
    resp = client.get("/api/v1/oee/dashboard?date_from=2020-01-01&date_to=2020-12-31", headers=h)
    wcs = {w["work_center_id"]: w for w in resp.json()["work_centers"]}
    assert wcs[wc.id]["current_oee_pct"] is None
    assert wcs[wc.id]["record_date"] is None


def test_trends_honors_date_from_date_to_window(client, db_session):
    """The trend series is bounded by the From/To range (previously it always used the period preset)."""
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    make_oee_record(db_session, wc, date(2021, 1, 10), 40.0)
    make_oee_record(db_session, wc, date(2021, 2, 20), 80.0)
    make_oee_record(db_session, wc, date(2021, 3, 30), 60.0)
    h = headers_for(admin)

    resp = client.get(
        f"/api/v1/oee/trends?work_center_id={wc.id}&date_from=2021-02-01&date_to=2021-02-28",
        headers=h,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    dates = [p["date"] for p in resp.json()["time_series"]]
    assert dates == ["2021-02-20"]  # only the in-window record, not the Jan/Mar ones


def test_dashboard_period_fallback_when_no_date_range(client, db_session):
    """Backward compatible: with no date_from/date_to the period preset still governs the window."""
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    make_oee_record(db_session, wc, date.today(), 75.0)
    h = headers_for(admin)

    resp = client.get("/api/v1/oee/dashboard?period=7d", headers=h)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    wcs = {w["work_center_id"]: w for w in resp.json()["work_centers"]}
    assert wcs[wc.id]["current_oee_pct"] == 75.0
