"""Coverage for 075_inventory_txn_allocation_ref (material-consumption PR 1 -- ledger half).

075 makes two additive changes to ``inventory_transactions``, the append-only stock
ledger and the hottest table in the system:

- ``allocation_id`` -- nullable Integer FK -> ``work_order_material_allocations.id``,
  indexed. The durable genealogy key: which tie caused this movement. It survives a
  re-tie because an untied allocation is CANCELLED rather than deleted.
- ``ix_inventory_txn_company_reference`` -- a NON-unique composite on
  ``(company_id, reference_type, reference_id)``, backing the genealogy/history reads
  for reference types the 041 partial unique indexes do not cover (notably
  ``'work_order_operation'``).

Two assertions in this file are load-bearing:

1. **The 041 idempotency indexes are untouched.** ``uq_wo_inventory_receipt`` and
   ``uq_wo_inventory_issue`` are the DB-level guards that keep a concurrent
   double-complete from double-receiving finished goods or double-issuing a backflushed
   component (``completion_inventory_service.py``). Their predicates are load-bearing,
   and the new consumption path deliberately sits OUTSIDE them by using a different
   ``reference_type``. This is checked at the source level AND behaviorally on both
   sides of the round-trip -- SQLite takes the batch (table-recreate) path for the FK
   column, so "the recreate faithfully rebuilt the 041 indexes" is a real risk, not a
   theoretical one.
2. **No backfill.** ``allocation_id`` is NULL for every pre-existing ledger row and for
   every movement that is not allocation-driven (PO receipts, manual adjusts, finished
   goods, existing backflush issues). Inventing a value would fabricate material
   genealogy in an AS9100D traceability record -- the same posture 073 took with
   delivery provenance, and the opposite of 072's load-bearing ``notified_at`` backfill.
   The re-upgrade in the round-trip is where the real ``ADD COLUMN`` runs, so that is
   where the absence of a backfill is proven.
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

REVISION = "075_inventory_txn_allocation_ref"
MIGRATION_FILE = "075_inventory_txn_allocation_ref.py"
DOWN_REVISION = "074_wo_material_allocations"

TXN_TABLE = "inventory_transactions"
ALLOCATIONS_TABLE = "work_order_material_allocations"
ALLOCATION_COLUMN = "allocation_id"
ALLOCATION_INDEX = "ix_inventory_transactions_allocation_id"
REFERENCE_INDEX = "ix_inventory_txn_company_reference"
REFERENCE_COLUMNS = ["company_id", "reference_type", "reference_id"]

# The 041 backflush-idempotency indexes. Never created, dropped, or altered by 075.
UNTOUCHABLE_INDEXES = ("uq_wo_inventory_receipt", "uq_wo_inventory_issue")


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_075", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Executable source with the module docstring stripped.

    The docstring names the untouchable 041 indexes and discusses backfills at length,
    so a naive substring check over the whole file would match prose rather than code.
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
    """One head, and the 073 -> 074 -> 075 chain is intact."""
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION
    assert scripts.get_revision(DOWN_REVISION).down_revision == "073_sms_provider_delivery"

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert {REVISION, DOWN_REVISION, "073_sms_provider_delivery"} <= chain


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # alembic_version.version_num is varchar(32) on a freshly bootstrapped DB.
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert callable(module._has_table)
    assert callable(module._has_column)
    assert callable(module._has_index)
    assert module.TXN_TABLE == TXN_TABLE
    assert module.ALLOCATIONS_TABLE == ALLOCATIONS_TABLE


@pytest.mark.unit
def test_declared_shape_is_lock_step_with_the_model():
    from app.models.inventory import InventoryTransaction

    module = _load_module()
    assert module.ALLOCATION_COLUMN == ALLOCATION_COLUMN
    assert module.ALLOCATION_INDEX == ALLOCATION_INDEX
    assert module.REFERENCE_INDEX == REFERENCE_INDEX
    assert module.REFERENCE_COLUMNS == REFERENCE_COLUMNS

    column = InventoryTransaction.__table__.c[ALLOCATION_COLUMN]
    assert column.nullable is True, "allocation_id must stay nullable (additive, no backfill)"
    assert column.server_default is None
    assert column.index is True
    assert {fk.target_fullname for fk in column.foreign_keys} == {f"{ALLOCATIONS_TABLE}.id"}

    # The composite index is declared on the model too, so create_all converges.
    composite = {
        index.name: ([c.name for c in index.columns], index.unique) for index in InventoryTransaction.__table__.indexes
    }
    assert composite[REFERENCE_INDEX] == (REFERENCE_COLUMNS, False), "the reference index must be NON-unique"

    # Postgres identifier limit.
    for name in (ALLOCATION_INDEX, REFERENCE_INDEX):
        assert len(name) <= 63


@pytest.mark.unit
def test_the_041_idempotency_indexes_are_never_touched():
    """uq_wo_inventory_receipt / uq_wo_inventory_issue are load-bearing -- hands off.

    They back the backflush idempotency guards in completion_inventory_service.py. 075
    must not create, drop, or alter either one; the new consumption path stays outside
    their predicates by using reference_type = 'work_order_operation'.
    """
    module = _load_module()
    assert module.DO_NOT_TOUCH_INDEXES == UNTOUCHABLE_INDEXES

    body = _body()
    for name in UNTOUCHABLE_INDEXES:
        for verb in ("op.create_index", "op.drop_index", "op.alter_column"):
            assert f'{verb}("{name}"' not in body
            assert f"{verb}({name}" not in body
    # The only place the names may appear in code is the documentary constant.
    occurrences = body.count('"uq_wo_inventory_receipt"') + body.count('"uq_wo_inventory_issue"')
    assert occurrences == 2, "the untouchable index names should appear only in DO_NOT_TOUCH_INDEXES"

    # And the model still declares both, unchanged (partial + unique on Postgres).
    from app.models.inventory import InventoryTransaction

    declared = {index.name: index for index in InventoryTransaction.__table__.indexes}
    for name in UNTOUCHABLE_INDEXES:
        assert declared[name].unique is True
        where = declared[name].dialect_options["postgresql"].get("where")
        assert where is not None and "reference_type = 'work_order'" in str(where)


@pytest.mark.unit
def test_upgrade_is_guarded_and_the_downgrade_is_real():
    body = _body()
    upgrade = body[body.index("def upgrade") :]
    assert "if not _has_column(TXN_TABLE, ALLOCATION_COLUMN):" in upgrade
    assert "if not _has_index(TXN_TABLE, ALLOCATION_INDEX):" in upgrade
    assert "if not _has_index(TXN_TABLE, REFERENCE_INDEX):" in upgrade
    # SQLite cannot ALTER-add an FK constraint -- the batch path is required.
    assert "op.batch_alter_table(TXN_TABLE)" in upgrade

    downgrade = body[body.index("def downgrade") :]
    assert "op.drop_column(TXN_TABLE, ALLOCATION_COLUMN)" in downgrade, "downgrade must not be a stub"
    assert "op.drop_index(REFERENCE_INDEX, table_name=TXN_TABLE)" in downgrade
    assert "op.drop_index(ALLOCATION_INDEX, table_name=TXN_TABLE)" in downgrade


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """No backfill, no UPDATE, no INSERT -- material genealogy is never fabricated."""
    body = _body()
    for statement in ("op.execute", "UPDATE ", "INSERT ", "op.bulk_insert", "DELETE "):
        assert statement not in body, f"075 must not run a data statement ({statement!r})"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """075 only ALTERs a pre-existing table (059 already swept it)."""
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body
    assert "op.create_table" not in _source()


# ---------------------------------------------------------------------------
# 2. Real round-trip: upgrade -> downgrade -> upgrade over a bootstrapped DB
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


def _has_column(engine, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(engine).get_columns(table))


def _index_map(engine, table: str) -> dict:
    return {ix["name"]: (list(ix["column_names"]), bool(ix["unique"])) for ix in sa.inspect(engine).get_indexes(table)}


def _seed_ledger_row(engine, reference_type="work_order", reference_id=42) -> int:
    """A pre-075 ledger row: a movement that genuinely has no allocation."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO inventory_transactions "
                "(company_id, part_id, transaction_type, quantity, reference_type, reference_id, "
                " reference_number, created_by) "
                "VALUES (1, 7, 'ISSUE', -3, :rt, :ri, 'WO-1001', 1)"
            ),
            {"rt": reference_type, "ri": reference_id},
        )
        return conn.execute(sa.text("SELECT max(id) FROM inventory_transactions")).scalar()


