"""Add vendor_id to documents

Revision ID: 020_add_vendor_id_to_documents
Revises: 019_normalize_unitofmeasure_enum
Create Date: 2026-01-29
"""

from alembic import op
import sqlalchemy as sa

revision = '020_add_vendor_id_to_documents'
down_revision = '019_normalize_unitofmeasure_enum'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('vendor_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_documents_vendor_id_vendors',
        'documents',
        'vendors',
        ['vendor_id'],
        ['id']
    )
    op.create_index('ix_documents_vendor_id', 'documents', ['vendor_id'])


def downgrade() -> None:
    op.drop_index('ix_documents_vendor_id', table_name='documents')
    op.drop_constraint('fk_documents_vendor_id_vendors', 'documents', type_='foreignkey')
    op.drop_column('documents', 'vendor_id')
