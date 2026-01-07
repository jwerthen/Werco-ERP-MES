from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
from app.models.job import JobStatus, JobPriority


class JobBase(BaseModel):
    job_type: str
    queue: str = "default"
    priority: JobPriority = JobPriority.NORMAL
    args: Optional[Dict[str, Any]] = None


class JobCreate(JobBase):
    job_id: str
    created_by: Optional[str] = None


class JobUpdate(BaseModel):
    status: Optional[JobStatus] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    attempts: Optional[int] = None


class JobResponse(JobBase):
    id: int
    job_id: str
    status: JobStatus
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    attempts: int
    max_attempts: int
    enqueued_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_by: Optional[str] = None

    class Config:
        from_attributes = True


class JobStatsResponse(BaseModel):
    total_jobs: int
    pending: int
    in_progress: int
    completed: int
    failed: int
    queue_depth: int
    worker_status: str
