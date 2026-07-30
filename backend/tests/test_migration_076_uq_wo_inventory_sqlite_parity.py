"""Coverage for 076_uq_wo_inv_sqlite_parity (file 076_uq_wo_inventory_sqlite_parity.py).

076 restores DIALECT PARITY for the two work-order completion idempotency indexes,
``uq_wo_inventory_receipt`` and ``uq_wo_inventory_issue`` (migration 041). They declared
only ``postgresql_where``, so they were PARTIAL on Postgres but degraded to FULL unique
indexes on SQLite -- dev and the entire pytest suite -- meaning the test environment
enforced a constraint production does not.

Four things are load-bearing here:

1. **Postgres is a NO-OP.** Both ``upgrade`` and ``downgrade`` return early off
   ``_is_postgres``. The Postgres indexes are already correct; rebuilding a UNIQUE index
   on ``inventory_transactions`` -- the hottest ledger table in the system -- would take a
   real lock and briefly leave the live double-receive race unguarded, for zero benefit.
   Asserted at the source level AND by generating the offline SQL for the postgresql
   dialect and proving it contains no DDL at all.
2. **The predicate literal is the same on both dialects.** SQLAlchemy binds the UPPERCASE
   enum MEMBER NAME (``'RECEIVE'``/``'ISSUE'``) for ``TransactionType`` under Postgres AND
   SQLite, so one literal is correct for both. Asserted at the bind-processor level, not
   assumed -- if the two dialects ever bind different literals the predicate would have to
   be written per-dialect and this test fails loudly.
3. **The guard the indexes exist for is NOT weakened.** For
   ``reference_type = 'work_order'`` RECEIVE/ISSUE rows, coverage is bit-identical before
   and after. Asserted behaviorally on both sides of the round-trip.
4. **No backfill, no data statement.** ``inventory_transactions`` is a regulated,
   traceability-bearing AS9100D record; 076 only rebuilds indexes and never inserts,
   updates, or deletes a row -- including on the downgrade, whose stricter restored shape
   can legitimately fail. It reports the offending groups and raises rather than
   deduplicating the ledger to make the rollback fit (the 041 posture).
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

REVISION = "076_uq_wo_inv_sqlite_parity"
MIGRATION_FILE = "076_uq_wo_inventory_sqlite_parity.py"
DOWN_REVISION = "075_inventory_txn_allocation_ref"

TXN_TABLE = "inventory_transactions"

RECEIPT_INDEX = "uq_wo_inventory_receipt"
RECEIPT_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type"]
RECEIPT_WHERE = "reference_type = 'work_order' AND transaction_type = 'RECEIVE'"

ISSUE_INDEX = "uq_wo_inventory_issue"
ISSUE_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type", "part_id"]
ISSUE_WHERE = "reference_type = 'work_order' AND transaction_type = 'ISSUE'"

TARGET_INDEXES = ((RECEIPT_INDEX, RECEIPT_COLUMNS, RECEIPT_WHERE), (ISSUE_INDEX, ISSUE_COLUMNS, ISSUE_WHERE))
EXPECTED_PREDICATE = {RECEIPT_INDEX: RECEIPT_WHERE, ISSUE_INDEX: ISSUE_WHERE}


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_076", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Executable source with the module docstring stripped.

    The docstring discusses backfills, DDL, and both index names at length, so a naive
    substring check over the whole file would match prose rather than code.
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
    """One head, and the 074 -> 075 -> 076 chain is intact.

    Deliberately does NOT assert that 076 is the head. It was when written, but
    pinning that makes this test fail on every subsequent migration for a reason
    that has nothing to do with 076 (077 tripped it). What matters here is that
    there is exactly ONE head and that 076 is still an ancestor of it with its
    chain intact -- both asserted below.
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION
    assert scripts.get_revision(DOWN_REVISION).down_revision == "074_wo_material_allocations"

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert {REVISION, DOWN_REVISION, "074_wo_material_allocations"} <= chain


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    """alembic_version.version_num is varchar(32) on a freshly bootstrapped DB.

    The descriptive filename (33 chars) is deliberately longer than the revision id --
    the 051/052/053/054 precedent -- so this is the assertion that matters.
    """
    assert len(REVISION) <= 32
    assert len(MIGRATION_FILE[: -len(".py")]) > 32, "the filename is the long, descriptive one"


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert callable(module._is_postgres)
    assert callable(module._is_sqlite)
    assert callable(module._has_table)
    assert callable(module._reflect_index)
    assert module.TABLE_NAME == TXN_TABLE


