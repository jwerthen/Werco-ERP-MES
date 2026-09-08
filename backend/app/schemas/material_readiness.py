from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class MaterialSupplySource(BaseModel):
    kind: Literal["stock", "purchase_order"]
    id: int
    line_id: Optional[int] = None
    label: str
    quantity: float
    available_date: date
    expires_on: Optional[date] = None


class MaterialReadinessLine(BaseModel):
    part_id: Optional[int] = None
    part_number: str
    unit_of_measure: Optional[str] = None
    required_quantity: float
    covered_quantity: float
    shortage_quantity: float
    reason: Optional[str] = None
    sources: List[MaterialSupplySource] = Field(default_factory=list)


class MaterialReadiness(BaseModel):
    status: Literal["ready", "unknown", "not_defined"]
    ready_date: Optional[date] = None
    lines: List[MaterialReadinessLine] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    basis: str
