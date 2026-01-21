"""Add availability rate to work centers

Revision ID: 015_add_work_center_availability_rate
Revises: 014_add_hardware_consumable
Create Date: 2026-01-21

Adds availability_rate column to work_centers for scheduling availability metrics.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision = '015_add_work_center_availability_rate'
down_revision = '014_add_hardware_consumable'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns('work_centers')]

    if 'availability_rate' not in columns:
        op.add_column(
            'work_centers',
            sa.Column('availability_rate', sa.Float(), nullable=False, server_default='100.0')
        )
        op.alter_column('work_centers', 'availability_rate', server_default=None)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns('work_centers')]

    if 'availability_rate' in columns:
        op.drop_column('work_centers', 'availability_rate')
