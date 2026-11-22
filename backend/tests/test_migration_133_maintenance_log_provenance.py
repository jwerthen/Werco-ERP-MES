"""Coverage for 133_maintenance_log_provenance: ``maintenance_logs.api_token_id`` (Integer NULL, no
foreign key, never backfilled) and the index ``ix_maintenance_logs_maintenance_wo_id``.

The behavior the column carries (``filed_via_api_token`` on every log read) is tested through the API
in ``tests/api/test_maintenance_log_entries.py``. This file covers the MIGRATION:

1. **Script wiring** -- single head, the down-revision, an id that is the file stem and fits
   ``alembic_version``.
2. **Model / migration lock-step** -- the column and the index are exactly what ``MaintenanceLog``
   declares (the stamped-over hazard: ``create_all`` must build what the migration builds).
3. **A real upgrade -> re-run -> downgrade round-trip** over a scratch SQLite database built from the
   model's own pre-133 DDL, with rows and a child: idempotent guards, a partial bootstrap recovers each
   object, nothing is backfilled, and the downgrade removes exactly what the upgrade added.
4. **The create_all bootstrap is a no-op.**
5. **The SQL PostgreSQL receives** (offline ``--sql``): lock_timeout taken LOCAL and handed back, a
   metadata-only ``ADD COLUMN``, a plain index; no backfill and no RLS / role DDL.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  -- registers every table the model references
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.maintenance import MaintenanceLog

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "133_maintenance_log_provenance.py")
REVISION = "133_maintenance_log_provenance"
DOWN_REVISION = "132_maintenance_pm_integrity"
TABLE = "maintenance_logs"
COLUMN = "api_token_id"
INDEX = "ix_maintenance_logs_maintenance_wo_id"


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_133", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _ws(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


# ---------------------------------------------------------------------------- 1. wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# ---------------------------------------------------------------------------- 2. lock-step


def test_the_names_match_the_model():
    module = _module()
    assert module.TABLE == MaintenanceLog.__tablename__ == TABLE
    assert module.TOKEN_COLUMN == COLUMN
    assert module.WO_INDEX == INDEX


def test_the_column_is_a_nullable_integer_with_no_foreign_key_or_default():
    column = MaintenanceLog.__table__.c[COLUMN]
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True
    assert not column.foreign_keys, "tokens are revoked, never deleted: an identifier, not a foreign key"
    assert column.server_default is None and column.default is None


def test_the_index_matches_the_model():
    [index] = [ix for ix in MaintenanceLog.__table__.indexes if ix.name == INDEX]
    assert [column.name for column in index.columns] == ["maintenance_wo_id"]
    assert index.unique is False
    assert not index.dialect_options["postgresql"]["where"] and not index.dialect_options["sqlite"]["where"]
    # ...and nothing else on maintenance_wo_id: one index, not two.
    on_wo = [ix.name for ix in MaintenanceLog.__table__.indexes if "maintenance_wo_id" in ix.columns]
    assert on_wo == [INDEX]


# ---------------------------------------------------------------------------- scratch database

PARENTS_DDL = (
    # The tables maintenance_logs references (a batch rebuild reflects every FK target).
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE work_centers (id INTEGER PRIMARY KEY)",
    "CREATE TABLE maintenance_assets (id INTEGER PRIMARY KEY)",
    "CREATE TABLE maintenance_work_orders (id INTEGER PRIMARY KEY)",
)
SEED = (
    "INSERT INTO maintenance_logs (id, company_id, work_center_id, maintenance_wo_id, event_type, description, "
    "performed_by, event_date, cost) VALUES "
    "(1, 1, 1, 7, 'started', 'Synthetic start', 3, '2026-11-01 08:00:00', 0), "
    "(2, 1, 1, 7, 'calibration check', 'Legacy free-text entry', NULL, NULL, 12.5), "
    "(3, 2, 2, NULL, 'note', 'Another company', 4, '2026-11-02 09:00:00', 0)"
)
ROWS_SQL = (
    f"SELECT id, company_id, work_center_id, maintenance_wo_id, event_type, description, performed_by, event_date, "
    f"cost FROM {TABLE} ORDER BY id"
)


def _pre_133_ddl():
    """The model's own DDL, less exactly what 133 adds -- so the scratch table tracks the model."""
    table = MaintenanceLog.__table__
    create = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    lines = [line for line in create.splitlines() if f"\t{COLUMN} " not in line]
    assert len(lines) == len(create.splitlines()) - 1
    statements = list(PARENTS_DDL) + ["\n".join(lines)]
    for index in sorted(table.indexes, key=lambda ix: ix.name):
        if index.name != INDEX:
            statements.append(str(CreateIndex(index).compile(dialect=sqlite.dialect())))
    return statements


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'log-provenance-migration.db'}")
    with engine.begin() as connection:
        for statement in _pre_133_ddl():
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(SEED)
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _objects(connection):
    inspector = sa.inspect(connection)
    return {
        "columns": {
            column["name"]: (str(column["type"]), column["nullable"], column["default"])
            for column in inspector.get_columns(TABLE)
        },
        "fks": sorted(
            (fk["name"] or "", tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(TABLE)
        ),
        "checks": {ck["name"]: _ws(ck["sqltext"]) for ck in inspector.get_check_constraints(TABLE)},
        "indexes": {ix["name"]: (tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE)},
        "uniques": {uq["name"]: tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(TABLE)},
    }


def _stored_index_sql(connection):
    rows = connection.exec_driver_sql(
        f"SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND tbl_name = '{TABLE}'"
    )
    return {name: _ws(sql) for name, sql in rows}


def _rows(connection):
    return [tuple(row) for row in connection.exec_driver_sql(ROWS_SQL)]


def _no_temp_tables(connection):
    return not connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").fetchall()


# ---------------------------------------------------------------------------- 3. round trip


def test_upgrade_adds_exactly_the_column_and_the_index_and_backfills_nothing(scratch):
    connection, migration = scratch
    before, rows, index_sql = _objects(connection), _rows(connection), _stored_index_sql(connection)
    assert COLUMN not in before["columns"] and INDEX not in before["indexes"]

    migration.upgrade()

    after = _objects(connection)
    assert set(after["columns"]) == set(before["columns"]) | {COLUMN}
    assert after["columns"][COLUMN] == ("INTEGER", True, None)
    for name, spec in before["columns"].items():
        assert after["columns"][name] == spec, name
    assert set(after["indexes"]) == set(before["indexes"]) | {INDEX}
    assert after["indexes"][INDEX] == (("maintenance_wo_id",), False)
    for kind in ("fks", "checks", "uniques"):
        assert after[kind] == before[kind], kind
    assert not any(fk[1] == (COLUMN,) for fk in after["fks"])
    stored = _stored_index_sql(connection)
    assert stored[INDEX] == f"CREATE INDEX {INDEX} ON {TABLE} (maintenance_wo_id)"
    for name, sql in index_sql.items():
        assert stored[name] == sql, name
    # Never backfilled: no existing entry claims a token filed it.
    assert connection.exec_driver_sql(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NOT NULL").scalar() == 0
    assert _rows(connection) == rows
    assert _no_temp_tables(connection)


def test_rerun_is_a_no_op_and_a_partial_bootstrap_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    upgraded = _objects(connection)
    migration.upgrade()  # replay
    assert _objects(connection) == upgraded

    connection.exec_driver_sql(f"DROP INDEX {INDEX}")  # the column survives, the index is rebuilt
    migration.upgrade()
    assert _objects(connection) == upgraded


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    before, rows, index_sql = _objects(connection), _rows(connection), _stored_index_sql(connection)
    # The comparison is only as strong as what the scratch table carries: the model's CHECK, its named
    # FK and its other indexes, all of which a SQLite batch rebuild could drop.
    assert "ck_maintenance_logs_one_target" in before["checks"]
    assert "fk_maintenance_logs_asset_id" in {fk[0] for fk in before["fks"]}
    assert {"ix_maintenance_logs_company_id", "ix_maintenance_logs_asset_id"} <= set(before["indexes"])
    migration.upgrade()
    connection.exec_driver_sql(f"UPDATE {TABLE} SET {COLUMN} = 42 WHERE id = 1")

    migration.downgrade()

    assert _objects(connection) == before
    assert _stored_index_sql(connection) == index_sql
    assert _rows(connection) == rows
    assert _no_temp_tables(connection)
    migration.downgrade()  # a second downgrade is a no-op, not an error
    assert _objects(connection) == before
    migration.upgrade()
    assert COLUMN in _objects(connection)["columns"] and INDEX in _objects(connection)["indexes"]


def test_a_missing_table_is_a_no_op_both_ways(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.downgrade()
        assert not sa.inspect(connection).has_table(TABLE)
    engine.dispose()


# ---------------------------------------------------------------------------- 4. bootstrap


def test_upgrade_over_a_create_all_schema_emits_nothing(tmp_path):
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert COLUMN in {column["name"] for column in sa.inspect(connection).get_columns(TABLE)}
        assert INDEX in {index["name"] for index in sa.inspect(connection).get_indexes(TABLE)}
    statements = []
    with engine.begin() as connection:
        sa.event.listen(
            connection,
            "before_cursor_execute",
            lambda conn, cursor, statement, *args: statements.append(statement),
        )
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
    engine.dispose()
    written = [s for s in statements if not s.lstrip().upper().startswith(("PRAGMA", "SELECT"))]
    assert written == [], written


# ---------------------------------------------------------------------------- 5. PostgreSQL


def _render(function_name: str):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    assert _render("upgrade") == [
        "SET LOCAL lock_timeout = '5s'",
        # Nullable with no default: metadata-only on PostgreSQL, no table rewrite, no backfill.
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INTEGER",
        f"CREATE INDEX {INDEX} ON {TABLE} (maintenance_wo_id)",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_offline_downgrade_drops_the_index_then_the_column():
    assert _render("downgrade") == [
        "SET LOCAL lock_timeout = '5s'",
        f"DROP INDEX {INDEX}",
        f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_the_model_bootstrap_carries_the_same_objects_on_postgres():
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(_ws(statement.compile(dialect=engine.dialect))),
    )
    MaintenanceLog.__table__.create(engine, checkfirst=False)
    [create_table] = [s for s in statements if s.startswith(f"CREATE TABLE {TABLE}")]
    assert f"{COLUMN} INTEGER," in create_table
    assert f"FOREIGN KEY({COLUMN})" not in create_table
    assert f"CREATE INDEX {INDEX} ON {TABLE} (maintenance_wo_id)" in statements


def test_no_backfill_rls_or_role_ddl_in_either_direction():
    """The table already exists (``059`` enabled RLS on it), and the column is never backfilled: the
    audit rows' ``extra_data.credential`` stays the record for older entries."""
    rendered = " ; ".join(_render("upgrade") + _render("downgrade")).upper()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "ROW LEVEL SECURITY", "REVOKE", "GRANT", "POLICY", "PG_ROLES"):
        assert forbidden not in rendered, forbidden
    module = _module()
    with open(MIGRATION_FILE, encoding="utf-8") as handle:
        code = handle.read().replace(module.__doc__, "")
    code_lines = [line.split("#", 1)[0] for line in code.splitlines()]
    for forbidden in ("ROW LEVEL SECURITY", "REVOKE", "GRANT", "POLICY", "pg_roles", "anon", "authenticated"):
        assert not any(forbidden in line for line in code_lines), forbidden
