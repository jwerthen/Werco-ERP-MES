import enum
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class CalculationSource(str, enum.Enum):
    """Vocabulary for ``OEERecord.calculation_source`` (Lean Phase 1).

    Stored as a plain ``String(20)`` (NOT a native SQLEnum) so adding a source
    never needs an ``ALTER TYPE``. ``MANUAL`` covers hand-entered records and the
    on-demand ``POST /oee/calculate/{work_center_id}`` trigger (a human asked for
    it); ``AUTO`` is minted only by the nightly ARQ cron. The cron never
    overwrites a ``MANUAL`` row; ``AUTO`` rows may be recomputed by re-runs.
    """

    MANUAL = "manual"
    AUTO = "auto"


class OEERecord(Base, TenantMixin):
    """Daily OEE snapshot per work center"""

    __tablename__ = "oee_records"
    # Lean Phase 1 (migration 063): at most ONE record per (company, work center,
    # date, shift). ``shift`` is nullable and Postgres unique constraints treat
    # NULLs as distinct, so the key uses COALESCE(shift, '') -- a NULL shift and an
    # empty-string shift are deliberately the same "no shift" key. Expression
    # index (not UniqueConstraint) so both Postgres and the SQLite create_all
    # path build the same rule; keep in lock-step with migration 063.
    __table_args__ = (
        Index(
            "uq_oee_company_wc_date_shift",
            "company_id",
            "work_center_id",
            "record_date",
            text("COALESCE(shift, '')"),
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, index=True)
    record_date = Column(Date, nullable=False, index=True)
    shift = Column(String(50), nullable=True)

    # Lean Phase 1: how this snapshot was produced. 'manual' = hand-entered (all
    # pre-063 rows backfill to it via server_default); the Phase 1 auto-calculator
    # will mint its own token when it ships. Plain String (not SQLEnum) so new
    # sources never need an ALTER TYPE.
    calculation_source = Column(String(20), nullable=False, default="manual", server_default="manual")

    # Availability inputs
    planned_production_time_minutes = Column(Float, nullable=False, default=0.0)
    actual_run_time_minutes = Column(Float, nullable=False, default=0.0)
    downtime_minutes = Column(Float, nullable=False, default=0.0)

    # Performance inputs
    total_parts_produced = Column(Integer, nullable=False, default=0)
    ideal_cycle_time_seconds = Column(Float, nullable=False, default=0.0)
    actual_operating_time_minutes = Column(Float, nullable=False, default=0.0)

    # Quality inputs
    good_parts = Column(Integer, nullable=False, default=0)
    total_parts = Column(Integer, nullable=False, default=0)
    defect_parts = Column(Integer, nullable=False, default=0)
    rework_parts = Column(Integer, nullable=False, default=0)

    # Calculated OEE components (stored as 0-100 percentages)
    availability_pct = Column(Float, nullable=False, default=0.0)
    performance_pct = Column(Float, nullable=False, default=0.0)
    quality_pct = Column(Float, nullable=False, default=0.0)
    oee_pct = Column(Float, nullable=False, default=0.0)

    # Six big losses breakdown (minutes)
    unplanned_stop_minutes = Column(Float, default=0.0)
    planned_stop_minutes = Column(Float, default=0.0)
    small_stop_minutes = Column(Float, default=0.0)
    slow_cycle_minutes = Column(Float, default=0.0)
    production_reject_count = Column(Integer, default=0)
    startup_reject_count = Column(Integer, default=0)

    notes = Column(Text)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    work_center = relationship("WorkCenter")


class OEETarget(Base, TenantMixin):
    """Target OEE per work center"""

    __tablename__ = "oee_targets"

    id = Column(Integer, primary_key=True, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, unique=True)

    target_oee_pct = Column(Float, default=85.0)
    target_availability_pct = Column(Float, default=90.0)
    target_performance_pct = Column(Float, default=95.0)
    target_quality_pct = Column(Float, default=99.0)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter")
