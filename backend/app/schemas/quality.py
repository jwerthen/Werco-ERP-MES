from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
from app.models.quality import NCRStatus, NCRDisposition, NCRSource, CARStatus, CARType, FAIStatus
from app.core.validation import Money, MoneySmall, DescriptionLong


# NCR Schemas
class NCRCreate(BaseModel):
    part_id: Optional[int] = Field(None, gt=0)
    work_order_id: Optional[int] = Field(None, gt=0)
    lot_number: Optional[str] = Field(None, max_length=50, description="Required for AS9100D traceability")
    serial_number: Optional[str] = Field(None, max_length=100)
    quantity_affected: Decimal = Field(default=Decimal('1.0'), gt=Decimal('0'), description="Quantity of affected parts")
    source: NCRSource = Field(..., description="Source of defect")
    title: str = Field(..., min_length=5, max_length=200, description="NCR title")
    description: DescriptionLong = Field(..., description="Detailed defect description")
    specification: Optional[str] = Field(None, max_length=1000, description="Reference specification")
    actual_value: Optional[str] = Field(None, max_length=500, description="Actual measurement/value")
    required_value: Optional[str] = Field(None, max_length=500, description="Required specification")
    supplier_name: Optional[str] = Field(None, max_length=255)
    supplier_lot: Optional[str] = Field(None, max_length=50)
    po_number: Optional[str] = Field(None, max_length=50)
    detected_date: Optional[date] = Field(None, description="Date defect was detected")

    @field_validator('lot_number', mode='before')
    @classmethod
    def uppercase_lot(cls, v: Optional[str]) -> Optional[str]:
        """Ensure lot number is uppercase"""
        return v.upper().strip() if v else v


class NCRUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    status: Optional[NCRStatus] = None
    disposition: Optional[NCRDisposition] = None
    quantity_rejected: Optional[Decimal] = Field(None, ge=Decimal('0'))
    root_cause: Optional[str] = Field(None, max_length=2000, description="Required for closure, 20-2000 chars")
    containment_action: Optional[str] = Field(None, max_length=2000)
    estimated_cost: Optional[Money] = None
    actual_cost: Optional[Money] = None
    assigned_to: Optional[int] = Field(None, gt=0)
    car_required: Optional[bool] = None
    car_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode='after')
    def validate_closure(self) -> 'NCRUpdate':
        """Ensure required fields for closure"""
        if self.status == NCRStatus.CLOSED:
            if not self.disposition:
                raise ValueError('Disposition required for NCR closure')
            if not self.root_cause or len(self.root_cause) < 20:
                raise ValueError('Root cause required (minimum 20 characters) for NCR closure')
        return self


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    
    class Config:
        from_attributes = True


class NCRResponse(BaseModel):
    id: int
    version: Optional[int] = 0
    ncr_number: str
    part_id: Optional[int]
    part: Optional[PartSummary] = None
    work_order_id: Optional[int]
    lot_number: Optional[str]
    serial_number: Optional[str]
    quantity_affected: MoneySmall
    quantity_rejected: MoneySmall
    source: NCRSource
    status: NCRStatus
    disposition: Optional[NCRDisposition]
    title: str
    description: str
    root_cause: Optional[str]
    containment_action: Optional[str]
    specification: Optional[str]
    actual_value: Optional[str]
    required_value: Optional[str]
    supplier_name: Optional[str]
    estimated_cost: Money
    actual_cost: Money
    detected_date: Optional[date]
    closed_date: Optional[date]
    car_required: bool
    car_id: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


# CAR Schemas
class CARCreate(BaseModel):
    car_type: CARType = Field(default=CARType.CORRECTIVE)
    priority: int = Field(default=3, ge=1, le=10, description="Priority (1=highest, 10=lowest)")
    title: str = Field(..., min_length=10, max_length=200)
    problem_description: DescriptionLong = Field(..., description="Detailed problem description")
    due_date: Optional[date] = None
    containment_due: Optional[date] = None
    corrective_due: Optional[date] = None

    @model_validator(mode='after')
    def validate_dates(self) -> 'CARCreate':
        """Validate date relationships"""
        today = date.today()

        if self.due_date and self.due_date < today:
            raise ValueError('Due date cannot be in the past')

        if self.containment_due and self.containment_due < today:
            raise ValueError('Containment due date cannot be in the past')

        if self.corrective_due and self.corrective_due < today:
            raise ValueError('Corrective due date cannot be in the past')

        return self


