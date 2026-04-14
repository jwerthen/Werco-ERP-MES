from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, Date, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base
from app.db.mixins import TenantMixin


class OEERecord(Base, TenantMixin):
    """Daily OEE snapshot per work center"""
    __tablename__ = "oee_records"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, index=True)
    record_date = Column(Date, nullable=False, index=True)
    shift = Column(String(50), nullable=True)

    # Availability inputs
    planned_production_time_minutes = Column(Float, nullable=False, default=0.0)
    actual_run_time_minutes = Column(Float, nullable=False, default=0.0)
    downtime_minutes = Column(Float, nullable=False, default=0.0)

    # Performance inputs
    total_parts_produced = Column(Integer, nullable=False, default=0)
    ideal_cycle_time_seconds = Column(Float, nullable=False, default=0.0)
    actual_operating_time_minutes = Column(Float, nullable=False, default=0.0)

    # Quality inputs
    good_parts = Column(Integer, nullable=False, default=0)
    total_parts = Column(Integer, nullable=False, default=0)
    defect_parts = Column(Integer, nullable=False, default=0)
    rework_parts = Column(Integer, nullable=False, default=0)

    # Calculated OEE components (stored as 0-100 percentages)
    availability_pct = Column(Float, nullable=False, default=0.0)
    performance_pct = Column(Float, nullable=False, default=0.0)
    quality_pct = Column(Float, nullable=False, default=0.0)
    oee_pct = Column(Float, nullable=False, default=0.0)

    # Six big losses breakdown (minutes)
    unplanned_stop_minutes = Column(Float, default=0.0)
    planned_stop_minutes = Column(Float, default=0.0)
    small_stop_minutes = Column(Float, default=0.0)
    slow_cycle_minutes = Column(Float, default=0.0)
    production_reject_count = Column(Integer, default=0)
    startup_reject_count = Column(Integer, default=0)

    notes = Column(Text)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    work_center = relationship("WorkCenter")


class OEETarget(Base, TenantMixin):
    """Target OEE per work center"""
    __tablename__ = "oee_targets"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, unique=True)

    target_oee_pct = Column(Float, default=85.0)
    target_availability_pct = Column(Float, default=90.0)
    target_performance_pct = Column(Float, default=95.0)
    target_quality_pct = Column(Float, default=99.0)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter")
