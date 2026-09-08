"""A durable receipt for one additive production submission; never updated."""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin


class ProductionReceipt(Base, TenantMixin):
    __tablename__ = "production_receipts"
    __table_args__ = (UniqueConstraint("company_id", "request_id", name="uq_production_receipt_request"),)

    id = Column(Integer, primary_key=True)
    request_id = Column(String(100), nullable=False)
    request_hash = Column(String(64), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=False)
    time_entry_id = Column(Integer, ForeignKey("time_entries.id"), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
