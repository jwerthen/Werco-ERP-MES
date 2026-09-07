"""Associate reviewed MRP recommendations with supply drafts and retry identity.

Revision ID: 093_mrp_supply_links
Revises: 092_operational_inbox_state
"""

import sqlalchemy as sa

from alembic import op

revision = '093_mrp_supply_links'
down_revision = '092_operational_inbox_state'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mrp_supply_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('action_id', sa.Integer(), sa.ForeignKey('mrp_actions.id'), nullable=False),
        sa.Column('request_key', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('purchase_order_id', sa.Integer(), sa.ForeignKey('purchase_orders.id')),
        sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id')),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('company_id', 'action_id', name='uq_mrp_supply_action'),
        sa.UniqueConstraint('company_id', 'request_key', name='uq_mrp_supply_request'),
        sa.CheckConstraint(
            '(purchase_order_id IS NOT NULL AND work_order_id IS NULL) OR '
            '(purchase_order_id IS NULL AND work_order_id IS NOT NULL)',
            name='ck_mrp_supply_one_document',
        ),
    )
    op.create_index('ix_mrp_supply_links_company_id', 'mrp_supply_links', ['company_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE public.mrp_supply_links ENABLE ROW LEVEL SECURITY')
        # App users authenticate through FastAPI, not Supabase auth.uid().
        op.execute('REVOKE ALL ON TABLE public.mrp_supply_links FROM anon, authenticated')
        op.execute('REVOKE ALL ON SEQUENCE public.mrp_supply_links_id_seq FROM anon, authenticated')


def downgrade():
    op.drop_index('ix_mrp_supply_links_company_id', table_name='mrp_supply_links')
    op.drop_table('mrp_supply_links')
