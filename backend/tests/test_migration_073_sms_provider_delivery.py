"""Coverage for 073_sms_provider_delivery (notification system PR 4 -- the SMS channel).

073 is a small, purely ADDITIVE migration: ``notification_logs`` gains two nullable
columns that carry the carrier's own delivery provenance for an SMS row --

- ``provider_message_id`` ``String(64)`` -- the Twilio message SID,
- ``provider_status``     ``String(40)`` -- the provider-reported status string,

both nullable, no server default, no index, no new table (so no ``ENABLE ROW LEVEL
SECURITY`` statement -- ``notification_logs`` is pre-existing and was swept by 059).

The load-bearing assertion in this file is the **absence** of a backfill. Both columns
are provenance fields in a compliance record: every pre-073 ``notification_logs`` row is
an email/in-app delivery attempt that genuinely has no provider message id and no
provider status, so NULL is the only truthful value. Stamping anything there would
fabricate delivery evidence. That is the deliberate contrast with 072, whose
``operational_events.notified_at`` backfill WAS load-bearing (without it the relay
sweeper would have re-dispatched the entire event history as a go-live storm) -- and
which ``test_migration_072_notifications_foundation.py`` asserts in the mirror image of
the test below.

Three layers, mirroring the suite's migration-test idioms (closest precedent:
tests/test_migration_072_notifications_foundation.py):

1. Script wiring + source/model lock-step (unit).
2. A real upgrade -> downgrade -> upgrade round-trip over a bootstrapped SQLite DB via
   the alembic CLI (integration/slow). The re-upgrade is where the guards do NOT fire
   and the genuine ``ADD COLUMN`` runs, so that is where "no backfill" is proven, and
   where the ``notification_logs`` foreign keys (including 072's ``notification_id`` ->
   ``notifications.id``) and indexes are checked to have survived the plain
   ``DROP COLUMN`` / ``ADD COLUMN`` cycle.
3. create_all parity -- the bootstrap path and the migration path converge on the same
   two columns, types, nullability, and (absent) indexes.
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

REVISION = "073_sms_provider_delivery"
MIGRATION_FILE = "073_sms_provider_delivery.py"
DOWN_REVISION = "072_notifications_foundation"

LOGS_TABLE = "notification_logs"
# (column, declared length) -- lock-step with app/models/notification.py::NotificationLog.
ADDED_COLUMNS = [("provider_message_id", 64), ("provider_status", 40)]


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_073", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """The executable source with the module docstring stripped.

    The docstring discusses the RLS convention and the absent backfill at length, so a
    naive substring check over the whole file would match prose rather than code.
    """
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    """One head, and 073 chains directly onto 072.

    Deliberately does NOT assert that 073 IS the head -- 074 will chain onto it, and
    pinning the head here would make the next migration fail this test for no reason
    (the trap the 072 test hit when 073 landed). What matters is the invariant: a
    single head, 073's parent is 072, and 073 is reachable walking down from the head.
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert REVISION in chain, f"073 is not an ancestor of head {heads[0]}"
    assert DOWN_REVISION in chain, "073 must sit above 072 on the same path"


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
    # The guard idiom that makes the create_all-bootstrapped path a clean no-op.
    assert callable(module._has_table)
    assert callable(module._has_column)
    assert module.LOGS_TABLE == LOGS_TABLE


@pytest.mark.unit
def test_declared_columns_are_lock_step_with_the_model():
    """The migration's column list and the SQLAlchemy model must not drift apart."""
    from app.models.notification import NotificationLog

    module = _load_module()
    declared = {name: column_type for name, column_type in module.PROVIDER_COLUMNS}
    assert sorted(declared) == sorted(name for name, _ in ADDED_COLUMNS)

    for name, length in ADDED_COLUMNS:
        assert declared[name].length == length, f"{name}: migration width drifted from the model"
        model_column = NotificationLog.__table__.c[name]
        assert model_column.type.length == length
        assert model_column.nullable is True, f"{name} must stay nullable (additive, no backfill)"
        assert model_column.server_default is None
        assert model_column.index in (None, False), f"{name} declares no index -- see the migration's note"


