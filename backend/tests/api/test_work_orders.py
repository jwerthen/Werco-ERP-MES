import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.part import Part
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
        assert float(data["quantity_ordered"]) == sample_work_order_data["quantity_ordered"]

    def test_create_work_order_unauthorized(
        self, client: TestClient, sample_work_order_data: dict
    ):
        """Test creating a work order without authentication."""
        response = client.post("/api/v1/work-orders/", json=sample_work_order_data)
        assert response.status_code == status.HTTP_403_FORBIDDEN

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

    def test_get_work_order_not_found(
        self, client: TestClient, auth_headers: dict
    ):
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


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrdersValidation:
    """Test work order validation."""

    def test_create_work_order_missing_required_fields(
        self, client: TestClient, auth_headers: dict
    ):
        """Test creating a work order with missing required fields."""
        invalid_data = {"customer_name": "Test Customer"}
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=invalid_data)
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
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=invalid_data)
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
