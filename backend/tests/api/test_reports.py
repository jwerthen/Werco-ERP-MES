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

    def test_work_order_costing_returns_200_for_wo_with_part(self, client: TestClient, auth_headers: dict, db_session):
        """Regression: the costing report read the nonexistent ``part.unit_cost``
        column, raising AttributeError -> HTTP 500 and dark-screening Reports.

        This is the exact previously-500ing path: a work order whose part has
        costs. Must now return 200 with a sane shape.
        """
        part = Part(
            part_number="COST-200-001",
            name="Costing 200 Part",
            part_type="manufactured",
            unit_of_measure="each",
            standard_cost=18.0,
            material_cost=10.0,
            labor_cost=5.0,
            overhead_cost=3.0,
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-COST-200",
            part_id=part.id,
            quantity_ordered=7,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/work-order-costing",
            headers=auth_headers,
            params={"work_order_id": work_order.id},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        row = next(item for item in data if item["work_order_number"] == "WO-COST-200")
        assert row["work_order_id"] == work_order.id
        assert row["part_number"] == "COST-200-001"
        assert row["quantity"] == 7
        # Shape sanity: the cost roll-up fields are present and total is consistent.
        for field in ("estimated_material", "actual_material", "actual_labor", "actual_overhead", "actual_total"):
            assert field in row
        assert row["actual_total"] == row["actual_material"] + row["actual_labor"] + row["actual_overhead"]

    def test_work_order_costing_uses_material_component_not_standard_cost(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Pin the no-double-count fix: the material line uses the MATERIAL
        component (``part.material_cost``), NOT the fully-loaded
        ``standard_cost`` (which already bundles labor + overhead). Using
        standard_cost here would double-count labor/overhead, since the report
        adds those separately.
        """
        part = Part(
            part_number="COST-MAT-001",
            name="Material Component Part",
            part_type="manufactured",
            unit_of_measure="each",
            # Distinct values so material vs standard can't be confused.
            material_cost=10.0,
            labor_cost=5.0,
            overhead_cost=3.0,
            standard_cost=18.0,
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.flush()

        quantity = 7
        work_order = WorkOrder(
            work_order_number="WO-COST-MAT",
            part_id=part.id,
            quantity_ordered=quantity,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.commit()

        response = client.get(
            "/api/v1/reports/work-order-costing",
            headers=auth_headers,
            params={"work_order_id": work_order.id},
        )

        assert response.status_code == status.HTTP_200_OK
        row = next(item for item in response.json() if item["work_order_number"] == "WO-COST-MAT")

        expected_material = part.material_cost * quantity  # 10 * 7 = 70
        double_counted = part.standard_cost * quantity  # 18 * 7 = 126 (the bug)

        assert row["actual_material"] == expected_material
        assert row["estimated_material"] == expected_material
        assert row["actual_material"] != double_counted

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


@pytest.mark.unit
def test_part_model_has_no_unit_cost_column():
    """Regression guard for the costing-report 500.

    The costing report used to read ``part.unit_cost`` -- a column that never
    existed on ``Part`` -- which raised AttributeError -> HTTP 500. The Part
    cost model is {standard_cost, material_cost, labor_cost, overhead_cost}.
    A future edit that reintroduces ``part.unit_cost`` should fail here.
    """
    assert not hasattr(Part, "unit_cost")
    cost_columns = {c.name for c in Part.__table__.columns if c.name.endswith("_cost")}
    assert cost_columns == {"standard_cost", "material_cost", "labor_cost", "overhead_cost"}
