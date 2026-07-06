"""Migration 058_process_sheets round-trip: upgrade -> downgrade -> upgrade.

Runs the real alembic CLI against a disposable SQLite file (the same dialect
pytest fixtures and local dev use), following the documented bootstrap path
(docs/DEVELOPMENT.md): ``Base.metadata.create_all()`` -> ``stamp`` -> upgrade.
Same precedent as tests/test_migration_057_kiosk_stations.py; revisions are
pinned to 058 (never "head") so later migrations can't change what this
round-trip exercises.

Proves, in one sequenced test:
1. Upgrade over a create_all-bootstrapped DB (four process-sheet tables and
   three added FK columns already present) is a guarded no-op that leaves the
   schema structurally identical (idempotency).
2. Downgrade drops the four tables (``process_sheets``,
   ``process_sheet_steps``, ``wo_operation_steps``,
   ``operation_step_records``), the three added FK columns
   (``routing_operations.process_sheet_id``, ``spc_measurements.operation_id``,
   ``work_order_blockers.ncr_id``) and their indexes, and touches nothing else
   (real reversibility, not a ``pass``).
3. Downgrade with everything already absent is a guarded no-op.
4. Upgrade re-creates it all from the migration DDL and the result is
   structurally EQUAL to the model/create_all schema (lock-step parity), so
   migration-built and bootstrap-built databases converge.

Snapshot normalization: host-table column lists are compared UNORDERED -- on
SQLite the upgrade path re-adds the FK column at the END of the table (batch
recreate) while create_all emits it in declared order; same columns, different
position. FK snapshots exclude constraint names for the same reason (the
SQLite batch path must NAME the re-added FK; create_all leaves it unnamed).
The four brand-new tables keep the strict ordered comparison.
"""

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REVISION = "058_process_sheets"
PREVIOUS = "057_kiosk_stations"

NEW_TABLES = ["process_sheets", "process_sheet_steps", "wo_operation_steps", "operation_step_records"]

# (host_table, added_column, added_index) -- the three nullable FK columns 058
# adds to existing tenant tables.
ADDED_COLUMNS = [
    ("routing_operations", "process_sheet_id", "ix_routing_operations_process_sheet_id"),
    ("spc_measurements", "operation_id", "ix_spc_measurements_operation_id"),
    ("work_order_blockers", "ncr_id", "ix_work_order_blockers_ncr_id"),
]

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _alembic(db_url: str, *args: str) -> None:
    """Run the alembic CLI in a subprocess pointed at the scratch DB."""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _table_snapshot(inspector, table: str, ordered_columns: bool):
    """Normalized structural snapshot of one table (None if absent).

    FK entries deliberately exclude the constraint name (see module docstring);
    host tables pass ``ordered_columns=False`` to tolerate SQLite's append-at-end
    batch-recreate column position.
    """
    if not inspector.has_table(table):
        return None
    columns = [(c["name"], str(c["type"]), bool(c["nullable"])) for c in inspector.get_columns(table)]
    if not ordered_columns:
        columns = sorted(columns)
    pk = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
    fks = sorted(
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in inspector.get_foreign_keys(table)
    )
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table))
    return {"columns": columns, "pk": pk, "fks": fks, "indexes": indexes}


def _snapshot(engine):
    """Snapshot everything 058 owns: the four new tables + the three host tables."""
    inspector = sa.inspect(engine)
    snap = {table: _table_snapshot(inspector, table, ordered_columns=True) for table in NEW_TABLES}
    for host_table, _column, _index in ADDED_COLUMNS:
        snap[host_table] = _table_snapshot(inspector, host_table, ordered_columns=False)
    return snap


def _all_tables(engine):
    return sorted(sa.inspect(engine).get_table_names())


def test_migration_058_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig058.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        reference = _snapshot(engine)
        for table in NEW_TABLES:
            assert reference[table] is not None, f"create_all did not build {table} — model not wired into app/models"
        for host_table, column, index in ADDED_COLUMNS:
            column_names = {name for name, _type, _nullable in reference[host_table]["columns"]}
            assert column in column_names, f"create_all did not add {host_table}.{column}"
            index_names = {name for name, _cols, _uq in reference[host_table]["indexes"]}
            assert index in index_names, f"create_all did not build {index}"

        _alembic(db_url, "stamp", PREVIOUS)
        tables_at_baseline = _all_tables(engine)

        # 1. Upgrade over a bootstrapped schema: guarded no-op, structurally unchanged.
        _alembic(db_url, "upgrade", REVISION)
        assert _snapshot(engine) == reference, "058 upgrade mutated a create_all-bootstrapped schema"

        # 2. Downgrade drops the four tables, the three columns + indexes, and nothing else.
        _alembic(db_url, "downgrade", "-1")
        downgraded = _snapshot(engine)
        for table in NEW_TABLES:
            assert downgraded[table] is None, f"downgrade did not drop {table}"
        for host_table, column, index in ADDED_COLUMNS:
            column_names = {name for name, _type, _nullable in downgraded[host_table]["columns"]}
            assert column not in column_names, f"downgrade did not drop {host_table}.{column}"
            index_names = {name for name, _cols, _uq in downgraded[host_table]["indexes"]}
            assert index not in index_names, f"downgrade did not drop {index}"
        assert _all_tables(engine) == sorted(
            set(tables_at_baseline) - set(NEW_TABLES)
        ), "downgrade disturbed other tables"
        inspector = sa.inspect(engine)
        new_table_prefixes = tuple(f"ix_{table}" for table in NEW_TABLES)
        leftover = [
            ix["name"]
            for table in inspector.get_table_names()
            for ix in inspector.get_indexes(table)
            if ix["name"] and ix["name"].startswith(new_table_prefixes)
        ]
        assert not leftover, f"leftover process-sheet indexes after downgrade: {leftover}"

        # 3. Downgrade with everything already absent: guarded no-op.
        _alembic(db_url, "stamp", REVISION)
        _alembic(db_url, "downgrade", "-1")
        for table in NEW_TABLES:
            assert _snapshot(engine)[table] is None

        # 4. Upgrade re-creates from migration DDL, converging on the model schema.
        _alembic(db_url, "upgrade", REVISION)
        rebuilt = _snapshot(engine)
        assert rebuilt == reference, (
            "migration-built process-sheet schema differs from the create_all/model schema:\n"
            f"{rebuilt}\nvs\n{reference}"
        )
    finally:
        engine.dispose()
