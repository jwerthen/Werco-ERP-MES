"""Coverage for 132_maintenance_pm_integrity: ``maintenance_work_orders.schedule_checklist_snapshot``
(Text NULL, never backfilled) and the partial unique index
``uq_maintenance_work_orders_open_schedule`` -- one OPEN work order per PM schedule, per company.

The behavior the two objects carry is tested through the API in
``tests/api/test_maintenance_pm_integrity.py``. This file covers the MIGRATION:

1. **Script wiring** -- single head, the down-revision, an id that is the file stem and fits
   ``alembic_version``.
2. **Model / migration lock-step** -- the predicate is byte-identical in both, names exactly the
   member NAMES of ``OPEN_MAINTENANCE_STATUSES`` (``SQLEnum`` persists names, not values), and the
   model's index declares it for BOTH dialects (a ``postgresql_where`` alone would make SQLite build
   a FULL unique index: one work order per schedule, ever); the column is what the model declares.
3. **The duplicate probe and the refusal** -- ``open_work_order_duplicates`` finds exactly the rows
   the index would refuse; the upgrade over such data RAISES naming every offending work order and
   changes nothing.
4. **A real upgrade -> re-run -> downgrade round-trip** over a scratch SQLite database built from the
   model's own pre-132 DDL, with rows and children: idempotent guards, a partial bootstrap recovers
   each object, nothing is backfilled, the index is real, and the downgrade removes exactly what the
   upgrade added.
5. **The create_all bootstrap is a no-op.**
6. **The SQL PostgreSQL receives** (offline ``--sql``): lock_timeout taken LOCAL and handed back,
   a metadata-only ``ADD COLUMN``, the partial unique index; no backfill and no RLS / role DDL.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  -- registers every table the model references
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.api.endpoints import maintenance as maintenance_endpoints
from app.models.maintenance import (
    OPEN_MAINTENANCE_STATUSES,
    OPEN_SCHEDULE_WORK_ORDER_INDEX,
    OPEN_SCHEDULE_WORK_ORDER_PREDICATE,
    MaintenanceStatus,
    MaintenanceWorkOrder,
)

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "132_maintenance_pm_integrity.py")
REVISION = "132_maintenance_pm_integrity"
DOWN_REVISION = "131_document_access"
TABLE = "maintenance_work_orders"
SNAPSHOT = "schedule_checklist_snapshot"
INDEX = "uq_maintenance_work_orders_open_schedule"
# The PR contract, copied here independently of both the migration and the model.
CONTRACT_PREDICATE = "schedule_id IS NOT NULL AND status IN ('SCHEDULED', 'OVERDUE', 'IN_PROGRESS', 'ON_HOLD')"


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_132", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _ws(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _model_index() -> sa.Index:
    [index] = [ix for ix in MaintenanceWorkOrder.__table__.indexes if ix.name == INDEX]
    return index


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


def test_the_predicate_and_names_are_byte_identical_in_migration_model_and_contract():
    module = _module()
    assert module.OPEN_SCHEDULE_PREDICATE == OPEN_SCHEDULE_WORK_ORDER_PREDICATE == CONTRACT_PREDICATE
    assert module.OPEN_SCHEDULE_INDEX == OPEN_SCHEDULE_WORK_ORDER_INDEX == INDEX
    assert module.TABLE == MaintenanceWorkOrder.__tablename__ == TABLE
    assert module.SNAPSHOT_COLUMN == SNAPSHOT


def test_the_predicate_names_exactly_the_open_statuses_by_member_name():
    match = re.fullmatch(r"schedule_id IS NOT NULL AND status IN \((.*)\)", OPEN_SCHEDULE_WORK_ORDER_PREDICATE)
    assert match, OPEN_SCHEDULE_WORK_ORDER_PREDICATE
    listed = re.findall(r"'([^']*)'", match.group(1))
    assert len(listed) == len(set(listed))
    assert set(listed) == {status.name for status in OPEN_MAINTENANCE_STATUSES}
    # NAMES, which is what SQLEnum(MaintenanceStatus) persists -- the values would never match a row.
    assert MaintenanceWorkOrder.__table__.c.status.type.enums == [status.name for status in MaintenanceStatus]
    assert not set(listed) & {status.value for status in MaintenanceStatus}


def test_open_and_terminal_partition_every_status():
    """A status added to the enum must be classified: open (covered by the index and the endpoints'
    one-open-work-order rule) or terminal."""
    assert maintenance_endpoints._OPEN_STATUSES is OPEN_MAINTENANCE_STATUSES
    open_, terminal = set(OPEN_MAINTENANCE_STATUSES), set(maintenance_endpoints._TERMINAL_STATUSES)
    assert terminal == {MaintenanceStatus.COMPLETED, MaintenanceStatus.CANCELLED}
    assert not open_ & terminal and open_ | terminal == set(MaintenanceStatus)


def test_the_model_index_declares_the_predicate_for_both_dialects():
    index = _model_index()
    assert index.unique is True
    assert [column.name for column in index.columns] == ["company_id", "schedule_id"]
    for dialect in ("postgresql", "sqlite"):
        where = index.dialect_options[dialect]["where"]
        assert where is not None, dialect
        assert str(where) == CONTRACT_PREDICATE, dialect


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgresql", "sqlite"])
def test_the_model_index_compiles_partial_on_each_dialect(dialect):
    assert _ws(CreateIndex(_model_index()).compile(dialect=dialect)) == (
        f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} (company_id, schedule_id) WHERE {CONTRACT_PREDICATE}"
    )


def test_the_snapshot_column_matches_the_model():
    column = MaintenanceWorkOrder.__table__.c[SNAPSHOT]
    assert isinstance(column.type, sa.Text)
    assert column.nullable is True
    assert column.server_default is None and column.default is None


# ---------------------------------------------------------------------------- scratch database

CHILDREN_DDL = (
    # The tables maintenance_work_orders references (a batch rebuild reflects every FK target).
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE work_centers (id INTEGER PRIMARY KEY)",
    "CREATE TABLE maintenance_assets (id INTEGER PRIMARY KEY)",
    "CREATE TABLE maintenance_schedules (id INTEGER PRIMARY KEY)",
    # ...and the two that reference it, whose rows must survive the rebuild.
    "CREATE TABLE maintenance_logs (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
    "maintenance_wo_id INTEGER REFERENCES maintenance_work_orders(id), event_type VARCHAR(50) NOT NULL, "
    "description TEXT NOT NULL)",
    "CREATE TABLE downtime_events (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
    "maintenance_work_order_id INTEGER REFERENCES maintenance_work_orders(id))",
)

INSERT_WO = (
    "INSERT INTO maintenance_work_orders (id, company_id, schedule_id, work_center_id, wo_number, title, status, "
    "notes) VALUES ({id}, {company}, {schedule}, 1, '{number}', 'Synthetic PM', '{status}', 'kept')"
)
# A legal pre-132 history: one open work order per (company, schedule) at most.
CLEAN_ROWS = (
    (1, 1, 1, "MWO-2026-0001", "COMPLETED"),
    (2, 1, 1, "MWO-2026-0002", "CANCELLED"),
    (3, 1, 1, "MWO-2026-0003", "IN_PROGRESS"),
    (4, 1, 2, "MWO-2026-0004", "ON_HOLD"),
    (5, 2, 1, "MWO-2026-0005", "SCHEDULED"),  # company 2 naming company 1's schedule id: its own key
    (6, 1, "NULL", "MWO-2026-0006", "SCHEDULED"),  # corrective work orders carry no schedule
    (7, 1, "NULL", "MWO-2026-0007", "IN_PROGRESS"),
)


def _pre_132_ddl():
    """The model's own DDL, less exactly what 132 adds -- so the scratch table tracks the model."""
    table = MaintenanceWorkOrder.__table__
    create = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    lines = [line for line in create.splitlines() if SNAPSHOT not in line]
    assert len(lines) == len(create.splitlines()) - 1
    statements = ["\n".join(lines)]
    for index in sorted(table.indexes, key=lambda ix: ix.name):
        if index.name != INDEX:
            statements.append(str(CreateIndex(index).compile(dialect=sqlite.dialect())))
    return statements + list(CHILDREN_DDL)


