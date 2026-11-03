"""Run the REAL fresh-bootstrap chain on CI's disposable PostgreSQL.

An empty database is built as ``create_all`` -> ``alembic stamp <STAMP_BASELINE>`` ->
``alembic upgrade head`` (scripts/reset_dev_db.py; CLAUDE.md -> "Bootstrap is not ``alembic
upgrade head``"). tests/test_migration_fresh_bootstrap.py walks that chain on SQLite only, so
every Postgres-only branch above the stamp (RLS, REVOKEs, triggers, ``DO $$`` role guards) ran
nowhere in CI. That is how 093's bare ``REVOKE ... FROM anon, authenticated`` shipped: it aborts
the whole upgrade on any cluster without the Supabase roles. This script runs the chain for real:

1. ``create_all`` through the app engine, then stamp, then ``upgrade head``; the version must be
   the script directory's single head.
2. Re-stamp the baseline and ``upgrade head`` again: it must succeed and change nothing.
3. Every table in ``public`` must have row-level security enabled.
4. ``downgrade 088_api_tokens`` -> ``upgrade head``: runs every 089+ Postgres downgrade branch,
   and must land back on the same schema.

Each step runs in a SUBPROCESS with ``DATABASE_URL`` pointed at a uniquely named scratch
database, which is dropped ``WITH (FORCE)`` afterwards. ``DATABASE_URL``'s own database is
never touched: E2E seeds it next. The app does not simply use ``DATABASE_URL`` (it prefers a
Supabase URL from the environment or ``backend/.env``), so each child re-runs the local-only
guard on the URL the APP resolved, and checks it names the scratch database, before any DDL.

Run from backend/: ``python -m scripts.verify_fresh_bootstrap_postgres`` with ``DATABASE_URL``,
``ENVIRONMENT=test``, ``SECRET_KEY`` and ``REFRESH_TOKEN_SECRET_KEY`` set, as CI's E2E job does.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts._pg_verify import existing_data_api_roles, require_local_test_postgres, scratch_database
from scripts.reset_dev_db import STAMP_BASELINE

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Mirrors ROUND_TRIP_FLOOR in tests/test_migration_fresh_bootstrap.py: 089 on is where every
# migration guards its creates; below it lie known, accepted model-vs-migration differences.
ROUND_TRIP_FLOOR = "088_api_tokens"

SUBPROCESS_TIMEOUT_SECONDS = 600

# Run first in every child: refuse unless the URL the app itself resolved is local and names the
# scratch database passed as argv[1].
CHILD_GUARD = (
    "import sys\n"
    "from app.core.config import settings\n"
    "from scripts._pg_verify import require_local_test_postgres\n"
    "resolved = require_local_test_postgres(settings.SQLALCHEMY_DATABASE_URL)\n"
    "if resolved.database != sys.argv[1]:\n"
    "    raise SystemExit(f'app settings resolved database {resolved.database!r}, not the scratch database')\n"
)

# The create_all step of scripts/reset_dev_db.py: the app engine, built from settings.
CREATE_ALL_CHILD = CHILD_GUARD + (
    "from app.db.database import Base, engine\n"
    "import app.models  # noqa: F401 -- registers every model on Base.metadata\n"
    "Base.metadata.create_all(bind=engine)\n"
)

# ``python -m alembic`` is ``CommandLine(prog='alembic').main()``; this is the same call, behind
# the guard. ``--raiseerr`` makes a failure print its traceback, which names the migration file.
ALEMBIC_CHILD = CHILD_GUARD + "from alembic.config import main\nmain(argv=sys.argv[2:], prog='alembic')\n"

# Alembic logs each step as ``Running upgrade <from> -> <to>`` / ``Running downgrade <from> -> <to>``.
_RUNNING_STEP = re.compile(r"Running (upgrade|downgrade) ([^\s,]*) -> ([^\s,]*)")


class BootstrapFailure(AssertionError):
    pass


def single_head():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise BootstrapFailure(f"expected exactly one alembic head, found {sorted(heads)}")
    return heads[0]


def describe_failure(label, result):
    """Name the revision that was running when the child died, then the log tail.

    An upgrade ``a -> b`` runs ``b``'s ``upgrade()``; a downgrade ``b -> a`` runs ``b``'s
    ``downgrade()``.
    """
    lines = [f"{label} failed (exit {result.returncode})"]
    running = _RUNNING_STEP.findall(result.stderr)
    if running:
        direction, source, target = running[-1]
        ran = target if direction == "upgrade" else source
        lines.append(
            f"FAILING REVISION: {ran} ({direction} {source or '<base>'} -> {target or '<base>'} "
            f"was the last step started, i.e. {ran}'s {direction}())"
        )
    lines += ["--- stderr (tail) ---", *result.stderr.splitlines()[-60:]]
    lines += ["--- stdout (tail) ---", *result.stdout.splitlines()[-20:]]
    return "\n".join(lines)


def run_child(scratch_url, label, code, *args):
    """Run ``code`` in a fresh interpreter with ``DATABASE_URL`` set to the scratch database."""
    result = subprocess.run(
        [sys.executable, "-c", code, scratch_url.database, *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": scratch_url.render_as_string(hide_password=False)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise BootstrapFailure(describe_failure(label, result))
    return result


def alembic(scratch_url, *args):
    """Run one alembic command; return how many revision steps it logged."""
    result = run_child(scratch_url, "alembic " + " ".join(args), ALEMBIC_CHILD, "--raiseerr", *args)
    return len(_RUNNING_STEP.findall(result.stderr))


def snapshot(engine):
    """The schema objects in ``public`` this script compares across runs, plus the version rows."""
    queries = {
        "tables": """SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity, c.relacl::text
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm')""",
        "columns": """SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns WHERE table_schema = 'public'""",
        "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
        "constraints": """SELECT c.relname, k.conname, pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'""",
        "triggers": """SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND NOT t.tgisinternal""",
        "functions": """SELECT p.proname, pg_get_function_identity_arguments(p.oid), md5(p.prosrc)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'""",
        "types": """SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            LEFT JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE n.nspname = 'public' AND t.typtype = 'e' GROUP BY t.typname""",
        "policies": "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'",
        "alembic_version": "SELECT version_num FROM alembic_version",
    }
    with engine.connect() as connection:
        return {name: frozenset(map(tuple, connection.execute(sa.text(sql)).all())) for name, sql in queries.items()}


