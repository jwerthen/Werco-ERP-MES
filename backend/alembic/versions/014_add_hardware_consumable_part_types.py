"""Add hardware and consumable part types

Revision ID: 014_add_hardware_consumable
Revises: 013_change_bom_enums
Create Date: 2026-01-20

Adds new part types for hardware (bolts, nuts, fasteners) and consumables
(adhesives, lubricants) so BOMs can reference hardware inventory directly.
"""
from alembic import op
from sqlalchemy import text

revision = '014_add_hardware_consumable'
down_revision = '013_change_bom_enums'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    
    # Add new enum values to parttype enum
    # PostgreSQL requires ALTER TYPE to add enum values
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'hardware'"))
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'consumable'"))


def downgrade():
    # PostgreSQL doesn't support removing enum values easily
    # Would need to recreate the type and migrate data
    pass