def _insert(connection, rows):
    for wo_id, company, schedule, number, status_ in rows:
        connection.exec_driver_sql(
            INSERT_WO.format(id=wo_id, company=company, schedule=schedule, number=number, status=status_)
        )


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pm-integrity-migration.db'}")
    with engine.begin() as connection:
        for statement in _pre_132_ddl():
            connection.exec_driver_sql(statement)
        _insert(connection, CLEAN_ROWS)
        connection.exec_driver_sql(
            "INSERT INTO maintenance_logs VALUES (1, 1, 1, 'completed', 'done'), (2, 1, 3, 'started', 'go')"
        )
        connection.exec_driver_sql("INSERT INTO downtime_events VALUES (1, 1, 3)")
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
    return [
        tuple(row)
        for row in connection.exec_driver_sql(
            f"SELECT id, company_id, schedule_id, wo_number, status, notes FROM {TABLE} ORDER BY id"
        )
    ]


def _children(connection):
    return (
        connection.exec_driver_sql("SELECT * FROM maintenance_logs ORDER BY id").fetchall(),
        connection.exec_driver_sql("SELECT * FROM downtime_events ORDER BY id").fetchall(),
    )


def _no_temp_tables(connection):
    return not connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").fetchall()


# ---------------------------------------------------------------------------- 3. duplicates


