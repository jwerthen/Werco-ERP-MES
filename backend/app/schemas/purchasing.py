from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
from app.core.validation import MoneySmall, Money, DescriptionLong


# Vendor schemas
class VendorBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=20, pattern=r'^[A-Z0-9\-]+$', description="Vendor code (unique)")
    name: str = Field(..., min_length=2, max_length=200, description="Vendor name")
    contact_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    address_line1: Optional[str] = Field(None, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=2, pattern=r'^[A-Z]{2}$', description="2-letter state code")
    postal_code: Optional[str] = Field(None, max_length=20)
    country: str = Field(default="USA", max_length=2, pattern=r'^[A-Z]{2}$')
    payment_terms: Optional[str] = Field(None, max_length=100)
    lead_time_days: int = Field(default=14, ge=0, le=365, description="Default lead time in days")
    is_approved: bool = False
    is_as9100_certified: bool = False
    is_iso9001_certified: bool = False
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator('code', mode='before')
    @classmethod
    def uppercase_code(cls, v: str) -> str:
        """Ensure vendor code is uppercase"""
        return v.upper().strip() if isinstance(v, str) else v


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    contact_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    address_line1: Optional[str] = Field(None, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=2, pattern=r'^[A-Z]{2}$')
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=2, pattern=r'^[A-Z]{2}$')
    payment_terms: Optional[str] = Field(None, max_length=100)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    is_approved: Optional[bool] = None
    is_as9100_certified: Optional[bool] = None
    is_iso9001_certified: Optional[bool] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=2000)


class VendorResponse(VendorBase):
    id: int
    version: Optional[int] = 0
    is_active: bool
    approval_date: Optional[date] = None
    quality_rating: Optional[float] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# PO Line schemas
class POLineBase(BaseModel):
    part_id: int = Field(..., gt=0)
    quantity_ordered: MoneySmall = Field(..., gt=Decimal("0"), description="Quantity to order")
    unit_price: Money = Field(..., ge=Decimal("0"), description="Unit price")
    required_date: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=500)


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
    quantity_received: MoneySmall
    line_total: Money
    is_closed: bool
    part: Optional[PartSummary] = None
    created_at: datetime

    class Config:
        from_attributes = True


# PO schemas
class POBase(BaseModel):
    vendor_id: int = Field(..., gt=0)
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    ship_to: Optional[str] = Field(None, max_length=255)
    shipping_method: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode='after')
    def validate_dates(self) -> 'POBase':
        """Validate date relationships"""
        if self.expected_date and self.required_date:
            if self.expected_date <= self.required_date:
                raise ValueError('Expected date must be after required date')

        return self


class POCreate(POBase):
    lines: List[POLineCreate] = Field(default_factory=list)


class POUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    ship_to: Optional[str] = Field(None, max_length=255)
    shipping_method: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=2000)
    status: Optional[str] = None


class VendorSummary(BaseModel):
    id: int
    code: str
    name: str

    class Config:
        from_attributes = True


class POResponse(POBase):
    id: int
    version: Optional[int] = 0
    po_number: str
    status: str
    order_date: Optional[date] = None
    subtotal: Money
    tax: Money
    shipping: Money
    total: Money
    vendor: Optional[VendorSummary] = None
    lines: List[POLineResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: Optional[datetime] = None

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
    total: Money
    line_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        use_enum_values = True


# Receipt schemas
class ReceiptCreate(BaseModel):
    po_line_id: int = Field(..., gt=0)
    quantity_received: MoneySmall = Field(..., gt=Decimal("0"))
    lot_number: str = Field(..., min_length=1, max_length=50, description="Required for AS9100D traceability")
    serial_numbers: Optional[str] = Field(None, max_length=500)
    heat_number: Optional[str] = Field(None, max_length=50)
    cert_number: Optional[str] = Field(None, max_length=50)
    coc_attached: bool = False
    location_id: Optional[int] = Field(None, gt=0)
    requires_inspection: bool = True
    packing_slip_number: Optional[str] = Field(None, max_length=50)
    carrier: Optional[str] = Field(None, max_length=100)
    tracking_number: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=2000)
    over_receive_approved: bool = False


class ReceiptInspection(BaseModel):
    quantity_accepted: MoneySmall = Field(..., ge=Decimal("0"))
    quantity_rejected: MoneySmall = Field(default=Decimal("0"), ge=Decimal("0"))
    inspection_method: str = Field(..., description="visual, dimensional, functional, documentation_review, etc.")
    defect_type: Optional[str] = Field(None, max_length=100, description="Required if quantity_rejected > 0")
    inspection_notes: Optional[str] = Field(None, max_length=2000, description="Required if quantity_rejected > 0")


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
    quantity_received: MoneySmall
    quantity_accepted: MoneySmall
    quantity_rejected: MoneySmall
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
    quantity_received: MoneySmall
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
