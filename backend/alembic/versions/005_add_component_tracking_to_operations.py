"""Add component tracking columns to work_order_operations

Revision ID: 005_add_component_tracking
Revises: 004_add_optimistic_locking
Create Date: 2026-01-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '005_add_component_tracking'
down_revision = '004_add_optimistic_locking'
branch_labels = None
depends_on = None


def column_exists(connection, table_name, column_name):
    """Check if a column exists."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def upgrade():
    conn = op.get_bind()
    
    # Add component tracking columns to work_order_operations
    if not column_exists(conn, 'work_order_operations', 'component_part_id'):
        op.add_column('work_order_operations', 
            sa.Column('component_part_id', sa.Integer(), nullable=True))
        # Add FK separately to avoid issues
        op.create_foreign_key(
            'fk_woo_component_part',
            'work_order_operations', 'parts',
            ['component_part_id'], ['id'],
            ondelete='SET NULL'
        )
        print("Added component_part_id column")
    else:
        print("Skipping component_part_id: already exists")
    
    if not column_exists(conn, 'work_order_operations', 'component_quantity'):
        op.add_column('work_order_operations', 
            sa.Column('component_quantity', sa.Float(), nullable=True))
        print("Added component_quantity column")
    else:
        print("Skipping component_quantity: already exists")
    
    if not column_exists(conn, 'work_order_operations', 'operation_group'):
        op.add_column('work_order_operations', 
            sa.Column('operation_group', sa.String(50), nullable=True))
        print("Added operation_group column")
    else:
        print("Skipping operation_group: already exists")


def downgrade():
    conn = op.get_bind()
    
    if column_exists(conn, 'work_order_operations', 'operation_group'):
        op.drop_column('work_order_operations', 'operation_group')
    if column_exists(conn, 'work_order_operations', 'component_quantity'):
        op.drop_column('work_order_operations', 'component_quantity')
    if column_exists(conn, 'work_order_operations', 'component_part_id'):
        op.drop_constraint('fk_woo_component_part', 'work_order_operations', type_='foreignkey')
        op.drop_column('work_order_operations', 'component_part_id')
