"""Independent database invariants for saved-input run and checkpoint history."""

import importlib.util
import io
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision
from app.models.quote_nesting_run import QuoteNestingRun, QuoteNestingRunCheckpoint

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 8, 12)
LEASE = '7d2f1a6e-1122-4333-8444-0123456789ab'


def migration(filename):
    path = Path(__file__).parents[1] / 'alembic/versions' / (filename + '.py')
    spec = importlib.util.spec_from_file_location(filename, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revision(**changes):
    return dict(
        id=1,
        company_id=1,
        draft_id=1,
        revision_number=1,
        draft_version=1,
        name='Synthetic saved inputs',
        estimate_json={'groups': []},
        content_sha256='a' * 64,
        payload_schema_version=6,
        payload_bytes=13,
        created_by=1,
        created_at=NOW,
        request_key=str(uuid4()),
        request_hash='b' * 64,
        review_issues_json=[],
        **changes,
    )


def run(**changes):
    return {
        **dict(
            id=1,
            company_id=1,
            draft_id=1,
            revision_id=1,
            revision_number=1,
            input_sha256='a' * 64,
            request_key=str(uuid4()),
            request_hash='c' * 64,
            created_by=1,
            settings_json={'limit': 36},
            created_at=NOW,
            updated_at=NOW,
            release_identity='synthetic-build',
        ),
        **changes,
    }


def checkpoint(**changes):
    return {
        **dict(
            id=1,
            company_id=1,
            run_id=1,
            lease_token=LEASE,
            sequence=1,
            group_id='carbon-125',
            stock_option_id='sheet-a',
            result_json={'complete': False, 'unplaced': 2},
            content_sha256='d' * 64,
            payload_bytes=31,
            created_at=NOW,
        ),
        **changes,
    }


@pytest.fixture(params=['migration', 'metadata'])
def connection(request):
    engine = sa.create_engine('sqlite://')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('companies', 'users', 'api_tokens'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO companies VALUES (1), (2)')
        conn.exec_driver_sql('INSERT INTO users VALUES (1), (2)')
        conn.exec_driver_sql('INSERT INTO api_tokens VALUES (1), (2)')
        if request.param == 'migration':
            with Operations.context(MigrationContext.configure(conn)):
                migration('101_quote_nesting_drafts').upgrade()
                migration('102_quote_nesting_runs').upgrade()
        else:
            for table in (
                QuoteNestingDraft.__table__,
                QuoteNestingRevision.__table__,
                QuoteNestingRun.__table__,
                QuoteNestingRunCheckpoint.__table__,
            ):
                table.create(conn)
        conn.execute(sa.insert(QuoteNestingDraft), dict(id=1, company_id=1, name='Synthetic draft', created_by=1))
        conn.execute(sa.insert(QuoteNestingRevision), revision())
        conn.commit()
        yield conn
    engine.dispose()


def claim(conn):
    conn.execute(
        sa.update(QuoteNestingRun)
        .where(QuoteNestingRun.id == 1)
        .values(
            version=2, status='RUNNING', lease_token=LEASE, lease_expires_at=NOW + timedelta(minutes=3), started_at=NOW
        )
    )


def advance(conn, **changes):
    current = conn.scalar(sa.select(QuoteNestingRun.version).where(QuoteNestingRun.id == 1))
    return conn.execute(
        sa.update(QuoteNestingRun).where(QuoteNestingRun.id == 1).values(version=current + 1, **changes)
    )


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'revision_id': 99},
        {'draft_id': 99},
        {'revision_number': 2},
        {'input_sha256': 'e' * 64},
    ],
)
def test_run_must_reference_exact_tenant_revision_and_digest(connection, changes):
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRun), run(**changes))
    assert connection.scalar(sa.select(sa.func.count()).select_from(QuoteNestingRun)) == 0


@pytest.mark.parametrize('status', ['QUEUED', 'RUNNING'])
def test_only_one_active_run_per_company_and_retry_key_remains_unique(connection, status):
    first = run()
    connection.execute(sa.insert(QuoteNestingRun), first)
    if status == 'RUNNING':
        claim(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRun), run(id=2))
    advance(connection, status='CANCELLED', finished_at=NOW)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRun), run(id=2, request_key=first['request_key']))
    connection.execute(sa.insert(QuoteNestingRun), run(id=2))
    assert connection.scalar(sa.select(sa.func.count()).select_from(QuoteNestingRun)) == 2


