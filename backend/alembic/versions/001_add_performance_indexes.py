"""Add performance indexes for common query patterns

Revision ID: 001_performance_indexes
Revises: 
Create Date: 2026-01-05

This migration adds indexes to improve query performance.
All operations check if index exists before creating.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = '001_performance_indexes'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def safe_create_index(conn, name, table, columns):
    """Create index if it doesn't exist."""
    if not table_exists(conn, table):
        print(f"Skipping index {name}: table {table} missing")
        return
    if index_exists(conn, name):
        print(f"Skipping index {name}: already exists")
        return
    op.create_index(name, table, columns)
    print(f"Created index {name}")


def upgrade() -> None:
    conn = op.get_bind()
    
    # Work Orders
    safe_create_index(conn, 'ix_work_orders_status', 'work_orders', ['status'])
    safe_create_index(conn, 'ix_work_orders_due_date', 'work_orders', ['due_date'])
    safe_create_index(conn, 'ix_work_orders_status_due_date', 'work_orders', ['status', 'due_date'])
    safe_create_index(conn, 'ix_work_orders_created_at', 'work_orders', ['created_at'])
    safe_create_index(conn, 'ix_work_orders_customer_name', 'work_orders', ['customer_name'])
    safe_create_index(conn, 'ix_work_orders_actual_end', 'work_orders', ['actual_end'])
    
    # Work Order Operations
    safe_create_index(conn, 'ix_woo_work_center_status', 'work_order_operations', ['work_center_id', 'status'])
    safe_create_index(conn, 'ix_woo_status', 'work_order_operations', ['status'])
    safe_create_index(conn, 'ix_woo_scheduled_start', 'work_order_operations', ['scheduled_start'])
    
    # Time Entries
    safe_create_index(conn, 'ix_time_entries_user_clock_out', 'time_entries', ['user_id', 'clock_out'])
    safe_create_index(conn, 'ix_time_entries_wc_clock_in', 'time_entries', ['work_center_id', 'clock_in'])
    safe_create_index(conn, 'ix_time_entries_type_clock_in', 'time_entries', ['entry_type', 'clock_in'])
    
    # Inventory Items
    safe_create_index(conn, 'ix_inventory_items_part_active', 'inventory_items', ['part_id', 'is_active'])
    safe_create_index(conn, 'ix_inventory_items_status', 'inventory_items', ['status'])
    safe_create_index(conn, 'ix_inventory_items_warehouse', 'inventory_items', ['warehouse'])
    
    # Inventory Transactions
    safe_create_index(conn, 'ix_inv_txn_part_type_created', 'inventory_transactions', ['part_id', 'transaction_type', 'created_at'])
    safe_create_index(conn, 'ix_inv_txn_created_at', 'inventory_transactions', ['created_at'])
    
    # NCRs
    safe_create_index(conn, 'ix_ncrs_status', 'ncrs', ['status'])
    safe_create_index(conn, 'ix_ncrs_status_created', 'ncrs', ['status', 'created_at'])
    safe_create_index(conn, 'ix_ncrs_source', 'ncrs', ['source'])
    safe_create_index(conn, 'ix_ncrs_disposition', 'ncrs', ['disposition'])
    
    # CARs
    safe_create_index(conn, 'ix_cars_status', 'cars', ['status'])
    safe_create_index(conn, 'ix_cars_due_date', 'cars', ['due_date'])
    
    # Equipment
    safe_create_index(conn, 'ix_equipment_next_cal_date', 'equipment', ['next_calibration_date'])
    safe_create_index(conn, 'ix_equipment_status_active', 'equipment', ['status', 'is_active'])
    
    # Purchase Orders
    safe_create_index(conn, 'ix_purchase_orders_status', 'purchase_orders', ['status'])
    safe_create_index(conn, 'ix_purchase_orders_vendor_status', 'purchase_orders', ['vendor_id', 'status'])
    safe_create_index(conn, 'ix_purchase_orders_required_date', 'purchase_orders', ['required_date'])
    
    # PO Receipts
    safe_create_index(conn, 'ix_po_receipts_status', 'po_receipts', ['status'])
    safe_create_index(conn, 'ix_po_receipts_inspection_status', 'po_receipts', ['inspection_status'])
    safe_create_index(conn, 'ix_po_receipts_received_at', 'po_receipts', ['received_at'])
    
    # FAIs
    safe_create_index(conn, 'ix_fais_status', 'fais', ['status'])
    
    # Cycle Counts
    safe_create_index(conn, 'ix_cycle_counts_status_scheduled', 'cycle_counts', ['status', 'scheduled_date'])
    
    # Quotes
    safe_create_index(conn, 'ix_quotes_status', 'quotes', ['status'])
    safe_create_index(conn, 'ix_quotes_updated_at', 'quotes', ['updated_at'])


def downgrade() -> None:
    conn = op.get_bind()
    
    indexes = [
        'ix_quotes_updated_at', 'ix_quotes_status', 'ix_cycle_counts_status_scheduled',
        'ix_fais_status', 'ix_po_receipts_received_at', 'ix_po_receipts_inspection_status',
        'ix_po_receipts_status', 'ix_purchase_orders_required_date', 'ix_purchase_orders_vendor_status',
        'ix_purchase_orders_status', 'ix_equipment_status_active', 'ix_equipment_next_cal_date',
        'ix_cars_due_date', 'ix_cars_status', 'ix_ncrs_disposition', 'ix_ncrs_source',
        'ix_ncrs_status_created', 'ix_ncrs_status', 'ix_inv_txn_created_at', 'ix_inv_txn_part_type_created',
        'ix_inventory_items_warehouse', 'ix_inventory_items_status', 'ix_inventory_items_part_active',
        'ix_time_entries_type_clock_in', 'ix_time_entries_wc_clock_in', 'ix_time_entries_user_clock_out',
        'ix_woo_scheduled_start', 'ix_woo_status', 'ix_woo_work_center_status',
        'ix_work_orders_actual_end', 'ix_work_orders_customer_name', 'ix_work_orders_created_at',
        'ix_work_orders_status_due_date', 'ix_work_orders_due_date', 'ix_work_orders_status',
    ]
    
    for idx in indexes:
        if index_exists(conn, idx):
            # Extract table name from index name
            op.execute(text(f"DROP INDEX IF EXISTS {idx}"))
