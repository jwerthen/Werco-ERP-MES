"""Coverage for 122_car_form_quote_rev (the CAR form columns + ``quote_lines.part_revision``).

122 is purely additive over two EXISTING tenant tables -- nullable columns, four named FKs,
one index and one CHECK on ``cars``; one nullable column on ``quote_lines``; no data. This
file covers the MIGRATION (the behavior is in ``tests/api/test_car_form_and_print.py`` and
``tests/api/test_quote_line_revision_gate.py``):

1. **Script wiring** -- the revision id and the down_revision the build assigned, an id that
   fits ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- every column (name, type compiled for PostgreSQL,
   nullability, no default), every FK name and target, the index and the CHECK text the
   migration builds are exactly what the models declare (the stamped-over hazard), and
   none of the server-stamped / snapshot columns is accepted by a request schema.
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database
   holding legacy rows: the rows survive both directions untouched, every new column reads
   NULL (no backfill), the re-run is a no-op, the CHECK refuses an unknown reason, and the
   downgrade removes exactly what the upgrade added.
4. **The create_all bootstrap is a no-op** -- over a schema the models already built, the
   upgrade emits nothing.
5. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions,
   with the lock_timeout taken LOCAL and handed back, and no RLS / role statement.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quality import CAR_INITIATION_REASON_CHECK, CorrectiveActionRequest
from app.models.quote import QuoteLine
from app.schemas.quality import CARCreate, CARUpdate

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(BACKEND_DIR, "alembic", "versions", "122_car_form_quote_rev.py")

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("_migtest_122", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


# ---------------------------------------------------------------------------- 1. wiring


def test_revision_ids():
    module = _load()
    assert module.revision == "122_car_form_quote_rev"
    assert module.down_revision == "121_job_cost_subcontract"
    assert len(module.revision) <= 32
    assert os.path.basename(MIGRATION_PATH)[:-3] == module.revision


# ------------------------------------------------------------------- 2. model lock-step


def test_every_car_column_matches_the_model():
    module = _load()
    table = CorrectiveActionRequest.__table__
    for column in module._car_columns():
        model_column = table.c[column.name]
        assert _pg(model_column.type) == _pg(column.type), column.name
        assert model_column.nullable and column.nullable, column.name
        assert model_column.default is None and model_column.server_default is None, column.name
    assert set(module.CAR_COLUMN_NAMES) <= set(table.c.keys())


def test_car_foreign_keys_index_and_check_match_the_model():
    module = _load()
    table = CorrectiveActionRequest.__table__
    model_fks = {
        fk.constraint.name: (fk.column.table.name, fk.parent.name) for column in table.c for fk in column.foreign_keys
    }
    for name, referred_table, local_column in module.CAR_FOREIGN_KEYS:
        assert model_fks[name] == (referred_table, local_column)
    assert module.PART_INDEX in {index.name for index in table.indexes}
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name
    }
    assert checks[module.REASON_CHECK] == module.REASON_CHECK_SQL == CAR_INITIATION_REASON_CHECK


def test_quote_line_revision_matches_the_model():
    module = _load()
    column = QuoteLine.__table__.c[module.QUOTE_LINE_REVISION]
    assert _pg(column.type) == "VARCHAR(20)" and column.nullable and column.server_default is None


def test_server_stamped_and_snapshot_columns_are_on_no_request_schema():
    for schema in (CARCreate, CARUpdate):
        fields = set(schema.model_fields)
        assert not fields & {"quality_approved_by", "quality_approved_at", "customer_name"}, schema.__name__
        assert {"initiation_reason", "part_id", "customer_id", "vendor_id", "cost_labor"} <= fields


# ------------------------------------------------------------- 3. real SQLite round-trip

LEGACY_DDL = [
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE parts (id INTEGER PRIMARY KEY)",
    "CREATE TABLE customers (id INTEGER PRIMARY KEY)",
    "CREATE TABLE vendors (id INTEGER PRIMARY KEY)",
    "CREATE TABLE ncrs (id INTEGER PRIMARY KEY, car_id INTEGER REFERENCES cars (id))",
    (
        "CREATE TABLE cars (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, car_number VARCHAR(50) NOT NULL, "
        "title VARCHAR(255) NOT NULL, problem_description TEXT NOT NULL, due_date DATE)"
    ),
    "CREATE INDEX ix_cars_due_date ON cars (due_date)",
    (
        "CREATE TABLE quote_lines (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, quote_id INTEGER, "
        "line_number INTEGER NOT NULL, description TEXT NOT NULL, quantity FLOAT NOT NULL)"
    ),
    "INSERT INTO cars VALUES (7, 1, 'CAR-LEGACY-001', 'Legacy CAR', 'Recorded before 122.', '2026-01-02')",
    "INSERT INTO ncrs VALUES (1, 7)",
    "INSERT INTO quote_lines VALUES (3, 1, 9, 1, 'Legacy line', 20)",
]


def _columns(conn, table):
    return {column["name"] for column in sa.inspect(conn).get_columns(table)}


def test_upgrade_rerun_downgrade_round_trip_preserves_legacy_rows(tmp_path):
    module = _load()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm122.db'}")
    with engine.begin() as conn:
        for statement in LEGACY_DDL:
            conn.exec_driver_sql(statement)
        legacy_cars = set(_columns(conn, "cars"))
        legacy_lines = set(_columns(conn, "quote_lines"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            inspector = sa.inspect(conn)
            assert set(module.CAR_COLUMN_NAMES) <= _columns(conn, "cars")
            assert module.QUOTE_LINE_REVISION in _columns(conn, "quote_lines")
            assert {fk["name"] for fk in inspector.get_foreign_keys("cars")} >= {
                name for name, _, _ in module.CAR_FOREIGN_KEYS
            }
            assert module.PART_INDEX in {ix["name"] for ix in inspector.get_indexes("cars")}
            assert "ix_cars_due_date" in {ix["name"] for ix in inspector.get_indexes("cars")}
            checks = {ck["name"]: ck["sqltext"] for ck in inspector.get_check_constraints("cars")}
            assert re.sub(r"\s+", " ", checks[module.REASON_CHECK]) == module.REASON_CHECK_SQL

            # Legacy rows survive and read NULL for everything new (no backfill).
            row = conn.exec_driver_sql(
                "SELECT car_number, title, due_date, initiation_reason, submitted_date, part_id, cost_labor, "
                "quality_approved_by FROM cars WHERE id = 7"
            ).one()
            assert tuple(row) == ("CAR-LEGACY-001", "Legacy CAR", "2026-01-02", None, None, None, None, None)
            assert conn.exec_driver_sql("SELECT car_id FROM ncrs WHERE id = 1").scalar_one() == 7
            assert conn.exec_driver_sql("SELECT description, part_revision FROM quote_lines").one() == (
                "Legacy line",
                None,
            )

            # The CHECK holds the reason to the eight form boxes.
            conn.exec_driver_sql("UPDATE cars SET initiation_reason = 'internal_audit' WHERE id = 7")
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.exec_driver_sql("UPDATE cars SET initiation_reason = 'hunch' WHERE id = 7")

            module.upgrade()  # a re-run is a guarded no-op
            module.downgrade()
            assert _columns(conn, "cars") == legacy_cars
            assert _columns(conn, "quote_lines") == legacy_lines
            assert "ix_cars_due_date" in {ix["name"] for ix in sa.inspect(conn).get_indexes("cars")}
            assert conn.exec_driver_sql("SELECT car_number, title FROM cars WHERE id = 7").one() == (
                "CAR-LEGACY-001",
                "Legacy CAR",
            )
            assert conn.exec_driver_sql("SELECT description FROM quote_lines").scalar_one() == "Legacy line"
            module.downgrade()  # and so is a second downgrade
    engine.dispose()


# -------------------------------------------------------- 4. create_all bootstrap no-op


class _Recorder:
    def __init__(self, bind):
        self._bind = bind
        self.calls = []

    def get_bind(self):
        return self._bind

    def get_context(self):
        return MigrationContext.configure(self._bind)

    def __getattr__(self, name):  # any DDL call is recorded instead of run
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


def test_upgrade_over_a_create_all_schema_emits_nothing(tmp_path):
    from app.db.database import Base

    module = _load()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    tables = [
        Base.metadata.tables[name]
        for name in ("companies", "users", "parts", "customers", "vendors", "work_orders", "cars", "quote_lines")
        if name in Base.metadata.tables
    ]
    with engine.begin() as conn:
        # Only what 122 touches and what it references; FKs to other tables are irrelevant here.
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        Base.metadata.create_all(conn, tables=tables)
        recorder = _Recorder(conn)
        module.op = recorder
        module.upgrade()
    engine.dispose()
    assert recorder.calls == []


# ------------------------------------------------------------- 5. offline PostgreSQL SQL


def _render(function_name: str):
    module = _load()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    assert statements[0] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-1] == "SET LOCAL lock_timeout = DEFAULT"
    sql = "\n".join(statements)
    assert "ALTER TABLE cars ADD COLUMN initiation_reason VARCHAR(40)" in sql
    assert "ALTER TABLE cars ADD COLUMN cost_labor NUMERIC(12, 2)" in sql
    assert "ALTER TABLE cars ADD COLUMN quality_approved_at TIMESTAMP WITHOUT TIME ZONE" in sql
    for name, referred, column in _load().CAR_FOREIGN_KEYS:
        assert f"ALTER TABLE cars ADD CONSTRAINT {name} FOREIGN KEY({column}) REFERENCES {referred} (id)" in sql
    assert "ALTER TABLE cars ADD CONSTRAINT ck_cars_initiation_reason CHECK (initiation_reason IN (" in sql
    assert "CREATE INDEX ix_cars_part_id ON cars (part_id)" in sql
    assert "ALTER TABLE quote_lines ADD COLUMN part_revision VARCHAR(20)" in sql
    assert "NOT NULL" not in sql and "DEFAULT" not in sql.replace("lock_timeout = DEFAULT", "")
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "122 is pure DDL: no backfill"
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|\banon\b|authenticated|service_role", sql)


def test_offline_downgrade_drops_exactly_what_upgrade_added():
    sql = "\n".join(_render("downgrade"))
    assert "ALTER TABLE quote_lines DROP COLUMN part_revision" in sql
    assert "DROP INDEX ix_cars_part_id" in sql
    assert "ALTER TABLE cars DROP CONSTRAINT ck_cars_initiation_reason" in sql
    for name, _, _ in _load().CAR_FOREIGN_KEYS:
        assert f"ALTER TABLE cars DROP CONSTRAINT {name}" in sql
    for column in _load().CAR_COLUMN_NAMES:
        assert f"ALTER TABLE cars DROP COLUMN {column}" in sql
    assert "DROP TABLE" not in sql
