"""Widen alembic version column

Revision ID: 014b_widen_alembic_version
Revises: 014_add_hardware_consumable
Create Date: 2026-01-22

Ensures alembic_version.version_num can store longer revision ids.
"""
from alembic import op
import sqlalchemy as sa

revision = '014b_widen_alembic_version'
down_revision = '014_add_hardware_consumable'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alembic_version') as batch_op:
        batch_op.alter_column('version_num', type_=sa.String(128))


def downgrade() -> None:
    with op.batch_alter_table('alembic_version') as batch_op:
        batch_op.alter_column('version_num', type_=sa.String(32))
