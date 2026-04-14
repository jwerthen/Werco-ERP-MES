from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base
from app.db.mixins import TenantMixin


class JobCostStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REVIEWED = "reviewed"


class CostEntryType(str, enum.Enum):
    MATERIAL = "material"
    LABOR = "labor"
    OVERHEAD = "overhead"
    OTHER = "other"


class CostEntrySource(str, enum.Enum):
    TIME_ENTRY = "time_entry"
    MATERIAL_ISSUE = "material_issue"
    PURCHASE = "purchase"
    MANUAL = "manual"


class JobCost(Base, TenantMixin):
    """Job costing record linked to a work order"""
    __tablename__ = "job_costs"

    id = Column(Integer, primary_key=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, unique=True)

    # Estimated costs
    estimated_material_cost = Column(Float, default=0.0)
    estimated_labor_cost = Column(Float, default=0.0)
    estimated_overhead_cost = Column(Float, default=0.0)
    estimated_total_cost = Column(Float, default=0.0)

    # Actual costs
    actual_material_cost = Column(Float, default=0.0)
    actual_labor_cost = Column(Float, default=0.0)
    actual_overhead_cost = Column(Float, default=0.0)
    actual_total_cost = Column(Float, default=0.0)

    # Variance (actual - estimated)
    material_variance = Column(Float, default=0.0)
    labor_variance = Column(Float, default=0.0)
    overhead_variance = Column(Float, default=0.0)
    total_variance = Column(Float, default=0.0)

    # Margin
    margin_amount = Column(Float, default=0.0)
    margin_percent = Column(Float, default=0.0)

    # Revenue / selling price for margin calculation
    revenue = Column(Float, default=0.0)

    # Status
    status = Column(SQLEnum(JobCostStatus), default=JobCostStatus.IN_PROGRESS, index=True)

    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_order = relationship("WorkOrder", backref="job_cost")
    entries = relationship("CostEntry", back_populates="job_cost", cascade="all, delete-orphan")


class CostEntry(Base, TenantMixin):
    """Individual cost line item for a job cost record"""
    __tablename__ = "cost_entries"

    id = Column(Integer, primary_key=True, index=True)
    job_cost_id = Column(Integer, ForeignKey("job_costs.id"), nullable=False)

    entry_type = Column(SQLEnum(CostEntryType), nullable=False)
    description = Column(String(500), nullable=False)
    quantity = Column(Float, default=1.0)
    unit_cost = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)

    # Link to specific operation (optional)
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True)

    # Source tracking
    source = Column(SQLEnum(CostEntrySource), default=CostEntrySource.MANUAL)
    reference = Column(String(255))  # PO number, time entry ID, etc.

    entry_date = Column(Date, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    job_cost = relationship("JobCost", back_populates="entries")
    operation = relationship("WorkOrderOperation")
    creator = relationship("User")
