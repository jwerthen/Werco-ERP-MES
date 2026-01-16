"""Fix NULL line_type values in bom_items

Revision ID: 011_fix_bom_line_type
Revises: 010
Create Date: 2026-01-16

Fixes any BOM items that may have NULL line_type values from before
the line_type column was added with a proper default.
"""
from alembic import op
from sqlalchemy import text

revision = '011_fix_bom_line_type'
down_revision = '010_add_role_permissions'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    
    # Fix any NULL values in bom_items that could cause serialization errors
    conn.execute(text("UPDATE bom_items SET line_type = 'component' WHERE line_type IS NULL"))
    conn.execute(text("UPDATE bom_items SET item_type = 'make' WHERE item_type IS NULL"))
    conn.execute(text("UPDATE bom_items SET scrap_factor = 0.0 WHERE scrap_factor IS NULL"))
    conn.execute(text("UPDATE bom_items SET operation_sequence = 10 WHERE operation_sequence IS NULL"))
    conn.execute(text("UPDATE bom_items SET quantity = 1.0 WHERE quantity IS NULL"))
    conn.execute(text("UPDATE bom_items SET lead_time_offset = 0 WHERE lead_time_offset IS NULL"))


def downgrade():
    # No need to revert - we're just fixing data
    pass