class CARUpdate(BaseModel):
    version: int = Field(..., ge=0)
    status: Optional[CARStatus] = None
    priority: Optional[int] = Field(None, ge=1, le=10)
    root_cause_analysis: Optional[str] = Field(None, max_length=2000)
    root_cause: Optional[str] = Field(None, max_length=2000)
    containment_action: Optional[str] = Field(None, max_length=2000)
    corrective_action: Optional[str] = Field(None, max_length=2000)
    preventive_action: Optional[str] = Field(None, max_length=2000)
    verification_method: Optional[str] = Field(None, max_length=2000)
    verification_results: Optional[str] = Field(None, max_length=2000)
    effectiveness_check: Optional[str] = Field(None, max_length=2000)
    assigned_to: Optional[int] = Field(None, gt=0)
    due_date: Optional[date] = None
    verification_due: Optional[date] = None


class CARResponse(BaseModel):
    id: int
    car_number: str
    car_type: CARType
    status: CARStatus
    priority: int
    title: str
    problem_description: str
    root_cause_analysis: Optional[str]
    root_cause: Optional[str]
    containment_action: Optional[str]
    corrective_action: Optional[str]
    preventive_action: Optional[str]
    verification_method: Optional[str]
    verification_results: Optional[str]
    due_date: Optional[date]
    closed_date: Optional[date]
    created_at: datetime
    
    class Config:
        from_attributes = True


# FAI Schemas
class FAICharacteristicCreate(BaseModel):
    char_number: int = Field(..., ge=1, description="Characteristic number")
    characteristic: str = Field(..., min_length=5, max_length=255, description="Characteristic description")
    nominal: Optional[str] = Field(None, max_length=100, description="Nominal value")
    tolerance_plus: Optional[str] = Field(None, max_length=50, description="Positive tolerance")
    tolerance_minus: Optional[str] = Field(None, max_length=50, description="Negative tolerance")
    specification: Optional[str] = Field(None, max_length=500, description="Reference specification")
    is_critical: bool = False
    is_major: bool = False


class FAICharacteristicUpdate(BaseModel):
    actual_value: Optional[str] = Field(None, max_length=100, description="Measured value")
    measuring_device: Optional[str] = Field(None, max_length=100, description="Equipment used")
    is_conforming: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=500, description="Inspection notes")


class FAICharacteristicResponse(BaseModel):
    id: int
    fai_id: int
    char_number: int
    characteristic: str
    nominal: Optional[str]
    tolerance_plus: Optional[str]
    tolerance_minus: Optional[str]
    specification: Optional[str]
    actual_value: Optional[str]
    measuring_device: Optional[str]
    is_conforming: Optional[bool]
    is_critical: bool
    is_major: bool
    notes: Optional[str]
    
    class Config:
        from_attributes = True


class FAICreate(BaseModel):
    part_id: int = Field(..., gt=0)
    part_revision: Optional[str] = Field(None, max_length=20, pattern=r'^[A-Z0-9]+$')
    work_order_id: Optional[int] = Field(None, gt=0)
    serial_number: Optional[str] = Field(None, max_length=100)
    fai_type: str = Field(default='full', max_length=50, description="Full or partial FAI")
    reason: Optional[str] = Field(None, max_length=500, description="Reason for FAI")
    due_date: Optional[date] = None
    customer_approval_required: bool = False


class FAIUpdate(BaseModel):
    version: int = Field(..., ge=0)
    status: Optional[FAIStatus] = None
    notes: Optional[str] = Field(None, max_length=2000)
    deviations: Optional[str] = Field(None, max_length=2000)
    inspection_date: Optional[date] = None
    inspector_id: Optional[int] = Field(None, gt=0)


class FAIResponse(BaseModel):
    id: int
    fai_number: str
    part_id: int
    part: Optional[PartSummary] = None
    part_revision: Optional[str]
    work_order_id: Optional[int]
    serial_number: Optional[str]
    fai_type: str
    reason: Optional[str]
    status: FAIStatus
    total_characteristics: int
    characteristics_passed: int
    characteristics_failed: int
    notes: Optional[str]
    deviations: Optional[str]
    inspection_date: Optional[date]
    due_date: Optional[date]
    completed_date: Optional[date]
    customer_approval_required: bool
    customer_approved: bool
    characteristics: List[FAICharacteristicResponse] = []
    created_at: datetime
    
    class Config:
        from_attributes = True
