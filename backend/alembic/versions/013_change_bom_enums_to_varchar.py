"""Change BOM item_type and line_type from enum to varchar

Revision ID: 013_change_bom_enums
Revises: 012_fix_null_part_types
Create Date: 2026-01-19

SQLAlchemy's enum handling was causing issues with PostgreSQL enums.
Converting to varchar columns solves the uppercase/lowercase mismatch.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '013_change_bom_enums'
down_revision = '012_fix_null_part_types'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    
    # Change item_type from enum to varchar
    # First cast existing data to text, then alter column type
    conn.execute(text("""
        ALTER TABLE bom_items 
        ALTER COLUMN item_type TYPE VARCHAR(20) 
        USING item_type::text
    """))
    
    # Change line_type from enum to varchar
    conn.execute(text("""
        ALTER TABLE bom_items 
        ALTER COLUMN line_type TYPE VARCHAR(20) 
        USING line_type::text
    """))
    
    # Optionally drop the old enum types (they may still be used elsewhere)
    # conn.execute(text("DROP TYPE IF EXISTS bomitemtype"))
    # conn.execute(text("DROP TYPE IF EXISTS bomlinetype"))


def downgrade():
    # Convert back to enum types if needed
    conn = op.get_bind()
    
    # This is a simplified downgrade - may need adjustment
    conn.execute(text("""
        ALTER TABLE bom_items 
        ALTER COLUMN item_type TYPE bomitemtype 
        USING item_type::bomitemtype
    """))
    
    conn.execute(text("""
        ALTER TABLE bom_items 
        ALTER COLUMN line_type TYPE bomlinetype 
        USING line_type::bomlinetype
    """))
