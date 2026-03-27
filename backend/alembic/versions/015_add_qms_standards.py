"""Add QMS standards, clauses, and evidence tables

Revision ID: 015_add_qms_standards
Revises: 014b_widen_alembic_version
Create Date: 2026-03-27

Adds tables for QMS (Quality Management System) standard tracking:
- qms_standards: Top-level standards (AS9100D, ISO 9001, etc.)
- qms_clauses: Individual clauses/requirements within standards
- qms_clause_evidence: Evidence links mapping clauses to system records
"""
from alembic import op
import sqlalchemy as sa

revision = '015_add_qms_standards'
down_revision = '014b_widen_alembic_version'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # QMS Standards table
    op.create_table(
        'qms_standards',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('version', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('standard_body', sa.String(255), nullable=True),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('documents.id'), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_qms_standards_id', 'qms_standards', ['id'])
    op.create_index('ix_qms_standards_name', 'qms_standards', ['name'])

    # QMS Clauses table
    op.create_table(
        'qms_clauses',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('standard_id', sa.Integer(), sa.ForeignKey('qms_standards.id', ondelete='CASCADE'), nullable=False),
        sa.Column('clause_number', sa.String(50), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('parent_clause_id', sa.Integer(), sa.ForeignKey('qms_clauses.id'), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('compliance_status', sa.String(50), server_default='not_assessed', nullable=False),
        sa.Column('compliance_notes', sa.Text(), nullable=True),
        sa.Column('last_assessed_date', sa.DateTime(), nullable=True),
        sa.Column('last_assessed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('next_review_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_qms_clauses_id', 'qms_clauses', ['id'])
    op.create_index('ix_qms_clauses_standard_id', 'qms_clauses', ['standard_id'])
    op.create_index('ix_qms_clauses_clause_number', 'qms_clauses', ['clause_number'])

    # QMS Clause Evidence table
    op.create_table(
        'qms_clause_evidence',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('clause_id', sa.Integer(), sa.ForeignKey('qms_clauses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('evidence_type', sa.String(50), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('documents.id'), nullable=True),
        sa.Column('module_reference', sa.String(255), nullable=True),
        sa.Column('record_type', sa.String(100), nullable=True),
        sa.Column('record_id', sa.Integer(), nullable=True),
        sa.Column('is_verified', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('verified_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('verified_date', sa.DateTime(), nullable=True),
        sa.Column('verification_notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_qms_clause_evidence_id', 'qms_clause_evidence', ['id'])
    op.create_index('ix_qms_clause_evidence_clause_id', 'qms_clause_evidence', ['clause_id'])


def downgrade() -> None:
    op.drop_table('qms_clause_evidence')
    op.drop_table('qms_clauses')
    op.drop_table('qms_standards')
