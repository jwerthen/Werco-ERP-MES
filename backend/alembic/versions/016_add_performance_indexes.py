"""Add performance indexes

Revision ID: 016_add_performance_indexes
Revises: 015_add_work_center_availability_rate
Create Date: 2026-01-22
"""
from alembic import op


def _create_index_if_not_exists(name: str, table: str, columns: list[str]) -> None:
    cols = ", ".join(columns)
    op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")


def _drop_index_if_exists(name: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {name}")

revision = '016_add_performance_indexes'
down_revision = '015_add_work_center_availability_rate'
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_index_if_not_exists("ix_work_orders_status", "work_orders", ["status"])
    _create_index_if_not_exists("ix_work_orders_priority", "work_orders", ["priority"])
    _create_index_if_not_exists("ix_work_orders_due_date", "work_orders", ["due_date"])
    _create_index_if_not_exists("ix_inventory_items_part_id", "inventory_items", ["part_id"])
    _create_index_if_not_exists("ix_inventory_transactions_part_id", "inventory_transactions", ["part_id"])
    _create_index_if_not_exists("ix_inventory_transactions_transaction_type", "inventory_transactions", ["transaction_type"])
    _create_index_if_not_exists("ix_inventory_transactions_created_at", "inventory_transactions", ["created_at"])
    _create_index_if_not_exists("ix_bom_items_bom_id", "bom_items", ["bom_id"])
    _create_index_if_not_exists("ix_bom_items_component_part_id", "bom_items", ["component_part_id"])


def downgrade() -> None:
    _drop_index_if_exists("ix_bom_items_component_part_id")
    _drop_index_if_exists("ix_bom_items_bom_id")
    _drop_index_if_exists("ix_inventory_transactions_created_at")
    _drop_index_if_exists("ix_inventory_transactions_transaction_type")
    _drop_index_if_exists("ix_inventory_transactions_part_id")
    _drop_index_if_exists("ix_inventory_items_part_id")
    _drop_index_if_exists("ix_work_orders_due_date")
    _drop_index_if_exists("ix_work_orders_priority")
    _drop_index_if_exists("ix_work_orders_status")
