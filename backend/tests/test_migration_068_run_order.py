"""Coverage for 068_operation_run_order (manual dispatch rank per work center).

068 is a single additive change over an existing table: a nullable, indexed
``run_order`` Integer on ``work_order_operations``, with the non-unique index
``ix_work_order_operations_run_order``.

Two complementary layers, mirroring the migration-test idioms already in the
suite (tests/test_migration_064_visitor_entered_by.py,
tests/test_migration_067_standalone_laser.py):

1. Script wiring + source/model lock-step (unit) — single alembic head, the
   067->068 chain, the id fits alembic_version's varchar(32), the ADD COLUMN is
   guarded by ``_has_column`` and the index by COVERED COLUMN (so the
   create_all -> stamp -> upgrade bootstrap path no-ops instead of erroring),
   the downgrade is real (drops the index by reflected name, then the column,
   batch-mode on SQLite), no table is created (so the RLS new-table convention
   does not apply), and the model declares exactly the column the migration
   builds.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) — the
   alembic CLI over a disposable SQLite file bootstrapped create_all ->
   stamp(067). Unlike 066/067 the DDL here is dialect-neutral, so SQLite
   exercises it for real: the column + index actually disappear on downgrade and
   come back on re-upgrade. The round-trip also re-runs ``upgrade()`` over a DB
   that ALREADY has the column and index (stamp back to 067, upgrade again) to
   prove the guards make it idempotent rather than relying on alembic's version
   bookkeeping to skip it.

Semantics pinned here so a future reader does not conflate the two ordering
columns: ``sequence`` is routing-step precedence WITHIN one work order and gates
predecessors; ``run_order`` is a cross-work-order, ADVISORY manual rank within a
work center and gates nothing. NULL means unranked.
"""

import importlib.util
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_068 = "068_operation_run_order"
MIGRATION_FILE = "068_operation_run_order.py"
DOWN_REVISION = "067_standalone_laser_nest_wo"

TABLE = "work_order_operations"
COLUMN = "run_order"
INDEX_NAME = "ix_work_order_operations_run_order"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_068", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped -- the prose explains what the
    migration deliberately does NOT do, so assertions about absent constructs
    have to look at code only."""
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_068)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_068) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_068
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Constants describe exactly the objects this migration owns.
    assert module.TABLE_NAME == TABLE
    assert module.COLUMN_NAME == COLUMN
    assert module.INDEX_NAME == INDEX_NAME


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency + a real, guarded downgrade)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upgrade_is_guarded_for_both_column_and_index():
    """Safe to re-run, and a clean no-op on the create_all -> stamp -> upgrade
    bootstrap path where the model's index=True already built both objects."""
    module = _load_module()
    assert callable(module._has_column)
    assert callable(module._index_names_on_column)

    source = _source()
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in source
    assert "if not _index_names_on_column(TABLE_NAME, COLUMN_NAME):" in source
    # The index guard is by COVERED COLUMN, not by name, so a differently-named
    # pre-existing index on the column is still recognised (precedent 064/065).
    assert 'ix.get("column_names") == [column_name]' in source


@pytest.mark.unit
def test_downgrade_is_real_and_guarded():
    """Not a `pass` stub: index dropped by REFLECTED name first (SQLite cannot
    drop an indexed column), then the column, guarded, batch-mode on SQLite."""
    source = _source()
    assert "def downgrade() -> None:" in source
    assert "op.drop_index(actual_index_name, table_name=TABLE_NAME)" in source
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):\n        return" in source
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in source
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in source
    # A downgrade that silently did nothing would be a stub in disguise.
    assert "    pass" not in source


@pytest.mark.unit
def test_no_backfill_and_no_raw_dml():
    """Every pre-existing row is legitimately NULL (unranked), so the migration
    emits pure DDL -- no data statements at all, which also makes it structurally
    incapable of touching the tamper-evident audit_log hash chain."""
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "sa.text(", "conn.execute"):
        assert forbidden not in body, f"068 must be pure DDL; found {forbidden}"


@pytest.mark.unit
def test_column_add_only_so_no_rls_needed():
    """068 creates no table, so the ENABLE ROW LEVEL SECURITY new-table
    convention does not apply (the 059-gate test keys off op.create_table)."""
    assert "op.create_table" not in _body()


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same objects)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_nullable_indexed_run_order():
    from app.models.work_order import WorkOrderOperation

    col = WorkOrderOperation.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.Integer)
    assert col.nullable is True, "run_order must be nullable -- NULL means unranked"
    assert col.index is True
    assert col.unique is not True, "run_order is advisory; ranks are rewritten wholesale"
    assert col.default is None and col.server_default is None, "no backfill/default"


@pytest.mark.unit
def test_model_index_name_matches_the_migration():
    """create_all (via index=True) and the migration must converge on ONE index."""
    from app.models.work_order import WorkOrderOperation

    names = {ix.name for ix in WorkOrderOperation.__table__.indexes if [c.name for c in ix.columns] == [COLUMN]}
    assert names == {INDEX_NAME}, f"expected {INDEX_NAME}, found {sorted(names)}"


@pytest.mark.unit
def test_run_order_is_distinct_from_sequence():
    """``sequence`` stays required routing precedence within a work order;
    ``run_order`` is the optional cross-work-order dispatch rank."""
    from app.models.work_order import WorkOrderOperation

    assert WorkOrderOperation.__table__.columns["sequence"].nullable is False
    assert WorkOrderOperation.__table__.columns[COLUMN].nullable is True


# ---------------------------------------------------------------------------
# 4. Real round-trip: upgrade -> downgrade -> upgrade over a bootstrapped DB
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


def _index_names_on_column(engine, table: str, column: str) -> list:
    return [ix["name"] for ix in sa.inspect(engine).get_indexes(table) if ix.get("column_names") == [column]]


@pytest.mark.integration
@pytest.mark.slow
def test_migration_068_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig068.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-068 shape straight from the model.
        assert _has_column(engine, TABLE, COLUMN), "create_all did not build run_order"
        assert INDEX_NAME in _index_names_on_column(engine, TABLE, COLUMN)

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: both guards fire, so this is
        #    a clean no-op and the objects survive untouched.
        _alembic(db_url, "upgrade", REVISION_068)
        assert _has_column(engine, TABLE, COLUMN)
        assert INDEX_NAME in _index_names_on_column(engine, TABLE, COLUMN)

        # 2. Downgrade: a REAL drop here (the DDL is dialect-neutral, so SQLite
        #    exercises it) -- index first, then the column via batch mode.
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)
        assert _index_names_on_column(engine, TABLE, COLUMN) == []
        # Batch mode rebuilds the table -- the neighbouring schema must survive.
        remaining = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
        assert {"id", "work_order_id", "work_center_id", "sequence", "company_id"} <= remaining
        assert "ix_woo_work_order_sequence" in {ix["name"] for ix in sa.inspect(engine).get_indexes(TABLE)}

        # 3. Re-upgrade: the column and index come back (the un-guarded path).
        _alembic(db_url, "upgrade", REVISION_068)
        assert _has_column(engine, TABLE, COLUMN)
        assert INDEX_NAME in _index_names_on_column(engine, TABLE, COLUMN)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping:
        #    stamp back and run upgrade() again over a DB that already has both
        #    objects. The guards must make it a no-op instead of erroring.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_068)
        assert _has_column(engine, TABLE, COLUMN)
        assert _index_names_on_column(engine, TABLE, COLUMN) == [INDEX_NAME]
    finally:
        engine.dispose()
