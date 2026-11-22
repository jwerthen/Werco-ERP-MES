"""Coverage for 134_part_customer_supplied: ``parts.customer_supplied`` (Boolean NOT NULL, server
default ``'false'``), the flag behind the owner decision (2026-11-22) that a customer-supplied part
creates no MRP demand.

134 is purely additive over an EXISTING table -- one plain column with a constant server default;
no table, no index, no constraint, no data. What MRP does with the flag is tested with the MRP
service and the parts API. This file covers the MIGRATION:

1. **Script wiring** -- single head, the down_revision, an id that is the file stem and fits
   ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- the column's type (compiled for PostgreSQL and SQLite),
   nullability, plain-string server default and Python default are exactly what ``Part`` declares
   and what the PR contract says (the stamped-over hazard); nothing indexes the column.
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database built
   from the model's own pre-134 DDL, with ``PRAGMA foreign_keys = ON``, legacy parts (one
   soft-deleted), ``ON DELETE CASCADE`` and NO ACTION children, and a trigger whose body reads
   ``parts`` the way ``tr_stock_observation_insert`` does: both directions are SQLite's native
   ``ALTER`` (``parts`` keeps its b-tree root page -- never rebuilt), every pre-existing part reads
   ``customer_supplied`` False through the model (the SQLite-only storage normalization), the
   guard makes a re-run a no-op that keeps a flag a person set, a partial schema recovers the
   column, and the downgrade removes exactly what the upgrade added.
4. **The create_all bootstrap** -- over the full model schema the upgrade emits nothing, and a
   downgrade -> upgrade over it (the real stock-piece trigger that reads ``parts`` included)
   returns exactly the ``create_all`` schema.
5. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions: the
   lock_timeout taken LOCAL and handed back, one metadata-only ``ADD COLUMN`` / ``DROP COLUMN``,
   no backfill, no index / constraint and no RLS / role statement; only SQLite gets the storage
   normalization; and the model's own PostgreSQL DDL carries the identical column clause.
"""

import importlib.util
import io
import os
import re
import warnings

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  -- registers every table the model references
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.part import Part

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "134_part_customer_supplied.py")
REVISION = "134_part_customer_supplied"
DOWN_REVISION = "133_maintenance_log_provenance"
TABLE = "parts"
COLUMN = "customer_supplied"

# The PR contract, copied here independently of both the migration and the model:
# (PostgreSQL type, nullable, server default -- a plain string, Python default).
CONTRACT = ("BOOLEAN", False, "false", False)
COLUMN_DDL = "customer_supplied BOOLEAN DEFAULT 'false' NOT NULL"


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_134", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _ws(value) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


# ---------------------------------------------------------------------------- 1. wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert module.branch_labels is None and module.depends_on is None
    assert len(REVISION) <= 32


# ---------------------------------------------------------------------------- 2. lock-step


def test_the_migration_declares_the_contract():
    module = _module()
    assert (module.TABLE_NAME, module.COLUMN_NAME) == (TABLE, COLUMN)
    column = module._column()
    pg_type, nullable, server_default, _python_default = CONTRACT
    assert column.name == COLUMN
    assert _pg(column.type) == pg_type
    assert column.nullable is nullable
    # A plain string, never sa.text / sa.false(): the fresh-bootstrap diff compares the rendered DEFAULT.
    assert isinstance(column.server_default.arg, str) and column.server_default.arg == server_default
    assert not column.index and not column.unique and not column.foreign_keys
    # Native ALTER on SQLite: never a copy-and-rename rebuild of parts.
    assert module.BATCH_RECREATE == "never"


