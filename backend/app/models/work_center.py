from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base


class WorkCenter(Base):
    __tablename__ = "work_centers"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    work_center_type = Column(String(50), nullable=False)
    description = Column(Text)
    
    # Capacity planning
    hourly_rate = Column(Float, default=0.0)  # Cost per hour
    capacity_hours_per_day = Column(Float, default=8.0)
    efficiency_factor = Column(Float, default=1.0)  # 1.0 = 100%
    availability_rate = Column(Float, default=100.0)
    
    # Status
    is_active = Column(Boolean, default=True)
    current_status = Column(String(50), default="available")  # available, in_use, maintenance, offline
    
    # Location tracking
    building = Column(String(50))
    area = Column(String(50))
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    operations = relationship("WorkOrderOperation", back_populates="work_center")
    time_entries = relationship("TimeEntry", back_populates="work_center")
