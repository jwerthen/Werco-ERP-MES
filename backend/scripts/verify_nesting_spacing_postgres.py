"""Actual concurrent policy writes in a uniquely named, disposable PostgreSQL schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingEvent as Event
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingPolicy as Policy
from app.models.quote_nesting_spacing_policy import QuoteNestingSpacingRevision as Revision


def assert_spacing_policy_races(engine):
    """Never target remote databases, E2E seed tables or real manufacturing records."""
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or engine.url.get_backend_name() != 'postgresql'
        or engine.url.host not in {'localhost', '127.0.0.1', 'postgres'}
    ):
        raise RuntimeError('Spacing policy races require a local disposable PostgreSQL database')
    schema = 'nest_policy_check_' + uuid4().hex
    quoted = engine.dialect.identifier_preparer.quote(schema)
    now = datetime.utcnow()
    effective = now + timedelta(days=1)
    digest = 'a' * 64
    created_key = str(uuid4())

    def scoped(conn):
        conn.exec_driver_sql(f'SET LOCAL search_path TO {quoted}')
        conn.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout TO '15s'")

    def event_values(event_id, version, **changes):
        return {
            'id': event_id,
            'company_id': 1,
            'policy_id': 1,
            'policy_version': version,
            'kind': 'PUBLISHED',
            'revision_id': 1,
            'revision_number': 1,
            'content_sha256': digest,
            'publication_id': None,
            'effective_at': effective,
            'reason': 'Synthetic PostgreSQL review',
            'created_by': 1,
            'created_at': now,
            'request_key': str(uuid4()),
            'request_hash': 'b' * 64,
            **changes,
        }

    def advance(conn, version):
        return conn.execute(
            Policy.__table__.update()
            .where(Policy.id == 1, Policy.company_id == 1, Policy.version == version - 1)
            .values(version=version, latest_revision_number=1, updated_at=now)
        ).rowcount

    def refused(conn, statement, code):
        savepoint = conn.begin_nested()
        try:
            conn.execute(statement)
        except sa.exc.DBAPIError as exc:
            savepoint.rollback()
            assert exc.orig.pgcode == code, str(exc.orig)
        else:
            savepoint.rollback()
            raise AssertionError('PostgreSQL accepted a prohibited policy write')

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {quoted}')
            scoped(conn)
            for table in ('companies', 'users', 'api_tokens'):
                conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                conn.exec_driver_sql(f'INSERT INTO {table} VALUES (1),(2)')
            path = Path(__file__).resolve().parents[1] / 'alembic/versions/103_quote_nesting_spacing_policies.py'
            spec = importlib.util.spec_from_file_location('spacing_migration', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
                module.upgrade()
            conn.execute(
                Policy.__table__.insert().values(id=1, company_id=1, created_by=1, created_at=now, updated_at=now)
            )
            assert advance(conn, 1) == 1
            conn.execute(
                Revision.__table__.insert().values(
                    id=1,
                    company_id=1,
                    policy_id=1,
                    revision_number=1,
                    name='Synthetic policy',
                    content_json={'bands': []},
                    content_sha256=digest,
                    payload_schema_version=1,
                    payload_bytes=12,
                    created_by=1,
                    created_at=now,
                )
            )
            conn.execute(
                Event.__table__.insert().values(
                    **event_values(
                        1,
                        1,
                        kind='REVISION_CREATED',
                        effective_at=None,
                        request_key=created_key,
                    )
                )
            )

        barrier = Barrier(2)

        def publication(event_id):
            with engine.begin() as conn:
                scoped(conn)
                barrier.wait(timeout=10)
                if not advance(conn, 2):
                    return None
                conn.execute(Event.__table__.insert().values(**event_values(event_id, 2)))
                return event_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publication, (2, 3)))
        assert sum(result is not None for result in results) == 1, results
        published = next(result for result in results if result is not None)
        barrier = Barrier(2)

        def withdrawal(event_id):
            with engine.begin() as conn:
                scoped(conn)
                barrier.wait(timeout=10)
                if not advance(conn, 3):
                    return False
                conn.execute(
                    Event.__table__.insert().values(
                        **event_values(
                            event_id,
                            3,
                            kind='WITHDRAWN',
                            publication_id=published,
                            effective_at=None,
                        )
                    )
                )
                return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(withdrawal, (4, 5)))
        assert sorted(results) == [False, True], results

        with engine.begin() as conn:
            scoped(conn)
            assert conn.scalar(sa.select(sa.func.count()).select_from(Event.__table__)) == 3
            assert conn.scalar(sa.select(Policy.version)) == 3
            # An error must roll back its header advance too, so the next request
            # does not inherit a phantom successful publication version.
            for changes, code in (
                ({'effective_at': effective}, '23505'),
                ({'effective_at': effective + timedelta(days=1), 'request_key': created_key}, '23505'),
                ({'effective_at': effective + timedelta(days=1), 'content_sha256': 'c' * 64}, '23503'),
            ):
                savepoint = conn.begin_nested()
                assert advance(conn, 4) == 1
                try:
                    conn.execute(Event.__table__.insert().values(**event_values(6, 4, **changes)))
                except sa.exc.DBAPIError as exc:
                    savepoint.rollback()
                    assert exc.orig.pgcode == code, str(exc.orig)
                else:
                    raise AssertionError('PostgreSQL accepted conflicting policy evidence')
                assert conn.scalar(sa.select(Policy.version)) == 3
            for model in (Revision, Event):
                refused(conn, model.__table__.update().values(created_by=2), '23514')
                refused(conn, model.__table__.delete(), '23514')
            refused(conn, Policy.__table__.delete(), '23514')
            refused(conn, sa.text('TRUNCATE quote_nesting_spacing_events'), '23514')
            refused(conn, sa.text('TRUNCATE quote_nesting_spacing_revisions, quote_nesting_spacing_events'), '23514')
            refused(
                conn,
                sa.text(
                    'TRUNCATE quote_nesting_spacing_policies, quote_nesting_spacing_revisions, quote_nesting_spacing_events'
                ),
                '23514',
            )
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
                module.downgrade()
                module.upgrade()
            assert conn.scalar(sa.select(sa.func.count()).select_from(Policy.__table__)) == 0
        print(
            'PostgreSQL spacing policies: one publication winner, one withdrawal winner, exact immutable references, '
            'shared request identity, effective-date conflict and complete transaction rollback verified.'
        )
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted} CASCADE')
