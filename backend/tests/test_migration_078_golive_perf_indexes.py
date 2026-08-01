"""Coverage for 078_golive_perf_indexes (file 078_golive_perf_indexes.py).

078 adds EIGHT non-unique performance indexes for the go-live scale audit's hot read
paths (labor costing, lot/serial traceability, SPC reads, per-part/WO document lookups,
the audit list view), each mirrored in the owning model's ``__table_args__`` so the
``create_all`` bootstrap path emits them too (the 041/042 lock-step precedent).

What is load-bearing here:

1. **The drift guard.** The migration's frozen ``INDEXES`` literals, this test's frozen
   copy, and what the models actually declare on ``Base.metadata`` must all agree —
   name, exact column order, ``unique=False``, and (for the four partial indexes) an
   identical predicate declared for BOTH dialects (``postgresql_where`` AND
   ``sqlite_where``, the 076 dialect-parity convention). A model edit that drops or
   reshapes any of the eight, or a partial that loses one dialect's predicate, fails
   here loudly.
2. **Non-unique on purpose.** These are pure read-path speedups; none may ever become
   UNIQUE (that would invent a constraint 041/076 deliberately do not express).
3. **Postgres builds CONCURRENTLY inside an autocommit block, self-healing an INVALID
   leftover** (interrupted prior build), and SQLite is a clean early-return no-op in
   both directions — ``create_all`` already made the indexes there.
4. **No data statement.** ``audit_logs`` and ``inventory_transactions`` are regulated
   tables; 078 reads/writes zero rows (its only raw SQL is the read-only
   ``pg_index.indisvalid`` probe) and creates no table, so the RLS new-table
   convention does not apply.
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

REVISION = "078_golive_perf_indexes"
MIGRATION_FILE = "078_golive_perf_indexes.py"
DOWN_REVISION = "077_audit_seq_paused_chain"

# Frozen copy of the migration's INDEXES list: (table, index_name, columns, partial_where).
# partial_where is None for a full-table index. Three-way lock-step is asserted below:
# these literals == the migration's literals == the model __table_args__ declarations.
EXPECTED_INDEXES = [
    (
        "time_entries",
        "ix_time_entries_work_order_clock_out",
        ["work_order_id", "clock_out"],
        None,
    ),
    (
        "inventory_transactions",
        "ix_inv_txn_company_lot",
        ["company_id", "lot_number"],
        "lot_number IS NOT NULL",
    ),
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
    (
        "documents",
        "ix_documents_company_part",
        ["company_id", "part_id"],
        "part_id IS NOT NULL",
    ),
    (
        "documents",
        "ix_documents_company_work_order",
        ["company_id", "work_order_id"],
        "work_order_id IS NOT NULL",
    ),
    (
        "audit_logs",
        "ix_audit_logs_company_timestamp",
        ["company_id", "timestamp"],
        None,
    ),
    (
        "audit_logs",
        "ix_audit_logs_company_user_timestamp",
        ["company_id", "user_id", "timestamp"],
        None,
    ),
]


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_078", path)
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
    """{index_name: Index} for every model-declared index on the five target tables."""
    import app.models  # noqa: F401  # register every model on Base.metadata
    from app.db.database import Base

    declared = {}
    for table_name in {entry[0] for entry in EXPECTED_INDEXES}:
        assert table_name in Base.metadata.tables, f"{table_name} missing from Base.metadata"
        for index in Base.metadata.tables[table_name].indexes:
            declared[index.name] = index
    return declared


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    """One head, and 078 sits on 077 inside the head's ancestry.

    Deliberately does NOT assert that 078 IS the head (the 076-test lesson: pinning
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
    normalized = [(table, name, list(columns), where) for table, name, columns, where in module.INDEXES]
    assert normalized == EXPECTED_INDEXES, f"migration INDEXES drifted from the expected eight: {module.INDEXES!r}"
    assert len(module.INDEXES) == 8


@pytest.mark.unit
def test_all_eight_indexes_exist_in_base_metadata_with_exact_shape():
    """Every 078 index is mirrored in the owning model's __table_args__ — exactly.

    Name, column order, unique=False, and (for the four partials) the SAME predicate
    on BOTH dialects, so the SQLite create_all path builds the same partial shape a
    migrated Postgres DB has (the 076 dialect-parity convention). Full-table indexes
    must declare NO predicate on either dialect.
    """
    declared = _metadata_indexes()

    for table_name, index_name, columns, predicate in EXPECTED_INDEXES:
        assert index_name in declared, f"{index_name} not declared on the {table_name} model"
        index = declared[index_name]
        assert index.table.name == table_name, f"{index_name} declared on {index.table.name}, not {table_name}"
        assert index.unique is False, f"{index_name} must stay NON-unique (pure read-path speedup)"
        assert [c.name for c in index.columns] == columns, f"{index_name} column drift"

        pg_where = index.dialect_options["postgresql"]["where"]
        sqlite_where = index.dialect_options["sqlite"]["where"]
        if predicate is None:
            assert pg_where is None, f"{index_name} must not declare postgresql_where"
            assert sqlite_where is None, f"{index_name} must not declare sqlite_where"
        else:
            # BOTH dialects, same literal — partial on Postgres AND on SQLite.
            assert pg_where is not None, f"{index_name} lost its postgresql_where predicate"
            assert sqlite_where is not None, f"{index_name} lost its sqlite_where predicate"
            assert str(pg_where) == predicate, f"{index_name} postgresql_where drift: {pg_where!s}"
            assert str(sqlite_where) == predicate, f"{index_name} sqlite_where drift: {sqlite_where!s}"


@pytest.mark.unit
def test_the_three_pre_existing_unique_guards_are_untouched():
    """078 must not have reshaped inventory_transactions' 041/076 UNIQUE indexes."""
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
    that if_not_exists would silently keep forever; 078 must probe indisvalid."""
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
    """audit_logs/inventory_transactions are regulated tables — 078 touches no row.

    The only raw SQL allowed is the read-only pg_index validity probe.
    """
    body = _body()
    for statement in ("op.execute", "op.bulk_insert", "INSERT INTO", "DELETE FROM"):
        assert statement not in body, f"078 must not run a data statement ({statement!r})"
    # An UPDATE could only arrive via op.execute / a Session — both excluded above.
    assert "SELECT i.indisvalid" in body, "the pg_index probe is the one expected raw-SQL read"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """078 only adds indexes to pre-existing tables (059 already swept them)."""
    assert "op.create_table" not in _source()


# ---------------------------------------------------------------------------
# 4. create_all parity (the SQLite bootstrap really emits all eight)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_bootstrap_emits_all_eight_indexes(tmp_path):
    """create_all must build every 078 index — the partials WITH their predicate.

    This is the half of the lock-step the migration itself cannot cover: on SQLite
    the migration is a no-op, so the model declarations are the only thing standing
    between a bootstrapped DB and a migrated one.
    """
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig078_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        inspector = sa.inspect(engine)
        for table_name, index_name, columns, predicate in EXPECTED_INDEXES:
            reflected = {index["name"]: index for index in inspector.get_indexes(table_name)}
            assert index_name in reflected, f"create_all did not build {index_name} on {table_name}"
            index = reflected[index_name]
            assert not index["unique"], f"{index_name} must be NON-unique"
            assert list(index["column_names"]) == columns, f"{index_name} column drift in built DDL"
            where = (index.get("dialect_options") or {}).get("sqlite_where")
            if predicate is None:
                assert where is None, f"{index_name} unexpectedly built partial: {where!s}"
            else:
                assert where is not None, f"{index_name} built WITHOUT its partial predicate"
                assert str(where) == predicate, f"{index_name} predicate drift in built DDL: {where!s}"

        # And the raw DDL SQLite stored is a plain (non-unique) CREATE INDEX, with the
        # WHERE clause present exactly on the four partials.
        with engine.connect() as conn:
            for _table, index_name, _columns, predicate in EXPECTED_INDEXES:
                ddl = conn.execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"),
                    {"n": index_name},
                ).scalar()
                assert ddl is not None, f"{index_name} missing from sqlite_master"
                assert ddl.strip().upper().startswith("CREATE INDEX"), f"{index_name} must not be UNIQUE: {ddl}"
                if predicate is None:
                    assert "WHERE" not in ddl.upper(), f"{index_name} DDL grew a predicate: {ddl}"
                else:
                    assert f"WHERE {predicate}" in ddl, f"{index_name} DDL lost its predicate: {ddl}"
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
    """create_all -> stamp 077 -> upgrade 078 -> downgrade -> upgrade, byte-identical.

    On SQLite both directions early-return; create_all already built all eight
    indexes, and the round trip must not add, drop, or reshape ANY index DDL.
    """
    db_path = tmp_path / "mig078.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        bootstrapped = _index_ddl_snapshot(engine)
        for _table, index_name, _columns, _predicate in EXPECTED_INDEXES:
            assert index_name in bootstrapped, f"create_all must build {index_name}"

        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _index_ddl_snapshot(engine) == bootstrapped, "078 upgrade must be a no-op on SQLite"

        _alembic(db_url, "downgrade", "-1")
        assert _index_ddl_snapshot(engine) == bootstrapped, "078 downgrade must be a no-op on SQLite"

        _alembic(db_url, "upgrade", REVISION)
        assert _index_ddl_snapshot(engine) == bootstrapped, "re-upgrade must stay a no-op"
    finally:
        engine.dispose()
