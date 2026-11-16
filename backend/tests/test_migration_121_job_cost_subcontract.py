"""Coverage for 121_job_cost_subcontract (P1.3: the subcontract element of job cost).

121 is additive over two EXISTING objects:

- ``job_costs.actual_subcontract_cost``  Float, NOT NULL, server default ``'0'``, no index.
- the ``SUBCONTRACT`` label on the native PostgreSQL enum ``costentrytype`` behind
  ``cost_entries.entry_type`` -- only where that type exists (migration 024 built the column
  as ``VARCHAR(50)``; ``create_all`` builds the native enum).

The layers follow 117's test:

1. **Script wiring** -- one head, the assigned 120 -> 121 chain, an id that fits varchar(32).
2. **What the functions EMIT** -- ``upgrade()`` / ``downgrade()`` against a recording ``op``
   with the column guard stubbed both ways.
2b. **The SQL PostgreSQL receives** -- the real ``Operations`` in offline mode.
3. **Source invariants** -- pure DDL, no table, no RLS statement, no ``audit_log`` contact.
4. **Model / migration lock-step** -- by DIALECT-COMPILING the DDL, never by trusting
   SQLite's execution (CLAUDE.md -> "Why the tests run on SQLite").
5. **A real upgrade -> downgrade -> upgrade round-trip** over a create_all-bootstrapped
   SQLite file, plus the data claim: a pre-121 job cost reads ``actual_subcontract_cost = 0``
   and keeps every other value.

THE THINGS A FUTURE READER WILL TRY TO "FIX"
--------------------------------------------
* **The label is UPPERCASE.** ``SQLEnum(CostEntryType)`` has no ``values_callable`` and
  stores member NAMES; adding ``'subcontract'`` to the type would make every insert fail.
* **The ALTER is guarded by ``to_regtype``.** Removing the guard (066 has none) aborts the
  upgrade on any database whose ``entry_type`` is 024's VARCHAR.
* **The server default is not a backfill guess.** No SUBCONTRACT entry could exist before
  121, so 0 is the true total for every pre-existing row.
"""

import importlib
import importlib.util
import io
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import List

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.unit

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_121 = "121_job_cost_subcontract"
MIGRATION_FILE = "121_job_cost_subcontract.py"
DOWN_REVISION = "120_production_calendar"

TABLE = "job_costs"
COLUMN = "actual_subcontract_cost"

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = DEFAULT"
ADD_LABEL = (
    "DO $$ BEGIN IF to_regtype('costentrytype') IS NOT NULL THEN "
    "ALTER TYPE costentrytype ADD VALUE IF NOT EXISTS 'SUBCONTRACT'; END IF; END $$"
)


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_121", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped (the prose names what the code avoids)."""
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


