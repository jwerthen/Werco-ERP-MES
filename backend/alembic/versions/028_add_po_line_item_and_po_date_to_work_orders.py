"""Add po_line_item and po_date to work_orders

Revision ID: 028_add_po_line_item_and_po_date
Revises: 027_tenant_uniques_and_webhook_fk
Create Date: 2026-04-17

Adds two nullable columns to work_orders so each WO can store the
specific PO line item number and the date the PO was issued. Needed
for the one-shot legacy job-list import and for all future WOs.
"""
from alembic import op
import sqlalchemy as sa


revision = '028_add_po_line_item_and_po_date'
down_revision = '027_tenant_uniques_and_webhook_fk'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('work_orders', sa.Column('po_line_item', sa.String(length=50), nullable=True))
    op.add_column('work_orders', sa.Column('po_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('work_orders', 'po_date')
    op.drop_column('work_orders', 'po_line_item')
