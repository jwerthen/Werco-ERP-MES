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
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    
    # Update any NULL line_type values to 'component' (the default)
    conn.execute(text("UPDATE bom_items SET line_type = 'component' WHERE line_type IS NULL"))


def downgrade():
    # No need to revert - we're just fixing data
    pass