def test_the_column_matches_the_model():
    model_column = Part.__table__.c[COLUMN]
    migrated = _module()._column()
    assert _pg(model_column.type) == _pg(migrated.type) == CONTRACT[0]
    assert model_column.nullable is migrated.nullable is CONTRACT[1]
    assert isinstance(model_column.server_default.arg, str)
    assert model_column.server_default.arg == migrated.server_default.arg == CONTRACT[2]
    assert model_column.default is not None and model_column.default.arg is CONTRACT[3]
    assert not model_column.foreign_keys and not model_column.unique and not model_column.index
    # Spelled exactly as the other Part booleans with a server default, so the column clauses agree.
    for sibling in ("backflush_components", "is_deleted"):
        assert Part.__table__.c[sibling].server_default.arg == migrated.server_default.arg, sibling


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgresql", "sqlite"])
def test_model_and_migration_render_the_identical_column_clause(dialect):
    model_ddl = _ws(CreateTable(Part.__table__).compile(dialect=dialect))
    assert re.search(rf"[(,] {re.escape(COLUMN_DDL)} ?[,)]", model_ddl), model_ddl
    standalone = sa.Table("parts_probe", sa.MetaData(), _module()._column())
    assert _ws(CreateTable(standalone).compile(dialect=dialect)) == f"CREATE TABLE parts_probe ( {COLUMN_DDL} )"


def test_the_model_indexes_and_constrains_nothing_on_the_column():
    table = Part.__table__
    for index in table.indexes:
        assert COLUMN not in {column.name for column in index.columns}, index.name
    for constraint in table.constraints:
        assert COLUMN not in {column.name for column in getattr(constraint, "columns", ())}, constraint.name
        assert COLUMN not in _ws(getattr(constraint, "sqltext", "")), constraint.name


# ------------------------------------------------------------- 3. real SQLite round-trip

# Stub parents of parts' FKs, children of parts, and a trigger whose body reads parts the way the
# real tr_stock_observation_insert (104) does. Synthetic names only: the repository is public.
SUPPORT_DDL = (
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE vendors (id INTEGER PRIMARY KEY)",
    # A rebuild's DROP TABLE under foreign_keys = ON would cascade-delete these rows and abort on the
    # next table's. The native ALTER leaves both alone.
    (
        "CREATE TABLE part_notes (id INTEGER PRIMARY KEY, "
        "part_id INTEGER NOT NULL REFERENCES parts (id) ON DELETE CASCADE, note TEXT)"
    ),
    "CREATE TABLE bom_lines (id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts (id))",
    "CREATE TABLE stock_reads (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, part_id INTEGER NOT NULL)",
    (
        "CREATE TRIGGER tr_stock_read_insert BEFORE INSERT ON stock_reads\n"
        "BEGIN\n"
        " SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM parts AS p\n"
        " WHERE p.id=NEW.part_id AND p.company_id=NEW.company_id AND COALESCE(p.is_deleted,0)=0)\n"
        " THEN RAISE(ABORT, 'Stock read must name a live tenant part.') END;\n"
        "END"
    ),
)

SEED_SQL = (
    "INSERT INTO companies VALUES (1), (2)",
    "INSERT INTO users VALUES (5)",
    "INSERT INTO vendors VALUES (9)",
    (
        "INSERT INTO parts (id, part_number, revision, name, part_type, backflush_components, is_active, "
        "primary_supplier_id, created_by, is_deleted, company_id) VALUES "
        "(1, 'SYN-1001', 'A', 'Synthetic bracket', 'MANUFACTURED', 0, 1, 9, 5, 0, 1), "
        "(2, 'SYN-1002', 'B', 'Synthetic fastener', 'PURCHASED', 1, 1, NULL, NULL, 0, 1), "
        "(3, 'SYN-1001', 'A', 'Other tenant bracket', 'MANUFACTURED', 0, 1, NULL, NULL, 0, 2), "
        "(4, 'SYN-1004', 'A', 'Retired part', 'PURCHASED', 0, 0, NULL, 5, 1, 1)"
    ),
    "INSERT INTO part_notes VALUES (1, 1, 'kept'), (2, 4, 'kept on a deleted part')",
    "INSERT INTO bom_lines VALUES (1, 1), (2, 2)",
    "INSERT INTO stock_reads VALUES (1, 1, 1)",
)

