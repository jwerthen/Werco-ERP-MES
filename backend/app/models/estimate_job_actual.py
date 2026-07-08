"""Estimate Job Actuals — quoted vs shop-floor hours for Cut/Bend tuning (Phase 5)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class EstimateJobActual(Base, TenantMixin, SoftDeleteMixin):
    """Post-job actuals entered against a workbench estimate (or standalone).

    Used by the Shop Data "Quoted vs Actual" view to prompt table tuning.
    One row per estimate (unique when estimate_id is set).
    """

    __tablename__ = "estimate_job_actuals"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "quote_estimate_id",
            name="uq_estimate_job_actuals_company_estimate",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    quote_estimate_id = Column(Integer, ForeignKey("quote_estimates.id"), nullable=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
    job_label = Column(String(255), nullable=True)  # free-text when no WO link

    # Quoted hours (copied from estimate.internal_breakdown at entry time)
    quoted_laser_hours = Column(Float, nullable=False, default=0.0)
    quoted_brake_hours = Column(Float, nullable=False, default=0.0)
    quoted_weld_hours = Column(Float, nullable=False, default=0.0)

    # Actual hours from the floor
    actual_laser_hours = Column(Float, nullable=True)
    actual_brake_hours = Column(Float, nullable=True)
    actual_weld_hours = Column(Float, nullable=True)

    notes = Column(Text, nullable=True)
    entered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    estimate = relationship("QuoteEstimate", foreign_keys=[quote_estimate_id])
