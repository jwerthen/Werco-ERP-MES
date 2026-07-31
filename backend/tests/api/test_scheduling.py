from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from tests.api.kiosk_test_helpers import (
    COMPANY_B,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)


@pytest.mark.api
@pytest.mark.requires_db
class TestSchedulingAPI:
    def test_schedule_work_order_targets_current_operation_not_first(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        part = Part(
            part_number="SCHED-PART-001",
            name="Schedule Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc10 = WorkCenter(
            code="SCHED-WC-10",
            name="Schedule WC 10",
            work_center_type="machining",
            is_active=True,
            company_id=1,
        )
        wc20 = WorkCenter(
            code="SCHED-WC-20",
            name="Schedule WC 20",
            work_center_type="machining",
            is_active=True,
            company_id=1,
        )
        wc30 = WorkCenter(
            code="SCHED-WC-30",
            name="Schedule WC 30",
            work_center_type="machining",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc10, wc20, wc30])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-SCHED-001",
            part_id=part.id,
            quantity_ordered=5,
            status="released",
            priority=5,
            due_date=date(2026, 2, 28),
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        # Op 10 is complete (history should be preserved)
        op10 = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=wc10.id,
            sequence=10,
            operation_number="Op 10",
            name="Cut",
            status=OperationStatus.COMPLETE,
            scheduled_start=datetime(2026, 2, 1, 8, 0, 0),
            scheduled_end=datetime(2026, 2, 1, 12, 0, 0),
            setup_time_hours=1,
            run_time_hours=1,
            company_id=1,
        )
        # Op 20 is current (first non-complete) and unscheduled
        op20 = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=wc20.id,
            sequence=20,
            operation_number="Op 20",
            name="Bore",
            status=OperationStatus.PENDING,
            setup_time_hours=2,
            run_time_hours=6,
            company_id=1,
        )
        # Op 30 has stale schedule that should be cleared
        op30 = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=wc30.id,
            sequence=30,
            operation_number="Op 30",
            name="Inspect",
            status=OperationStatus.PENDING,
            scheduled_start=datetime(2026, 2, 10, 8, 0, 0),
            scheduled_end=datetime(2026, 2, 10, 12, 0, 0),
            setup_time_hours=1,
            run_time_hours=1,
            company_id=1,
        )
        db_session.add_all([op10, op20, op30])
        db_session.commit()

        response = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json={"scheduled_start": "2026-02-25"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        db_session.refresh(op10)
        db_session.refresh(op20)
        db_session.refresh(op30)

        assert data["first_operation_id"] == op20.id
        assert data["scheduled_start"] == "2026-02-25"
        assert op20.scheduled_start is not None
        assert op20.scheduled_start.date() == date(2026, 2, 25)
        assert op20.scheduled_end is not None
        assert op20.status == OperationStatus.READY

        # Preserve completed history
        assert op10.scheduled_start is not None
        assert op10.scheduled_end is not None

        # Clear subsequent scheduling
        assert op30.scheduled_start is None
        assert op30.scheduled_end is None

    def test_schedule_work_order_earliest_respects_existing_capacity(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        today = date.today()

        part = Part(
            part_number="SCHED-PART-002",
            name="Earliest Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc = WorkCenter(
            code="SCHED-WC-CAP",
            name="Capacity WC",
            work_center_type="machining",
            capacity_hours_per_day=8.0,
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc])
        db_session.flush()

        busy_work_order = WorkOrder(
            work_order_number="WO-SCHED-BUSY",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=4,
            due_date=today + timedelta(days=5),
            company_id=1,
        )
        target_work_order = WorkOrder(
            work_order_number="WO-SCHED-EARLIEST",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=2,
            due_date=today + timedelta(days=3),
            company_id=1,
        )
        db_session.add_all([busy_work_order, target_work_order])
        db_session.flush()

        busy_op = WorkOrderOperation(
            work_order_id=busy_work_order.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="Op 10",
            name="Busy",
            status=OperationStatus.READY,
            scheduled_start=datetime.combine(today, datetime.min.time()),
            scheduled_end=datetime.combine(today, datetime.min.time()),
            setup_time_hours=0,
            run_time_hours=8,
            company_id=1,
        )
        target_op = WorkOrderOperation(
            work_order_id=target_work_order.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="Op 10",
            name="Target",
            status=OperationStatus.PENDING,
            setup_time_hours=0,
            run_time_hours=4,
            company_id=1,
        )
        db_session.add_all([busy_op, target_op])
        db_session.commit()

        response = client.post(
            f"/api/v1/scheduling/work-orders/{target_work_order.id}/schedule-earliest",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()

        expected_start = today + timedelta(days=1)
        assert payload["scheduled_start"] == expected_start.isoformat()

        db_session.refresh(target_op)
        assert target_op.scheduled_start is not None
        assert target_op.scheduled_start.date() == expected_start
        assert target_op.status == OperationStatus.READY

    def test_capacity_heatmap_flags_overloaded_day(self, client: TestClient, auth_headers: dict, db_session):
        start = date.today() + timedelta(days=2)

        part = Part(
            part_number="SCHED-PART-003",
            name="Heatmap Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc = WorkCenter(
            code="SCHED-WC-HEAT",
            name="Heatmap WC",
            work_center_type="machining",
            capacity_hours_per_day=4.0,
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-SCHED-HEAT",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=3,
            due_date=start + timedelta(days=2),
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        overloaded_op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="Op 10",
            name="Overloaded",
            status=OperationStatus.READY,
            scheduled_start=datetime.combine(start, datetime.min.time()),
            scheduled_end=datetime.combine(start, datetime.min.time()),
            setup_time_hours=0,
            run_time_hours=8,
            company_id=1,
        )
        db_session.add(overloaded_op)
        db_session.commit()

        response = client.get(
            "/api/v1/scheduling/capacity-heatmap",
            headers=auth_headers,
            params={
                "start_date": start.isoformat(),
                "end_date": start.isoformat(),
                "work_center_id": wc.id,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()

        assert payload["overload_cells"] == 1
        assert wc.id in payload["overloaded_work_centers"]
        assert len(payload["work_centers"]) == 1

        day = payload["work_centers"][0]["days"][0]
        assert day["date"] == start.isoformat()
        assert day["overloaded"] is True
        assert day["utilization_pct"] == 200.0

    def test_capacity_summary_counts_spanning_operations_and_machine_capacity(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        start = date.today() + timedelta(days=1)
        end = start + timedelta(days=1)

        part = Part(
            part_number="SCHED-PART-SUMMARY",
            name="Summary Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc = WorkCenter(
            code="SCHED-WC-SUM",
            name="Summary WC",
            work_center_type="laser",
            capacity_hours_per_day=10.0,
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-SCHED-SUM",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=3,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="Op 10",
            name="Spanning Op",
            status=OperationStatus.READY,
            scheduled_start=datetime.combine(start - timedelta(days=1), datetime.min.time()),
            scheduled_end=datetime.combine(end, datetime.min.time()),
            setup_time_hours=0,
            run_time_hours=12,
            company_id=1,
        )
        db_session.add(op)
        db_session.commit()

        response = client.get(
            "/api/v1/scheduling/capacity",
            headers=auth_headers,
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )

        assert response.status_code == status.HTTP_200_OK
        row = next(item for item in response.json() if item["work_center_id"] == wc.id)

        assert row["scheduled_hours"] == 8.0
        assert row["available_hours"] == 20.0
        assert row["capacity_hours_per_day"] == 10.0
        assert row["utilization_pct"] == 40.0

    def test_capacity_preview_includes_projected_work_order_routing(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        target_date = date.today() + timedelta(days=2)

        part = Part(
            part_number="SCHED-PART-PREVIEW",
            name="Preview Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc_laser = WorkCenter(
            code="SCHED-WC-LAS",
            name="Preview Laser",
            work_center_type="laser",
            capacity_hours_per_day=8.0,
            is_active=True,
            company_id=1,
        )
        wc_weld = WorkCenter(
            code="SCHED-WC-WELD",
            name="Preview Weld",
            work_center_type="welding",
            capacity_hours_per_day=8.0,
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc_laser, wc_weld])
        db_session.flush()

        existing_work_order = WorkOrder(
            work_order_number="WO-SCHED-PREVIEW-BUSY",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=5,
            company_id=1,
        )
        target_work_order = WorkOrder(
            work_order_number="WO-SCHED-PREVIEW",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=3,
            company_id=1,
        )
        db_session.add_all([existing_work_order, target_work_order])
        db_session.flush()

        existing_op = WorkOrderOperation(
            work_order_id=existing_work_order.id,
            work_center_id=wc_laser.id,
            sequence=10,
            operation_number="Op 10",
            name="Existing Cut",
            status=OperationStatus.READY,
            scheduled_start=datetime.combine(target_date, datetime.min.time()),
            scheduled_end=datetime.combine(target_date, datetime.min.time()),
            setup_time_hours=1,
            run_time_hours=1,
            company_id=1,
        )
        current_op = WorkOrderOperation(
            work_order_id=target_work_order.id,
            work_center_id=wc_laser.id,
            sequence=10,
            operation_number="Op 10",
            name="Projected Cut",
            status=OperationStatus.PENDING,
            setup_time_hours=1,
            run_time_hours=2,
            company_id=1,
        )
        next_op = WorkOrderOperation(
            work_order_id=target_work_order.id,
            work_center_id=wc_weld.id,
            sequence=20,
            operation_number="Op 20",
            name="Projected Weld",
            status=OperationStatus.PENDING,
            setup_time_hours=2,
            run_time_hours=3,
            company_id=1,
        )
        db_session.add_all([existing_op, current_op, next_op])
        db_session.commit()

        response = client.post(
            "/api/v1/scheduling/capacity-for-date",
            headers=auth_headers,
            json={
                "work_center_id": wc_laser.id,
                "target_date": target_date.isoformat(),
                "work_order_id": target_work_order.id,
                "forward_schedule": True,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()

        assert payload["existing_hours"] == 2.0
        assert payload["projected_hours"] == 3.0
        assert payload["projected_total_hours"] == 8.0
        assert payload["used_hours"] == 5.0
        assert any(job["projected"] for job in payload["jobs_on_date"])

    def test_schedule_earliest_forward_schedule_checks_downstream_capacity(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        today = date.today()

        part = Part(
            part_number="SCHED-PART-DOWNSTREAM",
            name="Downstream Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        wc_cut = WorkCenter(
            code="SCHED-WC-CUT",
            name="Cut WC",
            work_center_type="laser",
            capacity_hours_per_day=8.0,
            is_active=True,
            company_id=1,
        )
        wc_weld = WorkCenter(
            code="SCHED-WC-DOWN",
            name="Downstream WC",
            work_center_type="welding",
            capacity_hours_per_day=8.0,
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, wc_cut, wc_weld])
        db_session.flush()

        busy_work_order = WorkOrder(
            work_order_number="WO-SCHED-DOWN-BUSY",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=5,
            company_id=1,
        )
        target_work_order = WorkOrder(
            work_order_number="WO-SCHED-DOWN-TARGET",
            part_id=part.id,
            quantity_ordered=1,
            status="released",
            priority=2,
            company_id=1,
        )
        db_session.add_all([busy_work_order, target_work_order])
        db_session.flush()

        busy_op = WorkOrderOperation(
            work_order_id=busy_work_order.id,
            work_center_id=wc_weld.id,
            sequence=10,
            operation_number="Op 10",
            name="Busy Weld",
            status=OperationStatus.READY,
            scheduled_start=datetime.combine(today + timedelta(days=1), datetime.min.time()),
            scheduled_end=datetime.combine(today + timedelta(days=1), datetime.min.time()),
            setup_time_hours=0,
            run_time_hours=8,
            company_id=1,
        )
        cut_op = WorkOrderOperation(
            work_order_id=target_work_order.id,
            work_center_id=wc_cut.id,
            sequence=10,
            operation_number="Op 10",
            name="Target Cut",
            status=OperationStatus.PENDING,
            setup_time_hours=0,
            run_time_hours=1,
            company_id=1,
        )
        weld_op = WorkOrderOperation(
            work_order_id=target_work_order.id,
            work_center_id=wc_weld.id,
            sequence=20,
            operation_number="Op 20",
            name="Target Weld",
            status=OperationStatus.PENDING,
            setup_time_hours=0,
            run_time_hours=8,
            company_id=1,
        )
        db_session.add_all([busy_op, cut_op, weld_op])
        db_session.commit()

        response = client.post(
            f"/api/v1/scheduling/work-orders/{target_work_order.id}/schedule-earliest",
            headers=auth_headers,
            json={"forward_schedule": True},
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()

        assert payload["scheduled_start"] == (today + timedelta(days=1)).isoformat()

        db_session.refresh(cut_op)
        db_session.refresh(weld_op)
        assert cut_op.scheduled_start.date() == today + timedelta(days=1)
        assert weld_op.scheduled_start.date() == today + timedelta(days=2)


# ---------------------------------------------------------------------------
# Audit rows on the two scheduling endpoints (compliance invariant 2)
# ---------------------------------------------------------------------------

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_work_center(db_session, *, active: bool = True) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        code=f"SCHED-AUD-WC-{n}",
        name=f"Audit WC {n}",
        work_center_type="machining",
        capacity_hours_per_day=8.0,
        is_active=active,
        company_id=1,
    )
    db_session.add(wc)
    db_session.flush()
    return wc


def _make_scheduling_wo(
    db_session,
    *,
    work_center: WorkCenter,
    op_status: OperationStatus = OperationStatus.READY,
    run_order=None,
):
    """A released WO with one unscheduled operation on ``work_center``. Commits."""
    n = _next()
    part = Part(
        part_number=f"SCHED-AUD-P-{n}",
        name=f"Audit Part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.flush()
    work_order = WorkOrder(
        work_order_number=f"WO-SCHED-AUD-{n}",
        part_id=part.id,
        quantity_ordered=1,
        status="released",
        priority=3,
        due_date=date.today() + timedelta(days=14),
        company_id=1,
    )
    db_session.add(work_order)
    db_session.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="Op 10",
        name="Audit Op",
        status=op_status,
        run_order=run_order,
        setup_time_hours=1,
        run_time_hours=1,
        company_id=1,
    )
    db_session.add(operation)
    db_session.commit()
    return work_order, operation


def _committed_op_audit_rows(db_session, operation_id: int):
    """Fetch the operation's AuditLog rows that were actually COMMITTED.

    The ``client`` fixture overrides ``get_db`` to yield the ONE shared, never-
    closed ``db_session``, so the endpoint and the test share a single open
    transaction. ``AuditService.log()`` only ``flush()``es -- the handler owns the
    ``commit()``. If a handler logged the audit row AFTER its ``db.commit()``,
    the row would be flushed into a fresh, never-committed transaction, yet a
    naive ``db.query(AuditLog)`` here would still SEE it. Rolling back BEFORE
    querying closes that loophole: a committed row survives, a flushed-only row
    vanishes. (Same guard as tests/api/test_work_orders_audit_persistence.py.)
    """
    db_session.rollback()
    db_session.expire_all()
    return (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == operation_id,
        )
        .order_by(AuditLog.sequence_number.desc())
        .all()
    )


@pytest.mark.api
@pytest.mark.requires_db
class TestSchedulingAuditRows:
    """Invariant 2: both scheduling endpoints write a COMMITTED audit_log UPDATE
    row for the operation they reschedule / move -- exactly like the dedicated
    move endpoints -- and a refused call writes nothing and mutates nothing."""

    def test_schedule_with_work_center_change_writes_committed_move_audit(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """PUT /schedule with work_center_id is a move: one committed row whose
        changes carry the work-center swap AND the rank clear (run_order N -> None)."""
        wc_from = _make_work_center(db_session)
        wc_to = _make_work_center(db_session)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc_from, run_order=3)

        response = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json={"scheduled_start": "2026-03-02", "work_center_id": wc_to.id},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_op_audit_rows(db_session, op.id)
        assert len(rows) == 1, "expected exactly one COMMITTED audit row for the scheduled operation"
        row = rows[0]
        assert row.action == "UPDATE"
        assert row.resource_type == "work_order_operation"
        assert row.resource_id == op.id
        assert row.resource_identifier == "Op 10"
        assert row.company_id == 1
        changes = row.extra_data["changes"]
        assert changes["work_center_id"] == {"old": wc_from.id, "new": wc_to.id}
        assert changes["run_order"] == {"old": 3, "new": None}
        assert row.extra_data["via"] == "schedule"
        assert row.extra_data["work_order_id"] == work_order.id

    def test_schedule_pure_reschedule_writes_committed_audit(self, client: TestClient, auth_headers: dict, db_session):
        """PUT /schedule WITHOUT work_center_id still audits: the schedule change
        alone carries the diff; the work center and the manual rank are untouched."""
        wc = _make_work_center(db_session)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc, run_order=2)

        response = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json={"scheduled_start": "2026-03-02"},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_op_audit_rows(db_session, op.id)
        assert len(rows) == 1
        changes = rows[0].extra_data["changes"]
        assert changes["scheduled_start"]["old"] is None
        # Schedule values are normalized to one midnight-anchored ISO form on
        # BOTH diff sides (_audit_schedule_value), so the date payload and the
        # DateTime column compare equal on a same-day re-submit.
        assert changes["scheduled_start"]["new"] == "2026-03-02T00:00:00"
        assert "work_center_id" not in changes, "no move happened; the WC key must not appear in the diff"
        assert "run_order" not in changes, "a pure reschedule must leave the manual rank alone"
        assert rows[0].company_id == 1

    def test_identical_resubmit_self_suppresses(self, client: TestClient, auth_headers: dict, db_session):
        """A byte-identical re-submit is a genuine no-op: the normalized diff is
        empty, so log_update self-suppresses and no second row is written. This
        only holds because both diff sides normalize through
        _audit_schedule_value -- the raw DateTime-column-vs-date-payload forms
        would never compare equal."""
        wc = _make_work_center(db_session)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc)

        payload = {"scheduled_start": "2026-03-02"}
        first = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json=payload,
        )
        assert first.status_code == status.HTTP_200_OK, first.text
        assert len(_committed_op_audit_rows(db_session, op.id)) == 1

        second = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json=payload,
        )
        assert second.status_code == status.HTTP_200_OK, second.text
        assert len(_committed_op_audit_rows(db_session, op.id)) == 1, "identical re-submit must not write a second row"

    def test_schedule_earliest_with_move_writes_committed_audit(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """POST /schedule-earliest with an explicit different work_center_id is a
        move: the committed row carries the swap and the rank clear."""
        wc_from = _make_work_center(db_session)
        wc_to = _make_work_center(db_session)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc_from, run_order=1)

        response = client.post(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule-earliest",
            headers=auth_headers,
            json={"work_center_id": wc_to.id},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_op_audit_rows(db_session, op.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "UPDATE"
        assert row.company_id == 1
        changes = row.extra_data["changes"]
        assert changes["work_center_id"] == {"old": wc_from.id, "new": wc_to.id}
        assert changes["run_order"] == {"old": 1, "new": None}
        assert row.extra_data["via"] == "schedule_earliest"

    def test_schedule_earliest_same_work_center_still_writes_committed_audit(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """POST /schedule-earliest with NO work_center_id defaults to the op's own
        WC (old == new on that key) -- the row must still commit, carried by the
        scheduled_start / status keys. The rank survives a same-WC re-send."""
        wc = _make_work_center(db_session)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc, op_status=OperationStatus.PENDING, run_order=4)

        response = client.post(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule-earliest",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _committed_op_audit_rows(db_session, op.id)
        assert len(rows) == 1
        changes = rows[0].extra_data["changes"]
        assert "work_center_id" not in changes, "same-WC re-send: the WC key must not appear in the diff"
        assert "run_order" not in changes, "same-WC re-send must leave the manual rank alone"
        assert changes["scheduled_start"]["old"] is None
        assert changes["scheduled_start"]["new"] is not None
        # The PENDING -> READY flip on a released WO rides in the same UPDATE row.
        assert changes["status"] == {"old": "pending", "new": "ready"}

    def test_refused_move_writes_no_audit_and_mutates_nothing(self, client: TestClient, auth_headers: dict, db_session):
        """An inactive target WC refuses BOTH endpoints with 404: zero committed
        audit rows, and the operation keeps its WC, rank, schedule, and status."""
        wc_from = _make_work_center(db_session)
        wc_inactive = _make_work_center(db_session, active=False)
        work_order, op = _make_scheduling_wo(db_session, work_center=wc_from, run_order=2)

        response = client.put(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule",
            headers=auth_headers,
            json={"scheduled_start": "2026-03-02", "work_center_id": wc_inactive.id},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

        response = client.post(
            f"/api/v1/scheduling/work-orders/{work_order.id}/schedule-earliest",
            headers=auth_headers,
            json={"work_center_id": wc_inactive.id},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

        assert _committed_op_audit_rows(db_session, op.id) == []
        fresh = db_session.get(WorkOrderOperation, op.id)
        assert fresh.work_center_id == wc_from.id
        assert fresh.run_order == 2
        assert fresh.scheduled_start is None
        assert fresh.scheduled_end is None
        assert fresh.status == OperationStatus.READY


# ---------------------------------------------------------------------------
# GET /scheduling/work-orders — canonical server order + the RUN chip fields
# ---------------------------------------------------------------------------

SCHEDULABLE_URL = "/api/v1/scheduling/work-orders"


def _schedulable_rows(client: TestClient, headers: dict, **params) -> list:
    resp = client.get(SCHEDULABLE_URL, headers=headers, params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


@pytest.mark.api
@pytest.mark.requires_db
class TestGetSchedulableWorkOrders:
    """The planners' Dispatch Queue read, run-order-first (owner decision).

    Rows arrive in the server's canonical CROSS-machine order — priority, due
    date, then ``work_order_number`` as the deterministic tiebreak — and the
    client renders that order verbatim (the old client dispatch-score re-sort
    is gone). Each row also carries the current op's ``work_center_code`` and
    ``run_order``: the same gap-free position the kiosk / shop-floor /
    Dispatch Board RUN chip shows, computed from the work center's FULL live
    queue; null when the op is unranked or not queued. A per-machine rank is
    display context here, never a sort key.
    """

    def test_rows_arrive_priority_due_date_then_wo_number(self, client: TestClient, db_session):
        """Rows the OLD client score would have inverted. The deleted
        utils/dispatchScore.ts gave overdue P5 work ~366 points vs ~160 for
        far-future P1 work, so the score sort led with the overdue P5 row.
        The server's SQL order (priority, due_date, work_order_number) puts
        the P1 row first — and breaks a full priority+due tie by WO number,
        not by id/creation order."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)

        wo_p1_future, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_p5_overdue, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_tie_zz, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_tie_aa, _ = make_wo_with_operation(db_session, work_center=wc)

        wo_p1_future.priority = 1
        wo_p1_future.due_date = date.today() + timedelta(days=60)
        wo_p5_overdue.priority = 5
        wo_p5_overdue.due_date = date.today() - timedelta(days=30)
        # Same priority AND due date: only the WO-number tiebreak decides. The
        # EARLIER-created WO gets the lexicographically LATER number, so an id
        # or creation-order tiebreak could not fake this result.
        for wo in (wo_tie_zz, wo_tie_aa):
            wo.priority = 5
            wo.due_date = date.today() + timedelta(days=10)
        wo_tie_zz.work_order_number = f"ZZ-{wo_tie_zz.work_order_number}"
        wo_tie_aa.work_order_number = f"AA-{wo_tie_aa.work_order_number}"
        db_session.commit()

        rows = _schedulable_rows(client, user_headers(manager), work_center_id=wc.id)
        assert [row["work_order_number"] for row in rows] == [
            wo_p1_future.work_order_number,  # priority 1 beats everything, however far out its due date
            wo_p5_overdue.work_order_number,  # the old score sort's #1 pick lands mid-list on priority
            wo_tie_aa.work_order_number,  # AA-* before ZZ-*: WO number, not creation order
            wo_tie_zz.work_order_number,
        ]

    def test_run_order_matches_kiosk_position_over_the_full_queue(self, client: TestClient, db_session):
        """Parity with the kiosk RUN chip, including full-queue semantics: rank 1
        at the laser belongs to a later-sequence op of a WO this endpoint emits
        under ANOTHER work center, so the emitted laser rows take positions 2
        and 3 — the numbers the operator sees at the machine — never 1 and 2
        renumbered over this endpoint's filtered row set."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc_mill = make_work_center(db_session)
        wc_laser = make_work_center(db_session)

        # WO-X: current op at the MILL; its later laser op is READY (laser-nest
        # style cross-WC readiness) and holds the laser's rank 1.
        wo_x, op_x_mill = make_wo_with_operation(db_session, work_center=wc_mill)
        laser_rank1 = WorkOrderOperation(
            work_order_id=wo_x.id,
            work_center_id=wc_laser.id,
            sequence=20,
            operation_number="OP20",
            name="Laser trim",
            status=OperationStatus.READY,
            run_order=1,
            company_id=1,
        )
        db_session.add(laser_rank1)
        _, op_y = make_wo_with_operation(db_session, work_center=wc_laser)
        _, op_z = make_wo_with_operation(db_session, work_center=wc_laser)
        op_y.run_order = 10  # sparse stored ranks: the wire value must be the gap-free position
        op_z.run_order = 20
        db_session.commit()

        rows = _schedulable_rows(client, user_headers(manager), work_center_id=wc_laser.id)
        assert [(row["current_operation_id"], row["run_order"]) for row in rows] == [
            (op_y.id, 2),
            (op_z.id, 3),
        ]
        assert all(row["work_center_code"] == wc_laser.code for row in rows)

        # Byte-for-byte the kiosk queue's chip numbers for the same operations.
        kiosk_resp = client.get(queue_url(wc_laser.id), headers=user_headers(manager))
        assert kiosk_resp.status_code == status.HTTP_200_OK, kiosk_resp.text
        kiosk_positions = {row["operation_id"]: row["run_order"] for row in kiosk_resp.json()["queue"]}
        assert kiosk_positions[laser_rank1.id] == 1
        assert kiosk_positions[op_y.id] == 2
        assert kiosk_positions[op_z.id] == 3

        # WO-X's own row surfaces under the mill with its op-10 context: queued
        # there but unranked, so the chip stays off.
        mill_rows = _schedulable_rows(client, user_headers(manager), work_center_id=wc_mill.id)
        assert [(row["work_order_id"], row["current_operation_id"]) for row in mill_rows] == [(wo_x.id, op_x_mill.id)]
        assert mill_rows[0]["work_center_code"] == wc_mill.code
        assert mill_rows[0]["run_order"] is None

    def test_pending_and_unranked_rows_carry_null_run_order(self, client: TestClient, db_session):
        """A PENDING current op is emitted (only all-COMPLETE WOs are skipped) but
        is NOT on the live dispatch queue: run_order must be null even when a
        stale stored rank exists, and it must not steal a position — the lone
        ranked READY op is RUN 1 (gap-free position, not its raw stored 5)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op_pending = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.PENDING)
        _, op_unranked = make_wo_with_operation(db_session, work_center=wc)
        _, op_ranked = make_wo_with_operation(db_session, work_center=wc)
        op_pending.run_order = 1  # stale rank left behind by a status change
        op_ranked.run_order = 5
        db_session.commit()

        rows = _schedulable_rows(client, user_headers(manager), work_center_id=wc.id)
        by_op = {row["current_operation_id"]: row["run_order"] for row in rows}
        assert by_op == {op_pending.id: None, op_unranked.id: None, op_ranked.id: 1}

    def test_all_complete_skip_and_work_center_filter_unchanged(self, client: TestClient, db_session):
        """The endpoint's pre-existing filter semantics survive the rework: a WO
        whose every op is COMPLETE never emits, work_center_id narrows to WOs
        whose CURRENT op sits at that center, and another tenant's WOs stay
        invisible either way."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        other_wc = make_work_center(db_session)
        wo_done, _ = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.COMPLETE)
        wo_live, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_other, _ = make_wo_with_operation(db_session, work_center=other_wc)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_B)
        wo_foreign, _ = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=foreign_wc)

        filtered_ids = [
            row["work_order_id"] for row in _schedulable_rows(client, user_headers(manager), work_center_id=wc.id)
        ]
        assert filtered_ids == [wo_live.id]

        # Unfiltered read (membership only — sibling tests on this worker DB may
        # contribute rows of their own).
        unfiltered_ids = {row["work_order_id"] for row in _schedulable_rows(client, user_headers(manager))}
        assert wo_live.id in unfiltered_ids
        assert wo_other.id in unfiltered_ids
        assert wo_done.id not in unfiltered_ids, "an all-COMPLETE WO must never emit"
        assert wo_foreign.id not in unfiltered_ids, "cross-tenant leak: another company's WO surfaced"

    def test_soft_deleted_wo_neither_lists_nor_schedules(self, client: TestClient, db_session):
        """Invariant 3 (soft delete): a soft-deleted RELEASED WO must not appear
        in the planners' Dispatch Queue and must 404 on the schedule verbs —
        pre-existing gap (neither query filtered ``is_deleted``) closed on this
        branch; a deleted WO could previously still list AND be rescheduled."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        wo_live, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_deleted, _ = make_wo_with_operation(db_session, work_center=wc)
        wo_deleted.soft_delete(manager.id)
        db_session.commit()

        listed_ids = {row["work_order_id"] for row in _schedulable_rows(client, user_headers(manager))}
        assert wo_live.id in listed_ids
        assert wo_deleted.id not in listed_ids, "soft-deleted WO surfaced in the scheduling list"

        resp = client.put(
            f"/api/v1/scheduling/work-orders/{wo_deleted.id}/schedule",
            headers=user_headers(manager),
            json={"scheduled_start": date.today().isoformat()},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND, "soft-deleted WO must not be reschedulable"