class _RecordedDDL:
    """A stand-in for alembic's ``op`` that records calls instead of executing them."""

    def __init__(self, dialect_name: str):
        self.dialect_name = dialect_name
        self.calls: list = []

    def get_bind(self):  # pragma: no cover - the guard is stubbed; reaching this is a bug
        raise AssertionError("the recorder stubs _has_column; nothing may inspect a database")

    def get_context(self):
        recorder = self

        @contextmanager
        def autocommit_block():
            recorder.calls.append(("autocommit_begin", None))
            yield
            recorder.calls.append(("autocommit_end", None))

        return SimpleNamespace(
            as_sql=False, dialect=SimpleNamespace(name=self.dialect_name), autocommit_block=autocommit_block
        )

    def add_column(self, table_name, column, **kwargs):
        self.calls.append(("add_column", table_name, column))

    def drop_column(self, table_name, column_name, **kwargs):
        self.calls.append(("drop_column", table_name, column_name))

    @contextmanager
    def batch_alter_table(self, table_name, **kwargs):
        recorder = self

        class _Batch:
            def drop_column(self, column_name, **kw):
                recorder.calls.append(("batch_drop_column", table_name, column_name))

        yield _Batch()

    def execute(self, statement):
        self.calls.append(("execute", " ".join(str(statement).split())))

    def create_table(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.calls.append(("create_table",))

    def create_index(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.calls.append(("create_index",))

    @property
    def sequence(self) -> list:
        return [(call[0], call[1]) for call in self.calls]


def _record(function_name: str, *, dialect: str, has_column: bool) -> _RecordedDDL:
    module = _load_module()
    recorder = _RecordedDDL(dialect)
    module.op = recorder
    module._has_table = lambda _table: True
    module._has_column = lambda _table, _column: has_column
    getattr(module, function_name)()
    return recorder


def _render(function_name: str, *, dialect: str) -> List[str]:
    module = _load_module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)

    def _must_not_inspect(*_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("offline mode must not inspect a database")

    module._has_column = _must_not_inspect
    module._has_table = _must_not_inspect
    with context.begin_transaction():
        getattr(module, function_name)()
    return [" ".join(statement.split()) for statement in buffer.getvalue().split(";\n") if statement.strip()]


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


def test_single_head_and_the_assigned_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"
    assert scripts.get_revision(REVISION_121).down_revision == DOWN_REVISION


def test_revision_id_fits_alembic_version_varchar32():
    assert len(REVISION_121) <= 32


def test_module_names_exactly_the_objects_it_owns():
    module = _load_module()
    assert module.revision == REVISION_121
    assert module.down_revision == DOWN_REVISION
    assert module.TABLE_NAME == TABLE
    assert module.COLUMN_NAME == COLUMN
    assert module.ENUM_TYPE_NAME == "costentrytype"
    assert module.NEW_ENUM_LABEL == "SUBCONTRACT"
    assert " ".join(module.ADD_ENUM_LABEL_SQL.split()) == ADD_LABEL


# ---------------------------------------------------------------------------
# 2. What upgrade() / downgrade() emit
# ---------------------------------------------------------------------------


def test_postgres_virgin_upgrade_adds_the_label_in_autocommit_then_the_column_under_a_local_timeout():
    recorder = _record("upgrade", dialect="postgresql", has_column=False)
    assert recorder.sequence == [
        ("autocommit_begin", None),
        ("execute", ADD_LABEL),
        ("autocommit_end", None),
        ("execute", LOCK_TIMEOUT),
        ("add_column", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    [(_, _, column)] = [call for call in recorder.calls if call[0] == "add_column"]
    assert column.name == COLUMN
    assert isinstance(column.type, sa.Float)
    assert column.nullable is False
    assert column.server_default.arg == "0"
    assert not column.index


def test_postgres_bootstrapped_upgrade_still_runs_the_self_guarded_label_and_skips_the_column():
    """create_all already built the column (guard -> skip) and, from the updated model, the
    label -- the in-database ``IF NOT EXISTS`` makes re-adding it a no-op, so the statement
    may run unconditionally on PostgreSQL."""
    recorder = _record("upgrade", dialect="postgresql", has_column=True)
    assert recorder.sequence == [("autocommit_begin", None), ("execute", ADD_LABEL), ("autocommit_end", None)]


def test_sqlite_upgrade_never_touches_an_enum_type():
    """SQLite has no native enum and SQLEnum creates no CHECK, so only the column applies."""
    assert _record("upgrade", dialect="sqlite", has_column=False).sequence == [("add_column", TABLE)]
    assert _record("upgrade", dialect="sqlite", has_column=True).calls == []


def test_postgres_downgrade_drops_the_column_and_leaves_the_label():
    recorder = _record("downgrade", dialect="postgresql", has_column=True)
    assert recorder.sequence == [
        ("execute", LOCK_TIMEOUT),
        ("drop_column", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    assert recorder.calls[1][2] == COLUMN


def test_sqlite_downgrade_drops_the_column_in_batch_mode():
    assert _record("downgrade", dialect="sqlite", has_column=True).calls == [("batch_drop_column", TABLE, COLUMN)]


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_downgrade_over_an_already_downgraded_database_is_a_no_op(dialect: str):
    assert _record("downgrade", dialect=dialect, has_column=False).calls == []


# ---------------------------------------------------------------------------
# 2b. The SQL PostgreSQL actually receives
# ---------------------------------------------------------------------------


def test_postgres_upgrade_sql():
    """The label commits on its own (COMMIT / DO / BEGIN), then the NOT NULL DEFAULT '0'
    column -- a constant default, so the ADD COLUMN is metadata-only on PostgreSQL 11+."""
    assert _render("upgrade", dialect="postgresql") == [
        "BEGIN",
        "COMMIT",
        ADD_LABEL,
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} FLOAT DEFAULT '0' NOT NULL",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


def test_postgres_downgrade_sql():
    assert _render("downgrade", dialect="postgresql") == [
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


def test_the_label_statement_adds_the_uppercase_name_only_where_the_type_resolves():
    module = _load_module()
    sql = module.ADD_ENUM_LABEL_SQL
    assert "to_regtype('costentrytype') IS NOT NULL" in sql
    assert "ADD VALUE IF NOT EXISTS 'SUBCONTRACT'" in sql
    assert "'subcontract'" not in sql, "the type stores member NAMES; the lowercase .value must never be added"


# ---------------------------------------------------------------------------
# 3. Source invariants
# ---------------------------------------------------------------------------


def test_pure_ddl_no_backfill_and_no_audit_contact():
    body = _body()
    for forbidden in ("op.bulk_insert", "conn.execute", "sa.text(", "UPDATE ", "INSERT ", "DELETE "):
        assert forbidden not in body, f"121 must be pure DDL; found {forbidden!r}"
    assert "audit_log" not in body
    for chain_column in ("sequence_number", "previous_hash", "integrity_hash"):
        assert chain_column not in body


def test_no_table_is_created_so_no_rls_statement_is_needed():
    body = _body()
    assert "op.create_table" not in body
    assert "ROW LEVEL SECURITY" not in body
    assert "REVOKE" not in body
    assert "GRANT" not in body
    for role in ("anon", "authenticated", "service_role"):
        assert not re.search(rf"\b{role}\b", body), role


def test_downgrade_is_real_and_the_rollback_hazard_is_disclosed():
    body = _body()
    downgrade = body[body.index("def downgrade") :]
    assert "batch_op.drop_column(COLUMN_NAME)" in downgrade
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in downgrade
    docstring = _load_module().__doc__ or ""
    assert "WHERE entry_type = 'SUBCONTRACT'" in docstring


def test_the_table_already_carries_tenant_scoping():
    from app.models.job_costing import CostEntry, JobCost

    for model in (JobCost, CostEntry):
        company_id = model.__table__.columns["company_id"]
        assert company_id.nullable is False
        assert company_id.index is True


# ---------------------------------------------------------------------------
# 4. Model / migration lock-step (dialect-compiled)
# ---------------------------------------------------------------------------


def test_the_migration_and_the_model_agree_on_the_column():
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.job_costing import JobCost

    [(_, _, migrated)] = [
        call for call in _record("upgrade", dialect="postgresql", has_column=False).calls if call[0] == "add_column"
    ]
    model = JobCost.__table__.columns[COLUMN]
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        assert str(sa.schema.CreateColumn(migrated).compile(dialect=dialect)) == str(
            sa.schema.CreateColumn(model).compile(dialect=dialect)
        )
    assert migrated.nullable is model.nullable is False
    assert model.default.arg == 0.0
    assert not model.index
    assert not [idx for idx in JobCost.__table__.indexes if COLUMN in idx.columns]


def test_the_model_enum_stores_the_uppercase_name_on_the_type_the_migration_alters():
    from sqlalchemy.dialects import postgresql

    from app.models.job_costing import CostEntry, CostEntryType

    entry_type = CostEntry.__table__.c.entry_type.type
    assert entry_type.values_callable is None
    assert entry_type.name == "costentrytype"
    assert entry_type.enums == ["MATERIAL", "LABOR", "OVERHEAD", "OTHER", "SUBCONTRACT"]
    assert CostEntryType.SUBCONTRACT.value == "subcontract"
    rendered = str(sa.schema.CreateTable(CostEntry.__table__).compile(dialect=postgresql.dialect()))
    assert "entry_type costentrytype NOT NULL" in rendered
    create_type = str(postgresql.CreateEnumType(entry_type).compile(dialect=postgresql.dialect()))
    assert "'SUBCONTRACT'" in create_type and "'subcontract'" not in create_type


def test_the_column_is_on_no_request_schema():
    """Computed from the entries by recalculate_totals; a client never sets it."""
    import pkgutil

    from pydantic import BaseModel

    import app.schemas as schemas_pkg

    offenders = []
    for info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f"app.schemas.{info.name}")
        for name, obj in vars(module).items():
            if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
                continue
            if not name.endswith(("Create", "Update", "Request")):
                continue
            if COLUMN in getattr(obj, "model_fields", {}):
                offenders.append(f"{module.__name__}.{name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# 5. Real round-trip over a create_all-bootstrapped SQLite file
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert (
        result.returncode == 0
    ), f"alembic {' '.join(args)} failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _column(engine, table: str, column: str):
    return next((c for c in sa.inspect(engine).get_columns(table) if c["name"] == column), None)


def _neighbours(engine) -> tuple:
    """Everything on job_costs OTHER than the 121 column -- what batch mode must carry across."""
    inspector = sa.inspect(engine)
    columns = sorted(
        (c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default")))
        for c in inspector.get_columns(TABLE)
        if c["name"] != COLUMN
    )
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE))
    fks = sorted(
        (fk.get("name") or "", tuple(fk["constrained_columns"]), fk["referred_table"])
        for fk in inspector.get_foreign_keys(TABLE)
    )
    uniques = sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(TABLE))
    return columns, indexes, fks, uniques


def _assert_present(engine) -> None:
    column = _column(engine, TABLE, COLUMN)
    assert column is not None, f"{TABLE}.{COLUMN} is missing"
    assert column["nullable"] is False
    assert str(column.get("default")).strip("'") == "0"


@pytest.mark.integration
@pytest.mark.slow
def test_migration_121_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mig121.db'}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        _assert_present(engine)
        neighbours = _neighbours(engine)

        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_121)
        _assert_present(engine)
        assert _neighbours(engine) == neighbours

        _alembic(db_url, "downgrade", "-1")
        assert _column(engine, TABLE, COLUMN) is None
        assert _neighbours(engine) == neighbours

        _alembic(db_url, "upgrade", REVISION_121)
        _assert_present(engine)
        assert _neighbours(engine) == neighbours

        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_121)
        _assert_present(engine)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_a_pre_121_job_cost_reads_zero_subcontract_and_keeps_every_other_value(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mig121_forward.db'}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # Reach the PRE-121 shape by running 121's own downgrade.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_121)
        _alembic(db_url, "downgrade", "-1")
        assert _column(engine, TABLE, COLUMN) is None

        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders (id, work_order_number, work_order_type, quantity_ordered, status, "
                    "company_id, version) VALUES (1, 'PRE-121-WO', 'laser_cutting', 1, 'COMPLETE', 1, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO job_costs (id, work_order_id, actual_material_cost, actual_labor_cost, "
                    "actual_overhead_cost, actual_total_cost, status, updated_at, company_id) "
                    "VALUES (1, 1, 10.0, 20.0, 5.0, 35.0, 'COMPLETED', '2026-10-01 08:15:00.000000', 1)"
                )
            )

        _alembic(db_url, "upgrade", REVISION_121)

        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {COLUMN}, actual_material_cost, actual_labor_cost, actual_overhead_cost, "
                    "actual_total_cost, status, updated_at FROM job_costs WHERE id = 1"
                )
            ).one()
            assert tuple(row) == (0.0, 10.0, 20.0, 5.0, 35.0, "COMPLETED", "2026-10-01 08:15:00.000000")
            # The new entry type is insertable straight away (no CHECK on SQLite / VARCHAR).
            conn.execute(
                sa.text(
                    "INSERT INTO cost_entries (job_cost_id, entry_type, description, total_cost, entry_date, "
                    "company_id) VALUES (1, 'SUBCONTRACT', 'Plating', 12.5, '2026-10-02', 1)"
                )
            )
    finally:
        engine.dispose()
