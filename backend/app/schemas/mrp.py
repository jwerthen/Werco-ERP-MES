from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
from app.models.mrp import MRPRunStatus, PlanningAction


class MRPRunCreate(BaseModel):
    planning_horizon_days: int = 90
    include_safety_stock: bool = True
    include_allocated: bool = True


class MRPRunResponse(BaseModel):
    id: int
    run_number: str
    planning_horizon_days: int
    include_safety_stock: bool
    include_allocated: bool
    status: MRPRunStatus
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_message: Optional[str]
    total_parts_analyzed: int
    total_requirements: int
    total_actions: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    part_type: str
    
    class Config:
        from_attributes = True


class MRPRequirementResponse(BaseModel):
    id: int
    mrp_run_id: int
    part_id: int
    part: Optional[PartSummary] = None
    required_date: date
    quantity_required: float
    quantity_on_hand: float
    quantity_on_order: float
    quantity_allocated: float
    quantity_available: float
    quantity_shortage: float
    source_type: Optional[str]
    source_number: Optional[str]
    bom_level: int
    
    class Config:
        from_attributes = True


class MRPActionResponse(BaseModel):
    id: int
    mrp_run_id: int
    part_id: int
    part: Optional[PartSummary] = None
    action_type: PlanningAction
    priority: int
    quantity: float
    required_date: date
    suggested_order_date: date
    current_date: Optional[date]
    reference_type: Optional[str]
    reference_number: Optional[str]
    is_processed: bool
    processed_at: Optional[datetime]
    result_reference: Optional[str]
    notes: Optional[str]
    
    class Config:
        from_attributes = True


class MRPRunDetail(MRPRunResponse):
    requirements: List[MRPRequirementResponse] = []
    actions: List[MRPActionResponse] = []


class MRPPartAnalysis(BaseModel):
    """Analysis for a single part"""
    part_id: int
    part_number: str
    part_name: str
    part_type: str
    lead_time_days: int
    safety_stock: float
    reorder_point: float
    
    # Current inventory status
    on_hand: float
    allocated: float
    available: float
    on_order: float
    
    # Requirements summary
    total_required: float
    total_shortage: float
    
    # Time-phased requirements (by week)
    weekly_requirements: List[dict]
    
    # Recommended actions
    actions: List[MRPActionResponse]


class ProcessActionRequest(BaseModel):
    """Request to process an MRP action"""
    action_id: int
    notes: Optional[str] = None


class ProcessActionResponse(BaseModel):
    success: bool
    message: str
    created_reference: Optional[str] = None  # WO or PO number
