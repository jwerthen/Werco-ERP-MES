"""Fix NULL part_type values in parts table

Revision ID: 012_fix_null_part_types
Revises: 011_fix_bom_line_type
Create Date: 2026-01-17

Fixes any parts that may have NULL part_type values which cause
serialization errors in the API.
"""
from alembic import op
from sqlalchemy import text

revision = '012_fix_null_part_types'
down_revision = '011_fix_bom_line_type'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    
    # Fix any NULL part_type values - default to 'manufactured'
    conn.execute(text("UPDATE parts SET part_type = 'manufactured' WHERE part_type IS NULL"))
    
    # Fix any NULL unit_of_measure values - default to 'each'
    conn.execute(text("UPDATE parts SET unit_of_measure = 'each' WHERE unit_of_measure IS NULL"))
    
    # Fix any NULL status values
    conn.execute(text("UPDATE parts SET status = 'active' WHERE status IS NULL"))
    
    # Fix any NULL is_active values
    conn.execute(text("UPDATE parts SET is_active = true WHERE is_active IS NULL"))
    
    # Fix any NULL revision values
    conn.execute(text("UPDATE parts SET revision = 'A' WHERE revision IS NULL"))


def downgrade():
    # No need to revert - we're just fixing data
    pass