def test_the_duplicate_probe_finds_exactly_what_the_index_would_refuse(scratch):
    connection, migration = scratch
    assert migration.open_work_order_duplicates(connection) == {}  # the clean history holds none

    _insert(
        connection,
        (
            (10, 1, 1, "MWO-2026-0010", "SCHEDULED"),  # a second open one on company 1 / schedule 1
            (11, 1, 5, "MWO-2026-0011", "OVERDUE"),
            (12, 1, 5, "MWO-2026-0012", "ON_HOLD"),
            (13, 1, 5, "MWO-2026-0013", "IN_PROGRESS"),
            (14, 1, 6, "MWO-2026-0014", "SCHEDULED"),  # alone on its schedule
            (15, 1, 6, "MWO-2026-0015", "COMPLETED"),  # terminal: never counts
            (16, 1, 6, "MWO-2026-0016", "CANCELLED"),
            (17, 1, "NULL", "MWO-2026-0017", "SCHEDULED"),  # no schedule: never counts
            (18, 2, 2, "MWO-2026-0018", "IN_PROGRESS"),  # company 2's own key
        ),
    )

    duplicates = migration.open_work_order_duplicates(connection)

    assert list(duplicates.items()) == [
        ((1, 1), ["MWO-2026-0003", "MWO-2026-0010"]),
        ((1, 5), ["MWO-2026-0011", "MWO-2026-0012", "MWO-2026-0013"]),
    ]


def test_the_upgrade_refuses_over_duplicates_naming_them_and_changes_nothing(scratch):
    connection, migration = scratch
    _insert(connection, ((10, 1, 1, "MWO-2026-0010", "ON_HOLD"), (11, 2, 1, "MWO-2026-0011", "OVERDUE")))
    before, rows, index_sql = _objects(connection), _rows(connection), _stored_index_sql(connection)

    with pytest.raises(RuntimeError) as refused:
        migration.upgrade()

    message = str(refused.value)
    assert "company 1, PM schedule 1: MWO-2026-0003, MWO-2026-0010" in message
    assert "company 2, PM schedule 1: MWO-2026-0005, MWO-2026-0011" in message
    assert "2 PM schedule(s)" in message and "Nothing was changed" in message
    assert _objects(connection) == before and SNAPSHOT not in before["columns"]
    assert _rows(connection) == rows and _stored_index_sql(connection) == index_sql

    # Fixed by hand (in the app, which audits it -- here, the scratch rows), the upgrade runs.
    connection.exec_driver_sql(f"UPDATE {TABLE} SET status = 'CANCELLED' WHERE id IN (10, 11)")
    migration.upgrade()
    assert INDEX in _objects(connection)["indexes"]


