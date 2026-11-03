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

# Every object is guarded: the fresh bootstrap (create_all -> stamp 058 -> upgrade) runs
# this over a schema the models already built. Every op inside a batch is guarded too: on
# SQLite a FK op makes the batch rebuild the table, and re-adding an existing column there
# is what failed.


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit every statement, as before.
    return op.get_context().as_sql


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col['name'] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix['name'] == index_name for ix in _inspector().get_indexes(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk['name'] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def upgrade():
    offline = _offline()
    add_column = offline or not _has_column('documents', 'previous_revision_id')
    add_fk = offline or not _has_foreign_key('documents', 'fk_documents_previous_revision')
    add_index = offline or not _has_index('documents', 'ix_documents_company_previous_revision')
    if add_column or add_fk or add_index:
        with op.batch_alter_table('documents') as batch:
            if add_column:
                batch.add_column(sa.Column('previous_revision_id', sa.Integer(), nullable=True))
            if add_fk:
                batch.create_foreign_key('fk_documents_previous_revision', 'documents', ['previous_revision_id'], ['id'])
            if add_index:
                batch.create_index('ix_documents_company_previous_revision', ['company_id', 'previous_revision_id'])


def downgrade():
    offline = _offline()
    drop_index = offline or _has_index('documents', 'ix_documents_company_previous_revision')
    drop_fk = offline or _has_foreign_key('documents', 'fk_documents_previous_revision')
    drop_column = offline or _has_column('documents', 'previous_revision_id')
    if drop_index or drop_fk or drop_column:
        with op.batch_alter_table('documents') as batch:
            if drop_index:
                batch.drop_index('ix_documents_company_previous_revision')
            if drop_fk:
                batch.drop_constraint('fk_documents_previous_revision', type_='foreignkey')
            if drop_column:
                batch.drop_column('previous_revision_id')
