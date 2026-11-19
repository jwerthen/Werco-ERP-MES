"""Coverage for 128_po_flowdown: ``po_flowdown_profiles``, ``po_quality_clauses`` and the two
``purchase_orders`` columns (``quality_clause_codes``, ``flowdown_snapshot``).

Four layers:

1. Script wiring -- one head, the down-revision, the id is the file stem and fits ``alembic_version``.
2. A real upgrade -> replay -> downgrade -> replay round trip over a scratch SQLite database holding
   pre-existing companies, users and purchase orders (one of them soft-deleted): every object is
   guarded independently (a partially bootstrapped table recovers its unique constraint, its foreign
   keys and each index; each ``purchase_orders`` column is added on its own), nothing is back-filled,
   the constraints are enforced, and the downgrade leaves every purchase order -- its soft-delete
   columns included -- and the rest of the schema alone.
3. Model == migration lock-step: columns (type, nullability, server default), index names and
   columns, unique-constraint names and columns, and the unnamed foreign keys, table by table, plus
   the two ``purchase_orders`` columns.
4. The PostgreSQL posture, rendered offline: ENABLE ROW LEVEL SECURITY and the REVOKEs on both new
   tables and their sequences, the ``anon`` / ``authenticated`` ones under a ``pg_roles`` check, in
   BOTH the migration and the models' ``after_create`` listeners -- and in the migration even when
   the tables already exist (the create_all path), because RLS must never sit under a table guard.
"""

import importlib.util
import io
import json
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.db.mixins import OptimisticLockMixin
from app.models import po_flowdown as flowdown_models
from app.models.po_flowdown import POFlowdownProfile, POQualityClause
from app.models.purchasing import PurchaseOrder

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "128_po_flowdown.py")
REVISION = "128_po_flowdown"
DOWN_REVISION = "127_ncr_car_records"
MODELS = {"po_flowdown_profiles": POFlowdownProfile, "po_quality_clauses": POQualityClause}
TABLES = tuple(MODELS)
PO_COLUMNS = ("quality_clause_codes", "flowdown_snapshot")

EXPECTED_INDEXES = {
    "po_flowdown_profiles": {"ix_po_flowdown_profiles_company_id": ("company_id",)},
    "po_quality_clauses": {"ix_po_quality_clauses_company_id": ("company_id",)},
}
EXPECTED_UNIQUES = {
    "po_flowdown_profiles": {"uq_po_flowdown_profiles_company": ("company_id",)},
    "po_quality_clauses": {"uq_po_quality_clauses_company_code": ("company_id", "code")},
}
EXPECTED_FKS = {
    "po_flowdown_profiles": {(("updated_by",), "users"), (("company_id",), "companies")},
    "po_quality_clauses": {(("created_by",), "users"), (("updated_by",), "users"), (("company_id",), "companies")},
}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_128", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


# --------------------------------------------------------------------------- wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# --------------------------------------------------------------------------- round trip

