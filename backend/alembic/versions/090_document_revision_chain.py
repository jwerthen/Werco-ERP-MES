"""Link explicitly uploaded document revisions without changing historical document numbers.

Revision ID: 090_document_revision_chain
Revises: 089_quote_line_conversion
"""

import sqlalchemy as sa

from alembic import op

revision = '090_document_revision_chain'
down_revision = '089_quote_line_conversion'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('previous_revision_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_documents_previous_revision', 'documents', ['previous_revision_id'], ['id'])
        batch.create_index('ix_documents_company_previous_revision', ['company_id', 'previous_revision_id'])


def downgrade():
    with op.batch_alter_table('documents') as batch:
        batch.drop_index('ix_documents_company_previous_revision')
        batch.drop_constraint('fk_documents_previous_revision', type_='foreignkey')
        batch.drop_column('previous_revision_id')
