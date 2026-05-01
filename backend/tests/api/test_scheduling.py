from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation


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

    def test_capacity_heatmap_flags_overloaded_day(
        self, client: TestClient, auth_headers: dict, db_session
    ):
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
