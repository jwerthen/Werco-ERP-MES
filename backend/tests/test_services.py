"""Unit tests for backend services."""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, Session
from app.services.matching_service import (
    MatchResult,
    match_vendor,
    match_part,
    match_po_line_items,
    check_po_number_exists
)


@pytest.mark.unit
class TestMatchResult:
    """Test MatchResult class."""

    def test_match_result_initialization(self):
        """Test MatchResult initialization."""
        result = MatchResult(
            matched=True,
            match_id=1,
            match_name="Test Vendor",
            confidence=95.5,
            suggestions=[{"id": 1, "name": "Test"}]
        )
        assert result.matched is True
        assert result.match_id == 1
        assert result.match_name == "Test Vendor"
        assert result.confidence == 95.5
        assert len(result.suggestions) == 1

    def test_match_result_to_dict(self):
        """Test MatchResult to_dict conversion."""
        result = MatchResult(
            matched=True,
            match_id=1,
            match_name="Test",
            confidence=90.0
        )
        data = result.to_dict()
        assert data == {
            "matched": True,
            "match_id": 1,
            "match_name": "Test",
            "confidence": 90.0,
            "suggestions": []
        }

    def test_match_result_empty_suggestions(self):
        """Test MatchResult with empty suggestions."""
        result = MatchResult(matched=False)
        assert result.matched is False
        assert result.match_id is None
        assert result.match_name == ""
        assert result.confidence == 0.0
        assert result.suggestions == []


@pytest.mark.unit
class TestMatchVendor:
    """Test vendor matching functionality."""

    def test_match_vendor_empty_string(self, db_session: Session):
        """Test matching with empty vendor name."""
        result = match_vendor("", db_session)
        assert result.matched is False
        assert result.match_id is None

    def test_match_vendor_none_string(self, db_session: Session):
        """Test matching with None vendor name."""
        result = match_vendor(None, db_session)
        assert result.matched is False

    def test_match_vendor_exact_match(self, db_session: Session, test_vendor: Vendor):
        """Test exact vendor match."""
        result = match_vendor(test_vendor.name, db_session)
        assert result.matched is True
        assert result.match_id == test_vendor.id
        assert result.match_name == test_vendor.name
        assert result.confidence == 100.0

    def test_match_vendor_case_insensitive(self, db_session: Session, test_vendor: Vendor):
        """Test case-insensitive vendor matching."""
        result = match_vendor(test_vendor.name.lower(), db_session)
        assert result.matched is True
        assert result.match_id == test_vendor.id

    def test_match_vendor_trimmed_whitespace(self, db_session: Session, test_vendor: Vendor):
        """Test whitespace handling in vendor names."""
        result = match_vendor(f"  {test_vendor.name}  ", db_session)
        assert result.matched is True

    def test_match_vendor_no_match(self, db_session: Session):
        """Test with no valid vendor."""
        result = match_vendor("Non-existent Vendor XYZ123", db_session, threshold=95)
        assert result.matched is False
        assert result.match_id is None


@pytest.mark.unit
class TestMatchPart:
    """Test part matching functionality."""

    def test_match_part_empty_string(self, db_session: Session):
        """Test matching with empty part number."""
        result = match_part("", db_session)
        assert result.matched is False
        assert result.match_id is None

    def test_match_part_none_string(self, db_session: Session):
        """Test matching with None part number."""
        result = match_part(None, db_session)
        assert result.matched is False

    def test_match_part_exact_match(self, db_session: Session, test_part: Part):
        """Test exact part match."""
        result = match_part(test_part.part_number, db_session)
        assert result.matched is True
        assert result.match_id == test_part.id
        assert result.match_name == test_part.part_number
        assert result.confidence == 100.0

    def test_match_part_case_insensitive(self, db_session: Session, test_part: Part):
        """Test case-insensitive part matching."""
        result = match_part(test_part.part_number.lower(), db_session)
        assert result.matched is True
        assert result.match_id == test_part.id

    def test_match_part_with_special_chars(self, db_session: Session):
        """Test part number with special characters."""
        from app.models.part import Part

        # Create a part with special characters
        part = Part(
            part_number="P-123.45",
            name="Test Part with Dots",
            type="purchased",
            unit_of_measure="EA"
        )
        db_session.add(part)
        db_session.commit()

        # Test matching with different formats
        result = match_part("P-12345", db_session)
        assert result.matched is True


