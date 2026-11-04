import importlib.util
import io
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

# The offline Postgres DDL adds each FK as its own ALTER, so the migration's own words give
# (table, name, column, referred table, referred column) -- and its downgrade drops by name.
_ADD_FK = re.compile(r'ALTER TABLE (\w+) ADD CONSTRAINT (\w+) FOREIGN KEY\((\w+)\) REFERENCES (\w+) \((\w+)\)')
_DROP_FK = re.compile(r'ALTER TABLE (\w+) DROP CONSTRAINT (\w+)')


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


def render_postgres(step):
    output = io.StringIO()
    module = migration()
    module.op = Operations(
        MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    )
    getattr(module, step)()
    return output.getvalue()


def test_postgres_ddl_revokes_table_and_sequence_for_client_roles():
    sql = render_postgres('upgrade')
    assert 'UNIQUE (company_id, request_key)' in sql and 'ENABLE ROW LEVEL SECURITY' in sql
    for role in ['PUBLIC', 'anon', 'authenticated']:
        assert f'REVOKE ALL ON receiving_delivery_batches FROM {role}' in sql
        assert f'REVOKE ALL ON SEQUENCE receiving_delivery_batches_id_seq FROM {role}' in sql
    assert 'auth.uid()' not in sql and 'CREATE POLICY' not in sql
    assert 'FOREIGN KEY(certificate_document_id) REFERENCES documents (id)' in sql


@pytest.mark.unit
def test_model_names_each_fk_exactly_as_the_migration_creates_and_drops_it():
    """The model and migration 100 name the same four FKs the same way.

    100's upgrade guards each FK by COLUMN but its downgrade drops it BY NAME, so a
    ``create_all``-built database whose model left them unnamed carries FKs that downgrade
    can never address, and differs from every incrementally migrated one. Both sides are
    read, never restated here: the migration from its rendered Postgres DDL, the model from
    ``Base.metadata``. A rename on either side fails this test.
    """
    import app.models  # noqa: F401 -- registers every model on Base.metadata
    from app.db.database import Base

    created = {
        (table, column): (name, referred, referred_column)
        for table, name, column, referred, referred_column in _ADD_FK.findall(render_postgres('upgrade'))
    }
    # 100 is applied and frozen: two receipt FKs and two PO FKs. Fewer means the pattern
    # stopped matching and the comparison below would shrink without failing.
    assert len(created) == 4, f'expected the four ALTER-added FKs in 100, parsed {sorted(created)}'
    assert set(_DROP_FK.findall(render_postgres('downgrade'))) == {
        (table, name) for (table, _), (name, _, _) in created.items()
    }, "100's downgrade must drop exactly the FK names its upgrade creates"

    mismatches = []
    for (table, column), (name, referred, referred_column) in sorted(created.items()):
        model = [
            (fk.name, fk.referred_table.name, [element.column.name for element in fk.elements])
            for fk in Base.metadata.tables[table].foreign_key_constraints
            if fk.column_keys == [column]
        ]
        if model != [(name, referred, [referred_column])]:
            mismatches.append(f'{table}.{column}: migration 100 creates {name} -> {referred}, model declares {model}')
    assert not mismatches, 'model FKs out of lock-step with migration 100:\n  ' + '\n  '.join(mismatches)
