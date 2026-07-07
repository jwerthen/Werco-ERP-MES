"""Regression coverage for 059_supabase_rls_hardening + 060_audit_log_immutability.

Both migrations are Postgres-only security hardening (deny-by-default RLS +
PostgREST grant revocation; the restored 008 audit-log immutability triggers
with a pinned search_path). Their author rehearsed them end-to-end against a
Docker Postgres 17 prod simulation; this file locks in what CAN regress from
inside this repo's SQLite-based suite:

1. Script-directory wiring (unit): single head; 059 revises
   ``058_process_sheets``; 060 revises 059; every revision id added AFTER the
   058 bootstrap baseline fits ``alembic_version``'s varchar(32) (a freshly
   bootstrapped prod DB is ``create_all`` -> ``stamp 058`` -> ``upgrade``, so
   014b's column widening never runs there -- docs/DEVELOPMENT.md); both
   modules import and expose callable ``upgrade()``/``downgrade()``.
2. Dialect guard (unit): on a non-postgresql bind, ``upgrade()`` and
   ``downgrade()`` of BOTH migrations return without emitting one statement.
3. Emitted-SQL invariants (unit, fake-postgresql recording bind) -- the
   load-bearing security properties: 059 enables RLS WITHOUT ``FORCE`` (the
   owning app role must keep bypassing) and only on tables reported as
   ``rowsecurity = false``; its revokes target exactly anon+authenticated,
   never ``service_role``, and are skipped entirely when the Supabase roles
   don't exist (plain/CI Postgres); its downgrade never disables RLS on
   ``companies``. 060 pins ``SET search_path = ''`` on both trigger functions,
   creates the triggers only when missing (existence check scoped via
   ``to_regclass('public.audit_logs')``), and its downgrade ONLY resets
   search_path -- it never drops the functions or triggers (that lifecycle
   belongs to 008, and dropping would reopen the CMMC AU-3.3.8 gap).
4. SQLite no-op round trip (integration/slow, real alembic CLI -- same idiom
   as tests/test_migration_057_kiosk_stations.py / 058): create_all ->
   stamp 058 -> upgrade 060 -> downgrade 058 -> upgrade 060, asserting the
   full structural schema snapshot is identical at every step, i.e. the
   dialect guards make both migrations true no-ops on the SQLite dialect the
   suite and local dev run on, in both directions, via the real CLI.

The live-Postgres assertions (zero ``rowsecurity = false`` tables, zero
anon/authenticated grants, both triggers present, proconfig search_path
pinned, UPDATE/DELETE on audit_logs raising) intentionally stay with the
author's rehearsal and docs/SUPABASE_SECURITY.md's verification SQL: this
suite has no real-Postgres path -- tests/conftest.py forces per-xdist-worker
SQLite even under CI's postgres service, so a live-PG test here could never
run.
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

REVISION_059 = "059_supabase_rls_hardening"
REVISION_060 = "060_audit_log_immutability"
BOOTSTRAP_BASELINE = "058_process_sheets"

MIGRATION_FILES = {
    REVISION_059: "059_supabase_rls_hardening.py",
    REVISION_060: "060_audit_log_immutability.py",
}

STRAY_POLICY_DDL = 'DROP POLICY IF EXISTS "Enable read access for all users" ON public.companies'


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_migration(revision: str):
    """Import a migration module fresh from its file (names start with digits)."""
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILES[revision])
    spec = importlib.util.spec_from_file_location(f"_migtest_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _RecordingBind:
    """Stands in for ``op.get_bind()``: records every statement it sees and
    answers catalog queries from (substring -> rows) responders; anything
    unmatched gets zero rows."""

    def __init__(self, dialect_name, responders=()):
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements = []
        self._responders = list(responders)

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        for substring, rows in self._responders:
            if substring in sql:
                return _StubResult(rows)
        return _StubResult([])


class _RecordingOp:
    def __init__(self, bind):
        self._bind = bind

    def get_bind(self):
        return self._bind

    def execute(self, statement):
        self._bind.statements.append(str(statement))


def _run(revision, direction, dialect_name, responders=()):
    """Run one migration direction against a recording bind; return every
    statement it emitted (catalog SELECTs included)."""
    module = _load_migration(revision)
    bind = _RecordingBind(dialect_name, responders)
    module.op = _RecordingOp(bind)
    getattr(module, direction)()
    return bind.statements


def _ddl(statements):
    """Everything that is not a catalog SELECT: the DDL the migration emits."""
    return [s for s in statements if not s.lstrip().upper().startswith("SELECT")]


@pytest.mark.unit
class TestScriptDirectoryWiring:
    def test_single_head(self):
        heads = _script_directory().get_heads()
        assert len(heads) == 1, f"multiple alembic heads: {heads}"

    def test_revision_chain(self):
        script = _script_directory()
        assert script.get_revision(REVISION_059).down_revision == BOOTSTRAP_BASELINE
        assert script.get_revision(REVISION_060).down_revision == REVISION_059

    def test_post_bootstrap_revision_ids_fit_varchar32(self):
        # A fresh prod bootstrap is create_all -> stamp 058 -> upgrade, and the
        # stamped alembic_version.version_num is varchar(32) (014b's widening
        # sits upstream of the stamp and never runs there), so every revision
        # AFTER the 058 baseline must keep its id <= 32 chars. Locks 059/060
        # and every future revision.
        script = _script_directory()
        (head,) = script.get_heads()
        post_bootstrap = [
            rev.revision
            for rev in script.iterate_revisions(head, BOOTSTRAP_BASELINE)
            if rev.revision != BOOTSTRAP_BASELINE
        ]
        assert REVISION_059 in post_bootstrap
        assert REVISION_060 in post_bootstrap
        too_long = [r for r in post_bootstrap if len(r) > 32]
        assert not too_long, f"revision ids exceed alembic_version varchar(32): {too_long}"

    @pytest.mark.parametrize("revision", [REVISION_059, REVISION_060])
    def test_module_imports_and_exposes_upgrade_downgrade(self, revision):
        module = _load_migration(revision)
        assert module.revision == revision
        assert callable(module.upgrade)
        assert callable(module.downgrade)

    def test_every_table_creating_migration_after_059_enables_rls(self):
        # In-repo gate for the deny-by-default posture (docs/SUPABASE_SECURITY.md,
        # CLAUDE.md migrations section): every migration AFTER 059 that creates a
        # table must ENABLE ROW LEVEL SECURITY on it, or the Supabase Security
        # Advisor's rls_disabled_in_public ERROR returns. The Advisor is external
        # and post-deploy; this test is what fails CI when migration N+1 forgets.
        script = _script_directory()
        (head,) = script.get_heads()
        offenders = []
        for rev in script.iterate_revisions(head, REVISION_059):
            if rev.revision == REVISION_059:
                continue
            with open(rev.path, encoding="utf-8") as fh:
                source = fh.read()
            if "op.create_table" in source and "ENABLE ROW LEVEL SECURITY" not in source:
                offenders.append(rev.revision)
        assert not offenders, (
            f"table-creating migrations missing ENABLE ROW LEVEL SECURITY: {offenders} "
            "(see docs/SUPABASE_SECURITY.md -- new-table convention)"
        )


@pytest.mark.unit
class TestDialectGuard:
    @pytest.mark.parametrize("revision", [REVISION_059, REVISION_060])
    @pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
    def test_non_postgres_bind_is_a_complete_no_op(self, revision, direction):
        statements = _run(revision, direction, dialect_name="sqlite")
        assert statements == [], f"{revision}.{direction}() emitted SQL on sqlite: {statements}"

    @pytest.mark.parametrize("revision", [REVISION_059, REVISION_060])
    def test_guard_is_what_gates_execution(self, revision):
        # Positive control: the same harness on a 'postgresql' bind DOES record
        # statements, so the empty list above proves the guard, not a mute stub.
        assert _run(revision, "upgrade", dialect_name="postgresql")


@pytest.mark.unit
class TestMigration059EmittedSql:
    def test_upgrade_enables_rls_without_force_and_drops_stray_policy(self):
        responders = [
            ("NOT rowsecurity", [("audit_logs",), ("work_orders",), ('odd"name',)]),
            ("pg_roles", []),  # plain/CI Postgres: PostgREST roles absent
        ]
        ddl = _ddl(_run(REVISION_059, "upgrade", "postgresql", responders))
        # Fail-fast lock_timeout is set before any lock-taking DDL (boot-time
        # migration must not queue behind long-running queries), then the stray
        # anon-read policy drop leads the real work.
        assert ddl[0] == "SET lock_timeout = '5s'"
        assert ddl[1] == STRAY_POLICY_DDL
        enables = [s for s in ddl if "ENABLE ROW LEVEL SECURITY" in s]
        assert enables == [
            'ALTER TABLE public."audit_logs" ENABLE ROW LEVEL SECURITY',
            'ALTER TABLE public."work_orders" ENABLE ROW LEVEL SECURITY',
            'ALTER TABLE public."odd""name" ENABLE ROW LEVEL SECURITY',  # identifiers quoted, quotes doubled
        ]
        assert not any("FORCE" in s for s in ddl), "059 must never FORCE ROW LEVEL SECURITY (app role must bypass)"

    def test_upgrade_skips_revokes_when_postgrest_roles_absent(self):
        responders = [("NOT rowsecurity", []), ("pg_roles", [])]
        ddl = _ddl(_run(REVISION_059, "upgrade", "postgresql", responders))
        assert not any("REVOKE" in s or "GRANT" in s for s in ddl), f"revoked without anon/authenticated: {ddl}"

    def test_upgrade_revokes_target_exactly_the_postgrest_roles(self):
        responders = [("NOT rowsecurity", []), ("pg_roles", [(1,)])]
        ddl = _ddl(_run(REVISION_059, "upgrade", "postgresql", responders))
        revokes = [s for s in ddl if "REVOKE" in s]
        assert revokes == [
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated",
            "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated",
            "REVOKE USAGE ON SCHEMA public FROM anon, authenticated",
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated",
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "REVOKE ALL ON SEQUENCES FROM anon, authenticated",
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "REVOKE ALL ON FUNCTIONS FROM anon, authenticated",
        ]
        assert not any("service_role" in s for s in ddl), "059 must never touch service_role"

    def test_downgrade_restores_prior_state_but_keeps_companies_rls_on(self):
        responders = [
            ("AND rowsecurity", [("companies",), ("work_orders",)]),
            ("pg_roles", [(1,)]),
            ("pg_policies", []),
        ]
        ddl = _ddl(_run(REVISION_059, "downgrade", "postgresql", responders))
        disables = [s for s in ddl if "DISABLE ROW LEVEL SECURITY" in s]
        assert disables == ['ALTER TABLE public."work_orders" DISABLE ROW LEVEL SECURITY']
        assert "ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY" in ddl
        assert any(s.startswith('CREATE POLICY "Enable read access for all users"') for s in ddl)
        grants = [s for s in ddl if "GRANT" in s]
        assert len(grants) == 7
        assert all(s.endswith("TO anon, authenticated") for s in grants)
        assert not any("service_role" in s for s in ddl)


@pytest.mark.unit
class TestMigration060EmittedSql:
    def test_upgrade_pins_search_path_and_creates_missing_triggers(self):
        statements = _run(REVISION_060, "upgrade", "postgresql", [("pg_trigger", [])])
        ddl = _ddl(statements)
        functions = [s for s in ddl if "CREATE OR REPLACE FUNCTION" in s]
        assert len(functions) == 2
        assert all("SET search_path = ''" in s for s in functions), "trigger functions must pin search_path"
        assert any("public.audit_log_immutable_update()" in s for s in functions)
        assert any("public.audit_log_immutable_delete()" in s for s in functions)
        triggers = [s for s in ddl if "CREATE TRIGGER" in s]
        assert len(triggers) == 2
        assert all("ON public.audit_logs" in s for s in triggers)
        assert any("tr_audit_log_no_update" in s and "BEFORE UPDATE" in s for s in triggers)
        assert any("tr_audit_log_no_delete" in s and "BEFORE DELETE" in s for s in triggers)
        assert any("COMMENT ON TABLE public.audit_logs" in s for s in ddl)
        # Existence check is scoped to public.audit_logs, not tgname alone (008's gap).
        trigger_checks = [s for s in statements if "pg_trigger" in s]
        assert trigger_checks
        assert all("to_regclass('public.audit_logs')" in s for s in trigger_checks)

    def test_upgrade_skips_trigger_creation_when_already_present(self):
        ddl = _ddl(_run(REVISION_060, "upgrade", "postgresql", [("pg_trigger", [(1,)])]))
        assert not any("CREATE TRIGGER" in s for s in ddl)
        # Functions are still CREATE OR REPLACEd -- that's the idempotent pinning path.
        assert sum("CREATE OR REPLACE FUNCTION" in s for s in ddl) == 2

    def test_downgrade_only_resets_search_path_and_never_drops(self):
        ddl = _ddl(_run(REVISION_060, "downgrade", "postgresql", [("pg_proc", [(1,)])]))
        assert ddl == [
            "ALTER FUNCTION public.audit_log_immutable_update() RESET search_path",
            "ALTER FUNCTION public.audit_log_immutable_delete() RESET search_path",
        ], "060 downgrade must only unpin search_path -- dropping would reopen the AU-3.3.8 gap"

    def test_downgrade_is_a_no_op_when_functions_absent(self):
        assert _ddl(_run(REVISION_060, "downgrade", "postgresql", [("pg_proc", [])])) == []


# --- SQLite no-op round trip via the real alembic CLI (057/058 precedent) ---


def _alembic(db_url: str, *args: str) -> str:
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
    return result.stdout


def _schema_snapshot(engine):
    """Structural snapshot of EVERY table: columns, pk, fks, indexes."""
    inspector = sa.inspect(engine)
    snapshot = {}
    for table in sorted(inspector.get_table_names()):
        columns = [(c["name"], str(c["type"]), bool(c["nullable"])) for c in inspector.get_columns(table)]
        pk = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        fks = sorted(
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table)
        )
        indexes = sorted(
            (ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table)
        )
        snapshot[table] = {"columns": columns, "pk": pk, "fks": fks, "indexes": indexes}
    return snapshot


@pytest.mark.integration
@pytest.mark.slow
def test_migrations_059_060_sqlite_round_trip_is_a_pure_no_op(tmp_path):
    db_path = tmp_path / "mig059060.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        _alembic(db_url, "stamp", BOOTSTRAP_BASELINE)
        reference = _schema_snapshot(engine)  # after stamp, so alembic_version is included
        # Pin that the subprocess CLI and this snapshot hit the SAME database --
        # without this, a config URL-selection change could silently point the
        # CLI elsewhere and every equality below would pass vacuously.
        assert "alembic_version" in reference

        # Upgrade traverses 059 AND 060 (proves both are wired into the chain)
        # and must not touch a single table on SQLite.
        _alembic(db_url, "upgrade", REVISION_060)
        assert _schema_snapshot(engine) == reference, "059/060 upgrade mutated the schema on sqlite"
        assert REVISION_060 in _alembic(db_url, "current")

        # Downgrade back to the baseline: equally a guarded no-op.
        _alembic(db_url, "downgrade", BOOTSTRAP_BASELINE)
        assert _schema_snapshot(engine) == reference, "059/060 downgrade mutated the schema on sqlite"
        assert BOOTSTRAP_BASELINE in _alembic(db_url, "current")

        # And the re-upgrade (idempotency of the whole pair).
        _alembic(db_url, "upgrade", REVISION_060)
        assert _schema_snapshot(engine) == reference
    finally:
        engine.dispose()