# The pre-128 ``purchase_orders`` as far as this revision cares: a soft-deletable tenant table with
# its business key, indexes and FKs, which the downgrade's SQLite batch rebuild must carry across.
PRE_128 = (
    "CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE purchase_orders (id INTEGER NOT NULL, po_number VARCHAR(50) NOT NULL, status VARCHAR(9), "
    "notes TEXT, created_by INTEGER, is_deleted BOOLEAN DEFAULT 'false' NOT NULL, deleted_at DATETIME, "
    "deleted_by INTEGER, company_id INTEGER NOT NULL, PRIMARY KEY (id), "
    "CONSTRAINT uq_purchase_orders_company_po_number UNIQUE (company_id, po_number), "
    "FOREIGN KEY(created_by) REFERENCES users (id), FOREIGN KEY(company_id) REFERENCES companies (id))",
    "CREATE INDEX ix_purchase_orders_is_deleted ON purchase_orders (is_deleted)",
    "CREATE INDEX ix_purchase_orders_company_id ON purchase_orders (company_id)",
    "CREATE INDEX ix_purchase_orders_status ON purchase_orders (status)",
    "INSERT INTO companies VALUES (1, 'Existing company'), (2, 'Other company')",
    "INSERT INTO users VALUES (1, 'Existing user'), (2, 'Other user')",
    "INSERT INTO purchase_orders (id, po_number, status, notes, created_by, is_deleted, company_id) "
    "VALUES (1, 'PO-1001', 'DRAFT', 'still a draft', 1, 'false', 1), "
    "(2, 'PO-1002', 'SENT', 'issued before 128', 1, 'false', 1)",
    "INSERT INTO purchase_orders (id, po_number, status, is_deleted, deleted_at, deleted_by, company_id) "
    "VALUES (3, 'PO-0999', 'CANCELLED', 'true', '2026-11-01 10:00:00', 2, 2)",
)
PRE_128_PO_SHAPE = {
    "columns": ("id", "po_number", "status", "notes", "created_by", "is_deleted", "deleted_at", "deleted_by"),
    "indexes": {
        "ix_purchase_orders_is_deleted": ("is_deleted",),
        "ix_purchase_orders_company_id": ("company_id",),
        "ix_purchase_orders_status": ("status",),
    },
    "uniques": {"uq_purchase_orders_company_po_number": ("company_id", "po_number")},
    "fks": {(("created_by",), "users"), (("company_id",), "companies")},
}
PO_ROWS_SQL = (
    "SELECT id, po_number, status, notes, created_by, is_deleted, deleted_at, deleted_by, company_id "
    "FROM purchase_orders ORDER BY id"
)
NOW = "2026-11-19 12:00:00"
SNAPSHOT = {"enabled": True, "heading": "Synthetic heading", "clauses": [{"code": "ZQ1", "title": "x", "body": None}]}


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'po-flowdown-migration.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        for statement in PRE_128:
            connection.execute(sa.text(statement))
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _columns(connection, table):
    return {column["name"]: column for column in sa.inspect(connection).get_columns(table)}


def _indexes(connection, table):
    return {row["name"]: tuple(row["column_names"]) for row in sa.inspect(connection).get_indexes(table)}


def _uniques(connection, table):
    return {row["name"]: tuple(row["column_names"]) for row in sa.inspect(connection).get_unique_constraints(table)}


def _fks(connection, table):
    return {
        (tuple(fk["constrained_columns"]), fk["referred_table"])
        for fk in sa.inspect(connection).get_foreign_keys(table)
    }


def _po_rows(connection):
    return [tuple(row) for row in connection.execute(sa.text(PO_ROWS_SQL))]


def _assert_pre_128_purchase_orders_intact(connection, rows_before):
    """The batch rebuild (downgrade) or the ADD COLUMN (upgrade) left every row, the soft-delete
    trio, the business key, every index and every FK on ``purchase_orders`` alone."""
    assert set(PRE_128_PO_SHAPE["columns"]) <= set(_columns(connection, "purchase_orders"))
    assert _indexes(connection, "purchase_orders") == PRE_128_PO_SHAPE["indexes"]
    assert _uniques(connection, "purchase_orders") == PRE_128_PO_SHAPE["uniques"]
    assert _fks(connection, "purchase_orders") == PRE_128_PO_SHAPE["fks"]
    assert _po_rows(connection) == rows_before


def _assert_full_shape(connection):
    for table in TABLES:
        assert _indexes(connection, table) == EXPECTED_INDEXES[table], table
        assert _uniques(connection, table) == EXPECTED_UNIQUES[table], table
        assert _fks(connection, table) == EXPECTED_FKS[table], table
        assert all(fk["referred_columns"] == ["id"] for fk in sa.inspect(connection).get_foreign_keys(table))
    po_columns = _columns(connection, "purchase_orders")
    for name in PO_COLUMNS:
        assert po_columns[name]["nullable"] is True, name
        assert po_columns[name]["default"] is None, name
        assert str(po_columns[name]["type"]) == "JSON", name


def _seed(connection):
    connection.execute(
        sa.text(
            "INSERT INTO po_flowdown_profiles (id, enabled, document_reference, body, updated_by, created_at, "
            "updated_at, company_id) VALUES (1, 1, 'SYN-100 Rev A', 'Synthetic paragraph', 1, :now, :now, 1)"
        ),
        {"now": NOW},
    )
    connection.execute(
        sa.text(
            "INSERT INTO po_quality_clauses (id, code, title, body, created_by, created_at, updated_at, company_id) "
            "VALUES (1, 'ZQ1', 'Synthetic clause', 'Synthetic body', 1, :now, :now, 1)"
        ),
        {"now": NOW},
    )
    connection.execute(
        sa.text("UPDATE purchase_orders SET quality_clause_codes = :codes, flowdown_snapshot = :snap WHERE id = 2"),
        {"codes": json.dumps(["ZQ1"]), "snap": json.dumps(SNAPSHOT)},
    )


