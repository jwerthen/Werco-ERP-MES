"""Consumable checkout schema parity, safe replay and private PostgreSQL DDL."""

import importlib.util
import io
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.consumable import ConsumableCheckout

pytestmark = [pytest.mark.unit]
BACKEND = Path(__file__).parents[1]
TABLE = "consumable_checkouts"
REVISION = "112_consumable_checkouts"


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "consumable_checkout_migration", BACKEND / "alembic/versions/112_consumable_checkouts.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrated(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "consumables.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        for name in ("companies", "users", "parts", "inventory_items", "inventory_transactions"):
            connection.execute(sa.text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)"))
            connection.execute(sa.text(f"INSERT INTO {name} VALUES (1), (2)"))
        migration = migration_module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def fields(**changes):
    return {
        "company_id": 1,
        "inventory_item_id": 1,
        "inventory_transaction_id": 1,
        "part_id": 1,
        "part_number": "DISC-120",
        "part_name": "120-grit sanding disc",
        "unit_of_measure": "each",
        "location": "CRIB-1",
        "employee_id": 1,
        "employee_name": "Shop Employee",
        "employee_code": "EMP-1",
        "issued_by": 2,
        "issued_by_name": "Crib Attendant",
        "issued_at": datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc),
        "quantity": 3,
        "request_id": "8af26f41-30e6-45c1-9d1a-3ea9e29a53a8",
        "request_hash": "a" * 64,
        **changes,
    }


def schema_snapshot(connection):
    inspector = sa.inspect(connection)
    return {
        "columns": sorted(
            (column["name"], str(column["type"]), column["nullable"], column["default"])
            for column in inspector.get_columns(TABLE)
        ),
        "primary_key": tuple(inspector.get_pk_constraint(TABLE)["constrained_columns"]),
        "indexes": sorted(
            (index["name"], tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(TABLE)
        ),
        "foreign_keys": sorted(
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(TABLE)
        ),
        "unique": sorted(
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(TABLE)
        ),
        "checks": sorted(
            (constraint["name"], constraint["sqltext"]) for constraint in inspector.get_check_constraints(TABLE)
        ),
    }


def test_upgrade_replay_repairs_missing_index_and_preserves_records(migrated):
    connection, migration = migrated
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(**fields()))
    reference = schema_snapshot(connection)
    connection.execute(sa.text("DROP INDEX ix_consumable_checkout_employee_time"))
    migration.upgrade()
    migration.upgrade()
    assert schema_snapshot(connection) == reference
    assert connection.execute(sa.select(table.c.quantity)).scalar_one() == 3
    assert connection.execute(sa.select(table.c.employee_name)).scalar_one() == "Shop Employee"
    assert migration.down_revision == "111_continuous_improvement"
    migration.downgrade()
    migration.downgrade()
    assert not sa.inspect(connection).has_table(TABLE)
    assert connection.execute(sa.text("SELECT count(*) FROM inventory_transactions")).scalar_one() == 2
    migration.upgrade()
    assert schema_snapshot(connection) == reference


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 1000000001},
        {"quantity": float("inf")},
        {"quantity": float("nan")},
        {"company_id": 999},
        {"employee_id": 999},
        {"issued_by": 999},
        {"part_id": 999},
        {"inventory_item_id": 999},
        {"inventory_transaction_id": 999},
        {"request_id": "invalid"},
        {"request_hash": "invalid"},
    ],
)
def test_database_rejects_invalid_checkout_records(migrated, changes):
    connection, _ = migrated
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(**changes)))


def test_retry_identity_and_transaction_uniqueness_are_enforced(migrated):
    connection, _ = migrated
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(**fields()))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(inventory_transaction_id=2)))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(request_id="a11b39d0-3049-487b-9967-92605e9ce01d")))
    # Different companies have independent retry namespaces; the ledger FK stays unique.
    connection.execute(table.insert().values(**fields(company_id=2, inventory_transaction_id=2)))
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 2


def test_postgres_migration_and_create_all_keep_employee_history_private():
    migration = migration_module()
    output = io.StringIO()
    migration.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    migration.upgrade()
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    ConsumableCheckout.__table__.create(engine, checkfirst=False)
    for sql in (output.getvalue(), "\n".join(statements)):
        assert "ALTER TABLE consumable_checkouts ENABLE ROW LEVEL SECURITY" in sql
        assert "FORCE ROW LEVEL SECURITY" not in sql
        for role in ("PUBLIC", "anon", "authenticated"):
            assert f"REVOKE ALL ON TABLE consumable_checkouts FROM {role}" in sql
            assert f"REVOKE ALL ON SEQUENCE consumable_checkouts_id_seq FROM {role}" in sql
        assert "CREATE POLICY" not in sql and "SECURITY DEFINER" not in sql
        assert "BEFORE UPDATE OR DELETE ON consumable_checkouts" in sql
        assert "SET search_path = pg_catalog" in sql
        assert "REVOKE ALL ON FUNCTION prevent_consumable_checkout_change() FROM PUBLIC" in sql
        assert "TIMESTAMP WITH TIME ZONE" in sql
        assert "quantity > 0 AND quantity <= 1000000000" in sql
    migration.downgrade()
    assert "DROP TABLE consumable_checkouts" in output.getvalue()
    assert "DROP FUNCTION IF EXISTS prevent_consumable_checkout_change()" in output.getvalue()


def test_alembic_bootstrap_upgrade_downgrade_reupgrade_has_schema_parity(tmp_path):
    # Exercise real Alembic revision ordering on a disposable model bootstrap;
    # never read the developer's DATABASE_URL or connect to the deployed database.
    from app.db.database import Base

    db_url = f'sqlite:///{tmp_path / "bootstrap.db"}'
    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        reference = schema_snapshot(engine)
        all_tables = set(sa.inspect(engine).get_table_names())

        def alembic(*args):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=BACKEND,
                env={**os.environ, "DATABASE_URL": db_url},
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert result.returncode == 0, result.stdout + result.stderr

        alembic("stamp", "111_continuous_improvement")
        alembic("upgrade", REVISION)
        assert schema_snapshot(engine) == reference
        alembic("downgrade", "111_continuous_improvement")
        assert set(sa.inspect(engine).get_table_names()) - {"alembic_version"} == all_tables - {TABLE}
        alembic("upgrade", REVISION)
        assert schema_snapshot(engine) == reference
    finally:
        engine.dispose()
