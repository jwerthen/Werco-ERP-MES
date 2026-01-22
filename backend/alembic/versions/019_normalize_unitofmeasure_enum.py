"""Normalize unitofmeasure enum values to lowercase

Revision ID: 019_normalize_unitofmeasure_enum
Revises: 018_normalize_parttype_enum
Create Date: 2026-01-22
"""
from alembic import op
from sqlalchemy import text

revision = "019_normalize_unitofmeasure_enum"
down_revision = "018_normalize_parttype_enum"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.execute(text(
        "CREATE TYPE unitofmeasure_new AS ENUM ("
        "'each', 'feet', 'inches', 'pounds', 'kilograms', 'sheets', 'gallons', 'liters'"
        ")"
    ))
    conn.execute(text(
        "ALTER TABLE parts "
        "ALTER COLUMN unit_of_measure TYPE unitofmeasure_new "
        "USING lower(unit_of_measure::text)::unitofmeasure_new"
    ))
    conn.execute(text("DROP TYPE unitofmeasure"))
    conn.execute(text("ALTER TYPE unitofmeasure_new RENAME TO unitofmeasure"))


def downgrade():
    conn = op.get_bind()
    conn.execute(text(
        "CREATE TYPE unitofmeasure_old AS ENUM ("
        "'each', 'feet', 'inches', 'pounds', 'kilograms', 'sheets', 'gallons', 'liters'"
        ")"
    ))
    conn.execute(text(
        "ALTER TABLE parts "
        "ALTER COLUMN unit_of_measure TYPE unitofmeasure_old "
        "USING lower(unit_of_measure::text)::unitofmeasure_old"
    ))
    conn.execute(text("DROP TYPE unitofmeasure"))
    conn.execute(text("ALTER TYPE unitofmeasure_old RENAME TO unitofmeasure"))
