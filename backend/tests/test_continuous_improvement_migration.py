"""Upgrade/bootstrap parity and private Data API privileges for lean suggestions."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.continuous_improvement import ImprovementActivity, ImprovementSuggestion

pytestmark = [pytest.mark.unit]


def module():
    path = Path(__file__).parents[1] / 'alembic/versions/111_continuous_improvement.py'
    spec = importlib.util.spec_from_file_location('continuous_improvement_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrated(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "improvement.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text('PRAGMA foreign_keys=ON'))
        connection.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(sa.text("INSERT INTO companies VALUES (1, 'Existing company')"))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'Existing user')"))
        migration = module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def fields(**changes):
    return (
        dict(
            company_id=1,
            title='Fixture pin',
            problem='Backward part',
            proposed_solution='Add key',
            expected_benefit='No reversed parts',
            category='poka_yoke',
            created_by=1,
            updated_by=1,
            created_by_name='Manager',
            updated_by_name='Manager',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        | changes
    )


def test_upgrade_replay_downgrade_and_reupgrade_preserve_existing_company(migrated):
    connection, migration = migrated
    table = sa.Table('improvement_suggestions', sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(**fields()))
    migration.upgrade()
    assert connection.execute(sa.select(table.c.title)).scalar() == 'Fixture pin'
    connection.execute(sa.text('DROP INDEX ix_improvement_company_category'))
    migration.upgrade()
    assert 'ix_improvement_company_category' in {
        index['name'] for index in sa.inspect(connection).get_indexes(table.name)
    }
    assert migration.down_revision == '110_hank_workflows'
    migration.downgrade()
    migration.downgrade()
    assert connection.execute(sa.text('SELECT name FROM companies')).scalar() == 'Existing company'
    assert 'improvement_suggestions' not in sa.inspect(connection).get_table_names()
    migration.upgrade()
    assert connection.execute(sa.text('SELECT count(*) FROM improvement_suggestions')).scalar() == 0


@pytest.mark.parametrize('model', [ImprovementSuggestion, ImprovementActivity])
def test_model_bootstrap_schema_matches_migration(migrated, model):
    connection, _ = migrated
    inspector = sa.inspect(connection)
    table = model.__table__
    columns = {item['name']: item for item in inspector.get_columns(table.name)}
    assert set(columns) == set(table.columns.keys())
    for column in table.columns:
        assert columns[column.name]['nullable'] == column.nullable
        assert str(columns[column.name]['type']) == str(column.type)
        if column.server_default is not None:
            assert str(columns[column.name]['default']).strip("'()") == str(column.server_default.arg)
    assert {item['name']: tuple(item['column_names']) for item in inspector.get_indexes(table.name)} == {
        index.name: tuple(column.name for column in index.columns) for index in table.indexes
    }
    assert {item['name']: item['sqltext'] for item in inspector.get_check_constraints(table.name)} == {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


@pytest.mark.parametrize(
    'changes',
    [
        {'status': 'invented'},
        {'priority': 'critical'},
        {'category': 'invented'},
        {'version': 0},
        {'title': ' '},
        {'company_id': 999},
        {'created_by': 999},
        {'status': 'implemented'},
        {'status': 'implemented', 'implementation_notes': ' '},
    ],
)
def test_database_rejects_invalid_workflow_records(migrated, changes):
    connection, _ = migrated
    table = sa.Table('improvement_suggestions', sa.MetaData(), autoload_with=connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(table.insert().values(**fields(**changes)))


def test_postgres_upgrade_and_bootstrap_deny_clients_and_protect_history():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name='postgresql',
            opts={'as_sql': True, 'output_buffer': output},
        )
    )
    migration.upgrade()
    statements = []
    engine = sa.create_mock_engine(
        'postgresql://',
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    for model in (ImprovementSuggestion, ImprovementActivity):
        model.__table__.create(engine, checkfirst=False)
    for sql in (output.getvalue(), '\n'.join(statements)):
        for table in ('improvement_suggestions', 'improvement_activities'):
            assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in sql
            for role in ('PUBLIC', 'anon', 'authenticated'):
                assert f'REVOKE ALL ON TABLE {table} FROM {role}' in sql
                assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}' in sql
        assert 'CREATE POLICY' not in sql and 'SECURITY DEFINER' not in sql
        assert 'BEFORE UPDATE OR DELETE ON improvement_activities' in sql
        assert 'SET search_path = pg_catalog' in sql
        assert 'REVOKE ALL ON FUNCTION prevent_improvement_activity_change() FROM PUBLIC' in sql
        assert 'TIMESTAMP WITH TIME ZONE' in sql
