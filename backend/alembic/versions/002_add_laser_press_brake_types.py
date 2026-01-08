"""Add laser and press_brake work center types

Revision ID: 002_add_laser_press_brake
Revises: 001_add_performance_indexes
Create Date: 2026-01-05

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002_add_laser_press_brake'
down_revision = '001_performance_indexes'
branch_labels = None
depends_on = None


def upgrade():
    # Add new enum values to workcentertype
    # PostgreSQL requires ALTER TYPE to add new values
    op.execute("ALTER TYPE workcentertype ADD VALUE IF NOT EXISTS 'laser'")
    op.execute("ALTER TYPE workcentertype ADD VALUE IF NOT EXISTS 'press_brake'")


def downgrade():
    # Note: PostgreSQL doesn't support removing enum values directly
    # This would require recreating the enum type and migrating data
    pass
