"""Metric-exactness locks for ``services/quality_yield_service`` (Lean Phase 1, issue #88).

FPY per op = (complete - reworked - scrapped) / (complete + scrapped), clamped
at 0; groups are QUANTITY-WEIGHTED (sums of units, never means of ratios); RTY
per WO = product of its ops' FPYs, omitted under a work-center filter (route-
level metric).

Scrap Pareto reconciles the three scrap ledgers with NO double count:
  tier 1  TimeEntry.quantity_scrapped (window by clock_in; backfill/import
          provenance EXCLUDED from buckets, tallied separately),
  tier 2  op.quantity_scrapped minus that op's lifetime entry scrap (>= 0),
  tier 3  wo.quantity_scrapped minus its lifetime op scrap (>= 0) -- skipped
          under a work_center filter (WO scrap has no WC attribution).
Uncoded scrap lands in the 'unspecified' bucket; cost = qty x part.standard_cost.
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus, WorkOrderType
from app.services.quality_yield_service import get_fpy_rty, get_scrap_pareto, get_scrap_rate
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_entry,
    make_op,
    make_part,
    make_scrap_code,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = date(2026, 6, 1)
WINDOW_END = date(2026, 6, 10)
IN_WINDOW = datetime(2026, 6, 4, 12, 0)


def test_fpy_exact_per_op_quantity_weighted_groups_and_clamp(db_session: Session):
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo1 = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    # Op A: 10 complete, 2 reworked, 1 scrapped -> fp 7 / attempted 11 = 63.6%.
    make_op(
        db_session,
        wo1,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=10,
        quantity_reworked=2,
        quantity_scrapped=1,
        actual_end=IN_WINDOW,
    )
    wo2 = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    # Op B: 20 clean -> fp 20 / attempted 20.
    make_op(
        db_session,
        wo2,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=20,
        actual_end=IN_WINDOW,
    )
    # Op C: rework+scrap exceed complete -> first-pass units clamp at 0, not negative.
    wo3 = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    make_op(
        db_session,
        wo3,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=2,
        quantity_reworked=2,
        quantity_scrapped=2,
        actual_end=IN_WINDOW,
    )
    # Out-of-window and unfinished ops are not yield samples.
    make_op(
        db_session,
        wo3,
        wc,
        sequence=20,
        status_=OperationStatus.COMPLETE,
        quantity_complete=100,
        actual_end=datetime(2026, 5, 1, 0, 0),
    )
    make_op(db_session, wo3, wc, sequence=30, status_=OperationStatus.IN_PROGRESS, quantity_complete=50)

    result = get_fpy_rty(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    # Overall: (7 + 20 + 0) / (11 + 20 + 4) = 27/35 = 77.1% — unit-weighted,
    # NOT the mean of (63.6, 100.0, 0.0) = 54.5.
    assert result.overall_fpy_pct == pytest.approx(77.1)

    part_row = next(row for row in result.by_part if row.key == part.part_number)
    assert part_row.operations == 3
    assert part_row.units_attempted == pytest.approx(35.0)
    assert part_row.first_pass_units == pytest.approx(27.0)
    assert part_row.fpy_pct == pytest.approx(77.1)
    assert part_row.work_orders == 3

    wc_row = next(row for row in result.by_work_center if row.key == wc.code)
    assert wc_row.fpy_pct == pytest.approx(77.1)
    assert wc_row.rty_pct is None  # RTY is route-level, never per-WC


def test_rty_is_product_of_op_fpys_and_omitted_under_wc_filter(db_session: Session):
    part = make_part(db_session)
    wc1 = make_work_center(db_session)
    wc2 = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    # Op1: fp 6 / 10 = 0.6.  Op2: fp 5 / 10 = 0.5.  RTY = 0.6 x 0.5 = 30.0%.
    make_op(
        db_session,
        wo,
        wc1,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=8,
        quantity_scrapped=2,
        actual_end=IN_WINDOW,
    )
    make_op(
        db_session,
        wo,
        wc2,
        sequence=20,
        status_=OperationStatus.COMPLETE,
        quantity_complete=10,
        quantity_reworked=5,
        actual_end=IN_WINDOW,
    )

    result = get_fpy_rty(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.overall_rty_pct == pytest.approx(30.0)
    part_row = next(row for row in result.by_part if row.key == part.part_number)
    assert part_row.rty_pct == pytest.approx(30.0)

    # Narrowed to one WC the route is incomplete -> RTY must be omitted, and the
    # FPY population shrinks to that WC's ops only.
    filtered = get_fpy_rty(db_session, COMPANY_A, WINDOW_START, WINDOW_END, work_center_id=wc1.id)
    assert filtered.overall_rty_pct is None
    assert all(row.rty_pct is None for row in filtered.by_part)
    assert filtered.overall_fpy_pct == pytest.approx(60.0)


def test_fpy_counts_partless_laser_ops_in_overall_and_wc_but_not_by_part(db_session: Session):
    """Standalone laser-cutting WOs carry part_id NULL: their completed sheet-run
    ops must still count in overall FPY and the per-work-center buckets (the Part
    join is an OUTER join), while producing NO per-part row (nothing to attribute
    the yield to)."""
    part = make_part(db_session)
    wc = make_work_center(db_session, work_center_type="laser")

    part_wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    # Parted op: 10 clean -> fp 10 / attempted 10.
    make_op(
        db_session,
        part_wo,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=10,
        actual_end=IN_WINDOW,
    )

    laser_wo = WorkOrder(
        work_order_number="LEAN1-LASER-00001",
        part_id=None,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        quantity_ordered=4,
        status=WorkOrderStatus.COMPLETE,
        priority=5,
        company_id=COMPANY_A,
    )
    db_session.add(laser_wo)
    db_session.commit()
    # Part-less sheet-run op: 3 complete, 1 scrapped -> fp 2 / attempted 4.
    make_op(
        db_session,
        laser_wo,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=3,
        quantity_scrapped=1,
        actual_end=IN_WINDOW,
    )

    result = get_fpy_rty(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    # Overall counts BOTH WOs: (10 + 2) / (10 + 4) = 85.7%.
    assert result.overall_fpy_pct == pytest.approx(85.7)
    # Overall RTY averages BOTH WOs: (1.0 + 0.5) / 2 = 75.0%.
    assert result.overall_rty_pct == pytest.approx(75.0)

    # The part-less WO contributes NO per-part row...
    assert [row.key for row in result.by_part] == [part.part_number]
    part_row = result.by_part[0]
    assert part_row.units_attempted == pytest.approx(10.0)
    assert part_row.work_orders == 1

    # ...but its sheet-run ops DO count in the work-center bucket.
    wc_row = next(row for row in result.by_work_center if row.key == wc.code)
    assert wc_row.operations == 2
    assert wc_row.units_attempted == pytest.approx(14.0)
    assert wc_row.first_pass_units == pytest.approx(12.0)
    assert wc_row.work_orders == 2


def test_fpy_tenant_scoped_and_none_on_empty(db_session: Session):
    part_b = make_part(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B, status_=WorkOrderStatus.COMPLETE)
    make_op(
        db_session,
        wo_b,
        wc_b,
        company_id=COMPANY_B,
        status_=OperationStatus.COMPLETE,
        quantity_complete=10,
        actual_end=IN_WINDOW,
    )

    result = get_fpy_rty(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.overall_fpy_pct is None  # empty denominator -> n/a, never 0/100
    assert result.by_part == []
    assert result.by_work_center == []


def test_scrap_rate_exact_and_none_on_empty(db_session: Session):
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE)
    make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_complete=18,
        quantity_scrapped=2,
        actual_end=IN_WINDOW,
    )
    assert get_scrap_rate(db_session, COMPANY_A, WINDOW_START, WINDOW_END) == pytest.approx(10.0)
    # A window with no completed ops has no honest scrap rate.
    assert get_scrap_rate(db_session, COMPANY_A, date(2025, 1, 1), date(2025, 1, 31)) is None


def test_scrap_pareto_three_tiers_reconcile_without_double_count(db_session: Session):
    """entries -> op delta -> WO delta, each unit counted exactly once."""
    user = make_user(db_session)
    part = make_part(db_session, standard_cost=10.0)
    wc = make_work_center(db_session)
    code_a = make_scrap_code(db_session, code="A-DIM")
    code_b = make_scrap_code(db_session, code="B-TOOL")
    code_c = make_scrap_code(db_session, code="C-MAT")

    wo = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        quantity_scrapped=9,
        scrap_reason_code_id=code_c.id,
        actual_end=IN_WINDOW,
    )
    # Op ledger says 5 scrapped (code B), of which 3 came in via a time entry (code A):
    # tier 1 books 3 under A, tier 2 books the un-entry-backed delta 2 under B.
    op = make_op(
        db_session,
        wo,
        wc,
        status_=OperationStatus.COMPLETE,
        quantity_complete=20,
        quantity_scrapped=5,
        scrap_reason_code_id=code_b.id,
        actual_end=IN_WINDOW,
    )
    make_entry(
        db_session,
        user,
        wo,
        op,
        wc,
        clock_in=IN_WINDOW,
        quantity_produced=17,
        quantity_scrapped=3,
        scrap_reason_code_id=code_a.id,
        source="kiosk",
    )
    # WO ledger says 9, of which 5 are op-backed: tier 3 books the delta 4 under C.

    result = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    assert result.total_quantity == pytest.approx(9.0)  # 3 + 2 + 4, each unit once
    assert result.total_cost == pytest.approx(90.0)  # x standard_cost 10

    assert [(b.code, b.quantity, b.percentage, b.cumulative_pct) for b in result.buckets] == [
        ("C-MAT", 4.0, 44.4, 44.4),
        ("A-DIM", 3.0, 33.3, 77.8),
        ("B-TOOL", 2.0, 22.2, 100.0),
    ]
    by_code = {b.code: b for b in result.buckets}
    assert by_code["C-MAT"].cost == pytest.approx(40.0)
    assert by_code["A-DIM"].cost == pytest.approx(30.0)
    assert by_code["B-TOOL"].cost == pytest.approx(20.0)
    assert result.excluded_backfill_import_quantity == pytest.approx(0.0)


def test_scrap_pareto_uncoded_lands_in_unspecified(db_session: Session):
    user = make_user(db_session)
    part = make_part(db_session, standard_cost=5.0)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    make_entry(
        db_session,
        user,
        wo,
        op,
        wc,
        clock_in=IN_WINDOW,
        quantity_scrapped=2,
        scrap_reason="operator note only",
    )

    result = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert len(result.buckets) == 1
    bucket = result.buckets[0]
    assert bucket.scrap_reason_code_id is None
    assert bucket.code == "unspecified"
    assert bucket.quantity == pytest.approx(2.0)
    assert bucket.cost == pytest.approx(10.0)
    assert bucket.percentage == pytest.approx(100.0)


def test_scrap_pareto_work_center_filter_drops_wo_tier(db_session: Session):
    """WO-level scrap has no work-center attribution -> tier 3 skipped when filtering."""
    user = make_user(db_session)
    part = make_part(db_session)
    wc1 = make_work_center(db_session)
    wc2 = make_work_center(db_session)
    code = make_scrap_code(db_session, code="WCF")

    wo = make_wo(db_session, part, quantity_scrapped=10, actual_end=IN_WINDOW)  # tier-3 delta would be 7
    op1 = make_op(
        db_session,
        wo,
        wc1,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        quantity_scrapped=2,
        scrap_reason_code_id=code.id,
        actual_end=IN_WINDOW,
    )
    make_entry(db_session, user, wo, op1, wc1, clock_in=IN_WINDOW, quantity_scrapped=1, scrap_reason_code_id=code.id)
    # An op at the OTHER work center must drop out under the wc1 filter too.
    make_op(
        db_session,
        wo,
        wc2,
        sequence=20,
        status_=OperationStatus.COMPLETE,
        quantity_scrapped=1,
        scrap_reason_code_id=code.id,
        actual_end=IN_WINDOW,
    )

    unfiltered = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    # 1 (entry) + 1 (op1 delta 2-1) + 1 (op2 delta) + 7 (WO delta 10-3) = 10.
    assert unfiltered.total_quantity == pytest.approx(10.0)

    filtered = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END, work_center_id=wc1.id)
    # wc1 only: entry 1 + op1 delta 1. No WO tier, no wc2 op.
    assert filtered.total_quantity == pytest.approx(2.0)


def test_scrap_pareto_provenance_excludes_backfill_entries(db_session: Session):
    """Backfill/import-sourced entry scrap is excluded from the buckets and
    reported separately -- and the op tier still nets it out (no reappearance)."""
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    code = make_scrap_code(db_session, code="BF")

    wo = make_wo(db_session, part)
    # Op ledger fully backed by a backfill entry: tier 2 delta = 3 - 3 = 0.
    op = make_op(
        db_session,
        wo,
        wc,
        status_=OperationStatus.COMPLETE,
        quantity_scrapped=3,
        scrap_reason_code_id=code.id,
        actual_end=IN_WINDOW,
    )
    make_entry(
        db_session,
        user,
        wo,
        op,
        wc,
        clock_in=IN_WINDOW,
        quantity_scrapped=3,
        scrap_reason_code_id=code.id,
        source="import",
    )

    result = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.buckets == []
    assert result.total_quantity == pytest.approx(0.0)
    assert result.excluded_backfill_import_quantity == pytest.approx(3.0)


def test_scrap_pareto_part_filter_scopes_excluded_backfill_tally(db_session: Session):
    """A part-filtered Pareto's excluded figure counts ONLY that part's
    backfill/import scrap (regression: the excluded tally applied the
    work_center filter but not part_id, so a part-filtered report leaked other
    parts' backfill scrap into its excluded figure). The part is resolved via
    the entry's WorkOrder, the same way the tier-1 bucket query resolves it."""
    user = make_user(db_session)
    wc = make_work_center(db_session)
    part_a = make_part(db_session)
    part_b = make_part(db_session)

    # Backfill scrap on part A (2) and part B (7), same window, same WC.
    wo_a = make_wo(db_session, part_a)
    op_a = make_op(db_session, wo_a, wc, status_=OperationStatus.IN_PROGRESS)
    make_entry(db_session, user, wo_a, op_a, wc, clock_in=IN_WINDOW, quantity_scrapped=2, source="backfill")

    wo_b = make_wo(db_session, part_b)
    op_b = make_op(db_session, wo_b, wc, status_=OperationStatus.IN_PROGRESS)
    make_entry(db_session, user, wo_b, op_b, wc, clock_in=IN_WINDOW, quantity_scrapped=7, source="import")

    # Filtered on part A: excluded counts ONLY part A's backfill scrap.
    filtered = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END, part_id=part_a.id)
    assert filtered.buckets == []  # backfill/import never lands in buckets
    assert filtered.excluded_backfill_import_quantity == pytest.approx(2.0)

    # The work_center filter keeps working alongside it (both entries at wc).
    both_filters = get_scrap_pareto(
        db_session, COMPANY_A, WINDOW_START, WINDOW_END, work_center_id=wc.id, part_id=part_b.id
    )
    assert both_filters.excluded_backfill_import_quantity == pytest.approx(7.0)

    # Unfiltered, both parts' backfill scrap lands in the excluded tally.
    unfiltered = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert unfiltered.excluded_backfill_import_quantity == pytest.approx(9.0)


