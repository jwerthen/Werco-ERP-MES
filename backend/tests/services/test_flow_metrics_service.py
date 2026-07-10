"""Metric-exactness locks for ``services/flow_metrics_service`` (Lean Phase 1, issue #88).

Fixtures with hand-computable timestamps/quantities pin the arithmetic:
  * lead time = released_at -> actual_end (days, 2dp) + release -> first/last ship,
  * PCE = value-add RUN hours / (lead days x 24), backfill/import labor EXCLUDED
    from the baseline and reported in ``excluded_backfill_import_hours``,
  * queue time per started operation from the ``operation_ready`` event when one
    exists, else the predecessor's actual_end, else the WO's released_at,
  * Little's Law = avg daily open-WO count / daily completion rate,
  * WIP aging ordering (oldest release first) + days-in-current-operation from
    actual_start or the ready event.

All tenant-scoped: company B look-alike rows must never leak into company A math.
"""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntryType
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.flow_metrics_service import get_flow_metrics, get_wip_aging
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_entry,
    make_op,
    make_part,
    make_shipment,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = date(2026, 6, 1)
WINDOW_END = date(2026, 6, 10)


def _ready_event(db: Session, *, company_id: int, wo, op, occurred_at: datetime) -> OperationalEvent:
    """Seed an operation_ready event directly (what the release flips emit)."""
    event = OperationalEvent(
        company_id=company_id,
        event_type="operation_ready",
        source_module="work_order_state",
        entity_type="work_order_operation",
        entity_id=op.id,
        work_order_id=wo.id,
        operation_id=op.id,
        severity="info",
        event_payload={"operation_id": op.id},
        occurred_at=occurred_at,
    )
    db.add(event)
    db.commit()
    return event


def test_flow_summary_exact_lead_pce_littles_law_and_provenance(db_session: Session):
    """Two completed WOs with known timestamps -> exact summary numbers."""
    user = make_user(db_session)
    part = make_part(db_session)

    # F1: released Jun 1 00:00 -> done Jun 5 12:00 = 4.5 days lead.
    f1 = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        released_at=datetime(2026, 6, 1, 0, 0),
        actual_end=datetime(2026, 6, 5, 12, 0),
    )
    # Value-add: 5.4 RUN hours -> PCE = 5.4 / (4.5 * 24) = 5.0%.
    make_entry(
        db_session, user, f1, None, None, clock_in=datetime(2026, 6, 2, 8, 0), duration_hours=5.4, source="kiosk"
    )
    # INSPECTION labor is NOT value-add (PCE counts RUN only).
    make_entry(
        db_session,
        user,
        f1,
        None,
        None,
        entry_type=TimeEntryType.INSPECTION,
        clock_in=datetime(2026, 6, 2, 14, 0),
        duration_hours=2.0,
    )
    # Partial shipments: first Jun 3, last Jun 6 (release Jun 1 -> 2 and 5 days).
    make_shipment(db_session, f1, ship_date=date(2026, 6, 3), quantity_shipped=4)
    make_shipment(db_session, f1, ship_date=date(2026, 6, 6), quantity_shipped=6)

    # F2: released May 30 06:00 -> done Jun 8 06:00 = 9.0 days lead.
    f2 = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        released_at=datetime(2026, 5, 30, 6, 0),
        actual_end=datetime(2026, 6, 8, 6, 0),
    )
    # 10.8 RUN hours -> PCE = 10.8 / (9 * 24) = 5.0%.
    make_entry(db_session, user, f2, None, None, clock_in=datetime(2026, 6, 3, 8, 0), duration_hours=10.8)
    # Provenance rule: backfilled RUN labor is EXCLUDED from value-add/PCE and
    # reported separately (4.0h booked in the window via the backfill channel).
    make_entry(
        db_session, user, f2, None, None, clock_in=datetime(2026, 6, 4, 8, 0), duration_hours=4.0, source="backfill"
    )

    # Cross-tenant look-alike: identical completed WO in company B must not leak.
    part_b = make_part(db_session, company_id=COMPANY_B)
    make_wo(
        db_session,
        part_b,
        company_id=COMPANY_B,
        status_=WorkOrderStatus.COMPLETE,
        released_at=datetime(2026, 6, 1, 0, 0),
        actual_end=datetime(2026, 6, 2, 0, 0),
    )

    result = get_flow_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    summary = result.summary

    assert summary.work_orders_completed == 2
    assert summary.avg_lead_time_days == pytest.approx(6.75)  # (4.5 + 9.0) / 2
    assert summary.median_lead_time_days == pytest.approx(6.75)  # even count -> midpoint
    assert summary.avg_release_to_last_ship_days == pytest.approx(5.0)  # only F1 shipped
    assert summary.avg_pce_pct == pytest.approx(5.0)
    assert summary.excluded_backfill_import_hours == pytest.approx(4.0)

    # Little's Law over the 10-day window:
    #   F1 open Jun 1-4 (4 days), F2 open Jun 1-7 (7 days) -> avg WIP 11/10 = 1.1;
    #   2 completions / 10 days = 0.2/day -> throughput time 1.1 / 0.2 = 5.5 days.
    assert summary.daily_completion_rate == pytest.approx(0.2)
    assert summary.avg_wip == pytest.approx(1.1)
    assert summary.littles_law_throughput_days == pytest.approx(5.5)

    # Per-WO detail: exact lead/ship/PCE fields for F1.
    row_f1 = next(row for row in result.work_orders if row.work_order_id == f1.id)
    assert row_f1.lead_time_days == pytest.approx(4.5)
    assert row_f1.first_ship_date == date(2026, 6, 3)
    assert row_f1.last_ship_date == date(2026, 6, 6)
    assert row_f1.release_to_first_ship_days == pytest.approx(2.0)
    assert row_f1.release_to_last_ship_days == pytest.approx(5.0)
    assert row_f1.value_add_hours == pytest.approx(5.4)
    assert row_f1.pce_pct == pytest.approx(5.0)
    # Company B's WO is nowhere in the detail rows.
    assert len(result.work_orders) == 2


