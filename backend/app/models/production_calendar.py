"""The company's production calendar: production-day start, shift patterns and shift definitions.

Configuration, not production records. ``production_calendar_settings`` is a singleton per
company (the day start and the document ``version`` every edit compares and bumps); a
missing row means "unconfigured": production day = company-local calendar day, no shifts.
``shift_patterns`` group work centers (Werco: "Weld shop", "Rest of shop"); a work center
belongs to at most one pattern (``shift_pattern_work_centers``) and every unassigned one
uses the company's single default pattern. ``shift_definitions`` are integer wall-clock
minutes on the day the shift starts (``end_minute`` above 1440 ends the next day).

Writes go through ``production_calendar_service.save_production_calendar`` only (a whole-
document replace with CAS on ``version`` and a REQUIRED audit row). A shift an OEE record
references is retired (``is_active = false``), never deleted, and its code never changes.
Mirrors migration 120_production_calendar, including the Postgres RLS/REVOKE DDL.
"""

from datetime import datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)

from app.db.database import Base
from app.db.mixins import TenantMixin

# One default pattern per company. Declared for BOTH dialects (the 074/076/087
# convention): a postgresql_where alone degrades to a FULL unique index on SQLite.
DEFAULT_PATTERN_PREDICATE = "is_default"
UNIQUE_DEFAULT_PATTERN_INDEX = "uq_shift_patterns_company_default"


class ProductionCalendarSettings(Base, TenantMixin):
    __tablename__ = "production_calendar_settings"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_production_calendar_company"),
        CheckConstraint(
            "production_day_start_minute >= 0 AND production_day_start_minute <= 1439",
            name="ck_production_calendar_day_start",
        ),
        CheckConstraint("version >= 1", name="ck_production_calendar_version"),
    )

    id = Column(Integer, primary_key=True)
    production_day_start_minute = Column(Integer, nullable=False, default=0, server_default="0")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShiftPattern(Base, TenantMixin):
    __tablename__ = "shift_patterns"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_shift_patterns_company_name"),
        Index(
            UNIQUE_DEFAULT_PATTERN_INDEX,
            "company_id",
            unique=True,
            postgresql_where=text(DEFAULT_PATTERN_PREDICATE),
            sqlite_where=text(DEFAULT_PATTERN_PREDICATE),
        ),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False, server_default="false")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShiftPatternWorkCenter(Base, TenantMixin):
    __tablename__ = "shift_pattern_work_centers"
    __table_args__ = (
        # A work center is in at most one pattern, so its shifts are never ambiguous.
        UniqueConstraint("company_id", "work_center_id", name="uq_shift_pattern_wc_company_wc"),
    )

    id = Column(Integer, primary_key=True)
    shift_pattern_id = Column(Integer, ForeignKey("shift_patterns.id"), nullable=False, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)


class ShiftDefinition(Base, TenantMixin):
    __tablename__ = "shift_definitions"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_shift_definitions_company_code"),
        CheckConstraint("start_minute >= 0 AND start_minute <= 1439", name="ck_shift_definitions_start"),
        CheckConstraint(
            "end_minute > start_minute AND end_minute <= start_minute + 1440", name="ck_shift_definitions_end"
        ),
        CheckConstraint("days_mask >= 1 AND days_mask <= 127", name="ck_shift_definitions_days"),
        CheckConstraint(
            "planned_minutes > 0 AND planned_minutes <= end_minute - start_minute",
            name="ck_shift_definitions_planned",
        ),
    )

    id = Column(Integer, primary_key=True)
    shift_pattern_id = Column(Integer, ForeignKey("shift_patterns.id"), nullable=False, index=True)
    code = Column(String(20), nullable=False)
    name = Column(String(50), nullable=False)
    start_minute = Column(Integer, nullable=False)
    end_minute = Column(Integer, nullable=False)
    days_mask = Column(Integer, nullable=False)
    planned_minutes = Column(Integer, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


PRODUCTION_CALENDAR_TABLES = (
    "production_calendar_settings",
    "shift_patterns",
    "shift_pattern_work_centers",
    "shift_definitions",
)


def security_sql(table: str) -> tuple:
    """RLS + REVOKE for one of ``PRODUCTION_CALENDAR_TABLES``; the migration emits the identical statements."""
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC",
        f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE {table} FROM anon;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE {table} FROM authenticated;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM authenticated;
      END IF;
    END $$""",  # nosec B608 - ``table`` is one of this module's four table names, never input
    )


# Mirror migration 120 for model-bootstrap installations: deny-by-default RLS with zero
# policies; PostgREST client roles receive no grants. ERP server auth owns access.
for _model in (ProductionCalendarSettings, ShiftPattern, ShiftPatternWorkCenter, ShiftDefinition):
    for _statement in security_sql(_model.__tablename__):
        event.listen(_model.__table__, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
