from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
from app.models.quality import NCRStatus, NCRDisposition, NCRSource, CARStatus, CARType, FAIStatus


# NCR Schemas
class NCRCreate(BaseModel):
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    quantity_affected: float = 1.0
    source: NCRSource
    title: str
    description: str
    specification: Optional[str] = None
    actual_value: Optional[str] = None
    required_value: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_lot: Optional[str] = None
    po_number: Optional[str] = None
    detected_date: Optional[date] = None


class NCRUpdate(BaseModel):
    status: Optional[NCRStatus] = None
    disposition: Optional[NCRDisposition] = None
    quantity_rejected: Optional[float] = None
    root_cause: Optional[str] = None
    containment_action: Optional[str] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    assigned_to: Optional[int] = None
    car_required: Optional[bool] = None
    car_id: Optional[int] = None


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    
    class Config:
        from_attributes = True


class NCRResponse(BaseModel):
    id: int
    ncr_number: str
    part_id: Optional[int]
    part: Optional[PartSummary] = None
    work_order_id: Optional[int]
    lot_number: Optional[str]
    serial_number: Optional[str]
    quantity_affected: float
    quantity_rejected: float
    source: NCRSource
    status: NCRStatus
    disposition: NCRDisposition
    title: str
    description: str
    root_cause: Optional[str]
    containment_action: Optional[str]
    specification: Optional[str]
    actual_value: Optional[str]
    required_value: Optional[str]
    supplier_name: Optional[str]
    estimated_cost: float
    actual_cost: float
    detected_date: Optional[date]
    closed_date: Optional[date]
    car_required: bool
    car_id: Optional[int]
    created_at: datetime
    
    class Config:
        from_attributes = True


# CAR Schemas
class CARCreate(BaseModel):
    car_type: CARType = CARType.CORRECTIVE
    priority: int = 3
    title: str
    problem_description: str
    due_date: Optional[date] = None
    containment_due: Optional[date] = None
    corrective_due: Optional[date] = None


class CARUpdate(BaseModel):
    status: Optional[CARStatus] = None
    priority: Optional[int] = None
    root_cause_analysis: Optional[str] = None
    root_cause: Optional[str] = None
    containment_action: Optional[str] = None
    corrective_action: Optional[str] = None
    preventive_action: Optional[str] = None
    verification_method: Optional[str] = None
    verification_results: Optional[str] = None
    effectiveness_check: Optional[str] = None
    assigned_to: Optional[int] = None
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
    char_number: int
    characteristic: str
    nominal: Optional[str] = None
    tolerance_plus: Optional[str] = None
    tolerance_minus: Optional[str] = None
    specification: Optional[str] = None
    is_critical: bool = False
    is_major: bool = False


class FAICharacteristicUpdate(BaseModel):
    actual_value: Optional[str] = None
    measuring_device: Optional[str] = None
    is_conforming: Optional[bool] = None
    notes: Optional[str] = None


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
    part_id: int
    part_revision: Optional[str] = None
    work_order_id: Optional[int] = None
    serial_number: Optional[str] = None
    fai_type: str = "full"
    reason: Optional[str] = None
    due_date: Optional[date] = None
    customer_approval_required: bool = False


class FAIUpdate(BaseModel):
    status: Optional[FAIStatus] = None
    notes: Optional[str] = None
    deviations: Optional[str] = None
    inspection_date: Optional[date] = None
    inspector_id: Optional[int] = None


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