def test_a_partial_bootstrap_with_the_column_already_there_still_refuses_duplicates(scratch):
    connection, migration = scratch
    connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {SNAPSHOT} TEXT")
    _insert(connection, ((10, 1, 2, "MWO-2026-0010", "SCHEDULED"),))
    with pytest.raises(RuntimeError, match="MWO-2026-0004, MWO-2026-0010"):
        migration.upgrade()
    assert INDEX not in _objects(connection)["indexes"]


# ---------------------------------------------------------------------------- 4. round trip


def test_upgrade_adds_exactly_the_column_and_the_index_and_backfills_nothing(scratch):
    connection, migration = scratch
    before, rows, index_sql, children = (
        _objects(connection),
        _rows(connection),
        _stored_index_sql(connection),
        _children(connection),
    )
    migration.upgrade()
    after = _objects(connection)

    assert set(after["columns"]) == set(before["columns"]) | {SNAPSHOT}
    assert after["columns"][SNAPSHOT] == ("TEXT", True, None)
    for name, spec in before["columns"].items():
        assert after["columns"][name] == spec, name
    assert set(after["indexes"]) == set(before["indexes"]) | {INDEX}
    assert after["indexes"][INDEX] == (("company_id", "schedule_id"), True)
    for kind in ("fks", "checks", "uniques"):
        assert after[kind] == before[kind], kind
    stored = _stored_index_sql(connection)
    assert (
        stored[INDEX] == f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} (company_id, schedule_id) WHERE {CONTRACT_PREDICATE}"
    )
    for name, sql in index_sql.items():
        assert stored[name] == sql, name
    # Never backfilled: every existing work order is a legacy one (reads the live schedule).
    assert connection.exec_driver_sql(f"SELECT count(*) FROM {TABLE} WHERE {SNAPSHOT} IS NOT NULL").scalar() == 0
    assert _rows(connection) == rows and _children(connection) == children
    assert _no_temp_tables(connection)


def test_the_upgraded_index_holds_one_open_work_order_per_company_and_schedule(scratch):
    connection, migration = scratch
    migration.upgrade()
    for row in ((20, 1, 1, "MWO-2026-0020", "SCHEDULED"), (21, 1, 2, "MWO-2026-0021", "OVERDUE")):
        nested = connection.begin_nested()
        with pytest.raises(IntegrityError):
            _insert(connection, (row,))
        nested.rollback()
    # Terminal siblings, rows without a schedule and another company's key are all allowed.
    _insert(
        connection,
        (
            (22, 1, 1, "MWO-2026-0022", "COMPLETED"),
            (23, 1, 1, "MWO-2026-0023", "CANCELLED"),
            (24, 1, "NULL", "MWO-2026-0024", "ON_HOLD"),
            (25, 3, 1, "MWO-2026-0025", "IN_PROGRESS"),
            (26, 1, 3, "MWO-2026-0026", "IN_PROGRESS"),
        ),
    )
    # The index covers UPDATE too: a finished row cannot be moved back open beside an open one.
    nested = connection.begin_nested()
    with pytest.raises(IntegrityError):
        connection.exec_driver_sql(f"UPDATE {TABLE} SET status = 'ON_HOLD' WHERE id = 22")
    nested.rollback()


