from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class CalibrationStatus(str, enum.Enum):
    ACTIVE = "active"
    DUE = "due"
    OVERDUE = "overdue"
    OUT_OF_SERVICE = "out_of_service"
    RETIRED = "retired"


class Equipment(Base):
    """Measurement equipment/gauges for calibration tracking"""
    __tablename__ = "equipment"
    
    id = Column(Integer, primary_key=True, index=True)
    equipment_id = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    
    # Equipment details
    equipment_type = Column(String(100))  # caliper, micrometer, CMM, etc.
    manufacturer = Column(String(255))
    model = Column(String(100))
    serial_number = Column(String(100))
    
    # Location
    location = Column(String(255))
    assigned_to = Column(String(255))
    
    # Calibration info
    calibration_interval_days = Column(Integer, default=365)
    last_calibration_date = Column(Date, nullable=True)
    next_calibration_date = Column(Date, nullable=True)
    calibration_provider = Column(String(255))
    
    # Accuracy/Range
    range_min = Column(String(50))
    range_max = Column(String(50))
    accuracy = Column(String(100))
    resolution = Column(String(50))
    
    # Status
    status = Column(SQLEnum(CalibrationStatus), default=CalibrationStatus.ACTIVE)
    is_active = Column(Boolean, default=True)
    
    # Cost tracking
    purchase_cost = Column(Float, default=0.0)
    purchase_date = Column(Date, nullable=True)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    calibration_records = relationship("CalibrationRecord", back_populates="equipment", cascade="all, delete-orphan")


class CalibrationRecord(Base):
    """Calibration history record"""
    __tablename__ = "calibration_records"
    
    id = Column(Integer, primary_key=True, index=True)
    equipment_id = Column(Integer, ForeignKey("equipment.id"), nullable=False)
    
    # Calibration details
    calibration_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    
    # Provider
    performed_by = Column(String(255))
    calibration_provider = Column(String(255))
    certificate_number = Column(String(100))
    
    # Results
    result = Column(String(50))  # pass, fail, adjusted
    as_found = Column(Text)  # Condition as found
    as_left = Column(Text)  # Condition after calibration
    
    # Standards used
    standards_used = Column(Text)
    
    # Cost
    cost = Column(Float, default=0.0)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    equipment = relationship("Equipment", back_populates="calibration_records")