@pytest.mark.parametrize(
    'changes',
    [
        {'status': 'COMPLETED'},
        {'status': 'APPROVED'},
        {'version': 2},
        {'evaluated_count': 1},
        {'completed_count': 1},
        {'checkpoint_bytes': 1},
        {'lease_token': LEASE},
        {'node_version': 'v22'},
    ],
)
def test_new_run_cannot_start_with_fabricated_execution_history(connection, changes):
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRun), run(**changes))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'draft_id': 2},
        {'revision_id': 2},
        {'revision_number': 2},
        {'input_sha256': 'e' * 64},
        {'request_key': str(uuid4())},
        {'request_hash': 'f' * 64},
        {'created_by': 2},
        {'submitted_api_token_id': 1},
        {'created_at': NOW + timedelta(seconds=1)},
        {'settings_json': {'limit': 3}},
        {'release_identity': 'different-build'},
    ],
)
def test_raw_updates_cannot_replace_saved_input_identity(connection, changes):
    connection.execute(sa.insert(QuoteNestingRun), run())
    with pytest.raises(IntegrityError, match='immutable'), connection.begin_nested():
        advance(connection, **changes)


def test_claim_is_compare_and_swap_and_transition_requires_next_version(connection):
    connection.execute(sa.insert(QuoteNestingRun), run())
    statement = (
        sa.update(QuoteNestingRun)
        .where(QuoteNestingRun.id == 1, QuoteNestingRun.company_id == 1, QuoteNestingRun.version == 1)
        .values(
            version=2, status='RUNNING', lease_token=LEASE, lease_expires_at=NOW + timedelta(minutes=3), started_at=NOW
        )
    )
    assert connection.execute(statement).rowcount == 1
    assert connection.execute(statement).rowcount == 0
    for changes in ({'version': 2}, {'version': 4}, {'version': 3, 'status': 'QUEUED'}):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.update(QuoteNestingRun).where(QuoteNestingRun.id == 1).values(**changes))


@pytest.mark.parametrize('status', ['COMPLETED', 'PARTIAL', 'CANCELLED', 'FAILED'])
def test_terminal_run_cannot_be_reopened_changed_or_deleted(connection, status):
    connection.execute(sa.insert(QuoteNestingRun), run())
    claim(connection)
    advance(connection, status=status, finished_at=NOW)
    for statement in (
        sa.update(QuoteNestingRun).values(status='RUNNING', version=4),
        sa.update(QuoteNestingRun).values(error_message='edited', version=4),
        sa.delete(QuoteNestingRun),
    ):
        with pytest.raises(IntegrityError, match='immutable'), connection.begin_nested():
            connection.execute(statement)


def test_execution_identity_and_progress_cannot_be_rewritten(connection):
    connection.execute(sa.insert(QuoteNestingRun), run())
    claim(connection)
    advance(
        connection,
        solver_version='solver-v1',
        node_version='v22.0.0',
        bundle_sha256='e' * 64,
        evaluated_count=2,
        completed_count=1,
        checkpoint_bytes=100,
        cancel_requested=True,
    )
    for changes in (
        {'lease_token': str(uuid4())},
        {'started_at': NOW + timedelta(seconds=1)},
        {'solver_version': 'solver-v2'},
        {'node_version': None},
        {'bundle_sha256': 'f' * 64},
        {'evaluated_count': 1},
        {'completed_count': 0},
        {'checkpoint_bytes': 99},
        {'cancel_requested': False},
        {'evaluated_count': 37},
        {'checkpoint_bytes': 25165825},
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            advance(connection, **changes)
    advance(connection, lease_expires_at=NOW + timedelta(minutes=4))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'run_id': 2},
        {'lease_token': str(uuid4())},
        {'sequence': 0},
        {'sequence': 37},
        {'payload_bytes': 0},
        {'payload_bytes': 8388609},
        {'content_sha256': 'bad'},
        {'group_id': ''},
    ],
)
def test_checkpoint_requires_current_tenant_lease_and_bounded_metadata(connection, changes):
    connection.execute(sa.insert(QuoteNestingRun), run())
    claim(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint(**changes))


