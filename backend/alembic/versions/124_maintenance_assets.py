"""Maintenance equipment assets, asset targets on PM records, and the maintenance-WO downtime link

Revision ID: 124_maintenance_assets
Revises: 123_vendor_asl
Create Date: 2026-11-13

Context
-------
Werco's preventive-maintenance sheet covers machines that are production work centers (the
lathes, mills, brakes, roller, router, lasers, paint) AND equipment that is not (EZ-TRAK and
Bridgeport mills, the shear, the cold saw, forklifts, air compressors). Owner decision
2026-11-13: the second group becomes EQUIPMENT ASSETS in a table of their own -- never fake
work centers (they would surface in scheduling, OEE, dispatch, the kiosk and routing pickers)
and never the calibration ``equipment`` table (gauges). A maintenance work order that shuts a
work center down also opens a ``DowntimeEvent``, linked back to the work order.

This revision:

``maintenance_assets`` (new, tenant-scoped)
  id, company_id (FK companies, indexed), asset_tag String(50) NOT NULL, name String(255) NOT
  NULL, asset_type String(50), manufacturer / model / serial_number / location String(100),
  notes Text, is_active Boolean NOT NULL DEFAULT true, version Integer NOT NULL DEFAULT 1,
  created_at / updated_at; UNIQUE (company_id, asset_tag). ROW LEVEL SECURITY enabled with no
  policies and the PostgREST roles revoked (docs/SUPABASE_SECURITY.md).

``maintenance_schedules`` / ``maintenance_work_orders`` / ``maintenance_logs``
  - ``asset_id`` Integer NULL, FK ``maintenance_assets.id`` (named ``fk_<table>_asset_id``), indexed.
  - ``work_center_id`` becomes NULLABLE.
  - CHECK ``ck_<table>_one_target``: exactly one of (work_center_id, asset_id) is set. Every
    existing row has a work center and no asset, so it validates over live data.

``downtime_events``
  - ``maintenance_work_order_id`` Integer NULL, FK ``maintenance_work_orders.id`` (named
    ``fk_downtime_events_maintenance_work_order_id``), indexed.
  - ``maintenance_adopted`` Boolean NOT NULL DEFAULT false: the link came from adopting an
    operator's open event (a cancelled work order hands such an event back, unresolved).

Every object mirrors ``app/models/maintenance.py`` / ``app/models/downtime.py`` and is
guarded INDEPENDENTLY (the 088/089 style): the fresh bootstrap (create_all -> stamp 058 ->
upgrade) runs this over a schema the models already built, and a partial bootstrap recovers
object by object. The table changes go through ``batch_alter_table`` (a passthrough on
PostgreSQL; a table rebuild on SQLite, which cannot ALTER a constraint or a NOT NULL). RLS /
REVOKE sit OUTSIDE every guard, PostgreSQL-only and idempotent, the anon / authenticated
REVOKEs under a ``pg_roles`` existence check (plain Postgres in CI has no Supabase roles).

No backfill, no data change: every existing maintenance row keeps its work center.

Downgrade
---------
Real, and REFUSES rather than destroy records: if any schedule, work order or log targets
an asset (``work_center_id IS NULL``), ``work_center_id`` cannot be made NOT NULL again, and
the downgrade raises naming the counts instead of deleting maintenance history. Otherwise it
drops the downtime link columns, the CHECKs, the asset columns, restores NOT NULL and drops
``maintenance_assets`` (LOSSY for the asset register itself -- export it first).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "124_maintenance_assets"
down_revision = "123_vendor_asl"
branch_labels = None
depends_on = None

ASSETS = "maintenance_assets"
ASSETS_UNIQUE = "uq_maintenance_assets_company_asset_tag"
ASSETS_COMPANY_INDEX = "ix_maintenance_assets_company_id"

TARGET_TABLES = ("maintenance_schedules", "maintenance_work_orders", "maintenance_logs")

# Byte-identical to app.models.maintenance.ONE_TARGET_CHECK (a test pins it).
ONE_TARGET_CHECK = (
    "(work_center_id IS NOT NULL AND asset_id IS NULL) OR (work_center_id IS NULL AND asset_id IS NOT NULL)"
)

DOWNTIME = "downtime_events"
DOWNTIME_COLUMN = "maintenance_work_order_id"
DOWNTIME_FK = "fk_downtime_events_maintenance_work_order_id"
DOWNTIME_INDEX = "ix_downtime_events_maintenance_work_order_id"
DOWNTIME_ADOPTED_COLUMN = "maintenance_adopted"

# PostgreSQL only, and LOCAL (precedent 059/060/114/117/123): bounds the wait for the ACCESS
# EXCLUSIVE locks, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _check_name(table_name: str) -> str:
    return f"ck_{table_name}_one_target"


def _fk_name(table_name: str) -> str:
    return f"fk_{table_name}_asset_id"


def _index_name(table_name: str) -> str:
    return f"ix_{table_name}_asset_id"


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    if _offline():
        return True
    return _inspector().has_table(table_name)


def _columns(table_name: str) -> dict:
    if _offline() or not _has_table(table_name):
        return {}
    return {col["name"]: col for col in _inspector().get_columns(table_name)}


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in _columns(table_name)


def _is_nullable(table_name: str, column_name: str) -> bool:
    column = _columns(table_name).get(column_name)
    return bool(column and column.get("nullable"))


def _has_index(table_name: str, index_name: str) -> bool:
    if _offline() or not _has_table(table_name):
        return False
    return any(ix.get("name") == index_name for ix in _inspector().get_indexes(table_name))


def _has_unique(table_name: str, constraint_name: str) -> bool:
    if _offline() or not _has_table(table_name):
        return False
    return any(uq.get("name") == constraint_name for uq in _inspector().get_unique_constraints(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if _offline() or not _has_table(table_name):
        return False
    return any(fk.get("name") == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def _has_check(table_name: str, constraint_name: str) -> bool:
    """Name membership is sufficient: the name is explicit on both the migration path and the
    model's CheckConstraint (the create_all path), so the two cannot diverge on it."""
    if _offline() or not _has_table(table_name):
        return False
    return any(ck.get("name") == constraint_name for ck in _inspector().get_check_constraints(table_name))


