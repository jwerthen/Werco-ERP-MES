"""Consumable checkout contracts. Dates are server timestamps, never client input."""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConsumablePerson(BaseModel):
    id: int
    name: str
    department: Optional[str] = None
    is_active: bool


class ConsumableMetadata(BaseModel):
    can_manage: bool
    can_export: bool
    people: list[ConsumablePerson]


class ConsumableStock(BaseModel):
    inventory_item_id: Optional[int]
    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: str
    location: Optional[str]
    lot_number: Optional[str]
    quantity_on_hand: float
    quantity_available: float
    part_quantity_on_hand: float
    reorder_point: float
    is_low_stock: bool
    can_checkout: bool
    unavailable_reason: Optional[str]


class ConsumableStockList(BaseModel):
    items: list[ConsumableStock]
    total: int


class ConsumableCheckoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_item_id: int = Field(gt=0)
    employee_id: int = Field(gt=0)
    quantity: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    notes: Optional[str] = Field(default=None, max_length=2000)
    request_id: UUID


class ConsumableCheckoutResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    inventory_item_id: int
    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: str
    location: str
    lot_number: Optional[str]
    employee_id: int
    employee_name: str
    department: Optional[str]
    issued_by: int
    issued_by_name: str
    issued_at: datetime
    quantity: float
    notes: Optional[str]
    request_id: str
    replayed: bool = False

    @field_validator("issued_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        # SQLite discards timezone metadata; PostgreSQL returns aware timestamps.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ConsumableCheckoutList(BaseModel):
    items: list[ConsumableCheckoutResponse]
    total: int
