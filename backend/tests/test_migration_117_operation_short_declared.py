"""Coverage for 117_operation_short_declared (owner ruling 1: the persisted short declaration).

117 is purely additive over the EXISTING ``work_order_operations`` table -- ONE column,
no data:

- ``work_order_operations.quantity_short_declared``  Float, NULLABLE, no server default,
  no Python default, no index.

The behavior (who writes it, who reads it) is covered by the short-count API tests; this
file covers the MIGRATION, in the layers the suite already uses (…070, …083, …114):

1. **Script wiring** (unit) -- a single alembic head, the 116 -> 117 chain, an id that
   fits ``alembic_version``'s varchar(32).
2. **What the functions EMIT** (unit) -- ``upgrade()`` / ``downgrade()`` run against a
   recording stand-in for ``op`` with the guard stubbed both ways (088's idiom), so "the
   bootstrapped database is a clean no-op", "the downgrade is real", "SQLite drops in
   batch mode" and "the lock_timeout is LOCAL and handed back" are proven by execution,
   not by grepping source.
2b. **The SQL PostgreSQL actually receives** (unit) -- the real alembic ``Operations`` in
   offline (``as_sql``) mode renders both directions for the postgresql dialect.
3. **Source invariants** (unit) -- pure DDL, no table created (so no RLS statement), no
   ``audit_log`` contact, a downgrade that is not a stub.
4. **Model / migration lock-step** (unit) -- the model declares exactly the column the
   migration builds (the stamped-over hazard), asserted by DIALECT-COMPILING the DDL for
   PostgreSQL and SQLite rather than trusting SQLite's execution (CLAUDE.md -> "Why the
   tests run on SQLite"); and the column is on no request schema.
5. **A real upgrade -> downgrade -> upgrade round-trip** (integration/slow) over a
   disposable SQLite file bootstrapped create_all -> stamp(116), plus the data claim: a
   pre-117 operation -- a COMPLETE one with scrap on it, the exact shape a read-time
   derivation would have mistaken for a short close -- comes out with
   ``quantity_short_declared IS NULL`` and every other field untouched.

THE THINGS A FUTURE READER WILL TRY TO "FIX"
--------------------------------------------
* **No backfill.** Deriving a declaration for existing COMPLETE-with-scrap rows from
  their counts is exactly the inference the column exists to replace. Every pre-117 row
  is NULL = "never declared" = zero upstream credit, which is what keeps clock-out,
  reconcile-on-read and the header byte-identical on existing data after deploy.
* **No default, and never NOT NULL.** ``0`` would be a declaration ("closed with zero
  short") that nobody made; NULL is the only honest pre-117 value.
* **No index.** It is read row-wise off operations already selected by work order --
  never filtered or sorted on -- so an index would cost every shop-floor write and buy
  nothing.
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

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_117 = "117_operation_short_declared"
MIGRATION_FILE = "117_operation_short_declared.py"
DOWN_REVISION = "116_business_keys_per_company"

TABLE = "work_order_operations"
COLUMN = "quantity_short_declared"

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = DEFAULT"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_117", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose names at length every construct the migration deliberately avoids (no
    backfill, no default, no RLS statement) and quotes an export ``SELECT``, so assertions
    about ABSENT constructs have to look at code only or the docstring would fail them.
    """
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# Recording the DDL the migration actually emits (088's idiom)
# ---------------------------------------------------------------------------


