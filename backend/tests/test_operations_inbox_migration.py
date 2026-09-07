import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = Path(__file__).parents[1] / 'alembic/versions/092_operational_inbox_state.py'


def module():
    spec = importlib.util.spec_from_file_location('inbox_migration', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_upgrade_downgrade_preserves_preexisting_users_and_companies(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "migration.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text("INSERT INTO companies VALUES (1, 'Existing company')"))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'Existing user')"))
        migration = module()
        migration.op = Operations(MigrationContext.configure(connection))
        assert migration.down_revision == '091_user_workspaces'
        migration.upgrade()
        inspector = sa.inspect(connection)
        assert {'uq_inbox_company_source'} <= {
            row['name'] for row in inspector.get_unique_constraints('operational_inbox_states')
        }
        assert {'ix_inbox_company_owner'} <= {row['name'] for row in inspector.get_indexes('operational_inbox_states')}
        connection.execute(
            sa.text(
                "INSERT INTO operational_inbox_states (company_id,source_kind,source_id,updated_by,updated_at) VALUES (1,'blocker',10,1,CURRENT_TIMESTAMP)"
            )
        )
        migration.downgrade()
        assert 'operational_inbox_states' not in sa.inspect(connection).get_table_names()
        assert connection.execute(sa.text('SELECT name FROM companies')).scalar() == 'Existing company'
        assert connection.execute(sa.text('SELECT name FROM users')).scalar() == 'Existing user'
        migration.upgrade()
        assert connection.execute(sa.text('SELECT count(*) FROM operational_inbox_states')).scalar() == 0
    engine.dispose()


def test_postgres_sql_denies_data_api_access_without_guessing_custom_auth_policies():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    migration.upgrade()
    sql = output.getvalue()
    assert 'ENABLE ROW LEVEL SECURITY' in sql
    assert 'FROM PUBLIC' in sql and 'FROM anon' in sql and 'FROM authenticated' in sql
    assert 'auth.uid()' not in sql and 'CREATE POLICY' not in sql
    assert 'FOREIGN KEY(owner_id) REFERENCES users' in sql
    assert 'UNIQUE (company_id, source_kind, source_id)' in sql
