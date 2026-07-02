from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrdersAPI:
    """Test work orders API endpoints."""

    def test_list_work_orders_empty(self, client: TestClient, auth_headers: dict):
        """Test listing work orders when none exist."""
        response = client.get("/api/v1/work-orders/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 0

    def test_list_work_orders(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test listing work orders with existing data."""
        response = client.get("/api/v1/work-orders/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["work_order_number"] == test_work_order.work_order_number

    def test_work_order_list_reports_operation_progress_for_component_ops(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        part = Part(
            part_number="WO-PROG-ASM",
            name="Progress Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-PROG",
            name="Progress Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-PROG-001",
            part_id=part.id,
            quantity_ordered=1,
            quantity_complete=0,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        db_session.add_all(
            [
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=2,
                    sequence=10,
                    operation_number="Op 10",
                    name="Completed Component Cut",
                    status=OperationStatus.COMPLETE,
                    quantity_complete=2,
                    actual_end=datetime.utcnow(),
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=1,
                    sequence=20,
                    operation_number="Op 20",
                    name="Pending Component Cut",
                    status=OperationStatus.PENDING,
                    quantity_complete=0,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        response = client.get("/api/v1/work-orders/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        item = next(row for row in response.json() if row["work_order_number"] == "WO-PROG-001")
        assert item["quantity_complete"] == 0
        assert item["operation_count"] == 2
        assert item["operations_complete"] == 1
        assert item["operation_progress_percent"] == 50.0

    def test_work_order_progress_uses_historical_matching_completion(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="WO-PROG-HIST",
            name="Historical Progress Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-PROG-HIST",
            name="Historical Progress Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-PROG-HIST-001",
            part_id=part.id,
            quantity_ordered=1,
            quantity_complete=0,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        db_session.add_all(
            [
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=2,
                    sequence=10,
                    operation_number="Op 10",
                    name="05883 - Cut CNC 05883",
                    status=OperationStatus.COMPLETE,
                    quantity_complete=2,
                    actual_end=datetime.utcnow(),
                    completed_by=operator_user.id,
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=2,
                    sequence=10,
                    operation_number="Op 10",
                    name="05883 - Cut CNC 05883",
                    status=OperationStatus.PENDING,
                    quantity_complete=0,
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=1,
                    sequence=20,
                    operation_number="Op 20",
                    name="05884 - Cut CNC 05884",
                    status=OperationStatus.PENDING,
                    quantity_complete=0,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        response = client.get("/api/v1/work-orders/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        item = next(row for row in response.json() if row["work_order_number"] == "WO-PROG-HIST-001")
        assert item["operation_count"] == 2
        assert item["operations_complete"] == 1
        assert item["operation_progress_percent"] == 50.0

    def test_preview_laser_nest_package_from_folder(self, client: TestClient, auth_headers: dict, db_session, tmp_path):
        part = Part(
            part_number="ASM-LASER-PREVIEW",
            name="Laser Preview Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add(part)
        db_session.flush()
        work_order = WorkOrder(
            work_order_number="WO-LASER-PREVIEW",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.commit()

        package_dir = tmp_path / "ermaksan"
        package_dir.mkdir()
        (package_dir / "NEST-A_A36_10ga_60x120_QTY3.nc").write_text("M30")
        (package_dir / "NEST-B_304SS_0.25in_48x96_x2.tap").write_text("M30")

        response = client.post(
            f"/api/v1/work-orders/{work_order.id}/laser-nest-packages/preview",
            headers=auth_headers,
            data={"source_path": str(package_dir)},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["nest_count"] == 2
        assert data["total_planned_runs"] == 5
        assert {nest["planned_runs"] for nest in data["nests"]} == {2, 3}

    def test_import_laser_nest_package_creates_child_wo_and_run_tasks(
        self, client: TestClient, auth_headers: dict, db_session, tmp_path
    ):
        part = Part(
            part_number="ASM-LASER-IMPORT",
            name="Laser Import Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        laser_wc = WorkCenter(
            code="LASER-IMPORT",
            name="Laser Import",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, laser_wc])
        db_session.flush()
        parent = WorkOrder(
            work_order_number="WO-LASER-PARENT",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=1,
        )
        db_session.add(parent)
        db_session.commit()

        package_dir = tmp_path / "import-package"
        package_dir.mkdir()
        (package_dir / "NEST-A_A36_10ga_60x120_QTY3.nc").write_text("M30")
        (package_dir / "NEST-B_304SS_0.25in_48x96_x2.tap").write_text("M30")

        response = client.post(
            f"/api/v1/work-orders/{parent.id}/laser-nest-packages/import",
            headers=auth_headers,
            data={"source_path": str(package_dir), "work_center_id": str(laser_wc.id)},
        )

        assert response.status_code == status.HTTP_200_OK
        child = response.json()["child_work_order"]
        assert child["parent_work_order_id"] == parent.id
        assert child["work_order_type"] == "laser_cutting"
        assert child["quantity_ordered"] == 5
        assert len(child["operations"]) == 2

        refreshed_child = db_session.get(WorkOrder, child["id"])
        assert refreshed_child.parent_work_order_id == parent.id
        assert refreshed_child.work_order_type == "laser_cutting"

        package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=child["id"]).one()
        nests = db_session.query(LaserNest).filter_by(package_id=package.id).order_by(LaserNest.planned_runs).all()
        assert [nest.planned_runs for nest in nests] == [2, 3]
        assert db_session.query(WorkOrderOperation).filter_by(work_order_id=child["id"]).count() == 2
        assert {op.component_quantity for op in refreshed_child.operations} == {2, 3}

    def test_shop_floor_production_updates_laser_nest_completed_runs(
        self, client: TestClient, auth_headers: dict, db_session, tmp_path
    ):
        part = Part(
            part_number="ASM-LASER-PROD",
            name="Laser Production Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        laser_wc = WorkCenter(
            code="LASER-PROD",
            name="Laser Production",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, laser_wc])
        db_session.flush()
        parent = WorkOrder(
            work_order_number="WO-LASER-PROD-PARENT",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=3,
            company_id=1,
        )
        db_session.add(parent)
        db_session.commit()

        package_dir = tmp_path / "prod-package"
        package_dir.mkdir()
        (package_dir / "NEST-A_A36_10ga_60x120_QTY3.nc").write_text("M30")

        import_response = client.post(
            f"/api/v1/work-orders/{parent.id}/laser-nest-packages/import",
            headers=auth_headers,
            data={"source_path": str(package_dir), "work_center_id": str(laser_wc.id)},
        )
        assert import_response.status_code == status.HTTP_200_OK
        child = import_response.json()["child_work_order"]
        operation = child["operations"][0]

        clock_in_response = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=auth_headers,
            json={
                "work_order_id": child["id"],
                "operation_id": operation["id"],
                "work_center_id": laser_wc.id,
                "entry_type": "run",
            },
        )
        assert clock_in_response.status_code == status.HTTP_200_OK

        production_response = client.post(
            f"/api/v1/shop-floor/operations/{operation['id']}/production",
            headers=auth_headers,
            json={"quantity_complete_delta": 1, "quantity_scrapped_delta": 0},
        )
        assert production_response.status_code == status.HTTP_200_OK

        refreshed_nest = db_session.query(LaserNest).filter_by(work_order_operation_id=operation["id"]).one()
        refreshed_operation = db_session.get(WorkOrderOperation, operation["id"])
        assert refreshed_operation.quantity_complete == 1
        assert refreshed_operation.status == OperationStatus.IN_PROGRESS
        assert refreshed_nest.completed_runs == 1
        assert refreshed_nest.remaining_runs == 2

    def test_work_order_progress_matches_regenerated_slot_when_name_changes(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="WO-PROG-SLOT",
            name="Slot Progress Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-PROG-SLOT",
            name="Slot Progress Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-PROG-SLOT-001",
            part_id=part.id,
            quantity_ordered=1,
            quantity_complete=0,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        db_session.add_all(
            [
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=1,
                    sequence=10,
                    operation_number="Op 10",
                    name="05883 - Cut CNC 05883",
                    status=OperationStatus.COMPLETE,
                    quantity_complete=1,
                    actual_end=datetime.utcnow(),
                    completed_by=operator_user.id,
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=1,
                    sequence=10,
                    operation_number="Op 10",
                    name="Op 10 - 05883 - Cut CNC 05883",
                    status=OperationStatus.PENDING,
                    quantity_complete=0,
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    component_part_id=part.id,
                    component_quantity=1,
                    sequence=20,
                    operation_number="Op 20",
                    name="05884 - Cut CNC 05884",
                    status=OperationStatus.PENDING,
                    quantity_complete=0,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        response = client.get("/api/v1/work-orders/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        item = next(row for row in response.json() if row["work_order_number"] == "WO-PROG-SLOT-001")
        assert item["operation_count"] == 2
        assert item["operations_complete"] == 1
        assert item["operation_progress_percent"] == 50.0

    def test_work_order_progress_reconciles_time_entry_production(
        self, client: TestClient, auth_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="WO-PROG-TIME",
            name="Time Entry Progress Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-PROG-TIME",
            name="Time Entry Progress Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-PROG-TIME-001",
            part_id=part.id,
            quantity_ordered=1,
            quantity_complete=0,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        completed_operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="Cut From Time Entry",
            status=OperationStatus.PENDING,
            quantity_complete=0,
            company_id=1,
        )
        pending_operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=20,
            operation_number="Op 20",
            name="Bend Later",
            status=OperationStatus.PENDING,
            quantity_complete=0,
            company_id=1,
        )
        db_session.add_all([completed_operation, pending_operation])
        db_session.flush()
        db_session.add(
            TimeEntry(
                user_id=operator_user.id,
                work_order_id=work_order.id,
                operation_id=completed_operation.id,
                work_center_id=work_center.id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime(2026, 5, 1, 13, 0, 0),
                clock_out=datetime(2026, 5, 1, 13, 10, 0),
                duration_hours=1 / 6,
                quantity_produced=1,
                company_id=1,
            )
        )
        db_session.commit()

        response = client.get("/api/v1/work-orders/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        item = next(row for row in response.json() if row["work_order_number"] == "WO-PROG-TIME-001")
        assert item["operation_count"] == 2
        assert item["operations_complete"] == 1
        assert item["operation_progress_percent"] == 50.0

        db_session.refresh(completed_operation)
        assert completed_operation.status == OperationStatus.COMPLETE
        assert completed_operation.quantity_complete == 1

        shop_floor_response = client.get("/api/v1/shop-floor/operations", headers=auth_headers)
        assert shop_floor_response.status_code == status.HTTP_200_OK
        returned_ids = {operation["id"] for operation in shop_floor_response.json()["operations"]}
        assert completed_operation.id not in returned_ids
        assert pending_operation.id in returned_ids

    def test_create_work_order(self, client: TestClient, auth_headers: dict, sample_work_order_data: dict):
        """Test creating a new work order."""
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["work_order_number"].startswith("WO-")
        assert data["customer_name"] == sample_work_order_data["customer_name"]
        assert float(data["quantity_ordered"]) == sample_work_order_data["quantity_ordered"]

    def test_auto_routing_copies_routing_instructions_to_work_order(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        part = Part(
            part_number="WO-INSTR-001",
            name="Instruction Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-INSTR-001",
            name="Instruction Center",
            work_center_type="assembly",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        routing = Routing(part_id=part.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(routing)
        db_session.flush()
        db_session.add(
            RoutingOperation(
                routing_id=routing.id,
                sequence=10,
                operation_number="Op 10",
                name="Assemble",
                description="Build the assembly",
                work_center_id=work_center.id,
                setup_hours=0.25,
                run_hours_per_unit=0.5,
                setup_instructions="Stage fixtures and verify revision.",
                work_instructions="Assemble parts per drawing notes.",
                is_inspection_point=True,
                is_active=True,
                company_id=1,
            )
        )
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": part.id, "quantity_ordered": 2, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED
        operation = response.json()["operations"][0]
        assert operation["setup_instructions"] == "Stage fixtures and verify revision."
        assert operation["run_instructions"] == "Assemble parts per drawing notes."
        assert operation["requires_inspection"] is True

    def test_create_work_order_unauthorized(self, client: TestClient, sample_work_order_data: dict):
        """Test creating a work order without authentication."""
        response = client.post("/api/v1/work-orders/", json=sample_work_order_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_work_order_by_id(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test retrieving a single work order by ID."""
        response = client.get(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_work_order.id
        assert data["work_order_number"] == test_work_order.work_order_number

    def test_get_work_order_serializes_datetimes_as_utc_z(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder, db_session
    ):
        """Work order detail timestamps serialize as UTC ISO-8601 with a trailing 'Z'.

        Layer 2 timezone-consistency fix: naive-UTC datetimes are emitted as
        ``...Z`` (via ``UTCModel`` / ``to_utc_iso``) rather than a Central offset, so
        the frontend can parse them unambiguously as UTC.
        """
        operation = test_work_order.operations[0]
        test_work_order.actual_start = datetime(2026, 5, 1, 18, 17, 0)
        operation.actual_start = datetime(2026, 5, 1, 18, 17, 0)
        db_session.commit()

        response = client.get(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["actual_start"] == "2026-05-01T18:17:00Z"
        assert data["operations"][0]["actual_start"] == "2026-05-01T18:17:00Z"

    def test_get_work_order_not_found(self, client: TestClient, auth_headers: dict):
        """Test retrieving a non-existent work order."""
        response = client.get("/api/v1/work-orders/99999", headers=auth_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_work_order(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test updating an existing work order."""
        update_data = {"version": 0, "status": "released", "priority": 1}
        response = client.put(
            f"/api/v1/work-orders/{test_work_order.id}",
            headers=auth_headers,
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "released"
        assert data["priority"] == 1

    def test_update_work_order_priority_quick(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Priority can be changed quickly without full work order payload."""
        response = client.put(
            f"/api/v1/work-orders/{test_work_order.id}/priority",
            headers=auth_headers,
            json={"priority": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["work_order_id"] == test_work_order.id
        assert data["priority"] == 1

        wo_response = client.get(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)
        assert wo_response.status_code == status.HTTP_200_OK
        assert wo_response.json()["priority"] == 1

    def test_update_work_order_priority_with_reason_logged(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder, db_session
    ):
        reason = "Customer expedite request"
        response = client.put(
            f"/api/v1/work-orders/{test_work_order.id}/priority",
            headers=auth_headers,
            json={"priority": 1, "reason": reason},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["priority"] == 1
        assert data["reason"] == reason

        audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert reason in (audit.description or "")
        assert (audit.extra_data or {}).get("priority_reason") == reason

    def test_update_work_order_priority_forbidden_for_operator(
        self, client: TestClient, operator_headers: dict, test_work_order: WorkOrder
    ):
        response = client.put(
            f"/api/v1/work-orders/{test_work_order.id}/priority",
            headers=operator_headers,
            json={"priority": 1},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_work_order(self, client: TestClient, admin_headers: dict, test_work_order: WorkOrder, db_session):
        """Test deleting a work order (admin only)."""
        response = client.delete(f"/api/v1/work-orders/{test_work_order.id}", headers=admin_headers)
        assert response.status_code == status.HTTP_204_NO_CONTENT
        db_session.refresh(test_work_order)
        assert test_work_order.is_deleted is True

    def test_delete_released_work_order_soft_deletes(
        self, client: TestClient, admin_headers: dict, test_work_order: WorkOrder, db_session
    ):
        """Current work orders should be removable through soft delete."""
        test_work_order.status = WorkOrderStatus.RELEASED
        db_session.commit()

        response = client.delete(f"/api/v1/work-orders/{test_work_order.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_204_NO_CONTENT
        db_session.refresh(test_work_order)
        assert test_work_order.status == WorkOrderStatus.RELEASED
        assert test_work_order.is_deleted is True
        assert test_work_order.deleted_at is not None

    def test_delete_work_order_forbidden(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test that non-admin cannot delete work orders."""
        response = client.delete(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_release_work_order(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test releasing a work order."""
        response = client.post(f"/api/v1/work-orders/{test_work_order.id}/release", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "released"

    def test_search_work_orders(self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder):
        """Test searching work orders by customer name."""
        response = client.get(
            f"/api/v1/work-orders/?search={test_work_order.customer_name}",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1
        assert test_work_order.customer_name in data[0]["customer_name"]

    def test_get_work_order_includes_operator_tracking_fields(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Started/completed operator IDs should be visible on work order operations."""
        work_order_response = client.get(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)
        assert work_order_response.status_code == status.HTTP_200_OK
        operation_id = work_order_response.json()["operations"][0]["id"]

        start_response = client.post(
            f"/api/v1/work-orders/operations/{operation_id}/start",
            headers=auth_headers,
        )
        assert start_response.status_code == status.HTTP_200_OK

        complete_response = client.post(
            f"/api/v1/work-orders/operations/{operation_id}/complete",
            headers=auth_headers,
            params={
                "quantity_complete": test_work_order.quantity_ordered,
                "quantity_scrapped": 0,
            },
        )
        assert complete_response.status_code == status.HTTP_200_OK

        refreshed_work_order = client.get(f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers)
        assert refreshed_work_order.status_code == status.HTTP_200_OK
        operation = refreshed_work_order.json()["operations"][0]
        assert operation["started_by"] is not None
        assert operation["completed_by"] is not None

    def test_work_order_operation_completion_uses_component_target(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        part = Part(
            part_number="WO-COMPLETE-COMPONENT",
            name="Component Target Parent",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-COMPLETE-COMPONENT",
            name="Component Target Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-COMPLETE-COMPONENT",
            part_id=part.id,
            quantity_ordered=3,
            status=WorkOrderStatus.RELEASED,
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
            name="Make Component",
            component_part_id=part.id,
            component_quantity=6,
            status=OperationStatus.READY,
            company_id=1,
        )
        db_session.add(operation)
        db_session.commit()

        partial_response = client.post(
            f"/api/v1/work-orders/operations/{operation.id}/complete",
            headers=auth_headers,
            params={"quantity_complete": 3, "quantity_scrapped": 0},
        )
        assert partial_response.status_code == status.HTTP_200_OK
        assert partial_response.json()["message"] == "Progress updated"

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation.id)
        refreshed_work_order = db_session.get(WorkOrder, work_order.id)
        assert refreshed_operation.status == OperationStatus.IN_PROGRESS
        assert refreshed_work_order.status == WorkOrderStatus.IN_PROGRESS
        assert refreshed_work_order.quantity_complete == 0

        complete_response = client.post(
            f"/api/v1/work-orders/operations/{operation.id}/complete",
            headers=auth_headers,
            params={"quantity_complete": 6, "quantity_scrapped": 0},
        )
        assert complete_response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation.id)
        refreshed_work_order = db_session.get(WorkOrder, work_order.id)
        assert refreshed_operation.status == OperationStatus.COMPLETE
        assert refreshed_work_order.status == WorkOrderStatus.COMPLETE
        assert refreshed_work_order.quantity_complete == 3

    def test_assembly_work_order_uses_bom_component_and_assembly_routings(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Assembly auto-routing should include released BOM component routings."""
        assembly = Part(
            part_number="ASM-ORDER-001",
            name="Assembly Ordered",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component_one = Part(
            part_number="CMP-ORDER-001",
            name="Component One",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component_two = Part(
            part_number="CMP-ORDER-002",
            name="Component Two",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        nested_component = Part(
            part_number="CMP-ORDER-003",
            name="Nested Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component_one, component_two, nested_component])
        db_session.flush()

        laser_wc = WorkCenter(
            code="WC-LASER-SEQ",
            name="Laser Seq",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        bend_wc = WorkCenter(
            code="WC-BEND-SEQ",
            name="Bend Seq",
            work_center_type="press",
            is_active=True,
            company_id=1,
        )
        weld_wc = WorkCenter(
            code="WC-WELD-SEQ",
            name="Weld Seq",
            work_center_type="weld",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([laser_wc, bend_wc, weld_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(bom)
        db_session.flush()
        nested_bom = BOM(part_id=component_two.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(nested_bom)
        db_session.flush()
        db_session.add_all(
            [
                BOMItem(
                    bom_id=bom.id,
                    component_part_id=component_one.id,
                    item_number=10,
                    quantity=3,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                    company_id=1,
                ),
                BOMItem(
                    bom_id=bom.id,
                    component_part_id=component_two.id,
                    item_number=20,
                    quantity=1,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                    company_id=1,
                ),
                BOMItem(
                    bom_id=nested_bom.id,
                    component_part_id=nested_component.id,
                    item_number=10,
                    quantity=2,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                    company_id=1,
                ),
            ]
        )

        routing_one = Routing(part_id=component_one.id, revision="A", status="released", is_active=True, company_id=1)
        routing_two = Routing(part_id=component_two.id, revision="A", status="released", is_active=True, company_id=1)
        routing_nested = Routing(
            part_id=nested_component.id, revision="A", status="released", is_active=True, company_id=1
        )
        assembly_routing = Routing(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add_all([routing_one, routing_two, routing_nested, assembly_routing])
        db_session.flush()

        db_session.add_all(
            [
                RoutingOperation(
                    routing_id=routing_one.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Bend One",
                    work_center_id=bend_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=routing_one.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Weld One",
                    work_center_id=weld_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=routing_two.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Laser Two",
                    work_center_id=laser_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=routing_nested.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Bend Nested",
                    work_center_id=bend_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Assemble Frame",
                    work_center_id=weld_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.2,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Final Inspection",
                    work_center_id=laser_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.05,
                    is_active=True,
                    is_inspection_point=True,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        preview_response = client.get(
            f"/api/v1/work-orders/preview-operations/{assembly.id}",
            headers=auth_headers,
            params={"quantity": 1},
        )
        assert preview_response.status_code == status.HTTP_200_OK
        preview_names = [op["name"] for op in preview_response.json()["operations_preview"]]
        assert preview_names == [
            f"{component_one.part_number} - Bend One",
            f"{component_one.part_number} - Weld One",
            f"{component_two.part_number} - Laser Two",
            f"{nested_component.part_number} - Bend Nested",
            "Assemble Frame",
            "Final Inspection",
        ]
        preview_component_quantities = [
            op["component_quantity"] for op in preview_response.json()["operations_preview"] if op["component_part_id"]
        ]
        assert preview_component_quantities == [3, 3, 1, 2]

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": assembly.id, "quantity_ordered": 1, "priority": 5},
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()

        operation_names = [op["name"] for op in data["operations"]]
        assert operation_names == [
            f"{component_one.part_number} - Bend One",
            f"{component_one.part_number} - Weld One",
            f"{component_two.part_number} - Laser Two",
            f"{nested_component.part_number} - Bend Nested",
            "Assemble Frame",
            "Final Inspection",
        ]
        component_operations = data["operations"][:4]
        assert [op["component_part_id"] for op in component_operations] == [
            component_one.id,
            component_one.id,
            component_two.id,
            nested_component.id,
        ]
        assert [op["component_quantity"] for op in component_operations] == [3, 3, 1, 2]

    def test_work_order_manual_preview_operations_preserve_component_quantities(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Previewed component rows submitted as explicit ops should keep BOM qty metadata."""
        assembly = Part(
            part_number="ASM-MANUAL-QTY",
            name="Manual Qty Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-MANUAL-QTY",
            name="Manual Qty Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-MANUAL-QTY",
            name="Manual Qty WC",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component, work_center])
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={
                "part_id": assembly.id,
                "quantity_ordered": 2,
                "priority": 5,
                "operations": [
                    {
                        "sequence": 10,
                        "operation_number": "Op 10",
                        "name": f"{component.part_number} - Cut",
                        "work_center_id": work_center.id,
                        "setup_time_hours": 0.1,
                        "run_time_hours": 1.2,
                        "run_time_per_piece": 0.2,
                        "component_part_id": component.id,
                        "component_quantity": 6,
                        "operation_group": "LASER",
                    }
                ],
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        operation = response.json()["operations"][0]
        assert operation["component_part_id"] == component.id
        assert operation["component_part_number"] == component.part_number
        assert operation["component_quantity"] == 6
        assert operation["operation_group"] == "LASER"

    def test_work_order_detail_reconciles_router_quantities_from_bom(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Existing router rows with part-number-prefixed names should display BOM required qty."""
        assembly = Part(
            part_number="ASM-RECON-QTY",
            name="Reconcile Qty Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-RECON-QTY",
            name="Reconcile Qty Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-RECON-QTY",
            name="Reconcile Qty WC",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component, work_center])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(bom)
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=4,
                item_type="make",
                line_type="component",
                unit_of_measure="each",
                company_id=1,
            )
        )
        work_order = WorkOrder(
            work_order_number="WO-RECON-QTY",
            part_id=assembly.id,
            quantity_ordered=2,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()
        db_session.add(
            WorkOrderOperation(
                work_order_id=work_order.id,
                work_center_id=work_center.id,
                sequence=10,
                operation_number="Op 10",
                name=f"{component.part_number} - Cut",
                status=OperationStatus.PENDING,
                component_quantity=1,
                company_id=1,
            )
        )
        db_session.commit()

        response = client.get(f"/api/v1/work-orders/{work_order.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        operation = response.json()["operations"][0]
        assert operation["component_part_id"] == component.id
        assert operation["component_part_number"] == component.part_number
        assert operation["component_quantity"] == 8

    def test_assembly_work_order_places_final_inspection_last(self, client: TestClient, auth_headers: dict, db_session):
        """Final inspection should be moved to the last assembly stage."""
        assembly = Part(
            part_number="ASM-FINAL-001",
            name="Assembly Final",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-FINAL-001",
            name="Component Final",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        machine_wc = WorkCenter(
            code="WC-MACH-FINAL",
            name="Machine Final",
            work_center_type="machine",
            is_active=True,
            company_id=1,
        )
        assembly_wc = WorkCenter(
            code="WC-ASM-FINAL",
            name="Assembly Final",
            work_center_type="assembly",
            is_active=True,
            company_id=1,
        )
        inspect_wc = WorkCenter(
            code="WC-INSP-FINAL",
            name="Final Inspection",
            work_center_type="inspection",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([machine_wc, assembly_wc, inspect_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(bom)
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=1,
                item_type="make",
                line_type="component",
                unit_of_measure="each",
                company_id=1,
            )
        )

        component_routing = Routing(part_id=component.id, revision="A", status="released", is_active=True, company_id=1)
        assembly_routing = Routing(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add_all([component_routing, assembly_routing])
        db_session.flush()

        db_session.add_all(
            [
                RoutingOperation(
                    routing_id=component_routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Machine Component",
                    work_center_id=machine_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Final Inspection",
                    work_center_id=inspect_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.05,
                    is_active=True,
                    is_inspection_point=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Build Final Assembly",
                    work_center_id=assembly_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.2,
                    is_active=True,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": assembly.id, "quantity_ordered": 1, "priority": 5},
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()

        operation_names = [op["name"] for op in data["operations"]]
        assert operation_names == [
            f"{component.part_number} - Machine Component",
            "Build Final Assembly",
            "Final Inspection",
        ]
        operation_groups = [op["operation_group"] for op in data["operations"]]
        assert operation_groups == ["MACHINE", "ASSEMBLY", "INSPECT"]

    def test_work_order_uses_bom_component_routings_for_manufactured_part_with_bom(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Parts typed manufactured should still expand BOM component routings when a BOM exists."""
        parent = Part(
            part_number="MFG-BOM-001",
            name="Manufactured Part With BOM",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-MFG-BOM-001",
            name="Manufactured BOM Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-MFG-BOM",
            name="Manufactured BOM Work Center",
            work_center_type="machine",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([parent, component, work_center])
        db_session.flush()

        bom = BOM(part_id=parent.id, revision="A", status="released", is_active=True, company_id=1)
        routing = Routing(part_id=component.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add_all([bom, routing])
        db_session.flush()
        db_session.add_all(
            [
                BOMItem(
                    bom_id=bom.id,
                    component_part_id=component.id,
                    item_number=10,
                    quantity=2,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Machine BOM Component",
                    work_center_id=work_center.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        preview_response = client.get(
            f"/api/v1/work-orders/preview-operations/{parent.id}",
            headers=auth_headers,
            params={"quantity": 3},
        )
        assert preview_response.status_code == status.HTTP_200_OK
        preview = preview_response.json()
        assert preview["bom_found"] is True
        assert preview["operations_preview"][0]["name"] == f"{component.part_number} - Machine BOM Component"
        assert preview["operations_preview"][0]["component_quantity"] == 6

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": parent.id, "quantity_ordered": 3, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED
        operation = response.json()["operations"][0]
        assert operation["name"] == f"{component.part_number} - Machine BOM Component"
        assert operation["component_part_id"] == component.id
        assert operation["component_quantity"] == 6

        release_response = client.post(f"/api/v1/work-orders/{response.json()['id']}/release", headers=auth_headers)
        assert release_response.status_code == status.HTTP_200_OK

        shop_floor_response = client.get(
            "/api/v1/shop-floor/operations",
            headers=auth_headers,
            params={"work_center_id": work_center.id},
        )
        assert shop_floor_response.status_code == status.HTTP_200_OK
        shop_floor_operation = shop_floor_response.json()["operations"][0]
        assert shop_floor_operation["id"] == operation["id"]
        assert shop_floor_operation["quantity_ordered"] == 6
        assert shop_floor_operation["work_order_quantity_ordered"] == 3
        assert shop_floor_operation["component_quantity"] == 6

        partial_response = client.post(
            f"/api/v1/shop-floor/operations/{operation['id']}/complete",
            headers=auth_headers,
            json={"quantity_complete": 4},
        )
        assert partial_response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation["id"])
        refreshed_work_order = db_session.get(WorkOrder, response.json()["id"])
        assert refreshed_operation.quantity_complete == 4
        assert refreshed_operation.status == OperationStatus.IN_PROGRESS
        assert refreshed_work_order.quantity_complete == 0

    def test_shop_floor_work_center_counts_match_operation_list(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        part = Part(
            part_number="SHOP-COUNT-001",
            name="Shop Count Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-SHOP-COUNT",
            name="Shop Count Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-SHOP-COUNT-001",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()
        db_session.add_all(
            [
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Ready Shop Operation",
                    status=OperationStatus.READY,
                    company_id=1,
                ),
                WorkOrderOperation(
                    work_order_id=work_order.id,
                    work_center_id=work_center.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Active Shop Operation",
                    status=OperationStatus.IN_PROGRESS,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        dashboard_response = client.get("/api/v1/shop-floor/dashboard", headers=auth_headers)
        operations_response = client.get(
            "/api/v1/shop-floor/operations",
            headers=auth_headers,
            params={"work_center_id": work_center.id},
        )
        queue_response = client.get(
            f"/api/v1/shop-floor/work-center-queue/{work_center.id}",
            headers=auth_headers,
        )

        assert dashboard_response.status_code == status.HTTP_200_OK
        assert operations_response.status_code == status.HTTP_200_OK
        assert queue_response.status_code == status.HTTP_200_OK

        center = next(item for item in dashboard_response.json()["work_centers"] if item["id"] == work_center.id)
        assert center["queued_operations"] == 1
        assert center["active_operations"] == 1
        assert operations_response.json()["total"] == 2
        assert len(queue_response.json()["queue"]) == 2

    def test_assembly_work_order_blocks_out_of_sequence_start(self, client: TestClient, auth_headers: dict, db_session):
        """Operators cannot start a later operation before predecessors are complete."""
        assembly = Part(
            part_number="ASM-SEQ-001",
            name="Assembly Sequence",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-SEQ-001",
            name="Component Sequence",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        cut_wc = WorkCenter(
            code="WC-CUT-SEQ",
            name="Cut Seq",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        weld_wc = WorkCenter(
            code="WC-WELD-SEQ2",
            name="Weld Seq",
            work_center_type="weld",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([cut_wc, weld_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(bom)
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=1,
                item_type="make",
                line_type="component",
                unit_of_measure="each",
                company_id=1,
            )
        )

        assembly_routing = Routing(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=1)
        db_session.add(assembly_routing)
        db_session.flush()
        db_session.add_all(
            [
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Cut Assembly",
                    work_center_id=cut_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
                RoutingOperation(
                    routing_id=assembly_routing.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Weld Assembly",
                    work_center_id=weld_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                    company_id=1,
                ),
            ]
        )
        db_session.commit()

        create_response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": assembly.id, "quantity_ordered": 1, "priority": 5},
        )
        assert create_response.status_code == status.HTTP_201_CREATED
        work_order_id = create_response.json()["id"]
        operations = sorted(create_response.json()["operations"], key=lambda op: op["sequence"])
        second_operation_id = operations[1]["id"]

        release_response = client.post(f"/api/v1/work-orders/{work_order_id}/release", headers=auth_headers)
        assert release_response.status_code == status.HTTP_200_OK

        start_response = client.put(
            f"/api/v1/shop-floor/operations/{second_operation_id}/start",
            headers=auth_headers,
        )
        assert start_response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Previous operations must be completed first" in start_response.json()["detail"]

    def test_shop_floor_allows_out_of_sequence_start_within_same_work_center(
        self, client: TestClient, operator_headers: dict, db_session
    ):
        """Operators may choose any operation when prior steps are in the same work center."""
        part = Part(
            part_number="SHOP-SAME-WC-001",
            name="Same WC Sequence",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-SAME-SEQ",
            name="Same Sequence Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-SAME-WC-001",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            company_id=1,
        )
        db_session.add(work_order)
        db_session.flush()

        first_op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=10,
            operation_number="Op 10",
            name="First Same WC Operation",
            status=OperationStatus.READY,
            company_id=1,
        )
        second_op = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=20,
            operation_number="Op 20",
            name="Second Same WC Operation",
            status=OperationStatus.PENDING,
            company_id=1,
        )
        db_session.add_all([first_op, second_op])
        db_session.commit()

        operations_response = client.get(
            "/api/v1/shop-floor/operations",
            headers=operator_headers,
            params={"work_center_id": work_center.id},
        )
        assert operations_response.status_code == status.HTTP_200_OK
        second_shop_op = next(op for op in operations_response.json()["operations"] if op["id"] == second_op.id)
        assert second_shop_op["can_check_in"] is True
        assert second_shop_op["blocked_by_previous_operations"] is False

        start_response = client.put(
            f"/api/v1/shop-floor/operations/{second_op.id}/start",
            headers=operator_headers,
        )

        assert start_response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, second_op.id).status == OperationStatus.IN_PROGRESS

    def test_shop_floor_reports_production_without_clocking_operator_out(
        self, client: TestClient, operator_headers: dict, operator_user: User, db_session
    ):
        part = Part(
            part_number="SHOP-PROD-001",
            name="Shop Production Part",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        work_center = WorkCenter(
            code="WC-PROD-001",
            name="Production Tracking Work Center",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([part, work_center])
        db_session.flush()

        work_order = WorkOrder(
            work_order_number="WO-PROD-001",
            part_id=part.id,
            quantity_ordered=5,
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
            name="Track Production",
            status=OperationStatus.IN_PROGRESS,
            company_id=1,
        )
        db_session.add(operation)
        db_session.flush()

        time_entry = TimeEntry(
            user_id=operator_user.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            work_center_id=work_center.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow(),
            company_id=1,
        )
        db_session.add(time_entry)
        db_session.commit()

        response = client.post(
            f"/api/v1/shop-floor/operations/{operation.id}/production",
            headers=operator_headers,
            json={"quantity_complete_delta": 2, "quantity_scrapped_delta": 1, "scrap_reason": "Material defect"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["operation"]["quantity_complete"] == 2
        assert data["active_time_entry"]["quantity_produced"] == 2
        assert data["active_time_entry"]["quantity_scrapped"] == 1
        assert data["active_time_entry"]["clock_out"] is None

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation.id)
        refreshed_entry = db_session.get(TimeEntry, time_entry.id)
        assert refreshed_operation.quantity_complete == 2
        assert refreshed_operation.quantity_scrapped == 1
        assert refreshed_operation.status == OperationStatus.IN_PROGRESS
        assert refreshed_entry.quantity_produced == 2
        assert refreshed_entry.quantity_scrapped == 1
        assert refreshed_entry.clock_out is None

        target_response = client.post(
            f"/api/v1/shop-floor/operations/{operation.id}/production",
            headers=operator_headers,
            json={"quantity_complete_delta": 3, "quantity_scrapped_delta": 0},
        )
        assert target_response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation.id)
        refreshed_entry = db_session.get(TimeEntry, time_entry.id)
        assert refreshed_operation.quantity_complete == 5
        assert refreshed_operation.status == OperationStatus.IN_PROGRESS
        assert refreshed_operation.actual_end is None
        assert refreshed_entry.quantity_produced == 5
        assert refreshed_entry.clock_out is None

        list_response = client.get("/api/v1/work-orders/", headers=operator_headers)
        assert list_response.status_code == status.HTTP_200_OK
        summary = next(item for item in list_response.json() if item["work_order_number"] == "WO-PROD-001")
        assert summary["operations_complete"] == 0
        assert summary["operation_progress_percent"] == 100.0

        shop_floor_response = client.get("/api/v1/shop-floor/operations", headers=operator_headers)
        assert shop_floor_response.status_code == status.HTTP_200_OK
        returned_operation = next(
            item for item in shop_floor_response.json()["operations"] if item["id"] == operation.id
        )
        assert returned_operation["status"] == "in_progress"
        assert returned_operation["quantity_complete"] == 5

        clock_out_response = client.post(
            f"/api/v1/shop-floor/clock-out/{time_entry.id}",
            headers=operator_headers,
            json={"quantity_produced": 0, "quantity_scrapped": 0},
        )
        assert clock_out_response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        refreshed_operation = db_session.get(WorkOrderOperation, operation.id)
        refreshed_work_order = db_session.get(WorkOrder, work_order.id)
        refreshed_entry = db_session.get(TimeEntry, time_entry.id)
        assert refreshed_operation.quantity_complete == 5
        assert refreshed_operation.status == OperationStatus.COMPLETE
        assert refreshed_operation.actual_end is not None
        assert refreshed_work_order.status == WorkOrderStatus.COMPLETE
        assert refreshed_entry.quantity_produced == 5
        assert refreshed_entry.quantity_scrapped == 1
        assert refreshed_entry.clock_out is not None

        dashboard_response = client.get("/api/v1/shop-floor/dashboard", headers=operator_headers)
        assert dashboard_response.status_code == status.HTTP_200_OK
        assert any(
            item["work_order_number"] == "WO-PROD-001" and item["operation_name"] == "Track Production"
            for item in dashboard_response.json()["recent_completions"]
        )


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrdersValidation:
    """Test work order validation."""

    def test_create_work_order_missing_required_fields(self, client: TestClient, auth_headers: dict):
        """Test creating a work order with missing required fields."""
        invalid_data = {"customer_name": "Test Customer"}
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_work_order_invalid_quantity(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test creating a work order with invalid quantity."""
        invalid_data = {
            "customer_name": "Test Customer",
            "part_id": test_part.id,
            "quantity_ordered": -10,
        }
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_work_order_generates_unique_numbers(
        self, client: TestClient, auth_headers: dict, sample_work_order_data: dict
    ):
        """Test that work order numbers are generated uniquely."""
        response_one = client.post("/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data)
        assert response_one.status_code == status.HTTP_201_CREATED
        wo_number_one = response_one.json()["work_order_number"]

        response_two = client.post("/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data)
        assert response_two.status_code == status.HTTP_201_CREATED
        wo_number_two = response_two.json()["work_order_number"]

        assert wo_number_one != wo_number_two
