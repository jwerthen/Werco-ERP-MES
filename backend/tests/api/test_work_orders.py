import pytest
from fastapi import status


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkOrdersAPI:
    """Test work orders API endpoints."""

    def test_list_work_orders_empty(self, client: TestClient, auth_headers: dict):
        """Test listing work orders when none exist."""
        response = client.get("/api/v1/work-orders/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 0

    def test_list_work_orders(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test listing work orders with existing data."""
        response = client.get("/api/v1/work-orders/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["number"] == test_work_order.number

    def test_create_work_order(
        self, client: TestClient, auth_headers: dict, sample_work_order_data: dict
    ):
        """Test creating a new work order."""
        response = client.post(
            "/api/v1/work-orders/", headers=auth_headers, json=sample_work_order_data
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["number"] == sample_work_order_data["number"]
        assert data["customer_name"] == sample_work_order_data["customer_name"]
        assert data["quantity"] == sample_work_order_data["quantity"]

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
        assert data["number"] == test_work_order.number

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
        update_data = {"status": "released", "priority": 1}
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
        assert len(data["items"]) >= 1
        assert test_work_order.customer_name in data["items"][0]["customer_name"]


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
            "number": "WO-TEST-001",
            "customer_name": "Test Customer",
            "part_id": test_part.id,
            "quantity": -10,
        }
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_work_order_duplicate_number(
        self, client: TestClient, auth_headers: dict, test_work_order: WorkOrder
    ):
        """Test creating a work order with duplicate number."""
        duplicate_data = {
            "number": test_work_order.number,
            "customer_name": "Another Customer",
            "part_id": test_work_order.part_id,
            "quantity": 100,
        }
        response = client.post("/api/v1/work-orders/", headers=auth_headers, json=duplicate_data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
