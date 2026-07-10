"""Metric-exactness locks for ``services/adoption_metrics_service`` (Lean Phase 1, 1f).

Adoption:
  * digital-completion % from operation_completed EVENTS (payload source in
    kiosk/desktop/scanner = live; backfill/import = backfill; else unknown),
  * clock-in coverage: completed ops with >= 1 labor TimeEntry that is NOT
    backfill/import-sourced (provenance rule),
  * backfill rate over the window's closed entries,
  * per-ISO-week breakdown.

Hidden factory:
  * rework hours/quantity share, provenance-filtered, excluded hours reported,
  * planned vs reactive maintenance mix,
  * MTBF (staffed RUN+SETUP hours / unplanned events) and MTTR per work center.
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.models.maintenance import MaintenanceType, MaintenanceWorkOrder
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntryType
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.adoption_metrics_service import get_adoption_metrics, get_hidden_factory_metrics
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_downtime,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = date(2026, 6, 1)  # a Monday
WINDOW_END = date(2026, 6, 14)
IN_WEEK_1 = datetime(2026, 6, 3, 10, 0)
IN_WEEK_2 = datetime(2026, 6, 10, 10, 0)


def _completion_event(db: Session, *, occurred_at: datetime, source, company_id: int = COMPANY_A):
    payload = {"source": source} if source is not None else {}
    event = OperationalEvent(
        company_id=company_id,
        event_type="operation_completed",
        source_module="shop_floor",
        severity="info",
        event_payload=payload,
        occurred_at=occurred_at,
    )
    db.add(event)
    db.commit()
    return event


def test_digital_completion_pct_and_weekly_split_from_events(db_session: Session):
    # Week 1: kiosk + desktop (live) + backfill. Week 2: scanner (live) + unreported.
    _completion_event(db_session, occurred_at=IN_WEEK_1, source="kiosk")
    _completion_event(db_session, occurred_at=IN_WEEK_1, source="desktop")
    _completion_event(db_session, occurred_at=IN_WEEK_1, source="backfill")
    _completion_event(db_session, occurred_at=IN_WEEK_2, source="scanner")
    _completion_event(db_session, occurred_at=IN_WEEK_2, source=None)
    # Cross-tenant event never counts.
    _completion_event(db_session, occurred_at=IN_WEEK_1, source="kiosk", company_id=COMPANY_B)

    result = get_adoption_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    assert result.live_completions == 3
    assert result.backfill_completions == 1
    assert result.unknown_completions == 1
    assert result.digital_completion_pct == pytest.approx(60.0)  # 3/5

    weeks = {week.week_start: week for week in result.weekly}
    assert weeks[date(2026, 6, 1)].operation_completions == 3
    assert weeks[date(2026, 6, 1)].live_completions == 2
    assert weeks[date(2026, 6, 1)].backfill_completions == 1
    assert weeks[date(2026, 6, 1)].digital_completion_pct == pytest.approx(66.7)
    assert weeks[date(2026, 6, 8)].operation_completions == 2
    assert weeks[date(2026, 6, 8)].live_completions == 1
    assert weeks[date(2026, 6, 8)].unknown_completions == 1


def test_clock_in_coverage_requires_a_live_labor_entry(db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)

    covered = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, actual_end=IN_WEEK_1)
    make_entry(db_session, user, wo, covered, wc, clock_in=IN_WEEK_1, duration_hours=2, source="kiosk")

    # Backfill-only labor does NOT count as digital coverage (provenance rule).
    uncovered = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.COMPLETE, actual_end=IN_WEEK_2)
    make_entry(db_session, user, wo, uncovered, wc, clock_in=IN_WEEK_2, duration_hours=2, source="backfill")

    result = get_adoption_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.clock_in_coverage_pct == pytest.approx(50.0)

    weeks = {week.week_start: week for week in result.weekly}
    assert weeks[date(2026, 6, 1)].clock_in_coverage_pct == pytest.approx(100.0)
    assert weeks[date(2026, 6, 8)].clock_in_coverage_pct == pytest.approx(0.0)


def test_backfill_rate_over_closed_entries(db_session: Session):
    user = make_user(db_session)
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_1, duration_hours=1, source="kiosk")
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_1, duration_hours=1)  # NULL source: in baseline
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_2, duration_hours=1, source="import")
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_2, duration_hours=1, source="backfill")
    # Open entries are not "booked" yet -> not in the denominator.
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_2, open_entry=True, source="backfill")

    result = get_adoption_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.backfill_rate_pct == pytest.approx(50.0)  # 2 of 4 closed


def test_soft_deleted_wo_excluded_from_coverage_and_backfill_rate(db_session: Session):
    """A soft-deleted WO's completed ops leave the clock-in-coverage denominator
    and its entries leave the backfill rate, while WO-less entries (nullable FK)
    stay counted (pins the is_deleted join/filters added after review)."""
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    live_wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    covered = make_op(db_session, live_wo, wc, sequence=10, status_=OperationStatus.COMPLETE, actual_end=IN_WEEK_1)
    make_entry(db_session, user, live_wo, covered, wc, clock_in=IN_WEEK_1, duration_hours=1, source="kiosk")

    deleted_wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    ghost = make_op(db_session, deleted_wo, wc, sequence=10, status_=OperationStatus.COMPLETE, actual_end=IN_WEEK_1)
    # Would drag coverage to 50% and skew the backfill rate if it leaked in.
    make_entry(db_session, user, deleted_wo, ghost, wc, clock_in=IN_WEEK_1, duration_hours=1, source="backfill")
    deleted_wo.soft_delete(user.id)
    db_session.commit()

    # A WO-less entry must STAY in the backfill-rate denominator (outer join).
    make_entry(db_session, user, None, None, None, clock_in=IN_WEEK_1, duration_hours=1, source="import")

    result = get_adoption_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.clock_in_coverage_pct == pytest.approx(100.0)  # ghost op out of the denominator
    # Counted entries: live kiosk + WO-less import -> 1 of 2 is backfill/import.
    assert result.backfill_rate_pct == pytest.approx(50.0)


def test_hidden_factory_rework_share_and_provenance(db_session: Session):
    user = make_user(db_session)
    # Live labor: RUN 8h / 20 produced, REWORK 2h / 5 produced -> rework 20% of
    # hours (2/10) and 20% of production-bearing quantity (5/25).
    make_entry(
        db_session,
        user,
        None,
        None,
        None,
        clock_in=IN_WEEK_1,
        duration_hours=8,
        quantity_produced=20,
        source="kiosk",
    )
    make_entry(
        db_session,
        user,
        None,
        None,
        None,
        entry_type=TimeEntryType.REWORK,
        clock_in=IN_WEEK_1,
        duration_hours=2,
        quantity_produced=5,
    )
    # Backfilled REWORK labor: excluded from the baseline, reported separately.
    make_entry(
        db_session,
        user,
        None,
        None,
        None,
        entry_type=TimeEntryType.REWORK,
        clock_in=IN_WEEK_2,
        duration_hours=5,
        quantity_produced=9,
        source="backfill",
    )
    # BREAK is not labor -> never in the denominators.
    make_entry(db_session, user, None, None, None, entry_type=TimeEntryType.BREAK, clock_in=IN_WEEK_1, duration_hours=3)

    metrics = get_hidden_factory_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    assert metrics.rework_hours == pytest.approx(2.0)
    assert metrics.total_labor_hours == pytest.approx(10.0)
    assert metrics.rework_hours_pct == pytest.approx(20.0)
    assert metrics.rework_quantity == pytest.approx(5.0)
    assert metrics.total_quantity == pytest.approx(25.0)
    assert metrics.rework_quantity_pct == pytest.approx(20.0)
    assert metrics.excluded_backfill_import_hours == pytest.approx(5.0)


def test_maintenance_mix_planned_vs_reactive(db_session: Session):
    wc = make_work_center(db_session)
    kinds = [
        MaintenanceType.PREVENTIVE,
        MaintenanceType.PREVENTIVE,
        MaintenanceType.PREDICTIVE,
        MaintenanceType.CORRECTIVE,
    ]
    for index, kind in enumerate(kinds):
        mwo = MaintenanceWorkOrder(
            work_center_id=wc.id,
            wo_number=f"LEAN1-MWO-{index}",
            maintenance_type=kind,
            title=f"Maint {index}",
            completed_at=IN_WEEK_1,
            company_id=COMPANY_A,
        )
        db_session.add(mwo)
    db_session.commit()

    metrics = get_hidden_factory_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert metrics.maintenance.planned_count == 3
    assert metrics.maintenance.reactive_count == 1
    assert metrics.maintenance.planned_pct == pytest.approx(75.0)


def test_mtbf_mttr_per_work_center_from_unplanned_downtime(db_session: Session):
    user = make_user(db_session)
    wc = make_work_center(db_session)
    # Staffed run: RUN 20h + SETUP 10h = 30h. INSPECTION is not "run" time.
    make_entry(db_session, user, None, None, wc, clock_in=IN_WEEK_1, duration_hours=20)
    make_entry(db_session, user, None, None, wc, entry_type=TimeEntryType.SETUP, clock_in=IN_WEEK_1, duration_hours=10)
    make_entry(
        db_session, user, None, None, wc, entry_type=TimeEntryType.INSPECTION, clock_in=IN_WEEK_1, duration_hours=4
    )
    # Two unplanned events (60 + 30 min); planned downtime never counts.
    make_downtime(db_session, user, wc, start_time=IN_WEEK_1, duration_minutes=60)
    make_downtime(db_session, user, wc, start_time=IN_WEEK_2, duration_minutes=30)
    make_downtime(db_session, user, wc, start_time=IN_WEEK_2, duration_minutes=500, planned=True)

    metrics = get_hidden_factory_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert len(metrics.reliability_by_work_center) == 1
    row = metrics.reliability_by_work_center[0]
    assert row.work_center_id == wc.id
    assert row.unplanned_downtime_events == 2
    assert row.unplanned_downtime_hours == pytest.approx(1.5)
    assert row.staffed_run_hours == pytest.approx(30.0)
    assert row.mtbf_hours == pytest.approx(15.0)  # 30h / 2 failures
    assert row.mttr_hours == pytest.approx(0.75)  # (60 + 30) / 2 = 45 min
