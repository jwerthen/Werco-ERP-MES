"""Source receipts cannot change identity, cross tenants or replace a part's original."""

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
from app.models.quote_nesting_source import (
    POSTGRES_DDL,
    SOURCE_TABLES,
    SQLITE_DDL,
)
from app.models.quote_nesting_source import QuoteNestingSourceAttempt as Attempt
from app.models.quote_nesting_source import QuoteNestingSourceBinding as Binding
from app.models.quote_nesting_source import QuoteNestingSourceIntent as Intent
from app.models.quote_nesting_source import QuoteNestingSourceReceipt as Receipt


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/105_nesting_cad_sources.py'
    spec = importlib.util.spec_from_file_location('source_migration_105', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_parents(conn):
    for name in ('companies', 'users', 'api_tokens'):
        conn.exec_driver_sql(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql(f'INSERT INTO {name} VALUES (1), (2)')
    conn.exec_driver_sql('''CREATE TABLE quote_nesting_revisions (
        id INTEGER PRIMARY KEY, company_id INTEGER, draft_id INTEGER, revision_number INTEGER,
        content_sha256 VARCHAR(64), UNIQUE(company_id,id,draft_id,revision_number,content_sha256))''')
    for idx, company, draft, number, digest in ((11, 1, 1, 1, 'a'), (12, 1, 1, 2, 'b'), (22, 2, 2, 1, 'c')):
        conn.execute(
            sa.text('INSERT INTO quote_nesting_revisions VALUES (:id,:company,:draft,:number,:sha)'),
            dict(id=idx, company=company, draft=draft, number=number, sha=digest * 64),
        )


def intent(**changes):
    return (
        dict(
            company_id=1,
            draft_id=1,
            revision_id=11,
            revision_number=1,
            input_sha256='a' * 64,
            source_sha256='d' * 64,
            byte_count=12,
            source_name='synthetic.dxf',
            mime_type='application/dxf',
            targets_json=[{'group_id': 'group', 'part_id': 'part'}],
            target_count=1,
            targets_sha256='e' * 64,
            request_key=str(uuid4()),
            request_hash='f' * 64,
            created_by=1,
            created_at=datetime(2026, 9, 8),
        )
        | changes
    )


def attempt(**changes):
    key = str(uuid4())
    return (
        dict(
            company_id=1,
            intent_id=1,
            ordinal=1,
            object_key=key,
            storage_ref=f's3://synthetic-bucket/1/nesting-cad/{key}.dxf',
            provider_json={'backend': 's3'},
            provider_sha256='f' * 64,
            created_by=1,
            created_at=datetime(2026, 9, 8),
        )
        | changes
    )


def receipt(**changes):
    return (
        dict(
            company_id=1,
            intent_id=1,
            attempt_id=1,
            source_sha256='d' * 64,
            byte_count=12,
            created_by=1,
            verified_at=datetime(2026, 9, 8),
        )
        | changes
    )


def binding(**changes):
    return (
        dict(
            company_id=1,
            intent_id=1,
            receipt_id=1,
            revision_id=11,
            group_id='group',
            part_id='part',
            provenance_json={'sourceSha256': 'd' * 64},
        )
        | changes
    )


@pytest.fixture(params=['migration', 'metadata'])
def connection(request):
    engine = sa.create_engine('sqlite://')
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        seed_parents(conn)
        if request.param == 'migration':
            with Operations.context(MigrationContext.configure(conn)):
                migration().upgrade()
        else:
            for table in SOURCE_TABLES:
                table.create(conn)
        conn.execute(sa.insert(Intent), intent(id=1))
        conn.execute(sa.insert(Attempt), attempt(id=1))
        conn.execute(sa.insert(Receipt), receipt(id=1))
        conn.execute(sa.insert(Binding), binding(id=1))
        conn.commit()
        yield conn
    engine.dispose()


@pytest.mark.parametrize('model', [Intent, Attempt, Receipt, Binding])
@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_all_evidence_is_immutable_even_for_bulk_sql(connection, model, operation):
    statement = sa.update(model).values(id=model.id) if operation == 'update' else sa.delete(model)
    with pytest.raises(IntegrityError, match='Original CAD evidence is immutable'):
        connection.execute(statement)
    connection.rollback()
    assert connection.scalar(sa.select(sa.func.count()).select_from(model)) == 1


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'revision_id': 22},
        {'revision_id': 12},
        {'revision_number': 2},
        {'draft_id': 2},
        {'input_sha256': 'b' * 64},
        {'byte_count': 0},
        {'byte_count': 5000000},
        {'target_count': 0},
        {'target_count': 1001},
        {'source_name': ''},
        {'mime_type': ''},
        {'source_sha256': 'short'},
        {'targets_sha256': 'short'},
        {'request_key': 'short'},
    ],
)
def test_intent_requires_exact_revision_and_bounded_original_evidence(connection, changes):
    with pytest.raises(IntegrityError):
        connection.execute(sa.insert(Intent), intent(**changes))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'intent_id': 999},
        {'ordinal': 0},
        {'ordinal': 9},
        {'storage_ref': ''},
        {'object_key': 'bad'},
        {'provider_sha256': 'bad'},
    ],
)
def test_attempt_requires_tenant_intent_and_bounded_fresh_identity(connection, changes):
    with pytest.raises(IntegrityError):
        connection.execute(sa.insert(Attempt), attempt(ordinal=2) | changes)


