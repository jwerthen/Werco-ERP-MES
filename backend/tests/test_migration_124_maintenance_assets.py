"""Coverage for 124_maintenance_assets: the equipment-asset table, the asset target on PM
schedules / maintenance work orders / logs (exactly-one-target CHECK), and the
``downtime_events.maintenance_work_order_id`` link.

Three layers:

1. Script wiring -- single head, the down-revision, the id fits ``alembic_version``.
2. A real upgrade -> replay -> downgrade -> replay round-trip over a scratch SQLite database
   holding the PRE-124 shape of the four tables WITH DATA: every object is guarded
   independently (a partial bootstrap recovers each one), the CHECK is enforced, existing
   rows survive both directions, and the downgrade refuses rather than delete maintenance
   history when a row targets an asset.
3. Model == migration lock-step: the CHECK text (byte-identical), the asset table's columns,
   key and FKs, and the PostgreSQL DDL rendered offline (RLS + REVOKE under ``pg_roles``
   checks in BOTH the migration and the model's ``after_create`` listener).
"""

import importlib.util
import io
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.downtime import DowntimeEvent
from app.models.maintenance import (
    ONE_TARGET_CHECK,
    MaintenanceAsset,
    MaintenanceLog,
    MaintenanceSchedule,
    MaintenanceWorkOrder,
)

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "124_maintenance_assets.py")
REVISION = "124_maintenance_assets"
DOWN_REVISION = "123_vendor_asl"
TARGET_TABLES = ("maintenance_schedules", "maintenance_work_orders", "maintenance_logs")
TARGET_MODELS = {
    "maintenance_schedules": MaintenanceSchedule,
    "maintenance_work_orders": MaintenanceWorkOrder,
    "maintenance_logs": MaintenanceLog,
}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_124", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


# --------------------------------------------------------------------------- wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert scripts.get_heads() == [REVISION]
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# --------------------------------------------------------------------------- round trip

PRE_124_DDL = (
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE work_centers (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
    "code VARCHAR(20) NOT NULL, name VARCHAR(100) NOT NULL)",
    "CREATE TABLE work_orders (id INTEGER PRIMARY KEY)",
    "CREATE TABLE maintenance_schedules (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
    "work_center_id INTEGER NOT NULL REFERENCES work_centers(id), name VARCHAR(255) NOT NULL, checklist TEXT, "
    "is_active BOOLEAN)",
    "CREATE INDEX ix_maintenance_schedules_company_id ON maintenance_schedules (company_id)",
    "CREATE TABLE maintenance_work_orders (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
    "schedule_id INTEGER REFERENCES maintenance_schedules(id), work_center_id INTEGER NOT NULL REFERENCES "
    "work_centers(id), wo_number VARCHAR(50) NOT NULL UNIQUE, title VARCHAR(255) NOT NULL, status VARCHAR(11))",
    "CREATE TABLE maintenance_logs (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
    "work_center_id INTEGER NOT NULL REFERENCES work_centers(id), maintenance_wo_id INTEGER REFERENCES "
    "maintenance_work_orders(id), event_type VARCHAR(50) NOT NULL, description TEXT NOT NULL)",
    "CREATE TABLE downtime_events (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
    "work_center_id INTEGER NOT NULL REFERENCES work_centers(id), work_order_id INTEGER REFERENCES work_orders(id), "
    "start_time DATETIME NOT NULL, end_time DATETIME, reported_by INTEGER NOT NULL REFERENCES users(id))",
    "CREATE INDEX ix_downtime_events_work_center_id ON downtime_events (work_center_id)",
)
SEED = (
    "INSERT INTO companies VALUES (1)",
    "INSERT INTO users VALUES (1)",
    "INSERT INTO work_centers VALUES (1, 1, 'CNC-01', 'Synthetic mill')",
    "INSERT INTO maintenance_schedules VALUES (1, 1, 1, 'CNC-01 Monthly PM', '[\"Oil ways\"]', 1)",
    "INSERT INTO maintenance_work_orders VALUES (1, 1, 1, 1, 'MWO-2026-0001', 'Monthly PM', 'COMPLETED')",
    "INSERT INTO maintenance_logs VALUES (1, 1, 1, 1, 'completed', 'done')",
    "INSERT INTO downtime_events VALUES (1, 1, 1, NULL, '2026-11-01 08:00:00', '2026-11-01 09:00:00', 1)",
)


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'maintenance-assets-migration.db'}")
    with engine.begin() as connection:
        for statement in PRE_124_DDL + SEED:
            connection.execute(sa.text(statement))
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _columns(connection, table):
    return {column["name"]: column for column in sa.inspect(connection).get_columns(table)}


