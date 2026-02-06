from datetime import date, datetime

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
        )
        wc10 = WorkCenter(
            code="SCHED-WC-10",
            name="Schedule WC 10",
            work_center_type="machining",
            is_active=True,
        )
        wc20 = WorkCenter(
            code="SCHED-WC-20",
            name="Schedule WC 20",
            work_center_type="machining",
            is_active=True,
        )
        wc30 = WorkCenter(
            code="SCHED-WC-30",
            name="Schedule WC 30",
            work_center_type="machining",
            is_active=True,
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