LEGACY_ROWS_SQL = {
    "parts": "SELECT id, part_number, revision, name, part_type, backflush_components, is_active, "
    "primary_supplier_id, created_by, is_deleted, company_id FROM parts ORDER BY id",
    "part_notes": "SELECT * FROM part_notes ORDER BY id",
    "bom_lines": "SELECT * FROM bom_lines ORDER BY id",
    "stock_reads": "SELECT * FROM stock_reads ORDER BY id",
}
LEGACY_ROWS = {
    "parts": [
        (1, "SYN-1001", "A", "Synthetic bracket", "MANUFACTURED", 0, 1, 9, 5, 0, 1),
        (2, "SYN-1002", "B", "Synthetic fastener", "PURCHASED", 1, 1, None, None, 0, 1),
        (3, "SYN-1001", "A", "Other tenant bracket", "MANUFACTURED", 0, 1, None, None, 0, 2),
        (4, "SYN-1004", "A", "Retired part", "PURCHASED", 0, 0, None, 5, 1, 1),
    ],
    "part_notes": [(1, 1, "kept"), (2, 4, "kept on a deleted part")],
    "bom_lines": [(1, 1), (2, 2)],
    "stock_reads": [(1, 1, 1)],
}
PART_IDS = [1, 2, 3, 4]


def _pre_134_ddl():
    """The model's own DDL, less exactly the column 134 adds -- so the scratch table tracks the model."""
    table = Part.__table__
    create = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    lines = [line for line in create.splitlines() if not line.strip().startswith(f"{COLUMN} ")]
    assert len(lines) == len(create.splitlines()) - 1
    statements = ["\n".join(lines)]
    for index in sorted(table.indexes, key=lambda ix: ix.name):
        statements.append(str(CreateIndex(index).compile(dialect=sqlite.dialect())))
    return statements


def _enable_foreign_keys(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys = ON")


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm134.db'}")
    sa.event.listen(engine, "connect", _enable_foreign_keys)
    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        for statement in (*_pre_134_ddl(), *SUPPORT_DDL, *SEED_SQL):
            connection.exec_driver_sql(statement)
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _objects(connection, table=TABLE):
    inspector = sa.inspect(connection)
    return {
        "columns": {
            column["name"]: (str(column["type"]), column["nullable"], column["default"])
            for column in inspector.get_columns(table)
        },
        "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
        "fks": sorted(
            (
                fk["name"] or "",
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                tuple(sorted((fk.get("options") or {}).items())),
            )
            for fk in inspector.get_foreign_keys(table)
        ),
        "checks": {ck["name"]: _ws(ck["sqltext"]) for ck in inspector.get_check_constraints(table)},
        "indexes": {ix["name"]: (tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table)},
        "uniques": {uq["name"]: tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)},
    }


def _root_pages(connection):
    """Each table's b-tree root page: a copy-and-rename rebuild gives the table a new one."""
    return dict(connection.exec_driver_sql("SELECT name, rootpage FROM sqlite_master WHERE type = 'table'").fetchall())


def _stored_schema_sql(connection):
    """Every stored index / trigger / view definition: none may change, in either direction."""
    rows = connection.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND sql IS NOT NULL"
    )
    return {(kind, name): _ws(sql) for kind, name, sql in rows}


def _flags_through_the_model(connection):
    """Each part's ``customer_supplied`` read through the MODEL's Boolean type, as the ORM reads it."""
    table = Part.__table__
    rows = connection.execute(sa.select(table.c.id, table.c.customer_supplied).order_by(table.c.id))
    return [(row[0], row[1]) for row in rows]


def _writes_during(connection, action):
    """Run ``action`` and return every statement it sent that is not a read (PRAGMA / SELECT)."""
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    sa.event.listen(connection, "before_cursor_execute", record)
    try:
        action()
    finally:
        sa.event.remove(connection, "before_cursor_execute", record)
    return [sql for sql in statements if not sql.lstrip().upper().startswith(("PRAGMA", "SELECT"))]