class _RecordedDDL:
    """A stand-in for alembic's ``op`` that records calls instead of executing them."""

    def __init__(self, dialect_name: str, as_sql: bool = False):
        self.dialect_name = dialect_name
        self.as_sql = as_sql
        self.calls: list = []

    def get_bind(self):  # pragma: no cover - the guard is stubbed; reaching this is a bug
        raise AssertionError("the recorder stubs _has_column; nothing may inspect a database")

    def get_context(self):
        return SimpleNamespace(as_sql=self.as_sql, dialect=SimpleNamespace(name=self.dialect_name))

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
        self.calls.append(("execute", str(statement)))

    def create_index(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.calls.append(("create_index",))

    def bulk_insert(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.calls.append(("bulk_insert",))

    @property
    def sequence(self) -> list:
        """``(kind, subject)`` for every call; ``subject`` is the SQL for an execute."""
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
    """``upgrade()`` / ``downgrade()`` as the SQL the dialect receives, via offline mode.

    Offline mode has no database to inspect, so the migration takes its canonical path
    (``_offline()``) without any guard being stubbed -- which is also what proves the
    guards are never consulted under ``--sql``.
    """
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
    return [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_117)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_117) <= 32


@pytest.mark.unit
def test_module_loads_and_names_exactly_the_object_it_owns():
    module = _load_module()
    assert module.revision == REVISION_117
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert module.TABLE_NAME == TABLE
    assert module.COLUMN_NAME == COLUMN
    assert module.LOCK_TIMEOUT_SQL == LOCK_TIMEOUT
    assert module.RESET_LOCK_TIMEOUT_SQL == RESET_LOCK_TIMEOUT


# ---------------------------------------------------------------------------
# 2. What upgrade() / downgrade() emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_virgin_upgrade_adds_one_nullable_float_under_a_local_lock_timeout():
    recorder = _record("upgrade", dialect="postgresql", has_column=False)
    assert recorder.sequence == [
        ("execute", LOCK_TIMEOUT),
        ("add_column", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    [(_, _, column)] = [call for call in recorder.calls if call[0] == "add_column"]
    assert column.name == COLUMN
    assert isinstance(column.type, sa.Float)
    assert column.nullable is True
    assert column.server_default is None
    assert column.default is None
    assert not column.index


@pytest.mark.unit
def test_sqlite_virgin_upgrade_adds_the_column_and_sets_no_lock_timeout():
    """``lock_timeout`` is a PostgreSQL setting; SQLite would reject the statement."""
    recorder = _record("upgrade", dialect="sqlite", has_column=False)
    assert recorder.sequence == [("add_column", TABLE)]


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_the_bootstrapped_database_is_a_clean_no_op(dialect: str):
    """create_all builds the column from the model, then the chain runs every revision
    after the 058 stamp: the guard must make this one emit NOTHING -- not even the
    lock_timeout, which would otherwise take a pointless 5 s bound on a no-op."""
    assert _record("upgrade", dialect=dialect, has_column=True).calls == []


@pytest.mark.unit
def test_postgres_downgrade_is_a_plain_drop_under_the_same_local_lock_timeout():
    recorder = _record("downgrade", dialect="postgresql", has_column=True)
    assert recorder.sequence == [
        ("execute", LOCK_TIMEOUT),
        ("drop_column", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    assert recorder.calls[1][2] == COLUMN


@pytest.mark.unit
def test_sqlite_downgrade_drops_the_column_in_batch_mode():
    recorder = _record("downgrade", dialect="sqlite", has_column=True)
    assert recorder.calls == [("batch_drop_column", TABLE, COLUMN)]


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_downgrade_over_an_already_downgraded_database_is_a_no_op(dialect: str):
    assert _record("downgrade", dialect=dialect, has_column=False).calls == []


@pytest.mark.unit
@pytest.mark.parametrize("function_name", ["upgrade", "downgrade"])
@pytest.mark.parametrize("has_column", [True, False])
def test_the_only_statement_ever_executed_is_the_local_lock_timeout(function_name: str, has_column: bool):
    """LOCAL, never a session-level ``SET`` (which would outlive the revision and reach
    every later one in the same run), and whenever it is set it is reset -- AFTER the DDL
    it bounds."""
    recorder = _record(function_name, dialect="postgresql", has_column=has_column)
    executed = [call[1] for call in recorder.calls if call[0] == "execute"]
    assert executed in ([], [LOCK_TIMEOUT, RESET_LOCK_TIMEOUT])
    if executed:
        sequence = recorder.sequence
        ddl_at = [i for i, step in enumerate(sequence) if step[0] in ("add_column", "drop_column")]
        assert len(ddl_at) == 1
        assert sequence.index(("execute", LOCK_TIMEOUT)) < ddl_at[0] < sequence.index(("execute", RESET_LOCK_TIMEOUT))


# ---------------------------------------------------------------------------
# 2b. The SQL PostgreSQL actually receives (offline rendering)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_upgrade_sql():
    """A nullable ``FLOAT`` with no ``DEFAULT``: the shape that makes ``ADD COLUMN``
    metadata-only on PostgreSQL 11+ rather than a rewrite of every operation row."""
    assert _render("upgrade", dialect="postgresql") == [
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} FLOAT",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


@pytest.mark.unit
def test_postgres_downgrade_sql():
    assert _render("downgrade", dialect="postgresql") == [
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


@pytest.mark.unit
def test_sqlite_upgrade_sql_is_plain_ddl():
    assert _render("upgrade", dialect="sqlite") == [f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} FLOAT"]


# ---------------------------------------------------------------------------
# 3. Source invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pure_ddl_no_backfill_and_no_data_statement():
    """Every pre-117 operation is legitimately NULL (never declared), so the migration
    emits no data statement at all -- which also makes it structurally incapable of
    touching the tamper-evident audit_log hash chain. The only executed statements are the
    transaction-local ``lock_timeout`` and its reset."""
    body = _body()
    for forbidden in ("op.bulk_insert", "conn.execute", "sa.text(", "UPDATE ", "INSERT ", "DELETE ", "SELECT "):
        assert forbidden not in body, f"117 must be pure DDL; found {forbidden!r}"
    assert body.count("op.execute(LOCK_TIMEOUT_SQL)") == body.count("op.execute(RESET_LOCK_TIMEOUT_SQL)") == 2
    assert body.count("op.execute(") == 4
    assert "audit_log" not in body
    for chain_column in ("sequence_number", "previous_hash", "integrity_hash"):
        assert chain_column not in body


@pytest.mark.unit
def test_the_column_is_nullable_with_no_default_and_no_index():
    body = _body()
    assert "sa.Column(COLUMN_NAME, sa.Float(), nullable=True)" in body
    assert "server_default" not in body
    assert "default=" not in body
    assert "create_index" not in body
    assert "index=True" not in body


@pytest.mark.unit
def test_no_table_is_created_so_no_rls_statement_is_needed():
    """``work_order_operations`` exists and 059 already enabled RLS on it; the ENABLE-RLS
    convention is for NEW tables (the fresh-bootstrap gate keys off op.create_table). No
    statement names a Supabase role either."""
    body = _body()
    assert "op.create_table" not in body
    assert "ROW LEVEL SECURITY" not in body
    assert "REVOKE" not in body
    assert "GRANT" not in body
    for role in ("anon", "authenticated", "service_role"):
        # Whole words only: "canonical" contains "anon".
        assert not re.search(rf"\b{role}\b", body), role


@pytest.mark.unit
def test_downgrade_is_real_not_a_stub():
    source = _source()
    body = _body()
    assert "def downgrade() -> None:" in source
    downgrade = body[body.index("def downgrade") :]
    assert "if not _offline() and not _has_column(TABLE_NAME, COLUMN_NAME):" in downgrade
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in downgrade
    assert "batch_op.drop_column(COLUMN_NAME)" in downgrade
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in downgrade
    assert "\n    pass" not in source


@pytest.mark.unit
def test_the_lossy_downgrade_is_disclosed():
    """Dropping the column discards every declared short, and a later re-add cannot bring
    them back. The docstring must say so (and how to export them first) so nobody runs it
    as a routine code rollback -- pre-117 code ignores the column and needs no downgrade."""
    docstring = _load_module().__doc__ or ""
    assert "LOSSY" in docstring
    assert "WHERE quantity_short_declared IS NOT NULL" in docstring


@pytest.mark.unit
def test_the_table_already_carries_tenant_scoping():
    """Invariant 1 is unaffected: 117 adds no new tenant surface. Asserted rather than
    assumed, because "the table already has company_id" is why this migration may skip
    both the RLS statement and a company_id column of its own."""
    from app.models.work_order import WorkOrderOperation

    company_id = WorkOrderOperation.__table__.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True


# ---------------------------------------------------------------------------
# 4. Model / migration lock-step, asserted by DIALECT-COMPILING the DDL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_a_nullable_unindexed_float_with_no_default():
    from app.models.work_order import WorkOrderOperation

    col = WorkOrderOperation.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.Float)
    assert col.nullable is True
    assert not col.index
    assert col.unique is not True
    assert col.server_default is None
    assert col.default is None, "no python-side default either: NULL means never declared"
    assert not col.foreign_keys
    assert not [idx for idx in WorkOrderOperation.__table__.indexes if COLUMN in idx.columns]


@pytest.mark.unit
def test_the_migration_and_the_model_agree_on_the_column():
    """Lock-step: the create_all path and the upgrade path must build the same column
    (the 042/078/079/080 convention -- a migration-only object is how prod ends up missing
    what the model claims exists, and a model-only one is how a fresh database gets a
    column the incremental path never added)."""
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.work_order import WorkOrderOperation

    [(_, _, migrated)] = [
        call for call in _record("upgrade", dialect="postgresql", has_column=False).calls if call[0] == "add_column"
    ]
    model = WorkOrderOperation.__table__.columns[COLUMN]
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        assert migrated.type.compile(dialect=dialect) == model.type.compile(dialect=dialect)
    assert migrated.nullable is model.nullable is True
    assert migrated.server_default is model.server_default is None


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_column_ddl_compiles_nullable_with_no_default_on_both_engines(dialect_name: str):
    """DIALECT-COMPILED, not executed (CLAUDE.md -> "Why the tests run on SQLite")."""
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.work_order import WorkOrderOperation

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    col = WorkOrderOperation.__table__.columns[COLUMN]
    rendered = str(sa.schema.CreateColumn(col).compile(dialect=dialect)).upper()

    assert rendered.startswith(COLUMN.upper())
    assert "FLOAT" in rendered
    assert "NOT NULL" not in rendered
    assert "DEFAULT" not in rendered


@pytest.mark.unit
def test_the_column_is_on_no_request_schema():
    """Server-written ONLY, by the explicit close verbs, at the moment a named person
    closes an operation short. On ``WorkOrderOperationUpdate`` -- whose ``PUT`` blind-
    ``setattr``s every field -- it would let any editor rewrite what every later stage was
    credited with; on a create or complete request it would let a client assert a short
    nobody covered with scrap. Response schemas may carry it."""
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
    """Run the alembic CLI in a subprocess pointed at the scratch DB."""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _column(engine, table: str, column: str):
    return next((c for c in sa.inspect(engine).get_columns(table) if c["name"] == column), None)


def _neighbours(engine) -> tuple:
    """Everything on ``work_order_operations`` OTHER than the 117 column -- what batch
    mode's table rebuild must carry across untouched."""
    inspector = sa.inspect(engine)
    columns = sorted(
        (c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default")))
        for c in inspector.get_columns(TABLE)
        if c["name"] != COLUMN
    )
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE))
    checks = sorted((c["name"], c["sqltext"]) for c in inspector.get_check_constraints(TABLE))
    fks = sorted(
        (fk.get("name") or "", tuple(fk["constrained_columns"]), fk["referred_table"], str(fk.get("options")))
        for fk in inspector.get_foreign_keys(TABLE)
    )
    uniques = sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(TABLE))
    return columns, indexes, checks, fks, uniques


def _assert_present(engine) -> None:
    column = _column(engine, TABLE, COLUMN)
    assert column is not None, f"{TABLE}.{COLUMN} is missing"
    assert column["nullable"] is True
    assert column.get("default") is None


@pytest.mark.integration
@pytest.mark.slow
def test_migration_117_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig117.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-117 shape straight from the model.
        _assert_present(engine)
        neighbours = _neighbours(engine)

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: the guard fires -> clean no-op.
        _alembic(db_url, "upgrade", REVISION_117)
        _assert_present(engine)
        assert _neighbours(engine) == neighbours

        # 2. Downgrade: a REAL drop (the DDL is dialect-neutral; SQLite runs batch mode).
        _alembic(db_url, "downgrade", "-1")
        assert _column(engine, TABLE, COLUMN) is None
        # The batch-mode rebuild of work_order_operations carried every other column,
        # index, named CHECK, FK and unique constraint across untouched.
        assert _neighbours(engine) == neighbours

        # 3. Re-upgrade: the column comes back (the un-guarded ADD COLUMN).
        _alembic(db_url, "upgrade", REVISION_117)
        _assert_present(engine)
        assert _neighbours(engine) == neighbours

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping: stamp back
        #    and run upgrade() again over a DB that already has the column.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_117)
        _assert_present(engine)
        assert _neighbours(engine) == neighbours
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_pre_existing_operations_survive_the_upgrade_undeclared_and_untouched(tmp_path):
    """The data claim, executed rather than read off the source.

    The seeded operation is the exact shape a read-time derivation would have mistaken
    for a short close: COMPLETE at 8 good + 2 scrapped on a 10-piece work order. It must
    come out of the upgrade with ``quantity_short_declared IS NULL`` (never declared, so
    zero upstream credit downstream) and every other field -- counts, scrap reason,
    ``updated_at`` and the optimistic-lock ``version`` included -- exactly as it went in.
    """
    db_path = tmp_path / "mig117_forward_only.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all built the post-117 shape, so reach the PRE-117 shape by stamping the
        # parent, marking 117 applied, and running ITS downgrade -- a bare `downgrade -1`
        # from 116 would run 116's downgrade, a different migration.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_117)
        _alembic(db_url, "downgrade", "-1")
        assert _column(engine, TABLE, COLUMN) is None

        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO parts (id, part_number, name, part_type, unit_of_measure, company_id) "
                    "VALUES (1, 'PRE-117-PART', 'Bracket', 'manufactured', 'each', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_centers (id, code, name, work_center_type, company_id) "
                    "VALUES (1, 'WC-117', 'Brake', 'fabrication', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders "
                    "(id, work_order_number, work_order_type, part_id, quantity_ordered, status, company_id, version) "
                    "VALUES (1, 'PRE-117-WO', 'production', 1, 10, 'IN_PROGRESS', 1, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_order_operations "
                    "(id, work_order_id, work_center_id, sequence, operation_number, name, status, "
                    "quantity_complete, quantity_scrapped, scrap_reason, updated_at, company_id, version) "
                    "VALUES (1, 1, 1, 10, '10', 'Bend', 'COMPLETE', 8, 2, 'cracked on the bend', "
                    "'2026-10-01 08:15:00.000000', 1, 4)"
                )
            )

        _alembic(db_url, "upgrade", REVISION_117)

        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {COLUMN}, status, quantity_complete, quantity_scrapped, scrap_reason, updated_at, "
                    "version FROM work_order_operations WHERE id = 1"
                )
            ).one()
        assert row[0] is None, "a pre-117 operation must come out with NO declared short"
        assert row[1] == "COMPLETE"
        assert row[2] == 8
        assert row[3] == 2
        assert row[4] == "cracked on the bend"
        assert row[5] == "2026-10-01 08:15:00.000000", "the migration must not bump updated_at"
        assert row[6] == 4, "the migration must not bump the optimistic-lock version"
    finally:
        engine.dispose()
