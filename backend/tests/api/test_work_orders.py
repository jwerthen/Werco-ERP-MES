import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder


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

    def test_list_work_orders(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test listing work orders with existing data."""
        response = client.get("/api/v1/work-orders/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["work_order_number"] == test_work_order.work_order_number

    def test_create_work_order(
        self, client: TestClient, auth_headers: dict, sample_work_order_data: dict
    ):
        """Test creating a new work order."""
        response = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["work_order_number"].startswith("WO-")
        assert data["customer_name"] == sample_work_order_data["customer_name"]
        assert (
            float(data["quantity_ordered"])
            == sample_work_order_data["quantity_ordered"]
        )

    def test_create_work_order_unauthorized(
        self, client: TestClient, sample_work_order_data: dict
    ):
        """Test creating a work order without authentication."""
        response = client.post("/api/v1/work-orders/", json=sample_work_order_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_work_order_by_id(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test retrieving a single work order by ID."""
        response = client.get(
            f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_work_order.id
        assert data["work_order_number"] == test_work_order.work_order_number

    def test_get_work_order_not_found(self, client: TestClient, auth_headers: dict):
        """Test retrieving a non-existent work order."""
        response = client.get("/api/v1/work-orders/99999", headers=auth_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_work_order(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
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

    def test_delete_work_order(
        self, client: TestClient, admin_headers: dict, test_work_order: WorkOrder
    ):
        """Test deleting a work order (admin only)."""
        response = client.delete(
            f"/api/v1/work-orders/{test_work_order.id}", headers=admin_headers
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_work_order_forbidden(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test that non-admin cannot delete work orders."""
        response = client.delete(
            f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_release_work_order(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test releasing a work order."""
        response = client.post(
            f"/api/v1/work-orders/{test_work_order.id}/release", headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "released"

    def test_search_work_orders(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
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
        work_order_response = client.get(
            f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers
        )
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
            params={"quantity_complete": 1, "quantity_scrapped": 0},
        )
        assert complete_response.status_code == status.HTTP_200_OK

        refreshed_work_order = client.get(
            f"/api/v1/work-orders/{test_work_order.id}", headers=auth_headers
        )
        assert refreshed_work_order.status_code == status.HTTP_200_OK
        operation = refreshed_work_order.json()["operations"][0]
        assert operation["started_by"] is not None
        assert operation["completed_by"] is not None

    def test_assembly_work_order_uses_bom_and_routing_sequence_order(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Assembly auto-routing should follow BOM item and routing sequence order."""
        assembly = Part(
            part_number="ASM-ORDER-001",
            name="Assembly Ordered",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
        )
        component_one = Part(
            part_number="CMP-ORDER-001",
            name="Component One",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
        )
        component_two = Part(
            part_number="CMP-ORDER-002",
            name="Component Two",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
        )
        db_session.add_all([assembly, component_one, component_two])
        db_session.flush()

        laser_wc = WorkCenter(
            code="WC-LASER-SEQ",
            name="Laser Seq",
            work_center_type="laser",
            is_active=True,
        )
        bend_wc = WorkCenter(
            code="WC-BEND-SEQ",
            name="Bend Seq",
            work_center_type="press",
            is_active=True,
        )
        weld_wc = WorkCenter(
            code="WC-WELD-SEQ",
            name="Weld Seq",
            work_center_type="weld",
            is_active=True,
        )
        db_session.add_all([laser_wc, bend_wc, weld_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True)
        db_session.add(bom)
        db_session.flush()
        db_session.add_all(
            [
                BOMItem(
                    bom_id=bom.id,
                    component_part_id=component_one.id,
                    item_number=10,
                    quantity=1,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                ),
                BOMItem(
                    bom_id=bom.id,
                    component_part_id=component_two.id,
                    item_number=20,
                    quantity=1,
                    item_type="make",
                    line_type="component",
                    unit_of_measure="each",
                ),
            ]
        )

        routing_one = Routing(
            part_id=component_one.id, revision="A", status="released", is_active=True
        )
        routing_two = Routing(
            part_id=component_two.id, revision="A", status="released", is_active=True
        )
        db_session.add_all([routing_one, routing_two])
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
            f"{component_one.part_number} - Bend One",
            f"{component_one.part_number} - Weld One",
            f"{component_two.part_number} - Laser Two",
        ]

    def test_assembly_work_order_places_final_inspection_last(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Final inspection should be moved to the last assembly stage."""
        assembly = Part(
            part_number="ASM-FINAL-001",
            name="Assembly Final",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
        )
        component = Part(
            part_number="CMP-FINAL-001",
            name="Component Final",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        machine_wc = WorkCenter(
            code="WC-MACH-FINAL",
            name="Machine Final",
            work_center_type="machine",
            is_active=True,
        )
        assembly_wc = WorkCenter(
            code="WC-ASM-FINAL",
            name="Assembly Final",
            work_center_type="assembly",
            is_active=True,
        )
        inspect_wc = WorkCenter(
            code="WC-INSP-FINAL",
            name="Final Inspection",
            work_center_type="inspection",
            is_active=True,
        )
        db_session.add_all([machine_wc, assembly_wc, inspect_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True)
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
            )
        )

        component_routing = Routing(
            part_id=component.id, revision="A", status="released", is_active=True
        )
        assembly_routing = Routing(
            part_id=assembly.id, revision="A", status="released", is_active=True
        )
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
            "FINAL ASSEMBLY: Build Final Assembly",
            "FINAL INSPECTION: Final Inspection",
        ]
        operation_groups = [op["operation_group"] for op in data["operations"]]
        assert operation_groups == [component.part_number, "ASSEMBLY", "INSPECT"]

    def test_assembly_work_order_blocks_out_of_sequence_start(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        """Operators cannot start a later operation before predecessors are complete."""
        assembly = Part(
            part_number="ASM-SEQ-001",
            name="Assembly Sequence",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
        )
        component = Part(
            part_number="CMP-SEQ-001",
            name="Component Sequence",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        cut_wc = WorkCenter(
            code="WC-CUT-SEQ",
            name="Cut Seq",
            work_center_type="laser",
            is_active=True,
        )
        weld_wc = WorkCenter(
            code="WC-WELD-SEQ2",
            name="Weld Seq",
            work_center_type="weld",
            is_active=True,
        )
        db_session.add_all([cut_wc, weld_wc])
        db_session.flush()

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True)
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
            )
        )

        component_routing = Routing(
            part_id=component.id, revision="A", status="released", is_active=True
        )
        db_session.add(component_routing)
        db_session.flush()
        db_session.add_all(
            [
                RoutingOperation(
                    routing_id=component_routing.id,
                    sequence=10,
                    operation_number="Op 10",
                    name="Cut Component",
                    work_center_id=cut_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
                ),
                RoutingOperation(
                    routing_id=component_routing.id,
                    sequence=20,
                    operation_number="Op 20",
                    name="Weld Component",
                    work_center_id=weld_wc.id,
                    setup_hours=0,
                    run_hours_per_unit=0.1,
                    is_active=True,
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

        release_response = client.post(
            f"/api/v1/work-orders/{work_order_id}/release", headers=auth_headers
        )
        assert release_response.status_code == status.HTTP_200_OK

        start_response = client.put(
            f"/api/v1/shop-floor/operations/{second_operation_id}/start",
            headers=auth_headers,
        )
        assert start_response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Previous operations must be completed first" in start_response.json()["detail"]


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrdersValidation:
    """Test work order validation."""

    def test_create_work_order_missing_required_fields(
        self, client: TestClient, auth_headers: dict
    ):
        """Test creating a work order with missing required fields."""
        invalid_data = {"customer_name": "Test Customer"}
        response = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=invalid_data
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_work_order_invalid_quantity(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test creating a work order with invalid quantity."""
        invalid_data = {
            "customer_name": "Test Customer",
            "part_id": test_part.id,
            "quantity_ordered": -10,
        }
        response = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=invalid_data
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_work_order_generates_unique_numbers(
        self, client: TestClient, auth_headers: dict, sample_work_order_data: dict
    ):
        """Test that work order numbers are generated uniquely."""
        response_one = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data
        )
        assert response_one.status_code == status.HTTP_201_CREATED
        wo_number_one = response_one.json()["work_order_number"]

        response_two = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data
        )
        assert response_two.status_code == status.HTTP_201_CREATED
        wo_number_two = response_two.json()["work_order_number"]

        assert wo_number_one != wo_number_two
