import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class WorkOrderBlockerCategory(str, enum.Enum):
    MATERIAL_MISSING = "material_missing"
    MACHINE_DOWN = "machine_down"
    TOOLING_MISSING = "tooling_missing"
    QUALITY_HOLD = "quality_hold"
    LABOR_UNAVAILABLE = "labor_unavailable"
    ENGINEERING_QUESTION = "engineering_question"
    PREVIOUS_OPERATION = "previous_operation"
    OTHER = "other"


class WorkOrderBlockerSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WorkOrderBlockerStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class WorkOrderBlocker(Base, TenantMixin):
    """Operator-reported blocker that explains why a job or operation is stuck."""

    __tablename__ = "work_order_blockers"
    __table_args__ = (
        Index("ix_work_order_blockers_company_status", "company_id", "status", "severity"),
        Index("ix_work_order_blockers_company_category", "company_id", "category", "status"),
        Index("ix_work_order_blockers_company_work_order", "company_id", "work_order_id", "status"),
        Index("ix_work_order_blockers_company_operation", "company_id", "operation_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    material_part_id = Column(Integer, ForeignKey("parts.id"), nullable=True, index=True)

    category = Column(String(40), nullable=False, default=WorkOrderBlockerCategory.OTHER.value, index=True)
    severity = Column(String(20), nullable=False, default=WorkOrderBlockerSeverity.MEDIUM.value, index=True)
    status = Column(String(20), nullable=False, default=WorkOrderBlockerStatus.OPEN.value, index=True)

    title = Column(String(255), nullable=False)
    note = Column(Text, nullable=True)
    resolution_note = Column(Text, nullable=True)

    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    reported_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    work_order = relationship("WorkOrder")
    operation = relationship("WorkOrderOperation")
    material_part = relationship("Part")
    reporter = relationship("User", foreign_keys=[reported_by])
    assignee = relationship("User", foreign_keys=[assigned_to])
    resolver = relationship("User", foreign_keys=[resolved_by])
