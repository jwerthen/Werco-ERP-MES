"""Run migrations 106/107 and evidence controls in a disposable PostgreSQL schema.

Requires ENVIRONMENT=test and an explicit local database URL; all fixtures are
synthetic and rolled back. The migration-only pass never reads ERP settings or the
repository .env. The second, create_all-first pass must import the ERP models (their
after_create listeners are what it tests), which loads ERP settings and builds, but
never connects, the app engine; every statement still runs on --database-url.
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic.migration import MigrationContext
from alembic.operations import Operations

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # CI runs this file as a script, not with -m
    sys.path.insert(0, str(BACKEND_DIR))

from scripts._pg_verify import (  # noqa: E402  (app-free; needs BACKEND_DIR on sys.path)
    create_missing_data_api_roles,
    grant_inherited_data_api_privileges,
    require_local_test_postgres,
    trigger_names,
)


def migration(name, connection):
    path = BACKEND_DIR / 'alembic' / 'versions' / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


IMMUTABLE = (
    'fabrication_quote_revisions',
    'fabrication_quote_files',
    'fabrication_quote_actuals',
    'fabrication_quote_profiles',
)
FABRICATION_TABLES = ('fabrication_quotes', *IMMUTABLE)
# Stand-ins for the existing tables 106/107 reference; ids 1 and 2 are the two tenants.
REFERENCE_TABLES = ('companies', 'users', 'customers', 'quotes')


def create_reference_tables(connection):
    for table in REFERENCE_TABLES:
        connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql(f'INSERT INTO {table} VALUES (1), (2)')


def assert_immutability_triggers(connection, schema, built_by):
    """Exactly the update/delete/truncate three per evidence table.

    A fourth means a DROP TRIGGER IF EXISTS missed a differently named copy.
    """
    for table in IMMUTABLE:
        count = connection.scalar(
            sa.text('SELECT count(*) FROM pg_trigger WHERE tgrelid=to_regclass(:table) AND NOT tgisinternal'),
            {'table': f'{schema}.{table}'},
        )
        assert count == 3, f'{built_by}: {table} carries {count} triggers, not its update/delete/truncate three'


def must_refuse(connection, statement, expected_code):
    try:
        with connection.begin_nested():
            connection.execute(sa.text(statement))
    except DBAPIError as error:
        code = getattr(error.orig, 'pgcode', None) or getattr(error.orig, 'sqlstate', None)
        assert code == expected_code, f'Expected {expected_code}; received {code}'
    else:
        raise AssertionError('Database accepted a prohibited mutation')


def insert_evidence(connection):
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


def assert_evidence_boundaries(connection, schema):
    # DROP TRIGGER IF EXISTS + CREATE must leave exactly one trigger per action after two passes.
    assert_immutability_triggers(connection, schema, 'after 106/107')
    for table in FABRICATION_TABLES:
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
        if table in IMMUTABLE:
            must_refuse(connection, f'UPDATE {table} SET created_by=created_by WHERE id=1', '23514')
            must_refuse(connection, f'DELETE FROM {table} WHERE id=1', '23514')
            must_refuse(connection, f'TRUNCATE {table} CASCADE', '23514')


def verify_model_built(connection, quoting_migration, profiles_migration, migration_triggers):
    """Run 106/107 over tables the MODELS built, as the fresh bootstrap does.

    create_all's after_create listeners have already enabled RLS, revoked and built the
    immutability triggers before either migration runs; a model trigger named differently
    from the migration's would survive its DROP IF EXISTS as a duplicate, which the
    migration-only pass cannot see.
    """
    import app.models  # noqa: F401  (registers every table, so model FKs resolve)
    from app.db.database import Base

    schema = 'fabrication_model_check_' + uuid4().hex
    # Identifier contains only our fixed prefix and generated hexadecimal UUID.
    quoted = connection.dialect.identifier_preparer.quote(schema)
    connection.exec_driver_sql(f'CREATE SCHEMA {quoted}')
    connection.exec_driver_sql(f'SET LOCAL search_path TO {quoted}')
    grant_inherited_data_api_privileges(connection, schema)
    create_reference_tables(connection)
    Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in FABRICATION_TABLES])
    assert_immutability_triggers(connection, schema, 'create_all')
    quoting_migration.upgrade()
    profiles_migration.upgrade()
    insert_evidence(connection)
    assert_evidence_boundaries(connection, schema)
    assert (
        trigger_names(connection, schema, FABRICATION_TABLES) == migration_triggers
    ), '106/107 over model-built tables did not end with exactly the migration-only build\'s triggers'


def verify(database_url):
    url = require_local_test_postgres(database_url)
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
            # Temporary roles roll back with the transaction; an existing one is never altered.
            create_missing_data_api_roles(connection)
            create_reference_tables(connection)
            old_quote_count = connection.scalar(sa.text('SELECT count(*) FROM quotes'))
            quoting = migration('106_fabrication_quoting.py', connection)
            profiles = migration('107_fabrication_quote_profiles.py', connection)
            quoting.upgrade()
            profiles.upgrade()
            # Second pass over existing objects: the fresh bootstrap's case, which only the guards make a no-op.
            quoting.upgrade()
            profiles.upgrade()
            insert_evidence(connection)
            assert_evidence_boundaries(connection, schema)
            migration_triggers = trigger_names(connection, schema, FABRICATION_TABLES)
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
            quoting.downgrade()
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
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema AND p.proname LIKE 'fabrication_quote%_immutable'"
                    ),
                    {'schema': schema},
                )
                == 0
            ), 'downgrade left a fabrication_quote*_immutable() trigger function behind'
            verify_model_built(connection, quoting, profiles, migration_triggers)
        finally:
            transaction.rollback()
            engine.dispose()
    print(
        'PASS: migrations 106/107, immutable evidence, tenant/revision constraints, direct-role denial, downgrade and historical quote preservation; '
        '106/107 over create_all-built tables kept exactly 3 immutability triggers per evidence table, RLS, direct-role denial and immutability'
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', required=True)
    verify(parser.parse_args().database_url)
