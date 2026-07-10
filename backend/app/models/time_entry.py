import enum
from datetime import datetime

from sqlalchemy import Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class TimeEntryType(str, enum.Enum):
    SETUP = "setup"
    RUN = "run"
    REWORK = "rework"
    INSPECTION = "inspection"
    DOWNTIME = "downtime"
    BREAK = "break"


class TimeEntrySource(str, enum.Enum):
    """Adoption-telemetry channel that produced a labor/operation write (A0.1).

    Stored as a plain nullable string on ``TimeEntry.source`` (NULL = unknown /
    historical row; the server never guesses a channel the client didn't send).
    Distinguishes live digital capture (kiosk/scanner/desktop) from paper
    back-fill and bulk import so the adoption dashboard can compute clock-in
    coverage, digital completion %, and backfill rate.
    """

    KIOSK = "kiosk"
    DESKTOP = "desktop"
    SCANNER = "scanner"
    IMPORT = "import"
    BACKFILL = "backfill"


# Lean Phase 1 provenance rule, co-located with the vocabulary it partitions:
# every flow/quality/adoption metric EXCLUDES rows whose source is a
# backfill/bulk-import channel from its baseline and reports them separately --
# paper catch-up and migration loads must never masquerade as live capture.
# NULL source (unknown/legacy, or the office UI before A0.1) stays IN the
# baseline: it is real contemporaneous work, just with an unreported channel.
BASELINE_EXCLUDED_SOURCES = [TimeEntrySource.IMPORT.value, TimeEntrySource.BACKFILL.value]

# The channels that count as LIVE digital capture for adoption metrics (A0.1).
LIVE_CAPTURE_SOURCES = [TimeEntrySource.KIOSK.value, TimeEntrySource.DESKTOP.value, TimeEntrySource.SCANNER.value]


class TimeEntry(Base, TenantMixin):
    """Time tracking for shop floor labor"""

    __tablename__ = "time_entries"
    # Lock-step with migration 042_wo_completion_perf_indexes: backs the
    # reconcile_work_orders_from_completion_evidence rollups
    # (WHERE operation_id IN (...) [AND clock_out IS NOT NULL] GROUP BY operation_id)
    # and its ORDER BY operation_id, clock_out DESC latest-entry scan in
    # app/services/work_order_state_service.py.
    __table_args__ = (Index("ix_time_entries_operation_clock_out", "operation_id", "clock_out"),)

    id = Column(Integer, primary_key=True, index=True)

    # Optimistic locking (Batch 2 / SFI-2 / LOCK-1). The ``version`` column was
    # added at the DB level by migration ``004_add_optimistic_locking`` but was
    # never mapped, leaving locking inert. We map it here (scoped to this
    # completion-path model rather than the shared OptimisticLockMixin) so
    # SQLAlchemy enforces ``version_id_col`` on UPDATE: a concurrent stale write
    # to the same TimeEntry row raises StaleDataError, which the endpoint layer
    # translates to HTTP 409. Requires every row to have a non-null version;
    # migration 004 set server_default='1' and the Batch 2 migration backfills
    # any residual NULLs and re-asserts NOT NULL + server_default.
    version = Column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version}

    # Who/What/Where
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)

    # Time tracking
    entry_type = Column(SQLEnum(TimeEntryType), default=TimeEntryType.RUN)
    clock_in = Column(DateTime, nullable=False)
    clock_out = Column(DateTime, nullable=True)
    duration_hours = Column(Float, nullable=True)  # Calculated on clock_out

    # Production tracking
    quantity_produced = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)

    # A0.1 adoption telemetry: last known write channel for this labor record (values
    # from TimeEntrySource). Plain String (not SQLEnum) so adding a channel never needs
    # an ALTER TYPE; nullable because historical/paper-era rows are unknown -- NULL means
    # "not reported", never a guessed default. Note: /complete may fill a NULL on entries
    # it auto-closes with the completer's channel, so this column reads as "last channel
    # to touch the record"; the per-write capture channel is preserved losslessly on the
    # labor_clock_in/labor_clock_out OperationalEvent payloads -- compute per-operator
    # adoption metrics from events, not from this column.
    source = Column(String(20), nullable=True)

    # Notes and reason codes
    notes = Column(Text)
    scrap_reason = Column(String(255))
    # Lean Phase 1: structured scrap categorization. Nullable -- historical rows and
    # scrap=0 writes have no code; the free-text scrap_reason stays as narrative detail.
    scrap_reason_code_id = Column(Integer, ForeignKey("scrap_reason_codes.id"), nullable=True)
    downtime_reason = Column(String(255))

    # Approval workflow
    approved = Column(DateTime, nullable=True)
    approved_by = Column(Integer, nullable=True)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="time_entries")
    work_order = relationship("WorkOrder", back_populates="time_entries")
    operation = relationship("WorkOrderOperation", back_populates="time_entries")
    work_center = relationship("WorkCenter", back_populates="time_entries")
    scrap_reason_code = relationship("ScrapReasonCode", foreign_keys=[scrap_reason_code_id])
