"""Audited per-center working hours and dated capacity exceptions."""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin


class WorkingCalendar(Base, TenantMixin):
    __tablename__ = "working_calendars"
    __table_args__ = (UniqueConstraint("company_id", "work_center_id", name="uq_working_calendar_center"),)

    id = Column(Integer, primary_key=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)
    weekly_hours = Column(JSON, nullable=False)
    overrides = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
