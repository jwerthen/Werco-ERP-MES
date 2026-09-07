import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    spec = importlib.util.spec_from_file_location(
        'receiving_migration', Path(__file__).parents[1] / 'alembic/versions/100_receiving_supplier_followup.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_round_trip_preserves_existing_receipts_and_requested_dates(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "receiving.db"}')
    with engine.begin() as connection:
        for sql in [
            'CREATE TABLE companies (id INTEGER PRIMARY KEY)',
            'CREATE TABLE users (id INTEGER PRIMARY KEY)',
            'CREATE TABLE documents (id INTEGER PRIMARY KEY)',
            'CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, company_id INTEGER, status TEXT, required_date DATE)',
            'CREATE TABLE po_receipts (id INTEGER PRIMARY KEY, company_id INTEGER, quantity_received FLOAT, lot_number TEXT)',
            'INSERT INTO companies VALUES (1)',
            'INSERT INTO users VALUES (1)',
            "INSERT INTO purchase_orders VALUES (1,1,'sent','2026-09-10')",
            "INSERT INTO po_receipts VALUES (1,1,12,'PRIOR-LOT')",
        ]:
            connection.execute(sa.text(sql))
        module = migration()
        module.op = Operations(MigrationContext.configure(connection))
        assert module.down_revision == '099_recoverable_import_batches'
        module.upgrade()
        module.upgrade()
        columns = {column['name'] for column in sa.inspect(connection).get_columns('purchase_orders')}
        assert {'supplier_confirmed_date', 'follow_up_owner_id', 'follow_up_due_date'}.issubset(columns)
        assert connection.execute(sa.text('SELECT supplier_confirmed_date FROM purchase_orders')).scalar() is None
        module.downgrade()
        module.downgrade()
        assert connection.execute(sa.text('SELECT quantity_received, lot_number FROM po_receipts')).one() == (
            12,
            'PRIOR-LOT',
        )
        assert connection.execute(sa.text('SELECT required_date FROM purchase_orders')).scalar() == '2026-09-10'
        module.upgrade()
        assert connection.execute(sa.text('SELECT count(*) FROM receiving_delivery_batches')).scalar() == 0
    engine.dispose()


def test_postgres_ddl_revokes_table_and_sequence_for_client_roles():
    output = io.StringIO()
    module = migration()
    module.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    module.upgrade()
    sql = output.getvalue()
    assert 'UNIQUE (company_id, request_key)' in sql and 'ENABLE ROW LEVEL SECURITY' in sql
    for role in ['PUBLIC', 'anon', 'authenticated']:
        assert f'REVOKE ALL ON receiving_delivery_batches FROM {role}' in sql
        assert f'REVOKE ALL ON SEQUENCE receiving_delivery_batches_id_seq FROM {role}' in sql
    assert 'auth.uid()' not in sql and 'CREATE POLICY' not in sql
    assert 'FOREIGN KEY(certificate_document_id) REFERENCES documents (id)' in sql
