"""Real concurrent SQL checks in a uniquely named, disposable PostgreSQL schema."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def assert_nesting_run_races(engine):
    """Called by the existing CI verifier; never run against a remote database."""
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or engine.url.get_backend_name() != 'postgresql'
        or engine.url.host not in {'localhost', '127.0.0.1', 'postgres'}
    ):
        raise RuntimeError('Nesting races require a local disposable PostgreSQL database')
    schema = 'nest_run_check_' + uuid4().hex
    quoted_schema = engine.dialect.identifier_preparer.quote(schema)

    def scoped(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO {quoted_schema}')
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout TO '15s'")

    insert_run = sa.text("""INSERT INTO quote_nesting_runs
      (id, company_id, draft_id, revision_id, revision_number, input_sha256, request_key, request_hash,
       created_by, created_at, updated_at, settings_json)
      VALUES (:id, :company, 1, 1, 1, :digest, :key, :request_hash, 1, now(), now(), '{}')""")

    def parameters(run_id, **changes):
        return dict(id=run_id, company=1, digest='a' * 64, key=str(uuid4()), request_hash='b' * 64, **changes)

    def refused(connection, statement, params, code):
        with connection.begin_nested() as savepoint:
            try:
                connection.execute(statement, params)
            except sa.exc.DBAPIError as exc:
                savepoint.rollback()
                assert exc.orig.pgcode == code, str(exc.orig)
            else:
                raise AssertionError('PostgreSQL accepted a prohibited nesting run write')

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA {quoted_schema}')
            scoped(connection)
            for table in ('companies', 'users', 'api_tokens'):
                connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                connection.exec_driver_sql(f'INSERT INTO {table} VALUES (1), (2)')
            for filename in ('101_quote_nesting_drafts', '102_quote_nesting_runs'):
                path = Path(__file__).resolve().parents[1] / 'alembic/versions' / (filename + '.py')
                spec = importlib.util.spec_from_file_location(filename, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()
            connection.exec_driver_sql("""INSERT INTO quote_nesting_drafts
              (id, company_id, name, created_by, created_at, updated_at)
              VALUES (1, 1, 'Synthetic concurrency draft', 1, now(), now())""")
            connection.execute(
                sa.text(
                    """INSERT INTO quote_nesting_revisions
              (id, company_id, draft_id, revision_number, draft_version, name, estimate_json, content_sha256,
               payload_schema_version, payload_bytes, created_by, created_at, request_key, request_hash, review_issues_json)
              VALUES (1, 1, 1, 1, 1, 'Synthetic concurrency inputs', '{}', :hash, 6, 2, 1, now(), :key, :hash, '[]')"""
                ),
                {'hash': 'a' * 64, 'key': str(uuid4())},
            )

        # Both connections observe the same committed starting state, then race.
        # The unique partial index, rather than a process-local lock, decides.
        barrier = Barrier(2)

        def start(run_id):
            try:
                with engine.begin() as connection:
                    scoped(connection)
                    barrier.wait(timeout=10)
                    connection.execute(insert_run, parameters(run_id))
                return ('created', run_id)
            except sa.exc.IntegrityError as exc:
                assert exc.orig.pgcode == '23505'
                return ('conflict', run_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(start, (1, 2)))
        assert sorted(status for status, _ in outcomes) == ['conflict', 'created'], outcomes
        run_id = next(run_id for status, run_id in outcomes if status == 'created')
        barrier = Barrier(2)

        def claim(token):
            with engine.begin() as connection:
                scoped(connection)
                barrier.wait(timeout=10)
                changed = connection.execute(
                    sa.text("""UPDATE quote_nesting_runs
                  SET status='RUNNING', version=2, lease_token=:lease, started_at=now(),
                      lease_expires_at=now()+interval '3 minutes'
                  WHERE company_id=1 AND id=:id AND version=1 AND status='QUEUED'"""),
                    {'id': run_id, 'lease': token},
                ).rowcount
            return (changed, token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(claim, (str(uuid4()), str(uuid4()))))
        assert sorted(count for count, _ in claimed) == [0, 1], claimed
        lease = next(token for count, token in claimed if count == 1)

        with engine.begin() as connection:
            scoped(connection)
            checkpoint = sa.text("""INSERT INTO quote_nesting_run_checkpoints
              (company_id, run_id, lease_token, sequence, group_id, stock_option_id, result_json,
               content_sha256, payload_bytes, created_at)
              VALUES (:company, :run, :lease, :seq, 'synthetic-group', :option, :result, :hash, 18, now())""")
            params = dict(
                company=1, run=run_id, lease=lease, seq=1, option='sheet-a', hash='c' * 64, result='{"complete":false}'
            )
            refused(connection, checkpoint, {**params, 'company': 2}, '23503')
            refused(connection, checkpoint, {**params, 'lease': str(uuid4())}, '23503')
            connection.execute(checkpoint, params)
            refused(connection, checkpoint, {**params, 'option': 'sheet-b'}, '23505')
            refused(connection, checkpoint, {**params, 'seq': 2}, '23505')
            for command in (
                "UPDATE quote_nesting_run_checkpoints SET result_json='{}'",
                'DELETE FROM quote_nesting_run_checkpoints',
                'TRUNCATE quote_nesting_run_checkpoints',
                "UPDATE quote_nesting_runs SET input_sha256=repeat('d',64), version=3",
                'DELETE FROM quote_nesting_runs',
                'TRUNCATE quote_nesting_runs, quote_nesting_run_checkpoints',
            ):
                refused(connection, sa.text(command), {}, '23514')
            refused(connection, sa.text('UPDATE quote_nesting_runs SET version=2'), {}, '23514')
            connection.execute(
                sa.text("UPDATE quote_nesting_runs SET status='CANCELLED', version=3, finished_at=now()")
            )
            refused(connection, checkpoint, {**params, 'seq': 2, 'option': 'sheet-b'}, '23514')
            refused(connection, sa.text("UPDATE quote_nesting_runs SET status='RUNNING', version=4"), {}, '23514')
            assert connection.execute(sa.text('SELECT count(*) FROM quote_nesting_run_checkpoints')).scalar_one() == 1
            # A terminal result releases only the active-company claim; input
            # hashes and the original checkpoint remain unchanged.
            connection.execute(insert_run, parameters(3))
            refused(connection, insert_run, {**parameters(4), 'company': 2}, '23503')
        print(
            'PostgreSQL nesting runs: one active-run winner, one lease-CAS winner, exact tenant/input FKs, '
            'immutable checkpoints/terminal rows and released active slot verified.'
        )
    finally:
        # The schema name is generated here, never read from user or application
        # input. Separate committed DDL is necessary for true two-session races.
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS {quoted_schema} CASCADE')
