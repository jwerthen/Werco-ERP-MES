"""Add version columns for optimistic locking

Revision ID: 004_add_optimistic_locking
Revises: 003_add_database_constraints
Create Date: 2026-01-07

This migration adds version and updated_at columns to all tables
that support concurrent editing, enabling optimistic locking.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '004_add_optimistic_locking'
down_revision = '003_add_database_constraints'
branch_labels = None
depends_on = None

# Tables that need optimistic locking
TABLES_TO_VERSION = [
    'parts', 'work_orders', 'work_order_operations', 'purchase_orders',
    'purchase_order_lines', 'quotes', 'quote_lines', 'work_centers',
    'vendors', 'customers', 'boms', 'bom_items', 'routings', 'routing_operations',
    'inventory_items', 'inventory_locations', 'time_entries', 'ncrs', 'cars',
    'fais', 'fai_characteristics', 'po_receipts', 'users', 'documents',
    'mrp_runs', 'mrp_actions', 'mrp_requirements', 'shipments', 'cycle_counts',
    'cycle_count_items', 'equipment', 'calibration_records', 'supplier_part_mappings',
    'quote_materials', 'quote_machines', 'quote_finishes', 'quote_settings',
]


def column_exists(connection, table_name, column_name):
    """Check if a column exists in a table."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def index_exists(connection, index_name):
    """Check if an index exists."""
    result = connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE indexname = :name"
    ), {"name": index_name})
    return result.fetchone() is not None


def table_exists(connection, table_name):
    """Check if a table exists."""
    result = connection.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = :table AND table_schema = 'public'"
    ), {"table": table_name})
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()
    
    # Add version column to all tables that don't have it
    for table in TABLES_TO_VERSION:
        if not table_exists(conn, table):
            print(f"Skipping {table}: table does not exist")
            continue
        if not column_exists(conn, table, 'version'):
            op.add_column(
                table,
                sa.Column('version', sa.Integer(), nullable=False, server_default='1')
            )
            print(f"Added version column to {table}")
        else:
            print(f"Skipping {table}.version: already exists")
    
    # Add updated_at to tables that don't have it
    for table in TABLES_TO_VERSION:
        if not table_exists(conn, table):
            continue
        if not column_exists(conn, table, 'updated_at'):
            op.add_column(
                table,
                sa.Column(
                    'updated_at',
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text('CURRENT_TIMESTAMP')
                )
            )
            print(f"Added updated_at column to {table}")
        else:
            print(f"Skipping {table}.updated_at: already exists")
    
    # Create index on version for commonly queried tables
    for table in ['parts', 'work_orders', 'purchase_orders', 'quotes']:
        if not table_exists(conn, table):
            continue
        index_name = f'ix_{table}_version'
        if not index_exists(conn, index_name):
            op.create_index(index_name, table, ['version'])
            print(f"Created index {index_name}")
        else:
            print(f"Skipping index {index_name}: already exists")


def downgrade() -> None:
    conn = op.get_bind()
    
    # Drop indexes
    for table in ['parts', 'work_orders', 'purchase_orders', 'quotes']:
        index_name = f'ix_{table}_version'
        if index_exists(conn, index_name):
            op.drop_index(index_name, table_name=table)
    
    # Drop version column from all tables
    for table in TABLES_TO_VERSION:
        if table_exists(conn, table) and column_exists(conn, table, 'version'):
            op.drop_column(table, 'version')
