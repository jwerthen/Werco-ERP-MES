"""Coverage for 072_notifications_foundation (notification system PR 1).

072 is the DDL-bearing migration for the notification foundation
(docs/NOTIFICATIONS_PLAN.md §5):

- CREATE TABLE ``notifications`` (the per-user in-app inbox row, TenantMixin) +
  its indexes + ``ENABLE ROW LEVEL SECURITY`` (Postgres-only new-table convention).
- ``notification_logs`` += nullable ``notification_id`` FK -> ``notifications.id`` (+ index).
- ``operational_events`` += nullable ``notified_at`` (the transactional-outbox
  idempotency marker) + a plain index for the relay sweeper's ``IS NULL`` scan.
- ``users`` += nullable ``phone`` (String(32)); ``companies`` += ``allow_sms_egress``
  Boolean NOT NULL server_default false.
- DATA (no DDL): a one-time, idempotent JSON normalization that widens every stored
  ``notification_preferences.preferences`` per-event channel dict from
  ``{email, digest}`` to ``{in_app, email, sms, digest}`` by ADDING only the missing
  keys (``in_app`` -> True, ``sms`` -> False); existing values are never overwritten.

Three layers, mirroring the suite's migration-test idioms
(tests/test_migration_070_last_report.py, tests/test_migration_058_process_sheets.py):

1. Script wiring + source/model lock-step (unit).
2. The load-bearing JSON normalization exercised in-process against a real alembic
   ``Operations`` context over a scratch SQLite DB -- proves additive + preserves +
   idempotent + non-dict-tolerant, fast, no subprocess.
3. A real upgrade -> downgrade -> upgrade round-trip over a bootstrapped SQLite DB via
   the alembic CLI (integration/slow), which also proves the JSON widening through the
   actual migration machinery and its idempotency on re-run.
"""

import importlib.util
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "072_notifications_foundation"
MIGRATION_FILE = "072_notifications_foundation.py"
DOWN_REVISION = "071_soft_delete_purchasing_ncr"

NEW_TABLE = "notifications"
NEW_TABLE_INDEXES = [
    "ix_notifications_id",
    "ix_notifications_user_id",
    "ix_notifications_event_key",
    "ix_notifications_company_id",
    "ix_notifications_user_unread",
]
ADDED_COLUMNS = [
    ("notification_logs", "notification_id"),
    ("operational_events", "notified_at"),
    ("users", "phone"),
    ("companies", "allow_sms_egress"),
]


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_072", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"
    assert heads[0] == REVISION, "072 must be the single head"
    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32)
    # (create_all -> stamp -> upgrade bootstrap constraint, docs/DEVELOPMENT.md).
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert callable(module._normalize_notification_preferences)


@pytest.mark.unit
def test_new_table_enables_row_level_security():
    """The one new table follows the deny-by-default RLS new-table convention
    (docs/SUPABASE_SECURITY.md); the 059-gate test enforces this repo-wide, this
    pins it for 072 specifically."""
    module = _load_module()
    source = _source()
    assert "op.create_table" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    # The RLS statement targets the one new table (constant-interpolated in the f-string).
    assert module.NOTIFICATIONS == NEW_TABLE
    assert '{NOTIFICATIONS}" ENABLE ROW LEVEL SECURITY' in source


@pytest.mark.unit
def test_upgrade_is_guarded_and_reversible():
    """Every DDL op is guarded (create_all -> stamp -> upgrade bootstrap no-ops)
    and the downgrade is real, not a stub."""
    module = _load_module()
    assert callable(module._has_table)
    assert callable(module._has_column)
    assert callable(module._has_index)

    source = _source()
    assert "def downgrade() -> None:" in source
    assert "op.drop_table(NOTIFICATIONS)" in source
    assert "op.drop_column(COMPANIES_TABLE, COMPANIES_COLUMN)" in source
    # allow_sms_egress is NOT NULL with a CONSTANT server_default (metadata-only on PG).
    assert 'server_default=sa.text("false")' in source


@pytest.mark.unit
def test_json_widening_is_a_documented_noop_on_downgrade():
    """Reversing the widening is lossy and the extra keys are harmless, so the
    downgrade deliberately does NOT touch the preferences JSON."""
    down_src = _source()
    # The normalization helper is only invoked in upgrade(), never downgrade().
    down_body = down_src[down_src.index("def downgrade") :]
    assert "_normalize_notification_preferences" not in down_body


# ---------------------------------------------------------------------------
# 2. JSON normalization: additive + preserves + idempotent + non-dict-tolerant
# ---------------------------------------------------------------------------


def _scratch_engine(tmp_path):
    import app.models  # noqa: F401 (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig072_json.db'}")
    Base.metadata.create_all(engine)
    return engine


def _seed_pref(engine, *, pref_id: int, user_id: int, company_id: int, preferences: dict) -> None:
    from sqlalchemy.orm import Session

    from app.models.notification import NotificationPreference

    with Session(engine) as s:
        s.add(NotificationPreference(id=pref_id, user_id=user_id, company_id=company_id, preferences=preferences))
        s.commit()


