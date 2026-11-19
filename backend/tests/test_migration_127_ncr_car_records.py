"""Coverage for 127_ncr_car_records (NCR action / concession / re-verification / disposition stamp
columns on ``ncrs``; effectiveness verification and 10.2.1 review columns on ``cars``).

127 is purely additive over two EXISTING tenant tables -- nullable columns, three named FKs to
``users`` and two CHECKs; no index, no table, no data. This file covers the MIGRATION (the NCR /
CAR behavior is tested with the service and the API):

1. **Script wiring** -- single head, the down_revision, an id that is the file stem and fits
   ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- every column (name, type compiled for PostgreSQL,
   nullability, no default), every FK name and target, and both CHECK texts (byte-identical to the
   model constants and to the enums' values) are exactly what the models declare (the
   stamped-over hazard); and none of the server-stamped columns is accepted by a request schema.
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database holding
   legacy rows (a soft-deleted NCR among them, and a child table referencing both): the rows
   survive both directions untouched, every new column reads NULL (no backfill -- not even the
   disposition stamp of an NCR that already carries a disposition), the soft-delete columns are
   carried across, both CHECKs are enforced, a partially built schema recovers each missing
   object independently, and the downgrade removes exactly what the upgrade added.
4. **The create_all bootstrap is a no-op** -- over a schema the models already built, the upgrade
   emits nothing.
5. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions, with the
   lock_timeout taken LOCAL and handed back, ``ncrs`` before ``cars`` up and the reverse down, no
   backfill and no RLS / role statement; and the model's own PostgreSQL DDL carries the same FKs and
   CHECKs.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  -- registers every table the two models reference
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.quality import (
    CAR_EFFECTIVENESS_RESULT_CHECK,
    NCR_REVERIFICATION_RESULT_CHECK,
    CAREffectivenessResult,
    CorrectiveActionRequest,
    NCRReverificationResult,
    NonConformanceReport,
)
from app.schemas.quality import CARCreate, CARUpdate, NCRCreate, NCRUpdate

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "127_ncr_car_records.py")
REVISION = "127_ncr_car_records"
DOWN_REVISION = "126_qms_company_id"

pytestmark = pytest.mark.unit

# Server-stamped / verb-only columns: written by the disposition stamp, the re-verification verb or
# the effectiveness verb, never by a request body.
NCR_VERB_ONLY = {
    "reverification_result",
    "reverification_notes",
    "reverified_by",
    "reverified_at",
    "dispositioned_by",
    "dispositioned_at",
}
CAR_VERB_ONLY = {"effectiveness_result", "effectiveness_verified_by", "effectiveness_verified_at"}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_127", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


def _model_checks(table: sa.Table) -> dict:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name
    }


def _model_fks(table: sa.Table) -> dict:
    return {
        fk.constraint.name: (fk.column.table.name, fk.parent.name)
        for column in table.c
        for fk in column.foreign_keys
        if fk.constraint.name
    }


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


# ------------------------------------------------------------------- 2. model lock-step


@pytest.mark.parametrize(
    "model, factory_name, names_attr",
    [
        (NonConformanceReport, "_ncr_columns", "NCR_COLUMN_NAMES"),
        (CorrectiveActionRequest, "_car_columns", "CAR_COLUMN_NAMES"),
    ],
)
def test_every_column_matches_the_model(model, factory_name, names_attr):
    module = _module()
    table = model.__table__
    columns = getattr(module, factory_name)()
    for column in columns:
        model_column = table.c[column.name]
        assert _pg(model_column.type) == _pg(column.type), column.name
        assert model_column.nullable and column.nullable, column.name
        assert model_column.default is None and model_column.server_default is None, column.name
        assert column.default is None and column.server_default is None, column.name
    assert getattr(module, names_attr) == tuple(column.name for column in columns)
    assert set(getattr(module, names_attr)) <= set(table.c.keys())


def test_the_column_types_are_the_contract():
    module = _module()
    expected = {
        "action_taken": "TEXT",
        "concession_reference": "VARCHAR(100)",
        "concession_authorized_by": "VARCHAR(255)",
        "concession_date": "DATE",
        "reverification_result": "VARCHAR(20)",
        "reverification_notes": "TEXT",
        "reverified_by": "INTEGER",
        "reverified_at": "TIMESTAMP WITHOUT TIME ZONE",
        "dispositioned_by": "INTEGER",
        "dispositioned_at": "TIMESTAMP WITHOUT TIME ZONE",
        "effectiveness_result": "VARCHAR(20)",
        "effectiveness_verified_by": "INTEGER",
        "effectiveness_verified_at": "TIMESTAMP WITHOUT TIME ZONE",
        "effectiveness_due": "DATE",
        "human_factors_review": "TEXT",
        "similar_nonconformities_review": "TEXT",
    }
    actual = {column.name: _pg(column.type) for column in module._ncr_columns() + module._car_columns()}
    assert actual == expected
    # The existing evidence column is reused, never re-added or altered here.
    assert "effectiveness_check" not in module.CAR_COLUMN_NAMES


def test_foreign_keys_match_the_model():
    module = _module()
    assert module.NCR_FOREIGN_KEYS == (
        ("fk_ncrs_reverified_by", "users", "reverified_by"),
        ("fk_ncrs_dispositioned_by", "users", "dispositioned_by"),
    )
    assert module.CAR_FOREIGN_KEYS == (("fk_cars_effectiveness_verified_by", "users", "effectiveness_verified_by"),)
    for model, foreign_keys in (
        (NonConformanceReport, module.NCR_FOREIGN_KEYS),
        (CorrectiveActionRequest, module.CAR_FOREIGN_KEYS),
    ):
        model_fks = _model_fks(model.__table__)
        for name, referred_table, local_column in foreign_keys:
            assert model_fks[name] == (referred_table, local_column), name


def test_check_texts_are_byte_identical_to_the_model_and_the_enums():
    module = _module()
    assert module.REVERIFICATION_CHECK_SQL == "reverification_result IN ('conforming', 'nonconforming')"
    assert module.EFFECTIVENESS_CHECK_SQL == "effectiveness_result IN ('effective', 'not_effective')"
    assert module.REVERIFICATION_CHECK_SQL == NCR_REVERIFICATION_RESULT_CHECK
    assert module.EFFECTIVENESS_CHECK_SQL == CAR_EFFECTIVENESS_RESULT_CHECK
    ncr_checks = _model_checks(NonConformanceReport.__table__)
    car_checks = _model_checks(CorrectiveActionRequest.__table__)
    assert ncr_checks[module.REVERIFICATION_CHECK] == module.REVERIFICATION_CHECK_SQL
    assert car_checks[module.EFFECTIVENESS_CHECK] == module.EFFECTIVENESS_CHECK_SQL
    # 122's CHECK on cars is still declared alongside 127's.
    assert "ck_cars_initiation_reason" in car_checks
    assert [result.value for result in NCRReverificationResult] == ["conforming", "nonconforming"]
    assert [result.value for result in CAREffectivenessResult] == ["effective", "not_effective"]


def test_verb_only_columns_are_on_no_request_schema():
    for schema in (NCRCreate, NCRUpdate):
        assert not set(schema.model_fields) & NCR_VERB_ONLY, schema.__name__
    for schema in (CARCreate, CARUpdate):
        assert not set(schema.model_fields) & CAR_VERB_ONLY, schema.__name__
    assert {"action_taken", "concession_reference", "concession_authorized_by", "concession_date"} <= set(
        NCRUpdate.model_fields
    )
    for schema in (CARCreate, CARUpdate):
        assert {"human_factors_review", "similar_nonconformities_review", "effectiveness_due"} <= set(
            schema.model_fields
        ), schema.__name__


# ------------------------------------------------------------- 3. real SQLite round-trip

INITIATION_REASON_CHECK_SQL = "initiation_reason IN ('internal_audit', 'other')"

LEGACY_DDL = [
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    (
        "CREATE TABLE cars (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, car_number VARCHAR(50) NOT NULL, "
        "title VARCHAR(255) NOT NULL, problem_description TEXT NOT NULL, effectiveness_check TEXT, due_date DATE, "
        "initiation_reason VARCHAR(40), quality_approved_by INTEGER, "
        "CONSTRAINT fk_cars_quality_approved_by FOREIGN KEY(quality_approved_by) REFERENCES users (id), "
        f"CONSTRAINT ck_cars_initiation_reason CHECK ({INITIATION_REASON_CHECK_SQL}))"
    ),
    "CREATE INDEX ix_cars_due_date ON cars (due_date)",
    (
        "CREATE TABLE ncrs (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, ncr_number VARCHAR(50) NOT NULL, "
        "title VARCHAR(255) NOT NULL, description TEXT NOT NULL, disposition VARCHAR(16), "
        "car_id INTEGER REFERENCES cars (id), is_deleted BOOLEAN NOT NULL DEFAULT 0, deleted_at DATETIME, "
        "deleted_by INTEGER REFERENCES users (id), "
        "CONSTRAINT uq_ncrs_company_ncr_number UNIQUE (company_id, ncr_number))"
    ),
    "CREATE INDEX ix_ncrs_disposition ON ncrs (disposition)",
    (
        "CREATE TABLE customer_complaints (id INTEGER PRIMARY KEY, ncr_id INTEGER REFERENCES ncrs (id), "
        "car_id INTEGER REFERENCES cars (id))"
    ),
    "INSERT INTO companies VALUES (1)",
    "INSERT INTO users VALUES (5)",
    (
        "INSERT INTO cars VALUES (7, 1, 'CAR-LEGACY-001', 'Legacy CAR', 'Recorded before 127.', "
        "'Legacy evidence reviewed.', '2026-01-02', 'internal_audit', 5)"
    ),
    # A live NCR that ALREADY carries a disposition: the stamp must still read NULL (no backfill).
    (
        "INSERT INTO ncrs VALUES (1, 1, 'NCR-LEGACY-001', 'Legacy NCR', 'Recorded before 127.', 'rework', 7, 0, "
        "NULL, NULL)"
    ),
    # A soft-deleted NCR: its tombstone survives the rebuild in both directions.
    (
        "INSERT INTO ncrs VALUES (2, 1, 'NCR-LEGACY-002', 'Voided NCR', 'Deleted before 127.', 'scrap', NULL, 1, "
        "'2026-02-03 04:05:06', 5)"
    ),
    "INSERT INTO customer_complaints VALUES (3, 1, 7)",
]

NCR_LEGACY_ROWS_SQL = (
    "SELECT id, ncr_number, disposition, car_id, is_deleted, deleted_at, deleted_by FROM ncrs ORDER BY id"
)
NCR_LEGACY_ROWS = [
    (1, "NCR-LEGACY-001", "rework", 7, 0, None, None),
    (2, "NCR-LEGACY-002", "scrap", None, 1, "2026-02-03 04:05:06", 5),
]
CAR_LEGACY_ROW_SQL = (
    "SELECT car_number, title, effectiveness_check, due_date, initiation_reason, quality_approved_by FROM cars"
)
CAR_LEGACY_ROW = ("CAR-LEGACY-001", "Legacy CAR", "Legacy evidence reviewed.", "2026-01-02", "internal_audit", 5)


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm127.db'}")
    with engine.begin() as connection:
        for statement in LEGACY_DDL:
            connection.exec_driver_sql(statement)
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _columns(connection, table):
    return {column["name"] for column in sa.inspect(connection).get_columns(table)}


def _objects(connection, table):
    inspector = sa.inspect(connection)
    return {
        "columns": _columns(connection, table),
        "fks": {fk["name"] for fk in inspector.get_foreign_keys(table) if fk["name"]},
        "unnamed_fks": sorted(
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys(table)
            if not fk["name"]
        ),
        "checks": {ck["name"]: re.sub(r"\s+", " ", ck["sqltext"]) for ck in inspector.get_check_constraints(table)},
        "indexes": {ix["name"] for ix in inspector.get_indexes(table)},
        "uniques": {uq["name"] for uq in inspector.get_unique_constraints(table)},
    }


def _assert_legacy_rows_intact(connection):
    assert [tuple(row) for row in connection.exec_driver_sql(NCR_LEGACY_ROWS_SQL)] == NCR_LEGACY_ROWS
    assert tuple(connection.exec_driver_sql(CAR_LEGACY_ROW_SQL).one()) == CAR_LEGACY_ROW
    assert tuple(connection.exec_driver_sql("SELECT ncr_id, car_id FROM customer_complaints").one()) == (1, 7)


def test_upgrade_adds_every_object_and_backfills_nothing(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in ("ncrs", "cars")}
    migration.upgrade()

    ncrs, cars = _objects(connection, "ncrs"), _objects(connection, "cars")
    assert ncrs["columns"] == legacy["ncrs"]["columns"] | set(migration.NCR_COLUMN_NAMES)
    assert cars["columns"] == legacy["cars"]["columns"] | set(migration.CAR_COLUMN_NAMES)
    assert ncrs["fks"] == {name for name, _, _ in migration.NCR_FOREIGN_KEYS}
    assert cars["fks"] == {"fk_cars_quality_approved_by"} | {name for name, _, _ in migration.CAR_FOREIGN_KEYS}
    assert ncrs["checks"] == {migration.REVERIFICATION_CHECK: migration.REVERIFICATION_CHECK_SQL}
    assert cars["checks"] == {
        "ck_cars_initiation_reason": INITIATION_REASON_CHECK_SQL,
        migration.EFFECTIVENESS_CHECK: migration.EFFECTIVENESS_CHECK_SQL,
    }
    # Everything that was already there is carried across the SQLite rebuild.
    for table, objects in (("ncrs", ncrs), ("cars", cars)):
        assert objects["indexes"] == legacy[table]["indexes"], table
        assert objects["uniques"] == legacy[table]["uniques"], table
        assert objects["unnamed_fks"] == legacy[table]["unnamed_fks"], table
    _assert_legacy_rows_intact(connection)

    # No backfill: every new column reads NULL, including the disposition stamp of an NCR that
    # already carries a disposition, and the soft-deleted NCR.
    ncr_new = ", ".join(migration.NCR_COLUMN_NAMES)
    for row in connection.exec_driver_sql(f"SELECT {ncr_new} FROM ncrs"):
        assert set(row) == {None}
    car_new = ", ".join(migration.CAR_COLUMN_NAMES)
    assert set(connection.exec_driver_sql(f"SELECT {car_new} FROM cars").one()) == {None}


@pytest.mark.parametrize(
    "table, column, row_id, values",
    [
        ("ncrs", "reverification_result", 1, [result.value for result in NCRReverificationResult]),
        ("cars", "effectiveness_result", 7, [result.value for result in CAREffectivenessResult]),
    ],
)
def test_the_checks_admit_each_value_and_null_and_refuse_anything_else(scratch, table, column, row_id, values):
    connection, migration = scratch
    migration.upgrade()
    update = f"UPDATE {table} SET {column} = :value WHERE id = :id"
    for value in values + [None]:
        connection.execute(sa.text(update), {"value": value, "id": row_id})
    for refused in ("pass", values[0].upper(), "pending", ""):
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(update), {"value": refused, "id": row_id})
        nested.rollback()


def test_the_new_foreign_keys_point_at_users(scratch):
    connection, migration = scratch
    migration.upgrade()
    inspector = sa.inspect(connection)
    for table, foreign_keys in (("ncrs", migration.NCR_FOREIGN_KEYS), ("cars", migration.CAR_FOREIGN_KEYS)):
        reflected = {fk["name"]: fk for fk in inspector.get_foreign_keys(table)}
        for name, referred_table, local_column in foreign_keys:
            assert reflected[name]["referred_table"] == referred_table
            assert reflected[name]["constrained_columns"] == [local_column]
            assert reflected[name]["referred_columns"] == ["id"]


def test_rerun_is_a_no_op_and_a_partial_schema_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql(
        "UPDATE ncrs SET action_taken = 'Reworked bore.', reverification_result = 'conforming', "
        "reverified_by = 5, dispositioned_by = 5 WHERE id = 1"
    )
    connection.exec_driver_sql("UPDATE cars SET human_factors_review = 'Fatigue on 2nd shift.' WHERE id = 7")
    migration.upgrade()  # a re-run is a guarded no-op

    # Knock out one object of each kind -- an FK + its column on ncrs, a plain column on ncrs, the
    # CHECK and an FK on cars -- and prove each guard restores only what is missing.
    ops = Operations(MigrationContext.configure(connection))
    with ops.batch_alter_table("ncrs") as batch:
        batch.drop_constraint("fk_ncrs_dispositioned_by", type_="foreignkey")
        batch.drop_column("dispositioned_by")
        batch.drop_column("concession_date")
    with ops.batch_alter_table("cars") as batch:
        batch.drop_constraint(migration.EFFECTIVENESS_CHECK, type_="check")
        batch.drop_constraint("fk_cars_effectiveness_verified_by", type_="foreignkey")
    assert "dispositioned_by" not in _columns(connection, "ncrs")
    assert migration.EFFECTIVENESS_CHECK not in _objects(connection, "cars")["checks"]

    migration.upgrade()
    ncrs, cars = _objects(connection, "ncrs"), _objects(connection, "cars")
    assert set(migration.NCR_COLUMN_NAMES) <= ncrs["columns"]
    assert ncrs["fks"] == {name for name, _, _ in migration.NCR_FOREIGN_KEYS}
    assert cars["fks"] >= {name for name, _, _ in migration.CAR_FOREIGN_KEYS}
    assert cars["checks"][migration.EFFECTIVENESS_CHECK] == migration.EFFECTIVENESS_CHECK_SQL
    # Values recorded in the surviving columns are kept; nothing is overwritten.
    row = connection.exec_driver_sql(
        "SELECT action_taken, reverification_result, reverified_by, dispositioned_by FROM ncrs WHERE id = 1"
    ).one()
    assert tuple(row) == ("Reworked bore.", "conforming", 5, None)
    assert connection.exec_driver_sql("SELECT human_factors_review FROM cars").scalar_one() == "Fatigue on 2nd shift."
    _assert_legacy_rows_intact(connection)


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in ("ncrs", "cars")}
    migration.upgrade()
    connection.exec_driver_sql("UPDATE ncrs SET action_taken = 'Scrapped.', dispositioned_by = 5 WHERE id = 2")
    migration.downgrade()

    for table in ("ncrs", "cars"):
        assert _objects(connection, table) == legacy[table], table
    _assert_legacy_rows_intact(connection)
    migration.downgrade()  # a second downgrade is a guarded no-op, not an error
    for table in ("ncrs", "cars"):
        assert _objects(connection, table) == legacy[table], table

    migration.upgrade()  # and the chain replays forward cleanly
    assert set(migration.NCR_COLUMN_NAMES) <= _columns(connection, "ncrs")
    assert set(migration.CAR_COLUMN_NAMES) <= _columns(connection, "cars")
    _assert_legacy_rows_intact(connection)


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

    module = _module()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    tables = [Base.metadata.tables[name] for name in ("companies", "users", "cars", "ncrs")]
    with engine.begin() as conn:
        # Only what 127 touches and what its new FKs reference; other FK targets are irrelevant here.
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        Base.metadata.create_all(conn, tables=tables)
        recorder = _Recorder(conn)
        module.op = recorder
        module.upgrade()
    engine.dispose()
    assert recorder.calls == []


# ------------------------------------------------------------- 5. offline PostgreSQL SQL


def _render(function_name: str):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


def _first_index(statements, prefix):
    return next(i for i, statement in enumerate(statements) if statement.startswith(prefix))


def _last_index(statements, prefix):
    return max(i for i, statement in enumerate(statements) if statement.startswith(prefix))


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    assert statements[0] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-1] == "SET LOCAL lock_timeout = DEFAULT"
    sql = "\n".join(statements)
    assert "ALTER TABLE ncrs ADD COLUMN action_taken TEXT" in sql
    assert "ALTER TABLE ncrs ADD COLUMN concession_reference VARCHAR(100)" in sql
    assert "ALTER TABLE ncrs ADD COLUMN concession_authorized_by VARCHAR(255)" in sql
    assert "ALTER TABLE ncrs ADD COLUMN concession_date DATE" in sql
    assert "ALTER TABLE ncrs ADD COLUMN reverification_result VARCHAR(20)" in sql
    assert "ALTER TABLE ncrs ADD COLUMN reverified_at TIMESTAMP WITHOUT TIME ZONE" in sql
    assert "ALTER TABLE ncrs ADD COLUMN dispositioned_by INTEGER" in sql
    assert "ALTER TABLE cars ADD COLUMN effectiveness_result VARCHAR(20)" in sql
    assert "ALTER TABLE cars ADD COLUMN effectiveness_verified_at TIMESTAMP WITHOUT TIME ZONE" in sql
    assert "ALTER TABLE cars ADD COLUMN effectiveness_due DATE" in sql
    assert "ALTER TABLE cars ADD COLUMN similar_nonconformities_review TEXT" in sql
    module = _module()
    for table, foreign_keys in (("ncrs", module.NCR_FOREIGN_KEYS), ("cars", module.CAR_FOREIGN_KEYS)):
        for name, referred, column in foreign_keys:
            assert f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY({column}) REFERENCES {referred} (id)" in sql
    assert (
        "ALTER TABLE ncrs ADD CONSTRAINT ck_ncrs_reverification_result "
        "CHECK (reverification_result IN ('conforming', 'nonconforming'))"
    ) in sql
    assert (
        "ALTER TABLE cars ADD CONSTRAINT ck_cars_effectiveness_result "
        "CHECK (effectiveness_result IN ('effective', 'not_effective'))"
    ) in sql
    # ncrs first, then cars (the lock order the docstring states).
    assert _last_index(statements, "ALTER TABLE ncrs") < _first_index(statements, "ALTER TABLE cars")
    assert sum(statement.startswith("ALTER TABLE ncrs ADD COLUMN") for statement in statements) == 10
    assert sum(statement.startswith("ALTER TABLE cars ADD COLUMN") for statement in statements) == 6
    assert "NOT NULL" not in sql and "DEFAULT" not in sql.replace("lock_timeout = DEFAULT", "")
    assert "CREATE INDEX" not in sql and "CREATE TABLE" not in sql
    assert "effectiveness_check" not in sql and "is_deleted" not in sql and "audit_log" not in sql
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "127 is pure DDL: no backfill"
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|\banon\b|authenticated|service_role", sql)


def test_offline_downgrade_drops_exactly_what_upgrade_added_in_reverse():
    statements = _render("downgrade")
    assert statements[0] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-1] == "SET LOCAL lock_timeout = DEFAULT"
    sql = "\n".join(statements)
    module = _module()
    assert "ALTER TABLE ncrs DROP CONSTRAINT ck_ncrs_reverification_result" in sql
    assert "ALTER TABLE cars DROP CONSTRAINT ck_cars_effectiveness_result" in sql
    for table, foreign_keys, columns in (
        ("ncrs", module.NCR_FOREIGN_KEYS, module.NCR_COLUMN_NAMES),
        ("cars", module.CAR_FOREIGN_KEYS, module.CAR_COLUMN_NAMES),
    ):
        for name, _, _ in foreign_keys:
            assert f"ALTER TABLE {table} DROP CONSTRAINT {name}" in sql
        for column in columns:
            assert f"ALTER TABLE {table} DROP COLUMN {column}" in sql
        # Within a table: the CHECK, then the FKs, then the columns they constrain.
        drops = [s for s in statements if s.startswith(f"ALTER TABLE {table} DROP")]
        first_column = _first_index(drops, f"ALTER TABLE {table} DROP COLUMN")
        assert all("DROP CONSTRAINT" in s for s in drops[:first_column])
        assert all("DROP COLUMN" in s for s in drops[first_column:])
    assert sum(statement.startswith("ALTER TABLE ncrs DROP COLUMN") for statement in statements) == 10
    assert sum(statement.startswith("ALTER TABLE cars DROP COLUMN") for statement in statements) == 6
    # Reverse of the upgrade: cars first, then ncrs.
    assert _last_index(statements, "ALTER TABLE cars") < _first_index(statements, "ALTER TABLE ncrs")
    assert "DROP TABLE" not in sql and "DROP INDEX" not in sql
    assert "effectiveness_check" not in sql and "is_deleted" not in sql and "audit_log" not in sql
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql)


def test_create_all_ddl_carries_the_same_foreign_keys_and_checks_on_postgres():
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    NonConformanceReport.__table__.create(engine, checkfirst=False)
    CorrectiveActionRequest.__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    module = _module()
    assert f"CONSTRAINT {module.REVERIFICATION_CHECK} CHECK ({module.REVERIFICATION_CHECK_SQL})" in ddl
    assert f"CONSTRAINT {module.EFFECTIVENESS_CHECK} CHECK ({module.EFFECTIVENESS_CHECK_SQL})" in ddl
    for name, referred, column in module.NCR_FOREIGN_KEYS + module.CAR_FOREIGN_KEYS:
        assert f"CONSTRAINT {name} FOREIGN KEY({column}) REFERENCES {referred} (id)" in ddl
