"""Request / response contracts for maintenance equipment assets and the PM schedule import."""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import UTCModel


def _strip_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


class MaintenanceAssetBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_tag: str = Field(..., min_length=1, max_length=50, description="Unique per company (case-insensitive).")
    name: str = Field(..., min_length=1, max_length=255)
    asset_type: Optional[str] = Field(None, max_length=50, description="Free text, e.g. forklift, air_compressor.")
    manufacturer: Optional[str] = Field(None, max_length=100)
    model: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    location: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=4000)

    @field_validator("asset_tag", "name")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("asset_type", "manufacturer", "model", "serial_number", "location", "notes")
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        return _strip_or_none(value)


class MaintenanceAssetCreate(MaintenanceAssetBase):
    pass


class MaintenanceAssetUpdate(BaseModel):
    """Only the fields sent are applied. ``version`` is the asset's current version (409 if stale).

    Retiring an asset is ``DELETE /maintenance/assets/{id}`` (or ``is_active: false`` here);
    ``is_active: true`` brings it back.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1)
    asset_tag: Optional[str] = Field(None, min_length=1, max_length=50)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    asset_type: Optional[str] = Field(None, max_length=50)
    manufacturer: Optional[str] = Field(None, max_length=100)
    model: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    location: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=4000)
    is_active: Optional[bool] = None

    @field_validator("asset_tag", "name")
    @classmethod
    def _required_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            raise ValueError("cannot be null")
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("is_active")
    @classmethod
    def _not_null(cls, value: Optional[bool]) -> Optional[bool]:
        if value is None:
            raise ValueError("cannot be null")
        return value

    @field_validator("asset_type", "manufacturer", "model", "serial_number", "location", "notes")
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        return _strip_or_none(value)


class MaintenanceAssetResponse(UTCModel):
    id: int
    asset_tag: str
    name: str
    asset_type: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool
    version: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    active_schedule_count: int = 0
    open_work_order_count: int = 0


# ---------------------------------------------------------------------------
# PM schedule import (template ``maintenance-schedules``)
# ---------------------------------------------------------------------------


class PmImportRowResult(UTCModel):
    row: int
    target_type: Optional[str] = None  # "work_center" | "asset"
    target_code: Optional[str] = None
    schedule_name: Optional[str] = None
    task: Optional[str] = None
    frequency: Optional[str] = None
    outcome: str  # "create" | "update" | "unchanged" | "skipped" | "error"
    warnings: List[str] = []
    error: Optional[str] = None


class PmImportFieldChange(UTCModel):
    old: Any = None
    new: Any = None


class PmImportScheduleResult(UTCModel):
    schedule_id: Optional[int] = None
    name: str
    target_type: str
    target_id: Optional[int] = None
    target_code: str
    target_name: Optional[str] = None
    frequency: str
    frequency_days: Optional[int] = None
    tasks: List[str]
    next_due_date: Optional[date] = None
    estimated_duration_hours: Optional[float] = None
    requires_shutdown: Optional[bool] = None
    priority: Optional[str] = None
    outcome: str  # "create" | "update" | "unchanged" | "error"
    changes: Dict[str, PmImportFieldChange] = {}
    rows: List[int]


class PmImportAssetResult(UTCModel):
    asset_id: Optional[int] = None
    asset_tag: str
    name: str
    asset_type: Optional[str] = None
    rows: List[int]


class PmImportSummary(UTCModel):
    total_rows: int
    task_rows: int
    skipped_rows: int
    error_count: int
    warning_count: int
    schedules_to_create: int
    schedules_to_update: int
    schedules_unchanged: int
    assets_to_create: int


class PmImportResponse(UTCModel):
    filename: Optional[str] = None
    source_hash: str
    committed: bool
    summary: PmImportSummary
    rows: List[PmImportRowResult]
    schedules: List[PmImportScheduleResult]
    assets_to_create: List[PmImportAssetResult]
