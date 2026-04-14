from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base
from app.db.mixins import TenantMixin


class DowntimeCategory(str, enum.Enum):
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    TOOLING = "tooling"
    MATERIAL = "material"
    OPERATOR = "operator"
    QUALITY = "quality"
    CHANGEOVER = "changeover"
    PLANNED_MAINTENANCE = "planned_maintenance"
    BREAK = "break"
    MEETING = "meeting"
    NO_WORK = "no_work"
    OTHER = "other"


class DowntimePlannedType(str, enum.Enum):
    PLANNED = "planned"
    UNPLANNED = "unplanned"


class DowntimeEvent(Base, TenantMixin):
    """Machine downtime event tracking for OEE and production analysis"""
    __tablename__ = "downtime_events"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)

    start_time = Column(DateTime, nullable=False, default=datetime.utcnow)
    end_time = Column(DateTime, nullable=True)
    duration_minutes = Column(Float, nullable=True)

    category = Column(SQLEnum(DowntimeCategory), nullable=False, default=DowntimeCategory.OTHER)
    planned_type = Column(SQLEnum(DowntimePlannedType), nullable=False, default=DowntimePlannedType.UNPLANNED)

    reason_code = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    resolution = Column(Text, nullable=True)

    reported_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    work_order = relationship("WorkOrder", foreign_keys=[work_order_id])
    reporter = relationship("User", foreign_keys=[reported_by])
    resolver = relationship("User", foreign_keys=[resolved_by])


class DowntimeReasonCode(Base, TenantMixin):
    """Predefined reason codes for categorizing downtime events"""
    __tablename__ = "downtime_reason_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    category = Column(SQLEnum(DowntimeCategory), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
