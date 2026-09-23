"""Bounded operational evidence and reviewed commands for Hank."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.base import UTCModel
from app.schemas.purchasing import ReceiptCreate
from app.schemas.shipping import ShipmentCreate
from app.schemas.shop_floor_commands import OperationHoldRequest, ProductionReportRequest


class HankEvidenceReference(BaseModel):
    type: str
    id: int
    label: str
    url: str


class HankOperationalCheck(BaseModel):
    key: str
    title: str
    status: Literal['satisfied', 'attention', 'unknown', 'info']
    detail: str
    references: list[HankEvidenceReference] = Field(default_factory=list)


class HankOperationalReport(UTCModel):
    company_id: int
    checked_at: datetime
    title: str
    summary: str
    checks: list[HankOperationalCheck] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    draft_text: str | None = None


class HankReceiptLine(ReceiptCreate):
    model_config = ConfigDict(extra='forbid')
    requires_inspection: bool


class ReceiveDeliveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purchase_order_id: int = Field(gt=0)
    lines: list[HankReceiptLine] = Field(min_length=1, max_length=50)
    source_intake_file_id: int | None = Field(default=None, gt=0)
    source_intake_version: int | None = Field(default=None, ge=1)
    acknowledge_duplicate_source: bool = False

    @model_validator(mode='after')
    def no_duplicate_lines(self):
        if (self.source_intake_file_id is None) != (self.source_intake_version is None):
            raise ValueError('Provide both the intake source file and its reviewed version')
        if self.acknowledge_duplicate_source and self.source_intake_file_id is None:
            raise ValueError('Duplicate source acknowledgement requires an intake source')
        ids = [line.po_line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError('Enter each purchase order line once per delivery')
        return self


class HankOperationHold(OperationHoldRequest):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(min_length=1, max_length=2000)
    source: None = None

    @field_validator('note')
    @classmethod
    def require_reason(cls, value):
        if not value.strip():
            raise ValueError('A hold reason is required')
        return value


class ReportProductionInput(ProductionReportRequest):
    model_config = ConfigDict(extra='forbid')
    operation_id: int = Field(gt=0)
    # Idempotency identity and source are server-owned for Hank actions.
    request_id: None = None
    source: None = None
    quantity_complete_delta: float = Field(default=0, ge=0, allow_inf_nan=False)
    quantity_scrapped_delta: float = Field(default=0, ge=0, allow_inf_nan=False)
    notes: str | None = Field(default=None, max_length=2000)
    hold: HankOperationHold | None = None

    @model_validator(mode='after')
    def valid_production(self):
        if self.quantity_complete_delta == 0 and self.quantity_scrapped_delta == 0:
            raise ValueError('Enter a good or scrap quantity')
        if self.open_ncr and self.quantity_scrapped_delta <= 0:
            raise ValueError('Opening an NCR requires reported scrap')
        return self


class DraftShipmentInput(ShipmentCreate):
    model_config = ConfigDict(extra='forbid')
    work_order_id: int = Field(gt=0)
    cert_of_conformance: Literal[False] = False
    ship_to_name: str | None = Field(default=None, max_length=255)
    ship_to_address: str | None = Field(default=None, max_length=500)
    ship_to_city: str | None = Field(default=None, max_length=100)
    ship_to_state: str | None = Field(default=None, max_length=100)
    ship_to_zip: str | None = Field(default=None, max_length=30)
    carrier: str | None = Field(default=None, max_length=100)
    service_type: str | None = Field(default=None, max_length=100)
    packing_notes: str | None = Field(default=None, max_length=2000)
    num_packages: int = Field(default=1, ge=1, le=1000)
