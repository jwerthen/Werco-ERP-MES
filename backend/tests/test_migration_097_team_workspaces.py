import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/097_team_workspaces.py'
    spec = importlib.util.spec_from_file_location('team_migration', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_downgrade_preserves_private_drafts_and_work_orders(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "team.db"}')
    with engine.begin() as connection:
        for statement in [
            'CREATE TABLE companies (id INTEGER PRIMARY KEY)',
            'CREATE TABLE users (id INTEGER PRIMARY KEY)',
            'CREATE TABLE work_orders (id INTEGER PRIMARY KEY, company_id INTEGER, is_deleted BOOLEAN, priority INTEGER, due_date DATE)',
            'CREATE TABLE user_workspace_records (id INTEGER PRIMARY KEY, data TEXT)',
            'INSERT INTO companies VALUES (1)',
            'INSERT INTO users VALUES (1)',
            "INSERT INTO work_orders VALUES (1,1,0,3,'2026-09-07')",
            "INSERT INTO user_workspace_records VALUES (1,'private draft')",
        ]:
            connection.execute(sa.text(statement))
        mod = migration()
        mod.op = Operations(MigrationContext.configure(connection))
        assert mod.revision == '097_team_workspaces' and mod.down_revision == '096_working_calendars'
        mod.upgrade()
        assert 'team_workspace_records' in sa.inspect(connection).get_table_names()
        assert 'ix_work_orders_company_live_priority_due' in {
            row['name'] for row in sa.inspect(connection).get_indexes('work_orders')
        }
        connection.execute(
            sa.text(
                "INSERT INTO team_workspace_records (company_id, namespace, key, name, data, version, created_at, updated_at) VALUES (1, 'parts', 'test', 'Team', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        mod.downgrade()
        assert 'team_workspace_records' not in sa.inspect(connection).get_table_names()
        assert connection.execute(sa.text('SELECT data FROM user_workspace_records')).scalar() == 'private draft'
        assert connection.execute(sa.text('SELECT count(*) FROM work_orders')).scalar() == 1
        mod.upgrade()
        assert connection.execute(sa.text('SELECT count(*) FROM team_workspace_records')).scalar() == 0
    engine.dispose()


def test_postgres_offline_ddl_blocks_direct_data_api_and_scopes_unique_key():
    output = io.StringIO()
    mod = migration()
    mod.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    mod.upgrade()
    sql = output.getvalue()
    assert 'ENABLE ROW LEVEL SECURITY' in sql
    assert 'FROM PUBLIC' in sql and 'FROM anon' in sql and 'FROM authenticated' in sql
    assert 'auth.uid()' not in sql and 'CREATE POLICY' not in sql
    assert 'UNIQUE (company_id, namespace, key)' in sql
    assert 'FOREIGN KEY(updated_by) REFERENCES users' in sql
