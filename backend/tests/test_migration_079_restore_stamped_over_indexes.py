"""Coverage for 079_restore_stamped_over_idx (file 079_restore_stamped_over_indexes.py).

079 restores the FORTY-TWO non-unique read-path indexes that the create_all+stamp prod
bootstrap silently skipped (they lived only in migrations 001/003/020/026/027, never in
the models), and — the load-bearing half — mirrors every one into the owning model's
``__table_args__`` so the ``create_all`` bootstrap path emits them too and a future
stamp can never skip them again (the 042/078 lock-step precedent).

What is load-bearing here:

1. **The drift guard.** The migration's frozen ``INDEXES`` literals, this test's frozen
   copy, and what the models actually declare on ``Base.metadata`` must all agree —
   name, exact column order, ``unique=False``. 079 has NO partial indexes: none of the
   42 may declare ``postgresql_where`` or ``sqlite_where`` on either dialect. A model
   edit that drops or reshapes any of the 42 fails here loudly.
2. **The curation is deliberate.** ``ix_work_orders_status`` / ``ix_work_orders_due_date``
   (already present in prod via ``Column(index=True)``), ``ix_inv_txn_created_at``
   (same shape as the existing ``ix_inventory_transactions_created_at``) and
   ``ix_ncrs_status_source`` (no serving query; prefix-covered) are NOT restored — and
   must stay out of 079's literal. 078's eight indexes must not have been reshaped by
   079's model edits.
3. **Postgres builds CONCURRENTLY inside an autocommit block, self-healing an INVALID
   leftover** (interrupted prior build), and SQLite is a clean early-return no-op in
   both directions — ``create_all`` already made the indexes there.
4. **No data statement.** ``audit_logs`` carries the 008/060 tamper-evidence triggers
   and ``inventory_transactions`` is the regulated ledger; 079 reads/writes zero rows
   (its only raw SQL is the read-only ``pg_index.indisvalid`` probe) and creates no
   table, so the RLS new-table convention does not apply.
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

REVISION = "079_restore_stamped_over_idx"
MIGRATION_FILE = "079_restore_stamped_over_indexes.py"
DOWN_REVISION = "078_golive_perf_indexes"

# Frozen copy of the migration's INDEXES list: (table, index_name, columns).
# All 42 are plain full-table non-unique btree composites/singles — no partial
# predicates, no unique. Three-way lock-step is asserted below: these literals ==
# the migration's literals == the model __table_args__ declarations.
EXPECTED_INDEXES = [
    # Work Orders (001; 026; 027)
    ("work_orders", "ix_work_orders_status_due_date", ["status", "due_date"]),
    ("work_orders", "ix_work_orders_created_at", ["created_at"]),
    ("work_orders", "ix_work_orders_customer_name", ["customer_name"]),
    ("work_orders", "ix_work_orders_actual_end", ["actual_end"]),
    ("work_orders", "ix_work_orders_company_status", ["company_id", "status"]),
    ("work_orders", "ix_work_orders_company_due_date", ["company_id", "due_date"]),
    # Work Order Operations (001)
    ("work_order_operations", "ix_woo_work_center_status", ["work_center_id", "status"]),
    ("work_order_operations", "ix_woo_status", ["status"]),
    ("work_order_operations", "ix_woo_scheduled_start", ["scheduled_start"]),
    # Time Entries (001; 003)
    ("time_entries", "ix_time_entries_user_clock_out", ["user_id", "clock_out"]),
    ("time_entries", "ix_time_entries_wc_clock_in", ["work_center_id", "clock_in"]),
    ("time_entries", "ix_time_entries_type_clock_in", ["entry_type", "clock_in"]),
    ("time_entries", "ix_time_entries_user_clock_in", ["user_id", "clock_in"]),
    # Inventory Items (001)
    ("inventory_items", "ix_inventory_items_part_active", ["part_id", "is_active"]),
    ("inventory_items", "ix_inventory_items_status", ["status"]),
    ("inventory_items", "ix_inventory_items_warehouse", ["warehouse"]),
    # Inventory Transactions (001; 003)
    ("inventory_transactions", "ix_inv_txn_part_type_created", ["part_id", "transaction_type", "created_at"]),
    ("inventory_transactions", "ix_inventory_transactions_part_created", ["part_id", "created_at"]),
    # NCRs (001)
    ("ncrs", "ix_ncrs_status", ["status"]),
    ("ncrs", "ix_ncrs_status_created", ["status", "created_at"]),
    ("ncrs", "ix_ncrs_source", ["source"]),
    ("ncrs", "ix_ncrs_disposition", ["disposition"]),
    # CARs (001)
    ("cars", "ix_cars_status", ["status"]),
    ("cars", "ix_cars_due_date", ["due_date"]),
    # Equipment (001)
    ("equipment", "ix_equipment_next_cal_date", ["next_calibration_date"]),
    ("equipment", "ix_equipment_status_active", ["status", "is_active"]),
    # Purchase Orders (001)
    ("purchase_orders", "ix_purchase_orders_status", ["status"]),
    ("purchase_orders", "ix_purchase_orders_vendor_status", ["vendor_id", "status"]),
    ("purchase_orders", "ix_purchase_orders_required_date", ["required_date"]),
    # PO Receipts (001; 003)
    ("po_receipts", "ix_po_receipts_status", ["status"]),
    ("po_receipts", "ix_po_receipts_inspection_status", ["inspection_status"]),
    ("po_receipts", "ix_po_receipts_received_at", ["received_at"]),
    ("po_receipts", "ix_po_receipts_status_received", ["status", "received_at"]),
    # FAIs (001)
    ("fais", "ix_fais_status", ["status"]),
    # Cycle Counts (001)
    ("cycle_counts", "ix_cycle_counts_status_scheduled", ["status", "scheduled_date"]),
    # Quotes (001)
    ("quotes", "ix_quotes_status", ["status"]),
    ("quotes", "ix_quotes_updated_at", ["updated_at"]),
    # Audit Logs (003)
    ("audit_logs", "ix_audit_logs_resource_timestamp", ["resource_type", "resource_id", "timestamp"]),
    # MRP (003)
    ("mrp_requirements", "ix_mrp_requirements_run_part", ["mrp_run_id", "part_id"]),
    # Documents (020)
    ("documents", "ix_documents_vendor_id", ["vendor_id"]),
    # Parts / Users (026 tenancy composites)
    ("parts", "ix_parts_company_active", ["company_id", "is_active"]),
    ("users", "ix_users_company_active", ["company_id", "is_active"]),
]

# The curated SKIPS: names 079 must NOT declare (see the migration's "Curation
# decisions" header — already present via Column(index=True), shape-redundant, or
# prefix-covered with no serving query).
DELIBERATELY_NOT_RESTORED = (
    "ix_work_orders_status",
    "ix_work_orders_due_date",
    "ix_inv_txn_created_at",
    "ix_ncrs_status_source",
)

# Frozen copy of 078's eight indexes (table, name, columns, partial_where) — 079's
# model edits touch the same __table_args__ tuples, so guard that none were reshaped.
EXPECTED_078_INDEXES = [
    ("time_entries", "ix_time_entries_work_order_clock_out", ["work_order_id", "clock_out"], None),
    ("inventory_transactions", "ix_inv_txn_company_lot", ["company_id", "lot_number"], "lot_number IS NOT NULL"),
    (
        "inventory_transactions",
        "ix_inv_txn_company_serial",
        ["company_id", "serial_number"],
        "serial_number IS NOT NULL",
    ),
    (
        "spc_measurements",
        "ix_spc_measurements_char_subgroup",
        ["characteristic_id", "subgroup_number", "sample_number"],
        None,
    ),
    ("documents", "ix_documents_company_part", ["company_id", "part_id"], "part_id IS NOT NULL"),
    ("documents", "ix_documents_company_work_order", ["company_id", "work_order_id"], "work_order_id IS NOT NULL"),
    ("audit_logs", "ix_audit_logs_company_timestamp", ["company_id", "timestamp"], None),
    ("audit_logs", "ix_audit_logs_company_user_timestamp", ["company_id", "user_id", "timestamp"], None),
]


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_079", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Executable source with the module docstring stripped.

    The docstring discusses DDL, triggers, and every index name at length, so a naive
    substring check over the whole file would match prose rather than code.
    """
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


