import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/098_runtime_metrics.py"
    spec = importlib.util.spec_from_file_location("runtime_metrics_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_metric_schema_roundtrip(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'metrics.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table("runtime_metric_samples")
            assert inspector.has_table("runtime_metric_settings")
            assert inspector.get_unique_constraints("runtime_metric_samples")[0]["column_names"] == [
                "company_id",
                "metric_id",
            ]
            assert {column["name"] for column in inspector.get_columns("runtime_metric_samples")} == {
                "id",
                "company_id",
                "metric_id",
                "name",
                "route",
                "device",
                "navigation",
                "release",
                "value",
                "sequence",
                "created_at",
            }
            migration().downgrade()
            assert not sa.inspect(connection).has_table("runtime_metric_samples")
            assert not sa.inspect(connection).has_table("runtime_metric_settings")
    engine.dispose()


def test_postgres_metrics_are_private_to_fastapi_role():
    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    ):
        migration().upgrade()
    output = sql.getvalue()
    for table in ("runtime_metric_samples", "runtime_metric_settings"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in output
        for role in ("PUBLIC", "anon", "authenticated"):
            assert f"REVOKE ALL ON TABLE {table} FROM {role}" in output
    assert "REVOKE ALL ON SEQUENCE runtime_metric_samples_id_seq FROM authenticated" in output
    assert migration().down_revision == "097_team_workspaces"
