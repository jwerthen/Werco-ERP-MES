"""Exercise source-history guards and finalization races in disposable PostgreSQL schemas."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote_nesting_source import (
    SOURCE_TABLES,
)
from app.models.quote_nesting_source import QuoteNestingSourceAttempt as Attempt
from app.models.quote_nesting_source import QuoteNestingSourceBinding as Binding
from app.models.quote_nesting_source import QuoteNestingSourceIntent as Intent
from app.models.quote_nesting_source import QuoteNestingSourceReceipt as Receipt


def assert_nesting_source_races(engine):
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or engine.url.get_backend_name() != 'postgresql'
        or engine.url.host not in {'localhost', '127.0.0.1', 'postgres'}
    ):
        raise RuntimeError('Original source verification requires local disposable PostgreSQL')
    path = Path(__file__).parents[1] / 'alembic/versions/105_nesting_cad_sources.py'
    spec = importlib.util.spec_from_file_location('source_migration_pg', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for bootstrap in ('migration', 'metadata'):
        schema = 'nest_sources_' + uuid4().hex
        quoted = engine.dialect.identifier_preparer.quote(schema)

        def scope(conn):
            conn.exec_driver_sql(f'SET LOCAL search_path TO {quoted}')
            conn.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
            conn.exec_driver_sql("SET LOCAL statement_timeout TO '15s'")

        def make_intent(conn, **changes):
            values = (
                dict(
                    company_id=1,
                    draft_id=1,
                    revision_id=11,
                    revision_number=1,
                    input_sha256='a' * 64,
                    source_sha256='b' * 64,
                    byte_count=12,
                    source_name='synthetic.dxf',
                    mime_type='application/dxf',
                    targets_json=[],
                    targets_sha256='c' * 64,
                    target_count=1,
                    request_key=str(uuid4()),
                    request_hash='d' * 64,
                    created_by=1,
                    created_at=datetime(2026, 9, 8),
                )
                | changes
            )
            return conn.execute(sa.insert(Intent).returning(Intent.id), values).scalar_one()

        def make_attempt(conn, intent_id, ordinal=1):
            key = str(uuid4())
            return conn.execute(
                sa.insert(Attempt).returning(Attempt.id),
                dict(
                    company_id=1,
                    intent_id=intent_id,
                    ordinal=ordinal,
                    object_key=key,
                    storage_ref=f's3://synthetic-bucket/1/nesting-cad/{key}.dxf',
                    provider_json={'backend': 'synthetic'},
                    provider_sha256='e' * 64,
                    created_by=1,
                    created_at=datetime(2026, 9, 8),
                ),
            ).scalar_one()

        def complete(conn, intent_id, attempt_id, part_id):
            receipt_id = conn.execute(
                sa.insert(Receipt).returning(Receipt.id),
                dict(
                    company_id=1,
                    intent_id=intent_id,
                    attempt_id=attempt_id,
                    source_sha256='b' * 64,
                    byte_count=12,
                    created_by=1,
                    verified_at=datetime(2026, 9, 8),
                ),
            ).scalar_one()
            conn.execute(
                sa.insert(Binding),
                dict(
                    company_id=1,
                    receipt_id=receipt_id,
                    intent_id=intent_id,
                    revision_id=11,
                    group_id='g',
                    part_id=part_id,
                    provenance_json={'sourceSha256': 'b' * 64},
                ),
            )

        def refused(conn, statement, params=None, code='23514'):
            savepoint = conn.begin_nested()
            try:
                conn.execute(statement, params or {})
            except sa.exc.DBAPIError as error:
                savepoint.rollback()
                assert error.orig.pgcode == code, str(error.orig)
            else:
                savepoint.rollback()
                raise AssertionError('PostgreSQL accepted prohibited source evidence')

        def race(candidates, part_id):
            barrier = Barrier(2)

            def writer(candidate):
                try:
                    with engine.begin() as conn:
                        scope(conn)
                        barrier.wait(timeout=10)
                        complete(conn, *candidate, part_id)
                    return 'committed'
                except sa.exc.IntegrityError as error:
                    assert error.orig.pgcode == '23505', str(error.orig)
                    return 'conflict'

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(writer, candidates))
            assert sorted(outcomes) == ['committed', 'conflict'], outcomes

        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f'CREATE SCHEMA {quoted}')
                scope(conn)
                temporary_roles = []
                for role in ('anon', 'authenticated'):
                    if not conn.scalar(
                        sa.text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'), {'role': role}
                    ):
                        conn.exec_driver_sql(f'CREATE ROLE {role} NOLOGIN')
                        temporary_roles.append(role)
                for role in ('PUBLIC', 'anon', 'authenticated'):
                    for kind in ('TABLES', 'SEQUENCES'):
                        conn.exec_driver_sql(
                            f'ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted} GRANT ALL ON {kind} TO {role}'
                        )
                for table in ('companies', 'users', 'api_tokens'):
                    conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                    conn.exec_driver_sql(f'INSERT INTO {table} VALUES (1), (2)')
                conn.exec_driver_sql('''CREATE TABLE quote_nesting_revisions (
                    id INTEGER PRIMARY KEY, company_id INTEGER, draft_id INTEGER, revision_number INTEGER,
                    content_sha256 VARCHAR(64), UNIQUE(company_id,id,draft_id,revision_number,content_sha256))''')
                conn.execute(
                    sa.text('INSERT INTO quote_nesting_revisions VALUES (11,1,1,1,:a),(22,2,2,1,:b)'),
                    {'a': 'a' * 64, 'b': 'b' * 64},
                )
                if bootstrap == 'migration':
                    with Operations.context(MigrationContext.configure(conn)):
                        module.upgrade()
                        module.upgrade()
                else:
                    for table in SOURCE_TABLES:
                        table.create(conn)
                for table in SOURCE_TABLES:
                    ref = schema + '.' + table.name
                    assert conn.scalar(
                        sa.text('SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:ref AS regclass)'), {'ref': ref}
                    )
                    for role in ('anon', 'authenticated'):
                        for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                            assert not conn.scalar(
                                sa.text('SELECT has_table_privilege(:role,:ref,:priv)'),
                                {'role': role, 'ref': ref, 'priv': privilege},
                            )
                        for privilege in ('SELECT', 'UPDATE', 'USAGE'):
                            assert not conn.scalar(
                                sa.text('SELECT has_sequence_privilege(:role,:ref,:priv)'),
                                {'role': role, 'ref': ref + '_id_seq', 'priv': privilege},
                            )
                # Scoped test default grants/roles are removed before commit; existing role attributes stay untouched.
                for role in ('PUBLIC', 'anon', 'authenticated'):
                    for kind in ('TABLES', 'SEQUENCES'):
                        conn.exec_driver_sql(
                            f'ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted} REVOKE ALL ON {kind} FROM {role}'
                        )
                    # Parent fixtures inherited grants too and must not keep test roles alive.
                    conn.exec_driver_sql(f'REVOKE ALL ON ALL TABLES IN SCHEMA {quoted} FROM {role}')
                    conn.exec_driver_sql(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA {quoted} FROM {role}')
                for role in temporary_roles:
                    conn.exec_driver_sql(f'DROP ROLE {role}')
                first = make_intent(conn)
                first_attempt = make_attempt(conn, first)
                late_attempt = make_attempt(conn, first, ordinal=2)
                second = make_intent(conn)
                second_attempt = make_attempt(conn, second)
                third = make_intent(conn)
                third_attempt = make_attempt(conn, third)
            # A slow attempt and its retry cannot install different authoritative receipts.
            race([(first, first_attempt), (first, late_attempt)], 'p1')
            # Two independent upload intents cannot replace the same revision's part source.
            race([(second, second_attempt), (third, third_attempt)], 'p2')
            with engine.begin() as conn:
                scope(conn)
                assert conn.scalar(sa.select(sa.func.count()).select_from(Intent)) == 3
                assert conn.scalar(sa.select(sa.func.count()).select_from(Attempt)) == 4
                assert conn.scalar(sa.select(sa.func.count()).select_from(Receipt)) == 2
                assert conn.scalar(sa.select(sa.func.count()).select_from(Binding)) == 2
                for table in SOURCE_TABLES:
                    refused(conn, sa.update(table).values(id=table.c.id))
                    refused(conn, sa.delete(table))
                    refused(conn, sa.text(f'TRUNCATE TABLE {table.name} CASCADE'))
                # Wrong company/revision and attempt digest are rejected independently of app checks.
                refused(
                    conn,
                    sa.insert(Intent),
                    dict(
                        company_id=2,
                        draft_id=1,
                        revision_id=11,
                        revision_number=1,
                        input_sha256='a' * 64,
                        source_sha256='b' * 64,
                        byte_count=12,
                        source_name='synthetic.dxf',
                        mime_type='application/dxf',
                        targets_json=[],
                        targets_sha256='c' * 64,
                        target_count=1,
                        request_key=str(uuid4()),
                        request_hash='d' * 64,
                        created_by=1,
                        created_at=datetime(2026, 9, 8),
                    ),
                    '23503',
                )
                unused = third if conn.scalar(sa.select(Receipt.id).where(Receipt.intent_id == second)) else second
                unused_attempt = third_attempt if unused == third else second_attempt
                refused(
                    conn,
                    sa.insert(Receipt),
                    dict(
                        company_id=1,
                        intent_id=unused,
                        attempt_id=unused_attempt,
                        source_sha256='c' * 64,
                        byte_count=12,
                        created_by=1,
                        verified_at=datetime(2026, 9, 8),
                    ),
                    '23503',
                )
                assert conn.scalar(sa.text('SELECT count(*) FROM quote_nesting_revisions')) == 2
                with Operations.context(MigrationContext.configure(conn)):
                    module.downgrade()
                    module.downgrade()
                    module.upgrade()
                    module.upgrade()
                assert conn.scalar(sa.text('SELECT count(*) FROM quote_nesting_revisions')) == 2
            print(
                f'PostgreSQL source {bootstrap}: tenant/input guards, immutable history, receipt/part races and privileges passed.'
            )
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted} CASCADE')


if __name__ == '__main__':
    owner = sa.create_engine(os.environ['DATABASE_URL'])
    try:
        assert_nesting_source_races(owner)
    finally:
        owner.dispose()