def _read_pref(engine, pref_id: int) -> dict:
    from sqlalchemy.orm import Session

    from app.models.notification import NotificationPreference

    with Session(engine) as s:
        return dict(s.get(NotificationPreference, pref_id).preferences)


def _run_normalization(engine) -> None:
    module = _load_module()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    op_obj = Operations(ctx)
    with Operations.context(op_obj):
        module._normalize_notification_preferences()
    conn.commit()
    conn.close()


@pytest.mark.integration
def test_json_normalization_is_additive_and_preserves_existing(tmp_path):
    engine = _scratch_engine(tmp_path)
    try:
        # Legacy 2-channel shape (email/digest only) + one non-dict junk value.
        _seed_pref(
            engine,
            pref_id=1,
            user_id=1,
            company_id=1,
            preferences={
                "wo.late": {"email": True, "digest": False},
                "ncr.created": {"email": False, "digest": True},
                "junk": "not-a-dict",  # tolerated, skipped
            },
        )
        _run_normalization(engine)

        result = _read_pref(engine, 1)
        # in_app/sms ADDED; email/digest PRESERVED verbatim.
        assert result["wo.late"] == {"email": True, "digest": False, "in_app": True, "sms": False}
        assert result["ncr.created"] == {"email": False, "digest": True, "in_app": True, "sms": False}
        # The non-dict value is untouched (not coerced, not crashed on).
        assert result["junk"] == "not-a-dict"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_json_normalization_is_idempotent_and_does_not_overwrite_widened(tmp_path):
    engine = _scratch_engine(tmp_path)
    try:
        # An already-widened row with a user who OPTED OUT of in_app must stay opted out.
        _seed_pref(
            engine,
            pref_id=1,
            user_id=1,
            company_id=1,
            preferences={"ncr.closed": {"in_app": False, "email": True, "sms": True, "digest": False}},
        )
        _run_normalization(engine)
        first = _read_pref(engine, 1)
        # Running twice changes nothing further (idempotent by construction).
        _run_normalization(engine)
        second = _read_pref(engine, 1)

        assert first == second
        # The user's saved (opted-out) in_app choice is never overwritten to True.
        assert second["ncr.closed"] == {"in_app": False, "email": True, "sms": True, "digest": False}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. Real round-trip: upgrade -> downgrade -> upgrade over a bootstrapped DB
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


def _has_table(engine, table: str) -> bool:
    return sa.inspect(engine).has_table(table)


def _has_column(engine, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(engine).get_columns(table))


def _has_index(engine, table: str, index: str) -> bool:
    return any(ix["name"] == index for ix in sa.inspect(engine).get_indexes(table))


@pytest.mark.integration
@pytest.mark.slow
def test_migration_072_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig072.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
        Base.metadata.create_all(engine)
        for table, column in ADDED_COLUMNS:
            assert _has_column(engine, table, column), f"create_all did not build {table}.{column}"
        assert _has_table(engine, NEW_TABLE)

        # Seed a legacy-shape preference row so the round-trip proves the data
        # normalization through the real migration machinery.
        _seed_pref(
            engine,
            pref_id=1,
            user_id=1,
            company_id=1,
            preferences={"wo.late": {"email": True, "digest": True}},
        )

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: every DDL guard fires (no-op) but
        #    the JSON normalization still runs and widens the seeded row.
        _alembic(db_url, "upgrade", REVISION)
        assert _has_table(engine, NEW_TABLE)
        for idx in NEW_TABLE_INDEXES:
            assert _has_index(engine, NEW_TABLE, idx), f"missing {idx}"
        for table, column in ADDED_COLUMNS:
            assert _has_column(engine, table, column)
        widened = _read_pref(engine, 1)["wo.late"]
        assert widened == {"email": True, "digest": True, "in_app": True, "sms": False}

        # 2. Downgrade: a REAL drop (dialect-neutral DDL, so SQLite exercises it).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_table(engine, NEW_TABLE)
        assert not _has_column(engine, "notification_logs", "notification_id")
        assert not _has_column(engine, "operational_events", "notified_at")
        assert not _has_column(engine, "users", "phone")
        assert not _has_column(engine, "companies", "allow_sms_egress")
        # The JSON widening is a documented no-op on downgrade -> keys survive.
        assert _read_pref(engine, 1)["wo.late"].get("in_app") is True

        # 3. Re-upgrade: everything comes back and the JSON normalization is idempotent.
        _alembic(db_url, "upgrade", REVISION)
        assert _has_table(engine, NEW_TABLE)
        for table, column in ADDED_COLUMNS:
            assert _has_column(engine, table, column)
        assert _read_pref(engine, 1)["wo.late"] == {
            "email": True,
            "digest": True,
            "in_app": True,
            "sms": False,
        }

        # 4. Re-runnability at the DDL level (guards make upgrade() a no-op over a
        #    DB that already has the objects), not just alembic's bookkeeping.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _has_table(engine, NEW_TABLE)
    finally:
        engine.dispose()
