"""Migration 057_kiosk_stations round-trip: upgrade -> downgrade -> upgrade.

Runs the real alembic CLI against a disposable SQLite file (the same dialect
pytest fixtures and local dev use), following the documented bootstrap path
(docs/DEVELOPMENT.md): ``Base.metadata.create_all()`` -> ``stamp`` -> upgrade.

Proves, in one sequenced test:
1. Upgrade over a create_all-bootstrapped DB (table already present) is a
   guarded no-op that leaves the schema byte-identical (idempotency).
2. Downgrade drops ``kiosk_stations`` and its indexes and touches nothing else
   (real reversibility, not a ``pass``).
3. Downgrade with the table already absent is a guarded no-op.
4. Upgrade re-creates the table from the migration DDL and the result is
   structurally EQUAL to the model/create_all schema (lock-step parity), so
   migration-built and bootstrap-built databases converge.
"""

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = "kiosk_stations"

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


def _snapshot(engine):
    """Normalized structural snapshot of kiosk_stations (None if absent)."""
    inspector = sa.inspect(engine)
    if not inspector.has_table(TABLE):
        return None
    columns = [(c["name"], str(c["type"]), bool(c["nullable"])) for c in inspector.get_columns(TABLE)]
    pk = tuple(inspector.get_pk_constraint(TABLE)["constrained_columns"])
    fks = sorted(
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in inspector.get_foreign_keys(TABLE)
    )
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE))
    return {"columns": columns, "pk": pk, "fks": fks, "indexes": indexes}


def _all_tables(engine):
    return sorted(sa.inspect(engine).get_table_names())


def test_migration_057_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig057.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        reference = _snapshot(engine)
        assert reference is not None, "create_all did not build kiosk_stations — model not wired into app/models"
        assert {name for name, _cols, _uq in reference["indexes"]} == {
            "ix_kiosk_stations_company_id",
            "ix_kiosk_stations_id",
            "ix_kiosk_stations_work_center_id",
        }

        _alembic(db_url, "stamp", "056_visitor_logs")
        tables_at_baseline = _all_tables(engine)

        # 1. Upgrade over an existing table: guarded no-op, schema unchanged.
        # Pinned to 057 (not "head") so later migrations (058+) can't change what
        # this round-trip exercises -- "downgrade -1" must reverse 057 itself.
        _alembic(db_url, "upgrade", "057_kiosk_stations")
        assert _snapshot(engine) == reference, "057 upgrade mutated a create_all-bootstrapped schema"

        # 2. Downgrade drops table + indexes and nothing else.
        _alembic(db_url, "downgrade", "-1")
        assert _snapshot(engine) is None, "downgrade did not drop kiosk_stations"
        assert _all_tables(engine) == sorted(set(tables_at_baseline) - {TABLE}), "downgrade disturbed other tables"
        inspector = sa.inspect(engine)
        leftover = [
            ix["name"]
            for table in inspector.get_table_names()
            for ix in inspector.get_indexes(table)
            if ix["name"] and ix["name"].startswith("ix_kiosk_stations")
        ]
        assert not leftover, f"leftover kiosk_stations indexes after downgrade: {leftover}"

        # 3. Downgrade with the table already absent: guarded no-op.
        _alembic(db_url, "stamp", "057_kiosk_stations")
        _alembic(db_url, "downgrade", "-1")
        assert _snapshot(engine) is None

        # 4. Upgrade re-creates from migration DDL, converging on the model schema.
        _alembic(db_url, "upgrade", "057_kiosk_stations")
        rebuilt = _snapshot(engine)
        assert rebuilt == reference, (
            "migration-built kiosk_stations differs from the create_all/model schema:\n" f"{rebuilt}\nvs\n{reference}"
        )
    finally:
        engine.dispose()
