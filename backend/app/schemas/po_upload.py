"""
Schemas for Purchase Order PDF Upload and Extraction
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date


class VendorExtracted(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None


class LineItemExtracted(BaseModel):
    line_number: int = 1
    part_number: Optional[str] = None
    description: Optional[str] = None
    qty_ordered: float = 0
    unit_of_measure: str = "EA"
    unit_price: float = 0
    line_total: float = 0
    confidence: str = "medium"
    suggested_part_type: Optional[str] = None
    # Matching info
    part_match: Optional[Dict[str, Any]] = None
    matched_part_id: Optional[int] = None


class POExtractionResult(BaseModel):
    """Response from PDF extraction"""
    # Extracted PO data
    document_type: Optional[str] = None
    po_number: Optional[str] = None
    invoice_number: Optional[str] = None
    vendor: VendorExtracted = Field(default_factory=VendorExtracted)
    vendor_match: Optional[Dict[str, Any]] = None
    matched_vendor_id: Optional[int] = None
    
    order_date: Optional[str] = None
    expected_delivery_date: Optional[str] = None
    required_date: Optional[str] = None
    payment_terms: Optional[str] = None
    shipping_method: Optional[str] = None
    ship_to: Optional[str] = None
    
    line_items: List[LineItemExtracted] = Field(default_factory=list)
    
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    shipping_cost: Optional[float] = None
    total_amount: Optional[float] = None
    notes: Optional[str] = None
    
    # Extraction metadata
    extraction_confidence: str = "medium"
    pdf_was_ocr: bool = False
    pdf_page_count: int = 0
    pdf_path: str = ""
    
    # Validation
    validation_issues: List[Dict[str, str]] = Field(default_factory=list)
    po_number_exists: bool = False
    
    class Config:
        from_attributes = True


class LineItemConfirm(BaseModel):
    """Line item data for PO creation"""
    part_id: int
    part_number: str
    description: Optional[str] = None
    quantity_ordered: float
    unit_price: float
    line_total: Optional[float] = None
    notes: Optional[str] = None


class POCreateFromUpload(BaseModel):
    """Request to create PO from extracted data"""
    po_number: str
    vendor_id: int
    create_vendor: bool = False
    new_vendor_name: Optional[str] = None
    new_vendor_code: Optional[str] = None
    new_vendor_address: Optional[str] = None
    
    order_date: Optional[date] = None
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    
    payment_terms: Optional[str] = None
    shipping_method: Optional[str] = None
    ship_to: Optional[str] = None
    notes: Optional[str] = None
    
    line_items: List[LineItemConfirm]
    
    # Parts to create - each dict should have: part_number, description, part_type (optional: 'purchased' or 'raw_material')
    create_parts: List[Dict[str, Any]] = Field(default_factory=list)
    
    # PDF reference
    pdf_path: str


class POUploadResponse(BaseModel):
    """Response after PO creation"""
    success: bool
    po_id: Optional[int] = None
    po_number: Optional[str] = None
    message: str
    lines_created: int = 0
    vendor_created: bool = False
    parts_created: int = 0
