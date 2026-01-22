import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.part import Part


@pytest.mark.api
@pytest.mark.requires_db
class TestPartsAPI:
    """Test parts API endpoints."""

    def test_list_parts_empty(self, client: TestClient, auth_headers: dict):
        """Test listing parts when none exist."""
        response = client.get("/api/v1/parts/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 0

    def test_list_parts(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test listing parts with existing data."""
        response = client.get("/api/v1/parts/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["part_number"] == test_part.part_number

    def test_create_part(
        self, client: TestClient, auth_headers: dict, sample_part_data: dict
    ):
        """Test creating a new part."""
        response = client.post("/api/v1/parts/", headers=auth_headers, json=sample_part_data)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["part_number"] == sample_part_data["part_number"]
        assert data["name"] == sample_part_data["name"]
        assert data["part_type"] == sample_part_data["part_type"]

    def test_create_part_unauthorized(self, client: TestClient, sample_part_data: dict):
        """Test creating a part without authentication."""
        response = client.post("/api/v1/parts/", json=sample_part_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_part_by_id(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test retrieving a single part by ID."""
        response = client.get(f"/api/v1/parts/{test_part.id}", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_part.id
        assert data["part_number"] == test_part.part_number

    def test_get_part_not_found(self, client: TestClient, auth_headers: dict):
        """Test retrieving a non-existent part."""
        response = client.get("/api/v1/parts/99999", headers=auth_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_part(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test updating an existing part."""
        update_data = {"version": 0, "name": "Updated Part Name", "description": "Updated description"}
        response = client.put(
            f"/api/v1/parts/{test_part.id}", headers=auth_headers, json=update_data
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "Updated Part Name"
        assert data["description"] == "Updated description"

    def test_search_parts(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test searching parts by number or name."""
        response = client.get(
            f"/api/v1/parts/?search={test_part.part_number}", headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1
        assert test_part.part_number in data[0]["part_number"]

    def test_filter_parts_by_type(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test filtering parts by type."""
        response = client.get(
            f"/api/v1/parts/?part_type={test_part.part_type.value}", headers=auth_headers
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert all(item["part_type"] == test_part.part_type.value for item in data)


@pytest.mark.api
@pytest.mark.requires_db
class TestPartsValidation:
    """Test part validation."""

    def test_create_part_missing_required_fields(
        self, client: TestClient, auth_headers: dict
    ):
        """Test creating a part with missing required fields."""
        invalid_data = {"name": "Test Part"}
        response = client.post("/api/v1/parts/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_part_duplicate_number(
        self, client: TestClient, auth_headers: dict, test_part: Part
    ):
        """Test creating a part with duplicate number."""
        duplicate_data = {
            "part_number": test_part.part_number,
            "name": "Another Part",
            "part_type": "manufactured",
            "unit_of_measure": "each",
        }
        response = client.post("/api/v1/parts/", headers=auth_headers, json=duplicate_data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_part_type(self, client: TestClient, auth_headers: dict):
        """Test creating a part with invalid type."""
        invalid_data = {
            "part_number": "P-INVALID-001",
            "name": "Test Part",
            "part_type": "invalid_type",
            "unit_of_measure": "each",
        }
        response = client.post("/api/v1/parts/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
