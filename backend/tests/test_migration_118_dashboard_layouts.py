"""Saved dashboard layouts: additive, isolated per company/user, reversible and default-deny."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.dashboard_layout import DashboardLayout

VERSIONS = Path(__file__).parents[1] / 'alembic/versions'
FILENAME = '118_dashboard_layouts.py'
pytestmark = [pytest.mark.unit]


def module(filename=FILENAME):
    spec = importlib.util.spec_from_file_location(filename.removesuffix('.py'), VERSIONS / filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def fields(**changes):
    return {
        'company_id': 1,
        'user_id': 1,
        'created_at': datetime(2026, 11, 11, tzinfo=timezone.utc),
        'updated_at': datetime(2026, 11, 11, tzinfo=timezone.utc),
    } | changes


@pytest.fixture
def local_migration(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "dashboard-layouts-migration.db"}')
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


def layout_table(connection):
    return sa.Table('dashboard_layouts', sa.MetaData(), autoload_with=connection)


def test_revision_chain_follows_the_short_declaration_head():
    migration = module()
    assert migration.revision == '118_dashboard_layouts'
    assert migration.down_revision == '117_operation_short_declared'
    assert len(migration.revision) <= 32


def test_upgrade_replay_downgrade_reupgrade_preserves_existing_people(local_migration):
    connection, migration = local_migration
    table = layout_table(connection)
    assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 0
    saved_layout = {'format_version': 1, 'widgets': [{'id': 'kpi.overdue', 'size': None}]}
    connection.execute(table.insert().values(**fields(layout_json=saved_layout)))
    migration.upgrade()  # replay is a no-op
    saved = connection.execute(sa.select(table)).mappings().one()
    assert saved['layout_json'] == saved_layout and saved['version'] == 1
    # A partial bootstrap recovers each missing index without rewriting layouts.
    for index in ('ix_dashboard_layouts_user_id', 'ix_dashboard_layouts_company_id'):
        connection.execute(sa.text(f'DROP INDEX {index}'))
        migration.upgrade()
        assert index in {row['name'] for row in sa.inspect(connection).get_indexes(table.name)}
    assert connection.execute(sa.select(table.c.layout_json)).scalar() == saved_layout
    migration.downgrade()
    migration.downgrade()  # a second downgrade is a no-op, not an error
    assert 'dashboard_layouts' not in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.text('SELECT name FROM companies WHERE id=1')).scalar() == 'Existing company'
    assert connection.execute(sa.text('SELECT name FROM users WHERE id=1')).scalar() == 'Existing user'
    migration.upgrade()
    assert connection.execute(sa.text('SELECT count(*) FROM dashboard_layouts')).scalar() == 0


def test_defaults_and_one_layout_per_person_within_company(local_migration):
    connection, _ = local_migration
    table = layout_table(connection)
    connection.execute(table.insert().values(**fields()))
    row = connection.execute(sa.select(table)).mappings().one()
    assert row['version'] == 1 and row['layout_json'] == {}
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
        {'layout_json': sa.null()},
        {'created_at': sa.null()},
        {'updated_at': sa.null()},
    ],
)
def test_invalid_versions_missing_required_values_and_foreign_keys_fail(local_migration, changes):
    connection, _ = local_migration
    table = layout_table(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(**changes)))


def test_model_bootstrap_columns_indexes_constraints_and_defaults_match_migration(local_migration):
    connection, _ = local_migration
    inspector = sa.inspect(connection)
    model = DashboardLayout.__table__
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
    assert {
        (tuple(fk['constrained_columns']), fk['referred_table']) for fk in inspector.get_foreign_keys(model.name)
    } == {(('company_id',), 'companies'), (('user_id',), 'users')}


def test_model_json_defaults_are_independent_and_timestamps_start_in_utc(db_session, test_user, operator_user):
    first = DashboardLayout(company_id=1, user_id=test_user.id)
    second = DashboardLayout(company_id=1, user_id=operator_user.id)
    db_session.add_all([first, second])
    db_session.flush()
    assert first.layout_json == second.layout_json == {}
    assert first.layout_json is not second.layout_json
    first.layout_json['only_this_person'] = True
    assert second.layout_json == {}
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
    DashboardLayout.__table__.create(engine, checkfirst=False)
    for sql in (migration_sql, '\n'.join(statements)):
        assert 'ALTER TABLE dashboard_layouts ENABLE ROW LEVEL SECURITY' in sql
        for role in ('PUBLIC', 'anon', 'authenticated'):
            assert f'REVOKE ALL ON TABLE dashboard_layouts FROM {role}' in sql
            assert f'REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM {role}' in sql
        for role in ('anon', 'authenticated'):
            assert f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')" in sql
        assert 'service_role' not in sql
        assert 'CREATE POLICY' not in sql and 'auth.uid()' not in sql
        assert 'FOREIGN KEY(company_id) REFERENCES companies (id)' in sql
        assert 'FOREIGN KEY(user_id) REFERENCES users (id)' in sql
        assert 'UNIQUE (company_id, user_id)' in sql
        assert 'CHECK (version >= 1)' in sql
        assert 'TIMESTAMP WITH TIME ZONE' in sql
        assert 'INSERT INTO' not in sql
    assert 'CREATE INDEX ix_dashboard_layouts_company_id ON dashboard_layouts (company_id)' in migration_sql
    assert 'CREATE INDEX ix_dashboard_layouts_user_id ON dashboard_layouts (user_id)' in migration_sql
    output.seek(0)
    output.truncate(0)
    migration.downgrade()
    assert 'DROP TABLE dashboard_layouts' in output.getvalue()
