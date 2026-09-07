"""Local schema roundtrip and generated PostgreSQL access-control SQL."""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).parents[1] / 'alembic/versions/094_document_deliveries.py'
    spec = importlib.util.spec_from_file_location('document_delivery_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_document_delivery_migration_roundtrip(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            assert sa.inspect(connection).has_table('document_deliveries')
            migration().downgrade()
            assert not sa.inspect(connection).has_table('document_deliveries')
            migration().upgrade()
        assert {
            constraint['name'] for constraint in sa.inspect(connection).get_check_constraints('document_deliveries')
        } >= {'ck_delivery_status', 'ck_delivery_attachment_size', 'ck_delivery_body_size'}
    engine.dispose()


def test_delivery_postgres_sql_has_bounded_snapshots_rls_and_privilege_revocation():
    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': sql})
    ):
        migration().upgrade()
        migration().downgrade()
    output = sql.getvalue()
    assert 'ENABLE ROW LEVEL SECURITY' in output
    assert 'FROM anon' in output and 'FROM authenticated' in output and 'FROM PUBLIC' in output
    assert 'UNIQUE (company_id, request_key)' in output
    assert 'length(attachment) <= 5242880' in output
    assert migration().down_revision == '093_mrp_supply_links'
