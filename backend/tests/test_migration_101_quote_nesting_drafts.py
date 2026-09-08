"""The migration and metadata bootstrap must enforce the same snapshot invariants."""

import importlib.util
import io
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/101_quote_nesting_drafts.py'
    spec = importlib.util.spec_from_file_location('nest_draft_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=['migration', 'metadata'])
def connection(request):
    engine = sa.create_engine('sqlite://')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        conn.exec_driver_sql('CREATE TABLE companies (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO companies VALUES (1), (2)')
        conn.exec_driver_sql('INSERT INTO users VALUES (1)')
        if request.param == 'migration':
            with Operations.context(MigrationContext.configure(conn)):
                migration().upgrade()
        else:
            QuoteNestingDraft.__table__.create(conn)
            QuoteNestingRevision.__table__.create(conn)
        conn.execute(
            sa.insert(QuoteNestingDraft), {'id': 1, 'company_id': 1, 'name': 'Synthetic draft', 'created_by': 1}
        )
        conn.commit()
        yield conn
    engine.dispose()


def revision(**changes):
    return {
        'company_id': 1,
        'draft_id': 1,
        'revision_number': 1,
        'draft_version': 1,
        'name': 'Synthetic revision',
        'estimate_json': {'name': 'Synthetic estimate'},
        'content_sha256': 'a' * 64,
        'request_hash': 'b' * 64,
        'payload_schema_version': 6,
        'payload_bytes': 29,
        'created_by': 1,
        'request_key': str(uuid4()),
        'review_issues_json': [],
        **changes,
    }


def test_parent_and_revision_cannot_disagree_about_company(connection):
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRevision), revision(company_id=2))
    assert connection.scalar(sa.select(sa.func.count()).select_from(QuoteNestingRevision)) == 0


@pytest.mark.parametrize(
    'mutation',
    [
        {'revision_number': 2},
        {'draft_version': 0},
        {'payload_bytes': 0},
        {'payload_schema_version': 0},
        {'content_sha256': 'invalid'},
        {'request_key': 'invalid'},
        {'name': '   '},
    ],
)
def test_revision_constraints_reject_inconsistent_metadata(connection, mutation):
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRevision), revision(**mutation))


def test_company_wide_request_keys_and_revision_numbers_are_unique(connection):
    first = revision()
    connection.execute(sa.insert(QuoteNestingRevision), first)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRevision), revision())
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            sa.insert(QuoteNestingRevision),
            revision(revision_number=2, draft_version=2, request_key=first['request_key']),
        )
    connection.execute(
        sa.insert(QuoteNestingDraft), {'id': 2, 'company_id': 2, 'name': 'Other tenant', 'created_by': 1}
    )
    connection.execute(
        sa.insert(QuoteNestingRevision), revision(company_id=2, draft_id=2, request_key=first['request_key'])
    )
    assert connection.scalar(sa.select(sa.func.count()).select_from(QuoteNestingRevision)) == 2


@pytest.mark.parametrize(
    'statement',
    [
        "UPDATE quote_nesting_revisions SET name='Changed' WHERE draft_id=1",
        'DELETE FROM quote_nesting_revisions WHERE draft_id=1',
    ],
)
def test_database_guards_refuse_raw_revision_mutation(connection, statement):
    connection.execute(sa.insert(QuoteNestingRevision), revision())
    with pytest.raises(IntegrityError, match='immutable'), connection.begin_nested():
        connection.exec_driver_sql(statement)
    assert connection.scalar(sa.select(QuoteNestingRevision.name)) == 'Synthetic revision'


@pytest.mark.parametrize('delete', [False, True])
def test_mapper_guards_refuse_revision_mutation(connection, delete):
    connection.execute(sa.insert(QuoteNestingRevision), revision())
    connection.commit()
    with Session(bind=connection) as session:
        row = session.query(QuoteNestingRevision).one()
        if delete:
            session.delete(row)
        else:
            row.name = 'Changed'
        with pytest.raises(ValueError, match='immutable'):
            session.flush()
        session.rollback()
    assert connection.scalar(sa.select(QuoteNestingRevision.name)) == 'Synthetic revision'


def test_header_remains_draft_and_cas_advances_exactly_once(connection):
    for values in ({'status': 'APPROVED'}, {'version': 2}, {'name': ''}):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.update(QuoteNestingDraft).where(QuoteNestingDraft.id == 1).values(**values))
    statement = (
        sa.update(QuoteNestingDraft)
        .where(QuoteNestingDraft.company_id == 1, QuoteNestingDraft.id == 1, QuoteNestingDraft.version == 1)
        .values(version=2, latest_revision_number=2)
    )
    assert connection.execute(statement).rowcount == 1
    assert connection.execute(statement).rowcount == 0


def test_migration_roundtrip_is_idempotent_and_preserves_existing_tenants():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE companies (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO companies VALUES (10)')
        with Operations.context(MigrationContext.configure(conn)):
            module = migration()
            assert module.down_revision == '100_receiving_supplier_followup'
            module.upgrade()
            module.upgrade()
            module.downgrade()
            module.downgrade()
            assert set(sa.inspect(conn).get_table_names()) == {'companies', 'users'}
            assert conn.scalar(sa.text('SELECT id FROM companies')) == 10
            module.upgrade()
            assert sa.inspect(conn).has_table('quote_nesting_revisions')
    engine.dispose()


def test_postgres_migration_and_metadata_emit_private_immutable_schema():
    output = io.StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    ):
        migration().upgrade()
    emitted = []
    mock = sa.create_mock_engine(
        'postgresql://', lambda statement, *args, **kwargs: emitted.append(str(statement.compile(dialect=mock.dialect)))
    )
    QuoteNestingDraft.__table__.create(mock)
    QuoteNestingRevision.__table__.create(mock)
    for sql in (output.getvalue(), '\n'.join(emitted)):
        assert 'FOREIGN KEY(company_id, draft_id) REFERENCES quote_nesting_drafts (company_id, id)' in sql
        assert 'UNIQUE (company_id, id)' in sql
        assert 'UNIQUE (company_id, request_key)' in sql
        assert 'ON DELETE CASCADE' not in sql
        for table in ('quote_nesting_drafts', 'quote_nesting_revisions'):
            assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in sql
            for role in ('PUBLIC', 'anon', 'authenticated'):
                assert f'REVOKE ALL ON TABLE {table} FROM {role}' in sql
                assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}' in sql
        for operation in ('UPDATE', 'DELETE', 'TRUNCATE'):
            assert f'BEFORE {operation} ON quote_nesting_revisions' in sql
        assert "SET search_path = ''" in sql
        assert 'CREATE POLICY' not in sql
