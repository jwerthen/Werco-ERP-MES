import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/099_recoverable_import_batches.py'
    spec = importlib.util.spec_from_file_location('migration099', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_can_upgrade_and_downgrade_twice():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE companies (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        module = migration()
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
            assert {'import_batches', 'import_batch_rows'}.issubset(sa.inspect(connection).get_table_names())
            module.downgrade()
            module.downgrade()
            assert sa.inspect(connection).get_table_names() == ['companies', 'users']
            module.upgrade()
            assert sa.inspect(connection).has_table('import_batch_rows')


def test_postgres_sql_explicitly_revokes_table_and_sequence_access():
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        migration().upgrade()
    sql = output.getvalue()
    for table in ('import_batches', 'import_batch_rows'):
        assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in sql
        for role in ('PUBLIC', 'anon', 'authenticated'):
            assert f'REVOKE ALL ON TABLE {table} FROM {role}' in sql
            assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}' in sql