def schema_diff(before, after):
    lines = []
    for name in before:
        for label, rows in (("lost", before[name] - after[name]), ("gained", after[name] - before[name])):
            lines += [f"  {name} {label}: {row}" for row in sorted(rows, key=repr)[:20]]
    return "\n".join(lines)


def assert_at_head(state, head, when):
    versions = sorted(row[0] for row in state["alembic_version"])
    if versions != [head]:
        raise BootstrapFailure(f"{when}: alembic_version is {versions}, expected the single head {head!r}")


def assert_rls_everywhere(state, when):
    """Every ordinary/partitioned table in ``public`` must have RLS on. No exemption, not even
    ``alembic_version``: 059 enables RLS on every table it finds in ``pg_tables``, which includes
    it, and the Security Advisor lints that table like any other."""
    tables = {row[0]: row[2] for row in state["tables"] if row[1] in ("r", "p")}
    if not tables:
        raise BootstrapFailure(f"{when}: the scratch database has no tables in public")
    without = sorted(table for table, rls in tables.items() if not rls)
    if without:
        raise BootstrapFailure(f"{when}: row-level security is disabled on {without}")
    return len(tables)


def verify():
    started = time.monotonic()
    url = require_local_test_postgres(os.environ["DATABASE_URL"])
    head = single_head()
    with scratch_database(url, "fresh_bootstrap_") as scratch_url:
        engine = sa.create_engine(scratch_url)
        try:
            with engine.connect() as connection:
                server = connection.execute(sa.text("SHOW server_version")).scalar_one()
                present = existing_data_api_roles(connection)
            roles = (
                f"{'/'.join(present)} present, so the role guards' REVOKE branches ran"
                if present
                else "anon/authenticated absent, so the role guards' skip branches ran"
            )

            run_child(scratch_url, "create_all", CREATE_ALL_CHILD)
            alembic(scratch_url, "stamp", STAMP_BASELINE)
            upgraded = alembic(scratch_url, "upgrade", "head")
            built = snapshot(engine)
            assert_at_head(built, head, "after the bootstrap")
            table_count = assert_rls_everywhere(built, "after the bootstrap")

            alembic(scratch_url, "stamp", STAMP_BASELINE)
            alembic(scratch_url, "upgrade", "head")
            rerun = snapshot(engine)
            if rerun != built:
                raise BootstrapFailure("re-stamp + upgrade head changed the schema:\n" + schema_diff(built, rerun))

            downgraded = alembic(scratch_url, "downgrade", ROUND_TRIP_FLOOR)
            floor = snapshot(engine)
            assert_at_head(floor, ROUND_TRIP_FLOOR, f"after downgrade {ROUND_TRIP_FLOOR}")
            # Otherwise "restored the identical schema" below would hold for a downgrade that did nothing.
            dropped = {row[0] for row in built["tables"] if row[1] == "r"} - {row[0] for row in floor["tables"]}
            if not dropped:
                raise BootstrapFailure(f"downgrade {ROUND_TRIP_FLOOR} dropped no tables; the round-trip is vacuous")
            reupgraded = alembic(scratch_url, "upgrade", "head")
            round_trip = snapshot(engine)
            if round_trip != built:
                raise BootstrapFailure(
                    f"downgrade {ROUND_TRIP_FLOOR} -> upgrade head did not restore the schema:\n"
                    + schema_diff(built, round_trip)
                )
        finally:
            engine.dispose()
    print(
        f"PASS: fresh bootstrap on PostgreSQL {server} (scratch database, dropped): create_all -> stamp "
        f"{STAMP_BASELINE} -> upgrade head ran {upgraded} revisions to {head}; re-stamp + upgrade head "
        f"succeeded and left the schema unchanged; row-level security on all {table_count} public tables "
        f"(alembic_version included); downgrade {ROUND_TRIP_FLOOR} ({downgraded} revisions, {len(dropped)} "
        f"tables dropped) -> upgrade head ({reupgraded}) restored the identical schema; {roles}; "
        f"{time.monotonic() - started:.1f}s"
    )


if __name__ == "__main__":
    try:
        verify()
    except BootstrapFailure as failure:
        print(f"FAIL: {failure}", file=sys.stderr)
        raise SystemExit(1)
