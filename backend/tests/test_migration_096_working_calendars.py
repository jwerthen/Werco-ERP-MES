"""Receipt migration roundtrip and PostgreSQL access/uniqueness contracts."""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/096_working_calendars.py"
    spec = importlib.util.spec_from_file_location("working_calendars_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calendar_migration_roundtrip(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'calendars.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            assert sa.inspect(connection).has_table("working_calendars")
            assert sa.inspect(connection).get_unique_constraints("working_calendars")[0]["column_names"] == [
                "company_id",
                "work_center_id",
            ]
            migration().downgrade()
            assert not sa.inspect(connection).has_table("working_calendars")
    engine.dispose()


def test_calendar_postgres_sql_is_private_and_company_keyed():
    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    ):
        migration().upgrade()
    output = sql.getvalue()
    assert "UNIQUE (company_id, work_center_id)" in output
    assert "ENABLE ROW LEVEL SECURITY" in output
    assert "FROM PUBLIC" in output and "FROM anon" in output and "FROM authenticated" in output
    assert migration().down_revision == "095_kiosk_production_receipts"
