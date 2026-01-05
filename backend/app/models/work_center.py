from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class WorkCenterType(str, enum.Enum):
    FABRICATION = "fabrication"
    CNC_MACHINING = "cnc_machining"
    LASER = "laser"
    PRESS_BRAKE = "press_brake"
    PAINT = "paint"
    POWDER_COATING = "powder_coating"
    ASSEMBLY = "assembly"
    WELDING = "welding"
    INSPECTION = "inspection"
    SHIPPING = "shipping"


class WorkCenter(Base):
    __tablename__ = "work_centers"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    work_center_type = Column(SQLEnum(WorkCenterType), nullable=False)
    description = Column(Text)
    
    # Capacity planning
    hourly_rate = Column(Float, default=0.0)  # Cost per hour
    capacity_hours_per_day = Column(Float, default=8.0)
    efficiency_factor = Column(Float, default=1.0)  # 1.0 = 100%
    
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
