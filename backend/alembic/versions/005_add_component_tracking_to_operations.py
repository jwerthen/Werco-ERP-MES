"""Add component tracking columns to work_order_operations

Revision ID: 005
Revises: 004
Create Date: 2026-01-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    # Add component tracking columns to work_order_operations
    op.add_column('work_order_operations', 
        sa.Column('component_part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=True))
    op.add_column('work_order_operations', 
        sa.Column('component_quantity', sa.Float(), default=0.0, nullable=True))
    op.add_column('work_order_operations', 
        sa.Column('operation_group', sa.String(50), nullable=True))


def downgrade():
    op.drop_column('work_order_operations', 'operation_group')
    op.drop_column('work_order_operations', 'component_quantity')
    op.drop_column('work_order_operations', 'component_part_id')
