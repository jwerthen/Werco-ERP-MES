"""Imperial, exact-decimal quoting allowances; never machine parameters."""

from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

QUANTUM = Decimal("0.000000001")
MAX_POLICY_BYTES = 65536
Family = Literal["Carbon steel", "Stainless steel", "Aluminum"]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Reason = Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]


def canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_number(value: str) -> str:
    if canonical_decimal(Decimal(value)) != value:
        raise ValueError("Use canonical decimals without leading/trailing zeroes")
    return value


Number = Annotated[
    str,
    Field(min_length=1, max_length=14, pattern=r"^(0|[1-9][0-9]{0,3})(\.[0-9]{1,9})?$"),
    AfterValidator(canonical_number),
]


def utc_timestamp(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("Use a UTC timestamp ending in Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Use a complete UTC date and time")
    return value


Timestamp = Annotated[
    str,
    Field(max_length=40, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"),
    AfterValidator(utc_timestamp),
]


def naive_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def normalize_thickness(value: str) -> str:
    with localcontext(Context(prec=50)):
        supplied = Decimal(value)
        if not Decimal(0) < supplied <= Decimal(4):
            raise ValueError("Thickness must be greater than zero and at most 4 inches")
        result = supplied.quantize(QUANTUM, rounding=ROUND_HALF_UP)
    if not Decimal(0) < result <= Decimal(4):
        raise ValueError("Normalized thickness must be greater than zero and at most 4 inches")
    return canonical_decimal(result)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def schema_integer(cls, value):
        if isinstance(value, dict) and "schema_version" in value and type(value["schema_version"]) is not int:
            raise ValueError("Schema versions must be integers")
        return value


class SpacingBand(StrictModel):
    id: Identifier
    material: Family
    thickness_min_in: Number
    thickness_max_in: Number
    minimum_gap_in: Number
    gap_thickness_multiplier: Number
    minimum_margin_in: Number
    margin_thickness_multiplier: Number

    @model_validator(mode="after")
    def bounded_formula(self):
        if not Decimal(0) <= Decimal(self.thickness_min_in) < Decimal(self.thickness_max_in) <= Decimal(4):
            raise ValueError("Thickness bands require 0 <= minimum < maximum <= 4 inches")
        for minimum, multiplier in (
            (self.minimum_gap_in, self.gap_thickness_multiplier),
            (self.minimum_margin_in, self.margin_thickness_multiplier),
        ):
            if not Decimal(0) <= Decimal(minimum) <= Decimal(100) or not Decimal(0) <= Decimal(multiplier) <= Decimal(
                100
            ):
                raise ValueError("Spacing minima and multipliers must be between 0 and 100")
            if Decimal(minimum) == 0 and Decimal(multiplier) == 0:
                raise ValueError("Each spacing formula must have a positive minimum or multiplier")
        return self


def resolve_band(band: SpacingBand, thickness: str) -> tuple[str, str]:
    """Round allowances outward on the pinned 1e-9 inch grid."""
    with localcontext(Context(prec=50)):
        t = Decimal(normalize_thickness(thickness))
        gap = max(Decimal(band.minimum_gap_in), t * Decimal(band.gap_thickness_multiplier))
        margin = max(Decimal(band.minimum_margin_in), t * Decimal(band.margin_thickness_multiplier))
        return (
            canonical_decimal(gap.quantize(QUANTUM, rounding=ROUND_CEILING)),
            canonical_decimal(margin.quantize(QUANTUM, rounding=ROUND_CEILING)),
        )


class SpacingContent(StrictModel):
    schema_version: Literal[1]
    units: Literal["in"]
    name: Annotated[str, Field(min_length=1, max_length=199, pattern=r"\S")]
    bands: list[SpacingBand] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unambiguous_bands(self):
        if len({band.id for band in self.bands}) != len(self.bands):
            raise ValueError("Band IDs must be unique")
        previous = {}
        for band in sorted(self.bands, key=lambda item: (item.material, Decimal(item.thickness_min_in))):
            if band.material in previous and Decimal(band.thickness_min_in) < previous[band.material]:
                raise ValueError("Thickness bands for a material family must not overlap")
            previous[band.material] = Decimal(band.thickness_max_in)
        return self


class SpacingPolicySnapshot(StrictModel):
    schema_version: Literal[1]
    company_id: PositiveId
    policy_id: PositiveId
    publication_id: PositiveId
    revision_id: PositiveId
    revision_number: PositiveId
    content_sha256: Digest
    band: SpacingBand
    thickness_in: Number
    gap_in: Number
    margin_in: Number
    resolved_at: Timestamp

    @model_validator(mode="after")
    def formula_agrees(self):
        thickness = Decimal(self.thickness_in)
        if not Decimal(self.band.thickness_min_in) <= thickness < Decimal(self.band.thickness_max_in) or thickness <= 0:
            raise ValueError("Thickness does not match the selected policy band")
        if resolve_band(self.band, self.thickness_in) != (self.gap_in, self.margin_in):
            raise ValueError("Resolved spacing does not match the policy formula")
        return self


class SpacingOverride(StrictModel):
    schema_version: Literal[1]
    reason: Reason
    changed_at: Timestamp


class PolicyCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_company_id: PositiveId
    expected_version: Annotated[int, Field(strict=True, ge=0, le=2147483647)]
    request_key: UUID
    reason: Reason


class CreateSpacingRevision(PolicyCommand):
    content: SpacingContent


class PublishSpacingRevision(PolicyCommand):
    revision_number: PositiveId
    content_sha256: Digest
    effective_at: Timestamp | None


class WithdrawSpacingPublication(PolicyCommand):
    pass


class ResolveSpacingRequest(StrictModel):
    material: Family
    thickness_in: Annotated[str, Field(max_length=24, pattern=r"^[0-9]{1,4}(\.[0-9]{1,18})?$")]


class SpacingRevisionSummary(BaseModel):
    id: int
    company_id: int
    policy_id: int
    revision_number: int
    name: str
    content_sha256: str
    payload_schema_version: int
    payload_bytes: int
    created_by: int
    created_at: str


class SpacingRevisionResponse(SpacingRevisionSummary):
    schema_version: Literal[1] = 1
    content: SpacingContent


class SpacingWithdrawalResponse(BaseModel):
    id: int
    created_by: int
    created_at: str
    reason: str


class SpacingPublicationResponse(BaseModel):
    id: int
    company_id: int
    policy_id: int
    policy_version: int
    revision_id: int
    revision_number: int
    content_sha256: str
    effective_at: str
    created_by: int
    created_at: str
    reason: str
    status: Literal["scheduled", "current", "superseded", "withdrawn"]
    withdrawal: SpacingWithdrawalResponse | None


class SpacingPolicyHeader(BaseModel):
    id: int
    company_id: int
    version: int
    latest_revision_number: int
    created_by: int
    created_at: str
    updated_at: str


class SpacingStateResponse(BaseModel):
    schema_version: Literal[1] = 1
    policy: SpacingPolicyHeader | None
    current_publication: SpacingPublicationResponse | None
    revisions: list[SpacingRevisionSummary]
    publications: list[SpacingPublicationResponse]
    total_revisions: int
    total_publications: int
    page: int
    per_page: int


class SpacingCommandResponse(BaseModel):
    schema_version: Literal[1] = 1
    policy_version: int
    event_id: int
    revision: SpacingRevisionSummary
    publication: SpacingPublicationResponse | None


class SpacingResolutionResponse(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["resolved", "unmatched", "unavailable"]
    policy: SpacingPolicySnapshot | None
    explanation: str
