"""Rebuild the local SQLite dev database from scratch, correctly.

WHY THIS EXISTS
---------------
The local dev DB drifts. It is created once by ``Base.metadata.create_all()`` on
first boot and then never migrated, so every schema change after that day is
missing -- and because the app only fails when a request happens to touch a
missing column, the symptom is a confusing 500 on some unrelated page rather than
anything that says "your database is old".

A real instance: a dev DB created 2026-07-30 was still in use on 2026-08-22 with
no ``alembic_version`` table at all, missing ``work_orders.unit_number`` (081) and
``part_number_aliases`` (084). It made every authenticated page 500, which meant
weeks of frontend work could not be verified in a browser at all.

THE BOOTSTRAP PATH IS NOT ``alembic upgrade head``
---------------------------------------------------
On an empty database, a bare ``upgrade head`` FAILS -- migration ``002`` does
``ALTER TYPE workcentertype``, and there is no type to alter because no earlier
migration creates the schema (``001`` only adds indexes). The documented path,
the same one production follows, is:

    create_all  ->  alembic stamp 058_process_sheets  ->  alembic upgrade head

``create_all`` builds today's model schema in one shot. Stamping at ``058``
(rather than at ``head``) is deliberate and load-bearing: ``059`` and ``060`` add
things ``create_all`` CANNOT reproduce -- the Supabase RLS hardening and the
``audit_log`` immutability triggers -- so stamping past them would silently skip
them. That exact mistake cost production its audit triggers on 2026-07-07. See
CLAUDE.md -> "Migrations -- handle with care".

USAGE
-----
    python -m scripts.reset_dev_db            # rebuild, keeping a timestamped backup
    python -m scripts.reset_dev_db --no-seed  # schema only, no demo data
    python -m scripts.reset_dev_db --force    # skip the confirmation prompt

Refuses to run against anything that is not a local SQLite file. That guard is not
paranoia: this script DROPS THE DATABASE, and the same ``DATABASE_URL`` env var
that points at ``werco_dev.db`` here points at Supabase Postgres in production.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Stamp here, NOT at head: 059 (RLS) and 060 (audit-log immutability triggers)
# create things create_all cannot, so they must actually RUN.
STAMP_BASELINE = "058_process_sheets"


def _fail(message: str) -> "None":
    print(f"\n  ERROR: {message}\n", file=sys.stderr)
    raise SystemExit(1)


def _resolve_sqlite_path() -> Path:
    """The dev DB file, or a hard stop if DATABASE_URL is not local SQLite."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        env_file = BACKEND_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        _fail("No DATABASE_URL found in the environment or backend/.env.")

    if not url.startswith("sqlite"):
        _fail(
            f"DATABASE_URL is not SQLite ({url.split('://')[0]}://...). This script DROPS the "
            "database and will not run against anything but a local SQLite file."
        )

    # sqlite:///./werco_dev.db  ->  ./werco_dev.db
    raw = url.split("///", 1)[1] if "///" in url else url.split("//", 1)[1]
    if raw.startswith(":memory:") or not raw:
        _fail("DATABASE_URL points at an in-memory SQLite database; nothing to rebuild.")
    path = (BACKEND_DIR / raw).resolve() if not os.path.isabs(raw) else Path(raw)
    return path


def _run(cmd: list, label: str) -> None:
    print(f"  -> {label}")
    result = subprocess.run(cmd, cwd=BACKEND_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-4000:], file=sys.stderr)
        print(result.stderr[-4000:], file=sys.stderr)
        _fail(f"{label} failed (exit {result.returncode})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the local SQLite dev database.")
    parser.add_argument("--no-seed", action="store_true", help="Create the schema but load no demo data.")
    parser.add_argument("--force", action="store_true", help="Do not prompt before deleting.")
    args = parser.parse_args()

    db_path = _resolve_sqlite_path()
    python = sys.executable

    print(f"\nDev database: {db_path}")
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1_000_000
        mtime = datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  exists: {size_mb:.1f} MB, last written {mtime}")
        if not args.force:
            answer = input("  Replace it? A timestamped backup is kept. [y/N] ").strip().lower()
            if answer != "y":
                print("  Aborted; nothing changed.")
                return
        backup = db_path.with_name(f"{db_path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{db_path.suffix}")
        shutil.move(str(db_path), str(backup))
        print(f"  backed up -> {backup.name}")

    # 1) create_all: today's model schema, in one shot.
    print("\nBuilding schema")
    _run(
        [
            python,
            "-c",
            "from app.db.database import Base, engine\n"
            "import app.models  # noqa: F401 -- registers every model on Base.metadata\n"
            "Base.metadata.create_all(bind=engine)\n"
            "print('create_all ok')",
        ],
        "create_all (current model schema)",
    )

    # 2) Stamp at the baseline, NOT head -- see the module docstring.
    _run([python, "-m", "alembic", "stamp", STAMP_BASELINE], f"alembic stamp {STAMP_BASELINE}")

    # 3) Run the migrations create_all cannot reproduce (059 onward).
    _run([python, "-m", "alembic", "upgrade", "head"], "alembic upgrade head")

    if not args.no_seed:
        print("\nSeeding demo data")
        _run([python, "-m", "scripts.seed_data"], "scripts.seed_data")

    # 4) Prove it, rather than assuming. A schema that merely EXISTS is not the
    #    thing that was broken before -- what was broken was that it lagged the
    #    models, so check a column and a table from the most recent migrations.
    print("\nVerifying")
    _run(
        [
            python,
            "-c",
            "import sqlite3, sys\n"
            f"c = sqlite3.connect({str(db_path)!r})\n"
            "ver = c.execute('select version_num from alembic_version').fetchone()\n"
            "tables = {r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}\n"
            "wo_cols = {r[1] for r in c.execute('PRAGMA table_info(work_orders)')}\n"
            "problems = []\n"
            "if not ver: problems.append('no alembic_version row')\n"
            "if 'part_number_aliases' not in tables: problems.append('missing part_number_aliases (084)')\n"
            "if 'unit_number' not in wo_cols: problems.append('missing work_orders.unit_number (083)')\n"
            "if 'sequential_operations' not in wo_cols: problems.append('missing work_orders.sequential_operations (081)')\n"
            "if problems:\n"
            "    print('FAILED: ' + '; '.join(problems)); sys.exit(1)\n"
            "print(f'alembic at {ver[0]}, {len(tables)} tables, recent migrations present')",
        ],
        "schema check",
    )

    print("\nDone. Start the API with the dev server and the app should authenticate cleanly.\n")


if __name__ == "__main__":
    main()
