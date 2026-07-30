"""Coverage for 077_audit_seq_paused_chain (file 077_audit_sequence_for_paused_chain.py).

077 creates the Postgres sequence that backs ``audit_logs.sequence_number`` while the
hash chain is PAUSED (``settings.AUDIT_HASH_CHAIN_ENABLED = False``). Nothing consumes it
while the chain is enabled, so applying it is a behavioral no-op -- which is precisely why
the properties below have to be asserted rather than eyeballed.

What is load-bearing:

1. **Script wiring** -- exactly one alembic head, 077 revises 076, the revision id fits
   ``alembic_version.version_num`` varchar(32) (a fresh prod bootstrap is
   ``create_all`` -> ``stamp 058`` -> ``upgrade``, so 014b's widening never runs there).
   Deliberately does NOT pin 077 as the head: that assertion is what 076's test had to
   give up when 077 landed.
2. **Dialect guard** -- on a non-postgresql bind neither ``upgrade()`` nor ``downgrade()``
   emits a single statement, with a postgresql run as the positive control so the empty
   list proves the guard rather than a mute stub. SQLite has no sequences and the service
   falls back to MAX+1 there.
3. **Idempotent and reversible** -- ``CREATE SEQUENCE IF NOT EXISTS``, a ``setval``
   recomputed from the live table on every run (so re-applying cannot regress the
   counter), and a real ``DROP SEQUENCE IF EXISTS`` downgrade.
4. **The start margin** -- the sequence starts at ``MAX(sequence_number) + 1000``. The
   chain may still be running when this is applied and keeps allocating MAX+1, so starting
   at exactly MAX+1 would hand out values it is about to use and the first paused write
   would collide.
5. **``audit_logs`` is never rewritten** -- the table is a tamper-evident, append-only
   regulated record (migration 008/060 triggers block UPDATE/DELETE). This migration reads
   ``MAX(sequence_number)`` and otherwise emits no data statement at all.
6. **The sequence name is the same string the service calls ``nextval`` on** -- the
   migration and ``audit_service._AUDIT_SEQUENCE_NAME`` are separate literals in separate
   files, and a drift between them is a runtime failure on the first paused write.
"""

import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "077_audit_seq_paused_chain"
MIGRATION_FILE = "077_audit_sequence_for_paused_chain.py"
DOWN_REVISION = "076_uq_wo_inv_sqlite_parity"

SEQUENCE_NAME = "audit_logs_sequence_number_seq"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_077", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE), encoding="utf-8") as fh:
        return fh.read()


def _body() -> str:
    """Executable source with the module docstring stripped.

    The docstring discusses grants, tables and RLS at length, so a substring check over
    the whole file would match prose rather than code (the 076 test's idiom).
    """
    docstring = _load_module().__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


class _RecordingBind:
    def __init__(self, dialect_name):
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return None


class _RecordingOp:
    """Stands in for alembic's ``op`` so a direction can be run without a database."""

    def __init__(self, bind):
        self._bind = bind

    def get_bind(self):
        return self._bind

    def execute(self, statement):
        self._bind.statements.append(str(statement))


def _run(direction: str, dialect_name: str):
    module = _load_module()
    bind = _RecordingBind(dialect_name)
    module.op = _RecordingOp(bind)
    getattr(module, direction)()
    return bind.statements


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert {REVISION, DOWN_REVISION} <= chain


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    assert len(REVISION) <= 32
    # The descriptive filename is deliberately the long one (the 051-054/076 precedent).
    assert len(MIGRATION_FILE[: -len(".py")]) > len(REVISION)


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert module.branch_labels is None
    assert module.depends_on is None
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert callable(module._is_postgres)


@pytest.mark.unit
def test_sequence_name_matches_the_one_the_service_calls_nextval_on():
    """Two literals in two files; a drift breaks the first paused write at runtime."""
    from app.services.audit_service import _AUDIT_SEQUENCE_NAME

    assert _load_module().SEQUENCE_NAME == SEQUENCE_NAME == _AUDIT_SEQUENCE_NAME


# ---------------------------------------------------------------------------
# 2. Dialect guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_non_postgres_bind_is_a_complete_no_op(direction):
    statements = _run(direction, "sqlite")
    assert statements == [], f"077.{direction}() emitted SQL on sqlite: {statements}"


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_guard_is_what_gates_execution_not_a_mute_stub(direction):
    """Positive control for the assertion above: the same harness on postgresql DOES emit."""
    assert _run(direction, "postgresql")


