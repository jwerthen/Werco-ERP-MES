"""Receipt migration roundtrip and PostgreSQL access/uniqueness contracts."""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/095_kiosk_production_receipts.py"
    spec = importlib.util.spec_from_file_location("production_receipts_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_receipt_migration_roundtrip(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            assert sa.inspect(connection).has_table("production_receipts")
            assert sa.inspect(connection).get_unique_constraints("production_receipts")[0]["column_names"] == [
                "company_id",
                "request_id",
            ]
            migration().downgrade()
            assert not sa.inspect(connection).has_table("production_receipts")
    engine.dispose()


def test_receipt_postgres_sql_is_private_and_company_keyed():
    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    ):
        migration().upgrade()
    output = sql.getvalue()
    assert "UNIQUE (company_id, request_id)" in output
    assert "ENABLE ROW LEVEL SECURITY" in output
    assert "FROM PUBLIC" in output and "FROM anon" in output and "FROM authenticated" in output
    assert migration().down_revision == "094_document_deliveries"
