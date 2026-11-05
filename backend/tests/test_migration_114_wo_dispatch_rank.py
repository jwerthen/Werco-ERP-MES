"""Coverage for 114_wo_dispatch_rank (Dispatch Board v2: the planner's work-order order).

114 is purely additive over two EXISTING tables -- three objects, no data:

- ``work_orders.dispatch_rank``  Integer, NULLABLE, no server default
- ``ix_work_orders_company_dispatch_rank``  ``(company_id, dispatch_rank)``, non-unique
- ``ix_operational_events_company_entity_time``
  ``(company_id, entity_type, entity_id, occurred_at)``, non-unique

This file covers the MIGRATION, in the layers the suite already uses (…068, …083, …088):

1. **Script wiring** (unit) -- a single alembic head, the 113 -> 114 chain, an id that
   fits ``alembic_version``'s varchar(32).
2. **What the functions EMIT** (unit) -- ``upgrade()`` / ``downgrade()`` run against a
   recording stand-in for ``op`` with every guard stubbed both ways (088's idiom), so
   "each object is guarded independently", "the bootstrapped database is a clean no-op",
   "an INVALID Postgres index is rebuilt" and "the downgrade drops indexes before the
   column" are proven by execution, not by grepping source.
3. **Source invariants** (unit) -- pure DDL, no table created (so no RLS statement), no
   ``audit_log`` contact.
4. **Model / migration lock-step** (unit) -- the models declare exactly the objects the
   migration builds (the stamped-over hazard), asserted by DIALECT-COMPILING the DDL for
   PostgreSQL and SQLite rather than trusting SQLite's execution (CLAUDE.md -> "Why the
   tests run on SQLite"); and ``dispatch_rank`` is on no request schema.
5. **A real upgrade -> downgrade -> upgrade round-trip** (integration/slow) over a
   disposable SQLite file bootstrapped create_all -> stamp(113), plus the data claim:
   a pre-114 work order comes out with ``dispatch_rank IS NULL`` and everything else
   byte-identical, ``updated_at`` included.

THE THINGS A FUTURE READER WILL TRY TO "FIX"
--------------------------------------------
* **No UNIQUE constraint** on ``(company_id, dispatch_rank)``: the plan is rewritten
  wholesale in one transaction, so transient duplicates exist mid-rewrite, and the value
  is advisory.
* **No backfill.** NULL ("not placed in the plan by a planner") is the true state of every
  pre-existing work order; inventing ranks would assert a dispatch decision nobody made.
* **``ix_operational_events_company_entity`` is not dropped** even though the new index
  has it as a prefix. Dropping an index is its own decision, not a side effect.
"""

import importlib.util
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_114 = "114_wo_dispatch_rank"
MIGRATION_FILE = "114_wo_dispatch_rank.py"
DOWN_REVISION = "113_data_api_revoke_parity"

WO_TABLE = "work_orders"
COLUMN = "dispatch_rank"
WO_INDEX = "ix_work_orders_company_dispatch_rank"
WO_INDEX_COLUMNS = ["company_id", "dispatch_rank"]