def _assert_legacy_rows_intact(connection):
    for table, sql in LEGACY_ROWS_SQL.items():
        assert [tuple(row) for row in connection.exec_driver_sql(sql)] == LEGACY_ROWS[table], table
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    # 134 never needs 129's legacy_alter_table wrapper, so it never touches the connection's setting.
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0
    assert not connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").fetchall()


def _assert_the_trigger_resolves_parts(connection, read_id):
    connection.exec_driver_sql(f"INSERT INTO stock_reads VALUES ({read_id}, 1, 2)")
    # An unknown part, another tenant's part, a soft-deleted part.
    for company_id, part_id in ((1, 999), (2, 1), (1, 4)):
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError, match="Stock read must name a live tenant part"):
            connection.exec_driver_sql(f"INSERT INTO stock_reads VALUES ({read_id + 1}, {company_id}, {part_id})")
        nested.rollback()
    connection.exec_driver_sql(f"DELETE FROM stock_reads WHERE id = {read_id}")


def test_upgrade_adds_the_column_natively_and_every_part_reads_false(scratch):
    connection, migration = scratch
    before, pages, stored = _objects(connection), _root_pages(connection), _stored_schema_sql(connection)
    migration.upgrade()
    after = _objects(connection)

    assert set(after["columns"]) == set(before["columns"]) | {COLUMN}
    assert after["columns"][COLUMN] == ("BOOLEAN", False, "'false'")
    for name, spec in before["columns"].items():
        assert after["columns"][name] == spec, name
    # Nothing but the column: every key, FK, CHECK, index and unique is exactly what was there.
    for kind in ("pk", "fks", "checks", "indexes", "uniques"):
        assert after[kind] == before[kind], kind
    assert _stored_schema_sql(connection) == stored
    # Never rebuilt: the same b-tree, so no child row was cascade-deleted along the way.
    assert _root_pages(connection) == pages
    _assert_legacy_rows_intact(connection)

    # The server default is the only "backfill": every pre-existing part -- the soft-deleted one
    # included -- reads False, stored as the integer the ORM writes (the SQLite storage fix).
    stored_values = connection.exec_driver_sql(f"SELECT {COLUMN} FROM {TABLE} ORDER BY id").fetchall()
    assert [row[0] for row in stored_values] == [0, 0, 0, 0]
    assert _flags_through_the_model(connection) == [(part_id, False) for part_id in PART_IDS]


def test_without_the_normalization_sqlite_would_read_every_part_as_customer_supplied(scratch):
    """The reason the SQLite-only UPDATE exists: SQLite's own ADD COLUMN stores TEXT 'false' on a
    pre-existing row, and the model's Boolean reads that as True -- MRP would plan nothing on a
    local dev database. Proven on the raw ALTER the migration wraps."""
    connection, migration = scratch
    connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN_DDL}")
    assert _flags_through_the_model(connection) == [(part_id, True) for part_id in PART_IDS]
    connection.exec_driver_sql(migration.SQLITE_NORMALIZE_SQL)
    assert _flags_through_the_model(connection) == [(part_id, False) for part_id in PART_IDS]


def test_inserts_after_the_upgrade_get_false_and_the_not_null_is_real(scratch):
    connection, migration = scratch
    migration.upgrade()
    # The app's path (ORM / Core through the model's table) writes the Python default, False.
    connection.execute(
        Part.__table__.insert().values(
            id=5, part_number="SYN-1005", name="New part", part_type="PURCHASED", company_id=1
        )
    )
    # A pre-134 insert that never names the column lands on the server default (SQLite keeps the
    # TEXT spelling for a raw INSERT, as it does for every 'false' default; PostgreSQL stores false).
    connection.exec_driver_sql(
        f"INSERT INTO {TABLE} (id, part_number, name, part_type, company_id) VALUES (6, 'SYN-1006', 'Raw', 'X', 1)"
    )
    stored = connection.exec_driver_sql(f"SELECT {COLUMN} FROM {TABLE} WHERE id IN (5, 6) ORDER BY id").fetchall()
    assert [row[0] for row in stored] == [0, "false"]
    nested = connection.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        connection.exec_driver_sql(f"UPDATE {TABLE} SET {COLUMN} = NULL WHERE id = 1")
    nested.rollback()


