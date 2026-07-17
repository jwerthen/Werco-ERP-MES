from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.validation import Money, MoneySmall
from app.schemas.base import UTCModel


def blank_str_to_none(v: object) -> object:
    """Coerce '' / whitespace-only strings to None (mode='before' helper).

    HTML date inputs submit an empty string when left blank; without this an
    Optional[date] field 422s instead of accepting the omitted value.
    """
    if isinstance(v, str) and not v.strip():
        return None
    return v


# Vendor schemas
class VendorBase(UTCModel):
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
    country: str = Field(default="US", max_length=3, pattern=r'^[A-Z]{2,3}$')
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

    @field_validator('country', mode='before')
    @classmethod
    def normalize_country(cls, v: str) -> str:
        if not isinstance(v, str):
            return v
        value = v.strip().upper()
        if value == "USA":
            return "US"
        return value


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    code: Optional[str] = Field(
        None, min_length=2, max_length=20, pattern=r'^[A-Z0-9\-]+$', description="Vendor code (unique)"
    )
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    contact_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    address_line1: Optional[str] = Field(None, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=2, pattern=r'^[A-Z]{2}$')
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=3, pattern=r'^[A-Z]{2,3}$')
    payment_terms: Optional[str] = Field(None, max_length=100)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    is_approved: Optional[bool] = None
    is_as9100_certified: Optional[bool] = None
    is_iso9001_certified: Optional[bool] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator('code', mode='before')
    @classmethod
    def uppercase_code(cls, v: Optional[str]) -> Optional[str]:
        """Ensure vendor code is uppercase (mirrors VendorBase; None passes through)"""
        return v.upper().strip() if isinstance(v, str) else v


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
class POLineBase(UTCModel):
    part_id: int = Field(..., gt=0)
    quantity_ordered: MoneySmall = Field(..., gt=Decimal("0"), description="Quantity to order")
    unit_price: Money = Field(..., ge=Decimal("0"), description="Unit price")
    required_date: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=500)

    @field_validator('required_date', mode='before')
    @classmethod
    def empty_dates_to_none(cls, v: object) -> object:
        """HTML forms submit '' for a blank date input — treat it as omitted."""
        return blank_str_to_none(v)


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
class POBase(UTCModel):
    vendor_id: int = Field(..., gt=0)
    required_date: Optional[date] = None
    expected_date: Optional[date] = None
    ship_to: Optional[str] = Field(None, max_length=255)
    shipping_method: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator('required_date', 'expected_date', mode='before')
    @classmethod
    def empty_dates_to_none(cls, v: object) -> object:
        """HTML forms submit '' for a blank date input — treat it as omitted."""
        return blank_str_to_none(v)

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

    @field_validator('required_date', 'expected_date', mode='before')
    @classmethod
    def empty_dates_to_none(cls, v: object) -> object:
        """HTML forms submit '' for a blank date input — treat it as omitted (parity with POBase)."""
        return blank_str_to_none(v)


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


class POListResponse(UTCModel):
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
    lot_number: Optional[str] = Field(
        None,
        max_length=50,
        description="Optional; auto-assigned from the receipt number when blank (AS9100D traceability preserved)",
    )
    serial_numbers: Optional[str] = Field(None, max_length=500)
    heat_number: Optional[str] = Field(None, max_length=50)
    cert_number: Optional[str] = Field(None, max_length=50)
    coc_attached: bool = False
    location_id: Optional[int] = Field(None, gt=0)
    # Owner-requested receiving default: an omitted flag means "no inspection
    # required" (dock-to-stock). The part master's Part.requires_inspection is
    # NOT applied automatically — it is surfaced on the /receiving/open-pos and
    # /receiving/po/{id} line payloads as an advisory hint in the receiving UI.
    requires_inspection: bool = Field(
        False,
        description=(
            "Defaults to false when omitted: the receipt is dock-to-stock (auto-accepted into "
            "inventory). Pass true to hold the lot in the inspection queue. The part master's "
            "requires_inspection flag is an advisory hint in the receiving UI, not an automatic "
            "server-side default."
        ),
    )
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


class ReceiptResponse(UTCModel):
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


class InspectionQueueItem(UTCModel):
    receipt_id: int
    receipt_number: str
    # PO / part context is Optional so one orphaned receipt row (missing PO line,
    # part, or purchase order) degrades to None fields instead of 500ing the
    # whole inspection queue.
    po_number: Optional[str] = None
    po_id: Optional[int] = None
    vendor_name: Optional[str] = None
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    quantity_received: MoneySmall
    lot_number: str
    cert_number: Optional[str] = None
    coc_attached: bool = False
    received_at: Optional[datetime] = None
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
