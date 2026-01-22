"""Tests for PO Upload endpoint and related services."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.models.purchasing import Vendor, PurchaseOrder, PurchaseOrderLine, POStatus
from app.models.part import Part, PartType
from app.services.matching_service import (
    match_vendor, match_part, match_po_line_items, 
    check_po_number_exists, MatchResult
)


@pytest.mark.api
@pytest.mark.requires_db
class TestPOUploadSearchEndpoints:
    """Test PO upload search endpoints."""
    
    def test_search_parts(self, client: TestClient, auth_headers: dict, test_part: Part):
        """Test searching parts for PO matching."""
        response = client.get(
            f"/api/v1/po-upload/search-parts?q={test_part.part_number[:5]}",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        if len(data) > 0:
            assert "id" in data[0]
            assert "part_number" in data[0]
    
    def test_search_vendors(self, client: TestClient, auth_headers: dict, test_vendor):
        """Test searching vendors for PO matching."""
        response = client.get(
            f"/api/v1/po-upload/search-vendors?q={test_vendor.name[:5]}",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        if len(data) > 0:
            assert "id" in data[0]
            assert "name" in data[0]
    
    def test_search_parts_unauthorized(self, client: TestClient):
        """Test searching parts without authentication."""
        response = client.get("/api/v1/po-upload/search-parts?q=test")
        assert response.status_code == 401
    
    def test_search_vendors_unauthorized(self, client: TestClient):
        """Test searching vendors without authentication."""
        response = client.get("/api/v1/po-upload/search-vendors?q=test")
        assert response.status_code == 401


@pytest.mark.unit
class TestMatchingService:
    """Test the matching service functions."""
    
    def test_match_vendor_exact(self, db_session, vendor_factory):
        """Test exact vendor match."""
        vendor = vendor_factory("ACME Corporation", "ACME-001")
        result = match_vendor("ACME Corporation", db_session)
        
        assert result.matched is True
        assert result.match_id == vendor.id
        assert result.confidence == 100.0
    
    def test_match_vendor_case_insensitive(self, db_session, vendor_factory):
        """Test case-insensitive vendor match."""
        vendor = vendor_factory("Test Vendor Inc", "TV-001")
        result = match_vendor("TEST VENDOR INC", db_session)
        
        assert result.matched is True
        assert result.match_id == vendor.id
    
    def test_match_vendor_fuzzy(self, db_session, vendor_factory):
        """Test fuzzy vendor match."""
        vendor = vendor_factory("McMaster-Carr Supply", "MC-001")
        result = match_vendor("Mcmaster Carr", db_session, threshold=70)
        
        assert result.matched is True
        assert result.match_id == vendor.id
        assert result.confidence >= 70.0
    
    def test_match_vendor_no_match(self, db_session, vendor_factory):
        """Test vendor with no match."""
        vendor_factory("Completely Different Company", "CD-001")
        result = match_vendor("XYZ Totally Unrelated", db_session)
        
        assert result.matched is False
        assert result.suggestions is not None
    
    def test_match_vendor_empty_name(self, db_session):
        """Test vendor match with empty name."""
        result = match_vendor("", db_session)
        assert result.matched is False
    
    def test_match_vendor_none_name(self, db_session):
        """Test vendor match with None name."""
        result = match_vendor(None, db_session)
        assert result.matched is False
    
    def test_match_part_exact(self, db_session, part_factory):
        """Test exact part match."""
        part = part_factory("P-12345-A")
        result = match_part("P-12345-A", db_session)
        
        assert result.matched is True
        assert result.match_id == part.id
        assert result.confidence == 100.0
    
    def test_match_part_case_insensitive(self, db_session, part_factory):
        """Test case-insensitive part match."""
        part = part_factory("WIDGET-100")
        result = match_part("widget-100", db_session)
        
        assert result.matched is True
        assert result.match_id == part.id
    
    def test_match_part_with_dashes_spaces(self, db_session, part_factory):
        """Test part match ignoring dashes and spaces."""
        part = part_factory("ABC-123-DEF")
        result = match_part("ABC123DEF", db_session)
        
        assert result.matched is True
        assert result.match_id == part.id
    
    def test_match_part_no_match(self, db_session, part_factory):
        """Test part with no match."""
        part_factory("EXISTING-PART-001")
        result = match_part("TOTALLY-DIFFERENT-999", db_session)
        
        assert result.matched is False
        assert result.suggestions is not None
    
    def test_match_part_empty_number(self, db_session):
        """Test part match with empty number."""
        result = match_part("", db_session)
        assert result.matched is False
    
    def test_match_po_line_items(self, db_session, part_factory):
        """Test matching multiple PO line items."""
        part1 = part_factory("LINE-ITEM-001")
        part2 = part_factory("LINE-ITEM-002")
        
        line_items = [
            {"part_number": "LINE-ITEM-001", "qty_ordered": 10},
            {"part_number": "LINE-ITEM-002", "qty_ordered": 20},
            {"part_number": "UNKNOWN-PART", "qty_ordered": 5}
        ]
        
        result = match_po_line_items(line_items, db_session)
        
        assert len(result) == 3
        assert result[0]["matched_part_id"] == part1.id
        assert result[1]["matched_part_id"] == part2.id
        assert result[2]["matched_part_id"] is None
    
    def test_check_po_number_exists_true(self, db_session, test_vendor):
        """Test checking if PO number exists (true case)."""
        po = PurchaseOrder(
            po_number="PO-TEST-EXISTS",
            vendor_id=test_vendor.id,
            status=POStatus.DRAFT
        )
        db_session.add(po)
        db_session.commit()
        
        result = check_po_number_exists("PO-TEST-EXISTS", db_session)
        assert result is True
    
    def test_check_po_number_exists_false(self, db_session):
        """Test checking if PO number exists (false case)."""
        result = check_po_number_exists("PO-DOES-NOT-EXIST-999", db_session)
        assert result is False
    
    def test_check_po_number_exists_empty(self, db_session):
        """Test checking if PO number exists with empty string."""
        result = check_po_number_exists("", db_session)
        assert result is False
    
    def test_check_po_number_exists_none(self, db_session):
        """Test checking if PO number exists with None."""
        result = check_po_number_exists(None, db_session)
        assert result is False


@pytest.mark.unit
class TestPOCreateFromUploadRawMaterial:
    """Test PO creation with raw material parts."""
    
    def test_create_part_as_raw_material(self, client: TestClient, admin_headers: dict, test_vendor, db_session):
        """Test creating a part as raw_material type during PO creation."""
        data = {
            "po_number": "PO-TEST-RAW-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": 0,
                    "part_number": "RAW-STEEL-001",
                    "description": "Steel Sheet 4x8 16ga",
                    "quantity_ordered": 10,
                    "unit_price": 50.00
                }
            ],
            "create_parts": [
                {
                    "part_number": "RAW-STEEL-001",
                    "description": "Steel Sheet 4x8 16ga",
                    "part_type": "raw_material"
                }
            ],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["parts_created"] == 1
        
        created_part = db_session.query(Part).filter(Part.part_number == "RAW-STEEL-001").first()
        assert created_part is not None
        assert created_part.part_type == PartType.RAW_MATERIAL
    
    def test_create_part_as_purchased_default(self, client: TestClient, admin_headers: dict, test_vendor, db_session):
        """Test creating a part defaults to purchased type."""
        data = {
            "po_number": "PO-TEST-PURCH-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": 0,
                    "part_number": "BOLT-HEX-001",
                    "description": "Hex Bolt 1/4-20 x 1",
                    "quantity_ordered": 100,
                    "unit_price": 0.25
                }
            ],
            "create_parts": [
                {
                    "part_number": "BOLT-HEX-001",
                    "description": "Hex Bolt 1/4-20 x 1"
                }
            ],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["parts_created"] == 1
        
        created_part = db_session.query(Part).filter(Part.part_number == "BOLT-HEX-001").first()
        assert created_part is not None
        assert created_part.part_type == PartType.PURCHASED
    
    def test_create_part_with_explicit_purchased_type(self, client: TestClient, admin_headers: dict, test_vendor, db_session):
        """Test creating a part with explicit purchased type."""
        data = {
            "po_number": "PO-TEST-EXPL-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": 0,
                    "part_number": "NUT-HEX-001",
                    "description": "Hex Nut 1/4-20",
                    "quantity_ordered": 100,
                    "unit_price": 0.10
                }
            ],
            "create_parts": [
                {
                    "part_number": "NUT-HEX-001",
                    "description": "Hex Nut 1/4-20",
                    "part_type": "purchased"
                }
            ],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        
        created_part = db_session.query(Part).filter(Part.part_number == "NUT-HEX-001").first()
        assert created_part is not None
        assert created_part.part_type == PartType.PURCHASED


@pytest.mark.api
@pytest.mark.requires_db
class TestPOCreateFromUpload:
    """Test creating PO from uploaded data."""
    
    def test_create_po_basic(self, client: TestClient, admin_headers: dict, test_vendor, test_part):
        """Test basic PO creation from upload."""
        data = {
            "po_number": "PO-UPLOAD-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": test_part.id,
                    "part_number": test_part.part_number,
                    "description": "Test part description",
                    "quantity_ordered": 10,
                    "unit_price": 25.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["po_number"] == "PO-UPLOAD-001"
        assert result["lines_created"] == 1
    
    def test_create_po_with_new_vendor(self, client: TestClient, admin_headers: dict, test_part):
        """Test PO creation with new vendor."""
        data = {
            "po_number": "PO-NEWVENDOR-001",
            "vendor_id": 0,
            "create_vendor": True,
            "new_vendor_name": "Brand New Supplier Inc",
            "new_vendor_code": "BNS-001",
            "line_items": [
                {
                    "part_id": test_part.id,
                    "part_number": test_part.part_number,
                    "description": "Test part",
                    "quantity_ordered": 5,
                    "unit_price": 10.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["vendor_created"] is True
    
    def test_create_po_duplicate_number(self, client: TestClient, admin_headers: dict, test_vendor, test_part, db_session):
        """Test PO creation fails with duplicate number."""
        existing_po = PurchaseOrder(
            po_number="PO-DUPLICATE-001",
            vendor_id=test_vendor.id,
            status=POStatus.DRAFT
        )
        db_session.add(existing_po)
        db_session.commit()
        
        data = {
            "po_number": "PO-DUPLICATE-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": test_part.id,
                    "part_number": test_part.part_number,
                    "description": "Test",
                    "quantity_ordered": 1,
                    "unit_price": 1.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]
    
    def test_create_po_invalid_vendor(self, client: TestClient, admin_headers: dict, test_part):
        """Test PO creation fails with invalid vendor."""
        data = {
            "po_number": "PO-BADVENDOR-001",
            "vendor_id": 99999,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": test_part.id,
                    "part_number": test_part.part_number,
                    "description": "Test",
                    "quantity_ordered": 1,
                    "unit_price": 1.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 400
        assert "Vendor not found" in response.json()["detail"]
    
    def test_create_po_missing_part(self, client: TestClient, admin_headers: dict, test_vendor):
        """Test PO creation fails when part not found and not in create list."""
        data = {
            "po_number": "PO-NOPART-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": 0,
                    "part_number": "NONEXISTENT-PART",
                    "description": "Test",
                    "quantity_ordered": 1,
                    "unit_price": 1.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=admin_headers,
            json=data
        )
        
        assert response.status_code == 400
        assert "not found and not in create list" in response.json()["detail"]
    
    def test_create_po_unauthorized(self, client: TestClient, operator_headers: dict, test_vendor, test_part):
        """Test PO creation requires proper role."""
        data = {
            "po_number": "PO-UNAUTH-001",
            "vendor_id": test_vendor.id,
            "create_vendor": False,
            "line_items": [
                {
                    "part_id": test_part.id,
                    "part_number": test_part.part_number,
                    "description": "Test",
                    "quantity_ordered": 1,
                    "unit_price": 1.00
                }
            ],
            "create_parts": [],
            "pdf_path": ""
        }
        
        response = client.post(
            "/api/v1/po-upload/create-from-upload",
            headers=operator_headers,
            json=data
        )
        
        assert response.status_code == 403


@pytest.mark.unit
class TestMatchResult:
    """Test MatchResult class."""
    
    def test_match_result_to_dict(self):
        """Test MatchResult serialization."""
        result = MatchResult(
            matched=True,
            match_id=123,
            match_name="Test Vendor",
            confidence=95.0,
            suggestions=[{"id": 1, "name": "Other"}]
        )
        
        d = result.to_dict()
        
        assert d["matched"] is True
        assert d["match_id"] == 123
        assert d["match_name"] == "Test Vendor"
        assert d["confidence"] == 95.0
        assert len(d["suggestions"]) == 1
    
    def test_match_result_default_suggestions(self):
        """Test MatchResult with default suggestions."""
        result = MatchResult(matched=False)
        
        assert result.suggestions == []
        d = result.to_dict()
        assert d["suggestions"] == []