@pytest.mark.unit
def test_creates_no_table_so_the_rls_convention_does_not_apply():
    """073 only ALTERs a pre-existing table.

    The "every new table needs ENABLE ROW LEVEL SECURITY" convention
    (docs/SUPABASE_SECURITY.md, gated repo-wide by the 059/060 test) is satisfied by
    creating nothing: ``notification_logs`` was already swept by 059, and ALTERing a
    table does not change its RLS state.
    """
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body
    # The repo-wide 059/060 gate keys off the FULL source, so a table-creating edit
    # here would be caught there too; this pins the current shape.
    assert "op.create_table" not in _source()


@pytest.mark.unit
def test_upgrade_is_guarded_and_the_downgrade_is_real():
    source = _body()
    body = source[source.index("def upgrade") :]
    assert "if not _has_column(LOGS_TABLE, column_name):" in body
    assert "def downgrade() -> None:" in body
    # A real drop of both columns, not a `pass` stub.
    assert "op.drop_column(LOGS_TABLE, column_name)" in body
    assert "reversed(PROVIDER_COLUMNS)" in body


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """No backfill, no UPDATE, no INSERT -- provenance is never fabricated.

    Source-level guard so a future edit cannot quietly add "helpful" defaults to
    historical delivery rows; the round-trip below proves the same thing behaviorally.
    """
    source = _body()
    body = source[source.index("def upgrade") :]
    for statement in ("op.execute", "UPDATE ", "INSERT ", "op.bulk_insert"):
        assert statement not in body, f"073 must not run a data statement ({statement!r})"


# ---------------------------------------------------------------------------
# 2. Real round-trip: upgrade -> downgrade -> upgrade over a bootstrapped DB
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


def _has_column(engine, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(engine).get_columns(table))


def _index_names(engine, table: str) -> set:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def _fk_pairs(engine, table: str) -> set:
    """(constrained column, referred table, referred column) triples -- name-independent
    because SQLite does not name inline foreign keys."""
    return {
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in sa.inspect(engine).get_foreign_keys(table)
    }


