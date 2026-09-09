"""Exact, bounded original-byte evidence; never a CAD approval contract."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SOURCE_BYTES = 5_000_000 - 1
MAX_INTENT_BYTES = 256 * 1024
MAX_TARGET_SNAPSHOT_BYTES = 512 * 1024
MAX_TARGETS = 1000
MAX_ATTEMPTS = 8
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SourceTarget(SourceInput):
    group_id: Identifier
    part_id: Identifier

    @field_validator('group_id', 'part_id')
    @classmethod
    def valid_text(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError('Identifiers cannot contain control characters')
        return value


class CreateSourceIntent(SourceInput):
    expected_company_id: PositiveId
    request_key: str = Field(min_length=36, max_length=36)
    expected_input_sha256: Hash
    source_sha256: Hash
    byte_count: int = Field(strict=True, ge=1, le=MAX_SOURCE_BYTES)
    source_name: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(min_length=1, max_length=120)
    targets: list[SourceTarget] = Field(min_length=1, max_length=MAX_TARGETS)

    @field_validator('request_key')
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError('request_key must be a canonical UUID')
        return value

    @field_validator('source_name', 'mime_type')
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError('File metadata must be trimmed and contain no control characters')
        return value

    @model_validator(mode='after')
    def exact_file_and_targets(self):
        if not self.source_name.lower().endswith('.dxf') or '/' in self.source_name or '\\' in self.source_name:
            raise ValueError('Provide a DXF filename without a path')
        pairs = [(target.group_id, target.part_id) for target in self.targets]
        if len(set(pairs)) != len(pairs):
            raise ValueError('Each saved part can be selected only once')
        return self


class FinalizeSource(SourceInput):
    expected_company_id: PositiveId


class SourceBindingEvidence(BaseModel):
    group_id: str
    part_id: str
    provenance: dict[str, Any]


class SourceReceiptResponse(BaseModel):
    id: int
    source_sha256: str
    byte_count: int
    verified_at: str
    created_by: int
    submitted_api_token_id: int | None
    claim: Literal['server_hash_verified_unapproved'] = 'server_hash_verified_unapproved'


class SourceIntentResponse(BaseModel):
    id: int
    company_id: int
    draft_id: int
    revision_id: int
    revision_number: int
    input_sha256: str
    source_sha256: str
    byte_count: int
    source_name: str
    mime_type: str
    targets: list[SourceBindingEvidence]
    targets_sha256: str
    target_count: int
    request_key: str
    created_by: int
    submitted_api_token_id: int | None
    created_at: str
    state: Literal['PENDING', 'ATTACHED']
    attempt_count: int
    can_resume: bool
    receipt: SourceReceiptResponse | None


class SourcePageResponse(BaseModel):
    company_id: int
    draft_id: int
    revision_number: int
    input_sha256: str
    can_attach: bool
    items: list[SourceIntentResponse]
    total: int
    page: int
    per_page: int
