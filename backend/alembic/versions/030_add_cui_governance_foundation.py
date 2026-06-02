"""Add CUI governance foundation tables

Revision ID: 030_add_cui_governance_foundation
Revises: 029_heal_corrupted_work_center_types
Create Date: 2026-05-05
"""
from alembic import op
import json
import sqlalchemy as sa


revision = '030_add_cui_governance_foundation'
down_revision = '029_heal_corrupted_work_center_types'
branch_labels = None
depends_on = None


CLASSIFICATION_CHECK = (
    "'public', 'internal', 'business_confidential', 'fci', 'cui', "
    "'itar_export_controlled', 'restricted_customer', 'unknown'"
)


RETENTION_POLICY_ROWS = [
    {
        "company_id": 1,
        "policy_key": "customer_contract_record",
        "name": "Customer Contract Record",
        "description": "Customer contracts, customer-specific handling instructions, and controlled program metadata.",
        "default_retention_days": 2555,
        "retention_basis": "Contract term plus 7 years, or customer requirement if longer.",
        "retention_trigger": "contract_close_or_expiration",
        "applies_to_record_types": ["customer_contracts", "customer_handling_instructions"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "rfq_quote_record",
        "name": "RFQ And Quote Record",
        "description": "RFQs, quote packages, estimates, quote lines, and generated quote exports.",
        "default_retention_days": 2555,
        "retention_basis": "7 years after quote close, no-bid, win, or loss, or customer requirement if longer.",
        "retention_trigger": "quote_close",
        "applies_to_record_types": ["rfq_packages", "rfq_package_files", "quotes", "quote_estimates"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "controlled_document",
        "name": "Controlled Document",
        "description": "Drawings, specifications, work instructions, controlled procedures, and customer documents.",
        "default_retention_days": 3650,
        "retention_basis": "Life of part or program plus 10 years, or customer requirement if longer.",
        "retention_trigger": "part_or_program_end",
        "applies_to_record_types": ["documents", "document_files"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "engineering_record",
        "name": "Engineering Record",
        "description": "Parts, revisions, BOMs, routings, ECOs, approvals, and engineering release evidence.",
        "default_retention_days": 3650,
        "retention_basis": "Life of part or program plus 10 years.",
        "retention_trigger": "part_or_program_end",
        "applies_to_record_types": ["parts", "boms", "routings", "engineering_change_orders"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "production_record",
        "name": "Production Record",
        "description": "Work orders, released operation snapshots, travelers, job records, and traceability records.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment or completion, or customer requirement if longer.",
        "retention_trigger": "shipment_or_completion",
        "applies_to_record_types": ["work_orders", "work_order_operations", "jobs"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "quality_record",
        "name": "Quality Record",
        "description": "FAIs, inspection records, NCRs, CARs, SPC, and product quality evidence.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment or closure, or customer requirement if longer.",
        "retention_trigger": "shipment_or_closure",
        "applies_to_record_types": ["fais", "fai_characteristics", "ncrs", "cars", "spc_measurements"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "purchasing_receiving_record",
        "name": "Purchasing And Receiving Record",
        "description": "Purchase orders, receipts, supplier quality records, and material certificate metadata.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after receipt or shipment linkage, or customer requirement if longer.",
        "retention_trigger": "receipt_or_shipment_linkage",
        "applies_to_record_types": ["purchase_orders", "purchase_order_lines", "po_receipts"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "shipping_record",
        "name": "Shipping Record",
        "description": "Shipments, packing records, and customer delivery evidence.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment, or customer requirement if longer.",
        "retention_trigger": "shipment",
        "applies_to_record_types": ["shipments"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "training_record",
        "name": "Training Record",
        "description": "Operator certification, training, skill matrix, and security training evidence.",
        "default_retention_days": 2555,
        "retention_basis": "Employment term plus 7 years.",
        "retention_trigger": "employment_end",
        "applies_to_record_types": ["operator_certifications", "training_records", "skill_matrix"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "security_audit_record",
        "name": "Security Audit Record",
        "description": "Login events, access control changes, CUI access/export logs, and admin actions.",
        "default_retention_days": 1095,
        "retention_basis": "Minimum 1 year online and 3 years retained; longer if required.",
        "retention_trigger": "event_timestamp",
        "applies_to_record_types": ["audit_logs", "controlled_access_events", "export_events"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "application_audit_record",
        "name": "Application Audit Record",
        "description": "Business object create, update, release, void, supersede, and archive audit records.",
        "default_retention_days": None,
        "retention_basis": "Match parent record retention when tied to retained evidence.",
        "retention_trigger": "parent_record_retention",
        "applies_to_record_types": ["audit_logs", "classification_reviews", "legal_holds"],
        "requires_legal_review_before_purge": True,
        "active": True,
    },
    {
        "company_id": 1,
        "policy_key": "temporary_import_processing",
        "name": "Temporary Import Processing",
        "description": "Temporary parsed files, staging data, OCR/AI extraction artifacts, and intermediate import output.",
        "default_retention_days": 90,
        "retention_basis": "Delete after successful processing and verification, normally within 30-90 days.",
        "retention_trigger": "processing_complete",
        "applies_to_record_types": ["temporary_processing", "rfq_extraction_artifacts"],
        "requires_legal_review_before_purge": False,
        "active": True,
    },
]


def timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    ]


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        value = json.dumps(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def seed_retention_policies() -> None:
    for row in RETENTION_POLICY_ROWS:
        op.execute(
            "INSERT INTO retention_policies ("
            "company_id, policy_key, name, description, default_retention_days, "
            "retention_basis, retention_trigger, applies_to_record_types, "
            "requires_legal_review_before_purge, active"
            ") VALUES ("
            f"{sql_literal(row['company_id'])}, "
            f"{sql_literal(row['policy_key'])}, "
            f"{sql_literal(row['name'])}, "
            f"{sql_literal(row['description'])}, "
            f"{sql_literal(row['default_retention_days'])}, "
            f"{sql_literal(row['retention_basis'])}, "
            f"{sql_literal(row['retention_trigger'])}, "
            f"{sql_literal(row['applies_to_record_types'])}, "
            f"{sql_literal(row['requires_legal_review_before_purge'])}, "
            f"{sql_literal(row['active'])}"
            ")"
        )


def upgrade() -> None:
    op.create_table(
        'retention_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('policy_key', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('default_retention_days', sa.Integer(), nullable=True),
        sa.Column('retention_basis', sa.Text(), nullable=False),
        sa.Column('retention_trigger', sa.String(length=100), nullable=False),
        sa.Column('applies_to_record_types', sa.JSON(), nullable=True),
        sa.Column('requires_legal_review_before_purge', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'policy_key', name='uq_retention_policies_company_key'),
    )
    op.create_index('ix_retention_policies_company_id', 'retention_policies', ['company_id'])
    op.create_index('ix_retention_policies_policy_key', 'retention_policies', ['policy_key'])
    op.create_index('ix_retention_policies_active', 'retention_policies', ['active'])
    op.create_index('ix_retention_policies_company_active', 'retention_policies', ['company_id', 'active'])

    seed_retention_policies()

    op.create_table(
        'customer_contracts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('contract_number', sa.String(length=120), nullable=True),
        sa.Column('contract_name', sa.String(length=255), nullable=False),
        sa.Column('default_data_classification', sa.String(length=50), nullable=False, server_default='internal'),
        sa.Column('contains_cui', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('contains_fci', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('export_controlled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('itar_controlled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('dfars_clause_reference', sa.String(length=255), nullable=True),
        sa.Column('cui_categories', sa.JSON(), nullable=True),
        sa.Column('handling_instructions', sa.Text(), nullable=True),
        sa.Column('retention_policy_id', sa.Integer(), nullable=True),
        sa.Column('effective_date', sa.DateTime(), nullable=True),
        sa.Column('expiration_date', sa.DateTime(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            f"default_data_classification IN ({CLASSIFICATION_CHECK})",
            name='ck_customer_contracts_default_classification',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.ForeignKeyConstraint(['retention_policy_id'], ['retention_policies.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'contract_number', name='uq_customer_contracts_company_number'),
    )
    op.create_index('ix_customer_contracts_company_id', 'customer_contracts', ['company_id'])
    op.create_index('ix_customer_contracts_customer_id', 'customer_contracts', ['customer_id'])
    op.create_index('ix_customer_contracts_contract_number', 'customer_contracts', ['contract_number'])
    op.create_index('ix_customer_contracts_active', 'customer_contracts', ['active'])
    op.create_index('ix_customer_contracts_company_customer', 'customer_contracts', ['company_id', 'customer_id'])

    op.create_table(
        'customer_handling_instructions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('contract_id', sa.Integer(), nullable=True),
        sa.Column('instruction_type', sa.String(length=100), nullable=False),
        sa.Column('instruction_text', sa.Text(), nullable=False),
        sa.Column('default_data_classification', sa.String(length=50), nullable=False, server_default='unknown'),
        sa.Column('requires_marking', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('requires_export_review', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('retention_policy_id', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            f"default_data_classification IN ({CLASSIFICATION_CHECK})",
            name='ck_customer_handling_instructions_default_classification',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.ForeignKeyConstraint(['contract_id'], ['customer_contracts.id']),
        sa.ForeignKeyConstraint(['retention_policy_id'], ['retention_policies.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_customer_handling_instructions_company_id', 'customer_handling_instructions', ['company_id'])
    op.create_index('ix_customer_handling_instructions_customer_id', 'customer_handling_instructions', ['customer_id'])
    op.create_index('ix_customer_handling_instructions_contract_id', 'customer_handling_instructions', ['contract_id'])
    op.create_index('ix_customer_handling_instructions_active', 'customer_handling_instructions', ['active'])
    op.create_index(
        'ix_customer_handling_instructions_company_customer',
        'customer_handling_instructions',
        ['company_id', 'customer_id'],
    )

    op.create_table(
        'handling_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('classification_scope', sa.String(length=50), nullable=False, server_default='unknown'),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('contract_id', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            f"classification_scope IN ({CLASSIFICATION_CHECK})",
            name='ck_handling_groups_classification_scope',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.ForeignKeyConstraint(['contract_id'], ['customer_contracts.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'name', name='uq_handling_groups_company_name'),
    )
    op.create_index('ix_handling_groups_company_id', 'handling_groups', ['company_id'])
    op.create_index('ix_handling_groups_customer_id', 'handling_groups', ['customer_id'])
    op.create_index('ix_handling_groups_contract_id', 'handling_groups', ['contract_id'])
    op.create_index('ix_handling_groups_active', 'handling_groups', ['active'])
    op.create_index('ix_handling_groups_company_active', 'handling_groups', ['company_id', 'active'])

    op.create_table(
        'handling_group_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('handling_group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('membership_role', sa.String(length=50), nullable=False, server_default='member'),
        sa.Column('approved_by', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['handling_group_id'], ['handling_groups.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('handling_group_id', 'user_id', name='uq_handling_group_members_group_user'),
    )
    op.create_index('ix_handling_group_members_company_id', 'handling_group_members', ['company_id'])
    op.create_index('ix_handling_group_members_handling_group_id', 'handling_group_members', ['handling_group_id'])
    op.create_index('ix_handling_group_members_user_id', 'handling_group_members', ['user_id'])
    op.create_index('ix_handling_group_members_active', 'handling_group_members', ['active'])
    op.create_index('ix_handling_group_members_company_active', 'handling_group_members', ['company_id', 'active'])

    op.create_table(
        'classification_reviews',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(length=100), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('previous_classification', sa.String(length=50), nullable=True),
        sa.Column('new_classification', sa.String(length=50), nullable=False),
        sa.Column('review_type', sa.String(length=50), nullable=False, server_default='assignment'),
        sa.Column('justification', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('second_approved_by', sa.Integer(), nullable=True),
        sa.Column('second_approved_at', sa.DateTime(), nullable=True),
        sa.Column('extra_data', sa.JSON(), nullable=True),
        sa.CheckConstraint(
            f"new_classification IN ({CLASSIFICATION_CHECK})",
            name='ck_classification_reviews_new_classification',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.ForeignKeyConstraint(['second_approved_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_classification_reviews_company_id', 'classification_reviews', ['company_id'])
    op.create_index(
        'ix_classification_reviews_company_record',
        'classification_reviews',
        ['company_id', 'record_type', 'record_id'],
    )

    op.create_table(
        'document_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('parent_record_type', sa.String(length=100), nullable=False),
        sa.Column('parent_record_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('rfq_package_file_id', sa.Integer(), nullable=True),
        sa.Column('document_revision', sa.String(length=50), nullable=True),
        sa.Column('storage_provider', sa.String(length=50), nullable=False, server_default='local'),
        sa.Column('storage_container', sa.String(length=120), nullable=True),
        sa.Column('storage_key', sa.String(length=1000), nullable=False),
        sa.Column('original_file_name', sa.String(length=255), nullable=False),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('mime_type', sa.String(length=120), nullable=True),
        sa.Column('content_sha256', sa.String(length=64), nullable=True),
        sa.Column('file_purpose', sa.String(length=100), nullable=True),
        sa.Column('file_classification', sa.String(length=50), nullable=False, server_default='unknown'),
        sa.Column('retention_policy_id', sa.Integer(), nullable=True),
        sa.Column('legal_hold_active', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('released_by', sa.Integer(), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.Column('obsolete_by', sa.Integer(), nullable=True),
        sa.Column('obsolete_at', sa.DateTime(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            f"file_classification IN ({CLASSIFICATION_CHECK})",
            name='ck_document_files_file_classification',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['rfq_package_file_id'], ['rfq_package_files.id']),
        sa.ForeignKeyConstraint(['retention_policy_id'], ['retention_policies.id']),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id']),
        sa.ForeignKeyConstraint(['released_by'], ['users.id']),
        sa.ForeignKeyConstraint(['obsolete_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_document_files_company_id', 'document_files', ['company_id'])
    op.create_index('ix_document_files_parent_record_type', 'document_files', ['parent_record_type'])
    op.create_index('ix_document_files_parent_record_id', 'document_files', ['parent_record_id'])
    op.create_index('ix_document_files_document_id', 'document_files', ['document_id'])
    op.create_index('ix_document_files_rfq_package_file_id', 'document_files', ['rfq_package_file_id'])
    op.create_index('ix_document_files_content_sha256', 'document_files', ['content_sha256'])
    op.create_index('ix_document_files_company_parent', 'document_files', ['company_id', 'parent_record_type', 'parent_record_id'])
    op.create_index('ix_document_files_company_hash', 'document_files', ['company_id', 'content_sha256'])

    op.create_table(
        'legal_holds',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(length=100), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('hold_reason', sa.Text(), nullable=False),
        sa.Column('hold_owner', sa.String(length=255), nullable=True),
        sa.Column('placed_by', sa.Integer(), nullable=True),
        sa.Column('placed_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('released_by', sa.Integer(), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['placed_by'], ['users.id']),
        sa.ForeignKeyConstraint(['released_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_legal_holds_company_id', 'legal_holds', ['company_id'])
    op.create_index('ix_legal_holds_active', 'legal_holds', ['active'])
    op.create_index('ix_legal_holds_company_record', 'legal_holds', ['company_id', 'record_type', 'record_id'])
    op.create_index('ix_legal_holds_company_active', 'legal_holds', ['company_id', 'active'])

    op.create_table(
        'export_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(length=100), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('export_type', sa.String(length=100), nullable=False),
        sa.Column('export_format', sa.String(length=50), nullable=True),
        sa.Column('data_classification', sa.String(length=50), nullable=False, server_default='unknown'),
        sa.Column('included_record_refs', sa.JSON(), nullable=True),
        sa.Column('generated_file_id', sa.Integer(), nullable=True),
        sa.Column('export_reason', sa.Text(), nullable=True),
        sa.Column('exported_by', sa.Integer(), nullable=True),
        sa.Column('exported_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('destination_type', sa.String(length=100), nullable=True),
        sa.Column('destination_reference', sa.String(length=500), nullable=True),
        sa.Column('content_sha256', sa.String(length=64), nullable=True),
        sa.Column('extra_data', sa.JSON(), nullable=True),
        sa.CheckConstraint(
            f"data_classification IN ({CLASSIFICATION_CHECK})",
            name='ck_export_events_data_classification',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['generated_file_id'], ['document_files.id']),
        sa.ForeignKeyConstraint(['exported_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_export_events_company_id', 'export_events', ['company_id'])
    op.create_index('ix_export_events_company_record', 'export_events', ['company_id', 'record_type', 'record_id'])
    op.create_index('ix_export_events_company_exported_at', 'export_events', ['company_id', 'exported_at'])

    op.create_table(
        'controlled_access_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(length=100), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=True),
        sa.Column('file_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('allowed', sa.Boolean(), nullable=False),
        sa.Column('denial_reason', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('source_ip', sa.String(length=45), nullable=True),
        sa.Column('data_classification', sa.String(length=50), nullable=False, server_default='unknown'),
        sa.Column('extra_data', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['file_id'], ['document_files.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_controlled_access_events_company_id', 'controlled_access_events', ['company_id'])
    op.create_index(
        'ix_controlled_access_events_company_record',
        'controlled_access_events',
        ['company_id', 'record_type', 'record_id'],
    )
    op.create_index(
        'ix_controlled_access_events_company_occurred_at',
        'controlled_access_events',
        ['company_id', 'occurred_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_controlled_access_events_company_occurred_at', table_name='controlled_access_events')
    op.drop_index('ix_controlled_access_events_company_record', table_name='controlled_access_events')
    op.drop_index('ix_controlled_access_events_company_id', table_name='controlled_access_events')
    op.drop_table('controlled_access_events')

    op.drop_index('ix_export_events_company_exported_at', table_name='export_events')
    op.drop_index('ix_export_events_company_record', table_name='export_events')
    op.drop_index('ix_export_events_company_id', table_name='export_events')
    op.drop_table('export_events')

    op.drop_index('ix_legal_holds_company_active', table_name='legal_holds')
    op.drop_index('ix_legal_holds_company_record', table_name='legal_holds')
    op.drop_index('ix_legal_holds_active', table_name='legal_holds')
    op.drop_index('ix_legal_holds_company_id', table_name='legal_holds')
    op.drop_table('legal_holds')

    op.drop_index('ix_document_files_company_hash', table_name='document_files')
    op.drop_index('ix_document_files_company_parent', table_name='document_files')
    op.drop_index('ix_document_files_content_sha256', table_name='document_files')
    op.drop_index('ix_document_files_rfq_package_file_id', table_name='document_files')
    op.drop_index('ix_document_files_document_id', table_name='document_files')
    op.drop_index('ix_document_files_parent_record_id', table_name='document_files')
    op.drop_index('ix_document_files_parent_record_type', table_name='document_files')
    op.drop_index('ix_document_files_company_id', table_name='document_files')
    op.drop_table('document_files')

    op.drop_index('ix_classification_reviews_company_record', table_name='classification_reviews')
    op.drop_index('ix_classification_reviews_company_id', table_name='classification_reviews')
    op.drop_table('classification_reviews')

    op.drop_index('ix_handling_group_members_company_active', table_name='handling_group_members')
    op.drop_index('ix_handling_group_members_active', table_name='handling_group_members')
    op.drop_index('ix_handling_group_members_user_id', table_name='handling_group_members')
    op.drop_index('ix_handling_group_members_handling_group_id', table_name='handling_group_members')
    op.drop_index('ix_handling_group_members_company_id', table_name='handling_group_members')
    op.drop_table('handling_group_members')

    op.drop_index('ix_handling_groups_company_active', table_name='handling_groups')
    op.drop_index('ix_handling_groups_active', table_name='handling_groups')
    op.drop_index('ix_handling_groups_contract_id', table_name='handling_groups')
    op.drop_index('ix_handling_groups_customer_id', table_name='handling_groups')
    op.drop_index('ix_handling_groups_company_id', table_name='handling_groups')
    op.drop_table('handling_groups')

    op.drop_index('ix_customer_handling_instructions_company_customer', table_name='customer_handling_instructions')
    op.drop_index('ix_customer_handling_instructions_active', table_name='customer_handling_instructions')
    op.drop_index('ix_customer_handling_instructions_contract_id', table_name='customer_handling_instructions')
    op.drop_index('ix_customer_handling_instructions_customer_id', table_name='customer_handling_instructions')
    op.drop_index('ix_customer_handling_instructions_company_id', table_name='customer_handling_instructions')
    op.drop_table('customer_handling_instructions')

    op.drop_index('ix_customer_contracts_company_customer', table_name='customer_contracts')
    op.drop_index('ix_customer_contracts_active', table_name='customer_contracts')
    op.drop_index('ix_customer_contracts_contract_number', table_name='customer_contracts')
    op.drop_index('ix_customer_contracts_customer_id', table_name='customer_contracts')
    op.drop_index('ix_customer_contracts_company_id', table_name='customer_contracts')
    op.drop_table('customer_contracts')

    op.drop_index('ix_retention_policies_company_active', table_name='retention_policies')
    op.drop_index('ix_retention_policies_active', table_name='retention_policies')
    op.drop_index('ix_retention_policies_policy_key', table_name='retention_policies')
    op.drop_index('ix_retention_policies_company_id', table_name='retention_policies')
    op.drop_table('retention_policies')
