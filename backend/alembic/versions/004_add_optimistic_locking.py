"""Add version columns for optimistic locking

Revision ID: 004_add_optimistic_locking
Revises: 003_add_database_constraints
Create Date: 2026-01-07

This migration adds version and updated_at columns to all tables
that support concurrent editing, enabling optimistic locking.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '004_add_optimistic_locking'
down_revision = '003_add_database_constraints'
branch_labels = None
depends_on = None

# Tables that need optimistic locking
# Grouped by priority/usage frequency
TABLES_TO_VERSION = [
    # High priority - frequently edited
    'parts',
    'work_orders',
    'work_order_operations',
    'purchase_orders',
    'purchase_order_lines',
    'quotes',
    'quote_lines',
    'work_centers',
    'vendors',
    'customers',
    
    # Medium priority - occasionally edited
    'boms',
    'bom_items',
    'routings',
    'routing_operations',
    'inventory_items',
    'inventory_locations',
    'time_entries',
    
    # Quality records - important for compliance
    'ncrs',
    'cars',
    'fais',
    'fai_characteristics',
    'po_receipts',
    
    # Configuration - rarely edited but critical
    'users',
    'documents',
    
    # MRP/Planning
    'mrp_runs',
    'mrp_actions',
    'mrp_requirements',
    
    # Other
    'shipments',
    'cycle_counts',
    'cycle_count_items',
    'equipment',
    'calibration_records',
    'supplier_part_mappings',
    
    # Quote configuration
    'quote_materials',
    'quote_machines',
    'quote_finishes',
    'quote_settings',
]

# Tables that already have updated_at column
TABLES_WITH_UPDATED_AT = [
    'parts',
    'work_orders',
    'work_order_operations',
    'purchase_orders',
    'quotes',
    'work_centers',
    'vendors',
    'customers',
    'boms',
    'bom_items',
    'routings',
    'routing_operations',
    'inventory_items',
    'time_entries',
    'ncrs',
    'cars',
    'fais',
    'users',
    'documents',
    'shipments',
    'cycle_counts',
]


def upgrade() -> None:
    # Add version column to all tables
    for table in TABLES_TO_VERSION:
        try:
            op.add_column(
                table,
                sa.Column('version', sa.Integer(), nullable=False, server_default='1')
            )
        except Exception as e:
            print(f"Warning: Could not add version to {table}: {e}")
    
    # Add updated_at only to tables that don't have it
    for table in TABLES_TO_VERSION:
        if table not in TABLES_WITH_UPDATED_AT:
            try:
                op.add_column(
                    table,
                    sa.Column(
                        'updated_at',
                        sa.DateTime(timezone=True),
                        nullable=False,
                        server_default=sa.text('CURRENT_TIMESTAMP')
                    )
                )
            except Exception as e:
                print(f"Warning: Could not add updated_at to {table}: {e}")
    
    # Create index on version for commonly queried tables
    for table in ['parts', 'work_orders', 'purchase_orders', 'quotes']:
        try:
            op.create_index(
                f'ix_{table}_version',
                table,
                ['version']
            )
        except Exception as e:
            print(f"Warning: Could not create index on {table}: {e}")


def downgrade() -> None:
    # Drop indexes
    for table in ['parts', 'work_orders', 'purchase_orders', 'quotes']:
        try:
            op.drop_index(f'ix_{table}_version', table_name=table)
        except Exception:
            pass
    
    # Drop updated_at from tables we added it to
    for table in TABLES_TO_VERSION:
        if table not in TABLES_WITH_UPDATED_AT:
            try:
                op.drop_column(table, 'updated_at')
            except Exception:
                pass
    
    # Drop version column from all tables
    for table in TABLES_TO_VERSION:
        try:
            op.drop_column(table, 'version')
        except Exception:
            pass
