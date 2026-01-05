from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, Enum as SQLEnum
from sqlalchemy.sql import func
from app.db.base_class import Base
import enum


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class JobPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class Job(Base):
    """Background job tracking"""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String(100), unique=True, index=True, nullable=False)  # ARQ job ID
    job_type = Column(String(100), index=True, nullable=False)  # send_email, run_mrp, etc
    queue = Column(String(50), nullable=False, default="default")
    priority = Column(SQLEnum(JobPriority), default=JobPriority.NORMAL)
    status = Column(SQLEnum(JobStatus), default=JobStatus.PENDING, index=True)

    # Job data
    args = Column(JSON, nullable=True)  # Job arguments
    result = Column(JSON, nullable=True)  # Job result
    error = Column(Text, nullable=True)  # Error message if failed

    # Retry tracking
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)

    # Timing
    enqueued_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Audit
    created_by = Column(String(100), nullable=True)  # User who triggered job
