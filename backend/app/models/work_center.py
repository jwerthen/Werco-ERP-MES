from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin
from app.models.operator_certification import CertificationType


class WorkCenter(Base, TenantMixin):
    __tablename__ = "work_centers"
    __table_args__ = (UniqueConstraint('company_id', 'code', name='uq_work_centers_company_code'),)

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), index=True, nullable=False)
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

    # Operator-qualification gate (G5-B): when set, only operators holding an active
    # certification of this type may be assigned/clock in to this work center. NULL
    # (the common case) means the work center has no certification requirement.
    # Reuses the existing CertificationType native enum (created by operator_certifications).
    required_certification_type = Column(SQLEnum(CertificationType), nullable=True)

    # Location tracking
    building = Column(String(50))
    area = Column(String(50))

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    operations = relationship("WorkOrderOperation", back_populates="work_center")
    time_entries = relationship("TimeEntry", back_populates="work_center")
