"""Preventive maintenance: equipment assets, PM schedules, maintenance work orders and logs.

A maintenance record targets EXACTLY ONE thing: a production ``WorkCenter`` or a non-production
``MaintenanceAsset`` (forklifts, air compressors, a manual mill -- equipment that is maintained
but never scheduled, dispatched, run on a kiosk or counted in OEE). ``work_center_id`` and
``asset_id`` are both nullable and a CHECK on each table (``ONE_TARGET_CHECK``) holds exactly
one of them set. Lock-step with migration ``124_maintenance_assets``.
"""

import enum
from datetime import datetime

from sqlalchemy import DDL, Boolean, CheckConstraint, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class MaintenanceType(str, enum.Enum):
    PREVENTIVE = "preventive"
    CORRECTIVE = "corrective"
    PREDICTIVE = "predictive"
    EMERGENCY = "emergency"


class MaintenancePriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MaintenanceStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    ON_HOLD = "on_hold"


class MaintenanceFrequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"
    CUSTOM = "custom"


# A PM schedule's interval, read by ``app.services.maintenance_interval.next_due_date`` (the one place
# a next due date is computed). Day cadences add days; calendar cadences add CALENDAR MONTHS, clamped
# to the end of a shorter month (owner decision 2026-11-20): monthly used to be a fixed 30 days and
# annual 365, so an on-time monthly PM drifted about five days earlier every year. ``custom`` adds
# the schedule's own ``frequency_days``.
FREQUENCY_DAYS_MAP = {
    MaintenanceFrequency.DAILY: 1,
    MaintenanceFrequency.WEEKLY: 7,
    MaintenanceFrequency.BIWEEKLY: 14,
}
FREQUENCY_MONTHS_MAP = {
    MaintenanceFrequency.MONTHLY: 1,
    MaintenanceFrequency.QUARTERLY: 3,
    MaintenanceFrequency.SEMI_ANNUAL: 6,
    MaintenanceFrequency.ANNUAL: 12,
}


# A maintenance work order in one of these is still OPEN: its PM schedule takes no second one until it
# ends (the endpoints' ``_OPEN_STATUSES``, the PM import's open-work-order probe and the partial
# unique index below all read this one tuple).
OPEN_MAINTENANCE_STATUSES = (
    MaintenanceStatus.SCHEDULED,
    MaintenanceStatus.OVERDUE,
    MaintenanceStatus.IN_PROGRESS,
    MaintenanceStatus.ON_HOLD,
)

# One OPEN maintenance work order per PM schedule, in the database (migration 132). The rule is
# enforced first by ``create_work_order`` under the schedule's row lock (a 409 naming the open work
# order); this index is the backstop for any future writer. ``SQLEnum(MaintenanceStatus)`` persists
# member NAMES, so the predicate names them. Byte-identical in migration ``132_maintenance_pm_integrity``
# (a test pins it, and that it lists exactly ``OPEN_MAINTENANCE_STATUSES``), and declared for BOTH
# dialects (the 074/076/087 convention): a ``postgresql_where`` alone would make SQLite build a FULL
# unique index -- one work order per schedule, ever.
OPEN_SCHEDULE_WORK_ORDER_INDEX = "uq_maintenance_work_orders_open_schedule"
OPEN_SCHEDULE_WORK_ORDER_PREDICATE = (
    "schedule_id IS NOT NULL AND status IN ('SCHEDULED', 'OVERDUE', 'IN_PROGRESS', 'ON_HOLD')"
)


# Exactly one maintenance target per row (byte-identical in migration 124; a test pins it).
ONE_TARGET_CHECK = (
    "(work_center_id IS NOT NULL AND asset_id IS NULL) OR (work_center_id IS NULL AND asset_id IS NOT NULL)"
)


class MaintenanceAsset(Base, TenantMixin):
    """A maintained piece of NON-PRODUCTION equipment (owner decision 2026-11-13).

    Deliberately not a ``WorkCenter`` (a fake work center would surface in scheduling, OEE,
    dispatch, the kiosk and routing pickers) and not the calibration ``equipment`` table
    (gauges with calibration intervals). Nothing outside ``/maintenance`` reads this table.
    ``asset_tag`` is unique per company; ``is_active`` is the retirement flag (no physical
    delete). ``version`` is the compare-and-swap counter ``PUT /maintenance/assets/{id}``
    checks.
    """

    __tablename__ = "maintenance_assets"
    __table_args__ = (UniqueConstraint("company_id", "asset_tag", name="uq_maintenance_assets_company_asset_tag"),)

    id = Column(Integer, primary_key=True)
    asset_tag = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    asset_type = Column(String(50), nullable=True)
    manufacturer = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)
    location = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# Mirror migration 124 for model-bootstrap installations (precedent: dashboard_layouts). App-