def test_upgrade_replay_downgrade_reupgrade(scratch):
    connection, migration = scratch
    rows_before = _po_rows(connection)

    migration.upgrade()
    _assert_full_shape(connection)
    _assert_pre_128_purchase_orders_intact(connection, rows_before)
    # No backfill: every existing PO -- the issued one and the soft-deleted one included -- reads
    # NULL in both new columns (catalog defaults / issued before 128, no block), and the new tables
    # start empty.
    assert connection.execute(
        sa.text("SELECT count(*) FROM purchase_orders WHERE quality_clause_codes IS NULL AND flowdown_snapshot IS NULL")
    ).scalar() == len(rows_before)
    for table in TABLES:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

    _seed(connection)
    migration.upgrade()  # replay over the full schema is a no-op
    _assert_full_shape(connection)
    # The server defaults: plain strings on SQLite, exactly as create_all writes them.
    profile = connection.execute(sa.text("SELECT enabled, heading, version FROM po_flowdown_profiles")).one()
    assert tuple(profile) == (1, "Supplier Quality Requirements", 1)
    clause = connection.execute(sa.text("SELECT default_selected, is_active, sort_order FROM po_quality_clauses")).one()
    assert tuple(clause) == ("false", "true", 0)
    stored = connection.execute(sa.text("SELECT flowdown_snapshot FROM purchase_orders WHERE id = 2")).scalar()
    assert json.loads(stored) == SNAPSHOT

    migration.downgrade()
    migration.downgrade()  # a second downgrade is a no-op, not an error
    remaining = set(sa.inspect(connection).get_table_names())
    assert remaining == {"companies", "users", "purchase_orders"}
    assert not set(PO_COLUMNS) & set(_columns(connection, "purchase_orders"))
    _assert_pre_128_purchase_orders_intact(connection, rows_before)
    assert connection.execute(sa.text("SELECT name FROM companies WHERE id = 1")).scalar() == "Existing company"
    assert connection.execute(sa.text("SELECT name FROM users WHERE id = 1")).scalar() == "Existing user"

    migration.upgrade()
    _assert_full_shape(connection)
    _assert_pre_128_purchase_orders_intact(connection, rows_before)
    for table in TABLES:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0
    assert (
        connection.execute(sa.text("SELECT count(*) FROM purchase_orders WHERE flowdown_snapshot IS NOT NULL")).scalar()
        == 0
    )


@pytest.mark.parametrize("present", PO_COLUMNS)
def test_each_purchase_order_column_is_added_on_its_own(scratch, present):
    # A partial run: one column is there (with a value in it), the other is not.
    connection, migration = scratch
    connection.execute(sa.text(f"ALTER TABLE purchase_orders ADD COLUMN {present} JSON"))
    connection.execute(sa.text(f"UPDATE purchase_orders SET {present} = '[]' WHERE id = 1"))
    migration.upgrade()
    _assert_full_shape(connection)
    assert connection.execute(sa.text(f"SELECT {present} FROM purchase_orders WHERE id = 1")).scalar() == "[]"


def test_each_missing_index_is_recovered_on_its_own(scratch):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    for table in TABLES:
        for index_name in EXPECTED_INDEXES[table]:
            connection.execute(sa.text(f"DROP INDEX {index_name}"))
            migration.upgrade()
            assert index_name in _indexes(connection, table)
    _assert_full_shape(connection)
    assert connection.execute(sa.text("SELECT count(*) FROM po_quality_clauses")).scalar() == 1


def test_a_partially_bootstrapped_table_gets_its_unique_and_foreign_keys(scratch):
    # The table exists (an older model ran create_all) without its business key and FKs.
    connection, migration = scratch
    connection.execute(
        sa.text(
            "CREATE TABLE po_quality_clauses (id INTEGER NOT NULL, code VARCHAR(20) NOT NULL, "
            "title VARCHAR(255) NOT NULL, body TEXT, default_selected BOOLEAN DEFAULT 'false' NOT NULL, "
            "is_active BOOLEAN DEFAULT 'true' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, "
            "created_by INTEGER, updated_by INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "company_id INTEGER NOT NULL, PRIMARY KEY (id))"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO po_quality_clauses (id, code, title, created_at, updated_at, company_id) "
            "VALUES (7, 'ZQ7', 'Synthetic clause', :now, :now, 1)"
        ),
        {"now": NOW},
    )
    migration.upgrade()
    _assert_full_shape(connection)
    # A recovered FK carries the name PostgreSQL would have generated for the unnamed one.
    recovered = {fk["name"] for fk in sa.inspect(connection).get_foreign_keys("po_quality_clauses")}
    assert recovered == {
        "po_quality_clauses_created_by_fkey",
        "po_quality_clauses_updated_by_fkey",
        "po_quality_clauses_company_id_fkey",
    }
    assert connection.execute(sa.text("SELECT code FROM po_quality_clauses WHERE id = 7")).scalar() == "ZQ7"
    migration.upgrade()  # and the recovered table replays cleanly
    _assert_full_shape(connection)


