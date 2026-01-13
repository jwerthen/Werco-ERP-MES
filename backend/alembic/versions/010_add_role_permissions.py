"""Add role permissions table

Revision ID: 010_add_role_permissions
Revises: 009_add_mfa_fields
Create Date: 2026-01-13

Adds a table to store customized role permissions,
allowing admins to modify the default permission matrix.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '010_add_role_permissions'
down_revision = '009_add_mfa_fields'
branch_labels = None
depends_on = None


def table_exists(connection, table_name):
    """Check if a table exists."""
    result = connection.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = :table AND table_schema = 'public'"
    ), {"table": table_name})
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()
    
    if not table_exists(conn, 'role_permissions'):
        # Create role enum type if it doesn't exist
        op.execute("""
            DO $$ BEGIN
                CREATE TYPE userrole AS ENUM ('admin', 'manager', 'supervisor', 'operator', 'quality', 'shipping', 'viewer');
            EXCEPTION
                WHEN duplicate_object THEN null;
            END $$;
        """)
        
        op.create_table(
            'role_permissions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('role', sa.Enum('admin', 'manager', 'supervisor', 'operator', 'quality', 'shipping', 'viewer', name='userrole'), nullable=False),
            sa.Column('permissions', sa.JSON(), nullable=False, server_default='[]'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_by', sa.Integer(), nullable=True),
        )
        
        op.create_index('ix_role_permissions_role', 'role_permissions', ['role'], unique=True)
        print("Created role_permissions table")
    else:
        print("role_permissions table already exists")


def downgrade() -> None:
    op.drop_index('ix_role_permissions_role', table_name='role_permissions')
    op.drop_table('role_permissions')
