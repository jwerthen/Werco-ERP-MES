from sqlalchemy import Column, Integer, String, DateTime, Float, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class TimeEntryType(str, enum.Enum):
    SETUP = "setup"
    RUN = "run"
    REWORK = "rework"
    INSPECTION = "inspection"
    DOWNTIME = "downtime"
    BREAK = "break"


class TimeEntry(Base):
    """Time tracking for shop floor labor"""
    __tablename__ = "time_entries"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Who/What/Where
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    
    # Time tracking
    entry_type = Column(SQLEnum(TimeEntryType), default=TimeEntryType.RUN)
    clock_in = Column(DateTime, nullable=False)
    clock_out = Column(DateTime, nullable=True)
    duration_hours = Column(Float, nullable=True)  # Calculated on clock_out
    
    # Production tracking
    quantity_produced = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    
    # Notes and reason codes
    notes = Column(Text)
    scrap_reason = Column(String(255))
    downtime_reason = Column(String(255))
    
    # Approval workflow
    approved = Column(DateTime, nullable=True)
    approved_by = Column(Integer, nullable=True)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="time_entries")
    work_order = relationship("WorkOrder", back_populates="time_entries")
    operation = relationship("WorkOrderOperation", back_populates="time_entries")
    work_center = relationship("WorkCenter", back_populates="time_entries")
