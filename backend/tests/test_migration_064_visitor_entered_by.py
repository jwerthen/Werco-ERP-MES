"""Coverage for 064_visitor_log_entered_by (staff back-entry attribution).

064 adds ONE nullable FK column ``visitor_logs.entered_by_user_id -> users.id``
(app/models/visitor_log.py::VisitorLog) — the positive "this row was
back-entered by staff" flag written by ``POST /api/v1/visitor-logs/manual``.

Two complementary layers, mirroring the two migration-test idioms already in the
suite:

1. Script wiring + source/model lock-step (unit) — the idiom of
   tests/test_migration_063_scrap_oee.py: single alembic head, the 063->064
   revision chain, the id fits alembic_version's varchar(32), the module exposes
   callable upgrade/downgrade, the FK guard matches by CONSTRAINED COLUMN (so the
   create_all-bootstrapped auto-named FK idempotently no-ops), the FK add is
   Postgres-only and the downgrade uses SQLite batch mode, and the model carries
   the same nullable ``users.id`` FK the migration builds.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) — the
   idiom of tests/test_migration_058_process_sheets.py: run the alembic CLI over
   a disposable SQLite file bootstrapped create_all -> stamp(063), and assert the
   column is a guarded no-op on the bootstrapped schema, is dropped by downgrade,
   and is re-added by upgrade. Only COLUMN PRESENCE is asserted (never the FK
   set): the migration creates the named FK on Postgres ONLY — on SQLite the
   re-upgrade adds the bare column, exactly as the migration docstring documents.
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

REVISION_064 = "064_visitor_log_entered_by"
MIGRATION_FILE = "064_visitor_log_entered_by.py"
DOWN_REVISION = "063_scrap_reason_codes_oee"

LOGS_TABLE = "visitor_logs"
COLUMN = "entered_by_user_id"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_064", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_064)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_064) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_064
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Constants describe exactly the one column/table this migration owns.
    assert module.LOGS_TABLE == LOGS_TABLE
    assert module.COLUMN == COLUMN


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency + dialect handling)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fk_guard_matches_by_constrained_column_not_name():
    """Idempotency on the create_all-bootstrapped path: the FK guard must match
    ANY existing FK on the column (the model's inline FK is auto-named
    ``visitor_logs_entered_by_user_id_fkey``), not just the migration's own
    ``fk_visitor_logs_entered_by_user_id`` name."""
    module = _load_module()
    assert callable(module._has_fk_on_column)
    source = _source()
    assert 'fk.get("constrained_columns") == [column_name]' in source


@pytest.mark.unit
def test_add_column_and_fk_are_guarded_and_fk_is_postgres_only():
    """ADD COLUMN is guarded by ``_has_column``; the named FK is created ONLY on
    Postgres (SQLite cannot ADD CONSTRAINT after the fact and its create_all path
    already wired the inline FK)."""
    source = _source()
    assert "if not _has_column(LOGS_TABLE, COLUMN):" in source
    assert "if _is_postgres(conn) and not _has_fk_on_column(LOGS_TABLE, COLUMN):" in source


@pytest.mark.unit
def test_downgrade_drops_fk_by_reflected_name_then_column_and_uses_sqlite_batch():
    """Reversibility: Postgres drops any FK on the column by reflected name (covers
    both the named and auto-named variants) then the column; SQLite uses batch
    mode to recreate the table without the FK-bearing column."""
    source = _source()
    assert "_fk_names_on_column(LOGS_TABLE, COLUMN)" in source
    assert 'op.drop_constraint(actual_fk_name, LOGS_TABLE, type_="foreignkey")' in source
    assert "op.batch_alter_table(LOGS_TABLE)" in source
    assert "batch_op.drop_column(COLUMN)" in source


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same column)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_carries_the_same_nullable_users_fk_column():
    from app.models.visitor_log import VisitorLog

    col = VisitorLog.__table__.columns[COLUMN]
    assert col.nullable is True
    assert isinstance(col.type, sa.Integer)
    fks = list(col.foreign_keys)
    assert len(fks) == 1, f"expected exactly one FK on {COLUMN}"
    assert fks[0].column.table.name == "users"
    assert fks[0].column.name == "id"


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
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _has_column(engine, table: str, column: str) -> bool:
    inspector = sa.inspect(engine)
    if not inspector.has_table(table):
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


@pytest.mark.integration
@pytest.mark.slow
def test_migration_064_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig064.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the column — proves VisitorLog is wired into
        # app/models (else autogenerate would miss it; migration docstring).
        assert _has_column(
            engine, LOGS_TABLE, COLUMN
        ), f"create_all did not build {LOGS_TABLE}.{COLUMN} — model not wired into app/models"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over a bootstrapped schema: guarded no-op, column still present.
        _alembic(db_url, "upgrade", REVISION_064)
        assert _has_column(engine, LOGS_TABLE, COLUMN), "064 upgrade dropped a bootstrapped column"

        # 2. Downgrade drops the column (real reversibility, not a `pass`).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, LOGS_TABLE, COLUMN), "064 downgrade did not drop the column"

        # 3. Upgrade re-adds the column from the migration DDL (bare column on
        #    SQLite — the named FK is Postgres-only, per the migration docstring).
        _alembic(db_url, "upgrade", REVISION_064)
        assert _has_column(engine, LOGS_TABLE, COLUMN), "064 re-upgrade did not re-add the column"
    finally:
        engine.dispose()
