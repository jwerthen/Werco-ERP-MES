"""Add database constraints and foreign keys

Revision ID: 003_add_database_constraints
Revises: 002_add_laser_press_brake
Create Date: 2026-01-07

This migration adds constraints safely, checking if they exist first.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '003_add_database_constraints'
down_revision = '002_add_laser_press_brake'
branch_labels = None
depends_on = None


def constraint_exists(connection, constraint_name):
    """Check if a constraint exists."""
    result = connection.execute(text(
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE constraint_name = :name"
    ), {"name": constraint_name})
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


def column_exists(connection, table_name, column_name):
    """Check if a column exists."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def safe_create_fk(conn, name, source_table, ref_table, local_cols, remote_cols, ondelete='SET NULL'):
    """Create foreign key if it doesn't exist."""
    if constraint_exists(conn, name):
        print(f"Skipping FK {name}: already exists")
        return
    if not table_exists(conn, source_table) or not table_exists(conn, ref_table):
        print(f"Skipping FK {name}: table missing")
        return
    for col in local_cols:
        if not column_exists(conn, source_table, col):
            print(f"Skipping FK {name}: column {col} missing")
            return
    op.create_foreign_key(name, source_table, ref_table, local_cols, remote_cols, ondelete=ondelete)
    print(f"Created FK {name}")


def safe_create_check(conn, name, table, condition, required_columns=None):
    """Create check constraint if it doesn't exist."""
    if constraint_exists(conn, name):
        print(f"Skipping check {name}: already exists")
        return
    if not table_exists(conn, table):
        print(f"Skipping check {name}: table missing")
        return
    # Check if required columns exist
    if required_columns:
        for col in required_columns:
            if not column_exists(conn, table, col):
                print(f"Skipping check {name}: column {col} missing")
                return
    op.create_check_constraint(name, table, condition)
    print(f"Created check {name}")


def safe_create_index(conn, name, table, columns):
    """Create index if it doesn't exist."""
    if index_exists(conn, name):
        print(f"Skipping index {name}: already exists")
        return
    if not table_exists(conn, table):
        print(f"Skipping index {name}: table missing")
        return
    op.create_index(name, table, columns)
    print(f"Created index {name}")


def safe_alter_column_default(conn, table, column, default):
    """Set column default if table/column exist."""
    if not table_exists(conn, table):
        return
    if not column_exists(conn, table, column):
        return
    op.alter_column(table, column, server_default=default)