@pytest.mark.unit
def test_declared_shape_is_lock_step_with_the_model():
    """The migration's frozen literals must match what the model declares today.

    The duplication is intentional (a migration must stay frozen against future model
    edits -- the 041 precedent), so this is the check that keeps the two from drifting.
    """
    from app.models.inventory import (
        WO_ISSUE_INDEX_PREDICATE,
        WO_RECEIPT_INDEX_PREDICATE,
        InventoryTransaction,
    )

    module = _load_module()
    assert module.RECEIPT_INDEX == RECEIPT_INDEX
    assert module.ISSUE_INDEX == ISSUE_INDEX
    assert module.RECEIPT_COLUMNS == RECEIPT_COLUMNS
    assert module.ISSUE_COLUMNS == ISSUE_COLUMNS
    assert module.RECEIPT_WHERE == RECEIPT_WHERE == WO_RECEIPT_INDEX_PREDICATE
    assert module.ISSUE_WHERE == ISSUE_WHERE == WO_ISSUE_INDEX_PREDICATE

    declared = {index.name: index for index in InventoryTransaction.__table__.indexes}
    for name, columns, predicate in TARGET_INDEXES:
        index = declared[name]
        assert index.unique is True, f"{name} must stay UNIQUE"
        assert [c.name for c in index.columns] == columns
        # BOTH dialects, same literal. This is the whole point of 076.
        assert str(index.dialect_options["postgresql"]["where"]) == predicate
        assert str(index.dialect_options["sqlite"]["where"]) == predicate


