"""Coverage for 123_vendor_asl: the Approved Supplier List columns on ``vendors`` plus
``parts.supply_scope``.

Three layers:

1. Script wiring -- single head, the down-revision, the id fits ``alembic_version``.
2. A real upgrade -> replay -> downgrade -> replay round-trip over a scratch SQLite
   database holding minimal ``vendors`` / ``parts`` tables WITH DATA, proving every
   object is guarded independently (a partial bootstrap recovers each missing object),
   the CHECK is enforced, existing rows survive both directions, and the downgrade is real.
3. Model == migration lock-step: column types / nullability, the CHECK text (byte-
   identical), the index columns, and the PostgreSQL DDL actually rendered (offline
   ``as_sql``), so the create_all bootstrap and the incremental path build the same thing.
"""

import importlib.util
import io
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.part import Part
from app.models.purchasing import VENDOR_APPROVAL_STATUS_CHECK, VENDOR_APPROVAL_STATUS_VALUES, Vendor

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "123_vendor_asl.py")
REVISION = "123_vendor_asl"
DOWN_REVISION = "122_car_form_quote_rev"

VENDOR_COLUMNS = {
    "approval_status": sa.String,
    "approved_scopes": sa.JSON,
    "approval_bases": sa.JSON,
    "reevaluation_due": sa.Date,
    "last_performance_evaluation": sa.Date,
    "approval_notes": sa.Text,
}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_123", MIGRATION_FILE)
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
    assert len(REVISION) <= 32


# --------------------------------------------------------------------------- round trip


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'asl-migration.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE vendors (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), "
                "code VARCHAR(50) NOT NULL, name VARCHAR(255) NOT NULL, is_approved BOOLEAN, notes TEXT, "
                "CONSTRAINT uq_vendors_company_code UNIQUE (company_id, code))"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE parts (id INTEGER PRIMARY KEY, part_number VARCHAR(100), standard_cost FLOAT, "
                "CONSTRAINT chk_parts_standard_cost_non_negative CHECK (standard_cost >= 0))"
            )
        )
        connection.execute(sa.text("INSERT INTO companies VALUES (1)"))
        connection.execute(
            sa.text("INSERT INTO vendors VALUES (1, 1, 'VND-001', 'Existing supplier', 1, 'July go-live notes')")
        )
        connection.execute(sa.text("INSERT INTO parts VALUES (1, 'P-1', 2.5)"))
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _vendor_columns(connection):
    return {column["name"] for column in sa.inspect(connection).get_columns("vendors")}


def _objects(connection):
    inspector = sa.inspect(connection)
    return {
        "vendor_columns": _vendor_columns(connection),
        "part_columns": {column["name"] for column in inspector.get_columns("parts")},
        "checks": {check["name"] for check in inspector.get_check_constraints("vendors")},
        "indexes": {index["name"] for index in inspector.get_indexes("vendors")},
    }


def test_upgrade_adds_every_object_and_existing_rows_survive(scratch):
    connection, migration = scratch
    migration.upgrade()
    objects = _objects(connection)
    assert set(VENDOR_COLUMNS) <= objects["vendor_columns"]
    assert "supply_scope" in objects["part_columns"]
    assert "ck_vendors_approval_status" in objects["checks"]
    assert "ix_vendors_company_reevaluation_due" in objects["indexes"]
    # Nothing is backfilled: the existing vendor is "not on the ASL" and keeps its notes / flag.
    row = connection.execute(sa.text("SELECT * FROM vendors WHERE id = 1")).mappings().one()
    assert row["approval_status"] is None and row["approved_scopes"] is None
    assert row["notes"] == "July go-live notes" and row["is_approved"] == 1
    # The other CHECK on parts survives the batch rebuild untouched.
    checks = {c["name"] for c in sa.inspect(connection).get_check_constraints("parts")}
    assert "chk_parts_standard_cost_non_negative" in checks


def test_the_check_admits_every_status_and_null_and_refuses_anything_else(scratch):
    connection, migration = scratch
    migration.upgrade()
    for value in VENDOR_APPROVAL_STATUS_VALUES:
        connection.execute(sa.text("UPDATE vendors SET approval_status = :v WHERE id = 1"), {"v": value})
    connection.execute(sa.text("UPDATE vendors SET approval_status = NULL WHERE id = 1"))
    nested = connection.begin_nested()
    with pytest.raises(IntegrityError):
        connection.execute(sa.text("UPDATE vendors SET approval_status = 'Approved' WHERE id = 1"))
    nested.rollback()


def test_replay_is_a_no_op_and_a_partial_bootstrap_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.execute(
        sa.text("UPDATE vendors SET approval_status = 'approved', approved_scopes = '[\"hardware\"]' WHERE id = 1")
    )
    migration.upgrade()  # replay
    connection.execute(sa.text("DROP INDEX ix_vendors_company_reevaluation_due"))
    migration.upgrade()
    assert "ix_vendors_company_reevaluation_due" in _objects(connection)["indexes"]
    row = connection.execute(sa.text("SELECT approval_status, approved_scopes FROM vendors")).one()
    assert row[0] == "approved" and "hardware" in row[1]