def upgrade() -> None:
    conn = op.get_bind()
    
    # =========================================================================
    # PHASE 1: Foreign Key Constraints
    # =========================================================================
    
    safe_create_fk(conn, 'fk_users_created_by', 'users', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_parts_created_by', 'parts', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_parts_primary_supplier', 'parts', 'vendors', ['primary_supplier_id'], ['id'])
    safe_create_fk(conn, 'fk_work_orders_created_by', 'work_orders', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_work_orders_released_by', 'work_orders', 'users', ['released_by'], ['id'])
    safe_create_fk(conn, 'fk_work_orders_current_operation', 'work_orders', 'work_order_operations', ['current_operation_id'], ['id'])
    safe_create_fk(conn, 'fk_work_order_operations_started_by', 'work_order_operations', 'users', ['started_by'], ['id'])
    safe_create_fk(conn, 'fk_work_order_operations_completed_by', 'work_order_operations', 'users', ['completed_by'], ['id'])
    safe_create_fk(conn, 'fk_time_entries_approved_by', 'time_entries', 'users', ['approved_by'], ['id'])
    safe_create_fk(conn, 'fk_boms_created_by', 'boms', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_boms_approved_by', 'boms', 'users', ['approved_by'], ['id'])
    safe_create_fk(conn, 'fk_inventory_items_supplier', 'inventory_items', 'vendors', ['supplier_id'], ['id'])
    safe_create_fk(conn, 'fk_routing_operations_vendor', 'routing_operations', 'vendors', ['vendor_id'], ['id'])
    safe_create_fk(conn, 'fk_routings_created_by', 'routings', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_routings_approved_by', 'routings', 'users', ['approved_by'], ['id'])
    safe_create_fk(conn, 'fk_mrp_runs_created_by', 'mrp_runs', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_mrp_actions_processed_by', 'mrp_actions', 'users', ['processed_by'], ['id'])
    safe_create_fk(conn, 'fk_cycle_counts_assigned_to', 'cycle_counts', 'users', ['assigned_to'], ['id'])
    safe_create_fk(conn, 'fk_cycle_counts_completed_by', 'cycle_counts', 'users', ['completed_by'], ['id'])
    safe_create_fk(conn, 'fk_cycle_counts_created_by', 'cycle_counts', 'users', ['created_by'], ['id'])
    safe_create_fk(conn, 'fk_cycle_count_items_counted_by', 'cycle_count_items', 'users', ['counted_by'], ['id'])
    safe_create_fk(conn, 'fk_documents_released_by', 'documents', 'users', ['released_by'], ['id'])
    
    # =========================================================================
    # PHASE 2: Check Constraints
    # =========================================================================
    
    safe_create_check(conn, 'chk_work_orders_quantity_ordered_positive', 'work_orders', 'quantity_ordered > 0')
    safe_create_check(conn, 'chk_work_orders_quantity_complete_non_negative', 'work_orders', 'quantity_complete >= 0')
    safe_create_check(conn, 'chk_work_orders_quantity_scrapped_non_negative', 'work_orders', 'quantity_scrapped >= 0')
    safe_create_check(conn, 'chk_po_lines_quantity_ordered_positive', 'purchase_order_lines', 'quantity_ordered > 0')
    safe_create_check(conn, 'chk_po_lines_quantity_received_non_negative', 'purchase_order_lines', 'quantity_received >= 0')
    safe_create_check(conn, 'chk_po_lines_unit_price_non_negative', 'purchase_order_lines', 'unit_price >= 0')
    safe_create_check(conn, 'chk_po_receipts_quantity_received_positive', 'po_receipts', 'quantity_received > 0')
    safe_create_check(conn, 'chk_po_receipts_quantity_accepted_non_negative', 'po_receipts', 'quantity_accepted >= 0')
    safe_create_check(conn, 'chk_po_receipts_quantity_rejected_non_negative', 'po_receipts', 'quantity_rejected >= 0')
    safe_create_check(conn, 'chk_inventory_items_quantity_non_negative', 'inventory_items', 'quantity_on_hand >= 0')
    safe_create_check(conn, 'chk_inventory_items_allocated_non_negative', 'inventory_items', 'quantity_allocated >= 0')
    safe_create_check(conn, 'chk_bom_items_quantity_positive', 'bom_items', 'quantity > 0')
    safe_create_check(conn, 'chk_quote_lines_quantity_positive', 'quote_lines', 'quantity > 0')
    safe_create_check(conn, 'chk_quote_lines_unit_price_non_negative', 'quote_lines', 'unit_price >= 0')
    safe_create_check(conn, 'chk_work_order_ops_setup_time_non_negative', 'work_order_operations', 'setup_time_hours >= 0')
    safe_create_check(conn, 'chk_work_order_ops_run_time_non_negative', 'work_order_operations', 'run_time_hours >= 0')
    safe_create_check(conn, 'chk_routing_ops_setup_hours_non_negative', 'routing_operations', 'setup_hours >= 0')
    safe_create_check(conn, 'chk_routing_ops_run_hours_non_negative', 'routing_operations', 'run_hours_per_unit >= 0')
    safe_create_check(conn, 'chk_work_orders_priority_range', 'work_orders', 'priority >= 1 AND priority <= 10', ['priority'])
    safe_create_check(conn, 'chk_work_centers_efficiency_range', 'work_centers', 'efficiency >= 0 AND efficiency <= 200', ['efficiency'])
    safe_create_check(conn, 'chk_bom_items_scrap_factor_range', 'bom_items', 'scrap_factor >= 0 AND scrap_factor <= 1', ['scrap_factor'])
    safe_create_check(conn, 'chk_parts_standard_cost_non_negative', 'parts', 'standard_cost >= 0', ['standard_cost'])
    safe_create_check(conn, 'chk_work_centers_hourly_rate_non_negative', 'work_centers', 'hourly_rate >= 0', ['hourly_rate'])
    
    # =========================================================================
    # PHASE 3: Indexes (only new ones not in migration 001)
    # =========================================================================
    
    safe_create_index(conn, 'ix_time_entries_user_clock_in', 'time_entries', ['user_id', 'clock_in'])
    safe_create_index(conn, 'ix_inventory_transactions_part_created', 'inventory_transactions', ['part_id', 'created_at'])
    safe_create_index(conn, 'ix_po_receipts_status_received', 'po_receipts', ['status', 'received_at'])
    safe_create_index(conn, 'ix_audit_logs_resource_timestamp', 'audit_logs', ['resource_type', 'resource_id', 'timestamp'])
    safe_create_index(conn, 'ix_ncrs_status_source', 'ncrs', ['status', 'source'])
    safe_create_index(conn, 'ix_mrp_requirements_run_part', 'mrp_requirements', ['mrp_run_id', 'part_id'])
    
    # =========================================================================
    # PHASE 4: Column Defaults
    # =========================================================================
    
    safe_alter_column_default(conn, 'work_orders', 'created_at', sa.text('CURRENT_TIMESTAMP'))
    safe_alter_column_default(conn, 'work_order_operations', 'created_at', sa.text('CURRENT_TIMESTAMP'))
    safe_alter_column_default(conn, 'time_entries', 'created_at', sa.text('CURRENT_TIMESTAMP'))
    safe_alter_column_default(conn, 'inventory_transactions', 'created_at', sa.text('CURRENT_TIMESTAMP'))
    safe_alter_column_default(conn, 'audit_logs', 'timestamp', sa.text('CURRENT_TIMESTAMP'))


def downgrade() -> None:
    # Downgrade is complex - skip for now as we rarely downgrade in production
    pass