@pytest.mark.parametrize(
    "statement",
    [
        # One profile per company.
        "INSERT INTO po_flowdown_profiles (created_at, updated_at, company_id) VALUES (:now, :now, 1)",
        # One clause code per company.
        "INSERT INTO po_quality_clauses (code, title, created_at, updated_at, company_id) "
        "VALUES ('ZQ1', 'Again', :now, :now, 1)",
        # Required values.
        "INSERT INTO po_flowdown_profiles (created_at, updated_at) VALUES (:now, :now)",
        "INSERT INTO po_flowdown_profiles (heading, created_at, updated_at, company_id) VALUES (NULL, :now, :now, 2)",
        "INSERT INTO po_flowdown_profiles (updated_at, company_id) VALUES (:now, 2)",
        "INSERT INTO po_quality_clauses (title, created_at, updated_at, company_id) VALUES ('x', :now, :now, 1)",
        "INSERT INTO po_quality_clauses (code, created_at, updated_at, company_id) VALUES ('ZQ2', :now, :now, 1)",
        "INSERT INTO po_quality_clauses (code, title, created_at, updated_at) VALUES ('ZQ2', 'x', :now, :now)",
        "INSERT INTO po_quality_clauses (code, title, is_active, created_at, updated_at, company_id) "
        "VALUES ('ZQ2', 'x', NULL, :now, :now, 1)",
        # Foreign keys.
        "INSERT INTO po_flowdown_profiles (created_at, updated_at, company_id) VALUES (:now, :now, 999)",
        "INSERT INTO po_flowdown_profiles (updated_by, created_at, updated_at, company_id) VALUES (999, :now, :now, 2)",
        "INSERT INTO po_quality_clauses (code, title, created_by, created_at, updated_at, company_id) "
        "VALUES ('ZQ2', 'x', 999, :now, :now, 1)",
        "INSERT INTO po_quality_clauses (code, title, updated_by, created_at, updated_at, company_id) "
        "VALUES ('ZQ2', 'x', 999, :now, :now, 1)",
        "INSERT INTO po_quality_clauses (code, title, created_at, updated_at, company_id) "
        "VALUES ('ZQ2', 'x', :now, :now, 999)",
    ],
)
def test_constraints_are_enforced(scratch, statement):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.text(statement), {"now": NOW})


def test_the_business_keys_are_per_company(scratch):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    for statement in (
        # The other company gets its own profile, and may reuse a code company 1 already has.
        "INSERT INTO po_flowdown_profiles (created_at, updated_at, company_id) VALUES (:now, :now, 2)",
        "INSERT INTO po_quality_clauses (code, title, created_at, updated_at, company_id) "
        "VALUES ('ZQ1', 'Same code, other company', :now, :now, 2)",
        "INSERT INTO po_quality_clauses (code, title, created_at, updated_at, company_id) "
        "VALUES ('ZQ2', 'Another code, same company', :now, :now, 1)",
    ):
        connection.execute(sa.text(statement), {"now": NOW})
    assert connection.execute(sa.text("SELECT count(*) FROM po_flowdown_profiles")).scalar() == 2
    assert connection.execute(sa.text("SELECT count(*) FROM po_quality_clauses")).scalar() == 3


# --------------------------------------------------------------------------- lock-step


