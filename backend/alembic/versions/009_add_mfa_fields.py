"""Add MFA fields to users table

Revision ID: 009_add_mfa_fields
Revises: 008_add_audit_log_integrity
Create Date: 2026-01-13

CMMC Level 2 Control: AC-3.1.1 - Multi-Factor Authentication
- Adds mfa_enabled flag
- Adds mfa_secret for TOTP secret storage
- Adds mfa_backup_codes for one-time recovery codes
- Adds mfa_setup_at timestamp
- Adds version column for optimistic locking
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSON

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
    connection = op.get_bind()
    
    # Add mfa_enabled column (nullable to allow gradual migration)
    if not column_exists(connection, 'users', 'mfa_enabled'):
        op.add_column('users', sa.Column('mfa_enabled', sa.Boolean(), nullable=True, server_default='false'))
        print("Added mfa_enabled column")
    
    # Add mfa_secret column (encrypted TOTP secret)
    if not column_exists(connection, 'users', 'mfa_secret'):
        op.add_column('users', sa.Column('mfa_secret', sa.String(32), nullable=True))
        print("Added mfa_secret column")
    
    # Add mfa_backup_codes column (JSON array of hashed codes)
    if not column_exists(connection, 'users', 'mfa_backup_codes'):
        op.add_column('users', sa.Column('mfa_backup_codes', JSON, nullable=True))
        print("Added mfa_backup_codes column")
    
    # Add mfa_setup_at timestamp
    if not column_exists(connection, 'users', 'mfa_setup_at'):
        op.add_column('users', sa.Column('mfa_setup_at', sa.DateTime(), nullable=True))
        print("Added mfa_setup_at column")
    
    # Add version column for optimistic locking (if not exists from previous migration)
    if not column_exists(connection, 'users', 'version'):
        op.add_column('users', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
        print("Added version column")
    
    print("MFA fields migration completed successfully!")


def downgrade() -> None:
    # Remove columns in reverse order
    op.drop_column('users', 'version')
    op.drop_column('users', 'mfa_setup_at')
    op.drop_column('users', 'mfa_backup_codes')
    op.drop_column('users', 'mfa_secret')
    op.drop_column('users', 'mfa_enabled')
    print("MFA fields removed")
