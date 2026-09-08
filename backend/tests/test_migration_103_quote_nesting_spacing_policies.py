"""Independent SQL and metadata checks for policy evidence, not manufacturing approval."""

import importlib.util
import io
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent as Event
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingPolicy as Policy
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingRevision as Revision

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 9, 12)
MODELS = (Policy, Revision, Event)


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/103_quote_nesting_spacing_policies.py'
    spec = importlib.util.spec_from_file_location('spacing_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revision(**changes):
    return {
        'id': 1,
        'company_id': 1,
        'policy_id': 1,
        'revision_number': 1,
        'name': 'Synthetic quoting allowance',
        'content_json': {'bands': []},
        'content_sha256': 'a' * 64,
        'payload_schema_version': 1,
        'payload_bytes': 12,
        'created_by': 1,
        'created_at': NOW,
        **changes,
    }


def event(**changes):
    return {
        'id': 1,
        'company_id': 1,
        'policy_id': 1,
        'policy_version': 1,
        'kind': 'REVISION_CREATED',
        'revision_id': 1,
        'revision_number': 1,
        'content_sha256': 'a' * 64,
        'publication_id': None,
        'effective_at': None,
        'reason': 'Synthetic review evidence',
        'created_by': 1,
        'created_at': NOW,
        'request_key': str(uuid4()),
        'request_hash': 'b' * 64,
        **changes,
    }


@pytest.fixture(params=['migration', 'metadata'])
def connection(request):
    engine = sa.create_engine('sqlite://')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('companies', 'users', 'api_tokens'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            conn.exec_driver_sql(f'INSERT INTO {table} VALUES (1),(2)')
        if request.param == 'migration':
            with Operations.context(MigrationContext.configure(conn)):
                migration().upgrade()
        else:
            for model in MODELS:
                model.__table__.create(conn)
        # Reads and schema creation must never invent approved/default policy data.
        assert all(conn.scalar(sa.select(sa.func.count()).select_from(model.__table__)) == 0 for model in MODELS)
        conn.execute(Policy.__table__.insert().values(id=1, company_id=1, created_by=1, created_at=NOW, updated_at=NOW))
        conn.commit()
        yield conn
    engine.dispose()


def advance(conn, version, latest=1):
    return conn.execute(
        Policy.__table__.update()
        .where(Policy.id == 1, Policy.version == version - 1)
        .values(version=version, latest_revision_number=latest, updated_at=NOW)
    ).rowcount


def create_revision(conn):
    assert advance(conn, 1) == 1
    conn.execute(Revision.__table__.insert().values(**revision()))
    conn.execute(Event.__table__.insert().values(**event()))


def publish(conn):
    create_revision(conn)
    assert advance(conn, 2) == 1
    conn.execute(
        Event.__table__.insert().values(
            **event(id=2, policy_version=2, kind='PUBLISHED', effective_at=NOW + timedelta(days=1))
        )
    )


@pytest.mark.parametrize(
    'changes',
    [
        {'version': 1},
        {'latest_revision_number': 1},
        {'created_by': 999},
        {'company_id': 999},
    ],
)
def test_policy_cannot_bootstrap_fake_history_or_foreign_references(connection, changes):
    values = dict(id=2, company_id=2, created_by=1, created_at=NOW, updated_at=NOW)
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(Policy.__table__.insert().values(**{**values, **changes}))


def test_single_policy_and_exact_cas_counters(connection):
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(Policy.__table__.insert().values(company_id=1, created_by=1, created_at=NOW, updated_at=NOW))
    assert advance(connection, 1) == 1
    assert advance(connection, 1) == 0
    for changes in (
        {'version': 1},
        {'version': 3},
        {'version': 2, 'latest_revision_number': 0},
        {'version': 2, 'latest_revision_number': 3},
        {'version': 2, 'company_id': 2},
    ):
        with connection.begin_nested(), pytest.raises(sa.exc.IntegrityError):
            connection.execute(Policy.__table__.update().values(**changes))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'policy_id': 2},
        {'revision_number': 2},
        {'content_sha256': 'bad'},
        {'payload_bytes': 0},
        {'payload_bytes': 65537},
        {'payload_schema_version': 0},
        {'name': ' '},
    ],
)
def test_revision_references_and_payload_limits(connection, changes):
    assert advance(connection, 1) == 1
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(Revision.__table__.insert().values(**revision(**changes)))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'policy_id': 2},
        {'revision_id': 2},
        {'revision_number': 2},
        {'content_sha256': 'c' * 64},
        {'policy_version': 2},
        {'kind': 'APPROVED'},
        {'reason': ' '},
        {'request_key': 'short'},
        {'request_hash': 'short'},
        {'effective_at': NOW},
        {'publication_id': 1},
    ],
)
def test_event_cannot_forge_context_version_or_command_shape(connection, changes):
    assert advance(connection, 1) == 1
    connection.execute(Revision.__table__.insert().values(**revision()))
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(Event.__table__.insert().values(**event(**changes)))