@pytest.mark.parametrize("table", TABLES)
def test_the_migrated_table_matches_the_model(scratch, table):
    connection, migration = scratch
    migration.upgrade()
    inspector = sa.inspect(connection)
    model = MODELS[table].__table__
    reflected = _columns(connection, table)
    assert set(reflected) == set(model.columns.keys())
    for column in model.columns:
        assert reflected[column.name]["nullable"] == column.nullable, column.name
        assert str(reflected[column.name]["type"]) == str(column.type), column.name
        if column.server_default is not None:
            assert str(reflected[column.name]["default"]).strip("'") == str(column.server_default.arg), column.name
        else:
            assert reflected[column.name]["default"] is None, column.name
    assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
    assert _indexes(connection, table) == {
        index.name: tuple(column.name for column in index.columns) for index in model.indexes
    }
    assert all(not index.unique for index in model.indexes)
    assert _uniques(connection, table) == {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert not [c for c in model.constraints if isinstance(c, sa.CheckConstraint)]
    assert _fks(connection, table) == {
        (tuple(fk.parent.name for fk in constraint.elements), constraint.referred_table.name)
        for constraint in model.foreign_key_constraints
    }
    # Unnamed on both sides, so PostgreSQL names them identically on either bootstrap path.
    assert {fk["name"] for fk in inspector.get_foreign_keys(table)} == {None}
    assert {constraint.name for constraint in model.foreign_key_constraints} == {None}


def test_the_purchase_order_columns_match_the_model(scratch):
    connection, migration = scratch
    migration.upgrade()
    reflected = _columns(connection, "purchase_orders")
    for name in PO_COLUMNS:
        column = PurchaseOrder.__table__.c[name]
        assert column.nullable is True and column.server_default is None, name
        assert not column.foreign_keys and not column.index and not column.unique, name
        assert str(reflected[name]["type"]) == str(column.type) == "JSON", name


def test_the_model_declares_the_expected_names():
    for table, model in MODELS.items():
        assert {index.name: tuple(c.name for c in index.columns) for index in model.__table__.indexes} == (
            EXPECTED_INDEXES[table]
        )
        # Business keys are per-company UniqueConstraints, never a column-level unique=True.
        assert not [column.name for column in model.__table__.columns if column.unique]
        company_id = model.__table__.c.company_id
        assert company_id.nullable is False and company_id.index is True
        assert {fk.target_fullname for fk in company_id.foreign_keys} == {"companies.id"}


def test_the_profile_keeps_the_mixin_version_and_overrides_its_updated_at():
    profile = POFlowdownProfile.__table__
    mixin_version = OptimisticLockMixin.__dict__["version"].fget(OptimisticLockMixin)
    version = profile.c.version
    assert (
        (str(version.type), version.nullable, version.server_default.arg, version.default.arg)
        == (
            str(mixin_version.type),
            mixin_version.nullable,
            mixin_version.server_default.arg,
            mixin_version.default.arg,
        )
        == ("INTEGER", False, "1", 1)
    )
    # The mixin's updated_at carries server_default='now()', a quoted string PostgreSQL folds into
    # the CREATE TABLE instant; the profile declares its own, with a Python-side default only.
    assert OptimisticLockMixin.__dict__["updated_at"].fget(OptimisticLockMixin).server_default.arg == "now()"
    for model in MODELS.values():
        for name in ("created_at", "updated_at"):
            column = model.__table__.c[name]
            assert column.server_default is None, (model.__tablename__, name)
            assert column.nullable is False and column.default is not None, (model.__tablename__, name)
    assert POQualityClause.__table__.c.updated_at.onupdate is not None
    assert profile.c.updated_at.onupdate is not None


def test_security_sql_is_byte_identical_in_the_migration_and_the_model():
    migration = _module()
    for table in TABLES:
        assert migration.security_sql(table) == flowdown_models.security_sql(table)


# --------------------------------------------------------------------------- PostgreSQL posture


def _offline_sql(direction: str, migration=None) -> str:
    output = io.StringIO()
    migration = migration or _module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    getattr(migration, direction)()
    return output.getvalue()


def _model_statements(model) -> list:
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    model.__table__.create(engine, checkfirst=False)
    return statements


def _assert_default_deny(sql: str, table: str) -> None:
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
    for role in ("PUBLIC", "anon", "authenticated"):
        assert f"REVOKE ALL ON TABLE {table} FROM {role}" in sql
        assert f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}" in sql


