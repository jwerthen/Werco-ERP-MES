"""Run migrations 106/107 and evidence controls in a disposable PostgreSQL schema.

Requires ENVIRONMENT=test and an explicit local database URL. Never reads ERP
settings or the repository .env; all fixtures are synthetic and rolled back.
"""

import argparse
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration(name, connection):
    path = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def must_refuse(connection, statement, expected_code):
    try:
        with connection.begin_nested():
            connection.execute(sa.text(statement))
    except DBAPIError as error:
        code = getattr(error.orig, 'pgcode', None) or getattr(error.orig, 'sqlstate', None)
        assert code == expected_code, f'Expected {expected_code}; received {code}'
    else:
        raise AssertionError('Database accepted a prohibited mutation')


def verify(database_url):
    url = sa.engine.make_url(database_url)
    if (
        os.environ.get('ENVIRONMENT') != 'test'
        or url.get_backend_name() != 'postgresql'
        or url.host not in {'localhost', '127.0.0.1', 'postgres'}
    ):
        raise RuntimeError('Verification requires ENVIRONMENT=test and explicit local PostgreSQL')
    engine = sa.create_engine(url)
    schema = 'fabrication_check_' + uuid4().hex
    # Identifier contains only our fixed prefix and generated hexadecimal UUID.
    quoted = engine.dialect.identifier_preparer.quote(schema)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(f'CREATE SCHEMA {quoted}')
            connection.exec_driver_sql(f'SET LOCAL search_path TO {quoted}')
            connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
            connection.exec_driver_sql("SET LOCAL statement_timeout TO '30s'")
            for role in ('anon', 'authenticated'):
                if not connection.scalar(
                    sa.text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'), {'role': role}
                ):
                    connection.exec_driver_sql(f'CREATE ROLE {role} NOLOGIN')
            for table in ('companies', 'users', 'customers', 'quotes'):
                connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                connection.exec_driver_sql(f'INSERT INTO {table} VALUES (1), (2)')
            old_quote_count = connection.scalar(sa.text('SELECT count(*) FROM quotes'))
            base = migration('106_fabrication_quoting.py', connection)
            profiles = migration('107_fabrication_quote_profiles.py', connection)
            base.upgrade()
            profiles.upgrade()
            connection.exec_driver_sql("""
                INSERT INTO fabrication_quotes
                (id,company_id,title,status,revision,plan_json,calculation_json,created_by,created_at,updated_at)
                VALUES (1,1,'Synthetic','draft',1,'{}','{}',1,now(),now())
            """)
            connection.exec_driver_sql("""
                INSERT INTO fabrication_quote_revisions
                (id,company_id,quote_id,revision,action,snapshot_json,content_sha256,note,created_by,created_at)
                VALUES (1,1,1,1,'create','{}','synthetic','',1,now())
            """)
            connection.exec_driver_sql("""
                INSERT INTO fabrication_quote_files
                (id,company_id,quote_id,file_name,sha256,byte_count,units_override,content_type,content,analysis_json,created_by,created_at)
                VALUES (1,1,1,'synthetic.csv','synthetic',1,'','text/csv','x','{}',1,now())
            """)
            connection.exec_driver_sql("""
                INSERT INTO fabrication_quote_actuals
                (id,company_id,quote_id,quote_revision,request_key,observation_json,created_by,created_at)
                VALUES (1,1,1,1,'synthetic','{}',1,now())
            """)
            connection.exec_driver_sql("""
                INSERT INTO fabrication_quote_profiles
                (id,company_id,key,revision,name,process,currency,template_json,evidence_note,content_sha256,created_by,created_at)
                VALUES (1,1,'synthetic',1,'Synthetic','manual','USD','{}','Synthetic','synthetic',1,now())
            """)
            immutable = (
                'fabrication_quote_revisions',
                'fabrication_quote_files',
                'fabrication_quote_actuals',
                'fabrication_quote_profiles',
            )
            for table in ('fabrication_quotes', *immutable):
                assert connection.scalar(
                    sa.text('SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)'),
                    {'table': f'{schema}.{table}'},
                )
                for role in ('anon', 'authenticated'):
                    for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                        assert not connection.scalar(
                            sa.text('SELECT has_table_privilege(:role,:table,:privilege)'),
                            {'role': role, 'table': f'{schema}.{table}', 'privilege': privilege},
                        )
                if table in immutable:
                    must_refuse(connection, f'UPDATE {table} SET created_by=created_by WHERE id=1', '23514')
                    must_refuse(connection, f'DELETE FROM {table} WHERE id=1', '23514')
                    must_refuse(connection, f'TRUNCATE {table} CASCADE', '23514')
            must_refuse(
                connection,
                """
                INSERT INTO fabrication_quote_files
                (id,company_id,quote_id,file_name,sha256,byte_count,units_override,content_type,content,analysis_json,created_by,created_at)
                VALUES (2,2,1,'cross-tenant.csv','different',1,'','text/csv','x','{}',1,now())
            """,
                '23503',
            )
            must_refuse(
                connection,
                """
                INSERT INTO fabrication_quote_actuals
                (id,company_id,quote_id,quote_revision,request_key,observation_json,created_by,created_at)
                VALUES (2,1,1,99,'missing-revision','{}',1,now())
            """,
                '23503',
            )
            profiles.downgrade()
            base.downgrade()
            assert connection.scalar(sa.text('SELECT count(*) FROM quotes')) == old_quote_count
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema=:schema AND table_name LIKE 'fabrication_%'"
                    ),
                    {'schema': schema},
                )
                == 0
            )
        finally:
            transaction.rollback()
            engine.dispose()
    print(
        'PASS: migrations 106/107, immutable evidence, tenant/revision constraints, direct-role denial, downgrade and historical quote preservation'
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', required=True)
    verify(parser.parse_args().database_url)
