"""Add soft delete columns to QMS tables

Revision ID: 037_add_qms_soft_delete_columns
Revises: 036_add_laser_nest_work_orders
Create Date: 2026-06-05

Adds is_deleted, deleted_at, deleted_by columns to:
- qms_standards
- qms_clauses
- qms_clause_evidence

These three models are gaining SoftDeleteMixin (app/db/mixins.py), so the
schema must carry the soft-delete shape:
- is_deleted: Boolean NOT NULL, server_default 'false', indexed
- deleted_at: DateTime(timezone=True), nullable
- deleted_by: Integer, nullable

Soft delete supports data recovery and preserves the audit trail for
AS9100D/CMMC compliance (no hard deletes on QMS standards/clauses/evidence).

Mirrors 006_add_soft_delete_columns.py: idempotent column guards on both
upgrade and downgrade so the migration is safe to re-run.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = '037_add_qms_soft_delete_columns'
down_revision = '036_add_laser_nest_work_orders'
branch_labels = None
depends_on = None


def table_has_column(conn, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    conn = op.get_bind()

    # QMS tables to add soft delete columns to
    tables = ['qms_standards', 'qms_clauses', 'qms_clause_evidence']

    for table in tables:
        # Add is_deleted column if not exists
        if not table_has_column(conn, table, 'is_deleted'):
            op.add_column(table, sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'))
            op.create_index(f'ix_{table}_is_deleted', table, ['is_deleted'])

        # Add deleted_at column if not exists
        if not table_has_column(conn, table, 'deleted_at'):
            op.add_column(table, sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))

        # Add deleted_by column if not exists
        if not table_has_column(conn, table, 'deleted_by'):
            op.add_column(table, sa.Column('deleted_by', sa.Integer(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()

    tables = ['qms_standards', 'qms_clauses', 'qms_clause_evidence']

    for table in tables:
        # Drop columns in reverse order
        if table_has_column(conn, table, 'deleted_by'):
            op.drop_column(table, 'deleted_by')

        if table_has_column(conn, table, 'deleted_at'):
            op.drop_column(table, 'deleted_at')

        if table_has_column(conn, table, 'is_deleted'):
            op.drop_index(f'ix_{table}_is_deleted', table)
            op.drop_column(table, 'is_deleted')
