"""Short-lived, anonymous browser measurements. No person or business record fields."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint

from app.db.database import Base


class RuntimeMetricSample(Base):
    __tablename__ = "runtime_metric_samples"
    __table_args__ = (
        UniqueConstraint("company_id", "metric_id", name="uq_runtime_metric_receipt"),
        Index("ix_runtime_metric_company_created", "company_id", "created_at"),
        Index("ix_runtime_metric_retention", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    metric_id = Column(String(80), nullable=False)
    name = Column(String(3), nullable=False)
    route = Column(String(100), nullable=False)
    device = Column(String(10), nullable=False)
    navigation = Column(String(8), nullable=False)
    release = Column(String(40), nullable=False)
    value = Column(Float, nullable=False)
    sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class RuntimeMetricSetting(Base):
    __tablename__ = "runtime_metric_settings"

    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
