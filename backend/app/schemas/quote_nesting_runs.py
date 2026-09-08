"""Bounded, unapproved server calculations of immutable nesting input revisions."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import UTCModel

PROTOCOL_VERSION = 1
SOLVER_VERSION = "werco-contour-v5"
MAX_OPTIONS = 36
MAX_SECONDS = 120
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 24 * 1024 * 1024
LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 10
MAX_ESTIMATE_BYTES = 5 * 1024 * 1024
RUN_SETTINGS = {
    "profile": "standard-v1",
    "protocol": PROTOCOL_VERSION,
    "solver_version": SOLVER_VERSION,
    "max_seconds": MAX_SECONDS,
    "max_option_evaluations": MAX_OPTIONS,
    "max_input_bytes": MAX_ESTIMATE_BYTES,
    "max_message_bytes": MAX_MESSAGE_BYTES,
    "max_checkpoint_bytes": MAX_CHECKPOINT_BYTES,
    "node_heap_mib": 512,
    "seed": None,
    "units": "mm",
    "approved": False,
    "remnant_credit_usd": 0,
}
ACTIVE_STATUSES = ("QUEUED", "RUNNING")
TERMINAL_STATUSES = ("COMPLETED", "PARTIAL", "CANCELLED", "FAILED")
RunStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "CANCELLED", "FAILED"]
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: PositiveId
    revision_number: PositiveId
    input_sha256: Digest
    expected_company_id: PositiveId
    request_key: UUID


class CancelRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_company_id: PositiveId
    expected_version: PositiveId


class RunWarning(UTCModel):
    code: str
    message: str


class RunSummary(UTCModel):
    id: int
    company_id: int
    draft_id: int
    revision_id: int
    revision_number: int
    input_sha256: str
    created_by: int
    status: RunStatus
    version: int
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    release_identity: str | None
    solver_version: str | None
    bundle_sha256: str | None
    node_version: str | None
    evaluated_count: int
    completed_count: int
    checkpoint_bytes: int
    error_code: str | None
    error_message: str | None


class RunCheckpointMetadata(UTCModel):
    sequence: int
    group_id: str
    option_id: str
    content_sha256: str
    payload_bytes: int
    created_at: str
    complete: bool
    sheets: int
    placed: int
    unplaced: int


class RunCheckpointResponse(RunCheckpointMetadata):
    schema_version: Literal[1] = 1
    result: dict[str, Any]


class RunDetail(RunSummary):
    schema_version: Literal[1] = 1
    settings: dict[str, Any]
    summary: dict[str, Any] | None
    warnings: list[RunWarning]
    checkpoints: list[RunCheckpointMetadata]


class RunPage(UTCModel):
    schema_version: Literal[1] = 1
    items: list[RunSummary]
    total: int
    page: int
    per_page: int


class RunReport(UTCModel):
    schema_version: Literal[1] = 1
    status: Literal["UNAPPROVED"] = "UNAPPROVED"
    run: RunDetail
    estimate: dict[str, Any]
    checkpoints: list[RunCheckpointResponse]
    content_sha256: str


class RuntimeIdentity(UTCModel):
    release: str
    protocol: Literal[1]
    solver_version: str
    bundle_sha256: str
    node_version: str
    instance_id: str
    observed_at: str
    deployment_id: str | None


class RuntimeReadiness(UTCModel):
    schema_version: Literal[1] = 1
    available: bool
    reason: str
    identity: RuntimeIdentity | None