@pytest.mark.unit
def test_the_predicate_literal_is_identical_on_both_dialects():
    """SQLAlchemy binds the enum MEMBER NAME on Postgres AND SQLite -- verified, not assumed.

    041's predicates use ``'RECEIVE'``/``'ISSUE'`` (the ``enum.name``) rather than the
    lowercase ``str`` values because that is what the native Postgres enum stores. 076
    copies those literals into ``sqlite_where``, which is only correct if SQLite binds the
    SAME literal. If a future SQLAlchemy version or a ``values_callable`` ever made the two
    dialects diverge, the predicate would have to be written per-dialect -- and this test is
    what would catch it.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.inventory import InventoryTransaction, TransactionType

    column_type = InventoryTransaction.__table__.c.transaction_type.type
    bound = {}
    for name, dialect in (("postgresql", postgresql.dialect()), ("sqlite", sqlite.dialect())):
        processor = column_type.dialect_impl(dialect).bind_processor(dialect)
        bound[name] = {
            member.name: (processor(member) if processor else member)
            for member in (TransactionType.RECEIVE, TransactionType.ISSUE)
        }

    assert bound["postgresql"] == bound["sqlite"] == {"RECEIVE": "RECEIVE", "ISSUE": "ISSUE"}, (
        f"the two dialects bind different literals for TransactionType ({bound!r}) -- the "
        "sqlite_where predicate can no longer be copied from postgresql_where and must be "
        "written per-dialect"
    )

    # ...and the predicates the migration ships use exactly those bound literals.
    assert f"transaction_type = '{bound['sqlite']['RECEIVE']}'" in RECEIPT_WHERE
    assert f"transaction_type = '{bound['sqlite']['ISSUE']}'" in ISSUE_WHERE


@pytest.mark.unit
def test_postgres_is_guarded_to_a_no_op_in_both_directions():
    """Neither direction may emit DDL on Postgres -- source-level assertion."""
    body = _body()
    for direction in ("def upgrade", "def downgrade"):
        block = body[body.index(direction) :]
        block = block[: block.index("\ndef ", 1)] if "\ndef " in block[1:] else block
        assert "if _is_postgres(conn) or not _is_sqlite(conn):" in block, f"{direction} must guard on dialect"
        assert "return" in block


@pytest.mark.unit
def test_upgrade_is_guarded_and_the_downgrade_is_real():
    body = _body()

    upgrade = body[body.index("def upgrade") : body.index("def downgrade")]
    # Reflection-guarded: an already-correct partial index is left alone.
    assert "_normalized(_index_predicate(existing)) == _normalized(predicate)" in upgrade
    assert "continue" in upgrade
    assert "_create_partial(" in upgrade

    downgrade = body[body.index("def downgrade") :]
    assert "_create_full(" in downgrade, "downgrade must really restore the full-index shape, not be a stub"
    assert "op.drop_index(index_name, table_name=TABLE_NAME)" in downgrade
    assert "_assert_downgrade_is_possible(conn)" in downgrade


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """No backfill, no dedup -- the ledger is a regulated record and is never rewritten."""
    body = _body()
    for statement in ("op.execute", "UPDATE ", "INSERT ", "op.bulk_insert", "DELETE "):
        assert statement not in body, f"076 must not run a data statement ({statement!r})"
    # The one SELECT present is the downgrade's read-only duplicate REPORT.
    assert "SELECT" in body and "GROUP BY" in body
    assert "raise RuntimeError" in body, "un-downgradable data must raise, never be deleted"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """076 only rebuilds indexes on a pre-existing table (059 already swept it)."""
    body = _body()
    assert "op.create_table" not in _source()
    assert "ENABLE ROW LEVEL SECURITY" not in body


@pytest.mark.unit
def test_audit_log_is_never_touched():
    """The tamper-evident hash chain is out of scope for this revision."""
    assert "audit_log" not in _body()


# ---------------------------------------------------------------------------
# 2. Postgres really emits no DDL (offline SQL, both directions)
# ---------------------------------------------------------------------------


def _offline_sql(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args, "--sql"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": "postgresql://u:p@localhost:5432/nodb"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} --sql failed\n{result.stdout}\n{result.stderr}"
    return result.stdout


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    "direction,args",
    [
        ("upgrade", ("upgrade", f"{DOWN_REVISION}:{REVISION}")),
        ("downgrade", ("downgrade", f"{REVISION}:{DOWN_REVISION}")),
    ],
)
def test_postgres_emits_no_ddl_in_either_direction(direction, args):
    """On Postgres this revision must advance alembic_version and do NOTHING else.

    Dropping/recreating a UNIQUE index on the hottest ledger table for no behavioral
    change is unacceptable risk: it takes a real lock and opens a window with no
    double-receive guard. Generating the SQL offline is the strongest available proof
    without a live Postgres.
    """
    sql = _offline_sql(*args).upper()
    for forbidden in ("CREATE INDEX", "CREATE UNIQUE INDEX", "DROP INDEX", "ALTER TABLE", "REINDEX"):
        assert forbidden not in sql, f"{direction} emitted {forbidden} on Postgres -- it must be a no-op"
    assert "UPDATE ALEMBIC_VERSION" in sql, "the version bump is the only statement expected"
    assert "INVENTORY_TRANSACTIONS" not in sql


# ---------------------------------------------------------------------------
# 3. Real round-trip over a bootstrapped SQLite DB
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str, expect_ok: bool = True):
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_ok:
        assert (
            result.returncode == 0
        ), f"alembic {' '.join(args)} failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


def _predicates(engine) -> dict:
    """{index_name: (predicate_or_None, unique, columns)} for the two 041 indexes."""
    out = {}
    for index in sa.inspect(engine).get_indexes(TXN_TABLE):
        if index["name"] in EXPECTED_PREDICATE:
            where = (index.get("dialect_options") or {}).get("sqlite_where")
            out[index["name"]] = (
                str(where) if where is not None else None,
                bool(index["unique"]),
                list(index["column_names"]),
            )
    return out


def _degrade_to_full_indexes(engine) -> None:
    """Recreate both indexes WITHOUT a predicate -- the pre-076 SQLite shape.

    This is what every dev DB and CI database bootstrapped before 076 actually looks
    like, so it is the state the migration has to repair.
    """
    with engine.begin() as conn:
        for name, columns, _predicate in TARGET_INDEXES:
            conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
            conn.execute(sa.text(f"CREATE UNIQUE INDEX {name} ON {TXN_TABLE} ({', '.join(columns)})"))


def _seed(engine, *, reference_type, reference_id, transaction_type="ISSUE", part_id=7, company_id=1) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO inventory_transactions "
                "(company_id, part_id, transaction_type, quantity, reference_type, reference_id, "
                " reference_number, created_by) "
                "VALUES (:c, :p, :tt, -3, :rt, :ri, 'WO-1001', 1)"
            ),
            {"c": company_id, "p": part_id, "tt": transaction_type, "rt": reference_type, "ri": reference_id},
        )


@pytest.mark.integration
@pytest.mark.slow
def test_migration_076_upgrade_downgrade_upgrade_round_trip(tmp_path):
    """create_all -> stamp -> degrade -> upgrade -> downgrade -> upgrade, asserting each shape."""
    db_path = tmp_path / "mig076.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        # Bootstrap exactly as production does on an empty DB: create_all -> stamp. The
        # model now declares sqlite_where, so create_all builds the indexes ALREADY partial.
        Base.metadata.create_all(engine)
        bootstrapped = _predicates(engine)
        for name, columns, predicate in TARGET_INDEXES:
            assert bootstrapped[name] == (predicate, True, columns), f"create_all must build {name} partial"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the already-correct bootstrapped schema: guards fire -> clean no-op.
        _alembic(db_url, "upgrade", REVISION)
        assert _predicates(engine) == bootstrapped, "076 must no-op on an already-correct schema"

        # 2. Degrade to the real pre-076 shape, then upgrade: this is the actual repair.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _degrade_to_full_indexes(engine)
        degraded = _predicates(engine)
        for name, columns, _predicate in TARGET_INDEXES:
            assert degraded[name] == (None, True, columns), f"{name} should now be a FULL unique index"

        # The bug, reproduced against the degraded schema: a second consumption row for one
        # operation is rejected, though production Postgres accepts it.
        _seed(engine, reference_type="work_order_operation", reference_id=9)
        with pytest.raises(sa.exc.IntegrityError):
            _seed(engine, reference_type="work_order_operation", reference_id=9)

        _alembic(db_url, "upgrade", REVISION)
        assert _predicates(engine) == bootstrapped, "upgrade must rebuild both indexes partial"

        # Fixed: repeated consumption against one operation is now accepted, as on Postgres.
        _seed(engine, reference_type="work_order_operation", reference_id=9)
        with engine.connect() as conn:
            consumption = conn.execute(
                sa.text("SELECT count(*) FROM inventory_transactions WHERE reference_type = 'work_order_operation'")
            ).scalar()
        assert consumption == 2

        # ...and the guard the indexes EXIST for is untouched: WO-level duplicates still fail.
        _seed(engine, reference_type="work_order", reference_id=42, transaction_type="RECEIVE")
        with pytest.raises(sa.exc.IntegrityError):
            _seed(engine, reference_type="work_order", reference_id=42, transaction_type="RECEIVE")
        _seed(engine, reference_type="work_order", reference_id=43, transaction_type="ISSUE", part_id=5)
        with pytest.raises(sa.exc.IntegrityError):
            _seed(engine, reference_type="work_order", reference_id=43, transaction_type="ISSUE", part_id=5)

        # 3. Downgrade REFUSES while the un-downgradable consumption rows are present --
        #    it reports them and raises rather than deleting regulated ledger rows.
        refused = _alembic(db_url, "downgrade", "-1", expect_ok=False)
        assert refused.returncode != 0
        combined = refused.stdout + refused.stderr
        assert "Cannot restore the pre-076" in combined
        assert "work_order_operation" in combined, "the refusal must itemize the offending groups"
        assert _predicates(engine) == bootstrapped, "a refused downgrade must leave the schema untouched"

        with engine.connect() as conn:
            before = conn.execute(sa.text("SELECT count(*) FROM inventory_transactions")).scalar()

        # 4. Clear the blocker the way an operator would (deliberately), then downgrade for real.
        with engine.begin() as conn:
            doomed = conn.execute(
                sa.text("SELECT max(id) FROM inventory_transactions WHERE reference_type = 'work_order_operation'")
            ).scalar()
            conn.execute(sa.text("DELETE FROM inventory_transactions WHERE id = :i"), {"i": doomed})

        _alembic(db_url, "downgrade", "-1")
        after_downgrade = _predicates(engine)
        for name, columns, _predicate in TARGET_INDEXES:
            assert after_downgrade[name] == (None, True, columns), f"downgrade must restore {name} FULL"
        # Genuinely restored, not just cosmetically: the old over-broad behavior is back.
        with pytest.raises(sa.exc.IntegrityError):
            _seed(engine, reference_type="work_order_operation", reference_id=9)

        # 5. Re-upgrade: back to partial, and no ledger row was invented or lost along the way.
        _alembic(db_url, "upgrade", REVISION)
        assert _predicates(engine) == bootstrapped
        with engine.connect() as conn:
            after = conn.execute(sa.text("SELECT count(*) FROM inventory_transactions")).scalar()
        assert after == before - 1, "only the row the operator deleted is gone -- 076 touches no data"
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_downgrade_succeeds_when_no_offending_rows_exist(tmp_path):
    """The ordinary rollback path: nothing to report, so the downgrade just runs."""
    db_path = tmp_path / "mig076_clean.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        _alembic(db_url, "stamp", REVISION)

        # Only rows that the restored FULL indexes can accept.
        _seed(engine, reference_type="work_order", reference_id=1, transaction_type="RECEIVE")
        _seed(engine, reference_type="work_order_operation", reference_id=2)

        _alembic(db_url, "downgrade", "-1")
        for name, columns, _predicate in TARGET_INDEXES:
            assert _predicates(engine)[name] == (None, True, columns)

        _alembic(db_url, "upgrade", REVISION)
        for name, columns, predicate in TARGET_INDEXES:
            assert _predicates(engine)[name] == (predicate, True, columns)

        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM inventory_transactions")).scalar() == 2
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. create_all parity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_and_the_migration_converge(tmp_path):
    """The bootstrap path builds exactly what the migration would produce."""
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig076_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        for name, columns, predicate in TARGET_INDEXES:
            assert _predicates(engine)[name] == (predicate, True, columns)

        # And the raw DDL SQLite stored really carries the WHERE clause.
        with engine.connect() as conn:
            for name, _columns, predicate in TARGET_INDEXES:
                ddl = conn.execute(sa.text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": name}).scalar()
                assert ddl.strip().upper().startswith("CREATE UNIQUE INDEX")
                assert f"WHERE {predicate}" in ddl
    finally:
        engine.dispose()