def test_cancelled_and_out_of_window_wos_excluded(db_session: Session):
    """CANCELLED never enters WIP or completions; completions anchor on actual_end."""
    part = make_part(db_session)
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.CANCELLED,
        released_at=datetime(2026, 6, 2, 0, 0),
    )
    # Completed BEFORE the window -> not a window completion.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        released_at=datetime(2026, 5, 1, 0, 0),
        actual_end=datetime(2026, 5, 20, 0, 0),
    )

    result = get_flow_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    assert result.summary.work_orders_completed == 0
    assert result.summary.avg_lead_time_days is None
    assert result.summary.avg_wip is None
    assert result.summary.littles_law_throughput_days is None


def test_queue_time_event_predecessor_and_release_anchors(db_session: Session):
    """Queue time prefers the operation_ready event, then falls back to the
    predecessor's actual_end, then to the WO's released_at for a first op."""
    part = make_part(db_session)
    wc1 = make_work_center(db_session)
    wc2 = make_work_center(db_session)

    wo1 = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, released_at=datetime(2026, 6, 1, 0, 0))
    # Q1: ready event Jun 2 08:00, started Jun 2 12:00 -> 4.0h (from event).
    q1 = make_op(
        db_session,
        wo1,
        wc1,
        sequence=10,
        status_=OperationStatus.COMPLETE,
        actual_start=datetime(2026, 6, 2, 12, 0),
        actual_end=datetime(2026, 6, 3, 10, 0),
    )
    _ready_event(db_session, company_id=COMPANY_A, wo=wo1, op=q1, occurred_at=datetime(2026, 6, 2, 8, 0))
    # Q2: NO event; predecessor Q1 ended Jun 3 10:00, started Jun 3 16:00 -> 6.0h.
    make_op(
        db_session,
        wo1,
        wc2,
        sequence=20,
        status_=OperationStatus.IN_PROGRESS,
        actual_start=datetime(2026, 6, 3, 16, 0),
    )

    wo2 = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, released_at=datetime(2026, 6, 1, 0, 0))
    # Q3: first op, NO event -> released_at anchor; started Jun 1 08:00 -> 8.0h.
    make_op(
        db_session,
        wo2,
        wc1,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        actual_start=datetime(2026, 6, 1, 8, 0),
    )

    result = get_flow_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)

    assert result.summary.avg_queue_hours == pytest.approx(6.0)  # (4 + 6 + 8) / 3

    by_wc = {row.work_center_id: row for row in result.queue_by_work_center}
    assert by_wc[wc1.id].samples == 2
    assert by_wc[wc1.id].avg_queue_hours == pytest.approx(6.0)  # (4 + 8) / 2
    assert by_wc[wc1.id].max_queue_hours == pytest.approx(8.0)
    assert by_wc[wc1.id].from_ready_events == 1  # only Q1 was event-measured
    assert by_wc[wc2.id].samples == 1
    assert by_wc[wc2.id].avg_queue_hours == pytest.approx(6.0)
    assert by_wc[wc2.id].from_ready_events == 0