def _names(connection, table, kind):
    inspector = sa.inspect(connection)
    getter = {
        "check": inspector.get_check_constraints,
        "index": inspector.get_indexes,
        "fk": inspector.get_foreign_keys,
        "unique": inspector.get_unique_constraints,
    }[kind]
    return {item["name"] for item in getter(table)}


def test_upgrade_adds_every_object_and_existing_rows_survive(scratch):
    connection, migration = scratch
    migration.upgrade()
    assert sa.inspect(connection).has_table("maintenance_assets")
    assert "uq_maintenance_assets_company_asset_tag" in _names(connection, "maintenance_assets", "unique")
    assert "ix_maintenance_assets_company_id" in _names(connection, "maintenance_assets", "index")
    for table in TARGET_TABLES:
        columns = _columns(connection, table)
        assert columns["asset_id"]["nullable"] is True
        assert columns["work_center_id"]["nullable"] is True
        assert f"ck_{table}_one_target" in _names(connection, table, "check")
        assert f"fk_{table}_asset_id" in _names(connection, table, "fk")
        assert f"ix_{table}_asset_id" in _names(connection, table, "index")
        # Every existing row keeps its work center and gains no asset.
        row = connection.execute(sa.text(f"SELECT work_center_id, asset_id FROM {table} WHERE id = 1")).one()
        assert tuple(row) == (1, None)
    assert "maintenance_work_order_id" in _columns(connection, "downtime_events")
    adopted = _columns(connection, "downtime_events")["maintenance_adopted"]
    assert adopted["nullable"] is False and str(adopted["default"]) == "false"
    # The existing (unlinked) event reads as not adopted.
    assert not connection.execute(sa.text("SELECT maintenance_adopted FROM downtime_events WHERE id = 1")).scalar()
    assert "fk_downtime_events_maintenance_work_order_id" in _names(connection, "downtime_events", "fk")
    assert "ix_downtime_events_maintenance_work_order_id" in _names(connection, "downtime_events", "index")
    # The batch rebuilds kept what was there.
    assert "ix_downtime_events_work_center_id" in _names(connection, "downtime_events", "index")
    assert connection.execute(sa.text("SELECT wo_number FROM maintenance_work_orders")).scalar() == "MWO-2026-0001"


def test_the_check_holds_exactly_one_target(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.execute(
        sa.text(
            "INSERT INTO maintenance_assets (id, company_id, asset_tag, name, is_active, version, created_at, "
            "updated_at) VALUES (1, 1, 'FL-01', 'Forklift', 1, 1, '2026-11-13', '2026-11-13')"
        )
    )
    connection.execute(
        sa.text("INSERT INTO maintenance_schedules (id, company_id, asset_id, name) VALUES (2, 1, 1, 'a')")
    )
    for values in ("(3, 1, NULL, NULL, 'neither')", "(4, 1, 1, 1, 'both')"):
        nested = connection.begin_nested()
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    f"INSERT INTO maintenance_schedules (id, company_id, work_center_id, asset_id, name) VALUES {values}"
                )
            )
        nested.rollback()
    nested = connection.begin_nested()
    with pytest.raises(IntegrityError):  # asset_tag is unique per company
        connection.execute(
            sa.text(
                "INSERT INTO maintenance_assets (company_id, asset_tag, name, is_active, version, created_at, "
                "updated_at) VALUES (1, 'FL-01', 'again', 1, 1, '2026-11-13', '2026-11-13')"
            )
        )
    nested.rollback()


