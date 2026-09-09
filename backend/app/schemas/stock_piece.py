"""Bounded reported measurements in exact canonical inches; never verified stock."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_PAYLOAD_BYTES = 131072
MAX_SOURCE_VERTICES = 2000
MAX_ZONES = 16
ADVISORY = 'Recorded observation — availability and eligibility unverified.'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


ID = Annotated[StrictInt, Field(gt=0, le=2147483647)]
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Text120 = Annotated[str, Field(min_length=1, max_length=120)]
Note = Annotated[str, Field(min_length=1, max_length=1000)]


def decimal_value(value: Any, *, positive: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,9})?', value):
        raise ValueError('Use a canonical inch decimal string with at most nine fractional digits')
    if value == '-0' or ('.' in value and value.endswith('0')):
        raise ValueError('Decimal strings must omit trailing zeros and negative zero')
    number = Decimal(value)
    if number.copy_abs() > Decimal('100000') or (positive and number <= 0):
        raise ValueError('Dimensions must be positive and all coordinates bounded to 100000 inches')
    return value


class Point(StrictModel):
    x: str
    y: str
    _decimals = field_validator('x', 'y', mode='before')(decimal_value)


class Circle(StrictModel):
    kind: Literal['circle']
    cx: str
    cy: str
    r: str
    _coordinates = field_validator('cx', 'cy', mode='before')(decimal_value)

    @field_validator('r', mode='before')
    @classmethod
    def radius(cls, value):
        return decimal_value(value, positive=True)


class PolygonLoop(StrictModel):
    kind: Literal['polygon']
    pts: Annotated[list[Point], Field(min_length=3, max_length=2000)]


Loop = Annotated[Union[Circle, PolygonLoop], Field(discriminator='kind')]


class UnknownShape(StrictModel):
    kind: Literal['unknown']


class Rectangle(StrictModel):
    kind: Literal['rectangle']
    width: str
    height: str

    @field_validator('width', 'height', mode='before')
    @classmethod
    def dimensions(cls, value):
        return decimal_value(value, positive=True)


class PolygonShape(StrictModel):
    kind: Literal['polygon']
    outer: Annotated[list[Point], Field(min_length=3, max_length=2000)]
    holes: Annotated[list[Annotated[list[Point], Field(min_length=3, max_length=2000)]], Field(max_length=16)]


Shape = Annotated[Union[UnknownShape, Rectangle, Circle, PolygonShape], Field(discriminator='kind')]


class UnavailableZone(StrictModel):
    id: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')]
    label: Text120
    reason: Note
    outline: Loop

    @field_validator('label', 'reason')
    @classmethod
    def trimmed(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('Text must be nonempty and trimmed')
        return value


class ObservationEvidence(StrictModel):
    version: Literal[1]
    unit: Literal['in']
    measurement_method: Text120
    source_units: Literal['in', 'mm', 'unknown']
    geometry: Shape
    unavailable_zones: Annotated[list[UnavailableZone], Field(max_length=MAX_ZONES)]
    thickness: str | None
    grade: Text120 | None
    grain_axis: Literal['x', 'y'] | None
    location_note: Note | None
    ownership_note: Note | None
    certification_note: Note | None

    @field_validator('version', mode='before')
    @classmethod
    def literal_version(cls, value):
        if type(value) is not int:
            raise ValueError('version must be the integer 1')
        return value

    @field_validator('thickness', mode='before')
    @classmethod
    def thickness_value(cls, value):
        return None if value is None else decimal_value(value, positive=True)

    @field_validator('measurement_method', 'grade', 'location_note', 'ownership_note', 'certification_note')
    @classmethod
    def trimmed(cls, value):
        if value is not None and (value != value.strip() or not value.strip()):
            raise ValueError('Use trimmed text or explicit null for unknown evidence')
        return value

    @model_validator(mode='after')
    def budget(self):
        shape = self.geometry
        count = 0
        if isinstance(shape, PolygonShape):
            count = len(shape.outer) + sum(len(ring) for ring in shape.holes)
        elif isinstance(shape, Rectangle):
            count = 4
        elif isinstance(shape, Circle):
            count = 1
        count += sum(
            1 if isinstance(zone.outline, Circle) else len(zone.outline.pts) for zone in self.unavailable_zones
        )
        if count > MAX_SOURCE_VERTICES:
            raise ValueError('Measured shape, holes and unavailable zones together exceed 2000 source vertices')
        ids = [zone.id for zone in self.unavailable_zones]
        if len(ids) != len(set(ids)):
            raise ValueError('Unavailable-zone IDs must be unique')
        if isinstance(shape, UnknownShape) and self.unavailable_zones:
            raise ValueError('Unknown geometry cannot place unavailable zones')
        return self


class Command(StrictModel):
    expected_company_id: ID
    request_key: Annotated[str, Field(min_length=36, max_length=36)]
    reason: Note
    observed_at: str
    observer_name: Text120

    @field_validator('request_key')
    @classmethod
    def uuid_key(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('request_key must be a canonical UUID')
        return value

    @field_validator('reason', 'observer_name')
    @classmethod
    def trimmed(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('Text must be nonempty and trimmed')
        return value

    @field_validator('observed_at')
    @classmethod
    def utc_instant(cls, value):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', value):
            raise ValueError('observed_at must be a complete UTC date and time ending in Z')
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value

    def observed_datetime(self):
        return datetime.fromisoformat(self.observed_at.replace('Z', '+00:00')).astimezone(timezone.utc)


class RecordedCommand(Command):
    state: Literal['RECORDED']
    source_inventory_item_id: ID
    source_part_id: ID
    expected_source_sha256: Hash
    evidence: ObservationEvidence


class CreatePiece(RecordedCommand):
    label: Text120

    @field_validator('label')
    @classmethod
    def trimmed_label(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError('Label must be trimmed')
        return value


class RecordObservation(RecordedCommand):
    expected_version: ID


class WithdrawObservation(Command):
    state: Literal['WITHDRAWN']
    expected_version: ID


AppendObservation = Annotated[Union[RecordObservation, WithdrawObservation], Field(discriminator='state')]


class SourceResponse(StrictModel):
    inventory_item_id: int
    part_id: int
    source_sha256: str
    snapshot: dict[str, Any]
    review_issues: list[str]
    advisory: Literal['Recorded observation — availability and eligibility unverified.'] = ADVISORY


class SourcePage(StrictModel):
    company_id: int
    can_record: bool
    items: list[SourceResponse]
    total: int
    page: int
    per_page: int


class ObservationSummary(StrictModel):
    piece_id: int
    company_id: int
    label: str
    observation_number: int
    piece_version: int
    state: Literal['RECORDED', 'WITHDRAWN']
    reason: str
    observed_at: str
    observer_name: str
    created_at: str
    created_by: int
    submitted_api_token_id: int | None
    payload_schema_version: Literal[1]
    payload_sha256: str
    payload_bytes: int
    source_inventory_item_id: int
    source_part_id: int
    source_sha256: str
    source_status: Literal['unchanged', 'changed', 'missing']
    current_source_sha256: str | None
    review_issues: list[str]
    advisory: Literal['Recorded observation — availability and eligibility unverified.'] = ADVISORY


class ObservationDetail(ObservationSummary):
    evidence: ObservationEvidence
    source_snapshot: dict[str, Any]
    request_key: str


class ObservationPage(StrictModel):
    company_id: int
    can_record: bool
    items: list[ObservationSummary]
    total: int
    page: int
    per_page: int
