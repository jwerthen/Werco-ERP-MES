"""Company-scoped unapproved nesting drafts and immutable revisions.

Revision ID: 101_quote_nesting_drafts
Revises: 100_receiving_supplier_followup
"""

import sqlalchemy as sa

from alembic import op

revision = '101_quote_nesting_drafts'
down_revision = '100_receiving_supplier_followup'
branch_labels = None
depends_on = None


def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _index(name, table, columns):
    if op.get_context().as_sql or name not in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade():
    if not _exists('quote_nesting_drafts'):
        op.create_table(
            'quote_nesting_drafts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('latest_revision_number', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('company_id', 'id', name='uq_quote_nest_draft_company_id'),
            sa.CheckConstraint("status = 'DRAFT'", name='ck_quote_nest_draft_unapproved'),
            sa.CheckConstraint('version >= 1 AND version = latest_revision_number', name='ck_quote_nest_draft_version'),
            sa.CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_quote_nest_draft_name'),
        )
    _index('ix_quote_nesting_drafts_company_id', 'quote_nesting_drafts', ['company_id'])
    _index('ix_quote_nest_draft_company_updated', 'quote_nesting_drafts', ['company_id', 'updated_at', 'id'])
    if not _exists('quote_nesting_revisions'):
        op.create_table(
            'quote_nesting_revisions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('draft_id', sa.Integer(), nullable=False),
            sa.Column('revision_number', sa.Integer(), nullable=False),
            sa.Column('draft_version', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('estimate_json', sa.JSON(), nullable=False),
            sa.Column('content_sha256', sa.String(64), nullable=False),
            sa.Column('payload_schema_version', sa.Integer(), nullable=False),
            sa.Column('payload_bytes', sa.Integer(), nullable=False),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('request_key', sa.String(36), nullable=False),
            sa.Column('request_hash', sa.String(64), nullable=False),
            sa.Column('review_issues_json', sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(
                ['company_id', 'draft_id'], ['quote_nesting_drafts.company_id', 'quote_nesting_drafts.id'],
                name='fk_quote_nest_revision_tenant_draft',
            ),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_quote_nest_revision_request'),
            sa.UniqueConstraint('draft_id', 'revision_number', name='uq_quote_nest_revision_number'),
            sa.CheckConstraint('revision_number >= 1 AND draft_version = revision_number', name='ck_quote_nest_revision_version'),
            sa.CheckConstraint('payload_bytes > 0 AND payload_schema_version > 0', name='ck_quote_nest_revision_payload'),
            sa.CheckConstraint('length(content_sha256) = 64 AND length(request_hash) = 64', name='ck_quote_nest_revision_hashes'),
            sa.CheckConstraint('length(request_key) = 36', name='ck_quote_nest_revision_request_key'),
            sa.CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_quote_nest_revision_name'),
        )
    _index('ix_quote_nesting_revisions_company_id', 'quote_nesting_revisions', ['company_id'])
    _index('ix_quote_nest_revision_company_draft', 'quote_nesting_revisions', ['company_id', 'draft_id', 'revision_number'])
    dialect = op.get_bind().dialect.name
    if dialect == 'postgresql':
        for table in ('quote_nesting_drafts', 'quote_nesting_revisions'):
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
            op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')
            op.execute(f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC')
            for role in ('anon', 'authenticated'):
                op.execute(f"""DO $$ BEGIN
                  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON TABLE {table} FROM {role};
                    REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role};
                  END IF;
                END $$""")
        op.execute("""CREATE OR REPLACE FUNCTION quote_nest_revision_immutable() RETURNS TRIGGER
          LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN
            RAISE EXCEPTION 'Nesting draft revisions are immutable; append a new revision.' USING ERRCODE = '23514';
            RETURN NULL;
          END; $$""")
        for operation in ('UPDATE', 'DELETE', 'TRUNCATE'):
            trigger = f'tr_quote_nest_revision_no_{operation.lower()}'
            level = 'STATEMENT' if operation == 'TRUNCATE' else 'ROW'
            op.execute(f'DROP TRIGGER IF EXISTS {trigger} ON quote_nesting_revisions')
            op.execute(
                f'CREATE TRIGGER {trigger} BEFORE {operation} ON quote_nesting_revisions '
                f'FOR EACH {level} EXECUTE FUNCTION quote_nest_revision_immutable()'
            )
    elif dialect == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            op.execute(
                f'CREATE TRIGGER IF NOT EXISTS tr_quote_nest_revision_no_{operation.lower()} '
                f'BEFORE {operation} ON quote_nesting_revisions '
                "BEGIN SELECT RAISE(ABORT, 'Nesting draft revisions are immutable; append a new revision.'); END"
            )


def downgrade():
    # A downgrade deliberately removes this new feature and its saved drafts;
    # it never updates or deletes unrelated ERP records.
    for table in ('quote_nesting_revisions', 'quote_nesting_drafts'):
        if op.get_context().as_sql or _exists(table):
            op.drop_table(table)
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION IF EXISTS quote_nest_revision_immutable()')