def _create_assets_table() -> None:
    if not (_offline() or not _has_table(ASSETS)):
        return
    op.create_table(
        ASSETS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("asset_tag", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=True),
        sa.Column("manufacturer", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("serial_number", sa.String(length=100), nullable=True),
        sa.Column("location", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "asset_tag", name=ASSETS_UNIQUE),
    )


def _upgrade_assets() -> None:
    _create_assets_table()
    if not _offline() and _has_table(ASSETS) and not _has_unique(ASSETS, ASSETS_UNIQUE):
        # A partial bootstrap: the table exists without its business key.
        with op.batch_alter_table(ASSETS) as batch:
            batch.create_unique_constraint(ASSETS_UNIQUE, ["company_id", "asset_tag"])
    if _offline() or not _has_index(ASSETS, ASSETS_COMPANY_INDEX):
        op.create_index(ASSETS_COMPANY_INDEX, ASSETS, ["company_id"], unique=False)


def _upgrade_target_table(table_name: str) -> None:
    if not _has_table(table_name):
        return
    offline = _offline()
    add_column = offline or not _has_column(table_name, "asset_id")
    add_fk = offline or not _has_foreign_key(table_name, _fk_name(table_name))
    relax_wc = offline or not _is_nullable(table_name, "work_center_id")
    add_check = offline or not _has_check(table_name, _check_name(table_name))
    add_index = offline or not _has_index(table_name, _index_name(table_name))
    if not (add_column or add_fk or relax_wc or add_check or add_index):
        return
    # Every op inside the batch is guarded too: on SQLite any constraint op rebuilds the table,
    # and re-adding an existing column there is what fails.
    with op.batch_alter_table(table_name) as batch:
        if add_column:
            batch.add_column(sa.Column("asset_id", sa.Integer(), nullable=True))
        if add_fk:
            batch.create_foreign_key(_fk_name(table_name), ASSETS, ["asset_id"], ["id"])
        if relax_wc:
            batch.alter_column("work_center_id", existing_type=sa.Integer(), nullable=True)
        if add_check:
            batch.create_check_constraint(_check_name(table_name), sa.text(ONE_TARGET_CHECK))
        if add_index:
            batch.create_index(_index_name(table_name), ["asset_id"], unique=False)


def _upgrade_downtime() -> None:
    if not _has_table(DOWNTIME):
        return
    offline = _offline()
    add_column = offline or not _has_column(DOWNTIME, DOWNTIME_COLUMN)
    add_fk = offline or not _has_foreign_key(DOWNTIME, DOWNTIME_FK)
    add_index = offline or not _has_index(DOWNTIME, DOWNTIME_INDEX)
    add_adopted = offline or not _has_column(DOWNTIME, DOWNTIME_ADOPTED_COLUMN)
    if not (add_column or add_fk or add_index or add_adopted):
        return
    with op.batch_alter_table(DOWNTIME) as batch:
        if add_column:
            batch.add_column(sa.Column(DOWNTIME_COLUMN, sa.Integer(), nullable=True))
        if add_fk:
            batch.create_foreign_key(DOWNTIME_FK, "maintenance_work_orders", [DOWNTIME_COLUMN], ["id"])
        if add_index:
            batch.create_index(DOWNTIME_INDEX, [DOWNTIME_COLUMN], unique=False)
        if add_adopted:
            # A constant default: PostgreSQL 11+ adds it without rewriting the table, and every
            # existing row (none is linked yet) correctly reads false.
            batch.add_column(
                sa.Column(DOWNTIME_ADOPTED_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("false"))
            )


def _secure_assets_table() -> None:
    # RLS / REVOKE run under the dialect check only -- never under a table guard -- because
    # create_all can build this table before this revision runs. Idempotent on PostgreSQL.
    op.execute("ALTER TABLE maintenance_assets ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE maintenance_assets FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM PUBLIC")
    op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE maintenance_assets FROM anon;
            REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE maintenance_assets FROM authenticated;
            REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM authenticated;
          END IF;
        END $$""")


def upgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    # The asset table first: the three asset_id foreign keys point at it.
    _upgrade_assets()
    for table_name in TARGET_TABLES:
        _upgrade_target_table(table_name)
    _upgrade_downtime()

    if postgres:
        _secure_assets_table()
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def _asset_targeted_counts() -> dict:
    """Rows per table whose target is an asset (they cannot take NOT NULL work_center_id)."""
    if _offline():
        return {}
    counts = {}
    bind = op.get_bind()
    for table_name in TARGET_TABLES:
        if not _has_column(table_name, "work_center_id"):
            continue
        found = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE work_center_id IS NULL")).scalar()
        if found:
            counts[table_name] = int(found)
    return counts


def _downgrade_target_table(table_name: str) -> None:
    if not _has_table(table_name):
        return
    offline = _offline()
    drop_index = offline or _has_index(table_name, _index_name(table_name))
    drop_check = offline or _has_check(table_name, _check_name(table_name))
    drop_fk = offline or _has_foreign_key(table_name, _fk_name(table_name))
    drop_column = offline or _has_column(table_name, "asset_id")
    restore_wc = offline or (_has_column(table_name, "work_center_id") and _is_nullable(table_name, "work_center_id"))
    if not (drop_index or drop_check or drop_fk or drop_column or restore_wc):
        return
    with op.batch_alter_table(table_name) as batch:
        if drop_index:
            batch.drop_index(_index_name(table_name))
        if drop_check:
            batch.drop_constraint(_check_name(table_name), type_="check")
        if drop_fk:
            batch.drop_constraint(_fk_name(table_name), type_="foreignkey")
        if drop_column:
            batch.drop_column("asset_id")
        if restore_wc:
            batch.alter_column("work_center_id", existing_type=sa.Integer(), nullable=False)


def _downgrade_downtime() -> None:
    if not _has_table(DOWNTIME):
        return
    offline = _offline()
    drop_index = offline or _has_index(DOWNTIME, DOWNTIME_INDEX)
    drop_fk = offline or _has_foreign_key(DOWNTIME, DOWNTIME_FK)
    drop_column = offline or _has_column(DOWNTIME, DOWNTIME_COLUMN)
    drop_adopted = offline or _has_column(DOWNTIME, DOWNTIME_ADOPTED_COLUMN)
    if not (drop_index or drop_fk or drop_column or drop_adopted):
        return
    with op.batch_alter_table(DOWNTIME) as batch:
        if drop_adopted:
            batch.drop_column(DOWNTIME_ADOPTED_COLUMN)
        if drop_index:
            batch.drop_index(DOWNTIME_INDEX)
        if drop_fk:
            batch.drop_constraint(DOWNTIME_FK, type_="foreignkey")
        if drop_column:
            batch.drop_column(DOWNTIME_COLUMN)


def downgrade() -> None:
    blocked = _asset_targeted_counts()
    if blocked:
        detail = ", ".join(f"{table}: {count}" for table, count in sorted(blocked.items()))
        raise RuntimeError(
            "124_maintenance_assets cannot be downgraded: maintenance rows target an equipment asset "
            f"({detail}) and work_center_id cannot be made NOT NULL again. Re-point or remove them first; "
            "this downgrade never deletes maintenance history."
        )

    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    _downgrade_downtime()
    for table_name in reversed(TARGET_TABLES):
        _downgrade_target_table(table_name)
    if _offline() or _has_table(ASSETS):
        # LOSSY: the asset register goes with the table (its RLS / grants go with it).
        op.drop_table(ASSETS)

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
