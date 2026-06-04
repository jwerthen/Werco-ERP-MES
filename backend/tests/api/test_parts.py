import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
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

    def test_list_parts(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test listing parts with existing data."""
        response = client.get("/api/v1/parts/", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["part_number"] == test_part.part_number

    def test_create_part(self, client: TestClient, auth_headers: dict, sample_part_data: dict):
        """Test creating a new part."""
        response = client.post("/api/v1/parts/", headers=auth_headers, json=sample_part_data)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["part_number"] == sample_part_data["part_number"]
        assert data["name"] == sample_part_data["name"]
        assert data["part_type"] == sample_part_data["part_type"]

    def test_create_part_allows_customer_hash_numbers(self, client: TestClient, auth_headers: dict):
        """Customer part numbers can include # characters."""
        response = client.post(
            "/api/v1/parts/",
            headers=auth_headers,
            json={
                "part_number": "M#Z-72S-63S-QS-J-2410048-HSG",
                "name": "Miratech Housing",
                "part_type": "assembly",
                "unit_of_measure": "each",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["part_number"] == "M#Z-72S-63S-QS-J-2410048-HSG"

    def test_create_part_unauthorized(self, client: TestClient, sample_part_data: dict):
        """Test creating a part without authentication."""
        response = client.post("/api/v1/parts/", json=sample_part_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_part_by_id(self, client: TestClient, auth_headers: dict, test_part: Part):
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

    def test_update_part(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test updating an existing part."""
        update_data = {"version": 0, "name": "Updated Part Name", "description": "Updated description"}
        response = client.put(f"/api/v1/parts/{test_part.id}", headers=auth_headers, json=update_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "Updated Part Name"
        assert data["description"] == "Updated description"

    def test_search_parts(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test searching parts by number or name."""
        response = client.get(f"/api/v1/parts/?search={test_part.part_number}", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 1
        assert test_part.part_number in data[0]["part_number"]

    def test_filter_parts_by_type(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test filtering parts by type."""
        response = client.get(f"/api/v1/parts/?part_type={test_part.part_type.value}", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert all(item["part_type"] == test_part.part_type.value for item in data)

    def test_parts_default_to_engineering_items(self, client: TestClient, auth_headers: dict, db_session: Session):
        """The engineering parts list excludes materials and supplies by default."""
        rows = [
            Part(part_number="ENG-001", name="Machined Bracket", part_type="manufactured", unit_of_measure="each", is_active=True, company_id=1),
            Part(part_number="ASM-001", name="Top Assembly", part_type="assembly", unit_of_measure="each", is_active=True, company_id=1),
            Part(part_number="RAW-001", name="Steel Sheet", part_type="raw_material", unit_of_measure="sheets", is_active=True, company_id=1),
            Part(part_number="HW-001", name="Hex Bolt", part_type="hardware", unit_of_measure="each", is_active=True, company_id=1),
            Part(part_number="BUY-001", name="Purchased Motor", part_type="purchased", unit_of_measure="each", is_active=True, company_id=1),
        ]
        db_session.add_all(rows)
        db_session.commit()

        response = client.get("/api/v1/parts/?limit=500", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        part_numbers = {part["part_number"] for part in response.json()}
        assert part_numbers == {"ASM-001", "ENG-001"}

        response = client.get("/api/v1/parts/?item_group=all&limit=500", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        part_numbers = {part["part_number"] for part in response.json()}
        assert {"ASM-001", "ENG-001", "RAW-001", "HW-001", "BUY-001"}.issubset(part_numbers)

    def test_create_part_rejects_material_types(self, client: TestClient, auth_headers: dict):
        """Engineering parts endpoint does not create material/supply records."""
        response = client.post(
            "/api/v1/parts/",
            headers=auth_headers,
            json={
                "part_number": "RAW-BLOCKED-001",
                "name": "Steel Sheet",
                "part_type": "raw_material",
                "unit_of_measure": "sheets",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_hide_active_bom_components(self, client: TestClient, auth_headers: dict, db_session: Session):
        """Parts used in active BOMs can be hidden from the top-level parts list."""
        assembly = Part(
            part_number="ASM-HIDE-001",
            name="Top Assembly",
            part_type="assembly",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        component = Part(
            part_number="CMP-HIDE-001",
            name="Nested Component",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db_session.add_all([assembly, component])
        db_session.flush()

        bom = BOM(
            part_id=assembly.id,
            revision="A",
            status="released",
            is_active=True,
            company_id=1,
        )
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
                company_id=1,
            )
        )
        db_session.commit()

        response = client.get(
            "/api/v1/parts/?include_bom_components=false&limit=500",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        part_numbers = {part["part_number"] for part in response.json()}
        assert assembly.part_number in part_numbers
        assert component.part_number not in part_numbers

        response = client.get(
            "/api/v1/parts/?include_bom_components=true&limit=500",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        part_numbers = {part["part_number"] for part in response.json()}
        assert assembly.part_number in part_numbers
        assert component.part_number in part_numbers


@pytest.mark.api
@pytest.mark.requires_db
class TestMaterialsAPI:
    """Test materials and supplies API endpoints."""

    def test_list_materials_only(self, client: TestClient, auth_headers: dict, db_session: Session):
        rows = [
            Part(part_number="ENG-MAT-001", name="Machined Bracket", part_type="manufactured", unit_of_measure="each", is_active=True, company_id=1),
            Part(part_number="RAW-MAT-001", name="Aluminum Sheet", part_type="raw_material", unit_of_measure="sheets", is_active=True, company_id=1),
            Part(part_number="HW-MAT-001", name="Washer", part_type="hardware", unit_of_measure="each", is_active=True, company_id=1),
            Part(part_number="CON-MAT-001", name="Primer", part_type="consumable", unit_of_measure="gallons", is_active=True, company_id=1),
            Part(part_number="BUY-MAT-001", name="Purchased Pump", part_type="purchased", unit_of_measure="each", is_active=True, company_id=1),
        ]
        db_session.add_all(rows)
        db_session.commit()

        response = client.get("/api/v1/materials/?limit=500", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        part_numbers = {part["part_number"] for part in response.json()}
        assert part_numbers == {"BUY-MAT-001", "CON-MAT-001", "HW-MAT-001", "RAW-MAT-001"}

    def test_create_material_rejects_engineering_types(self, client: TestClient, auth_headers: dict):
        response = client.post(
            "/api/v1/materials/",
            headers=auth_headers,
            json={
                "part_number": "ASM-BLOCKED-001",
                "name": "Assembly",
                "part_type": "assembly",
                "unit_of_measure": "each",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_update_material_rejects_engineering_types(self, client: TestClient, auth_headers: dict, db_session: Session):
        material = Part(
            part_number="RAW-UPD-001",
            name="Steel Sheet",
            part_type="raw_material",
            unit_of_measure="sheets",
            is_active=True,
            company_id=1,
        )
        db_session.add(material)
        db_session.commit()

        response = client.put(
            f"/api/v1/materials/{material.id}",
            headers=auth_headers,
            json={"version": 0, "part_type": "manufactured"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.api
@pytest.mark.requires_db
class TestPartsValidation:
    """Test part validation."""

    def test_create_part_missing_required_fields(self, client: TestClient, auth_headers: dict):
        """Test creating a part with missing required fields."""
        invalid_data = {"name": "Test Part"}
        response = client.post("/api/v1/parts/", headers=auth_headers, json=invalid_data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_part_duplicate_number(self, client: TestClient, auth_headers: dict, test_part: Part):
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