def test_the_guard_makes_a_rerun_a_no_op_that_keeps_a_flag_a_person_set(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql(f"UPDATE {TABLE} SET {COLUMN} = 1 WHERE id = 2")
    upgraded, stored = _objects(connection), _stored_schema_sql(connection)

    assert _writes_during(connection, migration.upgrade) == []  # the column exists: guarded no-op

    assert _objects(connection) == upgraded and _stored_schema_sql(connection) == stored
    # The normalization only runs when the column is added: the flag survives the re-run.
    assert _flags_through_the_model(connection) == [(1, False), (2, True), (3, False), (4, False)]
    _assert_legacy_rows_intact(connection)


def test_a_partial_schema_recovers_the_column(scratch):
    connection, migration = scratch
    migration.upgrade()
    upgraded = _objects(connection)
    connection.exec_driver_sql(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
    assert COLUMN not in _objects(connection)["columns"]
    migration.upgrade()
    assert _objects(connection) == upgraded
    assert _flags_through_the_model(connection) == [(part_id, False) for part_id in PART_IDS]


def test_downgrade_removes_exactly_what_upgrade_added(scratch):
    connection, migration = scratch
    before, pages, stored = _objects(connection), _root_pages(connection), _stored_schema_sql(connection)
    migration.upgrade()
    connection.exec_driver_sql(f"UPDATE {TABLE} SET {COLUMN} = 1 WHERE id IN (2, 3)")

    migration.downgrade()

    assert COLUMN not in _objects(connection)["columns"]
    assert _objects(connection) == before
    assert _stored_schema_sql(connection) == stored
    assert _root_pages(connection) == pages  # the native DROP COLUMN rewrote no table either
    _assert_legacy_rows_intact(connection)
    # A second downgrade is a guarded no-op, not an error.
    assert _writes_during(connection, migration.downgrade) == []
    assert _objects(connection) == before


def test_downgrade_is_lossy_and_the_chain_replays_forward(scratch):
    connection, migration = scratch
    before = _objects(connection)
    migration.upgrade()
    connection.exec_driver_sql(f"UPDATE {TABLE} SET {COLUMN} = 1 WHERE id IN (2, 3)")
    migration.downgrade()
    assert _objects(connection) == before

    migration.upgrade()
    # LOSSY, as the docstring warns: the flags a person set are gone and every part reads False
    # again. A code rollback stamps the down_revision instead.
    assert _flags_through_the_model(connection) == [(part_id, False) for part_id in PART_IDS]
    _assert_legacy_rows_intact(connection)


def test_the_trigger_that_reads_parts_keeps_resolving(scratch):
    connection, migration = scratch
    migration.upgrade()
    _assert_the_trigger_resolves_parts(connection, 10)
    migration.downgrade()
    _assert_the_trigger_resolves_parts(connection, 20)
    _assert_legacy_rows_intact(connection)


def test_a_missing_parts_table_is_a_no_op_both_ways(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.downgrade()
        assert not sa.inspect(connection).has_table(TABLE)
    engine.dispose()


# -------------------------------------------------------- 4. create_all bootstrap


def _full_schema(connection):
    """Every table's reflected objects plus every stored index / trigger / view definition. The one
    expression index SQLAlchemy cannot reflect on SQLite is still compared through its stored DDL."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Skipped unsupported reflection of expression-based index", category=sa.exc.SAWarning
        )
        tables = {table: _objects(connection, table) for table in sa.inspect(connection).get_table_names()}
    return {"tables": tables, "stored": _stored_schema_sql(connection)}


@pytest.fixture
def bootstrapped(tmp_path):
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    sa.event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def test_upgrade_over_a_create_all_schema_emits_nothing(bootstrapped):
    connection, migration = bootstrapped
    assert _writes_during(connection, migration.upgrade) == []


def test_downgrade_then_upgrade_over_create_all_returns_the_create_all_schema(bootstrapped):
    """The column ``create_all`` builds from the model and the one this revision adds are the same
    object (type, nullability, DEFAULT text), and both directions leave every other object in the
    full model schema alone -- with the real stock-piece trigger that reads ``parts`` in place."""
    connection, migration = bootstrapped
    yardstick = _full_schema(connection)
    triggers_reading_parts = [
        name for (kind, name), sql in yardstick["stored"].items() if kind == "trigger" and re.search(r"\bparts\b", sql)
    ]
    assert "tr_stock_observation_insert" in triggers_reading_parts  # the test is not vacuous

    migration.downgrade()
    downgraded = _full_schema(connection)
    assert COLUMN not in downgraded["tables"][TABLE]["columns"]
    expected = {**yardstick["tables"][TABLE]}
    expected["columns"] = {name: spec for name, spec in expected["columns"].items() if name != COLUMN}
    assert downgraded["tables"][TABLE] == expected
    for table, objects in yardstick["tables"].items():
        if table != TABLE:
            assert downgraded["tables"][table] == objects, table
    assert downgraded["stored"] == yardstick["stored"]

    migration.upgrade()
    assert _full_schema(connection) == yardstick
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


# ------------------------------------------------------------- 5. offline PostgreSQL SQL


def _render(function_name: str, dialect_name: str = "postgresql"):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect_name, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


def _assert_pure_column_ddl(sql: str):
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "134 is pure DDL on PostgreSQL: no backfill"
    assert not re.search(r"\b(INDEX|CONSTRAINT|FOREIGN KEY|CHECK|UNIQUE|TRIGGER)\b", sql)
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|POLICY|pg_roles|\banon\b|authenticated|service_role", sql)
    assert "audit_log" not in sql and "CREATE TABLE" not in sql and "DROP TABLE" not in sql
    # The soft-delete columns are never touched.
    assert not re.search(r"\b(is_deleted|deleted_at|deleted_by)\b", sql)


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        # NOT NULL with a constant default: metadata-only on PostgreSQL 11+, no rewrite, no scan.
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN_DDL}",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    _assert_pure_column_ddl("\n".join(statements))


def test_offline_downgrade_drops_exactly_the_column():
    statements = _render("downgrade")
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    _assert_pure_column_ddl("\n".join(statements))


def test_only_sqlite_gets_the_storage_normalization_right_after_the_column():
    statements = _render("upgrade", dialect_name="sqlite")
    normalize = _module().SQLITE_NORMALIZE_SQL
    assert normalize == "UPDATE parts SET customer_supplied = 0 WHERE customer_supplied = 'false'"
    # SQLite's native ALTER (never a rebuild), then the storage fix; no PostgreSQL lock_timeout.
    assert statements == [f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN_DDL}", normalize]
    assert _render("downgrade", dialect_name="sqlite") == [f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"]


def test_create_all_ddl_carries_the_same_column_on_postgres():
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(_ws(statement.compile(dialect=engine.dialect))),
    )
    Part.__table__.create(engine, checkfirst=False)
    [create_table] = [s for s in statements if s.startswith(f"CREATE TABLE {TABLE} ")]
    # The model renders the identical clause the migration's ADD COLUMN carries.
    assert re.search(rf"[(,] {re.escape(COLUMN_DDL)} ?[,)]", create_table), create_table
    assert not any(COLUMN in s for s in statements if not s.startswith(f"CREATE TABLE {TABLE} "))


def test_no_rls_or_role_ddl_in_the_module_code():
    """``parts`` already exists (``059`` enabled RLS on it): the module's code names no RLS, REVOKE or
    role statement (its docstring may explain why there is none)."""
    module = _module()
    with open(MIGRATION_FILE, encoding="utf-8") as handle:
        code = handle.read().replace(module.__doc__, "")
    code_lines = [line.split("#", 1)[0] for line in code.splitlines()]
    for forbidden in ("ROW LEVEL SECURITY", "REVOKE", "GRANT", "POLICY", "pg_roles", "anon", "authenticated"):
        assert not any(forbidden in line for line in code_lines), forbidden
