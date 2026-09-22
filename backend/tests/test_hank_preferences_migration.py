"""Personal preference schema is additive, isolated, reversible and default-deny."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.hank_preferences import HankPreference

VERSIONS = Path(__file__).parents[1] / 'alembic/versions'
pytestmark = [pytest.mark.unit]


def module(filename='109_hank_preferences.py'):
    spec = importlib.util.spec_from_file_location(filename.removesuffix('.py'), VERSIONS / filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def fields(**changes):
    return {
        'company_id': 1,
        'user_id': 1,
        'created_at': datetime(2026, 9, 22, tzinfo=timezone.utc),
        'updated_at': datetime(2026, 9, 22, tzinfo=timezone.utc),
    } | changes


@pytest.fixture
def local_migration(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "hank-preferences-migration.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text('PRAGMA foreign_keys=ON'))
        connection.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text("INSERT INTO companies VALUES (1, 'Existing company'), (2, 'Other company')"))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'Existing user'), (2, 'Other user')"))
        operations = Operations(MigrationContext.configure(connection))
        tasks = module('108_hank_tasks.py')
        tasks.op = operations
        tasks.upgrade()
        task_table = sa.Table('hank_tasks', sa.MetaData(), autoload_with=connection)
        connection.execute(
            task_table.insert().values(
                company_id=1,
                owner_id=1,
                credential_key='user',
                request_key=str(uuid4()),
                request_hash='a' * 64,
                kind='watch_work_order',
                title='Existing completed follow-up',
                status='completed',
                result_json={'summary': 'Existing receipt', 'warnings': [], 'references': []},
                created_at=datetime(2026, 9, 21),
                updated_at=datetime(2026, 9, 21),
            )
        )
        migration = module()
        migration.op = operations
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def preference_table(connection):
    return sa.Table('hank_preferences', sa.MetaData(), autoload_with=connection)


def test_upgrade_replay_downgrade_reupgrade_preserves_tasks_and_existing_people(local_migration):
    connection, migration = local_migration
    assert migration.revision == '109_hank_preferences'
    assert migration.down_revision == '108_hank_tasks'
    table = preference_table(connection)
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 0
    connection.execute(table.insert().values(**fields(preferences_json={'focus_area': 'quality'})))
    migration.upgrade()
    saved = connection.execute(sa.select(table)).mappings().one()
    assert saved['preferences_json'] == {'focus_area': 'quality'} and saved['version'] == 1
    # A partial bootstrap can recover missing indexes without rewriting preferences.
    connection.execute(sa.text('DROP INDEX ix_hank_preferences_user_id'))
    migration.upgrade()
    assert 'ix_hank_preferences_user_id' in {row['name'] for row in sa.inspect(connection).get_indexes(table.name)}
    migration.downgrade()
    migration.downgrade()
    assert 'hank_preferences' not in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.text('SELECT title FROM hank_tasks')).scalar() == 'Existing completed follow-up'
    assert connection.execute(sa.text('SELECT name FROM companies WHERE id=1')).scalar() == 'Existing company'
    assert connection.execute(sa.text('SELECT name FROM users WHERE id=1')).scalar() == 'Existing user'
    migration.upgrade()
    assert connection.execute(sa.text('SELECT count(*) FROM hank_preferences')).scalar() == 0
    assert connection.execute(sa.text('SELECT count(*) FROM hank_tasks')).scalar() == 1


def test_defaults_and_unique_person_within_company(local_migration):
    connection, _ = local_migration
    table = preference_table(connection)
    connection.execute(table.insert().values(**fields()))
    row = connection.execute(sa.select(table)).mappings().one()
    assert row['version'] == 1 and row['preferences_json'] == {}
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields()))
    connection.execute(table.insert().values(**fields(company_id=2)))
    connection.execute(table.insert().values(**fields(user_id=2)))
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 3


@pytest.mark.parametrize(
    'changes',
    [
        {'version': 0},
        {'version': -1},
        {'company_id': 999},
        {'user_id': 999},
        {'company_id': sa.null()},
        {'user_id': sa.null()},
        {'preferences_json': sa.null()},
        {'created_at': sa.null()},
        {'updated_at': sa.null()},
    ],
)
def test_invalid_versions_missing_required_values_and_foreign_keys_fail(local_migration, changes):
    connection, _ = local_migration
    table = preference_table(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(**changes)))


def test_model_bootstrap_columns_indexes_constraints_and_defaults_match_migration(local_migration):
    connection, _ = local_migration
    inspector = sa.inspect(connection)
    model = HankPreference.__table__
    columns = {column['name']: column for column in inspector.get_columns(model.name)}
    assert set(columns) == set(model.columns.keys())
    for column in model.columns:
        reflected = columns[column.name]
        assert reflected['nullable'] == column.nullable
        assert str(reflected['type']) == str(column.type)
        if column.server_default is not None:
            assert str(reflected['default']).strip("'()") == str(column.server_default.arg)
        else:
            assert reflected['default'] is None
    assert {row['name']: tuple(row['column_names']) for row in inspector.get_indexes(model.name)} == {
        index.name: tuple(column.name for column in index.columns) for index in model.indexes
    }
    assert {row['name']: row['sqltext'] for row in inspector.get_check_constraints(model.name)} == {
        constraint.name: str(constraint.sqltext)
        for constraint in model.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {row['name']: tuple(row['column_names']) for row in inspector.get_unique_constraints(model.name)} == {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def test_model_json_defaults_are_independent_and_timestamps_start_in_utc(db_session, test_user, operator_user):
    first = HankPreference(company_id=1, user_id=test_user.id)
    second = HankPreference(company_id=1, user_id=operator_user.id)
    db_session.add_all([first, second])
    db_session.flush()
    assert first.preferences_json == second.preferences_json == {}
    assert first.preferences_json is not second.preferences_json
    first.preferences_json['only_this_person'] = True
    assert second.preferences_json == {}
    assert first.created_at.tzinfo == timezone.utc and first.updated_at.tzinfo == timezone.utc
    assert first.version == second.version == 1


def test_postgres_migration_and_model_bootstrap_deny_data_api_table_and_sequence_access():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    migration.upgrade()
    migration_sql = output.getvalue()
    statements = []
    engine = sa.create_mock_engine(
        'postgresql://',
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    HankPreference.__table__.create(engine, checkfirst=False)
    for sql in (migration_sql, '\n'.join(statements)):
        assert 'ALTER TABLE hank_preferences ENABLE ROW LEVEL SECURITY' in sql
        for role in ('PUBLIC', 'anon', 'authenticated'):
            assert f'REVOKE ALL ON TABLE hank_preferences FROM {role}' in sql
            assert f'REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM {role}' in sql
        assert 'CREATE POLICY' not in sql and 'auth.uid()' not in sql
        assert 'FOREIGN KEY(company_id) REFERENCES companies (id)' in sql
        assert 'FOREIGN KEY(user_id) REFERENCES users (id)' in sql
        assert 'UNIQUE (company_id, user_id)' in sql
        assert 'CHECK (version >= 1)' in sql
        assert 'TIMESTAMP WITH TIME ZONE' in sql
        assert 'INSERT INTO' not in sql
    output.seek(0)
    output.truncate(0)
    migration.downgrade()
    assert 'DROP TABLE hank_preferences' in output.getvalue()
    assert 'hank_tasks' not in output.getvalue()
