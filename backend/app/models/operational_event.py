from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class OperationalEvent(Base, TenantMixin):
    """Durable tenant-scoped production event used for real-time AI context."""

    __tablename__ = "operational_events"
    __table_args__ = (
        Index("ix_operational_events_company_module_time", "company_id", "source_module", "occurred_at"),
        Index("ix_operational_events_company_type_time", "company_id", "event_type", "occurred_at"),
        Index("ix_operational_events_company_entity", "company_id", "entity_type", "entity_id"),
        Index("ix_operational_events_company_work_order", "company_id", "work_order_id", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(80), nullable=False, index=True)
    source_module = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    severity = Column(String(20), nullable=False, default="info", index=True)
    event_payload = Column(JSON, nullable=True, default=dict)
    occurred_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    work_order = relationship("WorkOrder")
    operation = relationship("WorkOrderOperation")
    user = relationship("User")