EVENTS_TABLE = "operational_events"
EVENTS_INDEX = "ix_operational_events_company_entity_time"
EVENTS_INDEX_COLUMNS = ["company_id", "entity_type", "entity_id", "occurred_at"]
EVENTS_PREFIX_INDEX = "ix_operational_events_company_entity"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_114", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose names at length every construct the migration deliberately avoids (no
    backfill, no UNIQUE, no RLS statement), so assertions about ABSENT constructs have to
    look at code only or the docstring itself would fail them.
    """
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# Recording the DDL the migration actually emits (088's idiom)
# ---------------------------------------------------------------------------


class _RecordedDDL:
    """A stand-in for alembic's ``op`` that records calls instead of executing them.

    ``index_validity`` is what the fake Postgres catalog answers for the
    ``pg_index.indisvalid`` probe, per index name (absent key = valid).
    """

    def __init__(self, dialect_name: str, index_validity: dict = None):
        self.dialect_name = dialect_name
        self.index_validity = index_validity or {}
        self.calls: list = []
        self.probes: list = []

    def get_bind(self):
        def execute(statement, params=None):
            self.probes.append((str(statement), dict(params or {})))
            valid = self.index_validity.get((params or {}).get("index_name"), True)
            return SimpleNamespace(fetchone=lambda: (valid,))

        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect_name), execute=execute)

    def add_column(self, table_name, column, **kwargs):
        self.calls.append(("add_column", table_name, column))

    def drop_column(self, table_name, column_name, **kwargs):
        self.calls.append(("drop_column", table_name, column_name))

    def create_index(self, index_name, table_name, columns, **kwargs):
        self.calls.append(("create_index", index_name, table_name, list(columns), kwargs))

    def drop_index(self, index_name, **kwargs):
        self.calls.append(("drop_index", index_name, kwargs.get("table_name")))

    @contextmanager
    def batch_alter_table(self, table_name, **kwargs):
        recorder = self

        class _Batch:
            def drop_column(self, column_name, **kw):
                recorder.calls.append(("batch_drop_column", table_name, column_name))

        yield _Batch()

    def execute(self, statement):  # pragma: no cover - must never be reached
        self.calls.append(("execute", str(statement)))

    def bulk_insert(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.calls.append(("bulk_insert",))

    @property
    def kinds(self) -> list:
        return [call[0] for call in self.calls]


def _record(
    function_name: str,
    *,
    dialect: str = "postgresql",
    has_column: bool = False,
    has_indexes=False,
    index_validity: dict = None,
) -> _RecordedDDL:
    """Run ``upgrade()`` / ``downgrade()`` against a recorder instead of a database.

    ``has_indexes`` is a bool (every index) or a set of index names that exist. The
    real ``_index_state`` runs, so the Postgres validity probe is exercised too.
    """
    module = _load_module()
    recorder = _RecordedDDL(dialect, index_validity)
    module.op = recorder
    module._has_table = lambda _table: True
    module._has_column = lambda _table, _column: has_column
    if isinstance(has_indexes, bool):
        module._has_index = lambda _table, _index: has_indexes
    else:
        module._has_index = lambda _table, index: index in has_indexes
    getattr(module, function_name)()
    return recorder


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_114)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped DB has alembic_version.version_num varchar(32): the
    # create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_114) <= 32


@pytest.mark.unit
def test_module_loads_and_names_exactly_the_objects_it_owns():
    module = _load_module()
    assert module.revision == REVISION_114
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert module.TABLE_NAME == WO_TABLE
    assert module.COLUMN_NAME == COLUMN
    assert module.INDEXES == [
        (WO_TABLE, WO_INDEX, WO_INDEX_COLUMNS),
        (EVENTS_TABLE, EVENTS_INDEX, EVENTS_INDEX_COLUMNS),
    ]


# ---------------------------------------------------------------------------
# 2. What upgrade()/downgrade() emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_a_virgin_database_gets_the_column_then_both_indexes(dialect: str):
    recorder = _record("upgrade", dialect=dialect, has_column=False, has_indexes=False)

    assert recorder.kinds == ["add_column", "create_index", "create_index"]
    _kind, table, column = recorder.calls[0]
    assert table == WO_TABLE
    assert column.name == COLUMN
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is True
    assert column.server_default is None and column.default is None, "no default, no backfill"

    built = {call[1]: (call[2], call[3], call[4]) for call in recorder.calls if call[0] == "create_index"}
    assert built == {
        WO_INDEX: (WO_TABLE, WO_INDEX_COLUMNS, {"unique": False}),
        EVENTS_INDEX: (EVENTS_TABLE, EVENTS_INDEX_COLUMNS, {"unique": False}),
    }
    # The work-order index names the new column, so it is built AFTER it.
    assert recorder.calls[1][1] == WO_INDEX


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_the_bootstrapped_database_is_a_clean_no_op(dialect: str):
    """create_all already built all three objects (valid): every guard fires."""
    recorder = _record("upgrade", dialect=dialect, has_column=True, has_indexes=True)
    assert recorder.calls == []


@pytest.mark.unit
def test_each_object_is_guarded_independently():
    """A hand-patched database holding SOME of the objects gets exactly the missing ones."""
    only_column = _record("upgrade", has_column=True, has_indexes=False)
    assert only_column.kinds == ["create_index", "create_index"]

    only_events_index = _record("upgrade", has_column=True, has_indexes={EVENTS_INDEX})
    assert [(c[0], c[1]) for c in only_events_index.calls] == [("create_index", WO_INDEX)]

    only_wo_index = _record("upgrade", has_column=True, has_indexes={WO_INDEX})
    assert [(c[0], c[1]) for c in only_wo_index.calls] == [("create_index", EVENTS_INDEX)]

    indexes_without_column = _record("upgrade", has_column=False, has_indexes=True)
    assert indexes_without_column.kinds == ["add_column"]


@pytest.mark.unit
def test_an_invalid_postgres_index_is_dropped_and_rebuilt():
    """An interrupted out-of-band ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index the
    inspector still reports. A name-only guard would skip it forever; the validity probe
    rebuilds it -- and leaves the VALID sibling alone."""
    recorder = _record(
        "upgrade",
        dialect="postgresql",
        has_column=True,
        has_indexes=True,
        index_validity={EVENTS_INDEX: False},
    )
    assert [(c[0], c[1]) for c in recorder.calls] == [
        ("drop_index", EVENTS_INDEX),
        ("create_index", EVENTS_INDEX),
    ]
    assert recorder.calls[0][2] == EVENTS_TABLE
    # The probe is a catalog READ, scoped to the index AND its table.
    assert recorder.probes, "the validity probe must run on PostgreSQL"
    for sql, params in recorder.probes:
        assert sql.lstrip().upper().startswith("SELECT")
        assert "indisvalid" in sql
        for keyword in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
            assert keyword not in sql.upper()
    assert {p["index_name"] for _sql, p in recorder.probes} == {WO_INDEX, EVENTS_INDEX}
    assert {(p["index_name"], p["table_name"]) for _sql, p in recorder.probes} == {
        (WO_INDEX, WO_TABLE),
        (EVENTS_INDEX, EVENTS_TABLE),
    }


@pytest.mark.unit
def test_the_validity_probe_never_runs_on_sqlite():
    recorder = _record("upgrade", dialect="sqlite", has_column=True, has_indexes=True)
    assert recorder.probes == []
    assert recorder.calls == []


@pytest.mark.unit
def test_postgres_downgrade_drops_both_indexes_then_the_column():
    recorder = _record("downgrade", dialect="postgresql", has_column=True, has_indexes=True)
    assert [(c[0], c[1]) for c in recorder.calls] == [
        ("drop_index", WO_INDEX),
        ("drop_index", EVENTS_INDEX),
        ("drop_column", WO_TABLE),
    ]
    assert recorder.calls[0][2] == WO_TABLE
    assert recorder.calls[1][2] == EVENTS_TABLE
    assert recorder.calls[2][2] == COLUMN


@pytest.mark.unit
def test_sqlite_downgrade_drops_the_column_in_batch_mode():
    """Batch mode recreates the table without the column (precedent 063/064/065/083)."""
    recorder = _record("downgrade", dialect="sqlite", has_column=True, has_indexes=True)
    assert [(c[0], c[1]) for c in recorder.calls] == [
        ("drop_index", WO_INDEX),
        ("drop_index", EVENTS_INDEX),
        ("batch_drop_column", WO_TABLE),
    ]
    assert recorder.calls[2][2] == COLUMN


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_downgrade_over_an_already_downgraded_database_is_a_no_op(dialect: str):
    recorder = _record("downgrade", dialect=dialect, has_column=False, has_indexes=False)
    assert recorder.calls == []


@pytest.mark.unit
def test_downgrade_is_not_a_stub():
    source = _source()
    assert "def downgrade() -> None:" in source
    assert "    pass" not in source


# ---------------------------------------------------------------------------
# 3. Source invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pure_ddl_no_backfill_and_no_data_statement():
    """Every pre-existing work order is legitimately NULL (not in the plan), so the
    migration emits no data statement at all -- which also makes it structurally
    incapable of touching the tamper-evident audit_log hash chain. The one ``sa.text``
    is the read-only ``pg_index`` validity probe, asserted above."""
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "conn.execute", "UPDATE ", "INSERT ", "DELETE "):
        assert forbidden not in body, f"114 must be pure DDL; found {forbidden!r}"
    assert "audit_log" not in body


@pytest.mark.unit
def test_no_table_is_created_so_no_rls_statement_is_needed():
    """Both tables exist and 059 already enabled RLS on them; the ENABLE-RLS convention is
    for NEW tables (the fresh-bootstrap gate keys off op.create_table)."""
    body = _body()
    assert "op.create_table" not in body
    assert "ROW LEVEL SECURITY" not in body
    assert "REVOKE" not in body


@pytest.mark.unit
def test_the_prefix_index_is_left_alone():
    """``ix_operational_events_company_entity`` is the new index's prefix; 114 must not
    drop it -- not on upgrade, and not on downgrade either."""
    assert EVENTS_PREFIX_INDEX + '"' not in _body()
    for function_name in ("upgrade", "downgrade"):
        recorder = _record(function_name, has_column=True, has_indexes=True)
        assert EVENTS_PREFIX_INDEX not in {call[1] for call in recorder.calls}


# ---------------------------------------------------------------------------
# 4. Model / migration lock-step
# ---------------------------------------------------------------------------


def _model_index(table: sa.Table, name: str) -> sa.Index:
    [index] = [ix for ix in table.indexes if ix.name == name]
    return index


@pytest.mark.unit
def test_model_declares_a_nullable_unindexed_integer_with_no_default():
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.Integer)
    assert col.nullable is True, "dispatch_rank must be nullable -- NULL means not in the plan"
    assert not col.index, "the index is the NAMED tenant-led one in __table_args__, not index=True"
    assert col.unique is not True, "the plan is rewritten wholesale; ranks are advisory"
    assert col.default is None and col.server_default is None, "no default, no backfill"


@pytest.mark.unit
def test_dispatch_rank_is_distinct_from_run_order_and_priority():
    from app.models.work_order import WorkOrder, WorkOrderOperation

    assert COLUMN not in WorkOrderOperation.__table__.columns
    assert "run_order" in WorkOrderOperation.__table__.columns
    assert "run_order" not in WorkOrder.__table__.columns
    assert "priority" in WorkOrder.__table__.columns


@pytest.mark.unit
@pytest.mark.parametrize(
    "model_path, table_name, index_name, columns",
    [
        ("app.models.work_order:WorkOrder", WO_TABLE, WO_INDEX, WO_INDEX_COLUMNS),
        ("app.models.operational_event:OperationalEvent", EVENTS_TABLE, EVENTS_INDEX, EVENTS_INDEX_COLUMNS),
    ],
)
def test_each_index_is_declared_on_its_model_exactly_as_the_migration_builds_it(
    model_path: str, table_name: str, index_name: str, columns: list
):
    """create_all and the migration must converge on ONE object per name -- the
    stamped-over hazard (CLAUDE.md -> Migrations) is an index declared only here."""
    module_name, class_name = model_path.split(":")
    model = getattr(importlib.import_module(module_name), class_name)
    table = model.__table__
    assert table.name == table_name

    index = _model_index(table, index_name)
    assert [c.name for c in index.columns] == columns
    assert index.unique is False
    assert index.dialect_options["postgresql"].get("where") is None, "not partial"
    assert index.dialect_options["sqlite"].get("where") is None, "not partial"

    # And the migration lists it with the very same columns.
    migration_indexes = {name: (tbl, cols) for tbl, name, cols in _load_module().INDEXES}
    assert migration_indexes[index_name] == (table_name, columns)


@pytest.mark.unit
def test_no_other_model_index_covers_dispatch_rank():
    """Exactly one index mentions the column, under the migration's name -- so a stray
    ``index=True`` (which would add an unmigrated ``ix_work_orders_dispatch_rank``) fails."""
    from app.models.work_order import WorkOrder

    covering = {ix.name for ix in WorkOrder.__table__.indexes if COLUMN in [c.name for c in ix.columns]}
    assert covering == {WO_INDEX}


@pytest.mark.unit
def test_the_prefix_index_is_still_declared_on_the_model():
    from app.models.operational_event import OperationalEvent

    prefix = _model_index(OperationalEvent.__table__, EVENTS_PREFIX_INDEX)
    assert [c.name for c in prefix.columns] == EVENTS_INDEX_COLUMNS[:3]


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_column_ddl_compiles_nullable_with_no_default_on_both_engines(dialect_name: str):
    """DIALECT-COMPILED, not executed: a nullable INTEGER with no DEFAULT clause is the
    shape that makes the ADD COLUMN metadata-only on PostgreSQL (no rewrite of every
    work order in the shop)."""
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.work_order import WorkOrder

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    rendered = str(sa.schema.CreateColumn(WorkOrder.__table__.columns[COLUMN]).compile(dialect=dialect)).upper()
    assert rendered.split() == ["DISPATCH_RANK", "INTEGER"]


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
@pytest.mark.parametrize(
    "model_path, index_name, columns",
    [
        ("app.models.work_order:WorkOrder", WO_INDEX, WO_INDEX_COLUMNS),
        ("app.models.operational_event:OperationalEvent", EVENTS_INDEX, EVENTS_INDEX_COLUMNS),
    ],
)
def test_each_index_compiles_as_a_plain_non_unique_composite_index_on_both_engines(
    dialect_name: str, model_path: str, index_name: str, columns: list
):
    from sqlalchemy.dialects import postgresql, sqlite

    module_name, class_name = model_path.split(":")
    model = getattr(importlib.import_module(module_name), class_name)
    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    rendered = str(sa.schema.CreateIndex(_model_index(model.__table__, index_name)).compile(dialect=dialect))

    assert rendered.upper().startswith("CREATE INDEX")
    assert "UNIQUE" not in rendered.upper()
    assert " WHERE " not in rendered.upper()
    assert index_name in rendered
    assert f"({', '.join(columns)})" in rendered


@pytest.mark.unit
def test_dispatch_rank_is_on_no_request_schema():
    """The rank is written ONLY by the dispatch-board order verb (lock + CAS + one audit
    row). On ``WorkOrderUpdate`` -- whose PUT blind-``setattr``s every field -- it would be
    a second, unlocked, un-noticed writer of the plan; on a create/duplicate request it
    would let a new job place itself in the plan."""
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

    # Named explicitly too, so a rename of the suffix convention cannot hollow the scan
    # out. WorkOrderCreate's fields include everything it inherits from WorkOrderBase.
    from app.schemas.work_order import WorkOrderCreate, WorkOrderDuplicateRequest, WorkOrderUpdate

    for schema in (WorkOrderCreate, WorkOrderUpdate, WorkOrderDuplicateRequest):
        assert COLUMN not in schema.model_fields


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


def _has_column(engine, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(engine).get_columns(table))


def _index_columns(engine, table: str) -> dict:
    return {ix["name"]: ix["column_names"] for ix in sa.inspect(engine).get_indexes(table)}


def _assert_all_three_present(engine) -> None:
    assert _has_column(engine, WO_TABLE, COLUMN)
    assert _index_columns(engine, WO_TABLE).get(WO_INDEX) == WO_INDEX_COLUMNS
    assert _index_columns(engine, EVENTS_TABLE).get(EVENTS_INDEX) == EVENTS_INDEX_COLUMNS


def _work_order_neighbours(engine) -> tuple:
    """Everything on ``work_orders`` OTHER than the 114 objects -- what batch mode's table
    rebuild must carry across untouched."""
    inspector = sa.inspect(engine)
    indexes = {name: cols for name, cols in _index_columns(engine, WO_TABLE).items() if name != WO_INDEX}
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(WO_TABLE)}
    fks = sorted(
        (fk.get("name") or "", tuple(fk["constrained_columns"]), fk["referred_table"])
        for fk in inspector.get_foreign_keys(WO_TABLE)
    )
    uniques = sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(WO_TABLE))
    columns = {c["name"] for c in inspector.get_columns(WO_TABLE)} - {COLUMN}
    return columns, indexes, checks, fks, uniques


@pytest.mark.integration
@pytest.mark.slow
def test_migration_114_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig114.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-114 shape straight from the models.
        _assert_all_three_present(engine)
        neighbours = _work_order_neighbours(engine)

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: every guard fires -> clean no-op.
        _alembic(db_url, "upgrade", REVISION_114)
        _assert_all_three_present(engine)
        assert _work_order_neighbours(engine) == neighbours

        # 2. Downgrade: a REAL drop of all three (the DDL is dialect-neutral).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, WO_TABLE, COLUMN)
        assert WO_INDEX not in _index_columns(engine, WO_TABLE)
        assert EVENTS_INDEX not in _index_columns(engine, EVENTS_TABLE)
        # The batch-mode rebuild of work_orders carried every other column, index, named
        # CHECK, FK and unique constraint across untouched ...
        assert _work_order_neighbours(engine) == neighbours
        # ... and the prefix index on operational_events was never touched.
        assert _index_columns(engine, EVENTS_TABLE).get(EVENTS_PREFIX_INDEX) == EVENTS_INDEX_COLUMNS[:3]

        # 3. Re-upgrade: all three come back (the un-guarded ADD COLUMN / CREATE INDEX).
        _alembic(db_url, "upgrade", REVISION_114)
        _assert_all_three_present(engine)
        assert _work_order_neighbours(engine) == neighbours

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping: stamp back
        #    and run upgrade() again over a DB that already has all three.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_114)
        _assert_all_three_present(engine)
        assert _work_order_neighbours(engine) == neighbours
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_pre_existing_rows_survive_the_upgrade_unranked_and_untouched(tmp_path):
    """The data claim, executed rather than read off the source.

    A work order that exists BEFORE the column does must come out of the upgrade with
    ``dispatch_rank IS NULL`` (not in the plan) and every other field -- ``updated_at``
    and ``version`` included, the OTD/yield anchors and the optimistic lock -- exactly as
    it went in. An event row likewise survives its table gaining an index.
    """
    db_path = tmp_path / "mig114_forward_only.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all built the post-114 shape, so reach the PRE-114 shape by stamping the
        # parent, marking 114 applied, and running ITS downgrade.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_114)
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, WO_TABLE, COLUMN)

        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO parts (id, part_number, name, part_type, unit_of_measure, company_id) "
                    "VALUES (1, 'PRE-114-PART', 'Bracket', 'manufactured', 'each', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders "
                    "(work_order_number, work_order_type, part_id, quantity_ordered, status, priority, "
                    "updated_at, company_id, version) "
                    "VALUES ('PRE-114-WO', 'production', 1, 4, 'released', 3, "
                    "'2026-10-01 08:15:00.000000', 1, 7)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO operational_events "
                    "(event_type, source_module, entity_type, entity_id, severity, occurred_at, created_at, "
                    "company_id) "
                    "VALUES ('operation_ready', 'shop_floor', 'work_center', 9, 'info', "
                    "'2026-10-01 08:15:00', '2026-10-01 08:15:00', 1)"
                )
            )

        _alembic(db_url, "upgrade", REVISION_114)

        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {COLUMN}, status, priority, updated_at, version FROM work_orders "
                    "WHERE work_order_number = 'PRE-114-WO'"
                )
            ).one()
            events = conn.execute(
                sa.text("SELECT count(*) FROM operational_events WHERE entity_type = 'work_center' AND entity_id = 9")
            ).scalar()
        assert row[0] is None, "a pre-114 work order must come out NOT in the plan"
        assert row[1] == "released"
        assert row[2] == 3
        assert row[3] == "2026-10-01 08:15:00.000000", "the migration must not bump updated_at"
        assert row[4] == 7, "the migration must not bump the optimistic-lock version"
        assert events == 1
    finally:
        engine.dispose()