def test_queue_time_excludes_soft_deleted_wo_operations(db_session: Session):
    """Operations of a soft-deleted WO contribute NO queue-time samples (pins
    the WorkOrder join + is_deleted filter added after review)."""
    user = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    live_wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, released_at=datetime(2026, 6, 1, 0, 0))
    make_op(
        db_session,
        live_wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        actual_start=datetime(2026, 6, 1, 8, 0),  # 8.0h from released_at
    )

    deleted_wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, released_at=datetime(2026, 6, 1, 0, 0))
    make_op(
        db_session,
        deleted_wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        actual_start=datetime(2026, 6, 3, 0, 0),  # would be a 48h ghost sample
    )
    deleted_wo.soft_delete(user.id)
    db_session.commit()

    result = get_flow_metrics(db_session, COMPANY_A, WINDOW_START, WINDOW_END)
    by_wc = {row.work_center_id: row for row in result.queue_by_work_center}
    assert by_wc[wc.id].samples == 1
    assert by_wc[wc.id].avg_queue_hours == pytest.approx(8.0)  # the 48h ghost is out
    assert result.summary.avg_queue_hours == pytest.approx(8.0)


def test_wip_aging_ordering_and_anchors(db_session: Session):
    """Aging list: oldest release first; days-in-op from actual_start, else the
    ready event; days_to_due negative when past due. Tenant + status scoped."""
    now = datetime.utcnow()
    part = make_part(db_session)
    wc = make_work_center(db_session)

    # W1: released 10 days ago, current op started 2 days ago, due yesterday.
    w1 = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        released_at=now - timedelta(days=10),
        due_date=date.today() - timedelta(days=1),
    )
    op1 = make_op(
        db_session,
        w1,
        wc,
        status_=OperationStatus.IN_PROGRESS,
        actual_start=now - timedelta(days=2),
    )
    w1.current_operation_id = op1.id
    # W2: released 3 days ago, current op NOT started but READY 1 day ago.
    w2 = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, released_at=now - timedelta(days=3))
    op2 = make_op(db_session, w2, wc, status_=OperationStatus.READY)
    w2.current_operation_id = op2.id
    db_session.commit()
    _ready_event(db_session, company_id=COMPANY_A, wo=w2, op=op2, occurred_at=now - timedelta(days=1))

    # Excluded: a COMPLETE WO, and a company-B open WO.
    make_wo(db_session, part, status_=WorkOrderStatus.COMPLETE, released_at=now - timedelta(days=30))
    part_b = make_part(db_session, company_id=COMPANY_B)
    make_wo(
        db_session, part_b, company_id=COMPANY_B, status_=WorkOrderStatus.RELEASED, released_at=now - timedelta(days=99)
    )

    result = get_wip_aging(db_session, COMPANY_A)

    assert result.total_open == 2
    assert [item.work_order_id for item in result.items] == [w1.id, w2.id]  # oldest first

    item1, item2 = result.items
    assert item1.days_since_release == pytest.approx(10.0, abs=0.05)
    assert item1.days_in_current_operation == pytest.approx(2.0, abs=0.05)
    assert item1.current_operation_id == op1.id
    assert item1.current_work_center_name == wc.name
    assert item1.days_to_due == -1

    assert item2.days_since_release == pytest.approx(3.0, abs=0.05)
    # Not started: anchored on the operation_ready event instead.
    assert item2.days_in_current_operation == pytest.approx(1.0, abs=0.05)
    assert item2.days_to_due is None
