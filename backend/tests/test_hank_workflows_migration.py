"""Migration110 is additive, reversible and denies direct client access to saved work."""

import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.hank_teamwork import HankHandoff, HankRoutine, HankRoutineRun

MODELS = (HankIntakeBatch, HankIntakeFile, HankHandoff, HankRoutine, HankRoutineRun)
pytestmark = [pytest.mark.unit]


def module():
    path = Path(__file__).parents[1] / 'alembic/versions/110_hank_workflows.py'
    spec = importlib.util.spec_from_file_location('hank_workflows_migration', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def migrated(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "hank-workflows.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text('PRAGMA foreign_keys=ON'))
        for name in ('companies', 'users', 'work_orders'):
            connection.execute(sa.text(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY, label TEXT)'))
            connection.execute(sa.text(f"INSERT INTO {name} VALUES (1, 'Retained source')"))
        connection.execute(sa.text('CREATE TABLE hank_tasks (id INTEGER PRIMARY KEY, receipt TEXT)'))
        connection.execute(sa.text("INSERT INTO hank_tasks VALUES (1, 'Existing receipt')"))
        migration = module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def test_upgrade_replay_downgrade_reupgrade_retains_existing_records(migrated):
    connection, migration = migrated
    assert migration.revision == '110_hank_workflows' and migration.down_revision == '109_hank_preferences'
    migration.upgrade()
    connection.execute(sa.text('DROP INDEX ix_hank_handoff_recipient_status'))
    migration.upgrade()
    assert 'ix_hank_handoff_recipient_status' in {
        row['name'] for row in sa.inspect(connection).get_indexes('hank_handoffs')
    }
    migration.downgrade()
    migration.downgrade()
    assert set(sa.inspect(connection).get_table_names()) == {'companies', 'users', 'work_orders', 'hank_tasks'}
    assert connection.execute(sa.text('SELECT receipt FROM hank_tasks')).scalar() == 'Existing receipt'
    migration.upgrade()
    assert all(model.__tablename__ in sa.inspect(connection).get_table_names() for model in MODELS)
    assert connection.execute(sa.text('SELECT label FROM work_orders')).scalar() == 'Retained source'


@pytest.mark.parametrize('model', MODELS)
def test_migration_matches_bootstrap_columns_constraints_and_indexes(migrated, model):
    connection, _ = migrated
    inspector = sa.inspect(connection)
    table = model.__table__
    columns = {column['name']: column for column in inspector.get_columns(table.name)}
    assert set(columns) == set(table.columns.keys())
    for column in table.columns:
        reflected = columns[column.name]
        assert reflected['nullable'] == column.nullable
        assert str(reflected['type']) == str(column.type)
        if column.server_default is not None:
            assert str(reflected['default']).strip("'()") == str(column.server_default.arg)
        else:
            assert reflected['default'] is None
    assert {row['name']: tuple(row['column_names']) for row in inspector.get_indexes(table.name)} == {
        index.name: tuple(column.name for column in index.columns) for index in table.indexes
    }
    assert {row['name']: row['sqltext'] for row in inspector.get_check_constraints(table.name)} == {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {row['name']: tuple(row['column_names']) for row in inspector.get_unique_constraints(table.name)} == {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert inspector.get_foreign_keys(table.name)


@pytest.mark.parametrize(
    'values',
    [
        {'status': 'fabricated'},
        {'version': 0},
        {'company_id': 999},
        {'sender_id': 1, 'recipient_id': 1},
    ],
)
def test_handoff_integrity_is_enforced(migrated, values):
    connection, _ = migrated
    connection.execute(sa.text("INSERT INTO users VALUES (2, 'Recipient')"))
    fields = {
        'company_id': 1,
        'sender_id': 1,
        'recipient_id': 2,
        'sender_name': 'One',
        'recipient_name': 'Two',
        'work_order_id': 1,
        'work_order_number': 'WO-1',
        'request_key': 'a' * 36,
        'request_hash': 'a' * 64,
        'created_at': '2026-09-22',
        'updated_at': '2026-09-22',
        **values,
    }
    statement = (
        'INSERT INTO hank_handoffs (' + ','.join(fields) + ') VALUES (' + ','.join(':' + key for key in fields) + ')'
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.text(statement), fields)


def test_all_new_tables_and_sequences_are_default_deny_on_postgres():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    migration.upgrade()
    statements = []
    engine = sa.create_mock_engine(
        'postgresql://',
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    for model in MODELS:
        model.__table__.create(engine, checkfirst=False)
    for sql in (output.getvalue(), '\n'.join(statements)):
        for model in MODELS:
            table = model.__tablename__
            assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in sql
            for role in ('PUBLIC', 'anon', 'authenticated'):
                assert f'REVOKE ALL ON TABLE {table} FROM {role}' in sql
                assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}' in sql
        assert 'CREATE POLICY' not in sql and 'auth.uid()' not in sql and 'INSERT INTO' not in sql
        assert 'TIMESTAMP WITH TIME ZONE' in sql