def test_attempt_ordinals_are_unique_and_never_exceed_eight(connection):
    for ordinal in range(2, 9):
        connection.execute(sa.insert(Attempt), attempt(ordinal=ordinal))
    connection.commit()
    assert connection.scalar(sa.select(sa.func.count()).select_from(Attempt)) == 8
    with pytest.raises(IntegrityError):
        connection.execute(sa.insert(Attempt), attempt(ordinal=8))


@pytest.mark.parametrize(
    'changes',
    [
        {'company_id': 2},
        {'attempt_id': 999},
        {'source_sha256': 'e' * 64},
        {'byte_count': 13},
    ],
)
def test_receipt_must_match_its_intent_and_recorded_attempt(connection, changes):
    # Another valid intent avoids making a uniqueness error mask the intended FK check.
    connection.execute(sa.insert(Intent), intent(id=2))
    connection.execute(sa.insert(Attempt), attempt(id=2, intent_id=2))
    with pytest.raises(IntegrityError, match='FOREIGN KEY'):
        connection.execute(sa.insert(Receipt), receipt(intent_id=2, attempt_id=2) | changes)


@pytest.mark.parametrize('changes', [{'company_id': 2}, {'revision_id': 12}, {'receipt_id': 999}, {'intent_id': 999}])
def test_binding_cannot_change_tenant_revision_or_receipt(connection, changes):
    with pytest.raises(IntegrityError, match='FOREIGN KEY'):
        connection.execute(sa.insert(Binding), binding(part_id='other') | changes)


def test_different_receipt_cannot_replace_original_for_same_saved_part(connection):
    connection.execute(sa.insert(Intent), intent(id=2))
    connection.execute(sa.insert(Attempt), attempt(id=2, intent_id=2))
    connection.execute(sa.insert(Receipt), receipt(id=2, intent_id=2, attempt_id=2))
    with pytest.raises(IntegrityError, match='UNIQUE'):
        connection.execute(sa.insert(Binding), binding(intent_id=2, receipt_id=2))


def test_frozen_migration_matches_bootstrap_and_round_trips():
    module = migration()
    assert module.POSTGRES_DDL == POSTGRES_DDL
    assert module.SQLITE_DDL == SQLITE_DDL
    assert module.down_revision == '104_stock_piece_observations'
    assert len(module.revision) <= 32
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        seed_parents(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
            for table in SOURCE_TABLES:
                inspector = sa.inspect(conn)
                assert {c['name'] for c in inspector.get_columns(table.name)} == set(table.c.keys())
                assert {i['name'] for i in inspector.get_indexes(table.name)} == {i.name for i in table.indexes}
                assert {i['name'] for i in inspector.get_check_constraints(table.name)} == {
                    c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)
                }
            module.downgrade()
            module.downgrade()
            assert not set(SQLITE_DDL) & set(sa.inspect(conn).get_table_names())
            assert conn.scalar(sa.text('SELECT count(*) FROM quote_nesting_revisions')) == 3
            module.upgrade()
    engine.dispose()


def test_postgres_offline_contains_complete_tenant_immutable_and_privilege_guards():
    output = io.StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    ):
        migration().upgrade()
    sql = output.getvalue()
    for name in POSTGRES_DDL:
        assert f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY' in sql
        assert f'REVOKE ALL ON TABLE {name} FROM PUBLIC' in sql
        assert f'REVOKE ALL ON SEQUENCE {name}_id_seq FROM authenticated' in sql
        assert f'REVOKE ALL ON SEQUENCE {name}_id_seq FROM anon' in sql
        for operation in ('UPDATE', 'DELETE', 'TRUNCATE'):
            assert f'BEFORE {operation} ON {name}' in sql
    assert 'SECURITY DEFINER' not in sql
    assert "SET search_path = ''" in sql
    assert 'FOREIGN KEY(company_id, revision_id, draft_id, revision_number, input_sha256)' in sql
