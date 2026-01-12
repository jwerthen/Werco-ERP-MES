"""Add soft delete columns to critical entities

Revision ID: 006
Revises: 005
Create Date: 2024-01-12

Adds is_deleted, deleted_at, deleted_by columns to:
- parts
- work_orders
- customers
- boms
- routings

This supports data recovery and maintains audit trail for AS9100D compliance.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = '006'
down_revision = '005_add_component_tracking'
branch_labels = None
depends_on = None


def table_has_column(conn, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    conn = op.get_bind()
    
    # Tables to add soft delete columns to
    tables = ['parts', 'work_orders', 'customers', 'boms', 'routings']
    
    for table in tables:
        # Add is_deleted column if not exists
        if not table_has_column(conn, table, 'is_deleted'):
            op.add_column(table, sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'))
            op.create_index(f'ix_{table}_is_deleted', table, ['is_deleted'])
        
        # Add deleted_at column if not exists
        if not table_has_column(conn, table, 'deleted_at'):
            op.add_column(table, sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
        
        # Add deleted_by column if not exists
        if not table_has_column(conn, table, 'deleted_by'):
            op.add_column(table, sa.Column('deleted_by', sa.Integer(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    
    tables = ['parts', 'work_orders', 'customers', 'boms', 'routings']
    
    for table in tables:
        # Drop columns in reverse order
        if table_has_column(conn, table, 'deleted_by'):
            op.drop_column(table, 'deleted_by')
        
        if table_has_column(conn, table, 'deleted_at'):
            op.drop_column(table, 'deleted_at')
        
        if table_has_column(conn, table, 'is_deleted'):
            op.drop_index(f'ix_{table}_is_deleted', table)
            op.drop_column(table, 'is_deleted')