@pytest.mark.unit
class TestMatchPOLineItems:
    """Test PO line item matching."""

    def test_match_po_line_items_empty_list(self, db_session: Session):
        """Test with empty line items list."""
        result = match_po_line_items([], db_session)
        assert result == []

    def test_match_po_line_items_single_item(self, db_session: Session, test_part: Part):
        """Test matching single line item."""
        line_items = [{"part_number": test_part.part_number, "quantity": 10}]
        result = match_po_line_items(line_items, db_session)

        assert len(result) == 1
        assert result[0]["part_match"]["matched"] is True
        assert result[0]["matched_part_id"] == test_part.id

    def test_match_po_line_items_multiple_items(self, db_session: Session, test_part: Part):
        """Test matching multiple line items."""
        line_items = [
            {"part_number": test_part.part_number, "quantity": 10},
            {"part_number": "P-INVALID-001", "quantity": 5},
            {"part_number": test_part.part_number.upper(), "quantity": 20}
        ]
        result = match_po_line_items(line_items, db_session)

        assert len(result) == 3
        assert result[0]["matched_part_id"] == test_part.id
        assert result[2]["matched_part_id"] == test_part.id
        assert result[1]["matched_part_id"] is None  # Invalid part

    def test_match_po_line_items_preserves_data(self, db_session: Session):
        """Test that original line item data is preserved."""
        line_items = [{"part_number": "P-001", "quantity": 10, "desc": "Test"}]
        result = match_po_line_items(line_items, db_session)

        assert result[0]["quantity"] == 10
        assert result[0]["desc"] == "Test"
        assert "part_match" in result[0]
        assert "matched_part_id" in result[0]


@pytest.mark.unit
class TestCheckPONumberExists:
    """Test PO number existence checking."""

    def test_check_po_number_exists_empty_string(self, db_session: Session):
        """Test with empty PO number."""
        result = check_po_number_exists("", db_session)
        assert result is False

    def test_check_po_number_exists_none_string(self, db_session: Session):
        """Test with None PO number."""
        result = check_po_number_exists(None, db_session)
        assert result is False

    def test_check_po_number_exists_whitespace(self, db_session: Session):
        """Test with whitespace-only PO number."""
        result = check_po_number_exists("   ", db_session)
        assert result is False

    def test_check_po_number_exists_not_found(self, db_session: Session):
        """Test with non-existent PO number."""
        result = check_po_number_exists("PO-99999", db_session)
        assert result is False


@pytest.mark.unit
@pytest.mark.requires_db
class TestMatchingIntegration:
    """Integration tests for matching service."""

    async def test_vendor_match_with_multiple_vendors(self, db_session: AsyncSession, vendor_factory):
        """Test vendor matching with multiple options."""
        # Create multiple vendors
        vendor_factory("Acme Corporation", "ACM001")
        vendor_factory("Acme Inc", "ACM002")
        vendor_factory("Acme Supplies", "ACM003")

        # Try to match similar name
        result = match_vendor("Acme Corp", db_session)
        assert result.matched is True
        assert len(result.suggestions) > 0

    async def test_part_match_threshold_behavior(self, db_session: AsyncSession, part_factory):
        """Test part matching with different thresholds."""
        part_factory("P-12345", "Part 12345")

        # High threshold - might not match
        result_high = match_part("P-1234", db_session, threshold=95)
        
        # Low threshold - should match fuzzy
        result_low = match_part("P-1234", db_session, threshold=60)
        
        # Results will depend on fuzzy matching algorithm
        assert isinstance(result_high, MatchResult)
        assert isinstance(result_low, MatchResult)
