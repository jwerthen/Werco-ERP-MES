"""Add MFA fields to users table (placeholder for rollback)

Revision ID: 009_add_mfa_fields
Revises: 008_add_audit_log_integrity
Create Date: 2026-01-13

This migration was reverted. This file exists only to satisfy alembic's
revision chain. The downgrade will clean up any MFA columns that may exist.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '009_add_mfa_fields'
down_revision = '008_add_audit_log_integrity'
branch_labels = None
depends_on = None


def column_exists(connection, table_name, column_name):
    """Check if a column exists in a table."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def upgrade() -> None:
    # MFA feature was reverted - this is a no-op placeholder
    # The columns may or may not exist depending on deployment timing
    pass


def downgrade() -> None:
    # Clean up MFA columns if they exist
    connection = op.get_bind()
    
    if column_exists(connection, 'users', 'mfa_setup_at'):
        op.drop_column('users', 'mfa_setup_at')
    if column_exists(connection, 'users', 'mfa_backup_codes'):
        op.drop_column('users', 'mfa_backup_codes')
    if column_exists(connection, 'users', 'mfa_secret'):
        op.drop_column('users', 'mfa_secret')
    if column_exists(connection, 'users', 'mfa_enabled'):
        op.drop_column('users', 'mfa_enabled')