# ---------------------------------------------------------------------------
# 3. What Postgres actually gets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upgrade_is_idempotent_and_recomputes_the_start_from_the_live_table():
    statements = _run("upgrade", "postgresql")
    joined = "\n".join(statements)

    create = [s for s in statements if "CREATE SEQUENCE" in s]
    assert len(create) == 1
    assert f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME}" in create[0], "re-running must not error"
    assert "AS BIGINT" in create[0]

    # setval recomputed from the live table on EVERY run, and never below the sequence's
    # own last_value -- so re-applying can only move it forward, never regress it.
    setval = [s for s in statements if "setval" in s]
    assert len(setval) == 1
    assert "GREATEST" in setval[0]
    assert "MAX(sequence_number)" in setval[0]
    assert "FROM audit_logs" in setval[0]
    assert f"last_value FROM {SEQUENCE_NAME}" in setval[0]
    assert "true" in setval[0], "is_called=true means the next nextval() returns value + 1"

    # The headroom over MAX: the still-running chain keeps allocating MAX+1 while this is
    # applied, so starting at exactly MAX+1 would collide on the first paused write.
    module = _load_module()
    assert module.START_MARGIN == 1000
    assert f"+ {module.START_MARGIN} FROM audit_logs" in joined


@pytest.mark.unit
def test_downgrade_really_drops_the_sequence():
    statements = _run("downgrade", "postgresql")
    assert len(statements) == 1
    assert f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}" in statements[0]
    assert "audit_logs " not in statements[0], "the downgrade must not touch the table"


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_never_rewrites_the_audit_table(direction):
    """``audit_logs`` is append-only and tamper-evident -- 077 only reads MAX from it."""
    emitted = "\n".join(_run(direction, "postgresql")).upper()
    for forbidden in ("INSERT INTO", "UPDATE AUDIT_LOGS", "DELETE FROM", "ALTER TABLE", "DROP TABLE"):
        assert forbidden not in emitted, f"077.{direction}() emitted {forbidden}"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """A sequence is not a table; the ENABLE ROW LEVEL SECURITY convention has no target."""
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body
    # Grants are not widened either -- 059 revoked anon/authenticated on audit_logs and the
    # new sequence inherits owner-only.
    assert "GRANT" not in body.upper()


# ---------------------------------------------------------------------------
# 4. Through the real alembic machinery
# ---------------------------------------------------------------------------


def _alembic(*args: str, env: dict = None, expect_ok: bool = True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_ok:
        assert (
            result.returncode == 0
        ), f"alembic {' '.join(args)} failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    "direction,args,expected",
    [
        ("upgrade", ("upgrade", f"{DOWN_REVISION}:{REVISION}", "--sql"), "CREATE SEQUENCE IF NOT EXISTS"),
        ("downgrade", ("downgrade", f"{REVISION}:{DOWN_REVISION}", "--sql"), "DROP SEQUENCE IF EXISTS"),
    ],
)
def test_offline_postgres_sql_contains_the_sequence_ddl(direction, args, expected):
    """Generated through the real alembic CLI against a postgresql URL -- the closest
    available proof without a live Postgres."""
    sql = _alembic(*args, env={"DATABASE_URL": "postgresql://u:p@localhost:5432/nodb"}).stdout
    assert expected in sql, f"{direction} did not emit the expected DDL:\n{sql}"
    assert SEQUENCE_NAME in sql
    for forbidden in ("INSERT INTO", "DELETE FROM", "DROP TABLE"):
        assert forbidden not in sql.upper()


@pytest.mark.integration
@pytest.mark.slow
def test_sqlite_round_trip_changes_nothing(tmp_path):
    """create_all -> stamp 076 -> upgrade 077 -> downgrade -> upgrade, schema identical.

    The suite and local dev run on SQLite, so the guard has to hold through the real CLI,
    in both directions, without leaving a stray object behind.
    """
    db_path = tmp_path / "mig077.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)

        def snapshot():
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '%alembic%' ORDER BY name")
                ).fetchall()
            return [tuple(r) for r in rows]

        baseline = snapshot()
        assert any(name == "audit_logs" for _type, name, _sql in baseline)

        _alembic("stamp", DOWN_REVISION, env={"DATABASE_URL": db_url})
        _alembic("upgrade", REVISION, env={"DATABASE_URL": db_url})
        assert snapshot() == baseline, "077 must be a no-op on SQLite"

        _alembic("downgrade", "-1", env={"DATABASE_URL": db_url})
        assert snapshot() == baseline

        _alembic("upgrade", REVISION, env={"DATABASE_URL": db_url})
        assert snapshot() == baseline

        # And the version really advanced (the stamp/upgrade actually ran).
        with engine.connect() as conn:
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert version == REVISION
    finally:
        engine.dispose()