def _metadata_indexes() -> dict:
    """{index_name: Index} for every model-declared index on the target tables.

    Collects over the union of 079's tables and 078's tables so the not-reshaped
    guard can see spc_measurements too.
    """
    import app.models  # noqa: F401  # register every model on Base.metadata
    from app.db.database import Base

    tables = {entry[0] for entry in EXPECTED_INDEXES} | {entry[0] for entry in EXPECTED_078_INDEXES}
    declared = {}
    for table_name in tables:
        assert table_name in Base.metadata.tables, f"{table_name} missing from Base.metadata"
        for index in Base.metadata.tables[table_name].indexes:
            declared[index.name] = index
    return declared


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    """One head, and 079 sits on 078 inside the head's ancestry.

    Deliberately does NOT assert that 079 IS the head (the 076-test lesson: pinning
    head fails on every later migration for reasons unrelated to this revision).
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert {REVISION, DOWN_REVISION} <= chain


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    """alembic_version.version_num is varchar(32) on a freshly bootstrapped DB."""
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert callable(module._is_postgres)
    assert callable(module._index_validity)
    assert callable(module._ensure_index)


# ---------------------------------------------------------------------------
# 2. The drift guard: test literals == migration literals == Base.metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_migration_index_list_is_lock_step_with_this_test():
    """The migration's frozen INDEXES literals must match this test's frozen copy.

    The duplication is intentional (a migration must stay frozen against future
    edits); this is the check that keeps the two from drifting.
    """
    module = _load_module()
    normalized = [(table, name, list(columns)) for table, name, columns in module.INDEXES]
    assert normalized == EXPECTED_INDEXES, f"migration INDEXES drifted from the expected 42: {module.INDEXES!r}"
    assert len(module.INDEXES) == 42


@pytest.mark.unit
def test_all_42_indexes_exist_in_base_metadata_with_exact_shape():
    """Every 079 index is mirrored in the owning model's __table_args__ — exactly.

    Name, column order, unique=False — and, since 079 restores only plain
    full-table indexes, NO partial predicate on either dialect: a model edit that
    grows a postgresql_where/sqlite_where on any of the 42 would silently diverge
    the bootstrap DDL from what the migration builds.
    """
    declared = _metadata_indexes()

    for table_name, index_name, columns in EXPECTED_INDEXES:
        assert index_name in declared, f"{index_name} not declared on the {table_name} model"
        index = declared[index_name]
        assert index.table.name == table_name, f"{index_name} declared on {index.table.name}, not {table_name}"
        assert index.unique is False, f"{index_name} must stay NON-unique (pure read-path speedup)"
        assert [c.name for c in index.columns] == columns, f"{index_name} column drift"

        pg_where = index.dialect_options["postgresql"]["where"]
        sqlite_where = index.dialect_options["sqlite"]["where"]
        assert pg_where is None, f"{index_name} must not declare postgresql_where (079 has no partials)"
        assert sqlite_where is None, f"{index_name} must not declare sqlite_where (079 has no partials)"


@pytest.mark.unit
def test_deliberately_skipped_indexes_stay_out_of_079():
    """The curated skips must stay skipped — and their covering shapes must remain.

    ix_work_orders_status / ix_work_orders_due_date already exist in prod via
    Column(index=True) (restoring them would double-declare); ix_inv_txn_created_at
    duplicates the model-built ix_inventory_transactions_created_at on the ledger's
    highest-write column; ix_ncrs_status_source has no serving query and is
    prefix-covered by the restored ix_ncrs_status / ix_ncrs_source.
    """
    module = _load_module()
    migration_names = {name for _table, name, _columns in module.INDEXES}
    for skipped in DELIBERATELY_NOT_RESTORED:
        assert skipped not in migration_names, f"{skipped} was deliberately NOT restored by 079"

    declared = _metadata_indexes()

    # The two prod-present singles keep their Column(index=True) declarations, so
    # create_all still builds them without 079 naming them.
    for name, column in (("ix_work_orders_status", "status"), ("ix_work_orders_due_date", "due_date")):
        assert name in declared, f"{name} must stay model-declared via Column(index=True)"
        assert [c.name for c in declared[name].columns] == [column]
        assert declared[name].unique is False

    # ix_inv_txn_created_at exists under no name; its shape is already built as
    # the model's ix_inventory_transactions_created_at (single-column created_at).
    assert "ix_inv_txn_created_at" not in declared
    assert "ix_inventory_transactions_created_at" in declared
    assert [c.name for c in declared["ix_inventory_transactions_created_at"].columns] == ["created_at"]

    # ix_ncrs_status_source stays unrestored on the model too.
    assert "ix_ncrs_status_source" not in declared


@pytest.mark.unit
def test_078_eight_indexes_were_not_reshaped():
    """079 edits the same model __table_args__ tuples 078 lives in — none may move.

    Name, column order, unique=False, and (for 078's four partials) the identical
    predicate on BOTH dialects (the 076 dialect-parity convention).
    """
    declared = _metadata_indexes()
    for table_name, index_name, columns, predicate in EXPECTED_078_INDEXES:
        assert index_name in declared, f"{index_name} (078) vanished from the {table_name} model"
        index = declared[index_name]
        assert index.table.name == table_name
        assert index.unique is False, f"{index_name} (078) must stay NON-unique"
        assert [c.name for c in index.columns] == columns, f"{index_name} (078) column drift"
        pg_where = index.dialect_options["postgresql"]["where"]
        sqlite_where = index.dialect_options["sqlite"]["where"]
        if predicate is None:
            assert pg_where is None and sqlite_where is None, f"{index_name} (078) grew a predicate"
        else:
            assert pg_where is not None and str(pg_where) == predicate, f"{index_name} (078) postgresql_where drift"
            assert sqlite_where is not None and str(sqlite_where) == predicate, f"{index_name} (078) sqlite_where drift"


@pytest.mark.unit
def test_the_pre_existing_unique_guards_are_untouched():
    """079 must not have reshaped inventory_transactions' 041/076 UNIQUE indexes."""
    declared = _metadata_indexes()
    for name in ("uq_wo_inventory_receipt", "uq_wo_inventory_issue"):
        assert name in declared, f"{name} vanished from the model"
        assert declared[name].unique is True, f"{name} must stay UNIQUE"
    assert declared["ix_audit_logs_integrity"].unique is False


# ---------------------------------------------------------------------------
# 3. Source-level posture: CONCURRENTLY, self-heal, no data statements
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_postgres_is_an_early_return_in_both_directions():
    body = _body()
    for direction in ("def upgrade", "def downgrade"):
        block = body[body.index(direction) :]
        block = block[: block.index("\ndef ", 1)] if "\ndef " in block[1:] else block
        assert "if not _is_postgres(conn):" in block, f"{direction} must guard on dialect"
        assert "return" in block


@pytest.mark.unit
def test_postgres_builds_concurrently_inside_autocommit_blocks():
    """High-write tables: never take ACCESS EXCLUSIVE, in either direction."""
    body = _body()
    assert body.count("autocommit_block()") >= 2, "both directions must run inside an autocommit block"
    assert "postgresql_concurrently=True" in body
    upgrade = body[body.index("def upgrade") : body.index("def downgrade")]
    downgrade = body[body.index("def downgrade") :]
    assert "autocommit_block()" in upgrade
    assert "autocommit_block()" in downgrade
    assert "postgresql_concurrently=True" in downgrade, "rollback must drop CONCURRENTLY too"


@pytest.mark.unit
def test_invalid_leftover_index_is_dropped_and_rebuilt():
    """The 042 self-heal: an interrupted CONCURRENTLY build leaves an INVALID index
    that if_not_exists would silently keep forever; 079 must probe indisvalid."""
    body = _body()
    assert "indisvalid" in body
    assert "pg_index" in body
    ensure = body[body.index("def _ensure_index") : body.index("def upgrade")]
    assert 'if state == "invalid":' in ensure
    assert "op.drop_index(" in ensure
    assert "if_not_exists=True" in ensure
    assert "unique=False" in ensure, "the create call must pin unique=False"


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """audit_logs carries the 008/060 triggers and inventory_transactions is the
    regulated ledger — 079 touches no row.

    The only raw SQL allowed is the read-only pg_index validity probe.
    """
    body = _body()
    for statement in ("op.execute", "op.bulk_insert", "INSERT INTO", "DELETE FROM"):
        assert statement not in body, f"079 must not run a data statement ({statement!r})"
    # An UPDATE could only arrive via op.execute / a Session — both excluded above.
    assert "SELECT i.indisvalid" in body, "the pg_index probe is the one expected raw-SQL read"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """079 only adds indexes to pre-existing tables (059 already swept them)."""
    assert "op.create_table" not in _source()


# ---------------------------------------------------------------------------
# 4. create_all parity (the SQLite bootstrap really emits all 42)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_bootstrap_emits_all_42_indexes(tmp_path):
    """create_all must build every 079 index — plain, non-unique, no predicate.

    This is the half of the lock-step the migration itself cannot cover: on SQLite
    the migration is a no-op, so the model declarations are the only thing standing
    between a bootstrapped DB and a migrated one — the exact drift class 079 fixes.
    """
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig079_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        inspector = sa.inspect(engine)
        for table_name, index_name, columns in EXPECTED_INDEXES:
            reflected = {index["name"]: index for index in inspector.get_indexes(table_name)}
            assert index_name in reflected, f"create_all did not build {index_name} on {table_name}"
            index = reflected[index_name]
            assert not index["unique"], f"{index_name} must be NON-unique"
            assert list(index["column_names"]) == columns, f"{index_name} column drift in built DDL"
            where = (index.get("dialect_options") or {}).get("sqlite_where")
            assert where is None, f"{index_name} unexpectedly built partial: {where!s}"

        # And the raw DDL SQLite stored is a plain (non-unique) CREATE INDEX with
        # no WHERE clause on any of the 42.
        with engine.connect() as conn:
            for _table, index_name, _columns in EXPECTED_INDEXES:
                ddl = conn.execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"),
                    {"n": index_name},
                ).scalar()
                assert ddl is not None, f"{index_name} missing from sqlite_master"
                assert ddl.strip().upper().startswith("CREATE INDEX"), f"{index_name} must not be UNIQUE: {ddl}"
                assert "WHERE" not in ddl.upper(), f"{index_name} DDL grew a predicate: {ddl}"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. Real alembic round-trip on SQLite: a byte-identical no-op
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str):
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
    return result