@pytest.mark.integration
@pytest.mark.slow
def test_migration_075_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig075.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
        Base.metadata.create_all(engine)
        assert _has_column(engine, TXN_TABLE, ALLOCATION_COLUMN), "create_all did not build allocation_id"

        baseline_indexes = _index_map(engine, TXN_TABLE)
        for name in UNTOUCHABLE_INDEXES:
            assert name in baseline_indexes, f"{name} missing from the baseline"
        assert baseline_indexes[REFERENCE_INDEX] == (REFERENCE_COLUMNS, False)

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: guards fire -> clean no-op.
        _alembic(db_url, "upgrade", REVISION)
        assert _has_column(engine, TXN_TABLE, ALLOCATION_COLUMN)
        assert _index_map(engine, TXN_TABLE) == baseline_indexes

        # 2. Downgrade: a REAL drop of both indexes and the column. On SQLite this takes
        #    the batch (table-recreate) path, so the 041 indexes are genuinely at risk
        #    here -- assert they came back intact.
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TXN_TABLE, ALLOCATION_COLUMN), "downgrade left allocation_id behind"
        after_downgrade = _index_map(engine, TXN_TABLE)
        assert ALLOCATION_INDEX not in after_downgrade
        assert REFERENCE_INDEX not in after_downgrade
        for name in UNTOUCHABLE_INDEXES:
            assert after_downgrade[name] == baseline_indexes[name], f"the table recreate mangled {name}"

        # Seed a "historical" ledger row while allocation_id is ABSENT -- exactly the
        # pre-075 production state. The re-upgrade below is where the migration
        # genuinely ADDs the column, the only place a backfill could happen.
        historical_id = _seed_ledger_row(engine)
        operation_row_id = _seed_ledger_row(engine, reference_type="work_order_operation", reference_id=9)

        # 3. Re-upgrade: the guards do NOT fire, the real ADD COLUMN + two CREATE INDEX
        #    statements run.
        _alembic(db_url, "upgrade", REVISION)
        assert _has_column(engine, TXN_TABLE, ALLOCATION_COLUMN)
        assert _index_map(engine, TXN_TABLE) == baseline_indexes

        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, allocation_id, reference_type, reference_id, quantity "
                    "FROM inventory_transactions ORDER BY id"
                )
            ).fetchall()
        by_id = {row[0]: row for row in rows}
        assert len(rows) == 2, "both historical ledger rows must survive the round-trip"
        # THE headline assertion: no backfill. A pre-075 movement truthfully has no
        # allocation, and inventing one would fabricate material genealogy in an
        # AS9100D traceability record.
        assert by_id[historical_id][1] is None, "allocation_id must stay NULL -- 075 must not backfill"
        assert by_id[operation_row_id][1] is None, "no historical row gains an allocation"
        # The rest of each row is untouched by the ALTER / table recreate.
        assert by_id[historical_id][2:] == ("work_order", 42, -3.0)
        assert by_id[operation_row_id][2:] == ("work_order_operation", 9, -3.0)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _has_column(engine, TXN_TABLE, ALLOCATION_COLUMN)
        assert _index_map(engine, TXN_TABLE) == baseline_indexes
        with engine.connect() as conn:
            still_null = conn.execute(
                sa.text("SELECT count(*) FROM inventory_transactions WHERE allocation_id IS NOT NULL")
            ).scalar()
        assert still_null == 0, "a re-run must not stamp genealogy either"
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_consumption_reference_is_outside_the_041_predicates_on_both_dialects(tmp_path):
    """The consumption reference_type sits outside the 041 predicates -- on BOTH dialects.

    This test previously pinned a DIALECT DIVERGENCE as a known-bad fact: the 041 indexes
    declared only ``postgresql_where``, so on Postgres they were PARTIAL (scoped to
    ``reference_type = 'work_order'``) but on SQLite -- dev and the whole pytest suite --
    they degraded to FULL unique indexes covering EVERY reference_type. A second
    consumption row against one operation was therefore REJECTED locally even though
    production Postgres accepts it, which meant the harness enforced a constraint
    production does not.

    ``076_uq_wo_inv_sqlite_parity`` fixed that at the root by declaring ``sqlite_where``
    alongside ``postgresql_where`` from the same predicate constant. This test now
    asserts the CORRECTED behavior, and is the regression lock against the declaration
    ever losing a dialect again: both predicates must be present and identical, and the
    SQLite schema must actually let repeated consumption through.

    Note the schema under test here is what ``create_all`` builds (the bootstrap path),
    stamped at 074 and upgraded to 075 -- 076 itself is exercised in
    ``tests/test_migration_076_uq_wo_inventory_sqlite_parity.py``.
    """
    from app.models.inventory import (
        WO_ISSUE_INDEX_PREDICATE,
        WO_RECEIPT_INDEX_PREDICATE,
        InventoryTransaction,
    )

    # 1. Both dialects declare the SAME predicate, and it pins reference_type =
    #    'work_order', so 'work_order_operation' is outside both indexes everywhere.
    expected = {
        "uq_wo_inventory_receipt": WO_RECEIPT_INDEX_PREDICATE,
        "uq_wo_inventory_issue": WO_ISSUE_INDEX_PREDICATE,
    }
    declared = {index.name: index for index in InventoryTransaction.__table__.indexes}
    for name in UNTOUCHABLE_INDEXES:
        postgres_where = str(declared[name].dialect_options["postgresql"]["where"])
        sqlite_where = declared[name].dialect_options["sqlite"].get("where")
        assert sqlite_where is not None, (
            f"{name} must declare sqlite_where -- without it SQLite silently builds a FULL "
            "unique index and the test harness enforces a constraint production does not "
            "(the bug 076 fixed)"
        )
        assert str(sqlite_where) == postgres_where == expected[name], (
            f"{name}: the two dialect predicates must be the SAME literal. SQLAlchemy binds "
            "the uppercase enum MEMBER NAME ('RECEIVE'/'ISSUE') on Postgres AND SQLite, so "
            "one literal is correct for both"
        )
        assert "reference_type = 'work_order'" in postgres_where
        assert "work_order_operation" not in postgres_where, f"{name} must not cover the consumption path"

    # 2. The SQLite behavior, on the schema the migration produced.
    db_path = tmp_path / "mig075_reference.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)

        # Repeated consumption against ONE operation: many runs legitimately produce many
        # ledger rows. Accepted on Postgres, and -- since 076 -- on SQLite too.
        _seed_ledger_row(engine, reference_type="work_order_operation", reference_id=9)
        _seed_ledger_row(engine, reference_type="work_order_operation", reference_id=9)
        _seed_ledger_row(engine, reference_type="work_order_operation", reference_id=10)

        with engine.connect() as conn:
            count = conn.execute(
                sa.text("SELECT count(*) FROM inventory_transactions WHERE reference_type = 'work_order_operation'")
            ).scalar()
        assert count == 3, "the consumption path must not be deduped by the 041 indexes"

        # 3. ...while the guard the 041 indexes DO exist for is untouched: a duplicate
        #    work-order-referenced ISSUE for the same part is still rejected.
        _seed_ledger_row(engine, reference_type="work_order", reference_id=77)
        with pytest.raises(sa.exc.IntegrityError):
            _seed_ledger_row(engine, reference_type="work_order", reference_id=77)

        # 4. The new composite index is present and NON-unique (it must never dedupe
        #    consumption); the 041 indexes remain unique, partial, and untouched.
        indexes = _index_map(engine, TXN_TABLE)
        assert indexes[REFERENCE_INDEX] == (REFERENCE_COLUMNS, False)
        reflected = {ix["name"]: ix for ix in sa.inspect(engine).get_indexes(TXN_TABLE)}
        for name in UNTOUCHABLE_INDEXES:
            assert indexes[name][1] is True, f"{name} must remain unique"
            where = (reflected[name].get("dialect_options") or {}).get("sqlite_where")
            assert where is not None and str(where) == expected[name], f"{name} must be PARTIAL on SQLite"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. create_all parity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_and_the_migration_converge(tmp_path):
    """The bootstrap path and the migrated path agree on the column and both indexes."""
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig075_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns(TXN_TABLE)}
        assert ALLOCATION_COLUMN in columns, "create_all did not build allocation_id"
        assert columns[ALLOCATION_COLUMN]["nullable"] is True
        assert columns[ALLOCATION_COLUMN].get("default") is None
        assert str(columns[ALLOCATION_COLUMN]["type"]).upper() == "INTEGER"

        indexes = _index_map(engine, TXN_TABLE)
        assert indexes[ALLOCATION_INDEX] == ([ALLOCATION_COLUMN], False)
        assert indexes[REFERENCE_INDEX] == (REFERENCE_COLUMNS, False)

        # The FK really points at the allocations table.
        fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in sa.inspect(engine).get_foreign_keys(TXN_TABLE)
        }
        assert ((ALLOCATION_COLUMN,), ALLOCATIONS_TABLE, ("id",)) in fks
    finally:
        engine.dispose()
