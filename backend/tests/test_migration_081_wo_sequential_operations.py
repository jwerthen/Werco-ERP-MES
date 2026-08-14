"""Coverage for 081_wo_sequential_operations (the routing-vs-dispatch-pool discriminator).

081 is a purely additive change over the existing ``work_orders`` table: ONE column,

- ``sequential_operations``  Boolean, NOT NULL, ``server_default false``

deciding whether a work order's operations are a sequenced ROUTING (an operation reaches
READY only once every lower-sequence operation is COMPLETE, its own work center included)
or a DISPATCH POOL (operations sharing a work center are mutually startable and promote
together). The behavior is covered in ``tests/api/test_sequential_operations.py``; this
file covers the MIGRATION.

Two complementary layers, mirroring the idioms already in the suite
(tests/test_migration_070_last_report.py, ...068_run_order, ...064_visitor_entered_by):

1. Script wiring + source/model lock-step (unit) -- single alembic head, the 080 -> 081
   chain, the id fits ``alembic_version``'s varchar(32), the ADD COLUMN is guarded by
   ``_has_column`` (so the create_all -> stamp -> upgrade bootstrap path no-ops instead of
   erroring), the downgrade is real (drops exactly the one owned column, batch-mode on
   SQLite), the migration is pure DDL, no table is created (so the RLS new-table
   convention does not apply), no index is built, and the model declares exactly the
   column the migration adds.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) over a disposable
   SQLite file bootstrapped create_all -> stamp(080). The DDL is dialect-neutral, so
   SQLite exercises it for real, and the round-trip re-runs ``upgrade()`` over a DB that
   ALREADY has the column to prove the guard makes it idempotent rather than relying on
   alembic's version bookkeeping to skip it.

THE ONE THING A FUTURE READER WILL TRY TO "FIX"
-----------------------------------------------
The migration's ``server_default`` is **false** while the model's python-side ``default``
is **True**. That is not drift and the disagreement is asserted here in both directions:

* every row that already existed when 081 ran backfills to FALSE = pooled = the exact
  behavior it was released and scheduled under. An in-flight job must not change rules
  underneath the floor, and the batch WOs (18 operations each on one machine) keep showing
  every item on the kiosk;
* every work order inserted afterwards goes through the ORM, which supplies TRUE -- a
  sequenced routing, the common case and the one the pooled rule got wrong.

There is deliberately NO backfill and no ``UPDATE``: converting an existing job is an
explicit, audited flip through ``PUT /work-orders/{id}``, never a migration guessing at
intent. Pure DDL also makes the migration structurally incapable of touching the
tamper-evident ``audit_log`` hash chain.
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

REVISION_081 = "081_wo_sequential_operations"
MIGRATION_FILE = "081_wo_sequential_operations.py"
DOWN_REVISION = "080_restore_stamped_over_con"

TABLE = "work_orders"
COLUMN = "sequential_operations"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_081", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose explains at length what the migration deliberately does NOT do (no
    backfill, no UPDATE, no RLS statement), and it names every construct it avoids -- so
    assertions about absent constructs have to look at CODE only, or the docstring itself
    would fail them.
    """
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

    revision = scripts.get_revision(REVISION_081)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_081) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_081
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Constants describe exactly the object this migration owns.
    assert module.TABLE_NAME == TABLE
    assert module.COLUMN_NAME == COLUMN


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency + a real, guarded downgrade)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upgrade_is_guarded():
    """Safe to re-run, and a clean no-op on the create_all -> stamp -> upgrade bootstrap
    path where the ORM mapping already built the column."""
    module = _load_module()
    assert callable(module._has_column)

    source = _source()
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in source


@pytest.mark.unit
def test_upgrade_adds_a_not_null_column_with_a_constant_server_default():
    """NOT NULL needs a default to backfill the existing rows in place, and a CONSTANT
    one keeps it metadata-only on PostgreSQL 11+ (no table rewrite, no backfill pass)."""
    body = _body()
    assert "nullable=False" in body
    assert "server_default=sa.false()" in body


@pytest.mark.unit
def test_downgrade_is_real_and_guarded():
    """Not a `pass` stub: drops exactly the column this revision owns, guarded by
    _has_column, batch-mode on SQLite (which rebuilds the table)."""
    source = _source()
    assert "def downgrade() -> None:" in source
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in source
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in source
    assert "batch_op.drop_column(COLUMN_NAME)" in source
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in source
    # A downgrade that silently did nothing would be a stub in disguise.
    assert "\n    pass" not in source


@pytest.mark.unit
def test_no_backfill_and_no_raw_dml():
    """Pure DDL -- no data statements at all.

    Dropping the column returns every work order to pooled promotion (the pre-081 rule),
    which is why the downgrade needs no data step either. Being DDL-only also makes the
    migration structurally incapable of touching the tamper-evident audit_log hash chain.
    """
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "sa.text(", "conn.execute"):
        assert forbidden not in body, f"081 must be pure DDL; found {forbidden}"


@pytest.mark.unit
def test_column_add_only_so_no_rls_needed():
    """081 creates no table, so the ENABLE ROW LEVEL SECURITY new-table convention does
    not apply (the 059-gate test keys off op.create_table). ``work_orders`` is an
    EXISTING table that predates 059 and already carries RLS plus the TenantMixin
    non-null ``company_id`` + index."""
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body


