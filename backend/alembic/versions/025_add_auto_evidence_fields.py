"""Add auto-evidence fields to qms_clause_evidence

Revision ID: 025
Revises: 024
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '025_add_auto_evidence_fields'
down_revision = '024_add_missing_module_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('qms_clause_evidence', sa.Column('is_auto_linked', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('qms_clause_evidence', sa.Column('auto_link_query', sa.String(255), nullable=True))
    op.add_column('qms_clause_evidence', sa.Column('last_refreshed', sa.DateTime(), nullable=True))
    op.add_column('qms_clause_evidence', sa.Column('live_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('qms_clause_evidence', 'live_count')
    op.drop_column('qms_clause_evidence', 'last_refreshed')
    op.drop_column('qms_clause_evidence', 'auto_link_query')
    op.drop_column('qms_clause_evidence', 'is_auto_linked')
