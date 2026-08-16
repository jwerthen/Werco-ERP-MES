"""Coverage for 082_vendor_active_before_delete (the restore-preserves-is_active sidecar).

082 is a purely additive change over the existing ``vendors`` table: ONE column,

- ``is_active_before_delete``  Boolean, NULLABLE, no default

remembering the ``is_active`` value a vendor had at the moment it was soft-deleted, so
``POST /purchasing/vendors/{id}/restore`` puts THAT value back instead of unconditionally
reactivating the supplier. An approved-supplier list is an AS9100D-controlled artifact: a
supplier the shop deliberately DEACTIVATED and then deleted must not come back looking
active. The endpoint behavior is covered in
``tests/api/test_vendor_deleted_only_restore_state.py`` §7; this file covers the MIGRATION.

Two complementary layers, mirroring ``test_migration_081_wo_sequential_operations.py``:

1. Script wiring + source/model lock-step (unit) -- single alembic head, the 081 -> 082
   chain, the id fits ``alembic_version``'s varchar(32), the ADD COLUMN is guarded by
   ``_has_table``/``_has_column`` (so the create_all -> stamp -> upgrade bootstrap path
   no-ops instead of erroring), the downgrade is real, the migration is pure DDL, no table
   is created (so the RLS new-table convention does not apply), no index is built, and the
   model declares exactly the column the migration adds.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) over a disposable
   SQLite file bootstrapped create_all -> stamp(081). The DDL is dialect-neutral, so
   SQLite exercises it for real, and the round-trip re-runs ``upgrade()`` over a DB that
   ALREADY has the column to prove the guard makes it idempotent rather than relying on
   alembic's version bookkeeping to skip it.

THE ONE THING A FUTURE READER WILL TRY TO "FIX"
-----------------------------------------------
This column is NULLABLE with NO server default -- the opposite of 081's shape, and it is
not laziness. NULL has to stay REACHABLE because it is a meaningful value: it means "we
never recorded one", which is the true state of every vendor soft-deleted before 082
shipped. ``restore_vendor`` reads NULL as "prior state unknown" and, by OWNER DECISION,
restores those rows INACTIVE -- COALESCE(is_active_before_delete, FALSE). That is a
deliberate break from the pre-082 unconditional ``is_active = True``: on an
AS9100D-controlled approved-supplier list the safe unknown is OFF, so a legacy vendor
comes back switched off and a human reactivates it through the separately audited PUT.

Tightening the column to NOT NULL (with either default) would therefore have to invent a
prior ``is_active`` for already-deleted vendors -- fabricating supplier-approval state in a
quality record -- and would make the fallback branch unreachable. There is deliberately no
backfill and no ``UPDATE``: the prior value is genuinely unknown, because the delete
overwrote it in place and the ``audit_log`` delete row records the deletion, not the flag.
Pure DDL also makes the migration structurally incapable of touching the tamper-evident
``audit_log`` hash chain (invariant 2).
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

REVISION_082 = "082_vendor_active_before_delete"
MIGRATION_FILE = "082_vendor_active_before_delete.py"
DOWN_REVISION = "081_wo_sequential_operations"

TABLE = "vendors"
COLUMN = "is_active_before_delete"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_082", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose explains at length what the migration deliberately does NOT do (no backfill,
    no UPDATE, no RLS statement) and names every construct it avoids -- so assertions about
    absent constructs have to look at CODE only, or the docstring would fail them.
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

    revision = scripts.get_revision(REVISION_082)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_082) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_082
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
def test_upgrade_is_guarded_on_both_the_table_and_the_column():
    """Safe to re-run, and a clean no-op on the create_all -> stamp -> upgrade bootstrap
    path where the ORM mapping already built the column."""
    module = _load_module()
    assert callable(module._has_table)
    assert callable(module._has_column)

    source = _source()
    assert "if not _has_table(TABLE_NAME):" in source
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in source


@pytest.mark.unit
def test_upgrade_adds_a_nullable_column_with_no_server_default():
    """THE shape assertion, and the inverse of 081's.

    Nullable with no default keeps the ADD COLUMN metadata-only on PostgreSQL 11+ (no
    rewrite, no row scan) -- but the reason it MUST stay this way is semantic, not
    operational: NULL is the value that means "deleted before 082 shipped, prior state
    unknown", and ``restore_vendor`` resolves that unknown to INACTIVE. A NOT NULL column
    with a default would have to invent supplier-approval state for those rows -- and would
    make the unknown unrepresentable, which is the whole point of the branch.
    """
    body = _body()
    assert "nullable=True" in body
    assert "nullable=False" not in body, "NULL must stay reachable -- it means 'never recorded'"
    assert "server_default" not in body, "a server default would backfill a fabricated prior state"


@pytest.mark.unit
def test_downgrade_is_real_and_guarded():
    """Not a `pass` stub: drops exactly the column this revision owns, guarded, batch-mode
    on SQLite (which rebuilds the table), plain DROP COLUMN elsewhere."""
    source = _source()
    assert "def downgrade() -> None:" in source
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in source
    assert "batch_op.drop_column(COLUMN_NAME)" in source
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in source
    # A downgrade that silently did nothing on every path would be a stub in disguise.
    assert "\n    pass" not in source


@pytest.mark.unit
def test_no_backfill_and_no_raw_dml():
    """Pure DDL -- no data statements at all.

    An already-deleted vendor's prior ``is_active`` is genuinely unknown (the delete
    overwrote it in place), so any backfill would fabricate supplier-approval state in a
    quality record. Being DDL-only also makes the migration structurally incapable of
    touching the tamper-evident audit_log hash chain (invariant 2).
    """
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "sa.text(", "conn.execute", "UPDATE "):
        assert forbidden not in body, f"082 must be pure DDL; found {forbidden}"


@pytest.mark.unit
def test_column_add_only_so_no_rls_needed():
    """082 creates no table, so the ENABLE ROW LEVEL SECURITY new-table convention does not
    apply (the 059-gate test keys off op.create_table). ``vendors`` is an EXISTING table
    that predates 059 and already carries RLS plus the TenantMixin non-null ``company_id``
    + index. Stated as a test rather than skipped silently."""
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body


@pytest.mark.unit
def test_no_index_is_built():
    """The column is read row-wise off an already-resolved vendor inside ``restore_vendor``
    (by primary key), never filtered or sorted on -- deliberately un-indexed."""
    assert "op.create_index" not in _body()


@pytest.mark.unit
def test_the_table_already_carries_tenant_scoping():
    """Invariant 1 is unaffected: 082 adds no new tenant surface. Asserted rather than
    assumed, because "an existing table already has company_id" is the reason this
    migration is allowed to skip both the RLS statement and a company_id column."""
    from app.models.purchasing import Vendor

    company_id = Vendor.__table__.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same object)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_one_nullable_unindexed_boolean_with_no_defaults():
    """Lock-step (the 042/078/079/080 convention): the create_all path and the upgrade path
    must build the same column, or a freshly bootstrapped DB diverges from a migrated one.

    ``default=None`` is asserted explicitly: a python-side default of True/False would make
    every NEW vendor row carry a remembered state it never had, which is the same
    fabrication a server default would commit at the DB level.
    """
    from app.models.purchasing import Vendor

    col = Vendor.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.Boolean)
    assert col.nullable is True, "NULL must stay reachable -- it means 'deleted before 082'"
    assert col.server_default is None
    assert col.default is None, "a python-side default would pre-fill a state that never existed"
    assert col.index is not True, "deliberately un-indexed"
    assert col.unique is not True

    body = _body()
    assert "sa.Boolean()" in body


@pytest.mark.unit
def test_the_sidecar_sits_beside_the_column_it_shadows():
    """``is_active`` must still exist and still be the flag the sidecar remembers.

    The tempting "fix" this PR refused was to delete ``delete_vendor``'s
    ``is_active = False`` write so restore has nothing to put back. If someone later
    removes the ``is_active`` COLUMN instead, this sidecar becomes meaningless rather than
    merely unused -- so its continued existence is asserted here, next to the reason.
    """
    from app.models.purchasing import Vendor

    assert "is_active" in Vendor.__table__.columns
    assert isinstance(Vendor.__table__.columns["is_active"].type, sa.Boolean)
    # And the soft-delete columns the sidecar's lifetime is scoped to (added by 071).
    for soft_delete_column in ("is_deleted", "deleted_at", "deleted_by"):
        assert soft_delete_column in Vendor.__table__.columns


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
def test_migration_082_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig082.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-082 shape straight from the model.
        assert _has_column(engine, TABLE, COLUMN), "create_all did not build the column"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: the guard fires, so this is a clean
        #    no-op and the column survives untouched.
        _alembic(db_url, "upgrade", REVISION_082)
        assert _has_column(engine, TABLE, COLUMN)

        # 2. Downgrade: a REAL drop (the DDL is dialect-neutral, so SQLite exercises it).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)
        # Batch mode rebuilds the table -- the neighbouring schema must survive, including
        # the soft-delete columns this feature is built on and the tenant scoping.
        remaining = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
        assert {
            "id",
            "code",
            "name",
            "is_active",
            "is_approved",
            "is_deleted",
            "deleted_at",
            "deleted_by",
            "company_id",
        } <= remaining
        # ...and so must the per-company unique constraint the vendor-code duplicate probes
        # depend on (a batch rebuild that silently dropped it would let two vendors share a
        # code, which is what makes a restore un-restorable).
        uniques = {u["name"] for u in sa.inspect(engine).get_unique_constraints(TABLE)}
        assert "uq_vendors_company_code" in uniques

        # 3. Re-upgrade: the column comes back (the un-guarded ADD COLUMN path).
        _alembic(db_url, "upgrade", REVISION_082)
        assert _has_column(engine, TABLE, COLUMN)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping: stamp back
        #    and run upgrade() again over a DB that already has the column. The guard must
        #    make it a no-op instead of erroring.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_082)
        assert _has_column(engine, TABLE, COLUMN)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_pre_existing_vendors_land_on_null_not_a_fabricated_value(tmp_path):
    """THE data claim, executed rather than asserted from the source.

    A vendor that exists BEFORE the column does -- including one already soft-deleted, the
    case the fallback exists for -- must come out of the upgrade with NULL. NULL is what
    ``restore_vendor`` reads as "prior state unknown", restoring the row INACTIVE. A server
    default or a backfill would put a fabricated prior ``is_active`` on a supplier record
    instead, and a source-grep cannot prove its absence the way running it can.
    """
    db_path = tmp_path / "mig082_backfill.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all already built the post-082 shape, so reach the PRE-082 shape by
        # stamping the parent, marking 082 applied, and running ITS downgrade -- a bare
        # `downgrade -1` from 081 would run 081's downgrade, a different migration.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_082)
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)

        with engine.begin() as conn:
            # Two pre-082 vendors: one live-but-DEACTIVATED, one already soft-deleted.
            # The second is exactly the row the NULL fallback was written for.
            conn.execute(
                sa.text(
                    "INSERT INTO vendors (code, name, is_active, is_approved, is_deleted, company_id) "
                    "VALUES ('PRE082A', 'Deactivated supplier', 0, 1, 0, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO vendors (code, name, is_active, is_approved, is_deleted, company_id) "
                    "VALUES ('PRE082B', 'Removed supplier', 0, 1, 1, 1)"
                )
            )

        _alembic(db_url, "upgrade", REVISION_082)

        with engine.begin() as conn:
            rows = dict(
                conn.execute(sa.text(f"SELECT code, {COLUMN} FROM vendors WHERE code LIKE 'PRE082%'")).fetchall()
            )
        assert rows == {"PRE082A": None, "PRE082B": None}, "082 must not invent a prior is_active for existing rows"
    finally:
        engine.dispose()
