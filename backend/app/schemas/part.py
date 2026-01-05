from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models.part import PartType, UnitOfMeasure


class PartBase(BaseModel):
    part_number: str
    name: str
    part_type: PartType
    revision: str = "A"
    description: Optional[str] = None
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.EACH
    standard_cost: float = 0.0
    material_cost: float = 0.0
    labor_cost: float = 0.0
    overhead_cost: float = 0.0
    lead_time_days: int = 0
    safety_stock: float = 0.0
    reorder_point: float = 0.0
    reorder_quantity: float = 0.0
    is_critical: bool = False
    requires_inspection: bool = True
    inspection_requirements: Optional[str] = None
    customer_part_number: Optional[str] = None
    drawing_number: Optional[str] = None


class PartCreate(PartBase):
    pass


class PartUpdate(BaseModel):
    name: Optional[str] = None
    revision: Optional[str] = None
    description: Optional[str] = None
    unit_of_measure: Optional[UnitOfMeasure] = None
    standard_cost: Optional[float] = None
    material_cost: Optional[float] = None
    labor_cost: Optional[float] = None
    overhead_cost: Optional[float] = None
    lead_time_days: Optional[int] = None
    safety_stock: Optional[float] = None
    reorder_point: Optional[float] = None
    reorder_quantity: Optional[float] = None
    is_critical: Optional[bool] = None
    requires_inspection: Optional[bool] = None
    inspection_requirements: Optional[str] = None
    customer_part_number: Optional[str] = None
    drawing_number: Optional[str] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None


class PartResponse(PartBase):
    id: int
    is_active: bool
    status: str
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True
