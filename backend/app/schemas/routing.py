from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.validation import OperationNumber
from app.schemas.base import UTCModel


class RoutingOperationBase(UTCModel):
    sequence: int
    operation_number: Optional[OperationNumber] = None
    name: str
    description: Optional[str] = None
    work_center_id: int
    # Bounded to match chk_routing_ops_setup_hours_non_negative /
    # chk_routing_ops_run_hours_non_negative, the DB CHECKs migration 080
    # restored. The CSV importer already rejects negative hours
    # (routing_import_service._parse_hours); the interactive and AI-approve paths
    # did not -- and they chain, since WO operations snapshot these into
    # setup_time_hours / run_time_hours, which carry their own CHECKs.
    setup_hours: float = Field(default=0.0, ge=0)
    run_hours_per_unit: float = Field(default=0.0, ge=0)
    move_hours: float = 0.0
    queue_hours: float = 0.0
    cycle_time_seconds: Optional[float] = None
    pieces_per_cycle: int = 1
    labor_rate_override: Optional[float] = None
    overhead_rate: float = 0.0
    is_inspection_point: bool = False
    inspection_instructions: Optional[str] = None
    work_instructions: Optional[str] = None
    setup_instructions: Optional[str] = None
    tooling_requirements: Optional[str] = None
    fixture_requirements: Optional[str] = None
    is_outside_operation: bool = False
    vendor_id: Optional[int] = None
    outside_cost: float = 0.0
    outside_lead_days: int = 0
    # Attached process sheet (library reference; must be RELEASED in the same company —
    # validated at the endpoint). Snapshotted onto WO operations at WO creation (PR 3).
    process_sheet_id: Optional[int] = None


class RoutingOperationCreate(RoutingOperationBase):
    pass


class RoutingOperationUpdate(BaseModel):
    sequence: Optional[int] = None
    operation_number: Optional[OperationNumber] = None
    name: Optional[str] = None
    description: Optional[str] = None
    work_center_id: Optional[int] = None
    setup_hours: Optional[float] = Field(None, ge=0)
    run_hours_per_unit: Optional[float] = Field(None, ge=0)
    move_hours: Optional[float] = None
    queue_hours: Optional[float] = None
    cycle_time_seconds: Optional[float] = None
    pieces_per_cycle: Optional[int] = None
    labor_rate_override: Optional[float] = None
    overhead_rate: Optional[float] = None
    is_inspection_point: Optional[bool] = None
    inspection_instructions: Optional[str] = None
    work_instructions: Optional[str] = None
    setup_instructions: Optional[str] = None
    tooling_requirements: Optional[str] = None
    fixture_requirements: Optional[str] = None
    is_outside_operation: Optional[bool] = None
    vendor_id: Optional[int] = None
    outside_cost: Optional[float] = None
    outside_lead_days: Optional[int] = None
    is_active: Optional[bool] = None
    process_sheet_id: Optional[int] = None


class RoutingOperationReorderItem(BaseModel):
    """One (operation, new sequence) pair for ``POST /routing/{id}/operations/reorder``.

    The endpoint took a bare ``List[dict]`` and wrote ``str(item["sequence"])`` straight into
    the ``String(20)`` ``operation_number`` column -- the one path where a caller-controlled
    NON-INT reaches that column. Untyped, a float ``10.0`` persisted the string ``"10.0"``,
    ``None`` persisted ``"None"`` before the ``nullable=False`` IntegrityError fired, and a
    missing ``"id"`` key was a ``KeyError`` (500). Typing the body makes each of those a 422,
    and Pydantic's lax int coercion still accepts the ``10.0`` a JS client sends for a whole
    number while refusing a fractional ``10.5`` that no sequence can be.

    ``sequence`` deliberately carries NO upper bound: ``RoutingOperationBase.sequence`` -- the
    CREATE path for the same column -- declares none either, so a ceiling here would refuse
    reorders of rows this same API happily creates.
    """

    id: int = Field(..., gt=0, description="Id of the routing operation to move")
    sequence: int = Field(..., ge=0, description="Its new sequence")


class WorkCenterSummary(BaseModel):
    id: int
    code: str
    name: str
    work_center_type: str
    hourly_rate: float

    class Config:
        from_attributes = True


class RoutingOperationResponse(RoutingOperationBase):
    id: int
    routing_id: int
    work_center: Optional[WorkCenterSummary] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


class RoutingBase(UTCModel):
    part_id: int
    revision: str = "A"
    description: Optional[str] = None


class RoutingCreate(RoutingBase):
    pass


class RoutingUpdate(BaseModel):
    revision: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    part_type: str

    class Config:
        from_attributes = True
        use_enum_values = True


class RoutingResponse(RoutingBase):
    id: int
    status: str
    is_active: bool
    effective_date: Optional[datetime]
    total_setup_hours: float
    total_run_hours_per_unit: float
    total_labor_cost: float
    total_overhead_cost: float
    part: Optional[PartSummary] = None
    operations: List[RoutingOperationResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RoutingListResponse(UTCModel):
    id: int
    part_id: int
    part: Optional[PartSummary] = None
    revision: str
    status: str
    is_active: bool
    total_setup_hours: float
    total_run_hours_per_unit: float
    operation_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True
