import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus


@pytest.mark.api
@pytest.mark.requires_db
class TestCustomersAPI:
    def test_get_customer_names(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        customer = Customer(name="Acme Aerospace", code="ACM001", is_active=True)
        db_session.add(customer)
        db_session.commit()

        response = client.get("/api/v1/customers/names", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == [{"id": customer.id, "name": "Acme Aerospace"}]

    def test_customer_stats_includes_parts_assemblies_and_work_orders(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        customer = Customer(name="Acme Aerospace", code="ACM001", is_active=True)
        other_customer = Customer(name="Beta Systems", code="BET001", is_active=True)
        db_session.add_all([customer, other_customer])
        db_session.flush()

        assembly = Part(
            part_number="ASM-100",
            name="Bracket Assembly",
            part_type="assembly",
            unit_of_measure="each",
            customer_name=customer.name,
            is_active=True,
        )
        part = Part(
            part_number="PRT-100",
            name="Bracket Plate",
            part_type="manufactured",
            unit_of_measure="each",
            customer_name=customer.name,
            is_active=True,
        )
        unrelated_part = Part(
            part_number="PRT-200",
            name="Other Part",
            part_type="manufactured",
            unit_of_measure="each",
            customer_name=other_customer.name,
            is_active=True,
        )
        db_session.add_all([assembly, part, unrelated_part])
        db_session.flush()

        current_work_order = WorkOrder(
            work_order_number="WO-CURRENT-001",
            part_id=part.id,
            quantity_ordered=10,
            status=WorkOrderStatus.RELEASED,
        )
        past_work_order = WorkOrder(
            work_order_number="WO-PAST-001",
            part_id=assembly.id,
            quantity_ordered=6,
            status=WorkOrderStatus.COMPLETE,
            customer_name=customer.name,
        )
        by_name_work_order = WorkOrder(
            work_order_number="WO-CURRENT-002",
            part_id=unrelated_part.id,
            quantity_ordered=4,
            status=WorkOrderStatus.DRAFT,
            customer_name=customer.name,
        )
        other_customer_work_order = WorkOrder(
            work_order_number="WO-OTHER-001",
            part_id=unrelated_part.id,
            quantity_ordered=8,
            status=WorkOrderStatus.RELEASED,
            customer_name=other_customer.name,
        )
        db_session.add_all(
            [
                current_work_order,
                past_work_order,
                by_name_work_order,
                other_customer_work_order,
            ]
        )
        db_session.commit()

        response = client.get(
            f"/api/v1/customers/{customer.id}/stats", headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert data["customer_name"] == customer.name
        assert data["part_count"] == 2
        assert len(data["assemblies"]) == 1
        assert data["assemblies"][0]["part_number"] == assembly.part_number
        assert len(data["parts"]) == 1
        assert data["parts"][0]["part_number"] == part.part_number

        current_wo_numbers = {
            wo["work_order_number"] for wo in data["current_work_orders"]
        }
        assert "WO-CURRENT-001" in current_wo_numbers
        assert "WO-CURRENT-002" in current_wo_numbers
        assert "WO-OTHER-001" not in current_wo_numbers

        past_wo_numbers = {wo["work_order_number"] for wo in data["past_work_orders"]}
        assert past_wo_numbers == {"WO-PAST-001"}

        assert data["work_order_counts"]["total"] == 3
        assert data["work_order_counts"]["by_status"]["released"] == 1
        assert data["work_order_counts"]["by_status"]["draft"] == 1
        assert data["work_order_counts"]["by_status"]["complete"] == 1
