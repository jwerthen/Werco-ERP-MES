"""Coverage for 129_owned_property (the AS9100D 8.5.3 ownership marker on ``inventory_items`` and
``tools``: ``ownership`` / ``owner_customer_id`` / ``owner_vendor_id``, two named FKs, two CHECKs
and the ``(company_id, ownership)`` index on each).

129 is purely additive over two EXISTING tenant tables -- one NOT NULL column with a constant
server default, two nullable FK columns, two CHECKs and an index per table; no table, no data. This
file covers the MIGRATION (the ownership behavior is tested with the services and the API):

1. **Script wiring** -- single head, the down_revision, an id that is the file stem and fits
   ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- every column (name, type compiled for PostgreSQL,
   nullability, the plain ``'company'`` server default), both FK names, targets and (absent)
   ``ON DELETE``, both CHECK texts (byte-identical to the models, to the PR contract and to
   ``OwnershipType``) and the index are exactly what ``InventoryItem`` and ``Tool`` declare (the
   stamped-over hazard).
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database holding
   legacy rows, child tables and a trigger whose body reads ``inventory_items`` (104's
   ``tr_stock_observation_insert`` shape, which a plain SQLite rebuild cannot survive): every
   existing row reads ``'company'`` with no owner (no backfill beyond the server default), a
   pre-129 insert still lands as company stock, the rows, the trigger and every legacy column /
   index / constraint survive the rebuild in both directions, both CHECKs refuse a
   mismatched owner, the FKs point at ``customers`` / ``vendors``, a partially built schema
   recovers each missing object independently, and the downgrade removes exactly what the upgrade
   added (losing the owner, as the docstring warns).
4. **The create_all bootstrap is a no-op** -- over a schema the models already built, the upgrade
   emits nothing.
5. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions: the
   lock_timeout taken LOCAL and handed back, the metadata-only ``ADD COLUMN ... DEFAULT 'company'
   NOT NULL``, ``inventory_items`` before ``tools`` up and the reverse down, no backfill, no
   ``ON DELETE`` and no RLS / role statement; and the models' own PostgreSQL DDL carries the same
   column, FKs, CHECKs and index.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  -- registers every table the two models reference
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.inventory import OWNER_MATCHES_CHECK, OWNERSHIP_VALUE_CHECK, InventoryItem, OwnershipType
from app.models.tool_management import Tool

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "129_owned_property.py")
REVISION = "129_owned_property"
DOWN_REVISION = "128_po_flowdown"

pytestmark = pytest.mark.unit

MODELS = {"inventory_items": InventoryItem, "tools": Tool}
TABLES = tuple(MODELS)

# The PR contract's text, copied here independently of both the migration and the models.
CONTRACT_OWNERSHIP_CHECK = "ownership IN ('company', 'customer', 'supplier')"
CONTRACT_OWNER_MATCHES_CHECK = (
    "(ownership = 'company' AND owner_customer_id IS NULL AND owner_vendor_id IS NULL) OR "
    "(ownership = 'customer' AND owner_customer_id IS NOT NULL AND owner_vendor_id IS NULL) OR "
    "(ownership = 'supplier' AND owner_vendor_id IS NOT NULL AND owner_customer_id IS NULL)"
)
NEW_COLUMNS = ("ownership", "owner_customer_id", "owner_vendor_id")


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_129", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


def _ws(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _expected_names(table: str) -> dict:
    return {
        "fks": {
            f"fk_{table}_owner_customer_id": ("customers", "owner_customer_id"),
            f"fk_{table}_owner_vendor_id": ("vendors", "owner_vendor_id"),
        },
        "checks": {
            f"ck_{table}_ownership": CONTRACT_OWNERSHIP_CHECK,
            f"ck_{table}_owner_matches": CONTRACT_OWNER_MATCHES_CHECK,
        },
        "index": (f"ix_{table}_ownership", ("company_id", "ownership")),
    }


def _model_checks(table: sa.Table) -> dict:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name
    }


def _model_fks(table: sa.Table) -> dict:
    return {
        fk.constraint.name: (fk.column.table.name, fk.parent.name, fk.ondelete, fk.onupdate)
        for column in table.c
        for fk in column.foreign_keys
        if fk.constraint.name
    }


# ---------------------------------------------------------------------------- 1. wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# ------------------------------------------------------------------- 2. model lock-step


def test_the_migration_declares_the_contract():
    module = _module()
    assert module.COLUMN_NAMES == NEW_COLUMNS
    assert module.OWNERSHIP_CHECK_SQL == CONTRACT_OWNERSHIP_CHECK
    assert module.OWNER_MATCHES_CHECK_SQL == CONTRACT_OWNER_MATCHES_CHECK
    assert [change.table for change in module.TABLE_CHANGES] == list(TABLES)
    for change in module.TABLE_CHANGES:
        expected = _expected_names(change.table)
        assert {name: (referred, local) for name, referred, local in change.foreign_keys} == expected["fks"]
        assert dict(change.checks) == expected["checks"]
        assert change.index == expected["index"]
    columns = {column.name: column for column in module._columns()}
    assert {name: _pg(column.type) for name, column in columns.items()} == {
        "ownership": "VARCHAR(20)",
        "owner_customer_id": "INTEGER",
        "owner_vendor_id": "INTEGER",
    }
    assert columns["ownership"].nullable is False
    # A plain string, never sa.text: the fresh-bootstrap diff compares the rendered DEFAULT.
    assert columns["ownership"].server_default.arg == "company"
    for name in ("owner_customer_id", "owner_vendor_id"):
        assert columns[name].nullable is True and columns[name].server_default is None, name


def test_the_ownership_check_spells_exactly_the_enum():
    assert [member.value for member in OwnershipType] == ["company", "customer", "supplier"]
    rendered = "ownership IN (" + ", ".join(f"'{member.value}'" for member in OwnershipType) + ")"
    assert rendered == CONTRACT_OWNERSHIP_CHECK == _module().OWNERSHIP_CHECK_SQL


@pytest.mark.parametrize("table", TABLES)
def test_every_column_matches_the_model(table):
    module = _module()
    model_table = MODELS[table].__table__
    for column in module._columns():
        model_column = model_table.c[column.name]
        assert _pg(model_column.type) == _pg(column.type), column.name
        assert model_column.nullable == column.nullable, column.name
        if column.server_default is None:
            assert model_column.server_default is None, column.name
        else:
            assert isinstance(model_column.server_default.arg, str), column.name
            assert model_column.server_default.arg == column.server_default.arg == "company", column.name
    ownership = model_table.c.ownership
    assert ownership.default is not None and ownership.default.arg == "company"
    for name in ("owner_customer_id", "owner_vendor_id"):
        assert model_table.c[name].default is None, name


@pytest.mark.parametrize("table", TABLES)
def test_foreign_keys_match_the_model(table):
    model_fks = _model_fks(MODELS[table].__table__)
    for name, (referred_table, local_column) in _expected_names(table)["fks"].items():
        # NO ACTION: an owner still named on a row cannot be physically deleted out from under it.
        assert model_fks[name] == (referred_table, local_column, None, None), name


@pytest.mark.parametrize("table", TABLES)
def test_check_texts_are_byte_identical_to_the_model_and_the_contract(table):
    module = _module()
    model_checks = _model_checks(MODELS[table].__table__)
    migration_checks = dict(next(change for change in module.TABLE_CHANGES if change.table == table).checks)
    for name, contract_text in _expected_names(table)["checks"].items():
        assert model_checks[name] == migration_checks[name] == contract_text, name


def test_the_models_shared_check_constants_equal_the_migration_literals():
    module = _module()
    assert OWNERSHIP_VALUE_CHECK == module.OWNERSHIP_CHECK_SQL == CONTRACT_OWNERSHIP_CHECK
    assert OWNER_MATCHES_CHECK == module.OWNER_MATCHES_CHECK_SQL == CONTRACT_OWNER_MATCHES_CHECK


def test_both_tables_carry_the_same_check_texts():
    inventory = _model_checks(InventoryItem.__table__)
    tools = _model_checks(Tool.__table__)
    for suffix in ("ownership", "owner_matches"):
        assert inventory[f"ck_inventory_items_{suffix}"] == tools[f"ck_tools_{suffix}"], suffix
    # The pre-existing CHECK on inventory_items is still declared alongside 129's.
    assert "chk_inventory_items_allocated_non_negative" in inventory


@pytest.mark.parametrize("table", TABLES)
def test_the_index_matches_the_model(table):
    index_name, index_columns = _expected_names(table)["index"]
    model_indexes = {index.name: index for index in MODELS[table].__table__.indexes}
    assert tuple(column.name for column in model_indexes[index_name].columns) == index_columns
    assert not model_indexes[index_name].unique
    # The TenantMixin company_id index is untouched, and nothing else indexes the owner columns.
    assert f"ix_{table}_company_id" in model_indexes
    for index in model_indexes.values():
        assert not {"owner_customer_id", "owner_vendor_id"} & {column.name for column in index.columns}


# ------------------------------------------------------------- 3. real SQLite round-trip

ALLOCATED_CHECK_SQL = "quantity_allocated >= 0"

# The shape of 104's ``tr_stock_observation_insert``: a trigger ON another table whose BODY reads
# ``inventory_items``. SQLite 3.26+ re-parses it when a batch rebuild renames the copy into place,
# which fails ("no such table: main.inventory_items") unless the rebuild runs under
# ``PRAGMA legacy_alter_table = ON``.
OBSERVATION_TRIGGER = "tr_stock_observation_insert"
OBSERVATION_TRIGGER_SQL = (
    "CREATE TRIGGER tr_stock_observation_insert BEFORE INSERT ON stock_piece_observations\n"
    "BEGIN\n"
    " SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM inventory_items AS i\n"
    " WHERE i.id=NEW.source_inventory_item_id AND i.company_id=NEW.company_id)\n"
    " THEN RAISE(ABORT, 'Recorded source must match the tenant inventory item.') END;\n"
    "END"
)

# Synthetic names only: the repository is public.
LEGACY_DDL = [
    "CREATE TABLE companies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE parts (id INTEGER PRIMARY KEY)",
    "CREATE TABLE work_centers (id INTEGER PRIMARY KEY)",
    (
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, "
        "is_deleted BOOLEAN NOT NULL DEFAULT 0)"
    ),
    (
        "CREATE TABLE vendors (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, "
        "is_deleted BOOLEAN NOT NULL DEFAULT 0)"
    ),
    (
        "CREATE TABLE inventory_items (id INTEGER NOT NULL, part_id INTEGER NOT NULL, location VARCHAR(100) NOT NULL, "
        "warehouse VARCHAR(50), quantity_on_hand FLOAT, quantity_allocated FLOAT, lot_number VARCHAR(100), "
        "supplier_id INTEGER, unit_cost FLOAT, status VARCHAR(50), is_active BOOLEAN, company_id INTEGER NOT NULL, "
        "PRIMARY KEY (id), "
        f"CONSTRAINT chk_inventory_items_allocated_non_negative CHECK ({ALLOCATED_CHECK_SQL}), "
        "FOREIGN KEY(part_id) REFERENCES parts (id), "
        "CONSTRAINT fk_inventory_items_supplier FOREIGN KEY(supplier_id) REFERENCES vendors (id) ON DELETE SET NULL, "
        "FOREIGN KEY(company_id) REFERENCES companies (id))"
    ),
    "CREATE INDEX ix_inventory_items_part_active ON inventory_items (part_id, is_active)",
    "CREATE INDEX ix_inventory_items_company_id ON inventory_items (company_id)",
    "CREATE INDEX ix_inventory_items_lot_number ON inventory_items (lot_number)",
    (
        "CREATE TABLE tools (id INTEGER NOT NULL, tool_id VARCHAR(50) NOT NULL, name VARCHAR(255) NOT NULL, "
        "tool_type VARCHAR(10), status VARCHAR(11), current_work_center_id INTEGER, current_user_id INTEGER, "
        "is_active BOOLEAN, company_id INTEGER NOT NULL, PRIMARY KEY (id), "
        "CONSTRAINT uq_tools_company_tool_id UNIQUE (company_id, tool_id), "
        "FOREIGN KEY(current_work_center_id) REFERENCES work_centers (id), "
        "FOREIGN KEY(current_user_id) REFERENCES users (id), FOREIGN KEY(company_id) REFERENCES companies (id))"
    ),
    "CREATE INDEX ix_tools_tool_id ON tools (tool_id)",
    "CREATE INDEX ix_tools_company_id ON tools (company_id)",
    # Children: the SQLite rebuild must leave the rows that reference both tables alone.
    (
        "CREATE TABLE inventory_transactions (id INTEGER PRIMARY KEY, "
        "inventory_item_id INTEGER REFERENCES inventory_items (id), quantity FLOAT)"
    ),
    "CREATE TABLE tool_checkouts (id INTEGER PRIMARY KEY, tool_id INTEGER NOT NULL REFERENCES tools (id))",
    (
        "CREATE TABLE stock_piece_observations (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
        "source_inventory_item_id INTEGER NOT NULL)"
    ),
    OBSERVATION_TRIGGER_SQL,
    "INSERT INTO companies VALUES (1), (2)",
    "INSERT INTO users VALUES (5)",
    "INSERT INTO parts VALUES (1)",
    "INSERT INTO work_centers VALUES (3)",
    "INSERT INTO customers VALUES (10, 1, 'Synthetic customer', 0), (11, 2, 'Other tenant customer', 0)",
    "INSERT INTO vendors VALUES (20, 1, 'Synthetic vendor', 0)",
    "INSERT INTO inventory_items VALUES (1, 1, 'A-01', 'MAIN', 10, 0, 'LOT-1', 20, 2.5, 'available', 1, 1)",
    "INSERT INTO inventory_items VALUES (2, 1, 'B-02', 'MAIN', 0, 0, 'LOT-2', NULL, 0, 'on_hold', 0, 1)",
    "INSERT INTO tools VALUES (1, 'T-100', 'Synthetic fixture', 'FIXTURE', 'AVAILABLE', 3, 5, 1, 1)",
    "INSERT INTO tools VALUES (2, 'T-101', 'Retired gauge', 'GAUGE', 'RETIRED', NULL, NULL, 0, 2)",
    "INSERT INTO inventory_transactions VALUES (1, 1, 10.0), (2, 2, -4.0)",
    "INSERT INTO tool_checkouts VALUES (1, 1)",
    "INSERT INTO stock_piece_observations VALUES (1, 1, 1)",
]

LEGACY_ROWS_SQL = {
    "inventory_items": (
        "SELECT id, part_id, location, warehouse, quantity_on_hand, quantity_allocated, lot_number, supplier_id, "
        "unit_cost, status, is_active, company_id FROM inventory_items ORDER BY id"
    ),
    "tools": (
        "SELECT id, tool_id, name, tool_type, status, current_work_center_id, current_user_id, is_active, company_id "
        "FROM tools ORDER BY id"
    ),
}
LEGACY_ROWS = {
    "inventory_items": [
        (1, 1, "A-01", "MAIN", 10.0, 0.0, "LOT-1", 20, 2.5, "available", 1, 1),
        (2, 1, "B-02", "MAIN", 0.0, 0.0, "LOT-2", None, 0.0, "on_hold", 0, 1),
    ],
    "tools": [
        (1, "T-100", "Synthetic fixture", "FIXTURE", "AVAILABLE", 3, 5, 1, 1),
        (2, "T-101", "Retired gauge", "GAUGE", "RETIRED", None, None, 0, 2),
    ],
}
OWNERSHIP_SQL = "SELECT ownership, owner_customer_id, owner_vendor_id FROM {table} ORDER BY id"


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm129.db'}")
    with engine.begin() as connection:
        for statement in LEGACY_DDL:
            connection.exec_driver_sql(statement)
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _objects(connection, table):
    inspector = sa.inspect(connection)
    return {
        "columns": {column["name"] for column in inspector.get_columns(table)},
        "fks": sorted(
            (
                fk["name"] or "",
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                tuple(sorted((fk.get("options") or {}).items())),
            )
            for fk in inspector.get_foreign_keys(table)
        ),
        "checks": {ck["name"]: _ws(ck["sqltext"]) for ck in inspector.get_check_constraints(table)},
        "indexes": {ix["name"]: (tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table)},
        "uniques": {uq["name"]: tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)},
    }


def _new_fk_rows(table):
    return sorted(
        (name, (local,), referred, ("id",), ()) for name, (referred, local) in _expected_names(table)["fks"].items()
    )


def _assert_legacy_rows_intact(connection):
    for table, sql in LEGACY_ROWS_SQL.items():
        assert [tuple(row) for row in connection.exec_driver_sql(sql)] == LEGACY_ROWS[table], table
    children = connection.exec_driver_sql("SELECT id, inventory_item_id, quantity FROM inventory_transactions")
    assert [tuple(row) for row in children] == [(1, 1, 10.0), (2, 2, -4.0)]
    assert [tuple(row) for row in connection.exec_driver_sql("SELECT id, tool_id FROM tool_checkouts")] == [(1, 1)]
    # The trigger that reads inventory_items is still there, its stored text byte-identical, and the
    # connection's legacy_alter_table is back to what it was (OFF): nothing outlives the migration.
    triggers = connection.exec_driver_sql("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'")
    assert [tuple(row) for row in triggers] == [
        (OBSERVATION_TRIGGER, "stock_piece_observations", OBSERVATION_TRIGGER_SQL)
    ]
    observations = connection.exec_driver_sql(
        "SELECT id, company_id, source_inventory_item_id FROM stock_piece_observations"
    )
    assert [tuple(row) for row in observations] == [(1, 1, 1)]
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0


def _ownership(connection, table):
    return [tuple(row) for row in connection.exec_driver_sql(OWNERSHIP_SQL.format(table=table))]


def test_upgrade_adds_every_object_and_backfills_nothing(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in TABLES}
    migration.upgrade()

    for table in TABLES:
        objects, before, expected = _objects(connection, table), legacy[table], _expected_names(table)
        assert objects["columns"] == before["columns"] | set(NEW_COLUMNS), table
        assert objects["fks"] == sorted(before["fks"] + _new_fk_rows(table)), table
        assert objects["checks"] == {**before["checks"], **expected["checks"]}, table
        index_name, index_columns = expected["index"]
        assert objects["indexes"] == {**before["indexes"], index_name: (index_columns, False)}, table
        # Everything that was already there is carried across the SQLite rebuild.
        assert objects["uniques"] == before["uniques"], table
        reflected = {column["name"]: column for column in sa.inspect(connection).get_columns(table)}
        assert reflected["ownership"]["nullable"] is False
        assert reflected["ownership"]["default"] == "'company'"
        assert str(reflected["ownership"]["type"]) == "VARCHAR(20)"
        for name in ("owner_customer_id", "owner_vendor_id"):
            assert reflected[name]["nullable"] is True and reflected[name]["default"] is None, name
    assert legacy["inventory_items"]["checks"] == {"chk_inventory_items_allocated_non_negative": ALLOCATED_CHECK_SQL}
    _assert_legacy_rows_intact(connection)

    # No backfill beyond the server default: every existing row -- the inactive / retired / other
    # tenant's ones included -- is company property with no owner, which is what it always meant.
    for table in TABLES:
        assert _ownership(connection, table) == [("company", None, None)] * len(LEGACY_ROWS[table]), table


def test_a_pre_129_insert_still_lands_as_company_stock(scratch):
    """Old code never names the columns: the server default makes its rows company-owned, and the
    owner-match CHECK admits them -- which is what makes a stamp rollback safe for inserts."""
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql("INSERT INTO inventory_items (id, part_id, location, company_id) VALUES (3, 1, 'C', 1)")
    connection.exec_driver_sql("INSERT INTO tools (id, tool_id, name, company_id) VALUES (3, 'T-102', 'New', 1)")
    for table in TABLES:
        assert connection.exec_driver_sql(f"{OWNERSHIP_SQL.format(table=table)}").fetchall()[-1] == (
            "company",
            None,
            None,
        )


ACCEPTED = [("company", None, None), ("customer", 10, None), ("supplier", None, 20)]
REFUSED = [
    # A company row names no owner.
    ("company", 10, None),
    ("company", None, 20),
    ("company", 10, 20),
    # A customer row names its customer, and only its customer.
    ("customer", None, None),
    ("customer", None, 20),
    ("customer", 10, 20),
    # A supplier row names its vendor, and only its vendor.
    ("supplier", None, None),
    ("supplier", 10, None),
    ("supplier", 10, 20),
    # Only the three kinds, spelled exactly; and never NULL.
    ("Customer", 10, None),
    ("consignment", None, None),
    ("", None, None),
    (None, None, None),
]
UPDATE_OWNERSHIP_SQL = (
    "UPDATE {table} SET ownership = :ownership, owner_customer_id = :customer, owner_vendor_id = :vendor WHERE id = :id"
)


@pytest.mark.parametrize("table", TABLES)
def test_the_checks_admit_each_kind_with_its_owner_and_refuse_a_mismatch(scratch, table):
    connection, migration = scratch
    migration.upgrade()
    update = sa.text(UPDATE_OWNERSHIP_SQL.format(table=table))
    for ownership, customer, vendor in ACCEPTED:
        assert ownership in {member.value for member in OwnershipType}
        connection.execute(update, {"id": 1, "ownership": ownership, "customer": customer, "vendor": vendor})
        assert _ownership(connection, table)[0] == (ownership, customer, vendor)
    for ownership, customer, vendor in REFUSED:
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(update, {"id": 1, "ownership": ownership, "customer": customer, "vendor": vendor})
        nested.rollback()
    # An insert that names an owner but not the kind takes the 'company' default and is refused too:
    # no writer can mark a row owned by accident.
    nested = connection.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        if table == "inventory_items":
            connection.exec_driver_sql(
                "INSERT INTO inventory_items (part_id, location, company_id, owner_customer_id) VALUES (1, 'X', 1, 10)"
            )
        else:
            connection.exec_driver_sql(
                "INSERT INTO tools (tool_id, name, company_id, owner_vendor_id) VALUES ('T-9', 'X', 1, 20)"
            )
    nested.rollback()
    assert _ownership(connection, table)[0] == ACCEPTED[-1]


@pytest.mark.parametrize("table", TABLES)
def test_the_new_foreign_keys_point_at_customers_and_vendors(scratch, table):
    connection, migration = scratch
    migration.upgrade()
    reflected = {fk["name"]: fk for fk in sa.inspect(connection).get_foreign_keys(table) if fk["name"]}
    for name, (referred_table, local_column) in _expected_names(table)["fks"].items():
        assert reflected[name]["referred_table"] == referred_table
        assert reflected[name]["constrained_columns"] == [local_column]
        assert reflected[name]["referred_columns"] == ["id"]
        assert not reflected[name].get("options"), name  # no ON DELETE / ON UPDATE
    # SQLite enforces FKs only under a pragma, but ``foreign_key_check`` reads the declared FKs
    # regardless: a dangling owner is reported against the right parent table.
    update = sa.text(UPDATE_OWNERSHIP_SQL.format(table=table))
    connection.execute(update, {"id": 1, "ownership": "customer", "customer": 999, "vendor": None})
    connection.execute(update, {"id": 2, "ownership": "supplier", "customer": None, "vendor": 998})
    violations = {(row[0], row[1], row[2]) for row in connection.exec_driver_sql(f"PRAGMA foreign_key_check({table})")}
    assert violations == {(table, 1, "customers"), (table, 2, "vendors")}


def test_rerun_is_a_no_op_and_a_partial_schema_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql("UPDATE inventory_items SET ownership = 'customer', owner_customer_id = 10 WHERE id = 1")
    connection.exec_driver_sql("UPDATE tools SET ownership = 'customer', owner_customer_id = 10 WHERE id = 1")
    full = {table: _objects(connection, table) for table in TABLES}
    migration.upgrade()  # a re-run is a guarded no-op
    assert {table: _objects(connection, table) for table in TABLES} == full

    # Knock out one object of each kind -- the index, a CHECK and an FK on inventory_items; both
    # CHECKs, an FK and its column on tools -- and prove each guard restores only what is missing.
    # The knock-out rebuilds go through the migration's own ``op`` and its legacy_alter_table
    # wrapper: a bare rebuild of inventory_items dies on the observation trigger.
    ops = migration.op
    ops.drop_index("ix_inventory_items_ownership", table_name="inventory_items")
    with migration._sqlite_rebuild_safe(), ops.batch_alter_table("inventory_items") as batch:
        batch.drop_constraint("ck_inventory_items_owner_matches", type_="check")
        batch.drop_constraint("fk_inventory_items_owner_vendor_id", type_="foreignkey")
    with migration._sqlite_rebuild_safe(), ops.batch_alter_table("tools") as batch:
        batch.drop_constraint("ck_tools_owner_matches", type_="check")
        batch.drop_constraint("ck_tools_ownership", type_="check")
        batch.drop_constraint("fk_tools_owner_vendor_id", type_="foreignkey")
        batch.drop_column("owner_vendor_id")
    assert "ix_inventory_items_ownership" not in _objects(connection, "inventory_items")["indexes"]
    assert "owner_vendor_id" not in _objects(connection, "tools")["columns"]

    migration.upgrade()
    assert {table: _objects(connection, table) for table in TABLES} == full
    # Values recorded in the surviving columns are kept; nothing is overwritten or re-derived.
    assert _ownership(connection, "inventory_items") == [("customer", 10, None), ("company", None, None)]
    assert _ownership(connection, "tools") == [("customer", 10, None), ("company", None, None)]
    _assert_legacy_rows_intact(connection)


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in TABLES}
    migration.upgrade()
    connection.exec_driver_sql("UPDATE inventory_items SET ownership = 'customer', owner_customer_id = 10 WHERE id = 1")
    connection.exec_driver_sql("UPDATE tools SET ownership = 'supplier', owner_vendor_id = 20 WHERE id = 1")
    migration.downgrade()

    for table in TABLES:
        assert _objects(connection, table) == legacy[table], table
    _assert_legacy_rows_intact(connection)
    migration.downgrade()  # a second downgrade is a guarded no-op, not an error
    for table in TABLES:
        assert _objects(connection, table) == legacy[table], table

    migration.upgrade()  # the chain replays forward cleanly...
    for table in TABLES:
        assert set(NEW_COLUMNS) <= _objects(connection, table)["columns"], table
        # ...and the downgrade was LOSSY, as the docstring warns: the owned rows came back as
        # company stock. A code rollback stamps 128 instead.
        assert _ownership(connection, table) == [("company", None, None)] * len(LEGACY_ROWS[table]), table
    _assert_legacy_rows_intact(connection)


def _assert_the_observation_trigger_resolves_inventory_items(connection, observation_id):
    """The trigger fires against the REBUILT table: a real source passes, a missing one aborts."""
    connection.exec_driver_sql(f"INSERT INTO stock_piece_observations VALUES ({observation_id}, 1, 2)")
    nested = connection.begin_nested()
    with pytest.raises(sa.exc.IntegrityError, match="Recorded source must match"):
        connection.exec_driver_sql(f"INSERT INTO stock_piece_observations VALUES ({observation_id + 1}, 1, 999)")
    nested.rollback()
    nested = connection.begin_nested()
    with pytest.raises(sa.exc.IntegrityError, match="Recorded source must match"):
        # Right item, wrong tenant.
        connection.exec_driver_sql(f"INSERT INTO stock_piece_observations VALUES ({observation_id + 1}, 2, 1)")
    nested.rollback()
    connection.exec_driver_sql(f"DELETE FROM stock_piece_observations WHERE id = {observation_id}")


def test_a_trigger_that_reads_inventory_items_survives_both_rebuilds(scratch):
    connection, migration = scratch
    migration.upgrade()
    _assert_legacy_rows_intact(connection)
    _assert_the_observation_trigger_resolves_inventory_items(connection, 10)
    migration.downgrade()
    _assert_legacy_rows_intact(connection)
    _assert_the_observation_trigger_resolves_inventory_items(connection, 20)


def test_the_connection_keeps_its_own_legacy_alter_table_setting(scratch):
    connection, migration = scratch
    connection.exec_driver_sql("PRAGMA legacy_alter_table = ON")
    migration.upgrade()
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 1
    migration.downgrade()
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 1


# -------------------------------------------------------- 4. create_all bootstrap no-op


class _Recorder:
    def __init__(self, bind):
        self._bind = bind
        self.calls = []

    def get_bind(self):
        return self._bind

    def get_context(self):
        return MigrationContext.configure(self._bind)

    def __getattr__(self, name):  # any DDL call is recorded instead of run
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


def test_upgrade_over_a_create_all_schema_emits_nothing(tmp_path):
    from app.db.database import Base

    module = _module()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    names = ("companies", "users", "parts", "work_centers", "customers", "vendors", "inventory_items", "tools")
    tables = [Base.metadata.tables[name] for name in names]
    with engine.begin() as conn:
        # Only what 129 touches and what its new FKs reference; other FK targets are irrelevant here.
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        Base.metadata.create_all(conn, tables=tables)
        recorder = _Recorder(conn)
        module.op = recorder
        module.upgrade()
    engine.dispose()
    assert recorder.calls == []


# ------------------------------------------------------------- 5. offline PostgreSQL SQL


def _render(function_name: str):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


def _indexes_of(statements, *prefixes):
    return [i for i, statement in enumerate(statements) if statement.startswith(prefixes)]


def _table_statements(statements, table):
    return [
        s
        for s in statements
        if s.startswith(f"ALTER TABLE {table} ") or s.endswith(f"ON {table} (company_id, ownership)")
    ]


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    assert statements[0] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-1] == "SET LOCAL lock_timeout = DEFAULT"
    sql = "\n".join(statements)
    for table in TABLES:
        expected = _expected_names(table)
        ordered = [
            # Metadata-only on PostgreSQL 11+: a constant default, no rewrite, no backfill.
            f"ALTER TABLE {table} ADD COLUMN ownership VARCHAR(20) DEFAULT 'company' NOT NULL",
            f"ALTER TABLE {table} ADD COLUMN owner_customer_id INTEGER",
            f"ALTER TABLE {table} ADD COLUMN owner_vendor_id INTEGER",
            *(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY({local}) REFERENCES {referred} (id)"
                for name, (referred, local) in expected["fks"].items()
            ),
            *(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({text})" for name, text in expected["checks"].items()),
            f"CREATE INDEX {expected['index'][0]} ON {table} (company_id, ownership)",
        ]
        # Exactly these, in this order: columns, then the FKs, then the CHECKs, then the index.
        assert _table_statements(statements, table) == ordered, table
    # inventory_items first, then tools (the lock order the docstring states).
    assert max(_indexes_of(statements, "ALTER TABLE inventory_items", "CREATE INDEX ix_inventory_items")) < min(
        _indexes_of(statements, "ALTER TABLE tools", "CREATE INDEX ix_tools")
    )
    assert len(statements) == 2 + 2 * 8
    assert "ON DELETE" not in sql and "ON UPDATE" not in sql and "CONCURRENTLY" not in sql
    assert "is_deleted" not in sql and "audit_log" not in sql and "CREATE TABLE" not in sql
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "129 is pure DDL: no backfill"
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|\banon\b|authenticated|service_role", sql)


def test_offline_downgrade_drops_exactly_what_upgrade_added_in_reverse():
    statements = _render("downgrade")
    assert statements[0] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-1] == "SET LOCAL lock_timeout = DEFAULT"
    sql = "\n".join(statements)
    for table in TABLES:
        expected = _expected_names(table)
        ordered = [
            # The index first, then the CHECKs, then the FKs, then the columns -- each in reverse.
            f"DROP INDEX {expected['index'][0]}",
            *(f"ALTER TABLE {table} DROP CONSTRAINT {name}" for name in reversed(list(expected["checks"]))),
            *(f"ALTER TABLE {table} DROP CONSTRAINT {name}" for name in reversed(list(expected["fks"]))),
            *(f"ALTER TABLE {table} DROP COLUMN {name}" for name in reversed(NEW_COLUMNS)),
        ]
        actual = [s for s in statements if s.startswith((f"ALTER TABLE {table} ", f"DROP INDEX ix_{table}_"))]
        assert actual == ordered, table
    # Reverse of the upgrade: tools first, then inventory_items.
    assert max(_indexes_of(statements, "ALTER TABLE tools", "DROP INDEX ix_tools")) < min(
        _indexes_of(statements, "ALTER TABLE inventory_items", "DROP INDEX ix_inventory_items")
    )
    assert len(statements) == 2 + 2 * 8
    assert "DROP TABLE" not in sql and "is_deleted" not in sql and "audit_log" not in sql
    assert "supplier_id" not in sql and "chk_inventory_items_allocated_non_negative" not in sql
    assert not re.search(r"\b(UPDATE|INSERT)\b", sql)


@pytest.mark.parametrize("table", TABLES)
def test_create_all_ddl_carries_the_same_objects_on_postgres(table):
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    MODELS[table].__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    expected = _expected_names(table)
    assert "ownership VARCHAR(20) DEFAULT 'company' NOT NULL" in ddl
    assert "owner_customer_id INTEGER," in ddl and "owner_vendor_id INTEGER," in ddl
    for name, text in expected["checks"].items():
        assert f"CONSTRAINT {name} CHECK ({text})" in ddl, name
    for name, (referred, local) in expected["fks"].items():
        assert f"CONSTRAINT {name} FOREIGN KEY({local}) REFERENCES {referred} (id)" in ddl, name
        assert f"FOREIGN KEY({local}) REFERENCES {referred} (id) ON DELETE" not in ddl, name
    assert f"CREATE INDEX {expected['index'][0]} ON {table} (company_id, ownership)" in ddl
