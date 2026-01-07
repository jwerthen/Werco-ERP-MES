from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional
from datetime import datetime
from decimal import Decimal
from app.models.part import PartType, UnitOfMeasure
from app.core.validation import (
    PartNumber,
    Revision,
    DescriptionShort,
    NonNegativeInteger,
    Money,
    OptionalMoney,
    MoneySmall
)


class PartBase(BaseModel):
    part_number: PartNumber
    revision: Revision = "A"
    name: str = Field(min_length=2, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    part_type: PartType = Field(..., description="Type of part")
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.EACH
    
    # Costing (all optional and non-negative)
    standard_cost: Money = Field(default=Decimal("0.0"))
    material_cost: Money = Field(default=Decimal("0.0"))
    labor_cost: Money = Field(default=Decimal("0.0"))
    overhead_cost: Money = Field(default=Decimal("0.0"))
    
    # Lead time
    lead_time_days: NonNegativeInteger = Field(default=0)
    
    # Inventory (all optional and non-negative)
    safety_stock: MoneySmall = Field(default=Decimal("0.0"))
    reorder_point: MoneySmall = Field(default=Decimal("0.0"))
    reorder_quantity: MoneySmall = Field(default=Decimal("0.0"))
    
    # Classification
    is_critical: bool = False
    requires_inspection: bool = True
    inspection_requirements: Optional[str] = Field(None, max_length=2000)
    
    # Customer info
    customer_part_number: Optional[str] = Field(None, max_length=100)
    drawing_number: Optional[str] = Field(None, max_length=100)

    @field_validator('part_number', mode='before')
    @classmethod
    def uppercase_part_number(cls, v: str) -> str:
        """Ensure part number is uppercase"""
        return v.upper().strip() if isinstance(v, str) else str(v)

    @field_validator('revision', mode='before')
    @classmethod
    def uppercase_revision(cls, v: str) -> str:
        """Ensure revision is uppercase"""
        return v.upper().strip() if isinstance(v, str) else str(v)

    @model_validator(mode='after')
    def validate_consistency(self) -> 'PartBase':
        """Ensure data consistency"""
        # Reorder quantity should be set if reorder point is set
        if self.reorder_point > 0 and self.reorder_quantity == 0:
            raise ValueError('Reorder quantity must be greater than 0 when reorder point is set')

        return self


class PartCreate(PartBase):
    pass


class PartUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    revision: Optional[str] = Field(None, min_length=1, max_length=20, pattern=r'^[A-Z0-9]+$')
    description: Optional[str] = Field(None, max_length=2000)
    unit_of_measure: Optional[UnitOfMeasure] = None
    standard_cost: Optional[Decimal] = Field(None, ge=0, max_digits=8, decimal_places=2)
    material_cost: Optional[Decimal] = Field(None, ge=0, max_digits=8, decimal_places=2)
    labor_cost: Optional[Decimal] = Field(None, ge=0, max_digits=8, decimal_places=2)
    overhead_cost: Optional[Decimal] = Field(None, ge=0, max_digits=8, decimal_places=2)
    lead_time_days: Optional[int] = Field(None, ge=0, le=365)
    safety_stock: Optional[Decimal] = Field(None, ge=0, max_digits=10, decimal_places=4)
    reorder_point: Optional[Decimal] = Field(None, ge=0, max_digits=10, decimal_places=4)
    reorder_quantity: Optional[Decimal] = Field(None, ge=0, max_digits=10, decimal_places=4)
    is_critical: Optional[bool] = None
    requires_inspection: Optional[bool] = None
    inspection_requirements: Optional[str] = Field(None, max_length=2000)
    customer_part_number: Optional[str] = Field(None, max_length=100)
    drawing_number: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None
    status: Optional[str] = Field(None, max_length=50)

    @field_validator('revision', mode='before')
    @classmethod
    def uppercase_revision(cls, v: Optional[str]) -> Optional[str]:
        """Ensure revision is uppercase"""
        return v.upper().strip() if v else v


class PartResponse(PartBase):
    id: int
    version: int
    is_active: bool
    status: str
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True