def test_checkpoint_keys_are_unique_and_terminal_lease_cannot_append(connection):
    connection.execute(sa.insert(QuoteNestingRun), run())
    claim(connection)
    connection.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint())
    for changes in ({'id': 2, 'stock_option_id': 'other'}, {'id': 2, 'sequence': 2}):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint(**changes))
    advance(connection, status='CANCELLED', finished_at=NOW)
    with pytest.raises(IntegrityError, match='running'), connection.begin_nested():
        connection.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint(id=2, sequence=2, stock_option_id='other'))
    assert connection.scalar(sa.select(sa.func.count()).select_from(QuoteNestingRunCheckpoint)) == 1


@pytest.mark.parametrize('delete', [False, True])
def test_checkpoint_is_immutable_through_raw_sql_and_orm(connection, delete):
    connection.execute(sa.insert(QuoteNestingRun), run())
    claim(connection)
    connection.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint())
    statement = (
        sa.delete(QuoteNestingRunCheckpoint)
        if delete
        else sa.update(QuoteNestingRunCheckpoint).values(result_json={'complete': True})
    )
    with pytest.raises(IntegrityError, match='immutable'), connection.begin_nested():
        connection.execute(statement)
    connection.commit()
    with Session(bind=connection) as session:
        row = session.query(QuoteNestingRunCheckpoint).one()
        if delete:
            session.delete(row)
        else:
            row.result_json = {'complete': True}
        with pytest.raises(ValueError, match='immutable'):
            session.flush()
        session.rollback()


def test_migration_roundtrip_preserves_existing_immutable_draft_data():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('companies', 'users', 'api_tokens'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            conn.exec_driver_sql(f'INSERT INTO {table} VALUES (1)')
        with Operations.context(MigrationContext.configure(conn)):
            migration('101_quote_nesting_drafts').upgrade()
            conn.execute(sa.insert(QuoteNestingDraft), dict(id=1, company_id=1, name='Retained draft', created_by=1))
            conn.execute(sa.insert(QuoteNestingRevision), revision())
            module = migration('102_quote_nesting_runs')
            assert module.down_revision == '101_quote_nesting_drafts'
            module.upgrade()
            module.upgrade()
            conn.execute(sa.insert(QuoteNestingRun), run())
            claim(conn)
            conn.execute(sa.insert(QuoteNestingRunCheckpoint), checkpoint())
            module.downgrade()
            module.downgrade()
            assert not sa.inspect(conn).has_table('quote_nesting_runs')
            assert conn.scalar(sa.select(QuoteNestingRevision.content_sha256)) == 'a' * 64
            module.upgrade()
            conn.execute(sa.insert(QuoteNestingRun), run())
    engine.dispose()


def test_postgres_bootstrap_and_migration_compile_same_security_boundaries():
    output = io.StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    ):
        migration('102_quote_nesting_runs').upgrade()
    emitted = []
    mock = sa.create_mock_engine(
        'postgresql://', lambda statement, *args, **kwargs: emitted.append(str(statement.compile(dialect=mock.dialect)))
    )
    for table in (QuoteNestingRun.__table__, QuoteNestingRunCheckpoint.__table__):
        table.create(mock)
    for sql in (output.getvalue(), '\n'.join(emitted)):
        normalized = ' '.join(sql.split())
        assert 'UNIQUE INDEX ix_quote_nest_run_active_company' in normalized
        assert "WHERE status IN ('QUEUED', 'RUNNING')" in normalized
        assert 'FOREIGN KEY(company_id, revision_id, draft_id, revision_number, input_sha256)' in normalized
        assert 'FOREIGN KEY(company_id, run_id, lease_token)' in normalized
        assert 'ON DELETE CASCADE' not in normalized
        for table in ('quote_nesting_runs', 'quote_nesting_run_checkpoints'):
            assert f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in normalized
            for role in ('PUBLIC', 'anon', 'authenticated'):
                assert f'REVOKE ALL ON TABLE {table} FROM {role}' in normalized
                assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}' in normalized
            for operation in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                assert f'BEFORE {operation} ON {table}' in normalized
        assert "SET search_path = ''" in normalized
        assert 'TG_TABLE_SCHEMA' in normalized and 'FOR UPDATE' in normalized
        assert 'CREATE POLICY' not in normalized
