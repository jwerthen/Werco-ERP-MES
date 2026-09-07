"""Durable reviewed import rows and atomic business-record receipts.

Revision ID: 099_recoverable_import_batches
Revises: 098_runtime_metrics
"""

import sqlalchemy as sa

from alembic import op

revision = '099_recoverable_import_batches'
down_revision = '098_runtime_metrics'
branch_labels = None
depends_on = None


def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _create_table_once(table, *columns):
    if not _exists(table):
        op.create_table(table, *columns)


def _create_index_once(name, table, columns):
    if op.get_context().as_sql or name not in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def _drop_table_if_exists(table):
    if op.get_context().as_sql or _exists(table):
        op.drop_table(table)


def upgrade():
    _create_table_once(
        'import_batches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('entity', sa.String(30), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('request_key', sa.String(100), nullable=False),
        sa.Column('headers', sa.JSON(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('company_id', 'request_key', name='uq_import_batch_request'),
        sa.UniqueConstraint('company_id', 'entity', 'source_hash', name='uq_import_batch_source'),
    )
    _create_index_once('ix_import_batches_company_id', 'import_batches', ['company_id'])
    _create_index_once('ix_import_batch_company_created', 'import_batches', ['company_id', 'created_at'])
    _create_table_once(
        'import_batch_rows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('import_batches.id'), nullable=False),
        sa.Column('row_key', sa.String(36), nullable=False),
        sa.Column('group_key', sa.String(36), nullable=False),
        sa.Column('source_row', sa.Integer(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('error', sa.Text()),
        sa.Column('result', sa.JSON()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('batch_id', 'row_key', name='uq_import_batch_row_key'),
        sa.UniqueConstraint('batch_id', 'source_row', name='uq_import_batch_source_row'),
    )
    _create_index_once('ix_import_batch_rows_company_id', 'import_batch_rows', ['company_id'])
    _create_index_once('ix_import_row_company_batch', 'import_batch_rows', ['company_id', 'batch_id', 'source_row'])
    if op.get_bind().dialect.name == 'postgresql':
        for table in ('import_batches', 'import_batch_rows'):
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
            op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')
            op.execute(f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC')
            for role in ('anon', 'authenticated'):
                op.execute(f"""DO $$ BEGIN
                  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON TABLE {table} FROM {role};
                    REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role};
                  END IF;
                END $$""")


def downgrade():
    _drop_table_if_exists('import_batch_rows')
    _drop_table_if_exists('import_batches')
