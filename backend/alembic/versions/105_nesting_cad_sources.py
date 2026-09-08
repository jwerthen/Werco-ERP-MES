"""Track original DXF upload intents, attempts and immutable source bindings.

Revision ID: 105_nesting_cad_sources
Revises: 104_stock_piece_observations
No original files or production records are imported by this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = '105_nesting_cad_sources'
down_revision = '104_stock_piece_observations'
branch_labels = None
depends_on = None

# Frozen copy of model-bootstrap integrity DDL; no runtime application imports.
POSTGRES_DDL = {'quote_nesting_source_intents': ('ALTER TABLE quote_nesting_source_intents ENABLE ROW LEVEL SECURITY',
                                  'REVOKE ALL ON TABLE quote_nesting_source_intents FROM PUBLIC',
                                  'REVOKE ALL ON SEQUENCE quote_nesting_source_intents_id_seq FROM PUBLIC',
                                  "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
                                  'REVOKE ALL ON TABLE quote_nesting_source_intents FROM anon; REVOKE ALL ON '
                                  'SEQUENCE quote_nesting_source_intents_id_seq FROM anon; END IF; END $$',
                                  'DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE '
                                  "rolname='authenticated') THEN REVOKE ALL ON TABLE "
                                  'quote_nesting_source_intents FROM authenticated; REVOKE ALL ON SEQUENCE '
                                  'quote_nesting_source_intents_id_seq FROM authenticated; END IF; END $$',
                                  'CREATE OR REPLACE FUNCTION quote_nesting_source_intents_immutable() '
                                  "RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE "
                                  "EXCEPTION 'Original CAD evidence is immutable.' USING ERRCODE='23514'; "
                                  'RETURN NULL; END; $$',
                                  'DROP TRIGGER IF EXISTS tr_quote_nesting_source_intents_update ON '
                                  'quote_nesting_source_intents',
                                  'CREATE TRIGGER tr_quote_nesting_source_intents_update BEFORE UPDATE ON '
                                  'quote_nesting_source_intents FOR EACH ROW EXECUTE FUNCTION '
                                  'quote_nesting_source_intents_immutable()',
                                  'DROP TRIGGER IF EXISTS tr_quote_nesting_source_intents_delete ON '
                                  'quote_nesting_source_intents',
                                  'CREATE TRIGGER tr_quote_nesting_source_intents_delete BEFORE DELETE ON '
                                  'quote_nesting_source_intents FOR EACH ROW EXECUTE FUNCTION '
                                  'quote_nesting_source_intents_immutable()',
                                  'DROP TRIGGER IF EXISTS tr_quote_nesting_source_intents_truncate ON '
                                  'quote_nesting_source_intents',
                                  'CREATE TRIGGER tr_quote_nesting_source_intents_truncate BEFORE TRUNCATE '
                                  'ON quote_nesting_source_intents FOR EACH STATEMENT EXECUTE FUNCTION '
                                  'quote_nesting_source_intents_immutable()'),
 'quote_nesting_source_attempts': ('ALTER TABLE quote_nesting_source_attempts ENABLE ROW LEVEL SECURITY',
                                   'REVOKE ALL ON TABLE quote_nesting_source_attempts FROM PUBLIC',
                                   'REVOKE ALL ON SEQUENCE quote_nesting_source_attempts_id_seq FROM PUBLIC',
                                   "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
                                   'REVOKE ALL ON TABLE quote_nesting_source_attempts FROM anon; REVOKE ALL '
                                   'ON SEQUENCE quote_nesting_source_attempts_id_seq FROM anon; END IF; END '
                                   '$$',
                                   'DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE '
                                   "rolname='authenticated') THEN REVOKE ALL ON TABLE "
                                   'quote_nesting_source_attempts FROM authenticated; REVOKE ALL ON SEQUENCE '
                                   'quote_nesting_source_attempts_id_seq FROM authenticated; END IF; END $$',
                                   'CREATE OR REPLACE FUNCTION quote_nesting_source_attempts_immutable() '
                                   "RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE "
                                   "EXCEPTION 'Original CAD evidence is immutable.' USING ERRCODE='23514'; "
                                   'RETURN NULL; END; $$',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_attempts_update ON '
                                   'quote_nesting_source_attempts',
                                   'CREATE TRIGGER tr_quote_nesting_source_attempts_update BEFORE UPDATE ON '
                                   'quote_nesting_source_attempts FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_attempts_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_attempts_delete ON '
                                   'quote_nesting_source_attempts',
                                   'CREATE TRIGGER tr_quote_nesting_source_attempts_delete BEFORE DELETE ON '
                                   'quote_nesting_source_attempts FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_attempts_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_attempts_truncate ON '
                                   'quote_nesting_source_attempts',
                                   'CREATE TRIGGER tr_quote_nesting_source_attempts_truncate BEFORE TRUNCATE '
                                   'ON quote_nesting_source_attempts FOR EACH STATEMENT EXECUTE FUNCTION '
                                   'quote_nesting_source_attempts_immutable()'),
 'quote_nesting_source_receipts': ('ALTER TABLE quote_nesting_source_receipts ENABLE ROW LEVEL SECURITY',
                                   'REVOKE ALL ON TABLE quote_nesting_source_receipts FROM PUBLIC',
                                   'REVOKE ALL ON SEQUENCE quote_nesting_source_receipts_id_seq FROM PUBLIC',
                                   "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
                                   'REVOKE ALL ON TABLE quote_nesting_source_receipts FROM anon; REVOKE ALL '
                                   'ON SEQUENCE quote_nesting_source_receipts_id_seq FROM anon; END IF; END '
                                   '$$',
                                   'DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE '
                                   "rolname='authenticated') THEN REVOKE ALL ON TABLE "
                                   'quote_nesting_source_receipts FROM authenticated; REVOKE ALL ON SEQUENCE '
                                   'quote_nesting_source_receipts_id_seq FROM authenticated; END IF; END $$',
                                   'CREATE OR REPLACE FUNCTION quote_nesting_source_receipts_immutable() '
                                   "RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE "
                                   "EXCEPTION 'Original CAD evidence is immutable.' USING ERRCODE='23514'; "
                                   'RETURN NULL; END; $$',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_receipts_update ON '
                                   'quote_nesting_source_receipts',
                                   'CREATE TRIGGER tr_quote_nesting_source_receipts_update BEFORE UPDATE ON '
                                   'quote_nesting_source_receipts FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_receipts_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_receipts_delete ON '
                                   'quote_nesting_source_receipts',
                                   'CREATE TRIGGER tr_quote_nesting_source_receipts_delete BEFORE DELETE ON '
                                   'quote_nesting_source_receipts FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_receipts_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_receipts_truncate ON '
                                   'quote_nesting_source_receipts',
                                   'CREATE TRIGGER tr_quote_nesting_source_receipts_truncate BEFORE TRUNCATE '
                                   'ON quote_nesting_source_receipts FOR EACH STATEMENT EXECUTE FUNCTION '
                                   'quote_nesting_source_receipts_immutable()'),
 'quote_nesting_source_bindings': ('ALTER TABLE quote_nesting_source_bindings ENABLE ROW LEVEL SECURITY',
                                   'REVOKE ALL ON TABLE quote_nesting_source_bindings FROM PUBLIC',
                                   'REVOKE ALL ON SEQUENCE quote_nesting_source_bindings_id_seq FROM PUBLIC',
                                   "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
                                   'REVOKE ALL ON TABLE quote_nesting_source_bindings FROM anon; REVOKE ALL '
                                   'ON SEQUENCE quote_nesting_source_bindings_id_seq FROM anon; END IF; END '
                                   '$$',
                                   'DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE '
                                   "rolname='authenticated') THEN REVOKE ALL ON TABLE "
                                   'quote_nesting_source_bindings FROM authenticated; REVOKE ALL ON SEQUENCE '
                                   'quote_nesting_source_bindings_id_seq FROM authenticated; END IF; END $$',
                                   'CREATE OR REPLACE FUNCTION quote_nesting_source_bindings_immutable() '
                                   "RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE "
                                   "EXCEPTION 'Original CAD evidence is immutable.' USING ERRCODE='23514'; "
                                   'RETURN NULL; END; $$',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_bindings_update ON '
                                   'quote_nesting_source_bindings',
                                   'CREATE TRIGGER tr_quote_nesting_source_bindings_update BEFORE UPDATE ON '
                                   'quote_nesting_source_bindings FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_bindings_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_bindings_delete ON '
                                   'quote_nesting_source_bindings',
                                   'CREATE TRIGGER tr_quote_nesting_source_bindings_delete BEFORE DELETE ON '
                                   'quote_nesting_source_bindings FOR EACH ROW EXECUTE FUNCTION '
                                   'quote_nesting_source_bindings_immutable()',
                                   'DROP TRIGGER IF EXISTS tr_quote_nesting_source_bindings_truncate ON '
                                   'quote_nesting_source_bindings',
                                   'CREATE TRIGGER tr_quote_nesting_source_bindings_truncate BEFORE TRUNCATE '
                                   'ON quote_nesting_source_bindings FOR EACH STATEMENT EXECUTE FUNCTION '
                                   'quote_nesting_source_bindings_immutable()')}

SQLITE_DDL = {'quote_nesting_source_intents': ('CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_intents_update '
                                  'BEFORE UPDATE ON quote_nesting_source_intents BEGIN SELECT RAISE(ABORT, '
                                  "'Original CAD evidence is immutable.'); END",
                                  'CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_intents_delete '
                                  'BEFORE DELETE ON quote_nesting_source_intents BEGIN SELECT RAISE(ABORT, '
                                  "'Original CAD evidence is immutable.'); END"),
 'quote_nesting_source_attempts': ('CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_attempts_update '
                                   'BEFORE UPDATE ON quote_nesting_source_attempts BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END",
                                   'CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_attempts_delete '
                                   'BEFORE DELETE ON quote_nesting_source_attempts BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END"),
 'quote_nesting_source_receipts': ('CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_receipts_update '
                                   'BEFORE UPDATE ON quote_nesting_source_receipts BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END",
                                   'CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_receipts_delete '
                                   'BEFORE DELETE ON quote_nesting_source_receipts BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END"),
 'quote_nesting_source_bindings': ('CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_bindings_update '
                                   'BEFORE UPDATE ON quote_nesting_source_bindings BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END",
                                   'CREATE TRIGGER IF NOT EXISTS tr_quote_nesting_source_bindings_delete '
                                   'BEFORE DELETE ON quote_nesting_source_bindings BEGIN SELECT RAISE(ABORT, '
                                   "'Original CAD evidence is immutable.'); END")}

def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _index(name, table, columns):
    if op.get_context().as_sql or name not in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade():
    if not _exists('quote_nesting_source_intents'):
        op.create_table('quote_nesting_source_intents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('draft_id', sa.Integer(), nullable=False),
        sa.Column('revision_id', sa.Integer(), nullable=False),
        sa.Column('revision_number', sa.Integer(), nullable=False),
        sa.Column('input_sha256', sa.String(length=64), nullable=False),
        sa.Column('source_sha256', sa.String(length=64), nullable=False),
        sa.Column('byte_count', sa.Integer(), nullable=False),
        sa.Column('source_name', sa.String(length=1024), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False),
        sa.Column('targets_json', sa.JSON(), nullable=False),
        sa.Column('targets_sha256', sa.String(length=64), nullable=False),
        sa.Column('target_count', sa.Integer(), nullable=False),
        sa.Column('request_key', sa.String(length=36), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('submitted_api_token_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.CheckConstraint('byte_count > 0 AND byte_count < 5000000', name='ck_nest_source_size'),
        sa.CheckConstraint('length(mime_type) BETWEEN 1 AND 128', name='ck_nest_source_mime'),
        sa.CheckConstraint('length(source_name) BETWEEN 1 AND 1024', name='ck_nest_source_name'),
        sa.CheckConstraint('length(source_sha256)=64 AND length(input_sha256)=64 AND length(targets_sha256)=64 AND length(request_hash)=64 AND length(request_key)=36', name='ck_nest_source_hashes'),
        sa.CheckConstraint('revision_number > 0', name='ck_nest_source_revision'),
        sa.CheckConstraint('target_count BETWEEN 1 AND 1000', name='ck_nest_source_targets'),
        sa.ForeignKeyConstraint(['company_id', 'revision_id', 'draft_id', 'revision_number', 'input_sha256'], ['quote_nesting_revisions.company_id', 'quote_nesting_revisions.id', 'quote_nesting_revisions.draft_id', 'quote_nesting_revisions.revision_number', 'quote_nesting_revisions.content_sha256'], name='fk_nest_source_exact_revision'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['submitted_api_token_id'], ['api_tokens.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'id', 'revision_id', name='uq_nest_source_intent_revision'),
        sa.UniqueConstraint('company_id', 'id', 'source_sha256', 'byte_count', name='uq_nest_source_intent_bytes'),
        sa.UniqueConstraint('company_id', 'id', name='uq_nest_source_tenant_intent'),
        sa.UniqueConstraint('company_id', 'request_key', name='uq_nest_source_request')
        )
    _index('ix_nest_source_revision', 'quote_nesting_source_intents', ['company_id', 'revision_id', 'id'])
    _index('ix_quote_nesting_source_intents_company_id', 'quote_nesting_source_intents', ['company_id'])
    if not _exists('quote_nesting_source_attempts'):
        op.create_table('quote_nesting_source_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intent_id', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('object_key', sa.String(length=36), nullable=False),
        sa.Column('storage_ref', sa.String(length=2048), nullable=False),
        sa.Column('provider_json', sa.JSON(), nullable=False),
        sa.Column('provider_sha256', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('submitted_api_token_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.CheckConstraint('length(object_key)=36 AND length(provider_sha256)=64', name='ck_nest_source_attempt_hashes'),
        sa.CheckConstraint('length(storage_ref) BETWEEN 1 AND 2048', name='ck_nest_source_attempt_reference'),
        sa.CheckConstraint('ordinal BETWEEN 1 AND 8', name='ck_nest_source_attempt_limit'),
        sa.ForeignKeyConstraint(['company_id', 'intent_id'], ['quote_nesting_source_intents.company_id', 'quote_nesting_source_intents.id'], name='fk_nest_source_attempt_intent'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['submitted_api_token_id'], ['api_tokens.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'intent_id', 'id', name='uq_nest_source_attempt_identity'),
        sa.UniqueConstraint('company_id', 'intent_id', 'ordinal', name='uq_nest_source_attempt_ordinal'),
        sa.UniqueConstraint('object_key', name='uq_nest_source_object_key')
        )
    _index('ix_nest_source_attempt_intent', 'quote_nesting_source_attempts', ['company_id', 'intent_id', 'id'])
    _index('ix_quote_nesting_source_attempts_company_id', 'quote_nesting_source_attempts', ['company_id'])
    if not _exists('quote_nesting_source_receipts'):
        op.create_table('quote_nesting_source_receipts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intent_id', sa.Integer(), nullable=False),
        sa.Column('attempt_id', sa.Integer(), nullable=False),
        sa.Column('source_sha256', sa.String(length=64), nullable=False),
        sa.Column('byte_count', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('submitted_api_token_id', sa.Integer(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.CheckConstraint('byte_count > 0 AND byte_count < 5000000 AND length(source_sha256)=64', name='ck_nest_source_receipt_bytes'),
        sa.ForeignKeyConstraint(['company_id', 'intent_id', 'attempt_id'], ['quote_nesting_source_attempts.company_id', 'quote_nesting_source_attempts.intent_id', 'quote_nesting_source_attempts.id'], name='fk_nest_source_receipt_attempt'),
        sa.ForeignKeyConstraint(['company_id', 'intent_id', 'source_sha256', 'byte_count'], ['quote_nesting_source_intents.company_id', 'quote_nesting_source_intents.id', 'quote_nesting_source_intents.source_sha256', 'quote_nesting_source_intents.byte_count'], name='fk_nest_source_receipt_bytes'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['submitted_api_token_id'], ['api_tokens.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'id', 'intent_id', name='uq_nest_source_receipt_identity'),
        sa.UniqueConstraint('company_id', 'intent_id', name='uq_nest_source_receipt_intent')
        )
    _index('ix_quote_nesting_source_receipts_company_id', 'quote_nesting_source_receipts', ['company_id'])
    if not _exists('quote_nesting_source_bindings'):
        op.create_table('quote_nesting_source_bindings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('receipt_id', sa.Integer(), nullable=False),
        sa.Column('intent_id', sa.Integer(), nullable=False),
        sa.Column('revision_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.String(length=200), nullable=False),
        sa.Column('part_id', sa.String(length=200), nullable=False),
        sa.Column('provenance_json', sa.JSON(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.CheckConstraint('length(group_id) BETWEEN 1 AND 200 AND length(part_id) BETWEEN 1 AND 200', name='ck_nest_source_binding_target'),
        sa.ForeignKeyConstraint(['company_id', 'intent_id', 'revision_id'], ['quote_nesting_source_intents.company_id', 'quote_nesting_source_intents.id', 'quote_nesting_source_intents.revision_id'], name='fk_nest_source_binding_revision'),
        sa.ForeignKeyConstraint(['company_id', 'receipt_id', 'intent_id'], ['quote_nesting_source_receipts.company_id', 'quote_nesting_source_receipts.id', 'quote_nesting_source_receipts.intent_id'], name='fk_nest_source_binding_receipt'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'revision_id', 'group_id', 'part_id', name='uq_nest_source_revision_part')
        )
    _index('ix_nest_source_binding_receipt', 'quote_nesting_source_bindings', ['company_id', 'receipt_id'])
    _index('ix_quote_nesting_source_bindings_company_id', 'quote_nesting_source_bindings', ['company_id'])
    statements = POSTGRES_DDL if op.get_bind().dialect.name == 'postgresql' else SQLITE_DDL
    for table, rows in statements.items():
        for statement in rows:
            op.execute(statement)


def downgrade():
    # Destructive test/explicit schema rollback only: application rollback must retain source history.
    for table in reversed(tuple(SQLITE_DDL)):
        if op.get_context().as_sql or _exists(table):
            op.drop_table(table)
        if op.get_bind().dialect.name == 'postgresql':
            op.execute(f'DROP FUNCTION IF EXISTS {table}_immutable()')
