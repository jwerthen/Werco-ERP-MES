from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.work_order_blocker import (
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)


class WorkOrderBlockerCreate(BaseModel):
    operation_id: Optional[int] = Field(None, gt=0)
    material_part_id: Optional[int] = Field(None, gt=0)
    category: WorkOrderBlockerCategory = WorkOrderBlockerCategory.OTHER
    severity: WorkOrderBlockerSeverity = WorkOrderBlockerSeverity.MEDIUM
    title: Optional[str] = Field(None, max_length=255)
    note: Optional[str] = Field(None, max_length=2000)
    assigned_to: Optional[int] = Field(None, gt=0)
    put_operation_on_hold: bool = True


class WorkOrderBlockerUpdate(BaseModel):
    status: Optional[WorkOrderBlockerStatus] = None
    severity: Optional[WorkOrderBlockerSeverity] = None
    assigned_to: Optional[int] = Field(None, gt=0)
    resolution_note: Optional[str] = Field(None, max_length=2000)


class WorkOrderBlockerResolve(BaseModel):
    resolution_note: Optional[str] = Field(None, max_length=2000)


class WorkOrderBlockerResponse(BaseModel):
    id: int
    company_id: int
    work_order_id: int
    operation_id: Optional[int] = None
    material_part_id: Optional[int] = None
    category: WorkOrderBlockerCategory
    severity: WorkOrderBlockerSeverity
    status: WorkOrderBlockerStatus
    title: str
    note: Optional[str] = None
    resolution_note: Optional[str] = None
    reported_by: Optional[int] = None
    assigned_to: Optional[int] = None
    resolved_by: Optional[int] = None
    reported_at: datetime
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    work_order_number: Optional[str] = None
    operation_name: Optional[str] = None
    material_part_number: Optional[str] = None

    class Config:
        from_attributes = True
