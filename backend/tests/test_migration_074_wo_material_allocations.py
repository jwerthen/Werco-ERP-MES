"""Coverage for 074_wo_material_allocations (material-consumption PR 1 -- the tie table).

074 CREATEs one new tenant-scoped table, ``work_order_material_allocations``: the
OPTIONAL tie saying "this work order (or this operation of it) consumes this material
part". It changes nothing that already exists -- no column on ``work_orders``, no
default allocation, no data statement at all. "Not tied" is the ABSENCE of a row, which
is precisely why there is nothing to backfill: no historical work order may gain an
allocation, and an untied work order must keep behaving byte-identically to today.

Three things in this migration are quietly easy to get wrong, so each has a dedicated
test below:

1. **The partial-index predicate literal.** The two unique indexes are predicated on
   ``status = 'OPEN'`` -- the UPPERCASE enum MEMBER NAME, because the columns use a
   plain ``SQLEnum`` (no ``values_callable``) and that form persists ``enum.name``.
   Writing ``'open'`` would create an index whose predicate matches nothing: it builds
   cleanly, it shows up in ``\\d``, and uniqueness is silently never enforced. The
   convention is NOT uniform in this repo (``parttype`` / ``unitofmeasure`` DO use
   ``values_callable`` and store lowercase), so this is asserted three ways -- against
   the source, against what SQLAlchemy actually binds, and behaviorally against a real
   index.
2. **Status is the tombstone.** The table deliberately does NOT use ``SoftDeleteMixin``;
   untie sets ``status = CANCELLED``. A second tombstone would make the partial
   predicates ambiguous, so the absence of ``is_deleted`` is pinned as a contract.
3. **RLS on the new table.** Post-059 the deny-by-default posture requires every
   table-creating migration to ``ENABLE ROW LEVEL SECURITY`` or the Supabase Security
   Advisor re-flags ``rls_disabled_in_public``.

Layers mirror the suite's migration-test idioms (closest precedent:
tests/test_migration_073_sms_provider_delivery.py and ..._072_...):

1. Script wiring + source/model lock-step (unit).
2. A real upgrade -> downgrade -> upgrade round-trip over a bootstrapped SQLite DB via
   the alembic CLI (integration/slow), including a BEHAVIORAL proof that the partial
   unique indexes reject a second OPEN row while tolerating any number of CANCELLED
   ones -- the untie-then-re-tie flow.
3. create_all parity -- the bootstrap path and the migration path converge.
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

REVISION = "074_wo_material_allocations"
MIGRATION_FILE = "074_wo_material_allocations.py"
DOWN_REVISION = "073_sms_provider_delivery"

TABLE = "work_order_material_allocations"

# Every index the migration and create_all must both produce.
EXPECTED_INDEXES = {
    "ix_work_order_material_allocations_id",
    "ix_work_order_material_allocations_work_order_id",
    "ix_work_order_material_allocations_work_order_operation_id",
    "ix_work_order_material_allocations_part_id",
    "ix_work_order_material_allocations_status",
    "ix_work_order_material_allocations_company_id",
    "ix_wo_material_alloc_company_wo",
    "uq_wo_material_alloc_open_op",
    "uq_wo_material_alloc_open_wo",
}
PARTIAL_UNIQUE_INDEXES = {"uq_wo_material_alloc_open_op", "uq_wo_material_alloc_open_wo"}

# (column, nullable) -- lock-step with app/models/work_order_material.py.
EXPECTED_COLUMNS = [
    ("id", False),
    ("work_order_id", False),
    ("work_order_operation_id", True),
    ("part_id", False),
    ("source", False),
    ("status", False),
    ("qty_per_run", True),
    ("qty_planned", False),
    ("unit_of_measure", False),
    ("qty_consumed", False),
    ("pinned_inventory_item_id", True),
    ("pinned_lot_number", True),
    ("notes", True),
    ("created_by", True),
    ("created_at", False),
    ("updated_at", False),
    ("company_id", False),
]


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_074", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """The executable source with the module docstring stripped.

    The docstring discusses backfills, RLS, and the enum literal at length, so a naive
    substring check over the whole file would match prose rather than code.
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
    """One head, and 074 chains directly onto 073.

    Deliberately does NOT assert that 074 IS the head -- 075 chains onto it, and pinning
    the head here would make the next migration fail this test for no reason.
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert REVISION in chain, f"074 is not an ancestor of head {heads[0]}"
    assert DOWN_REVISION in chain, "074 must sit above 073 on the same path"


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
    assert callable(module._has_index)
    assert callable(module._is_postgres)
    assert module.ALLOCATIONS_TABLE == TABLE


# ---------------------------------------------------------------------------
# 1b. Source / model lock-step
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enum_labels_are_lock_step_with_the_model():
    from app.models.work_order_material import (
        ALLOCATION_SOURCE_ENUM_NAME,
        ALLOCATION_STATUS_ENUM_NAME,
        AllocationSource,
        AllocationStatus,
    )

    module = _load_module()
    assert module.SOURCE_ENUM_NAME == ALLOCATION_SOURCE_ENUM_NAME
    assert module.STATUS_ENUM_NAME == ALLOCATION_STATUS_ENUM_NAME
    # The migration's labels must be the UPPERCASE member NAMES -- what SQLAlchemy
    # persists for a plain SQLEnum -- not the lowercase .value strings.
    assert module.SOURCE_ENUM_LABELS == [member.name for member in AllocationSource]
    assert module.STATUS_ENUM_LABELS == [member.name for member in AllocationStatus]
    assert module.SOURCE_ENUM_LABELS == ["NEST", "BOM", "MANUAL"]
    assert module.STATUS_ENUM_LABELS == ["OPEN", "CLOSED", "CANCELLED"]


@pytest.mark.unit
def test_partial_index_predicate_uses_the_literal_sqlalchemy_actually_binds():
    """The predicate literal must equal what the native enum binds for OPEN.

    Not a style preference: with ``SQLEnum(AllocationStatus)`` (no ``values_callable``)
    SQLAlchemy persists ``enum.name``, so the bound value is ``'OPEN'``. A predicate
    written as ``'open'`` matches zero rows and the unique index silently enforces
    nothing. Asserted against the compiled Postgres bind processor rather than against a
    hard-coded string, so this test tracks SQLAlchemy's real behavior.
    """
    from sqlalchemy.dialects import postgresql

    from app.models.work_order_material import OPEN_STATUS_SQL_LITERAL, WorkOrderMaterialAllocation

    status_type = WorkOrderMaterialAllocation.__table__.c.status.type
    pg = postgresql.dialect()
    bind_processor = status_type.dialect_impl(pg).bind_processor(pg)

    from app.models.work_order_material import AllocationStatus

    bound = bind_processor(AllocationStatus.OPEN)
    assert bound == "OPEN", f"SQLAlchemy binds {bound!r} for AllocationStatus.OPEN"
    assert OPEN_STATUS_SQL_LITERAL == bound

    module = _load_module()
    assert module.OPEN_STATUS_SQL_LITERAL == bound, "migration literal drifted from what the enum binds"
    for predicate in (module.OPEN_OPERATION_PREDICATE, module.OPEN_WORK_ORDER_PREDICATE):
        assert f"status = '{bound}'" in predicate
        assert "status = 'open'" not in predicate, "lowercase predicate would silently match nothing"

    # The model's own index predicates must agree with the migration's, byte for byte.
    model_predicates = {
        index.name: str(index.dialect_options["postgresql"]["where"])
        for index in WorkOrderMaterialAllocation.__table__.indexes
        if index.dialect_options["postgresql"].get("where") is not None
    }
    assert model_predicates == {
        "uq_wo_material_alloc_open_op": module.OPEN_OPERATION_PREDICATE,
        "uq_wo_material_alloc_open_wo": module.OPEN_WORK_ORDER_PREDICATE,
    }


@pytest.mark.unit
def test_partial_predicates_are_declared_for_sqlite_too():
    """dev/pytest must get the same semantics as production Postgres.

    SQLite supports partial indexes (3.8.0+). Declaring only ``postgresql_where`` would
    give SQLite a FULL unique index, which would wrongly reject a re-tie once a
    CANCELLED row exists for the same key -- a divergence that only ever surfaces as a
    mystery local failure.
    """
    from app.models.work_order_material import WorkOrderMaterialAllocation

    for index in WorkOrderMaterialAllocation.__table__.indexes:
        if index.name not in PARTIAL_UNIQUE_INDEXES:
            continue
        pg_where = index.dialect_options["postgresql"].get("where")
        sqlite_where = index.dialect_options["sqlite"].get("where")
        assert pg_where is not None, f"{index.name} lost its postgresql_where"
        assert sqlite_where is not None, f"{index.name} must declare sqlite_where too"
        assert str(pg_where) == str(sqlite_where)

    body = _body()
    assert "sqlite_where" in body, "the migration must pass sqlite_where as well"
    assert "postgresql_where" in body


@pytest.mark.unit
def test_status_is_the_tombstone_and_the_table_is_tenant_scoped():
    """No SoftDeleteMixin (status=CANCELLED is the untie), and the TenantMixin shape."""
    from app.db.mixins import SoftDeleteMixin
    from app.models.work_order_material import WorkOrderMaterialAllocation

    assert not issubclass(WorkOrderMaterialAllocation, SoftDeleteMixin)
    columns = WorkOrderMaterialAllocation.__table__.c
    for tombstone in ("is_deleted", "deleted_at", "deleted_by"):
        assert tombstone not in columns, f"{tombstone} would be a second, ambiguous tombstone"

    # TenantMixin shape: non-null, indexed FK to companies.id.
    company_id = columns["company_id"]
    assert company_id.nullable is False
    assert {fk.target_fullname for fk in company_id.foreign_keys} == {"companies.id"}
    assert company_id.index is True
    # company_id leads every index so the tenant-scoped reads are index-backed.
    for index in WorkOrderMaterialAllocation.__table__.indexes:
        assert list(index.columns)[0].name == "company_id" or len(index.columns) == 1

    # The migration also puts company_id LAST (TenantMixin MRO emission order).
    assert EXPECTED_COLUMNS[-1][0] == "company_id"
    body = _body()
    assert body.index('sa.Column("company_id"') > body.index('sa.Column("created_at"')


@pytest.mark.unit
def test_declared_columns_are_lock_step_with_the_model():
    from app.models.work_order_material import WorkOrderMaterialAllocation

    columns = WorkOrderMaterialAllocation.__table__.c
    assert sorted(c.name for c in columns) == sorted(name for name, _ in EXPECTED_COLUMNS)
    for name, nullable in EXPECTED_COLUMNS:
        assert columns[name].nullable is nullable, f"{name} nullability drifted"

    # qty_consumed is a cache with a 0 default; qty_per_run has NO default (NULL on a
    # WO-scoped tie, and readers COALESCE it to 1.0 for an op-scoped one).
    assert columns["qty_consumed"].server_default is not None
    assert columns["qty_per_run"].server_default is None
    assert columns["qty_per_run"].default is None
    # status carries an app-side default only (no server_default), like Notification.severity.
    assert columns["status"].server_default is None

    body = _body()
    for name, _nullable in EXPECTED_COLUMNS:
        assert f'sa.Column("{name}"' in body, f"migration is missing column {name}"


@pytest.mark.unit
def test_migration_declares_every_index_the_model_declares():
    from app.models.work_order_material import WorkOrderMaterialAllocation

    module = _load_module()
    migration_indexes = {name for name, _cols, _unique, _where in module.ALLOCATION_INDEXES}
    assert migration_indexes == EXPECTED_INDEXES

    # Composite/partial indexes come from __table_args__; the single-column ones from
    # index=True (which create_all builds but op.create_table does not).
    declared = {index.name for index in WorkOrderMaterialAllocation.__table__.indexes}
    assert PARTIAL_UNIQUE_INDEXES | {"ix_wo_material_alloc_company_wo"} <= declared

    uniques = {name for name, _cols, unique, _where in module.ALLOCATION_INDEXES if unique}
    assert uniques == PARTIAL_UNIQUE_INDEXES, "only the two OPEN-scoped indexes may be unique"
    for name, _cols, unique, where in module.ALLOCATION_INDEXES:
        assert (where is not None) == unique, f"{name}: uniqueness and a predicate must go together"

    # Postgres identifier limit -- a silently truncated index name breaks the guards.
    for name in migration_indexes:
        assert len(name) <= 63, f"{name} exceeds the Postgres identifier limit"


@pytest.mark.unit
def test_new_table_enables_row_level_security():
    """Post-059 deny-by-default posture (docs/SUPABASE_SECURITY.md).

    Also gated repo-wide by test_migration_059_060_supabase_hardening.py, but pinned
    here with its Postgres guard and its downgrade counterpart.
    """
    body = _body()
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "DISABLE ROW LEVEL SECURITY" in body, "downgrade should defensively disable it"
    assert "_is_postgres(op.get_bind())" in body, "the RLS statement must be Postgres-guarded"
    # Deny-by-default means ZERO policies.
    assert "CREATE POLICY" not in body


@pytest.mark.unit
def test_upgrade_is_guarded_and_the_downgrade_is_real():
    body = _body()
    upgrade = body[body.index("def upgrade") :]
    assert "if not _has_table(ALLOCATIONS_TABLE):" in upgrade
    assert "if _has_index(ALLOCATIONS_TABLE, index_name):" in body
    assert "checkfirst=True" in body, "enum types must be created/dropped idempotently"

    downgrade = body[body.index("def downgrade") :]
    assert "op.drop_table(ALLOCATIONS_TABLE)" in downgrade, "downgrade must not be a stub"
    assert "reversed(ALLOCATION_INDEXES)" in downgrade
    assert "_drop_enums()" in downgrade


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """No backfill, no UPDATE, no INSERT -- "not tied" is the absence of a row.

    Source-level guard so a future edit cannot quietly manufacture allocations for
    historical work orders; the round-trip below proves the same thing behaviorally. The
    only permitted ``op.execute`` calls are the two RLS statements.
    """
    body = _body()
    for statement in ("UPDATE ", "INSERT ", "op.bulk_insert", "DELETE "):
        assert statement not in body, f"074 must not run a data statement ({statement!r})"
    executes = [line for line in body.splitlines() if "op.execute(" in line]
    assert executes, "expected the RLS statements"
    for line in executes:
        assert "ROW LEVEL SECURITY" in line, f"unexpected op.execute: {line.strip()}"


@pytest.mark.unit
def test_migration_touches_no_existing_table():
    """074 must be additive-only: it may CREATE its table, never ALTER another one."""
    body = _body()
    for forbidden in ("op.add_column", "op.drop_column", "op.alter_column", "op.batch_alter_table"):
        assert forbidden not in body, f"074 must not {forbidden} -- it only creates a new table"
    # audit_log is never touched (tamper-evident hash chain).
    assert "audit_log" not in body


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


def _index_names(engine, table: str) -> set:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def _partial_index_sql(engine, name: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name}
        ).scalar()


def _insert_allocation(engine, *, operation_id, status, part_id=7, work_order_id=42, company_id=1):
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {TABLE} "
                "(company_id, work_order_id, work_order_operation_id, part_id, source, status, "
                " qty_per_run, qty_planned, unit_of_measure, qty_consumed) "
                "VALUES (:c, :wo, :op, :p, 'NEST', :s, 1.0, 1.0, 'sheets', 0)"
            ),
            {"c": company_id, "wo": work_order_id, "op": operation_id, "p": part_id, "s": status},
        )


@pytest.mark.integration
@pytest.mark.slow
def test_migration_074_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig074.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
        Base.metadata.create_all(engine)
        assert sa.inspect(engine).has_table(TABLE), "create_all did not build the allocations table"
        assert _index_names(engine, TABLE) == EXPECTED_INDEXES

        # A pre-existing (untied) work-order world: seed a ledger row that must be
        # entirely unaffected by this migration.
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO inventory_transactions "
                    "(company_id, part_id, transaction_type, quantity, reference_type, reference_id, created_by) "
                    "VALUES (1, 7, 'ISSUE', -3, 'work_order', 42, 1)"
                )
            )

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over the bootstrapped schema: every guard fires -> clean no-op
        #    rather than a "table already exists" error.
        _alembic(db_url, "upgrade", REVISION)
        assert _index_names(engine, TABLE) == EXPECTED_INDEXES

        # 2. Downgrade: a REAL drop of the indexes and the table.
        _alembic(db_url, "downgrade", "-1")
        assert not sa.inspect(engine).has_table(TABLE), "downgrade left the table behind"

        # 3. Re-upgrade: the guards do NOT fire, so the genuine CREATE TABLE + nine
        #    CREATE INDEX statements run. This is the only place a backfill could
        #    happen -- and the table must come back EMPTY.
        _alembic(db_url, "upgrade", REVISION)
        assert sa.inspect(engine).has_table(TABLE)
        assert _index_names(engine, TABLE) == EXPECTED_INDEXES

        with engine.connect() as conn:
            allocation_count = conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar()
            ledger = conn.execute(
                sa.text("SELECT reference_type, reference_id, quantity FROM inventory_transactions")
            ).fetchall()
        # THE headline assertion: no backfill. No historical work order gains an
        # allocation, and an untied work order is untied precisely because no row
        # exists for it.
        assert allocation_count == 0, "074 must not manufacture allocations for historical work"
        # The existing ledger is untouched -- 074 does not read or write it at all.
        assert ledger == [("work_order", 42, -3.0)]

        columns = {c["name"]: c for c in sa.inspect(engine).get_columns(TABLE)}
        assert sorted(columns) == sorted(name for name, _ in EXPECTED_COLUMNS)
        for name, nullable in EXPECTED_COLUMNS:
            assert columns[name]["nullable"] is nullable, f"{name} nullability drifted after the round-trip"

        # The migration-built partial indexes really are partial (a FULL unique index
        # would break untie-then-re-tie), with the UPPERCASE literal.
        for name in PARTIAL_UNIQUE_INDEXES:
            ddl = _partial_index_sql(engine, name)
            assert "WHERE" in ddl, f"{name} came back as a FULL unique index"
            assert "status = 'OPEN'" in ddl, f"{name} predicate lost the uppercase enum literal"

        # 4. Re-runnability at the DDL level (guards no-op over an already-built DB).
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _index_names(engine, TABLE) == EXPECTED_INDEXES
        with engine.connect() as conn:
            assert conn.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar() == 0
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_partial_unique_indexes_enforce_one_open_tie_but_allow_re_tie(tmp_path):
    """Behavioral proof of the predicate, on the schema the MIGRATION built.

    ``uq_wo_material_alloc_open_op`` must reject a second OPEN allocation for the same
    (company, operation, part) while tolerating any number of CANCELLED ones -- that is
    the untie-then-re-tie / nest-re-import flow, and it is what the partial predicate
    buys over a plain unique constraint. The WO-scoped index is checked the same way.
    """
    db_path = tmp_path / "mig074_behavior.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        _alembic(db_url, "stamp", DOWN_REVISION)
        # Drop to nothing, then let the MIGRATION rebuild the table, so what is tested
        # below is the migration's DDL rather than create_all's.
        _alembic(db_url, "upgrade", REVISION)
        _alembic(db_url, "downgrade", "-1")
        _alembic(db_url, "upgrade", REVISION)

        # --- operation-scoped ---
        _insert_allocation(engine, operation_id=9, status="CANCELLED")
        _insert_allocation(engine, operation_id=9, status="CANCELLED")
        _insert_allocation(engine, operation_id=9, status="OPEN")
        with pytest.raises(sa.exc.IntegrityError):
            _insert_allocation(engine, operation_id=9, status="OPEN")
        # A different part on the same operation is a different slot.
        _insert_allocation(engine, operation_id=9, status="OPEN", part_id=8)
        # CLOSED never occupies the slot either.
        _insert_allocation(engine, operation_id=9, status="CLOSED")

        # --- work-order-scoped (operation_id IS NULL) ---
        _insert_allocation(engine, operation_id=None, status="CANCELLED", work_order_id=99)
        _insert_allocation(engine, operation_id=None, status="OPEN", work_order_id=99)
        with pytest.raises(sa.exc.IntegrityError):
            _insert_allocation(engine, operation_id=None, status="OPEN", work_order_id=99)
        # Another tenant with identical ids is never blocked -- the indexes are
        # company-scoped, so cross-tenant collisions are impossible by construction.
        _insert_allocation(engine, operation_id=None, status="OPEN", work_order_id=99, company_id=2)

        with engine.connect() as conn:
            open_rows = conn.execute(sa.text(f"SELECT count(*) FROM {TABLE} WHERE status = 'OPEN'")).scalar()
        assert open_rows == 4
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. create_all parity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_all_and_the_migration_converge(tmp_path):
    """The bootstrap path (create_all) and the migrated path agree on the table.

    Same columns, nullability, and index set -- including both partial predicates. Only
    ordinal position could differ, which autogenerate does not compare.
    """
    import app.models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig074_parity.db'}")
    try:
        Base.metadata.create_all(engine)
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns(TABLE)}
        assert sorted(columns) == sorted(name for name, _ in EXPECTED_COLUMNS)
        for name, nullable in EXPECTED_COLUMNS:
            assert columns[name]["nullable"] is nullable

        assert _index_names(engine, TABLE) == EXPECTED_INDEXES
        for name in PARTIAL_UNIQUE_INDEXES:
            ddl = _partial_index_sql(engine, name)
            assert "WHERE" in ddl and "status = 'OPEN'" in ddl

        # The enum columns render with the UPPERCASE member names on every dialect.
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"INSERT INTO {TABLE} (company_id, work_order_id, part_id, source, status, "
                    "qty_planned, unit_of_measure, qty_consumed) "
                    "VALUES (1, 1, 1, 'MANUAL', 'OPEN', 2.5, 'each', 0)"
                )
            )
        from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation

        with sa.orm.Session(engine) as session:
            row = session.query(WorkOrderMaterialAllocation).one()
            assert row.status is AllocationStatus.OPEN, "the ORM must round-trip the uppercase label"
            assert row.qty_consumed == 0.0
            assert row.qty_per_run is None
    finally:
        engine.dispose()