def test_rerun_is_a_no_op_and_a_partial_bootstrap_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    upgraded = _objects(connection)
    migration.upgrade()  # replay
    assert _objects(connection) == upgraded

    connection.exec_driver_sql(f"DROP INDEX {INDEX}")  # the column survives, the index is rebuilt
    migration.upgrade()
    assert _objects(connection) == upgraded
    assert _stored_index_sql(connection)[INDEX].endswith(f"WHERE {CONTRACT_PREDICATE}")


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    before, rows, index_sql, children = (
        _objects(connection),
        _rows(connection),
        _stored_index_sql(connection),
        _children(connection),
    )
    # The comparison is only as strong as what the scratch table carries: the model's CHECK, its named
    # FK and its unique wo_number index, all of which a SQLite batch rebuild could drop.
    assert "ck_maintenance_work_orders_one_target" in before["checks"]
    assert "fk_maintenance_work_orders_asset_id" in {fk[0] for fk in before["fks"]}
    assert before["indexes"]["ix_maintenance_work_orders_wo_number"] == (("wo_number",), True)
    migration.upgrade()
    connection.exec_driver_sql(f"UPDATE {TABLE} SET {SNAPSHOT} = '[\"Oil ways\"]' WHERE id = 3")

    migration.downgrade()

    assert _objects(connection) == before
    # The children still point at live work-order rows (the scratch parents of maintenance_work_orders
    # itself are empty stubs, so only the children are checked).
    for child in ("maintenance_logs", "downtime_events"):
        assert connection.exec_driver_sql(f"PRAGMA foreign_key_check({child})").fetchall() == [], child
    assert _stored_index_sql(connection) == index_sql
    assert _rows(connection) == rows and _children(connection) == children
    assert _no_temp_tables(connection)
    migration.downgrade()  # a second downgrade is a no-op, not an error
    assert _objects(connection) == before
    migration.upgrade()
    assert SNAPSHOT in _objects(connection)["columns"] and INDEX in _objects(connection)["indexes"]


def test_a_missing_table_is_a_no_op_both_ways(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.downgrade()
        assert not sa.inspect(connection).has_table(TABLE)
    engine.dispose()


# ---------------------------------------------------------------------------- 5. bootstrap


def test_upgrade_over_a_create_all_schema_emits_nothing(tmp_path):
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(engine)
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
    # Not even the duplicate probe: both objects already exist.
    assert not any(TABLE in s and "status IN" in s for s in statements)


# ---------------------------------------------------------------------------- 6. PostgreSQL


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
        f"ALTER TABLE {TABLE} ADD COLUMN {SNAPSHOT} TEXT",
        f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} (company_id, schedule_id) WHERE {CONTRACT_PREDICATE}",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_offline_downgrade_drops_the_index_then_the_column():
    assert _render("downgrade") == [
        "SET LOCAL lock_timeout = '5s'",
        f"DROP INDEX {INDEX}",
        f"ALTER TABLE {TABLE} DROP COLUMN {SNAPSHOT}",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_the_model_bootstrap_carries_the_same_objects_on_postgres():
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(_ws(statement.compile(dialect=engine.dialect))),
    )
    MaintenanceWorkOrder.__table__.create(engine, checkfirst=False)
    [create_table] = [s for s in statements if s.startswith(f"CREATE TABLE {TABLE}")]
    assert f"{SNAPSHOT} TEXT," in create_table
    assert f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} (company_id, schedule_id) WHERE {CONTRACT_PREDICATE}" in statements


def test_no_backfill_rls_or_role_ddl_in_either_direction():
    """The table already exists (``059`` enabled RLS on it), and the snapshot column is never
    backfilled: the list a legacy work order was opened with is unknowable."""
    rendered = " ; ".join(_render("upgrade") + _render("downgrade")).upper()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "ROW LEVEL SECURITY", "REVOKE", "GRANT", "POLICY", "PG_ROLES"):
        assert forbidden not in rendered, forbidden
    # ...and none in the module's code either (its docstring may explain why there is none).
    module = _module()
    with open(MIGRATION_FILE, encoding="utf-8") as handle:
        code = handle.read().replace(module.__doc__, "")
    code_lines = [line.split("#", 1)[0] for line in code.splitlines()]
    for forbidden in ("ROW LEVEL SECURITY", "REVOKE", "GRANT", "POLICY", "pg_roles", "anon", "authenticated"):
        assert not any(forbidden in line for line in code_lines), forbidden
