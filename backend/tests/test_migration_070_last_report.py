"""Coverage for 070_operation_last_report (kiosk Foundry last-report telemetry).

070 is a purely additive change over an existing table: THREE nullable,
un-indexed columns on ``work_order_operations`` --

- ``last_reported_at``       DateTime (naive UTC, like the sibling timestamps)
- ``last_reported_good``     Float
- ``last_reported_scrapped`` Float

recording the most recent production report's DELTAS (not running totals);
NULL means "no report recorded yet" (correct-forward, no backfill).

Two complementary layers, mirroring the migration-test idioms already in the
suite (tests/test_migration_064_visitor_entered_by.py,
tests/test_migration_068_run_order.py):

1. Script wiring + source/model lock-step (unit) -- single alembic head, the
   069->070 chain, the id fits alembic_version's varchar(32), every ADD COLUMN
   is guarded by ``_has_column`` (so the create_all -> stamp -> upgrade
   bootstrap path no-ops instead of erroring), the downgrade is real (drops
   exactly the three owned columns, batch-mode on SQLite), no table is created
   (so the RLS new-table convention does not apply), no index is built, and the
   model declares exactly the columns the migration adds.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) -- the
   alembic CLI over a disposable SQLite file bootstrapped create_all ->
   stamp(069). The DDL is dialect-neutral, so SQLite exercises it for real: the
   columns actually disappear on downgrade and come back on re-upgrade. The
   round-trip also re-runs ``upgrade()`` over a DB that ALREADY has the columns
   (stamp back to 069, upgrade again) to prove the guards make it idempotent
   rather than relying on alembic's version bookkeeping to skip it.

Semantics pinned here so a future reader does not conflate the columns with the
quantity model: ``quantity_complete`` / ``quantity_scrapped`` stay the running
totals under the monotonic-up / evidence-floor reconcile model;
``last_reported_*`` is display telemetry for the kiosk's "LAST REPORT" chip and
gates nothing.
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

REVISION_070 = "070_operation_last_report"
MIGRATION_FILE = "070_operation_last_report.py"
DOWN_REVISION = "069_work_order_version_guard"

TABLE = "work_order_operations"
COLUMNS = ("last_reported_at", "last_reported_good", "last_reported_scrapped")
COLUMN_TYPES = {
    "last_reported_at": sa.DateTime,
    "last_reported_good": sa.Float,
    "last_reported_scrapped": sa.Float,
}


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_070", path)
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

    revision = scripts.get_revision(REVISION_070)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_070) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_070
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Constants describe exactly the objects this migration owns.
    assert module.TABLE_NAME == TABLE
    assert tuple(name for name, _ in module.COLUMNS) == COLUMNS
    for name, type_factory in module.COLUMNS:
        assert type_factory is COLUMN_TYPES[name], f"{name} must be {COLUMN_TYPES[name].__name__}"


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency + a real, guarded downgrade)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upgrade_is_guarded_per_column():
    """Safe to re-run, and a clean no-op on the create_all -> stamp -> upgrade
    bootstrap path where the ORM mapping already built the columns."""
    module = _load_module()
    assert callable(module._has_column)

    source = _source()
    assert "if not _has_column(TABLE_NAME, column_name):" in source
    # Nullable, no default -> metadata-only ADD COLUMN on PostgreSQL.
    assert "nullable=True" in source
    assert "server_default" not in _body(), "070 must not add a server_default (no rewrite, no backfill)"


@pytest.mark.unit
def test_downgrade_is_real_and_guarded():
    """Not a `pass` stub: drops exactly the columns this revision owns, each
    guarded by _has_column, batch-mode on SQLite."""
    source = _source()
    assert "def downgrade() -> None:" in source
    assert "present = [name for name, _ in COLUMNS if _has_column(TABLE_NAME, name)]" in source
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in source
    assert "batch_op.drop_column(column_name)" in source
    assert "op.drop_column(TABLE_NAME, column_name)" in source
    # A downgrade that silently did nothing would be a stub in disguise.
    assert "    pass" not in source


@pytest.mark.unit
def test_no_backfill_and_no_raw_dml():
    """Every pre-existing row is legitimately NULL (no report recorded yet), so
    the migration emits pure DDL -- no data statements at all, which also makes
    it structurally incapable of touching the tamper-evident audit_log hash
    chain."""
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "sa.text(", "conn.execute"):
        assert forbidden not in body, f"070 must be pure DDL; found {forbidden}"


@pytest.mark.unit
def test_column_add_only_so_no_rls_needed():
    """070 creates no table, so the ENABLE ROW LEVEL SECURITY new-table
    convention does not apply (the 059-gate test keys off op.create_table)."""
    assert "op.create_table" not in _body()


@pytest.mark.unit
def test_no_index_is_built():
    """The columns are read row-wise off already-selected operations, never
    filtered or sorted on -- deliberately un-indexed (unlike 068's run_order)."""
    assert "op.create_index" not in _body()


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same objects)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_three_nullable_unindexed_columns():
    from app.models.work_order import WorkOrderOperation

    for name in COLUMNS:
        col = WorkOrderOperation.__table__.columns[name]
        assert isinstance(col.type, COLUMN_TYPES[name]), f"{name} must be {COLUMN_TYPES[name].__name__}"
        assert col.nullable is True, f"{name} must be nullable -- NULL means no report yet"
        assert col.index is not True, f"{name} is deliberately un-indexed"
        assert col.unique is not True
        assert col.default is None and col.server_default is None, f"{name}: no backfill/default"


@pytest.mark.unit
def test_last_report_is_distinct_from_running_totals():
    """``quantity_complete`` / ``quantity_scrapped`` stay the running totals of
    the monotonic-up quantity model; ``last_reported_*`` is per-report delta
    telemetry and must not replace them."""
    from app.models.work_order import WorkOrderOperation

    cols = WorkOrderOperation.__table__.columns
    assert "quantity_complete" in cols
    assert "quantity_scrapped" in cols
    for name in COLUMNS:
        assert name in cols


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


@pytest.mark.integration
@pytest.mark.slow
def test_migration_070_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig070.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-070 shape straight from the model.
        for name in COLUMNS:
            assert _has_column(engine, TABLE, name), f"create_all did not build {name}"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: every guard fires, so this is
        #    a clean no-op and the columns survive untouched.
        _alembic(db_url, "upgrade", REVISION_070)
        for name in COLUMNS:
            assert _has_column(engine, TABLE, name)

        # 2. Downgrade: a REAL drop here (the DDL is dialect-neutral, so SQLite
        #    exercises it) -- all three columns removed via one batch rebuild.
        _alembic(db_url, "downgrade", "-1")
        for name in COLUMNS:
            assert not _has_column(engine, TABLE, name)
        # Batch mode rebuilds the table -- the neighbouring schema must survive.
        remaining = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
        assert {"id", "work_order_id", "work_center_id", "sequence", "run_order", "company_id"} <= remaining
        surviving_indexes = {ix["name"] for ix in sa.inspect(engine).get_indexes(TABLE)}
        assert "ix_woo_work_order_sequence" in surviving_indexes
        assert "ix_work_order_operations_run_order" in surviving_indexes

        # 3. Re-upgrade: the columns come back (the un-guarded ADD COLUMN path).
        _alembic(db_url, "upgrade", REVISION_070)
        for name in COLUMNS:
            assert _has_column(engine, TABLE, name)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping:
        #    stamp back and run upgrade() again over a DB that already has the
        #    columns. The guards must make it a no-op instead of erroring.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_070)
        for name in COLUMNS:
            assert _has_column(engine, TABLE, name)
    finally:
        engine.dispose()
