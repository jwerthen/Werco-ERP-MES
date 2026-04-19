"""Add multi-tenancy support (companies table + company_id on all tables)

Revision ID: 026_add_multi_tenancy
Revises: 025_add_auto_evidence_fields
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '026_add_multi_tenancy'
down_revision = '025_add_auto_evidence_fields'
branch_labels = None
depends_on = None

# All tables that need company_id (via TenantMixin or direct column)
TENANT_TABLES = [
    'users',
    'work_centers',
    'parts',
    'work_orders',
    'work_order_operations',
    'time_entries',
    'boms',
    'bom_items',
    'routings',
    'routing_operations',
    'inventory_items',
    'inventory_transactions',
    'inventory_locations',
    'cycle_counts',
    'cycle_count_items',
    'documents',
    'mrp_runs',
    'mrp_requirements',
    'mrp_actions',
    'custom_field_definitions',
    'custom_field_values',
    'ncrs',
    'cars',
    'fais',
    'fai_characteristics',
    'vendors',
    'purchase_orders',
    'purchase_order_lines',
    'po_receipts',
    'shipments',
    'quotes',
    'quote_lines',
    'rfq_packages',
    'rfq_package_files',
    'quote_estimates',
    'quote_line_summaries',
    'price_snapshots',
    'customers',
    'equipment',
    'calibration_records',
    'supplier_part_mappings',
    'quote_materials',
    'quote_machines',
    'quote_finishes',
    'quote_settings',
    'labor_rates',
    'outside_services',
    'settings_audit_log',
    'report_templates',
    'notification_preferences',
    'notification_logs',
    'digest_queue',
    'jobs',
    'webhooks',
    'webhook_deliveries',
    'oee_records',
    'oee_targets',
    'downtime_events',
    'downtime_reason_codes',
    'job_costs',
    'cost_entries',
    'tools',
    'tool_checkouts',
    'tool_usage_logs',
    'maintenance_schedules',
    'maintenance_work_orders',
    'maintenance_logs',
    'operator_certifications',
    'training_records',
    'skill_matrix',
    'engineering_change_orders',
    'eco_approvals',
    'eco_implementation_tasks',
    'spc_characteristics',
    'spc_control_limits',
    'spc_measurements',
    'spc_process_capabilities',
    'customer_complaints',
    'return_material_authorizations',
    'supplier_scorecards',
    'supplier_audits',
    'approved_supplier_list',
    'qms_standards',
    'qms_clauses',
    'qms_clause_evidence',
    'role_permissions',
]

# Tables with unique constraints that need to become compound
UNIQUE_CONSTRAINT_CHANGES = [
    # (table, old_index_name_pattern, new_constraint_name, columns)
    ('users', 'ix_users_email', 'uq_users_company_email', ['company_id', 'email']),
    ('users', 'ix_users_employee_id', 'uq_users_company_employee_id', ['company_id', 'employee_id']),
    ('work_centers', 'ix_work_centers_code', 'uq_work_centers_company_code', ['company_id', 'code']),
    ('parts', 'ix_parts_part_number', 'uq_parts_company_part_number', ['company_id', 'part_number']),
    ('work_orders', 'ix_work_orders_work_order_number', 'uq_work_orders_company_wo_number', ['company_id', 'work_order_number']),
    ('quote_settings', 'ix_quote_settings_setting_key', 'uq_quote_settings_company_key', ['company_id', 'setting_key']),
    ('role_permissions', 'ix_role_permissions_role', 'uq_role_permissions_company_role', ['company_id', 'role']),
]


def upgrade() -> None:
    # 1. Create companies table
    op.create_table(
        'companies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('slug', sa.String(100), nullable=False),
        sa.Column('logo_url', sa.String(500), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('parent_company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=True),
        sa.Column('timezone', sa.String(50), server_default='America/Chicago'),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('phone', sa.String(50), nullable=True),
        sa.Column('website', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_companies_slug', 'companies', ['slug'], unique=True)

    # 2. Seed Werco as the initial company
    op.execute("INSERT INTO companies (id, name, slug, is_active) VALUES (1, 'Werco Manufacturing', 'werco', true)")
    op.execute("SELECT setval('companies_id_seq', 1)")

    # 3. Add PLATFORM_ADMIN to the UserRole enum.
    # Prod DBs were created with SQLAlchemy default behavior that stores enum
    # *names* (uppercase: 'ADMIN', 'MANAGER', ...) rather than the Python
    # `.value` strings (lowercase: 'admin', 'manager', ...). Detect which
    # case the existing enum uses and add the new value in the matching case
    # so the BEFORE clause resolves correctly on both styles.
    conn = op.get_bind()
    labels = {
        row[0] for row in conn.execute(sa.text(
            "SELECT enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'userrole'"
        )).fetchall()
    }
    if 'ADMIN' in labels:
        op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'PLATFORM_ADMIN' BEFORE 'ADMIN'")
    elif 'admin' in labels:
        op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'platform_admin' BEFORE 'admin'")
    else:
        raise RuntimeError(
            f"userrole enum has neither ADMIN nor admin label; found {labels!r}"
        )

    # 4. Add company_id to all tables (with DEFAULT 1 for existing data)
    for table in TENANT_TABLES:
        op.add_column(table, sa.Column('company_id', sa.Integer(), server_default='1', nullable=True))

    # 5. Backfill: ensure all rows have company_id = 1
    for table in TENANT_TABLES:
        op.execute(f"UPDATE {table} SET company_id = 1 WHERE company_id IS NULL")

    # 6. Set NOT NULL and add FK constraint
    for table in TENANT_TABLES:
        op.alter_column(table, 'company_id', nullable=False, server_default=None)
        op.create_foreign_key(
            f'fk_{table}_company_id',
            table,
            'companies',
            ['company_id'],
            ['id'],
        )
        # Add index for tenant-scoped queries
        op.create_index(f'ix_{table}_company_id', table, ['company_id'])

    # 7. Add company_id to audit_logs (nullable, since it's a special case)
    op.add_column('audit_logs', sa.Column('company_id', sa.Integer(), nullable=True))
    op.execute("UPDATE audit_logs SET company_id = 1 WHERE company_id IS NULL")
    op.create_foreign_key('fk_audit_logs_company_id', 'audit_logs', 'companies', ['company_id'], ['id'])
    op.create_index('ix_audit_logs_company_id', 'audit_logs', ['company_id'])

    # 8. Drop old unique constraints and create compound ones
    for table, old_idx, new_constraint, columns in UNIQUE_CONSTRAINT_CHANGES:
        # Try to drop the old unique index/constraint
        try:
            op.drop_index(old_idx, table_name=table)
        except Exception:
            pass
        try:
            op.drop_constraint(old_idx, table, type_='unique')
        except Exception:
            pass
        # Also try the common SQLAlchemy auto-generated names
        try:
            op.drop_constraint(f'{table}_{columns[-1]}_key', table, type_='unique')
        except Exception:
            pass

        # Create new compound unique constraint
        op.create_unique_constraint(new_constraint, table, columns)

    # 9. Add composite indexes for common query patterns
    op.create_index('ix_work_orders_company_status', 'work_orders', ['company_id', 'status'])
    op.create_index('ix_parts_company_active', 'parts', ['company_id', 'is_active'])
    op.create_index('ix_users_company_active', 'users', ['company_id', 'is_active'])


def downgrade() -> None:
    # Drop composite indexes
    op.drop_index('ix_work_orders_company_status', table_name='work_orders')
    op.drop_index('ix_parts_company_active', table_name='parts')
    op.drop_index('ix_users_company_active', table_name='users')

    # Restore old unique constraints
    for table, old_idx, new_constraint, columns in UNIQUE_CONSTRAINT_CHANGES:
        op.drop_constraint(new_constraint, table, type_='unique')
        op.create_index(old_idx, table, [columns[-1]], unique=True)

    # Drop company_id from audit_logs
    op.drop_index('ix_audit_logs_company_id', table_name='audit_logs')
    op.drop_constraint('fk_audit_logs_company_id', 'audit_logs', type_='foreignkey')
    op.drop_column('audit_logs', 'company_id')

    # Drop company_id from all tenant tables
    for table in reversed(TENANT_TABLES):
        op.drop_index(f'ix_{table}_company_id', table_name=table)
        op.drop_constraint(f'fk_{table}_company_id', table, type_='foreignkey')
        op.drop_column(table, 'company_id')

    # Note: Cannot remove enum values in PostgreSQL, so PLATFORM_ADMIN stays

    # Drop companies table
    op.drop_index('ix_companies_slug', table_name='companies')
    op.drop_table('companies')