def test_replay_is_a_no_op_and_a_partial_bootstrap_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    migration.upgrade()  # replay
    connection.execute(sa.text("DROP INDEX ix_maintenance_logs_asset_id"))
    connection.execute(sa.text("DROP INDEX ix_maintenance_assets_company_id"))
    connection.execute(sa.text("DROP INDEX ix_downtime_events_maintenance_work_order_id"))
    migration.upgrade()
    assert "ix_maintenance_logs_asset_id" in _names(connection, "maintenance_logs", "index")
    assert "ix_maintenance_assets_company_id" in _names(connection, "maintenance_assets", "index")
    assert "ix_downtime_events_maintenance_work_order_id" in _names(connection, "downtime_events", "index")
    assert connection.execute(sa.text("SELECT count(*) FROM maintenance_logs")).scalar() == 1


def test_downgrade_refuses_while_a_row_targets_an_asset_then_is_real(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.execute(
        sa.text(
            "INSERT INTO maintenance_assets (id, company_id, asset_tag, name, is_active, version, created_at, "
            "updated_at) VALUES (1, 1, 'FL-01', 'Forklift', 1, 1, '2026-11-13', '2026-11-13')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO maintenance_logs (id, company_id, asset_id, event_type, description) VALUES (2, 1, 1, 'x', 'y')"
        )
    )
    with pytest.raises(RuntimeError, match="maintenance_logs: 1"):
        migration.downgrade()
    assert connection.execute(sa.text("SELECT count(*) FROM maintenance_logs")).scalar() == 2  # nothing deleted

    connection.execute(sa.text("DELETE FROM maintenance_logs WHERE id = 2"))
    migration.downgrade()
    assert not sa.inspect(connection).has_table("maintenance_assets")
    for table in TARGET_TABLES:
        columns = _columns(connection, table)
        assert "asset_id" not in columns and columns["work_center_id"]["nullable"] is False
        assert f"ck_{table}_one_target" not in _names(connection, table, "check")
    assert "maintenance_work_order_id" not in _columns(connection, "downtime_events")
    assert "maintenance_adopted" not in _columns(connection, "downtime_events")
    migration.downgrade()  # a second downgrade is a no-op, not an error
    assert (
        connection.execute(sa.text("SELECT name FROM maintenance_schedules WHERE id = 1")).scalar()
        == "CNC-01 Monthly PM"
    )
    assert connection.execute(sa.text("SELECT count(*) FROM downtime_events")).scalar() == 1
    migration.upgrade()
    assert "asset_id" in _columns(connection, "maintenance_schedules")


# --------------------------------------------------------------------------- lock-step


def test_check_text_is_byte_identical_to_the_model():
    assert _module().ONE_TARGET_CHECK == ONE_TARGET_CHECK
    for table, model in TARGET_MODELS.items():
        checks = {c.name: str(c.sqltext) for c in model.__table__.constraints if isinstance(c, sa.CheckConstraint)}
        assert checks == {f"ck_{table}_one_target": ONE_TARGET_CHECK}
        assert model.__table__.c.work_center_id.nullable is True
        assert model.__table__.c.asset_id.nullable is True
        [fk] = model.__table__.c.asset_id.foreign_keys
        assert (fk.name, fk.target_fullname) == (f"fk_{table}_asset_id", "maintenance_assets.id")


def test_the_upgraded_asset_table_matches_the_model(scratch):
    connection, migration = scratch
    migration.upgrade()
    model = MaintenanceAsset.__table__
    reflected = _columns(connection, "maintenance_assets")
    assert set(reflected) == set(model.columns.keys())
    for column in model.columns:
        assert reflected[column.name]["nullable"] == column.nullable, column.name
        assert str(reflected[column.name]["type"]) == str(column.type), column.name
        if column.server_default is not None:
            assert str(reflected[column.name]["default"]).strip("'") == str(column.server_default.arg)
    assert {index.name for index in model.indexes} == _names(connection, "maintenance_assets", "index")
    [unique] = [c for c in model.constraints if isinstance(c, sa.UniqueConstraint)]
    assert unique.name in _names(connection, "maintenance_assets", "unique")
    [fk] = DowntimeEvent.__table__.c.maintenance_work_order_id.foreign_keys
    assert fk.name == "fk_downtime_events_maintenance_work_order_id"
    adopted_model = DowntimeEvent.__table__.c.maintenance_adopted
    adopted_reflected = _columns(connection, "downtime_events")["maintenance_adopted"]
    assert adopted_reflected["nullable"] == adopted_model.nullable is False
    assert str(adopted_reflected["type"]) == str(adopted_model.type)
    assert str(adopted_reflected["default"]) == str(adopted_model.server_default.arg) == "false"
    assert {index.name for index in DowntimeEvent.__table__.indexes} >= {"ix_downtime_events_maintenance_work_order_id"}


def _offline_sql(direction: str) -> str:
    output = io.StringIO()
    migration = _module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    getattr(migration, direction)()
    return output.getvalue()


def test_postgres_upgrade_and_model_bootstrap_are_default_deny():
    migration_sql = _offline_sql("upgrade")
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    MaintenanceAsset.__table__.create(engine, checkfirst=False)
    model_sql = "\n".join(statements)
    for sql in (migration_sql, model_sql):
        assert "ALTER TABLE maintenance_assets ENABLE ROW LEVEL SECURITY" in sql
        for role in ("PUBLIC", "anon", "authenticated"):
            assert f"REVOKE ALL ON TABLE maintenance_assets FROM {role}" in sql
            assert f"REVOKE ALL ON SEQUENCE maintenance_assets_id_seq FROM {role}" in sql
        for role in ("anon", "authenticated"):
            assert f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')" in sql
        assert "CREATE POLICY" not in sql and "service_role" not in sql
        assert "UNIQUE (company_id, asset_tag)" in sql
    for table in TARGET_TABLES:
        assert f"ALTER TABLE {table} ALTER COLUMN work_center_id DROP NOT NULL" in migration_sql
        assert (
            f"ADD CONSTRAINT fk_{table}_asset_id FOREIGN KEY(asset_id) REFERENCES maintenance_assets (id)"
            in migration_sql
        )
        assert f"ADD CONSTRAINT ck_{table}_one_target CHECK ({ONE_TARGET_CHECK})" in migration_sql
    assert (
        "ADD CONSTRAINT fk_downtime_events_maintenance_work_order_id FOREIGN KEY(maintenance_work_order_id) "
        "REFERENCES maintenance_work_orders (id)"
    ) in migration_sql
    assert "ALTER TABLE downtime_events ADD COLUMN maintenance_adopted BOOLEAN DEFAULT false NOT NULL" in migration_sql
    assert "SET LOCAL lock_timeout = '5s'" in migration_sql
    assert "INSERT INTO" not in migration_sql and "UPDATE " not in migration_sql


def test_postgres_downgrade_restores_not_null_and_drops_the_table():
    sql = _offline_sql("downgrade")
    for table in TARGET_TABLES:
        assert f"ALTER TABLE {table} ALTER COLUMN work_center_id SET NOT NULL" in sql
        assert f"ALTER TABLE {table} DROP COLUMN asset_id" in sql
    assert "ALTER TABLE downtime_events DROP COLUMN maintenance_work_order_id" in sql
    assert "ALTER TABLE downtime_events DROP COLUMN maintenance_adopted" in sql
    assert "DROP TABLE maintenance_assets" in sql
