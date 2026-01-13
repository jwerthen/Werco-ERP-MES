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
        # Use raw SQL to create table with existing userrole enum
        op.execute("""
            CREATE TABLE role_permissions (
                id SERIAL PRIMARY KEY,
                role userrole NOT NULL,
                permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER
            )
        """)
        op.execute("CREATE UNIQUE INDEX ix_role_permissions_role ON role_permissions (role)")
        print("Created role_permissions table")
    else:
        print("role_permissions table already exists")


def downgrade() -> None:
    op.drop_index('ix_role_permissions_role', table_name='role_permissions')
    op.drop_table('role_permissions')
