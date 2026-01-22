"""Normalize parttype enum values to lowercase

Revision ID: 018_normalize_parttype_enum
Revises: 017_add_uppercase_parttype_values
Create Date: 2026-01-22
"""
from alembic import op
from sqlalchemy import text

revision = "018_normalize_parttype_enum"
down_revision = "017_add_uppercase_parttype_values"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Create a clean enum with lowercase values
    conn.execute(text(
        "CREATE TYPE parttype_new AS ENUM ("
        "'manufactured', 'purchased', 'assembly', 'raw_material', 'hardware', 'consumable'"
        ")"
    ))
    # Normalize existing values to lowercase and cast
    conn.execute(text(
        "ALTER TABLE parts "
        "ALTER COLUMN part_type TYPE parttype_new "
        "USING lower(part_type::text)::parttype_new"
    ))
    # Replace old enum type
    conn.execute(text("DROP TYPE parttype"))
    conn.execute(text("ALTER TYPE parttype_new RENAME TO parttype"))


def downgrade():
    # Best-effort downgrade back to original enum without uppercase values
    conn = op.get_bind()
    conn.execute(text(
        "CREATE TYPE parttype_old AS ENUM ("
        "'manufactured', 'purchased', 'assembly', 'raw_material'"
        ")"
    ))
    conn.execute(text(
        "ALTER TABLE parts "
        "ALTER COLUMN part_type TYPE parttype_old "
        "USING lower(part_type::text)::parttype_old"
    ))
    conn.execute(text("DROP TYPE parttype"))
    conn.execute(text("ALTER TYPE parttype_old RENAME TO parttype"))
