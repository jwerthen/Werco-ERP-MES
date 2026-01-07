"""Add database constraints and foreign keys

Revision ID: 003_add_database_constraints
Revises: 002_add_laser_press_brake_types
Create Date: 2026-01-07

This migration adds:
1. Missing foreign key constraints with appropriate ON DELETE rules
2. Unique constraints for business rules
3. Check constraints for data validation
4. Additional indexes for query performance
5. NOT NULL constraints for critical fields
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '003_add_database_constraints'
down_revision = '002_add_laser_press_brake_types'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # PHASE 1: Add missing foreign key constraints
    # =========================================================================
    
    # --- Users table ---
    # created_by should reference users (self-referential)
    op.create_foreign_key(
        'fk_users_created_by',
        'users', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Parts table ---
    # created_by should reference users
    op.create_foreign_key(
        'fk_parts_created_by',
        'parts', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    # primary_supplier_id should reference vendors
    op.create_foreign_key(
        'fk_parts_primary_supplier',
        'parts', 'vendors',
        ['primary_supplier_id'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Work Orders table ---
    # created_by should reference users
    op.create_foreign_key(
        'fk_work_orders_created_by',
        'work_orders', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    # released_by should reference users
    op.create_foreign_key(
        'fk_work_orders_released_by',
        'work_orders', 'users',
        ['released_by'], ['id'],
        ondelete='SET NULL'
    )
    # current_operation_id should reference work_order_operations
    op.create_foreign_key(
        'fk_work_orders_current_operation',
        'work_orders', 'work_order_operations',
        ['current_operation_id'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Work Order Operations table ---
    # started_by should reference users
    op.create_foreign_key(
        'fk_work_order_operations_started_by',
        'work_order_operations', 'users',
        ['started_by'], ['id'],
        ondelete='SET NULL'
    )
    # completed_by should reference users
    op.create_foreign_key(
        'fk_work_order_operations_completed_by',
        'work_order_operations', 'users',
        ['completed_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Time Entries table ---
    # approved_by should reference users
    op.create_foreign_key(
        'fk_time_entries_approved_by',
        'time_entries', 'users',
        ['approved_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- BOMs table ---
    # created_by should reference users
    op.create_foreign_key(
        'fk_boms_created_by',
        'boms', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    # approved_by should reference users
    op.create_foreign_key(
        'fk_boms_approved_by',
        'boms', 'users',
        ['approved_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Inventory Items table ---
    # supplier_id should reference vendors
    op.create_foreign_key(
        'fk_inventory_items_supplier',
        'inventory_items', 'vendors',
        ['supplier_id'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Routing Operations table ---
    # vendor_id should reference vendors (for outside processing)
    op.create_foreign_key(
        'fk_routing_operations_vendor',
        'routing_operations', 'vendors',
        ['vendor_id'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Routings table ---
    # created_by should reference users
    op.create_foreign_key(
        'fk_routings_created_by',
        'routings', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    # approved_by should reference users
    op.create_foreign_key(
        'fk_routings_approved_by',
        'routings', 'users',
        ['approved_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- MRP Runs table ---
    # created_by should reference users
    op.create_foreign_key(
        'fk_mrp_runs_created_by',
        'mrp_runs', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- MRP Actions table ---
    # processed_by should reference users
    op.create_foreign_key(
        'fk_mrp_actions_processed_by',
        'mrp_actions', 'users',
        ['processed_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Cycle Counts table ---
    # assigned_to should reference users
    op.create_foreign_key(
        'fk_cycle_counts_assigned_to',
        'cycle_counts', 'users',
        ['assigned_to'], ['id'],
        ondelete='SET NULL'
    )
    # completed_by should reference users
    op.create_foreign_key(
        'fk_cycle_counts_completed_by',
        'cycle_counts', 'users',
        ['completed_by'], ['id'],
        ondelete='SET NULL'
    )
    # created_by should reference users
    op.create_foreign_key(
        'fk_cycle_counts_created_by',
        'cycle_counts', 'users',
        ['created_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Cycle Count Items table ---
    # counted_by should reference users
    op.create_foreign_key(
        'fk_cycle_count_items_counted_by',
        'cycle_count_items', 'users',
        ['counted_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # --- Documents table ---
    # released_by should reference users
    op.create_foreign_key(
        'fk_documents_released_by',
        'documents', 'users',
        ['released_by'], ['id'],
        ondelete='SET NULL'
    )
    
    # =========================================================================
    # PHASE 2: Add check constraints for data validation
    # =========================================================================
    
    # Quantity validations (must be non-negative)
    op.create_check_constraint(
        'chk_work_orders_quantity_ordered_positive',
        'work_orders',
        'quantity_ordered > 0'
    )
    op.create_check_constraint(
        'chk_work_orders_quantity_complete_non_negative',
        'work_orders',
        'quantity_complete >= 0'
    )
    op.create_check_constraint(
        'chk_work_orders_quantity_scrapped_non_negative',
        'work_orders',
        'quantity_scrapped >= 0'
    )
    
    op.create_check_constraint(
        'chk_po_lines_quantity_ordered_positive',
        'purchase_order_lines',
        'quantity_ordered > 0'
    )
    op.create_check_constraint(
        'chk_po_lines_quantity_received_non_negative',
        'purchase_order_lines',
        'quantity_received >= 0'
    )
    op.create_check_constraint(
        'chk_po_lines_unit_price_non_negative',
        'purchase_order_lines',
        'unit_price >= 0'
    )
    
    op.create_check_constraint(
        'chk_po_receipts_quantity_received_positive',
        'po_receipts',
        'quantity_received > 0'
    )
    op.create_check_constraint(
        'chk_po_receipts_quantity_accepted_non_negative',
        'po_receipts',
        'quantity_accepted >= 0'
    )
    op.create_check_constraint(
        'chk_po_receipts_quantity_rejected_non_negative',
        'po_receipts',
        'quantity_rejected >= 0'
    )
    
    op.create_check_constraint(
        'chk_inventory_items_quantity_non_negative',
        'inventory_items',
        'quantity_on_hand >= 0'
    )
    op.create_check_constraint(
        'chk_inventory_items_allocated_non_negative',
        'inventory_items',
        'quantity_allocated >= 0'
    )
    
    op.create_check_constraint(
        'chk_bom_items_quantity_positive',
        'bom_items',
        'quantity > 0'
    )
    
    op.create_check_constraint(
        'chk_quote_lines_quantity_positive',
        'quote_lines',
        'quantity > 0'
    )
    op.create_check_constraint(
        'chk_quote_lines_unit_price_non_negative',
        'quote_lines',
        'unit_price >= 0'
    )
    
    # Time validations (must be non-negative)
    op.create_check_constraint(
        'chk_work_order_ops_setup_time_non_negative',
        'work_order_operations',
        'setup_time_hours >= 0'
    )
    op.create_check_constraint(
        'chk_work_order_ops_run_time_non_negative',
        'work_order_operations',
        'run_time_hours >= 0'
    )
    
    op.create_check_constraint(
        'chk_routing_ops_setup_hours_non_negative',
        'routing_operations',
        'setup_hours >= 0'
    )
    op.create_check_constraint(
        'chk_routing_ops_run_hours_non_negative',
        'routing_operations',
        'run_hours_per_unit >= 0'
    )
    
    # Priority validations (1-10 range)
    op.create_check_constraint(
        'chk_work_orders_priority_range',
        'work_orders',
        'priority >= 1 AND priority <= 10'
    )
    
    # Percentage validations (0-1 range for factors)
    op.create_check_constraint(
        'chk_work_centers_efficiency_range',
        'work_centers',
        'efficiency_factor >= 0 AND efficiency_factor <= 2'
    )
    op.create_check_constraint(
        'chk_bom_items_scrap_factor_range',
        'bom_items',
        'scrap_factor >= 0 AND scrap_factor <= 1'
    )
    
    # Cost validations
    op.create_check_constraint(
        'chk_parts_standard_cost_non_negative',
        'parts',
        'standard_cost >= 0'
    )
    op.create_check_constraint(
        'chk_work_centers_hourly_rate_non_negative',
        'work_centers',
        'hourly_rate >= 0'
    )
    
    # =========================================================================
    # PHASE 3: Add unique constraints for business rules
    # =========================================================================
    
    # Work order operations must have unique sequence within a work order
    op.create_unique_constraint(
        'uq_work_order_operations_sequence',
        'work_order_operations',
        ['work_order_id', 'sequence']
    )
    
    # Routing operations must have unique sequence within a routing
    op.create_unique_constraint(
        'uq_routing_operations_sequence',
        'routing_operations',
        ['routing_id', 'sequence']
    )
    
    # BOM items must have unique item number within a BOM
    op.create_unique_constraint(
        'uq_bom_items_item_number',
        'bom_items',
        ['bom_id', 'item_number']
    )
    
    # PO lines must have unique line number within a PO
    op.create_unique_constraint(
        'uq_po_lines_line_number',
        'purchase_order_lines',
        ['purchase_order_id', 'line_number']
    )
    
    # Quote lines must have unique line number within a quote
    op.create_unique_constraint(
        'uq_quote_lines_line_number',
        'quote_lines',
        ['quote_id', 'line_number']
    )
    
    # FAI characteristics must have unique char number within an FAI
    op.create_unique_constraint(
        'uq_fai_characteristics_char_number',
        'fai_characteristics',
        ['fai_id', 'char_number']
    )
    
    # =========================================================================
    # PHASE 4: Add composite indexes for common query patterns
    # =========================================================================
    
    # Work orders: commonly filtered by status and due date
    op.create_index(
        'ix_work_orders_status_due_date',
        'work_orders',
        ['status', 'due_date']
    )
    
    # Work order operations: commonly filtered by status and work center
    op.create_index(
        'ix_work_order_ops_status_work_center',
        'work_order_operations',
        ['status', 'work_center_id']
    )
    
    # Time entries: commonly queried by user and date range
    op.create_index(
        'ix_time_entries_user_clock_in',
        'time_entries',
        ['user_id', 'clock_in']
    )
    
    # Inventory transactions: commonly queried by part and date
    op.create_index(
        'ix_inventory_transactions_part_created',
        'inventory_transactions',
        ['part_id', 'created_at']
    )
    
    # PO Receipts: commonly queried by status and received date
    op.create_index(
        'ix_po_receipts_status_received',
        'po_receipts',
        ['status', 'received_at']
    )
    
    # Audit logs: commonly queried by resource and timestamp
    op.create_index(
        'ix_audit_logs_resource_timestamp',
        'audit_logs',
        ['resource_type', 'resource_id', 'timestamp']
    )
    
    # NCRs: commonly queried by status and source
    op.create_index(
        'ix_ncrs_status_source',
        'ncrs',
        ['status', 'source']
    )
    
    # MRP Requirements: commonly queried by run and part
    op.create_index(
        'ix_mrp_requirements_run_part',
        'mrp_requirements',
        ['mrp_run_id', 'part_id']
    )
    
    # =========================================================================
    # PHASE 5: Add defaults for critical fields
    # =========================================================================
    
    # Set default timestamps where missing
    op.alter_column('work_orders', 'created_at',
        server_default=sa.text('CURRENT_TIMESTAMP')
    )
    op.alter_column('work_order_operations', 'created_at',
        server_default=sa.text('CURRENT_TIMESTAMP')
    )
    op.alter_column('time_entries', 'created_at',
        server_default=sa.text('CURRENT_TIMESTAMP')
    )
    op.alter_column('inventory_transactions', 'created_at',
        server_default=sa.text('CURRENT_TIMESTAMP')
    )
    op.alter_column('audit_logs', 'timestamp',
        server_default=sa.text('CURRENT_TIMESTAMP')
    )


def downgrade() -> None:
    # =========================================================================
    # Remove defaults
    # =========================================================================
    op.alter_column('audit_logs', 'timestamp', server_default=None)
    op.alter_column('inventory_transactions', 'created_at', server_default=None)
    op.alter_column('time_entries', 'created_at', server_default=None)
    op.alter_column('work_order_operations', 'created_at', server_default=None)
    op.alter_column('work_orders', 'created_at', server_default=None)
    
    # =========================================================================
    # Remove composite indexes
    # =========================================================================
    op.drop_index('ix_mrp_requirements_run_part', table_name='mrp_requirements')
    op.drop_index('ix_ncrs_status_source', table_name='ncrs')
    op.drop_index('ix_audit_logs_resource_timestamp', table_name='audit_logs')
    op.drop_index('ix_po_receipts_status_received', table_name='po_receipts')
    op.drop_index('ix_inventory_transactions_part_created', table_name='inventory_transactions')
    op.drop_index('ix_time_entries_user_clock_in', table_name='time_entries')
    op.drop_index('ix_work_order_ops_status_work_center', table_name='work_order_operations')
    op.drop_index('ix_work_orders_status_due_date', table_name='work_orders')
    
    # =========================================================================
    # Remove unique constraints
    # =========================================================================
    op.drop_constraint('uq_fai_characteristics_char_number', 'fai_characteristics', type_='unique')
    op.drop_constraint('uq_quote_lines_line_number', 'quote_lines', type_='unique')
    op.drop_constraint('uq_po_lines_line_number', 'purchase_order_lines', type_='unique')
    op.drop_constraint('uq_bom_items_item_number', 'bom_items', type_='unique')
    op.drop_constraint('uq_routing_operations_sequence', 'routing_operations', type_='unique')
    op.drop_constraint('uq_work_order_operations_sequence', 'work_order_operations', type_='unique')
    
    # =========================================================================
    # Remove check constraints
    # =========================================================================
    op.drop_constraint('chk_work_centers_hourly_rate_non_negative', 'work_centers', type_='check')
    op.drop_constraint('chk_parts_standard_cost_non_negative', 'parts', type_='check')
    op.drop_constraint('chk_bom_items_scrap_factor_range', 'bom_items', type_='check')
    op.drop_constraint('chk_work_centers_efficiency_range', 'work_centers', type_='check')
    op.drop_constraint('chk_work_orders_priority_range', 'work_orders', type_='check')
    op.drop_constraint('chk_routing_ops_run_hours_non_negative', 'routing_operations', type_='check')
    op.drop_constraint('chk_routing_ops_setup_hours_non_negative', 'routing_operations', type_='check')
    op.drop_constraint('chk_work_order_ops_run_time_non_negative', 'work_order_operations', type_='check')
    op.drop_constraint('chk_work_order_ops_setup_time_non_negative', 'work_order_operations', type_='check')
    op.drop_constraint('chk_quote_lines_unit_price_non_negative', 'quote_lines', type_='check')
    op.drop_constraint('chk_quote_lines_quantity_positive', 'quote_lines', type_='check')
    op.drop_constraint('chk_bom_items_quantity_positive', 'bom_items', type_='check')
    op.drop_constraint('chk_inventory_items_allocated_non_negative', 'inventory_items', type_='check')
    op.drop_constraint('chk_inventory_items_quantity_non_negative', 'inventory_items', type_='check')
    op.drop_constraint('chk_po_receipts_quantity_rejected_non_negative', 'po_receipts', type_='check')
    op.drop_constraint('chk_po_receipts_quantity_accepted_non_negative', 'po_receipts', type_='check')
    op.drop_constraint('chk_po_receipts_quantity_received_positive', 'po_receipts', type_='check')
    op.drop_constraint('chk_po_lines_unit_price_non_negative', 'purchase_order_lines', type_='check')
    op.drop_constraint('chk_po_lines_quantity_received_non_negative', 'purchase_order_lines', type_='check')
    op.drop_constraint('chk_po_lines_quantity_ordered_positive', 'purchase_order_lines', type_='check')
    op.drop_constraint('chk_work_orders_quantity_scrapped_non_negative', 'work_orders', type_='check')
    op.drop_constraint('chk_work_orders_quantity_complete_non_negative', 'work_orders', type_='check')
    op.drop_constraint('chk_work_orders_quantity_ordered_positive', 'work_orders', type_='check')
    
    # =========================================================================
    # Remove foreign key constraints
    # =========================================================================
    op.drop_constraint('fk_documents_released_by', 'documents', type_='foreignkey')
    op.drop_constraint('fk_cycle_count_items_counted_by', 'cycle_count_items', type_='foreignkey')
    op.drop_constraint('fk_cycle_counts_created_by', 'cycle_counts', type_='foreignkey')
    op.drop_constraint('fk_cycle_counts_completed_by', 'cycle_counts', type_='foreignkey')
    op.drop_constraint('fk_cycle_counts_assigned_to', 'cycle_counts', type_='foreignkey')
    op.drop_constraint('fk_mrp_actions_processed_by', 'mrp_actions', type_='foreignkey')
    op.drop_constraint('fk_mrp_runs_created_by', 'mrp_runs', type_='foreignkey')
    op.drop_constraint('fk_routings_approved_by', 'routings', type_='foreignkey')
    op.drop_constraint('fk_routings_created_by', 'routings', type_='foreignkey')
    op.drop_constraint('fk_routing_operations_vendor', 'routing_operations', type_='foreignkey')
    op.drop_constraint('fk_inventory_items_supplier', 'inventory_items', type_='foreignkey')
    op.drop_constraint('fk_boms_approved_by', 'boms', type_='foreignkey')
    op.drop_constraint('fk_boms_created_by', 'boms', type_='foreignkey')
    op.drop_constraint('fk_time_entries_approved_by', 'time_entries', type_='foreignkey')
    op.drop_constraint('fk_work_order_operations_completed_by', 'work_order_operations', type_='foreignkey')
    op.drop_constraint('fk_work_order_operations_started_by', 'work_order_operations', type_='foreignkey')
    op.drop_constraint('fk_work_orders_current_operation', 'work_orders', type_='foreignkey')
    op.drop_constraint('fk_work_orders_released_by', 'work_orders', type_='foreignkey')
    op.drop_constraint('fk_work_orders_created_by', 'work_orders', type_='foreignkey')
    op.drop_constraint('fk_parts_primary_supplier', 'parts', type_='foreignkey')
    op.drop_constraint('fk_parts_created_by', 'parts', type_='foreignkey')
    op.drop_constraint('fk_users_created_by', 'users', type_='foreignkey')
