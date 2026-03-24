from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class MaintenanceType(str, enum.Enum):
    PREVENTIVE = "preventive"
    CORRECTIVE = "corrective"
    PREDICTIVE = "predictive"
    EMERGENCY = "emergency"


class MaintenancePriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MaintenanceStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    ON_HOLD = "on_hold"


class MaintenanceFrequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"
    CUSTOM = "custom"


FREQUENCY_DAYS_MAP = {
    MaintenanceFrequency.DAILY: 1,
    MaintenanceFrequency.WEEKLY: 7,
    MaintenanceFrequency.BIWEEKLY: 14,
    MaintenanceFrequency.MONTHLY: 30,
    MaintenanceFrequency.QUARTERLY: 90,
    MaintenanceFrequency.SEMI_ANNUAL: 182,
    MaintenanceFrequency.ANNUAL: 365,
}


class MaintenanceSchedule(Base):
    """Preventive Maintenance schedule template"""
    __tablename__ = "maintenance_schedules"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)

    name = Column(String(255), nullable=False)
    description = Column(Text)

    maintenance_type = Column(SQLEnum(MaintenanceType), default=MaintenanceType.PREVENTIVE)
    frequency = Column(SQLEnum(MaintenanceFrequency))
    frequency_days = Column(Integer, nullable=True)  # custom interval in days
    estimated_duration_hours = Column(Float, default=1.0)

    priority = Column(SQLEnum(MaintenancePriority), default=MaintenancePriority.MEDIUM)
    checklist = Column(Text)  # JSON string of checklist items
    requires_shutdown = Column(Boolean, default=False)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    last_completed_date = Column(Date, nullable=True)
    next_due_date = Column(Date, nullable=True)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    work_orders = relationship("MaintenanceWorkOrder", back_populates="schedule")


class MaintenanceWorkOrder(Base):
    """Individual maintenance work order / task"""
    __tablename__ = "maintenance_work_orders"

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer, ForeignKey("maintenance_schedules.id"), nullable=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)

    wo_number = Column(String(50), unique=True, index=True, nullable=False)

    maintenance_type = Column(SQLEnum(MaintenanceType), default=MaintenanceType.PREVENTIVE)
    priority = Column(SQLEnum(MaintenancePriority), default=MaintenancePriority.MEDIUM)
    status = Column(SQLEnum(MaintenanceStatus), default=MaintenanceStatus.SCHEDULED)

    title = Column(String(255), nullable=False)
    description = Column(Text)
    checklist_results = Column(Text)  # JSON with completed checklist items

    scheduled_date = Column(Date)
    due_date = Column(Date)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    actual_duration_hours = Column(Float, nullable=True)
    requires_shutdown = Column(Boolean, default=False)
    downtime_minutes = Column(Float, default=0)

    parts_used = Column(Text)  # JSON list of spare parts used
    labor_cost = Column(Float, default=0)
    parts_cost = Column(Float, default=0)
    total_cost = Column(Float, default=0)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    notes = Column(Text)
    findings = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    schedule = relationship("MaintenanceSchedule", back_populates="work_orders")


class MaintenanceLog(Base):
    """History entry for maintenance events"""
    __tablename__ = "maintenance_logs"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)
    maintenance_wo_id = Column(Integer, ForeignKey("maintenance_work_orders.id"), nullable=True)

    event_type = Column(String(50), nullable=False)  # completed, inspection, repair, part_replacement, observation
    description = Column(Text, nullable=False)
    performed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    event_date = Column(DateTime, default=datetime.utcnow)
    cost = Column(Float, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    work_order = relationship("MaintenanceWorkOrder", foreign_keys=[maintenance_wo_id])
