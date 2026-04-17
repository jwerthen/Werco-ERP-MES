"""Fix tenant-scoped unique constraints + webhook FK + composite index

Revision ID: 027_tenant_uniques_and_webhook_fk
Revises: 026_add_multi_tenancy
Create Date: 2026-04-17

Migration 026 converted seven tables' unique constraints from global to
(company_id, X) but missed five more that store tenant-scoped identifiers
with globally unique columns. This blocks two companies from reusing the
same natural code (e.g. vendor code, PO number, bin location).

Also:
- Adds the missing foreign key + ON DELETE CASCADE on
  webhook_deliveries.webhook_id. Deleting a webhook currently leaves
  orphan delivery rows with no referential integrity.
- Adds a composite (company_id, due_date) index on work_orders to support
  the common late-work-orders query.

Pre-cleanup deletes any orphan webhook_deliveries before adding the FK
so the constraint add doesn't fail.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '027_tenant_uniques_and_webhook_fk'
down_revision = '026_add_multi_tenancy'
branch_labels = None
depends_on = None


# (table, old_index_name, new_constraint_name, columns)
UNIQUE_CONSTRAINT_CHANGES = [
    ('vendors', 'ix_vendors_code',
     'uq_vendors_company_code', ['company_id', 'code']),
    ('purchase_orders', 'ix_purchase_orders_po_number',
     'uq_purchase_orders_company_po_number', ['company_id', 'po_number']),
    ('po_receipts', 'ix_po_receipts_receipt_number',
     'uq_po_receipts_company_receipt_number', ['company_id', 'receipt_number']),
    ('inventory_locations', 'ix_inventory_locations_code',
     'uq_inventory_locations_company_code', ['company_id', 'code']),
    ('cycle_counts', 'ix_cycle_counts_count_number',
     'uq_cycle_counts_company_count_number', ['company_id', 'count_number']),
]


def upgrade() -> None:
    # 1. Convert global uniques to compound (company_id, <col>)
    for table, old_idx, new_constraint, columns in UNIQUE_CONSTRAINT_CHANGES:
        # Try each of the common ways the old unique could be stored: a
        # unique index, a unique constraint named like the index, or the
        # PG auto-generated <table>_<col>_key constraint.
        try:
            op.drop_index(old_idx, table_name=table)
        except Exception:
            pass
        try:
            op.drop_constraint(old_idx, table, type_='unique')
        except Exception:
            pass
        try:
            op.drop_constraint(f'{table}_{columns[-1]}_key', table, type_='unique')
        except Exception:
            pass

        op.create_unique_constraint(new_constraint, table, columns)

        # Keep a non-unique index on the column alone so existing
        # single-column lookups (e.g. "find vendor by code") still use
        # an index even though the uniqueness is now compound.
        op.create_index(
            f'ix_{table}_{columns[-1]}',
            table,
            [columns[-1]],
        )

    # 2. Clean up orphan webhook_deliveries, then add FK with cascade
    op.execute(
        "DELETE FROM webhook_deliveries "
        "WHERE webhook_id NOT IN (SELECT id FROM webhooks)"
    )
    op.create_foreign_key(
        'fk_webhook_deliveries_webhook_id',
        'webhook_deliveries', 'webhooks',
        ['webhook_id'], ['id'],
        ondelete='CASCADE',
    )

    # 3. Composite index for the late-work-orders query pattern
    op.create_index(
        'ix_work_orders_company_due_date',
        'work_orders',
        ['company_id', 'due_date'],
    )


def downgrade() -> None:
    # Drop composite index
    op.drop_index('ix_work_orders_company_due_date', table_name='work_orders')

    # Drop webhook FK
    op.drop_constraint(
        'fk_webhook_deliveries_webhook_id',
        'webhook_deliveries',
        type_='foreignkey',
    )

    # Restore old global unique constraints
    for table, old_idx, new_constraint, columns in UNIQUE_CONSTRAINT_CHANGES:
        try:
            op.drop_index(f'ix_{table}_{columns[-1]}', table_name=table)
        except Exception:
            pass
        op.drop_constraint(new_constraint, table, type_='unique')
        op.create_index(old_idx, table, [columns[-1]], unique=True)
