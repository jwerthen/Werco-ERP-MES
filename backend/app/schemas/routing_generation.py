"""Schemas for auto-generating draft routings from uploaded drawings."""

from pydantic import BaseModel, Field
from typing import Optional, List
from app.schemas.routing import RoutingOperationCreate


class DrawingExtractionInfo(BaseModel):
    """Manufacturing-relevant info extracted from the drawing."""
    material: Optional[str] = None
    thickness: Optional[str] = None
    finish: Optional[str] = None
    tolerances_noted: bool = False
    weld_required: bool = False
    assembly_required: bool = False
    flat_length: Optional[float] = None
    flat_width: Optional[float] = None
    cut_length: Optional[float] = None
    hole_count: Optional[int] = None
    bend_count: Optional[int] = None


class ProposedOperation(BaseModel):
    """A single proposed routing operation from the AI analysis."""
    sequence: int
    operation_name: str
    description: Optional[str] = None
    work_center_type: str
    work_center_id: Optional[int] = None
    work_center_name: Optional[str] = None
    setup_hours: float = 0.0
    run_hours_per_unit: float = 0.0
    cycle_time_seconds: Optional[float] = None
    is_inspection_point: bool = False
    is_outside_operation: bool = False
    tooling_requirements: Optional[str] = None
    work_instructions: Optional[str] = None
    confidence: str = "medium"


class RoutingGenerationResult(BaseModel):
    """Full result returned to the frontend for review."""
    part_id: int
    part_number: str
    part_name: str
    drawing_info: DrawingExtractionInfo
    proposed_operations: List[ProposedOperation] = Field(default_factory=list)
    extraction_confidence: str = "medium"
    file_type: str
    warnings: List[str] = Field(default_factory=list)
    existing_routing_warning: Optional[str] = None


class RoutingCreateFromGeneration(BaseModel):
    """Payload sent after user reviews and edits the proposed routing."""
    part_id: int
    revision: str = "A"
    description: Optional[str] = None
    operations: List[RoutingOperationCreate]
