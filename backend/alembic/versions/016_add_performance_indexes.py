"""Add performance indexes

Revision ID: 016_add_performance_indexes
Revises: 015_add_work_center_availability_rate
Create Date: 2026-01-22
"""
from alembic import op

revision = '016_add_performance_indexes'
down_revision = '015_add_work_center_availability_rate'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_work_orders_status", "work_orders", ["status"])
    op.create_index("ix_work_orders_priority", "work_orders", ["priority"])
    op.create_index("ix_work_orders_due_date", "work_orders", ["due_date"])
    op.create_index("ix_inventory_items_part_id", "inventory_items", ["part_id"])
    op.create_index("ix_inventory_transactions_part_id", "inventory_transactions", ["part_id"])
    op.create_index("ix_inventory_transactions_transaction_type", "inventory_transactions", ["transaction_type"])
    op.create_index("ix_inventory_transactions_created_at", "inventory_transactions", ["created_at"])
    op.create_index("ix_bom_items_bom_id", "bom_items", ["bom_id"])
    op.create_index("ix_bom_items_component_part_id", "bom_items", ["component_part_id"])


def downgrade() -> None:
    op.drop_index("ix_bom_items_component_part_id", table_name="bom_items")
    op.drop_index("ix_bom_items_bom_id", table_name="bom_items")
    op.drop_index("ix_inventory_transactions_created_at", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_transaction_type", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_part_id", table_name="inventory_transactions")
    op.drop_index("ix_inventory_items_part_id", table_name="inventory_items")
    op.drop_index("ix_work_orders_due_date", table_name="work_orders")
    op.drop_index("ix_work_orders_priority", table_name="work_orders")
    op.drop_index("ix_work_orders_status", table_name="work_orders")
