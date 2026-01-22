"""Add uppercase hardware/consumable enum values for compatibility

Revision ID: 017_add_uppercase_parttype_values
Revises: 016_add_performance_indexes
Create Date: 2026-01-22
"""
from alembic import op
from sqlalchemy import text

revision = "017_add_uppercase_parttype_values"
down_revision = "016_add_performance_indexes"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Add lowercase values (in case prior migration didn't run)
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'hardware'"))
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'consumable'"))
    # Add uppercase values for legacy clients that send uppercase enum names
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'HARDWARE'"))
    conn.execute(text("ALTER TYPE parttype ADD VALUE IF NOT EXISTS 'CONSUMABLE'"))


def downgrade():
    # PostgreSQL doesn't support removing enum values easily.
    pass