@pytest.mark.unit
def test_no_index_is_built():
    """The column is read row-wise off an already-selected work order (one resolver,
    ``work_order_allows_same_work_center``), never filtered or sorted on -- deliberately
    un-indexed, unlike 068's run_order."""
    assert "op.create_index" not in _body()


@pytest.mark.unit
def test_the_table_already_carries_tenant_scoping():
    """Invariant 1 is unaffected: 081 adds no new tenant surface. Asserted rather than
    assumed, because "an existing table already has company_id" is the reason this
    migration is allowed to skip both the RLS statement and a company_id column."""
    from app.models.work_order import WorkOrder

    company_id = WorkOrder.__table__.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same object)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_one_not_null_unindexed_boolean():
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.Boolean)
    assert col.nullable is False
    assert col.index is not True, "deliberately un-indexed"
    assert col.unique is not True


@pytest.mark.unit
def test_the_two_defaults_disagree_on_purpose():
    """THE assertion that stops someone "fixing" the asymmetry.

    ``server_default='false'`` backfills every pre-081 row to POOLED -- the behavior it
    was released under. ``default=True`` makes every NEW work order a sequenced ROUTING.
    Collapsing them either way is a behavior change, not a cleanup: making the create
    default False re-ships the reported WO-20260807-006 bug, and making the server
    default true silently re-sequences in-flight batch jobs underneath the floor.
    """
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns[COLUMN]
    assert col.server_default is not None, "existing rows must backfill in place"
    assert col.server_default.arg == "false", "pre-081 rows are POOLED"
    assert col.default is not None, "new rows must not fall through to the server default"
    assert col.default.arg is True, "new work orders are a sequenced ROUTING"
    assert col.default.arg is not (col.server_default.arg == "true"), "the two defaults must DISAGREE"


@pytest.mark.unit
def test_the_migration_and_the_model_agree_on_the_server_default():
    """Lock-step: the create_all path and the upgrade path must build the same column."""
    from app.models.work_order import WorkOrder

    body = _body()
    col = WorkOrder.__table__.columns[COLUMN]
    assert "sa.false()" in body and col.server_default.arg == "false"
    assert "sa.Boolean()" in body and isinstance(col.type, sa.Boolean)


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
def test_migration_081_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig081.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-081 shape straight from the model.
        assert _has_column(engine, TABLE, COLUMN), "create_all did not build the column"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: the guard fires, so this is a clean
        #    no-op and the column survives untouched.
        _alembic(db_url, "upgrade", REVISION_081)
        assert _has_column(engine, TABLE, COLUMN)

        # 2. Downgrade: a REAL drop (the DDL is dialect-neutral, so SQLite exercises it).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)
        # Batch mode rebuilds the table -- the neighbouring schema must survive.
        remaining = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
        assert {
            "id",
            "work_order_number",
            "part_id",
            "work_order_type",
            "quantity_ordered",
            "status",
            "version",
            "company_id",
        } <= remaining

        # 3. Re-upgrade: the column comes back (the un-guarded ADD COLUMN path).
        _alembic(db_url, "upgrade", REVISION_081)
        assert _has_column(engine, TABLE, COLUMN)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping: stamp back
        #    and run upgrade() again over a DB that already has the column. The guard must
        #    make it a no-op instead of erroring.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_081)
        assert _has_column(engine, TABLE, COLUMN)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_pre_existing_rows_backfill_to_pooled(tmp_path):
    """THE data-migration claim, executed rather than asserted from the source.

    A work order that exists BEFORE the column does must come out of the upgrade with
    ``sequential_operations = 0`` -- pooled, the rule it was released and scheduled
    under. This is the half that protects the in-flight batch WOs, and it is the half a
    source-grep cannot prove.
    """
    db_path = tmp_path / "mig081_backfill.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all already built the post-081 shape, so reach the PRE-081 shape by
        # stamping the parent, marking 081 applied, and running ITS downgrade -- a bare
        # `downgrade -1` from 080 would run 080's downgrade instead, which is a different
        # migration entirely.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_081)
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)

        with engine.begin() as conn:
            # A production WO needs a part (ck_work_orders_part_required_unless_laser).
            # The shape mirrors the real in-flight case: an 18-item batch work order,
            # RELEASED, already on the floor's board when the migration runs.
            conn.execute(
                sa.text(
                    "INSERT INTO parts (id, part_number, name, part_type, unit_of_measure, company_id) "
                    "VALUES (1, 'PRE-081-PART', 'Batch bracket', 'manufactured', 'each', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders "
                    "(work_order_number, work_order_type, part_id, quantity_ordered, status, company_id, version) "
                    "VALUES ('PRE-081-WO', 'production', 1, 18, 'released', 1, 1)"
                )
            )

        _alembic(db_url, "upgrade", REVISION_081)

        with engine.begin() as conn:
            value = conn.execute(
                sa.text(f"SELECT {COLUMN} FROM work_orders WHERE work_order_number = 'PRE-081-WO'")
            ).scalar()
        assert value == 0, "a pre-081 work order must backfill to POOLED, not to the create-default"
    finally:
        engine.dispose()
