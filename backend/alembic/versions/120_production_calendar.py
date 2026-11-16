"""Production calendar: the production-day start, shift patterns and shift definitions.

Revision ID: 120_production_calendar
Revises: 119_api_token_scope

Additive configuration, no data:

- ``production_calendar_settings`` -- one row per company (``production_day_start_minute``,
  the document ``version`` every edit compares and bumps, ``updated_by``/``updated_at``).
  NO ROW IS SEEDED: a company without a row is "unconfigured" -- its production day is
  its company-local calendar day and it has no shifts. Werco's configuration is applied by
  an Admin through ``PUT /production-calendar`` (docs/API.md -> Production calendar), not
  here: it is tenant-specific and ``updated_by`` must name the person who set it.
- ``shift_patterns`` -- named groups of work centers; at most one ``is_default`` per company
  (a partial unique index declared for both dialects).
- ``shift_pattern_work_centers`` -- membership; a work center is in at most one pattern.
- ``shift_definitions`` -- integer wall-clock minutes on the start day (``end_minute`` above
  1440 ends the next day), ``days_mask`` (bit 0 = Monday), ``planned_minutes``.
- ``oee_records.shift_id`` -- nullable FK to ``shift_definitions`` plus its index. The
  existing free-text ``shift`` column stays as the label snapshot. NO BACKFILL: no shift is
  defined when this runs, mapping a legacy '1st' to a definition would be a guess, and a
  backfill would rewrite quality records with no audit row.

Every object is guarded on its own (the 088/090/118 idiom): the fresh bootstrap runs this
over a schema ``create_all`` already built. RLS / REVOKE for the four new tables runs under
the dialect check only, never under a table guard, and names the Supabase roles only under
a ``pg_roles`` existence check. Mirrors ``app/models/production_calendar.py`` and
``app/models/oee.py`` exactly.

SQLite note: adding / dropping the ``shift_id`` FK column uses batch mode, which rebuilds
``oee_records`` from reflection -- and SQLAlchemy cannot reflect the expression index
``uq_oee_company_wc_date_shift`` (``COALESCE(shift, '')``, migration 063), so the rebuild
would silently drop it. Both directions re-create it (guarded) after the batch.
"""

import sqlalchemy as sa

from alembic import op

revision = "120_production_calendar"
down_revision = "119_api_token_scope"
branch_labels = None
depends_on = None

SETTINGS = "production_calendar_settings"
PATTERNS = "shift_patterns"
MEMBERS = "shift_pattern_work_centers"
SHIFTS = "shift_definitions"
NEW_TABLES = (SETTINGS, PATTERNS, MEMBERS, SHIFTS)

OEE = "oee_records"
OEE_SHIFT_COLUMN = "shift_id"
OEE_SHIFT_FK = "fk_oee_records_shift_id"
OEE_SHIFT_INDEX = "ix_oee_records_shift_id"
OEE_UNIQUE_INDEX = "uq_oee_company_wc_date_shift"

DEFAULT_PATTERN_INDEX = "uq_shift_patterns_company_default"
DEFAULT_PATTERN_PREDICATE = "is_default"


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit every statement.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return not _offline() and _inspector().has_table(table)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(col["name"] == column for col in _inspector().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    if _dialect() == "sqlite":
        # sqlite_master sees expression and partial indexes that reflection skips.
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name"), {"name": name}
        )
        return found.first() is not None
    return any(index["name"] == name for index in _inspector().get_indexes(table))


def _has_foreign_key(table: str, name: str) -> bool:
    return _has_table(table) and any(fk["name"] == name for fk in _inspector().get_foreign_keys(table))


