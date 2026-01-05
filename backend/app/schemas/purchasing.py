from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date


# Vendor schemas
class VendorBase(BaseModel):
    code: str
    name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "USA"
    payment_terms: Optional[str] = None
    lead_time_days: int = 14
    is_approved: bool = False
    is_as9100_certified: bool = False
    is_iso9001_certified: bool = False
    notes: Optional[str] = None


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    payment_terms: Optional[str] = None
    lead_time_days: Optional[int] = None
    is_approved: Optional[bool] = None
    is_as9100_certified: Optional[bool] = None
    is_iso9001_certified: Optional[bool] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class VendorResponse(VendorBase):
    id: int
    is_active: bool
    approval_date: Optional[date] = None
    quality_rating: Optional[float] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


# PO Line schemas
class POLineBase(BaseModel):
    part_id: int
    quantity_ordered: float
    unit_price: float
    required_date: Optional[date] = None
    notes: Optional[str] = None


class POLineCreate(POLineBase):
    pass


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    
    class Config:
        from_attributes = True


class POLineResponse(POLineBase):
    id: int
    purchase_order_id: int
    line_number: int
    quantity_received: float
    line_total: float
    is_closed: bool
    part: Optional[PartSummary] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


# PO schemas
class POBase(BaseModel):
    vendor_id: int
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    ship_to: Optional[str] = None
    shipping_method: Optional[str] = None
    notes: Optional[str] = None


class POCreate(POBase):
    lines: List[POLineCreate] = []


class POUpdate(BaseModel):
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    ship_to: Optional[str] = None
    shipping_method: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class VendorSummary(BaseModel):
    id: int
    code: str
    name: str
    
    class Config:
        from_attributes = True


class POResponse(POBase):
    id: int
    po_number: str
    status: str
    order_date: Optional[date] = None
    subtotal: float
    tax: float
    shipping: float
    total: float
    vendor: Optional[VendorSummary] = None
    lines: List[POLineResponse] = []
    created_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True


class POListResponse(BaseModel):
    id: int
    po_number: str
    vendor_id: int
    vendor_name: Optional[str] = None
    status: str
    order_date: Optional[date] = None
    required_date: Optional[date] = None
    total: float
    line_count: int
    created_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True


# Receipt schemas
class ReceiptCreate(BaseModel):
    po_line_id: int
    quantity_received: float
    lot_number: str  # Required for AS9100D traceability
    serial_numbers: Optional[str] = None
    heat_number: Optional[str] = None
    cert_number: Optional[str] = None
    coc_attached: bool = False
    location_id: Optional[int] = None
    requires_inspection: bool = True
    packing_slip_number: Optional[str] = None
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None
    over_receive_approved: bool = False  # Must be true if receiving more than ordered


class ReceiptInspection(BaseModel):
    quantity_accepted: float
    quantity_rejected: float = 0
    inspection_method: str  # visual, dimensional, functional, documentation_review, etc.
    defect_type: Optional[str] = None  # Required if quantity_rejected > 0
    inspection_notes: Optional[str] = None  # Required if quantity_rejected > 0


class LocationSummary(BaseModel):
    id: int
    code: str
    name: Optional[str] = None
    
    class Config:
        from_attributes = True


class UserSummary(BaseModel):
    id: int
    full_name: str
    employee_id: Optional[str] = None
    
    class Config:
        from_attributes = True


class ReceiptResponse(BaseModel):
    id: int
    receipt_number: str
    po_line_id: int
    quantity_received: float
    quantity_accepted: float
    quantity_rejected: float
    lot_number: str
    serial_numbers: Optional[str] = None
    heat_number: Optional[str] = None
    cert_number: Optional[str] = None
    coc_attached: bool = False
    location: Optional[LocationSummary] = None
    status: str
    inspection_status: str
    requires_inspection: bool
    inspection_method: Optional[str] = None
    defect_type: Optional[str] = None
    inspected_at: Optional[datetime] = None
    inspection_notes: Optional[str] = None
    packing_slip_number: Optional[str] = None
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    over_receive_approved: bool = False
    received_at: datetime
    received_by: Optional[int] = None
    inspected_by: Optional[int] = None
    notes: Optional[str] = None
    
    class Config:
        from_attributes = True
        use_enum_values = True


class InspectionQueueItem(BaseModel):
    receipt_id: int
    receipt_number: str
    po_number: str
    po_id: int
    vendor_name: Optional[str] = None
    part_id: int
    part_number: str
    part_name: Optional[str] = None
    quantity_received: float
    lot_number: str
    cert_number: Optional[str] = None
    coc_attached: bool = False
    received_at: datetime
    received_by_name: Optional[str] = None
    location_code: Optional[str] = None
    days_pending: int = 0


class InspectionResultResponse(BaseModel):
    receipt: ReceiptResponse
    inventory_created: bool = False
    inventory_item_id: Optional[int] = None
    ncr_created: bool = False
    ncr_number: Optional[str] = None
    ncr_id: Optional[int] = None
