"""Coverage for 083_wo_unit_number (the one-unit-per-work-order build identity).

083 is purely additive over the existing ``work_orders`` table: ONE column plus its
index,

- ``unit_number``  String(50), NULLABLE, no server default
- ``ix_work_orders_unit_number``  plain non-unique index

The behavior is covered in ``tests/api/test_work_order_unit_number.py`` and
``tests/api/test_unit_number_shop_floor_surfaces.py``; this file covers the MIGRATION,
in the three layers the suite already uses (…081, …080, …070, …068):

1. **Script wiring + source/model lock-step** (unit) — a single alembic head, the
   082 -> 083 chain, an id that fits ``alembic_version``'s varchar(32), a guarded
   ADD COLUMN *and* a separately guarded CREATE INDEX (so the create_all -> stamp ->
   upgrade bootstrap path no-ops instead of erroring), a real guarded downgrade, pure
   DDL, no ``create_table`` (so the RLS new-table convention does not apply), and a
   model that declares exactly the column+index the migration builds.

2. **Dialect compilation** (unit) — the DDL is asserted by COMPILING it for
   PostgreSQL and for SQLite rather than by executing it and trusting the result. That
   is the house rule for engine-specific claims (CLAUDE.md: the suite runs on SQLite,
   production is Supabase Postgres), and it is the only way to state "``VARCHAR(50)``,
   nullable, and a NON-UNIQUE index" about the engine that actually runs it.

3. **A real upgrade -> downgrade -> upgrade round-trip** (integration/slow) over a
   disposable SQLite file bootstrapped create_all -> stamp(082). The DDL is
   dialect-neutral, so SQLite exercises it for real, and the round-trip re-runs
   ``upgrade()`` over a DB that ALREADY has the column to prove the guards make it
   idempotent rather than leaning on alembic's version bookkeeping to skip it.

THE TWO THINGS A FUTURE READER WILL TRY TO "FIX"
-----------------------------------------------
* **No UNIQUE constraint.** Tempting, and wrong twice over: most work orders carry no
  unit number at all, and a REWORK work order legitimately names the same unit as the
  original it is re-running. Both directions are asserted.
* **No backfill out of ``notes``.** The value lived in ``work_orders.notes`` before 083
  and this migration deliberately does not go looking for it. An ``UPDATE`` here would
  mutate released quality records with no ``AuditService`` row (invariant 2) and could
  not tell a unit number from any other sentence in a note. Forward-only; the open jobs
  are re-keyed by hand. Being pure DDL is also what makes the migration structurally
  incapable of touching the tamper-evident ``audit_log`` chain.
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

REVISION_083 = "083_wo_unit_number"
MIGRATION_FILE = "083_wo_unit_number.py"
DOWN_REVISION = "082_vendor_active_before_delete"

TABLE = "work_orders"
COLUMN = "unit_number"
INDEX = "ix_work_orders_unit_number"
MAX_LENGTH = 50


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_083", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped.

    The prose names at length every construct the migration deliberately avoids (no
    backfill, no UNIQUE, no RLS statement), so assertions about ABSENT constructs have
    to look at code only or the docstring itself would fail them.
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
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_083)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_083) <= 32


@pytest.mark.unit
def test_module_loads_and_names_exactly_the_objects_it_owns():
    module = _load_module()
    assert module.revision == REVISION_083
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    assert module.TABLE_NAME == TABLE
    assert module.COLUMN_NAME == COLUMN
    assert module.INDEX_NAME == INDEX


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency, a real downgrade, and the two omissions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_column_and_the_index_are_guarded_SEPARATELY():
    """Two guards, not one.

    ``create_all`` builds BOTH from the model, so a single combined guard would still
    no-op the bootstrap path — but a hand-patched database that has the column and not
    the index is a real state (somebody adds the column in a console to unblock a
    deploy), and one guard keyed on the column would then skip the index forever.
    """
    module = _load_module()
    assert callable(module._has_column)
    assert callable(module._has_index)

    upgrade = _body()
    upgrade = upgrade[upgrade.index("def upgrade") : upgrade.index("def downgrade")]
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in upgrade
    assert "if not _has_index(TABLE_NAME, INDEX_NAME):" in upgrade


@pytest.mark.unit
def test_the_column_is_nullable_with_no_server_default():
    """Nullable + no default is what makes this metadata-only on PostgreSQL (no table
    rewrite, no backfill pass) and what makes "most work orders have no unit number"
    representable at all."""
    body = _body()
    assert "nullable=True" in body
    assert "server_default" not in body


@pytest.mark.unit
def test_the_index_is_not_unique():
    """THE assertion that stops someone "tightening" this.

    A rework work order legitimately re-states the unit number of the job it is
    re-running, and nothing stops two planners keying the same unit onto a fit-up WO
    and a weld-out WO. A unique index would refuse both — as a 500 at the moment
    somebody saves, on the one field the floor reads off the wall.
    """
    assert "unique=False" in _body()
    assert "unique=True" not in _body()


@pytest.mark.unit
def test_downgrade_is_real_and_drops_the_index_before_the_column():
    """Not a ``pass`` stub, and the ORDER is load-bearing: SQLite's batch-mode table
    rebuild would otherwise carry a dangling index definition for a column that no
    longer exists."""
    source = _source()
    body = _body()
    assert "def downgrade() -> None:" in source
    downgrade = body[body.index("def downgrade") :]
    assert downgrade.index("drop_index") < downgrade.index("drop_column"), "index must be dropped first"
    assert "if _has_index(TABLE_NAME, INDEX_NAME):" in downgrade
    assert "if not _has_column(TABLE_NAME, COLUMN_NAME):" in downgrade
    assert "with op.batch_alter_table(TABLE_NAME) as batch_op:" in downgrade
    assert "batch_op.drop_column(COLUMN_NAME)" in downgrade
    assert "op.drop_column(TABLE_NAME, COLUMN_NAME)" in downgrade
    assert "\n    pass" not in source


@pytest.mark.unit
def test_no_backfill_and_no_raw_dml():
    """Pure DDL. The value this column holds lived in ``work_orders.notes`` until 083
    and is deliberately NOT parsed out of it: an ``UPDATE`` here would mutate released
    records with no ``AuditService`` row (invariant 2) and could not distinguish a unit
    number from any other text in a note. Pure DDL also makes the migration
    structurally incapable of touching the tamper-evident ``audit_log`` hash chain."""
    body = _body()
    for forbidden in ("op.bulk_insert", "op.execute", "sa.text(", "conn.execute", "UPDATE "):
        assert forbidden not in body, f"083 must be pure DDL; found {forbidden}"


@pytest.mark.unit
def test_column_add_only_so_no_rls_statement_is_needed():
    """083 creates no table, so the ENABLE ROW LEVEL SECURITY new-table convention does
    not apply (the 059 gate keys off ``op.create_table``). ``work_orders`` is an
    EXISTING table that predates 059 and already carries RLS."""
    body = _body()
    assert "op.create_table" not in body
    assert "ENABLE ROW LEVEL SECURITY" not in body


@pytest.mark.unit
def test_the_table_already_carries_tenant_scoping():
    """Invariant 1 is unaffected: 083 adds no new tenant surface. Asserted rather than
    assumed, because "the table already has company_id" is the reason this migration is
    allowed to skip both the RLS statement and a company_id column of its own — and it
    is what keeps the two search paths that now match on ``unit_number`` scopeable."""
    from app.models.work_order import WorkOrder

    company_id = WorkOrder.__table__.columns["company_id"]
    assert company_id.nullable is False
    assert company_id.index is True


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step, asserted by DIALECT-COMPILING the DDL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_declares_one_nullable_indexed_non_unique_varchar():
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns[COLUMN]
    assert isinstance(col.type, sa.String)
    assert col.type.length == MAX_LENGTH
    assert col.nullable is True
    assert col.index is True
    assert col.unique is not True
    assert col.server_default is None
    assert col.default is None, "no python-side default either: absent means absent"


@pytest.mark.unit
def test_the_migration_and_the_model_agree_on_the_column():
    """Lock-step: the create_all path and the upgrade path must build the same column
    (the 042/078/079/080 convention — a migration-only object is how prod ends up
    missing what the model claims exists)."""
    from app.models.work_order import WorkOrder

    body = _body()
    col = WorkOrder.__table__.columns[COLUMN]
    assert f"sa.String(length={MAX_LENGTH})" in body and col.type.length == MAX_LENGTH
    assert "nullable=True" in body and col.nullable is True
    assert f'INDEX_NAME = "{INDEX}"' in _source()
    assert {idx.name for idx in WorkOrder.__table__.indexes if COLUMN in idx.columns} == {INDEX}


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_column_ddl_compiles_the_same_on_both_engines(dialect_name: str):
    """DIALECT-COMPILED, not executed.

    The suite runs on SQLite and production is Supabase Postgres, so an engine-specific
    claim proven by executing SQLite DDL proves nothing about prod (CLAUDE.md ->
    "Why the tests run on SQLite"). Compiling states it for both: a nullable
    ``VARCHAR(50)`` with no DEFAULT clause — the shape that makes the ADD COLUMN
    metadata-only on PostgreSQL 11+ rather than a full table rewrite of every work
    order in the shop.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.work_order import WorkOrder

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    col = WorkOrder.__table__.columns[COLUMN]
    rendered = str(sa.schema.CreateColumn(col).compile(dialect=dialect))

    assert "VARCHAR(50)" in rendered.upper()
    assert "NOT NULL" not in rendered.upper()
    assert "DEFAULT" not in rendered.upper()


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_index_ddl_compiles_as_a_plain_non_unique_index_on_both_engines(dialect_name: str):
    """The other half, same reasoning. ``CREATE INDEX``, never ``CREATE UNIQUE INDEX``
    — see ``test_the_index_is_not_unique`` for why a unique one would be a defect —
    and over exactly the one column, under the name the migration builds."""
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models.work_order import WorkOrder

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    [index] = [idx for idx in WorkOrder.__table__.indexes if idx.name == INDEX]
    rendered = str(sa.schema.CreateIndex(index).compile(dialect=dialect))

    assert "CREATE INDEX" in rendered.upper()
    assert "UNIQUE" not in rendered.upper()
    assert INDEX in rendered
    assert [c.name for c in index.columns] == [COLUMN]


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
        timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _has_column(engine, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(engine).get_columns(table))


