"""Immutable reviewed documents and explicit email attempt outcomes.

Revision ID: 094_document_deliveries
Revises: 093_mrp_supply_links
"""

import sqlalchemy as sa

from alembic import op

revision = '094_document_deliveries'
down_revision = '093_mrp_supply_links'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'document_deliveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('entity_type', sa.String(30), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('document_number', sa.String(100), nullable=False),
        sa.Column('issue_date', sa.Date()),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('attachment_name', sa.String(150), nullable=False),
        sa.Column('attachment_sha256', sa.String(64), nullable=False),
        sa.Column('attachment', sa.LargeBinary(), nullable=False),
        sa.Column('attachment_size', sa.Integer(), nullable=False),
        sa.Column('recipient', sa.String(320), nullable=False),
        sa.Column('subject', sa.String(200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('request_key', sa.String(100)),
        sa.Column('request_hash', sa.String(64)),
        sa.Column('provider_message_id', sa.String(150)),
        sa.Column('status_detail', sa.String(500)),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('sent_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('attempted_at', sa.DateTime()),
        sa.Column('accepted_at', sa.DateTime()),
        sa.Column('verified_at', sa.DateTime()),
        sa.Column('verified_by', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('verification_note', sa.Text()),
        sa.UniqueConstraint('company_id', 'request_key', name='uq_document_delivery_request'),
        sa.CheckConstraint("entity_type IN ('quote','purchase_order')", name='ck_delivery_entity'),
        sa.CheckConstraint("status IN ('prepared','sending','accepted','failed','unknown')", name='ck_delivery_status'),
        sa.CheckConstraint('length(attachment) <= 5242880', name='ck_delivery_attachment_size'),
        sa.CheckConstraint('length(body) <= 10000', name='ck_delivery_body_size'),
    )
    op.create_index('ix_document_deliveries_company_id', 'document_deliveries', ['company_id'])
    op.create_index('ix_delivery_company_source', 'document_deliveries', ['company_id', 'entity_type', 'entity_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE document_deliveries ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE document_deliveries FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE document_deliveries_id_seq FROM PUBLIC')
        op.execute('''DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE document_deliveries FROM anon;
            REVOKE ALL ON SEQUENCE document_deliveries_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE document_deliveries FROM authenticated;
            REVOKE ALL ON SEQUENCE document_deliveries_id_seq FROM authenticated;
          END IF;
        END $$''')


def downgrade():
    op.drop_table('document_deliveries')