def _index(table: str, name: str, columns, **kwargs) -> None:
    if _offline() or not _has_index(table, name):
        op.create_index(name, table, columns, **kwargs)


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def _create_tables() -> None:
    if not _has_table(SETTINGS):
        op.create_table(
            SETTINGS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("production_day_start_minute", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", name="uq_production_calendar_company"),
            sa.CheckConstraint(
                "production_day_start_minute >= 0 AND production_day_start_minute <= 1439",
                name="ck_production_calendar_day_start",
            ),
            sa.CheckConstraint("version >= 1", name="ck_production_calendar_version"),
        )
    _index(SETTINGS, "ix_production_calendar_settings_company_id", ["company_id"])

    if not _has_table(PATTERNS):
        op.create_table(
            PATTERNS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.UniqueConstraint("company_id", "name", name="uq_shift_patterns_company_name"),
        )
    _index(PATTERNS, "ix_shift_patterns_company_id", ["company_id"])
    _index(
        PATTERNS,
        DEFAULT_PATTERN_INDEX,
        ["company_id"],
        unique=True,
        postgresql_where=sa.text(DEFAULT_PATTERN_PREDICATE),
        sqlite_where=sa.text(DEFAULT_PATTERN_PREDICATE),
    )

    if not _has_table(MEMBERS):
        op.create_table(
            MEMBERS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("shift_pattern_id", sa.Integer(), sa.ForeignKey("shift_patterns.id"), nullable=False),
            sa.Column("work_center_id", sa.Integer(), sa.ForeignKey("work_centers.id"), nullable=False),
            sa.UniqueConstraint("company_id", "work_center_id", name="uq_shift_pattern_wc_company_wc"),
        )
    _index(MEMBERS, "ix_shift_pattern_work_centers_company_id", ["company_id"])
    _index(MEMBERS, "ix_shift_pattern_work_centers_shift_pattern_id", ["shift_pattern_id"])

    if not _has_table(SHIFTS):
        op.create_table(
            SHIFTS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("shift_pattern_id", sa.Integer(), sa.ForeignKey("shift_patterns.id"), nullable=False),
            sa.Column("code", sa.String(20), nullable=False),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("start_minute", sa.Integer(), nullable=False),
            sa.Column("end_minute", sa.Integer(), nullable=False),
            sa.Column("days_mask", sa.Integer(), nullable=False),
            sa.Column("planned_minutes", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            *_timestamps(),
            sa.UniqueConstraint("company_id", "code", name="uq_shift_definitions_company_code"),
            sa.CheckConstraint("start_minute >= 0 AND start_minute <= 1439", name="ck_shift_definitions_start"),
            sa.CheckConstraint(
                "end_minute > start_minute AND end_minute <= start_minute + 1440", name="ck_shift_definitions_end"
            ),
            sa.CheckConstraint("days_mask >= 1 AND days_mask <= 127", name="ck_shift_definitions_days"),
            sa.CheckConstraint(
                "planned_minutes > 0 AND planned_minutes <= end_minute - start_minute",
                name="ck_shift_definitions_planned",
            ),
        )
    _index(SHIFTS, "ix_shift_definitions_company_id", ["company_id"])
    _index(SHIFTS, "ix_shift_definitions_shift_pattern_id", ["shift_pattern_id"])


def _restore_oee_unique_index() -> None:
    """Re-create migration 063's expression index when a SQLite batch rebuild dropped it."""
    if _dialect() == "sqlite" and not _offline() and _has_table(OEE) and not _has_index(OEE, OEE_UNIQUE_INDEX):
        op.create_index(
            OEE_UNIQUE_INDEX,
            OEE,
            ["company_id", "work_center_id", "record_date", sa.text("COALESCE(shift, '')")],
            unique=True,
        )


def _add_oee_shift_id() -> None:
    offline = _offline()
    add_column = offline or not _has_column(OEE, OEE_SHIFT_COLUMN)
    add_fk = offline or not _has_foreign_key(OEE, OEE_SHIFT_FK)
    add_index = offline or not _has_index(OEE, OEE_SHIFT_INDEX)
    if add_column or add_fk or add_index:
        with op.batch_alter_table(OEE) as batch:
            if add_column:
                batch.add_column(sa.Column(OEE_SHIFT_COLUMN, sa.Integer(), nullable=True))
            if add_fk:
                batch.create_foreign_key(OEE_SHIFT_FK, SHIFTS, [OEE_SHIFT_COLUMN], ["id"])
            if add_index:
                batch.create_index(OEE_SHIFT_INDEX, [OEE_SHIFT_COLUMN])
        _restore_oee_unique_index()


def _security_statements(table: str) -> tuple:
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
    END $$""",  # nosec B608 - ``table`` is one of NEW_TABLES, never input
    )


def upgrade() -> None:
    _create_tables()
    _add_oee_shift_id()
    # RLS / REVOKE under the dialect check only -- never under a table guard -- because
    # create_all can build these tables before this revision runs (Postgres-only;
    # idempotent). Deny-by-default with zero policies, like every table since 059.
    if _dialect() == "postgresql":
        for table in NEW_TABLES:
            for statement in _security_statements(table):
                op.execute(statement)


def downgrade() -> None:
    # Drops every shift definition and production calendar setting (configuration only:
    # no production record is lost). OEE records keep their ``shift`` label snapshot.
    offline = _offline()
    drop_index = offline or _has_index(OEE, OEE_SHIFT_INDEX)
    drop_fk = offline or _has_foreign_key(OEE, OEE_SHIFT_FK)
    drop_column = offline or _has_column(OEE, OEE_SHIFT_COLUMN)
    if drop_index or drop_fk or drop_column:
        with op.batch_alter_table(OEE) as batch:
            if drop_index:
                batch.drop_index(OEE_SHIFT_INDEX)
            if drop_fk:
                batch.drop_constraint(OEE_SHIFT_FK, type_="foreignkey")
            if drop_column:
                batch.drop_column(OEE_SHIFT_COLUMN)
        _restore_oee_unique_index()
    for table in reversed(NEW_TABLES):
        if offline or _has_table(table):
            op.drop_table(table)