# layer tenancy is the enforcement; the Supabase Data API roles get nothing.
_ASSET_SECURITY_SQL = (
    "ALTER TABLE maintenance_assets ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON TABLE maintenance_assets FROM PUBLIC",
    "REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM PUBLIC",
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE maintenance_assets FROM anon;
        REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE maintenance_assets FROM authenticated;
        REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM authenticated;
      END IF;
    END $$""",
)
for _statement in _ASSET_SECURITY_SQL:
    event.listen(MaintenanceAsset.__table__, "after_create", DDL(_statement).execute_if(dialect="postgresql"))


class MaintenanceSchedule(Base, TenantMixin):
    """Preventive Maintenance schedule template"""

    __tablename__ = "maintenance_schedules"
    __table_args__ = (CheckConstraint(ONE_TARGET_CHECK, name="ck_maintenance_schedules_one_target"),)

    id = Column(Integer, primary_key=True, index=True)
    # Exactly one of the two (ONE_TARGET_CHECK).
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    asset_id = Column(
        Integer,
        ForeignKey("maintenance_assets.id", name="fk_maintenance_schedules_asset_id"),
        nullable=True,
        index=True,
    )

    name = Column(String(255), nullable=False)
    description = Column(Text)

    maintenance_type = Column(SQLEnum(MaintenanceType), default=MaintenanceType.PREVENTIVE)
    frequency = Column(SQLEnum(MaintenanceFrequency))
    frequency_days = Column(Integer, nullable=True)  # custom interval in days
    estimated_duration_hours = Column(Float, default=1.0)

    priority = Column(SQLEnum(MaintenancePriority), default=MaintenancePriority.MEDIUM)
    checklist = Column(Text)  # JSON string of checklist items
    requires_shutdown = Column(Boolean, default=False)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    last_completed_date = Column(Date, nullable=True)
    next_due_date = Column(Date, nullable=True)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    asset = relationship("MaintenanceAsset", foreign_keys=[asset_id])
    work_orders = relationship("MaintenanceWorkOrder", back_populates="schedule")


class MaintenanceWorkOrder(Base, TenantMixin):
    """Individual maintenance work order / task"""

    __tablename__ = "maintenance_work_orders"
    __table_args__ = (
        CheckConstraint(ONE_TARGET_CHECK, name="ck_maintenance_work_orders_one_target"),
        Index(
            OPEN_SCHEDULE_WORK_ORDER_INDEX,
            "company_id",
            "schedule_id",
            unique=True,
            postgresql_where=text(OPEN_SCHEDULE_WORK_ORDER_PREDICATE),
            sqlite_where=text(OPEN_SCHEDULE_WORK_ORDER_PREDICATE),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer, ForeignKey("maintenance_schedules.id"), nullable=True)
    # Exactly one of the two (ONE_TARGET_CHECK).
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    asset_id = Column(
        Integer,
        ForeignKey("maintenance_assets.id", name="fk_maintenance_work_orders_asset_id"),
        nullable=True,
        index=True,
    )

    wo_number = Column(String(50), unique=True, index=True, nullable=False)

    maintenance_type = Column(SQLEnum(MaintenanceType), default=MaintenanceType.PREVENTIVE)
    priority = Column(SQLEnum(MaintenancePriority), default=MaintenancePriority.MEDIUM)
    status = Column(SQLEnum(MaintenanceStatus), default=MaintenanceStatus.SCHEDULED)

    title = Column(String(255), nullable=False)
    description = Column(Text)
    checklist_results = Column(Text)  # JSON with completed checklist items
    # The PM schedule's checklist as it stood when this work order was OPENED (a JSON array of
    # strings; owner decision 2026-11-20, migration 132). Completion judges these steps, and every
    # response serves them as ``schedule_checklist_items``, so a later schedule edit or PM import
    # binds the NEXT work order, never this one or a finished record. ``NULL`` on a work order
    # without a schedule, and on a work order opened before migration 132: that one keeps the
    # pre-132 behavior of reading the schedule's live checklist until completing or cancelling it
    # writes the steps it answered for here (never backfilled -- the list a legacy work order was opened with is unknowable,
    # and an UPDATE outside AuditService would break invariant 2). ``"[]"`` is a schedule that had
    # no steps, not a legacy row.
    schedule_checklist_snapshot = Column(Text, nullable=True)

    scheduled_date = Column(Date)
    due_date = Column(Date)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    actual_duration_hours = Column(Float, nullable=True)
    requires_shutdown = Column(Boolean, default=False)
    downtime_minutes = Column(Float, default=0)

    parts_used = Column(Text)  # JSON list of spare parts used
    labor_cost = Column(Float, default=0)
    parts_cost = Column(Float, default=0)
    total_cost = Column(Float, default=0)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    notes = Column(Text)
    findings = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    asset = relationship("MaintenanceAsset", foreign_keys=[asset_id])
    schedule = relationship("MaintenanceSchedule", back_populates="work_orders")


class MaintenanceLog(Base, TenantMixin):
    """History entry for maintenance events"""

    __tablename__ = "maintenance_logs"
    __table_args__ = (CheckConstraint(ONE_TARGET_CHECK, name="ck_maintenance_logs_one_target"),)

    id = Column(Integer, primary_key=True, index=True)
    # Exactly one of the two (ONE_TARGET_CHECK).
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    asset_id = Column(
        Integer,
        ForeignKey("maintenance_assets.id", name="fk_maintenance_logs_asset_id"),
        nullable=True,
        index=True,
    )
    maintenance_wo_id = Column(Integer, ForeignKey("maintenance_work_orders.id"), nullable=True)

    event_type = Column(String(50), nullable=False)  # completed, inspection, repair, part_replacement, observation
    description = Column(Text, nullable=False)
    performed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    event_date = Column(DateTime, default=datetime.utcnow)
    cost = Column(Float, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    work_center = relationship("WorkCenter", foreign_keys=[work_center_id])
    asset = relationship("MaintenanceAsset", foreign_keys=[asset_id])
    work_order = relationship("MaintenanceWorkOrder", foreign_keys=[maintenance_wo_id])