def test_downgrade_is_real_keeps_rows_and_replays_cleanly(scratch):
    connection, migration = scratch
    migration.upgrade()
    migration.downgrade()
    objects = _objects(connection)
    assert not (set(VENDOR_COLUMNS) & objects["vendor_columns"])
    assert "supply_scope" not in objects["part_columns"]
    assert "ck_vendors_approval_status" not in objects["checks"]
    assert "ix_vendors_company_reevaluation_due" not in objects["indexes"]
    migration.downgrade()  # a second downgrade is a no-op, not an error
    row = connection.execute(sa.text("SELECT code, name, notes FROM vendors WHERE id = 1")).one()
    assert tuple(row) == ("VND-001", "Existing supplier", "July go-live notes")
    unique = {u["name"] for u in sa.inspect(connection).get_unique_constraints("vendors")}
    assert "uq_vendors_company_code" in unique
    migration.upgrade()
    assert set(VENDOR_COLUMNS) <= _vendor_columns(connection)


# --------------------------------------------------------------------------- lock-step


def test_check_text_is_byte_identical_to_the_model():
    assert _module().CHECK_CONDITION == VENDOR_APPROVAL_STATUS_CHECK
    model_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Vendor.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert model_checks["ck_vendors_approval_status"] == VENDOR_APPROVAL_STATUS_CHECK


def test_model_columns_match_the_migration():
    module = _module()
    migration_columns = {name: factory() for name, factory in module.VENDOR_COLUMNS}
    assert set(migration_columns) == set(VENDOR_COLUMNS)
    for name, expected_type in VENDOR_COLUMNS.items():
        column = Vendor.__table__.c[name]
        assert column.nullable is True and column.server_default is None, name
        assert isinstance(column.type, expected_type), name
        assert isinstance(migration_columns[name], expected_type), name
    assert Vendor.__table__.c.approval_status.type.length == 40 == migration_columns["approval_status"].length
    assert Vendor.__table__.c.approved_scopes.type.none_as_null is True
    supply_scope = Part.__table__.c.supply_scope
    assert supply_scope.nullable is True and supply_scope.type.length == 60 == module.PART_COLUMN[1]().length
    index = next(ix for ix in Vendor.__table__.indexes if ix.name == "ix_vendors_company_reevaluation_due")
    assert [c.name for c in index.columns] == module.INDEX_COLUMNS == ["company_id", "reevaluation_due"]


def test_postgres_upgrade_and_downgrade_sql():
    output = io.StringIO()
    module = _module()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    module.op = Operations(context)
    module.upgrade()
    sql = " ".join(output.getvalue().split())
    assert "SET LOCAL lock_timeout = '5s'" in sql and "SET LOCAL lock_timeout = DEFAULT" in sql
    assert "ALTER TABLE vendors ADD COLUMN approval_status VARCHAR(40)" in sql
    assert "ALTER TABLE vendors ADD COLUMN approved_scopes JSON" in sql
    assert "ALTER TABLE vendors ADD COLUMN reevaluation_due DATE" in sql
    assert "ALTER TABLE vendors ADD COLUMN approval_notes TEXT" in sql
    assert (
        f"ALTER TABLE vendors ADD CONSTRAINT ck_vendors_approval_status CHECK ({VENDOR_APPROVAL_STATUS_CHECK})" in sql
    )
    assert "CREATE INDEX ix_vendors_company_reevaluation_due ON vendors (company_id, reevaluation_due)" in sql
    assert "ALTER TABLE parts ADD COLUMN supply_scope VARCHAR(60)" in sql
    assert "NOT NULL" not in sql and "DEFAULT '" not in sql
    # Pure DDL: no backfill, no new table (so no RLS statement is owed).
    for forbidden in ("UPDATE ", "INSERT ", "CREATE TABLE", "audit_log"):
        assert forbidden not in sql
    output.seek(0)
    output.truncate(0)
    module.downgrade()
    down = " ".join(output.getvalue().split())
    assert "DROP INDEX ix_vendors_company_reevaluation_due" in down
    assert "ALTER TABLE vendors DROP CONSTRAINT ck_vendors_approval_status" in down
    assert "ALTER TABLE vendors DROP COLUMN approval_status" in down
    assert "ALTER TABLE parts DROP COLUMN supply_scope" in down


def test_create_all_ddl_carries_the_same_check_and_index_on_postgres():
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    Vendor.__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    assert f"CONSTRAINT ck_vendors_approval_status CHECK ({VENDOR_APPROVAL_STATUS_CHECK})" in ddl
    assert "CREATE INDEX ix_vendors_company_reevaluation_due ON vendors (company_id, reevaluation_due)" in ddl
    assert isinstance(postgresql.dialect(), sa.engine.Dialect)


def test_the_asl_fields_are_on_no_vendor_write_schema():
    from app.schemas.part import PartBase, PartCreate, PartUpdate
    from app.schemas.purchasing import VendorCreate, VendorUpdate

    for schema in (VendorCreate, VendorUpdate):
        assert not (set(VENDOR_COLUMNS) & set(schema.model_fields)), schema.__name__
    for schema in (PartBase, PartCreate):
        assert "supply_scope" not in schema.model_fields
    assert "supply_scope" in PartUpdate.model_fields
