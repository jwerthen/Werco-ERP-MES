"""Atomic delivery receipts, linked certificates, and supplier follow-up.

Revision ID: 100_receiving_supplier_followup
Revises: 099_recoverable_import_batches
"""

import sqlalchemy as sa

from alembic import op

revision = '100_receiving_supplier_followup'
down_revision = '099_recoverable_import_batches'
branch_labels = None
depends_on = None

PO_FIELDS = [
    ('supplier_confirmed_date', sa.Date()),
    ('supplier_acknowledged_at', sa.DateTime()),
    ('supplier_acknowledged_by', sa.Integer()),
    ('supplier_confirmation_reference', sa.String(100)),
    ('supplier_confirmation_note', sa.Text()),
    ('follow_up_owner_id', sa.Integer()),
    ('follow_up_due_date', sa.Date()),
]


def inspector():
    return None if op.get_context().as_sql else sa.inspect(op.get_bind())


def table_exists(table, offline=False):
    view = inspector()
    return offline if view is None else view.has_table(table)


def names(table, kind, offline=()):
    view = inspector()
    if view is None:
        return set(offline)
    if not view.has_table(table):
        return set()
    return {item['name'] for item in getattr(view, f'get_{kind}')(table)}


def has_fk(table, column):
    view = inspector()
    return view is not None and any(column in item['constrained_columns'] for item in view.get_foreign_keys(table))


def upgrade():
    if not table_exists('receiving_delivery_batches'):
        op.create_table(
            'receiving_delivery_batches',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('purchase_order_id', sa.Integer(), sa.ForeignKey('purchase_orders.id'), nullable=False),
            sa.Column('request_key', sa.String(80), nullable=False),
            sa.Column('payload_hash', sa.String(64), nullable=False),
            sa.Column('response', sa.JSON(), nullable=False),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_receiving_delivery_request'),
        )
    for name, columns in [
        ('ix_delivery_company_po', ['company_id', 'purchase_order_id']),
        ('ix_receiving_delivery_batches_company_id', ['company_id']),
    ]:
        if name not in names('receiving_delivery_batches', 'indexes'):
            op.create_index(name, 'receiving_delivery_batches', columns)
    receipt_columns = names('po_receipts', 'columns')
    receipt_indexes = names('po_receipts', 'indexes')
    receipt_fks = {column: has_fk('po_receipts', column) for column in ['certificate_document_id', 'delivery_batch_id']}
    with op.batch_alter_table('po_receipts') as batch:
        for column, target, constraint in [
            ('certificate_document_id', 'documents', 'fk_receipt_certificate_document'),
            ('delivery_batch_id', 'receiving_delivery_batches', 'fk_receipt_delivery_batch'),
        ]:
            if column not in receipt_columns:
                batch.add_column(sa.Column(column, sa.Integer(), nullable=True))
            if not receipt_fks[column]:
                batch.create_foreign_key(constraint, target, [column], ['id'])
            index = f'ix_po_receipts_{column}'
            if index not in receipt_indexes:
                batch.create_index(index, [column])
    po_columns = names('purchase_orders', 'columns')
    po_indexes = names('purchase_orders', 'indexes')
    po_fks = {
        column: has_fk('purchase_orders', column) for column in ['supplier_acknowledged_by', 'follow_up_owner_id']
    }
    with op.batch_alter_table('purchase_orders') as batch:
        for name, type_ in PO_FIELDS:
            if name not in po_columns:
                batch.add_column(sa.Column(name, type_, nullable=True))
        for column, constraint in [
            ('supplier_acknowledged_by', 'fk_po_supplier_acknowledged_by'),
            ('follow_up_owner_id', 'fk_po_follow_up_owner'),
        ]:
            if not po_fks[column]:
                batch.create_foreign_key(constraint, 'users', [column], ['id'])
        if 'ix_po_company_follow_up' not in po_indexes:
            batch.create_index('ix_po_company_follow_up', ['company_id', 'follow_up_due_date', 'status'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE receiving_delivery_batches ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON receiving_delivery_batches FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE receiving_delivery_batches_id_seq FROM PUBLIC')
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON receiving_delivery_batches FROM anon;
            REVOKE ALL ON SEQUENCE receiving_delivery_batches_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON receiving_delivery_batches FROM authenticated;
            REVOKE ALL ON SEQUENCE receiving_delivery_batches_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    for table, columns, indexes, fks in [
        (
            'purchase_orders',
            [name for name, _ in PO_FIELDS],
            ['ix_po_company_follow_up'],
            ['fk_po_supplier_acknowledged_by', 'fk_po_follow_up_owner'],
        ),
        (
            'po_receipts',
            ['certificate_document_id', 'delivery_batch_id'],
            ['ix_po_receipts_certificate_document_id', 'ix_po_receipts_delivery_batch_id'],
            ['fk_receipt_certificate_document', 'fk_receipt_delivery_batch'],
        ),
    ]:
        present_columns = names(table, 'columns', columns)
        present_indexes = names(table, 'indexes', indexes)
        present_fks = names(table, 'foreign_keys', fks)
        with op.batch_alter_table(table) as batch:
            for name in indexes:
                if name in present_indexes:
                    batch.drop_index(name)
            for name in fks:
                if name in present_fks:
                    batch.drop_constraint(name, type_='foreignkey')
            for column in columns:
                if column in present_columns:
                    batch.drop_column(column)
    if table_exists('receiving_delivery_batches', offline=True):
        op.drop_table('receiving_delivery_batches')