def test_scrap_pareto_tier1_excludes_soft_deleted_wos(db_session: Session):
    """Scrap booked on a soft-deleted WO's entries is off the Pareto -- both the
    buckets and the backfill excluded tally (tiers 2/3 already filter
    is_deleted); entries with NO work order stay counted via the outer join
    (pins the WO-liveness or_ filters added after review)."""
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    code = make_scrap_code(db_session, code="SDEL")

    live_wo = make_wo(db_session, part)
    live_op = make_op(db_session, live_wo, wc, status_=OperationStatus.IN_PROGRESS)
    make_entry(
        db_session, user, live_wo, live_op, wc, clock_in=IN_WINDOW, quantity_scrapped=2, scrap_reason_code_id=code.id
    )

    # WO-less scrap entry stays counted (outer-join semantics).
    make_entry(db_session, user, None, None, wc, clock_in=IN_WINDOW, quantity_scrapped=1, scrap_reason_code_id=code.id)

    deleted_wo = make_wo(db_session, part)
    deleted_op = make_op(db_session, deleted_wo, wc, status_=OperationStatus.IN_PROGRESS)
    make_entry(
        db_session,
        user,
        deleted_wo,
        deleted_op,
        wc,
        clock_in=IN_WINDOW,
        quantity_scrapped=50,
        scrap_reason_code_id=code.id,
    )
    make_entry(db_session, user, deleted_wo, deleted_op, wc, clock_in=IN_WINDOW, quantity_scrapped=9, source="backfill")
    deleted_wo.soft_delete(user.id)
    db_session.commit()

    result = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.total_quantity == pytest.approx(3.0)  # 2 live + 1 WO-less; the 50 is out
    assert [(bucket.code, bucket.quantity) for bucket in result.buckets] == [("SDEL", 3.0)]
    assert result.excluded_backfill_import_quantity == pytest.approx(0.0)  # deleted WO's backfill out too


def test_scrap_pareto_tenant_scoped(db_session: Session):
    user_b = make_user(db_session, company_id=COMPANY_B)
    part_b = make_part(db_session, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, company_id=COMPANY_B)
    op_b = make_op(db_session, wo_b, wc_b, company_id=COMPANY_B, status_=OperationStatus.IN_PROGRESS)
    make_entry(db_session, user_b, wo_b, op_b, wc_b, company_id=COMPANY_B, clock_in=IN_WINDOW, quantity_scrapped=5)

    result = get_scrap_pareto(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.buckets == []
    assert result.total_quantity == pytest.approx(0.0)