def test_postgres_upgrade_and_model_bootstrap_are_default_deny():
    migration_sql = _offline_sql("upgrade")
    for table, model in MODELS.items():
        model_sql = "\n".join(_model_statements(model))
        for sql in (migration_sql, model_sql):
            _assert_default_deny(sql, table)
            for role in ("anon", "authenticated"):
                assert f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')" in sql
            assert "CREATE POLICY" not in sql and "service_role" not in sql and "auth.uid()" not in sql
            assert "now()" not in sql
    assert "CONSTRAINT uq_po_flowdown_profiles_company UNIQUE (company_id)" in migration_sql
    assert "CONSTRAINT uq_po_quality_clauses_company_code UNIQUE (company_id, code)" in migration_sql
    assert "heading VARCHAR(255) DEFAULT 'Supplier Quality Requirements' NOT NULL" in migration_sql
    assert "enabled BOOLEAN DEFAULT 'false' NOT NULL" in migration_sql
    assert "version INTEGER DEFAULT '1' NOT NULL" in migration_sql
    assert "is_active BOOLEAN DEFAULT 'true' NOT NULL" in migration_sql
    assert "sort_order INTEGER DEFAULT '0' NOT NULL" in migration_sql
    assert "CREATE INDEX ix_po_flowdown_profiles_company_id ON po_flowdown_profiles (company_id)" in migration_sql
    assert "CREATE INDEX ix_po_quality_clauses_company_id ON po_quality_clauses (company_id)" in migration_sql
    for name in PO_COLUMNS:
        assert f"ALTER TABLE purchase_orders ADD COLUMN {name} JSON" in migration_sql
    # purchase_orders has had RLS since 059: nothing here re-secures or re-grants it.
    assert "ALTER TABLE purchase_orders ENABLE" not in migration_sql
    assert "ON TABLE purchase_orders" not in migration_sql
    # No backfill and no audit-chain contact.
    assert "INSERT INTO" not in migration_sql and "UPDATE " not in migration_sql
    assert "audit_log" not in migration_sql and "DELETE" not in migration_sql
    assert "CHECK" not in migration_sql
    assert migration_sql.index("SET LOCAL lock_timeout = '5s'") < migration_sql.index("CREATE TABLE")
    assert migration_sql.rstrip().rstrip(";").endswith("SET LOCAL lock_timeout = DEFAULT")


def test_the_model_listeners_emit_exactly_the_migration_security_statements():
    migration = _module()
    for table, model in MODELS.items():
        emitted = [s for s in _model_statements(model) if "ROW LEVEL SECURITY" in s or "REVOKE" in s]
        assert emitted == list(migration.security_sql(table)), table


def test_security_sql_runs_even_when_create_all_already_built_the_tables():
    """RLS / REVOKE never sit under a table guard: on the create_all path every object already
    exists, every create no-ops, and the security DDL must still be issued."""
    migration = _module()
    migration._has_table = lambda table_name: True
    migration._has_column = lambda table_name, column_name: True
    migration._has_index = lambda table_name, index_name: True
    migration._has_unique = lambda table_name, constraint_name: True
    migration._has_foreign_key = lambda table_name, columns, referred_table: True
    migration._offline = lambda: False
    sql = _offline_sql("upgrade", migration)
    assert "CREATE TABLE" not in sql and "CREATE INDEX" not in sql and "ADD CONSTRAINT" not in sql
    assert "ADD COLUMN" not in sql
    for table in TABLES:
        _assert_default_deny(sql, table)


def test_postgres_downgrade_drops_the_columns_then_the_tables():
    sql = _offline_sql("downgrade")
    positions = [
        sql.index("ALTER TABLE purchase_orders DROP COLUMN flowdown_snapshot"),
        sql.index("ALTER TABLE purchase_orders DROP COLUMN quality_clause_codes"),
        sql.index("DROP TABLE po_quality_clauses"),
        sql.index("DROP TABLE po_flowdown_profiles"),
    ]
    assert positions == sorted(positions)
    assert "audit_log" not in sql and "DELETE" not in sql and "UPDATE " not in sql
    # Soft-delete columns on purchase_orders are untouched.
    assert "is_deleted" not in sql and "deleted_at" not in sql and "deleted_by" not in sql
    assert "SET LOCAL lock_timeout = '5s'" in sql


# --------------------------------------------------------------------------- model behavior


def test_model_defaults(db_session):
    profile = POFlowdownProfile(company_id=1)
    clause = POQualityClause(company_id=1, code="ZQ1", title="Synthetic clause")
    db_session.add_all([profile, clause])
    db_session.flush()
    assert (profile.enabled, profile.heading, profile.version) == (False, "Supplier Quality Requirements", 1)
    assert profile.document_reference is None and profile.body is None and profile.updated_by is None
    assert profile.created_at is not None and profile.updated_at is not None
    assert (clause.default_selected, clause.is_active, clause.sort_order) == (False, True, 0)
    assert clause.created_at is not None and clause.updated_at is not None
