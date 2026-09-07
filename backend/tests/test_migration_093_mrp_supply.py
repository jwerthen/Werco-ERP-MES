"""Supply links have durable uniqueness and are server-auth only in Supabase."""

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def revision():
    path = Path(__file__).parents[1] / 'alembic/versions/093_mrp_supply_links.py'
    spec = importlib.util.spec_from_file_location('mrp_supply_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_roundtrip_and_supply_uniqueness(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mrp.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision().upgrade()
        insert = sa.text(
            "INSERT INTO mrp_supply_links (id,company_id,action_id,request_key,request_hash,quantity,purchase_order_id,created_by,created_at) VALUES (:id,1,:action,:key,'hash',10,1,1,'2026-09-07')"
        )
        connection.execute(insert, dict(id=1, action=1, key='retry-1'))
        for params in (dict(id=2, action=1, key='retry-2'), dict(id=3, action=2, key='retry-1')):
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(insert, params)
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text('UPDATE mrp_supply_links SET work_order_id=1 WHERE id=1'))
        with Operations.context(MigrationContext.configure(connection)):
            revision().downgrade()
            revision().upgrade()
        assert sa.inspect(connection).has_table('mrp_supply_links')
    engine.dispose()


def test_postgres_migration_has_rls_and_no_direct_browser_grants():
    sql = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': sql})
    with Operations.context(context):
        revision().upgrade()
        revision().downgrade()
    output = sql.getvalue()
    assert revision().down_revision == '092_operational_inbox_state'
    assert 'ENABLE ROW LEVEL SECURITY' in output
    assert 'REVOKE ALL ON TABLE public.mrp_supply_links FROM anon, authenticated' in output
    assert 'REVOKE ALL ON SEQUENCE public.mrp_supply_links_id_seq FROM anon, authenticated' in output
    assert 'UNIQUE (company_id, action_id)' in output
    assert 'UNIQUE (company_id, request_key)' in output
