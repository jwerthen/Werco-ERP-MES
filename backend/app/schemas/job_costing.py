"""Job-cost HTTP contracts; permission/tenant checks are enforced by the router."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.job_costing import CostEntrySource, CostEntryType, JobCostStatus


class JobCostCreate(BaseModel):
    work_order_id: int
    estimated_material_cost: float = 0.0
    estimated_labor_cost: float = 0.0
    estimated_overhead_cost: float = 0.0
    revenue: float = 0.0
    notes: Optional[str] = None


class JobCostUpdate(BaseModel):
    estimated_material_cost: Optional[float] = None
    estimated_labor_cost: Optional[float] = None
    estimated_overhead_cost: Optional[float] = None
    revenue: Optional[float] = None
    status: Optional[JobCostStatus] = None
    notes: Optional[str] = None

    @field_validator(
        "estimated_material_cost",
        "estimated_labor_cost",
        "estimated_overhead_cost",
        "revenue",
        "status",
        mode="before",
    )
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("This field cannot be null")
        return value


class CostEntryCreate(BaseModel):
    entry_type: CostEntryType
    description: str
    quantity: float = 1.0
    unit_cost: float = 0.0
    work_order_operation_id: Optional[int] = None
    source: CostEntrySource = CostEntrySource.MANUAL
    reference: Optional[str] = None
    entry_date: date


class CostEntryResponse(BaseModel):
    id: int
    job_cost_id: int
    entry_type: str
    description: str
    quantity: float
    unit_cost: float
    total_cost: float
    work_order_operation_id: Optional[int] = None
    source: str
    reference: Optional[str] = None
    entry_date: date
    created_by: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobCostResponse(BaseModel):
    id: int
    work_order_id: int
    estimated_material_cost: float
    estimated_labor_cost: float
    estimated_overhead_cost: float
    estimated_total_cost: float
    actual_material_cost: float
    actual_labor_cost: float
    actual_overhead_cost: float
    actual_total_cost: float
    material_variance: float
    labor_variance: float
    overhead_variance: float
    total_variance: float
    margin_amount: float
    margin_percent: float
    revenue: float
    status: str
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # Enriched fields from work order
    work_order_number: Optional[str] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    customer_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