def _index_ddl_snapshot(engine) -> dict:
    """{index_name: raw DDL} for every named index in the DB — byte-level snapshot."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name")
        ).fetchall()
    return {name: ddl for name, ddl in rows}


@pytest.mark.integration
@pytest.mark.slow
def test_sqlite_upgrade_downgrade_round_trip_is_a_clean_no_op(tmp_path):
    """create_all -> stamp 078 -> upgrade 079 -> downgrade -> upgrade, byte-identical.

    The bootstrap path this migration exists to fix, replayed end-to-end: on SQLite
    both directions early-return; create_all already built all 42 indexes, and the
    round trip must not add, drop, or reshape ANY index DDL. (Upgrades to REVISION,
    not `head`, per the 076-test lesson — today they are the same revision.)
    """
    db_path = tmp_path / "mig079.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        bootstrapped = _index_ddl_snapshot(engine)
        for _table, index_name, _columns in EXPECTED_INDEXES:
            assert index_name in bootstrapped, f"create_all must build {index_name}"

        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _index_ddl_snapshot(engine) == bootstrapped, "079 upgrade must be a no-op on SQLite"

        _alembic(db_url, "downgrade", "-1")
        assert _index_ddl_snapshot(engine) == bootstrapped, "079 downgrade must be a no-op on SQLite"

        _alembic(db_url, "upgrade", REVISION)
        assert _index_ddl_snapshot(engine) == bootstrapped, "re-upgrade must stay a no-op"
    finally:
        engine.dispose()