def _seed_log_row(engine) -> int:
    """A pre-073 delivery-log row: an email attempt with no provider provenance."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO notification_logs (company_id, user_id, event_type, channel, subject, body, sent) "
                "VALUES (1, 1, 'ncr.created', 'email', 'NCR created', 'A new NCR was created.', 1)"
            )
        )
        return conn.execute(sa.text("SELECT max(id) FROM notification_logs")).scalar()


@pytest.mark.integration
@pytest.mark.slow
def test_migration_073_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig073.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
        Base.metadata.create_all(engine)
        for column, _length in ADDED_COLUMNS:
            assert _has_column(engine, LOGS_TABLE, column), f"create_all did not build {LOGS_TABLE}.{column}"

        # Snapshot the surrounding schema so the ADD/DROP cycle can be shown to leave
        # it intact (SQLite's plain DROP COLUMN must not take the FKs/indexes with it).
        baseline_indexes = _index_names(engine, LOGS_TABLE)
        baseline_fks = _fk_pairs(engine, LOGS_TABLE)
        # 072's back-link FK is part of that baseline; pin it explicitly so a silently
        # dropped notification_id FK cannot pass as "unchanged".
        assert (("notification_id",), "notifications", ("id",)) in baseline_fks
        assert (("user_id",), "users", ("id",)) in baseline_fks
        assert "ix_notification_logs_notification_id" in baseline_indexes

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: both guards fire, so this is a
        #    clean no-op rather than a duplicate-column error.
        _alembic(db_url, "upgrade", REVISION)
        for column, _length in ADDED_COLUMNS:
            assert _has_column(engine, LOGS_TABLE, column)
        assert _index_names(engine, LOGS_TABLE) == baseline_indexes
        assert _fk_pairs(engine, LOGS_TABLE) == baseline_fks

        # 2. Downgrade: a REAL plain DROP COLUMN on both (dialect-neutral DDL, so
        #    SQLite genuinely exercises it).
        _alembic(db_url, "downgrade", "-1")
        for column, _length in ADDED_COLUMNS:
            assert not _has_column(engine, LOGS_TABLE, column), f"downgrade left {column} behind"
        # The rest of the table is untouched -- notably 072's notification_id FK.
        assert _index_names(engine, LOGS_TABLE) == baseline_indexes
        assert _fk_pairs(engine, LOGS_TABLE) == baseline_fks

        # Seed a "historical" delivery-log row while both columns are ABSENT -- exactly
        # the pre-073 production state. The re-upgrade below is where the migration
        # genuinely ADDs the columns, which is the only place a backfill could happen.
        historical_id = _seed_log_row(engine)

        # 3. Re-upgrade: the guards do NOT fire, the real ADD COLUMN runs.
        _alembic(db_url, "upgrade", REVISION)
        for column, _length in ADDED_COLUMNS:
            assert _has_column(engine, LOGS_TABLE, column)
        assert _index_names(engine, LOGS_TABLE) == baseline_indexes
        assert _fk_pairs(engine, LOGS_TABLE) == baseline_fks

        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT provider_message_id, provider_status, event_type, sent "
                    "FROM notification_logs WHERE id = :i"
                ),
                {"i": historical_id},
            ).first()
        assert row is not None, "the historical delivery row must survive the round-trip"
        # THE headline assertion: no backfill. A pre-SMS row truthfully has no provider
        # provenance, and inventing one would fabricate delivery evidence in a
        # compliance record. (Contrast 072's notified_at, where the backfill WAS
        # load-bearing -- see that migration's test.)
        assert row[0] is None, "provider_message_id must stay NULL -- 073 must not backfill provenance"
        assert row[1] is None, "provider_status must stay NULL -- 073 must not backfill provenance"
        # The rest of the row is untouched by the ALTER.
        assert row[2] == "ncr.created"
        assert bool(row[3]) is True

        # 4. Re-runnability at the DDL level (the guards make upgrade() a no-op over a
        #    DB that already has the columns), not just alembic's bookkeeping.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        for column, _length in ADDED_COLUMNS:
            assert _has_column(engine, LOGS_TABLE, column)
        with engine.connect() as conn:
            still_null = conn.execute(
                sa.text("SELECT provider_message_id, provider_status FROM notification_logs WHERE id = :i"),
                {"i": historical_id},
            ).first()
        assert still_null == (None, None), "a re-run must not stamp provenance either"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. create_all parity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_and_the_migration_converge(tmp_path):
    """The bootstrap path (create_all) and the migrated path agree on both columns.

    Types, nullability, absent server defaults, and the absence of an index all match;
    only ordinal position differs (create_all emits them where they are declared, while
    ADD COLUMN appends), which autogenerate does not compare.
    """
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig073_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns(LOGS_TABLE)}
        module = _load_module()
        declared = dict(module.PROVIDER_COLUMNS)

        for name, length in ADDED_COLUMNS:
            assert name in columns, f"create_all did not build {name}"
            built = columns[name]
            assert built["nullable"] is True
            assert built.get("default") is None
            assert str(built["type"]).upper() == f"VARCHAR({length})"
            assert str(declared[name].compile(dialect=engine.dialect)).upper() == f"VARCHAR({length})"

        # Neither column is indexed on either side (nothing queries them today).
        indexed = {column for ix in sa.inspect(engine).get_indexes(LOGS_TABLE) for column in ix["column_names"]}
        assert not indexed & {name for name, _ in ADDED_COLUMNS}
    finally:
        engine.dispose()