def _has_index(engine, table: str, index: str) -> bool:
    return any(i["name"] == index for i in sa.inspect(engine).get_indexes(table))


@pytest.mark.integration
@pytest.mark.slow
def test_migration_083_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig083.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-083 shape straight from the model — both objects.
        assert _has_column(engine, TABLE, COLUMN), "create_all did not build the column"
        assert _has_index(engine, TABLE, INDEX), "create_all did not build the index"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: both guards fire, so this is a clean
        #    no-op and both objects survive untouched.
        _alembic(db_url, "upgrade", REVISION_083)
        assert _has_column(engine, TABLE, COLUMN)
        assert _has_index(engine, TABLE, INDEX)

        # 2. Downgrade: a REAL drop (the DDL is dialect-neutral, so SQLite exercises it).
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)
        assert not _has_index(engine, TABLE, INDEX)
        # Batch mode rebuilds the table -- the neighbouring schema must survive.
        remaining = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
        assert {
            "id",
            "work_order_number",
            "part_id",
            "quantity_ordered",
            "status",
            "lot_number",
            "serial_numbers",
            "notes",
            "sequential_operations",
            "version",
            "company_id",
        } <= remaining

        # 3. Re-upgrade: both objects come back (the un-guarded ADD COLUMN / CREATE INDEX).
        _alembic(db_url, "upgrade", REVISION_083)
        assert _has_column(engine, TABLE, COLUMN)
        assert _has_index(engine, TABLE, INDEX)

        # 4. Re-runnability at the DDL level, not just alembic's bookkeeping: stamp back
        #    and run upgrade() again over a DB that already has both. The guards must
        #    make it a no-op instead of erroring.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_083)
        assert _has_column(engine, TABLE, COLUMN)
        assert _has_index(engine, TABLE, INDEX)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_pre_existing_rows_survive_the_upgrade_with_a_null_unit_number(tmp_path):
    """The data claim, executed rather than read off the source.

    A work order that exists BEFORE the column does must come out of the upgrade with
    ``unit_number IS NULL`` and its ``notes`` byte-for-byte intact — including the
    hand-typed "Unit #" the office used to put there, which 083 deliberately does not
    parse, move or delete. That is the half a source-grep cannot prove, and the half
    that says a re-key is a human decision rather than a migration's guess.
    """
    db_path = tmp_path / "mig083_forward_only.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    legacy_note = "Unit #2410048 -- match to the tag on the fixture. Purge backside."

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all already built the post-083 shape, so reach the PRE-083 shape by
        # stamping the parent, marking 083 applied, and running ITS downgrade -- a bare
        # `downgrade -1` from 082 would run 082's downgrade, a different migration.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION_083)
        _alembic(db_url, "downgrade", "-1")
        assert not _has_column(engine, TABLE, COLUMN)

        with engine.begin() as conn:
            # A production WO needs a part (ck_work_orders_part_required_unless_laser).
            conn.execute(
                sa.text(
                    "INSERT INTO parts (id, part_number, name, part_type, unit_of_measure, company_id) "
                    "VALUES (1, 'PRE-083-PART', 'Weld assembly', 'manufactured', 'each', 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO work_orders "
                    "(work_order_number, work_order_type, part_id, quantity_ordered, status, notes, "
                    "company_id, version) "
                    "VALUES ('PRE-083-WO', 'production', 1, 1, 'released', :notes, 1, 1)"
                ),
                {"notes": legacy_note},
            )

        _alembic(db_url, "upgrade", REVISION_083)

        with engine.begin() as conn:
            row = conn.execute(
                sa.text(f"SELECT {COLUMN}, notes FROM work_orders WHERE work_order_number = 'PRE-083-WO'")
            ).one()
        assert row[0] is None, "a pre-083 work order must come out with NO unit number"
        assert row[1] == legacy_note, "083 must not parse, move or clear the legacy note"
    finally:
        engine.dispose()
