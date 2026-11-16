"""Customer export-control contracts (Claude-plugin brief P0.3): the ADMIN write path's contracts.

A customer is export-controlled when any of its ``customer_contracts`` rows is ITAR / export
controlled / CUI (see ``app.services.export_control_service``). These schemas are the only
way the app writes those rows. Every write carries a ``reason`` -- it lands on the required
audit row -- and there is no delete: un-marking a customer is a ``PUT`` that clears the flags.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.governance import DataClassification
from app.schemas.base import UTCModel

REASON_MAX_LENGTH = 1000


def _required_reason(value: str) -> str:
    stripped = (value or "").strip()
    if not stripped:
        raise ValueError("A reason is required")
    return stripped


def _optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CustomerContractCreate(BaseModel):
    """Add a contract (and with it, possibly, export control) to a customer."""

    model_config = ConfigDict(extra="forbid")

    contract_name: str = Field(..., min_length=1, max_length=255)
    contract_number: Optional[str] = Field(None, max_length=120)
    itar_controlled: bool = False
    export_controlled: bool = False
    contains_cui: bool = False
    default_data_classification: DataClassification = DataClassification.INTERNAL
    effective_date: Optional[datetime] = None
    expiration_date: Optional[datetime] = None
    active: bool = True
    handling_instructions: Optional[str] = Field(None, max_length=10_000)
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_LENGTH)

    _reason = field_validator("reason")(_required_reason)
    _number = field_validator("contract_number", "handling_instructions")(_optional_text)

    @field_validator("contract_name")
    @classmethod
    def _name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("contract_name must not be blank")
        return stripped


class CustomerContractUpdate(BaseModel):
    """Change a contract. Only the supplied fields change; ``reason`` is always required."""

    model_config = ConfigDict(extra="forbid")

    contract_name: Optional[str] = Field(None, min_length=1, max_length=255)
    contract_number: Optional[str] = Field(None, max_length=120)
    itar_controlled: Optional[bool] = None
    export_controlled: Optional[bool] = None
    contains_cui: Optional[bool] = None
    default_data_classification: Optional[DataClassification] = None
    effective_date: Optional[datetime] = None
    expiration_date: Optional[datetime] = None
    active: Optional[bool] = None
    handling_instructions: Optional[str] = Field(None, max_length=10_000)
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_LENGTH)

    _reason = field_validator("reason")(_required_reason)
    _number = field_validator("contract_number", "handling_instructions")(_optional_text)

    @field_validator("contract_name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        # Runs only when the field is SENT (an omitted field keeps its value). An explicit
        # null on this NOT NULL column is a 422 -- it used to reach the flush and come back
        # as a misleading 409 "contract number already exists" (code review P0, finding 6).
        if value is None:
            raise ValueError("must not be null")
        stripped = value.strip()
        if not stripped:
            raise ValueError("contract_name must not be blank")
        return stripped

    @field_validator("itar_controlled", "export_controlled", "contains_cui", "active", "default_data_classification")
    @classmethod
    def _not_null(cls, value):
        # Explicit null on a NOT NULL column is a 422, not a 500 at flush.
        if value is None:
            raise ValueError("must not be null")
        return value


class CustomerContractResponse(UTCModel):
    id: int
    customer_id: Optional[int] = None
    contract_number: Optional[str] = None
    contract_name: str
    itar_controlled: bool
    export_controlled: bool
    contains_cui: bool
    default_data_classification: str
    effective_date: Optional[datetime] = None
    expiration_date: Optional[datetime] = None
    active: bool
    handling_instructions: Optional[str] = None
    # Does THIS contract make its customer export-controlled?
    controlling: bool = False
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CustomerExportControlResponse(UTCModel):
    """A customer's export-control standing and every contract recorded for it."""

    customer_id: int
    customer_name: str
    customer_is_deleted: bool = False
    # True when ANY contract (any active flag, any dates) or classified handling instruction
    # controls the customer -- the exact rule AI-external tokens are redacted by.
    controlled: bool
    data_classification: Optional[str] = None
    controlling_contract_id: Optional[int] = None
    contracts: List[CustomerContractResponse]