def test_publication_requires_recorded_revision_and_cannot_backdate(connection):
    assert advance(connection, 1) == 1
    connection.execute(Revision.__table__.insert().values(**revision()))
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(Event.__table__.insert().values(**event(kind='PUBLISHED', effective_at=NOW)))
    connection.execute(Event.__table__.insert().values(**event()))
    assert advance(connection, 2) == 1
    for effective in (None, NOW - timedelta(microseconds=1)):
        with connection.begin_nested(), pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                Event.__table__.insert().values(
                    **event(id=2, policy_version=2, kind='PUBLISHED', effective_at=effective)
                )
            )


def test_shared_request_identity_and_effective_schedule_conflicts(connection):
    publish(connection)
    key = connection.scalar(sa.select(Event.request_key).where(Event.id == 1))
    assert advance(connection, 3) == 1
    for changes in (
        {'request_key': key, 'effective_at': NOW + timedelta(days=2)},
        {'effective_at': NOW + timedelta(days=1)},
    ):
        with connection.begin_nested(), pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                Event.__table__.insert().values(**event(id=3, policy_version=3, kind='PUBLISHED', **changes))
            )


def test_withdrawal_requires_exact_publication_and_is_once_only(connection):
    publish(connection)
    assert advance(connection, 3) == 1
    for changes in (
        {'publication_id': 1},
        {'publication_id': 999},
        {'publication_id': 2, 'content_sha256': 'c' * 64},
        {'publication_id': 2, 'company_id': 2},
    ):
        with connection.begin_nested(), pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                Event.__table__.insert().values(**event(id=3, policy_version=3, kind='WITHDRAWN', **changes))
            )
    connection.execute(
        Event.__table__.insert().values(**event(id=3, policy_version=3, kind='WITHDRAWN', publication_id=2))
    )
    assert advance(connection, 4) == 1
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            Event.__table__.insert().values(**event(id=4, policy_version=4, kind='WITHDRAWN', publication_id=2))
        )


@pytest.mark.parametrize(
    'model,command',
    [(Revision, 'update'), (Revision, 'delete'), (Event, 'update'), (Event, 'delete'), (Policy, 'delete')],
)
def test_bulk_sql_cannot_mutate_history(connection, model, command):
    publish(connection)
    statement = model.__table__.delete() if command == 'delete' else model.__table__.update().values(created_by=2)
    with pytest.raises(sa.exc.IntegrityError, match='immutable'):
        connection.execute(statement)


def test_orm_guards_refuse_mutation_before_sql(connection):
    publish(connection)
    connection.commit()
    for model in (Revision, Event):
        with Session(connection) as session:
            row = session.get(model, 1)
            row.created_by = 2
            with pytest.raises(ValueError, match='immutable'):
                session.flush()


def test_migration_is_additive_idempotent_and_reversible():
    engine = sa.create_engine('sqlite://')
    with engine.connect() as conn:
        for table in ('companies', 'users', 'api_tokens'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE historical_input (content TEXT)')
        conn.exec_driver_sql("INSERT INTO historical_input VALUES ('unchanged hash and bytes')")
        with Operations.context(MigrationContext.configure(conn)):
            for _ in range(2):
                migration().upgrade()
                migration().upgrade()
                assert all(
                    conn.scalar(sa.select(sa.func.count()).select_from(model.__table__)) == 0 for model in MODELS
                )
                migration().downgrade()
                migration().downgrade()
                assert (
                    conn.exec_driver_sql('SELECT content FROM historical_input').scalar_one()
                    == 'unchanged hash and bytes'
                )
    engine.dispose()


@pytest.mark.parametrize('source', ['metadata', 'migration'])
def test_postgres_ddl_has_security_and_immutable_bulk_guards(source):
    statements = []
    engine = sa.create_mock_engine(
        'postgresql://', lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect)))
    )
    if source == 'metadata':
        for model in MODELS:
            model.__table__.create(engine)
        ddl = '\n'.join(statements)
    else:
        output = io.StringIO()
        with Operations.context(
            MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
        ):
            migration().upgrade()
        ddl = output.getvalue()
    for model in MODELS:
        table = model.__tablename__
        assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in ddl
        assert f'REVOKE ALL ON TABLE {table} FROM PUBLIC' in ddl
        assert f'REVOKE ALL ON TABLE {table} FROM anon' in ddl
        assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM authenticated' in ddl
        assert f'BEFORE TRUNCATE ON {table}' in ddl
    assert 'CREATE POLICY' not in ddl
    assert "SET search_path = ''" in ddl and 'FOR UPDATE' in ddl and 'TG_TABLE_SCHEMA' in ddl
    assert 'fk_nest_spacing_event_revision' in ddl and 'fk_nest_spacing_withdrawal_publication' in ddl
