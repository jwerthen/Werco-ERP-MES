import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    spec = importlib.util.spec_from_file_location(
        "workspace_migration",
        Path(__file__).parents[1] / "alembic/versions/091_user_workspaces.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_downgrade_preserves_users_and_is_unique_per_owner():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO users VALUES (1)"))
        module = migration()
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = sa.inspect(connection)
        unique = inspector.get_unique_constraints("user_workspace_records")
        assert unique[0]["column_names"] == [
            "company_id",
            "user_id",
            "namespace",
            "kind",
            "key",
        ]
        assert inspector.get_indexes("user_workspace_records")[0]["name"] == "ix_user_workspace_owner"
        assert module.down_revision == "090_document_revision_chain"
        module.downgrade()
        assert connection.execute(sa.text("SELECT id FROM users")).scalar() == 1
        assert "user_workspace_records" not in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_postgres_sql_disables_direct_client_access():
    output = io.StringIO()
    module = migration()
    module.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    module.upgrade()
    sql = output.getvalue()
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FROM PUBLIC" in sql and "FROM anon" in sql and "FROM authenticated" in sql
    assert "auth.uid" not in sql
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in sql
