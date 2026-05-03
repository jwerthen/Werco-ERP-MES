from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus


@pytest.mark.api
@pytest.mark.requires_db
class TestReportsAPI:
    def test_employee_time_includes_operation_completion_without_time_entry(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="REPORT-COMP-001",
            name="Report Completion Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-REPORT-COMP",
            name="Report Completion Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-REPORT-COMP",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.COMPLETE,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        completed_at = datetime.utcnow()
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Complete Without Time Entry",
            status=OperationStatus.COMPLETE,
            quantity_complete=5,
            completed_by=operator_user.id,
            actual_end=completed_at,
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/employee-time",
            headers=auth_headers,
            params={
                "start_date": completed_at.date().isoformat(),
                "end_date": completed_at.date().isoformat(),
                "user_id": operator_user.id,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["completed_operations"] == 1
        assert data[0]["quantity_produced"] == 5
        assert data[0]["entries"][0]["source"] == "operation_completion"
        assert data[0]["entries"][0]["work_order_number"] == "WO-REPORT-COMP"

    def test_employee_time_includes_entries_clocked_out_inside_window(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="REPORT-TIME-001",
            name="Report Time Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-REPORT-TIME",
            name="Report Time Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-REPORT-TIME",
            part_id=part.id,
            quantity_ordered=2,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Crosses Report Window",
            status=OperationStatus.COMPLETE,
            quantity_complete=2,
            completed_by=operator_user.id,
            actual_end=datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=1),
            company_id=1,
        )
        db_session.add(operation)
        db_session.flush()

        entry = TimeEntry(
            user_id=operator_user.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            work_center_id=work_center.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.combine(date.today() - timedelta(days=1), datetime.max.time()),
            clock_out=datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=1),
            duration_hours=1,
            quantity_produced=2,
            company_id=1,
        )
        db_session.add(entry)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/employee-time",
            headers=auth_headers,
            params={
                "start_date": date.today().isoformat(),
                "end_date": date.today().isoformat(),
                "user_id": operator_user.id,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["total_hours"] == 1
        assert data[0]["completed_operations"] == 1
        assert data[0]["entries"][0]["source"] == "time_entry"

    def test_shop_floor_dashboard_recent_completions_uses_completed_operations(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="DASH-COMP-001",
            name="Dashboard Completion Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-DASH-COMP",
            name="Dashboard Completion Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-DASH-COMP",
            part_id=part.id,
            quantity_ordered=4,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Dashboard Completed Op",
            status=OperationStatus.COMPLETE,
            quantity_complete=4,
            completed_by=operator_user.id,
            actual_end=datetime.utcnow(),
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()

        response = client.get("/api/v1/shop-floor/dashboard", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        completions = response.json()["recent_completions"]
        assert any(
            item["work_order_number"] == "WO-DASH-COMP"
            and item["operation_name"] == "Dashboard Completed Op"
            and item["operator_name"] == operator_user.full_name
            for item in completions
        )
