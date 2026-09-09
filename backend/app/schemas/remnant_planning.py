"""Read-only physical-piece planning evidence; no material or inventory approval."""

import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.stock_piece import ID, Hash, ObservationEvidence, decimal_value

MAX_SELECTION_BYTES = 256 * 1024
ADVISORY = 'Recorded piece — availability and eligibility unverified.'
Name = Annotated[str, Field(min_length=1, max_length=120)]
Note = Annotated[str, Field(min_length=1, max_length=1000)]
LegacyText = Annotated[str, Field(max_length=1024)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

    @model_validator(mode='before')
    @classmethod
    def literal_types(cls, value):
        if isinstance(value, dict):
            for key in ('version', 'payloadSchemaVersion', 'capacity'):
                if key in value and type(value[key]) is not int:
                    raise ValueError('Version and capacity fields require literal integers')
            for key in ('planningOnly', 'eligibilityVerified', 'availabilityVerified'):
                if key in value and type(value[key]) is not bool:
                    raise ValueError('Planning flags require literal booleans')
        return value


class SourceItem(StrictModel):
    id: ID
    company_id: ID
    part_id: ID
    location: LegacyText
    warehouse: LegacyText | None
    lot_number: LegacyText | None
    serial_number: LegacyText | None
    received_date: LegacyText | None
    supplier_id: ID | None
    po_number: LegacyText | None
    cert_number: LegacyText | None
    heat_lot: LegacyText | None
    expiration_date: LegacyText | None
    status: Literal['available']
    is_active: Literal[True]
    updated_at: LegacyText | None

    @field_validator('is_active', mode='before')
    @classmethod
    def active(cls, value):
        if value is not True:
            raise ValueError('Reported source must be active')
        return value


class SourcePart(StrictModel):
    id: ID
    company_id: ID
    part_number: LegacyText
    revision: LegacyText | None
    name: LegacyText
    part_type: LegacyText | None
    unit_of_measure: LegacyText | None
    is_active: Literal[True]
    is_deleted: Literal[False]
    updated_at: LegacyText | None

    @field_validator('is_active', 'is_deleted', mode='before')
    @classmethod
    def boolean_flags(cls, value):
        if type(value) is not bool:
            raise ValueError('Source status flags must be booleans')
        return value


class MovementWatermark(StrictModel):
    coverage: Literal['direct_item_and_unattributed_same_part']
    count: int = Field(ge=0, le=9007199254740991)
    max_id: ID | None
    max_created_at: LegacyText | None


class SourceEvidence(StrictModel):
    version: Literal[1]
    item: SourceItem
    part: SourcePart
    movement_watermark: MovementWatermark


class RemnantSnapshot(StrictModel):
    version: Literal[1]
    companyId: ID
    pieceId: ID
    label: Name
    observationNumber: ID
    state: Literal['RECORDED']
    observedAt: LegacyText
    observerName: Name
    reason: Note
    createdAt: LegacyText
    createdBy: ID
    submittedApiTokenId: ID | None
    payloadSchemaVersion: Literal[1]
    payloadSha256: Hash
    payloadBytes: int = Field(gt=0, le=128 * 1024)
    evidence: ObservationEvidence
    sourceInventoryItemId: ID
    sourcePartId: ID
    sourceSha256: Hash
    sourceEvidence: SourceEvidence

    @model_validator(mode='after')
    def bound_source(self):
        source = self.sourceEvidence
        if (
            source.item.company_id != self.companyId
            or source.part.company_id != self.companyId
            or source.item.id != self.sourceInventoryItemId
            or source.item.part_id != self.sourcePartId
            or source.part.id != self.sourcePartId
        ):
            raise ValueError('Remnant snapshot source identity does not agree')
        return self


class PlanningSnapshotRequest(StrictModel):
    expected_company_id: ID
    expected_payload_sha256: Hash
    expected_source_sha256: Hash


class PlanningSnapshotResponse(StrictModel):
    company_id: ID
    snapshot: RemnantSnapshot
    snapshot_sha256: Hash
    latest_observation_number: ID
    source_status: Literal['unchanged']
    current_source_sha256: Hash
    checked_at: str
    review_issues: list[str]
    advisory: Literal['Recorded piece — availability and eligibility unverified.'] = ADVISORY


class RemnantAssignment(StrictModel):
    version: Literal[1]
    basis: Literal['planner_declared_unverified']
    family: Literal['Carbon steel', 'Stainless steel', 'Aluminum']
    requiredGrade: Name
    thicknessIn: str
    reason: Note
    targetGroupSha256: Hash

    @field_validator('requiredGrade', 'reason')
    @classmethod
    def trimmed(cls, value):
        if value != value.strip() or not value:
            raise ValueError('Grade and reason must be nonempty trimmed text')
        return value

    @field_validator('thicknessIn', mode='before')
    @classmethod
    def thickness(cls, value):
        return decimal_value(value, positive=True)


class RemnantProfile(StrictModel):
    id: Literal['werco-remnant-domain-v1']
    sha256: Hash


class RemnantSelection(StrictModel):
    version: Literal[1]
    groupId: str = Field(min_length=1, max_length=200)
    snapshot: RemnantSnapshot
    snapshotSha256: Hash
    assignment: RemnantAssignment
    geometryProfile: RemnantProfile
    zoneClearanceIn: str
    capacity: Literal[1]
    planningOnly: Literal[True]
    eligibilityVerified: Literal[False]
    availabilityVerified: Literal[False]

    @field_validator('zoneClearanceIn', mode='before')
    @classmethod
    def clearance(cls, value):
        result = decimal_value(value)
        if not Decimal(0) <= Decimal(result) <= Decimal(100):
            raise ValueError('Zone clearance must be between zero and 100 inches')
        return result

    @model_validator(mode='after')
    def byte_budget(self):
        if (
            len(json.dumps(self.model_dump(mode='json'), ensure_ascii=False, separators=(',', ':')).encode())
            > MAX_SELECTION_BYTES
        ):
            raise ValueError('Remnant selection exceeds 256 KiB')
        return self
