"""Add line_type and hardware fields to BOM items

Revision ID: 007_add_bom_line_type
Revises: 006
Create Date: 2026-01-12

Adds line_type enum to BOM items for categorizing components:
- component: Standard manufactured or purchased component
- hardware: COTS hardware (bolts, nuts, washers, etc.)
- consumable: Consumables (adhesives, lubricants, etc.)
- reference: Reference only - not consumed

Also adds hardware-specific fields:
- torque_spec: Torque specification for fasteners
- installation_notes: Assembly instructions
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '007_add_bom_line_type'
down_revision = '006'
branch_labels = None
depends_on = None


def column_exists(connection, table_name, column_name):
    """Check if a column exists in a table."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def enum_exists(connection, enum_name):
    """Check if an enum type exists."""
    result = connection.execute(text(
        "SELECT 1 FROM pg_type WHERE typname = :enum_name"
    ), {"enum_name": enum_name})
    return result.fetchone() is not None


def upgrade():
    conn = op.get_bind()
    
    # Create the bomlinetype enum if it doesn't exist
    if not enum_exists(conn, 'bomlinetype'):
        op.execute("CREATE TYPE bomlinetype AS ENUM ('component', 'hardware', 'consumable', 'reference')")
    
    # Add line_type column with default 'component'
    if not column_exists(conn, 'bom_items', 'line_type'):
        op.add_column('bom_items', 
            sa.Column('line_type', sa.Enum('component', 'hardware', 'consumable', 'reference', name='bomlinetype'), 
                      nullable=False, server_default='component'))
    
    # Add torque_spec column
    if not column_exists(conn, 'bom_items', 'torque_spec'):
        op.add_column('bom_items', 
            sa.Column('torque_spec', sa.String(100), nullable=True))
    
    # Add installation_notes column
    if not column_exists(conn, 'bom_items', 'installation_notes'):
        op.add_column('bom_items', 
            sa.Column('installation_notes', sa.Text(), nullable=True))


def downgrade():
    conn = op.get_bind()
    
    # Remove columns
    if column_exists(conn, 'bom_items', 'installation_notes'):
        op.drop_column('bom_items', 'installation_notes')
    
    if column_exists(conn, 'bom_items', 'torque_spec'):
        op.drop_column('bom_items', 'torque_spec')
    
    if column_exists(conn, 'bom_items', 'line_type'):
        op.drop_column('bom_items', 'line_type')
    
    # Drop enum type
    if enum_exists(conn, 'bomlinetype'):
        op.execute("DROP TYPE bomlinetype")
