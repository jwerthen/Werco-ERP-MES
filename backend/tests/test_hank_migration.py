"""Hank's additive schema, reverse path, bootstrap parity and PostgreSQL access fence."""

import importlib.util
import io
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.hank import HankTask

MIGRATION = Path(__file__).parents[1] / 'alembic/versions/108_hank_tasks.py'


def module():
    spec = importlib.util.spec_from_file_location('hank_migration', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def fields(**changes):
    result = {
        'company_id': 1,
        'owner_id': 1,
        'credential_key': 'user',
        'request_key': str(uuid4()),
        'request_hash': 'a' * 64,
        'kind': 'repeat_job',
        'title': 'Prepare repeat job',
        'created_at': datetime(2026, 9, 22),
        'updated_at': datetime(2026, 9, 22),
    }
    return result | changes


@pytest.fixture
def local_migration(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "hank-migration.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text('PRAGMA foreign_keys=ON'))
        connection.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text("INSERT INTO companies VALUES (1, 'Existing company'), (2, 'Other company')"))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'Existing user'), (2, 'Other user')"))
        migration = module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def task_table(connection):
    return sa.Table('hank_tasks', sa.MetaData(), autoload_with=connection)


def test_upgrade_idempotence_downgrade_reupgrade_preserves_existing_data(local_migration):
    connection, migration = local_migration
    assert migration.down_revision == '107_fabrication_quote_profiles'
    table = task_table(connection)
    connection.execute(table.insert().values(**fields()))
    migration.upgrade()
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 1
    row = connection.execute(sa.select(table)).mappings().one()
    assert row['status'] == 'awaiting_review' and row['version'] == 1
    assert row['input_json'] == row['preview_json'] == row['source_versions_json'] == {}
    assert row['result_json'] is None
    migration.downgrade()
    migration.downgrade()
    assert 'hank_tasks' not in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.text('SELECT name FROM companies WHERE id=1')).scalar() == 'Existing company'
    assert connection.execute(sa.text('SELECT name FROM users WHERE id=1')).scalar() == 'Existing user'
    migration.upgrade()
    assert connection.execute(sa.text('SELECT count(*) FROM hank_tasks')).scalar() == 0


def test_request_key_uniqueness_is_tenant_bound_not_owner_bound(local_migration):
    connection, _ = local_migration
    table = task_table(connection)
    key = str(uuid4())
    connection.execute(table.insert().values(**fields(request_key=key)))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(request_key=key, owner_id=2)))
    connection.execute(table.insert().values(**fields(request_key=key, company_id=2, owner_id=2)))
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 2


@pytest.mark.parametrize(
    'changes',
    [
        {'version': 0},
        {'status': 'approved'},
        {'request_key': 'non-uuid'},
        {'request_hash': 'short'},
        {'title': '  '},
        {'owner_id': 999},
        {'company_id': 999},
    ],
)
def test_invalid_identity_status_version_and_foreign_keys_fail(local_migration, changes):
    connection, _ = local_migration
    table = task_table(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(**changes)))


def test_model_bootstrap_shape_matches_migration(local_migration):
    connection, _ = local_migration
    inspector = sa.inspect(connection)
    model = HankTask.__table__
    columns = {col['name']: col for col in inspector.get_columns('hank_tasks')}
    assert set(columns) == set(model.columns.keys())
    for column in model.columns:
        assert columns[column.name]['nullable'] == column.nullable
        assert str(columns[column.name]['type']) == str(column.type)
    assert {row['name']: tuple(row['column_names']) for row in inspector.get_indexes('hank_tasks')} == {
        index.name: tuple(column.name for column in index.columns) for index in model.indexes
    }
    assert {row['name'] for row in inspector.get_check_constraints('hank_tasks')} == {
        constraint.name for constraint in model.constraints if isinstance(constraint, sa.CheckConstraint)
    }
    assert {row['name'] for row in inspector.get_unique_constraints('hank_tasks')} == {
        constraint.name for constraint in model.constraints if isinstance(constraint, sa.UniqueConstraint)
    }


def test_model_json_defaults_are_independent_per_task(db_session, test_user):
    first = HankTask(**fields(owner_id=test_user.id))
    second = HankTask(**fields(owner_id=test_user.id))
    db_session.add_all([first, second])
    db_session.flush()
    for name in ('input_json', 'preview_json', 'source_versions_json'):
        first_value, second_value = getattr(first, name), getattr(second, name)
        assert first_value == second_value == {}
        assert first_value is not second_value
        first_value['one_task_only'] = True
        assert second_value == {}
    assert first.result_json is None and second.result_json is None


def test_postgres_migration_and_bootstrap_deny_table_and_sequence_data_api_access():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    migration.upgrade()
    migration_sql = output.getvalue()
    bootstrap_statements = []
    engine = sa.create_mock_engine(
        'postgresql://',
        lambda statement, *args, **kwargs: bootstrap_statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    HankTask.__table__.create(engine, checkfirst=False)
    bootstrap_sql = '\n'.join(bootstrap_statements)
    for sql in (migration_sql, bootstrap_sql):
        assert 'ENABLE ROW LEVEL SECURITY' in sql
        for role in ('PUBLIC', 'anon', 'authenticated'):
            assert f'REVOKE ALL ON TABLE hank_tasks FROM {role}' in sql
            assert f'REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM {role}' in sql
        assert 'CREATE POLICY' not in sql and 'auth.uid()' not in sql
        assert 'FOREIGN KEY(owner_id) REFERENCES users (id)' in sql
        assert 'FOREIGN KEY(company_id) REFERENCES companies (id)' in sql
        assert 'UNIQUE (company_id, request_key)' in sql
        assert 'TIMESTAMP WITH TIME ZONE' in sql
        assert 'ON hank_tasks (company_id, owner_id, created_at, id)' in sql
    output.truncate(0)
    output.seek(0)
    migration.downgrade()
    assert 'DROP TABLE hank_tasks' in output.getvalue()
