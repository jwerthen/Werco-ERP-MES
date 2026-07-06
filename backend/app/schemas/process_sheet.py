"""Pydantic contracts for the Process Sheets library API (PR 1).

Per-type ``config`` shape validation (measurement lsl/nominal/usl, list options,
INSTRUCTION never required, requires_gauge only on MEASUREMENT) lives in
``services/process_sheet_service.py`` — the service is the single source of truth so
update paths that merge partial payloads validate the *effective* step, not the delta.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.models.process_sheet import ProcessSheetStatus, StepType
from app.schemas.base import UTCModel


def _reject_explicit_null(value: Any, info: ValidationInfo) -> Any:
    """Reject an explicit JSON null on an update field backing a NOT NULL column.

    ``Optional[...]`` on the update schemas means "may be omitted" (PATCH semantics),
    not "may be null". Pydantic 2 skips validation of defaults (``validate_default``
    is False), so this fires only when the client actually sent ``null`` — an omitted
    field keeps its None default, is dropped by ``exclude_unset``, and never gets here.
    Without this, an explicit null survives ``exclude_unset`` and ``setattr``s None
    onto a NOT NULL column (IntegrityError 500) or flows None into the step-type
    validation, producing a misleading 400.
    """
    if value is None:
        raise ValueError(f"{info.field_name} cannot be null")
    return value


# ---------- Steps ----------


class ProcessSheetStepCreate(BaseModel):
    sequence: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=255)
    instruction_text: Optional[str] = None
    step_type: StepType
    is_required: bool = True
    config: Optional[Dict[str, Any]] = None
    requires_gauge: bool = False
    spc_characteristic_id: Optional[int] = None


class ProcessSheetStepUpdate(BaseModel):
    sequence: Optional[int] = Field(default=None, gt=0)
    label: Optional[str] = Field(default=None, min_length=1, max_length=255)
    instruction_text: Optional[str] = None
    step_type: Optional[StepType] = None
    is_required: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    requires_gauge: Optional[bool] = None
    spc_characteristic_id: Optional[int] = None

    # instruction_text / config / spc_characteristic_id map to NULLABLE columns, so an
    # explicit null legitimately clears them and is NOT rejected here.
    _no_null = field_validator("sequence", "label", "step_type", "is_required", "requires_gauge")(_reject_explicit_null)


class ProcessSheetStepResponse(UTCModel):
    id: int
    process_sheet_id: int
    sequence: int
    label: str
    instruction_text: Optional[str] = None
    step_type: str
    is_required: bool
    config: Optional[Dict[str, Any]] = None
    requires_gauge: bool
    spc_characteristic_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ---------- Sheets ----------


class ProcessSheetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None


class ProcessSheetUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None

    # description maps to a NULLABLE column (explicit null clears it); title is NOT NULL.
    _no_null = field_validator("title")(_reject_explicit_null)


class ProcessSheetResponse(UTCModel):
    id: int
    sheet_number: str
    title: str
    description: Optional[str] = None
    revision: str
    status: str
    effective_date: Optional[datetime] = None
    obsolete_date: Optional[datetime] = None
    is_active: bool
    version: int
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    steps: List[ProcessSheetStepResponse] = Field(default_factory=list)


class ProcessSheetListResponse(UTCModel):
    id: int
    sheet_number: str
    title: str
    revision: str
    status: str
    is_active: bool
    effective_date: Optional[datetime] = None
    step_count: int = 0
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ProcessSheetStatus",
    "StepType",
    "ProcessSheetCreate",
    "ProcessSheetUpdate",
    "ProcessSheetStepCreate",
    "ProcessSheetStepUpdate",
    "ProcessSheetStepResponse",
    "ProcessSheetResponse",
    "ProcessSheetListResponse",
]
