"""Append-only company quote-spacing revisions, approvals and withdrawals.

Revision ID: 103_quote_nesting_spacing_policies
Revises: 102_quote_nesting_runs
No records are seeded and no historical nesting input is rewritten.
"""

import sqlalchemy as sa

from alembic import op

revision = '103_quote_nesting_spacing_policies'
down_revision = '102_quote_nesting_runs'
branch_labels = None
depends_on = None


def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _index(name, table, columns, **kwargs):
    if op.get_context().as_sql or name not in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns, **kwargs)


POSTGRES_DDL = {
    'quote_nesting_spacing_policies': (
        """ALTER TABLE quote_nesting_spacing_policies ENABLE ROW LEVEL SECURITY""",
        """REVOKE ALL ON TABLE quote_nesting_spacing_policies FROM PUBLIC""",
        """REVOKE ALL ON SEQUENCE quote_nesting_spacing_policies_id_seq FROM PUBLIC""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_policies FROM anon;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_policies_id_seq FROM anon;
 END IF; END $$""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_policies FROM authenticated;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_policies_id_seq FROM authenticated;
 END IF; END $$""",
        """CREATE OR REPLACE FUNCTION nest_spacing_policy_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = '' AS $$  BEGIN
IF TG_OP IN ('DELETE','TRUNCATE') THEN
 RAISE EXCEPTION 'Spacing policy history is immutable.' USING ERRCODE='23514';
END IF;
IF TG_OP='INSERT' THEN
 IF NEW.version <> 0 OR NEW.latest_revision_number <> 0 THEN
  RAISE EXCEPTION 'New spacing policies must begin empty.' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END IF;
IF NEW.id IS DISTINCT FROM OLD.id OR NEW.company_id IS DISTINCT FROM OLD.company_id OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
 RAISE EXCEPTION 'Spacing policy identity is immutable.' USING ERRCODE='23514';
END IF;
IF NEW.version <> OLD.version+1 OR NEW.latest_revision_number < OLD.latest_revision_number
 OR NEW.latest_revision_number > OLD.latest_revision_number+1 THEN
 RAISE EXCEPTION 'Spacing policy updates require the next version.' USING ERRCODE='23514';
END IF;
RETURN NEW;
END; $$""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_policy_guard_insert ON quote_nesting_spacing_policies""",
        """CREATE TRIGGER tr_nest_spacing_policy_guard_insert BEFORE INSERT ON quote_nesting_spacing_policies FOR EACH ROW EXECUTE FUNCTION nest_spacing_policy_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_policy_guard_update ON quote_nesting_spacing_policies""",
        """CREATE TRIGGER tr_nest_spacing_policy_guard_update BEFORE UPDATE ON quote_nesting_spacing_policies FOR EACH ROW EXECUTE FUNCTION nest_spacing_policy_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_policy_guard_delete ON quote_nesting_spacing_policies""",
        """CREATE TRIGGER tr_nest_spacing_policy_guard_delete BEFORE DELETE ON quote_nesting_spacing_policies FOR EACH ROW EXECUTE FUNCTION nest_spacing_policy_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_policy_guard_truncate ON quote_nesting_spacing_policies""",
        """CREATE TRIGGER tr_nest_spacing_policy_guard_truncate BEFORE TRUNCATE ON quote_nesting_spacing_policies FOR EACH STATEMENT EXECUTE FUNCTION nest_spacing_policy_guard()""",
    ),
    'quote_nesting_spacing_revisions': (
        """ALTER TABLE quote_nesting_spacing_revisions ENABLE ROW LEVEL SECURITY""",
        """REVOKE ALL ON TABLE quote_nesting_spacing_revisions FROM PUBLIC""",
        """REVOKE ALL ON SEQUENCE quote_nesting_spacing_revisions_id_seq FROM PUBLIC""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_revisions FROM anon;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_revisions_id_seq FROM anon;
 END IF; END $$""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_revisions FROM authenticated;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_revisions_id_seq FROM authenticated;
 END IF; END $$""",
        """CREATE OR REPLACE FUNCTION nest_spacing_revision_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = '' AS $$ DECLARE parent_revision integer; BEGIN
IF TG_OP <> 'INSERT' THEN
 RAISE EXCEPTION 'Spacing policy revisions are immutable.' USING ERRCODE='23514';
END IF;
EXECUTE format('SELECT latest_revision_number FROM %I.quote_nesting_spacing_policies
 WHERE company_id=$1 AND id=$2 FOR UPDATE', TG_TABLE_SCHEMA)
 INTO parent_revision USING NEW.company_id, NEW.policy_id;
IF parent_revision IS NOT NULL AND parent_revision <> NEW.revision_number THEN
 RAISE EXCEPTION 'Revision requires the current policy revision counter.' USING ERRCODE='23514';
END IF;
RETURN NEW;
END; $$""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_revision_guard_insert ON quote_nesting_spacing_revisions""",
        """CREATE TRIGGER tr_nest_spacing_revision_guard_insert BEFORE INSERT ON quote_nesting_spacing_revisions FOR EACH ROW EXECUTE FUNCTION nest_spacing_revision_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_revision_guard_update ON quote_nesting_spacing_revisions""",
        """CREATE TRIGGER tr_nest_spacing_revision_guard_update BEFORE UPDATE ON quote_nesting_spacing_revisions FOR EACH ROW EXECUTE FUNCTION nest_spacing_revision_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_revision_guard_delete ON quote_nesting_spacing_revisions""",
        """CREATE TRIGGER tr_nest_spacing_revision_guard_delete BEFORE DELETE ON quote_nesting_spacing_revisions FOR EACH ROW EXECUTE FUNCTION nest_spacing_revision_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_revision_guard_truncate ON quote_nesting_spacing_revisions""",
        """CREATE TRIGGER tr_nest_spacing_revision_guard_truncate BEFORE TRUNCATE ON quote_nesting_spacing_revisions FOR EACH STATEMENT EXECUTE FUNCTION nest_spacing_revision_guard()""",
    ),
    'quote_nesting_spacing_events': (
        """ALTER TABLE quote_nesting_spacing_events ENABLE ROW LEVEL SECURITY""",
        """REVOKE ALL ON TABLE quote_nesting_spacing_events FROM PUBLIC""",
        """REVOKE ALL ON SEQUENCE quote_nesting_spacing_events_id_seq FROM PUBLIC""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_events FROM anon;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_events_id_seq FROM anon;
 END IF; END $$""",
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
 REVOKE ALL ON TABLE quote_nesting_spacing_events FROM authenticated;
 REVOKE ALL ON SEQUENCE quote_nesting_spacing_events_id_seq FROM authenticated;
 END IF; END $$""",
        """CREATE OR REPLACE FUNCTION nest_spacing_event_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = '' AS $$ DECLARE parent_version integer; parent_revision integer; prior_kind text; BEGIN
IF TG_OP <> 'INSERT' THEN
 RAISE EXCEPTION 'Spacing policy events are immutable.' USING ERRCODE='23514';
END IF;
EXECUTE format('SELECT version, latest_revision_number FROM %I.quote_nesting_spacing_policies
 WHERE company_id=$1 AND id=$2 FOR UPDATE', TG_TABLE_SCHEMA)
 INTO parent_version,parent_revision USING NEW.company_id,NEW.policy_id;
IF parent_version IS NOT NULL AND (parent_version <> NEW.policy_version OR
 (NEW.kind='REVISION_CREATED' AND parent_revision <> NEW.revision_number)) THEN
 RAISE EXCEPTION 'Event requires the current policy version.' USING ERRCODE='23514';
END IF;
IF NEW.kind='PUBLISHED' THEN
 EXECUTE format('SELECT kind FROM %I.quote_nesting_spacing_events WHERE company_id=$1
 AND policy_id=$2 AND revision_id=$3 AND kind=''REVISION_CREATED''', TG_TABLE_SCHEMA)
 INTO prior_kind USING NEW.company_id,NEW.policy_id,NEW.revision_id;
 IF prior_kind IS NULL THEN
  RAISE EXCEPTION 'Publication requires a recorded revision.' USING ERRCODE='23514';
 END IF;
END IF;
IF NEW.kind='WITHDRAWN' THEN
 EXECUTE format('SELECT kind FROM %I.quote_nesting_spacing_events WHERE company_id=$1
 AND policy_id=$2 AND id=$3', TG_TABLE_SCHEMA)
 INTO prior_kind USING NEW.company_id,NEW.policy_id,NEW.publication_id;
 IF prior_kind IS NOT NULL AND prior_kind <> 'PUBLISHED' THEN
  RAISE EXCEPTION 'Withdrawal must reference a publication.' USING ERRCODE='23514';
 END IF;
END IF;
RETURN NEW;
END; $$""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_event_guard_insert ON quote_nesting_spacing_events""",
        """CREATE TRIGGER tr_nest_spacing_event_guard_insert BEFORE INSERT ON quote_nesting_spacing_events FOR EACH ROW EXECUTE FUNCTION nest_spacing_event_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_event_guard_update ON quote_nesting_spacing_events""",
        """CREATE TRIGGER tr_nest_spacing_event_guard_update BEFORE UPDATE ON quote_nesting_spacing_events FOR EACH ROW EXECUTE FUNCTION nest_spacing_event_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_event_guard_delete ON quote_nesting_spacing_events""",
        """CREATE TRIGGER tr_nest_spacing_event_guard_delete BEFORE DELETE ON quote_nesting_spacing_events FOR EACH ROW EXECUTE FUNCTION nest_spacing_event_guard()""",
        """DROP TRIGGER IF EXISTS tr_nest_spacing_event_guard_truncate ON quote_nesting_spacing_events""",
        """CREATE TRIGGER tr_nest_spacing_event_guard_truncate BEFORE TRUNCATE ON quote_nesting_spacing_events FOR EACH STATEMENT EXECUTE FUNCTION nest_spacing_event_guard()""",
    ),
}

SQLITE_DDL = {
    'quote_nesting_spacing_policies': (
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_policy_guard_insert BEFORE INSERT ON quote_nesting_spacing_policies BEGIN
SELECT CASE WHEN NEW.version <> 0 OR NEW.latest_revision_number <> 0 THEN RAISE(ABORT,'New spacing policies must begin empty.') END;
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_policy_guard_update BEFORE UPDATE ON quote_nesting_spacing_policies BEGIN
SELECT CASE WHEN NEW.id IS NOT OLD.id OR NEW.company_id IS NOT OLD.company_id OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at THEN RAISE(ABORT,'Spacing policy identity is immutable.') END;
SELECT CASE WHEN NEW.version <> OLD.version+1 OR NEW.latest_revision_number < OLD.latest_revision_number OR NEW.latest_revision_number > OLD.latest_revision_number+1 THEN RAISE(ABORT,'Spacing policy updates require the next version.') END;
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_policy_guard_delete BEFORE DELETE ON quote_nesting_spacing_policies BEGIN
SELECT RAISE(ABORT,'Spacing policy history is immutable.');
END""",
    ),
    'quote_nesting_spacing_revisions': (
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_revision_guard_insert BEFORE INSERT ON quote_nesting_spacing_revisions BEGIN
SELECT CASE WHEN EXISTS(SELECT 1 FROM quote_nesting_spacing_policies WHERE company_id=NEW.company_id AND id=NEW.policy_id AND latest_revision_number <> NEW.revision_number) THEN RAISE(ABORT,'Revision requires the current policy revision counter.') END;
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_revision_guard_update BEFORE UPDATE ON quote_nesting_spacing_revisions BEGIN
SELECT RAISE(ABORT,'Spacing policy revisions are immutable.');
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_revision_guard_delete BEFORE DELETE ON quote_nesting_spacing_revisions BEGIN
SELECT RAISE(ABORT,'Spacing policy history is immutable.');
END""",
    ),
    'quote_nesting_spacing_events': (
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_event_guard_insert BEFORE INSERT ON quote_nesting_spacing_events BEGIN
SELECT CASE WHEN EXISTS(SELECT 1 FROM quote_nesting_spacing_policies WHERE company_id=NEW.company_id AND id=NEW.policy_id AND (version <> NEW.policy_version OR (NEW.kind='REVISION_CREATED' AND latest_revision_number <> NEW.revision_number))) THEN RAISE(ABORT,'Event requires the current policy version.') END;
SELECT CASE WHEN NEW.kind='PUBLISHED' AND NOT EXISTS(SELECT 1 FROM quote_nesting_spacing_events WHERE company_id=NEW.company_id AND policy_id=NEW.policy_id AND revision_id=NEW.revision_id AND kind='REVISION_CREATED') THEN RAISE(ABORT,'Publication requires a recorded revision.') END;
SELECT CASE WHEN NEW.kind='WITHDRAWN' AND EXISTS(SELECT 1 FROM quote_nesting_spacing_events WHERE company_id=NEW.company_id AND policy_id=NEW.policy_id AND id=NEW.publication_id AND kind <> 'PUBLISHED') THEN RAISE(ABORT,'Withdrawal must reference a publication.') END;
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_event_guard_update BEFORE UPDATE ON quote_nesting_spacing_events BEGIN
SELECT RAISE(ABORT,'Spacing policy events are immutable.');
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_nest_spacing_event_guard_delete BEFORE DELETE ON quote_nesting_spacing_events BEGIN
SELECT RAISE(ABORT,'Spacing policy history is immutable.');
END""",
    ),
}


def upgrade():
    if not _exists('quote_nesting_spacing_policies'):
        op.create_table(
            'quote_nesting_spacing_policies',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('latest_revision_number', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.CheckConstraint(
                'version >= 0 AND latest_revision_number >= 0 AND version >= latest_revision_number',
                name='ck_nest_spacing_policy_version',
            ),
            sa.UniqueConstraint('company_id', name='uq_nest_spacing_policy_company'),
            sa.UniqueConstraint('company_id', 'id', name='uq_nest_spacing_policy_tenant'),
        )
    _index('ix_quote_nesting_spacing_policies_company_id', 'quote_nesting_spacing_policies', ['company_id'])
    if not _exists('quote_nesting_spacing_revisions'):
        op.create_table(
            'quote_nesting_spacing_revisions',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('policy_id', sa.Integer(), nullable=False),
            sa.Column('revision_number', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('content_json', sa.JSON(), nullable=False),
            sa.Column('content_sha256', sa.String(64), nullable=False),
            sa.Column('payload_schema_version', sa.Integer(), nullable=False),
            sa.Column('payload_bytes', sa.Integer(), nullable=False),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.CheckConstraint('length(content_sha256) = 64', name='ck_nest_spacing_revision_hash'),
            sa.CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_nest_spacing_revision_name'),
            sa.CheckConstraint('revision_number >= 1', name='ck_nest_spacing_revision_number'),
            sa.CheckConstraint(
                'payload_schema_version >= 1 AND payload_bytes BETWEEN 1 AND 65536',
                name='ck_nest_spacing_revision_payload',
            ),
            sa.ForeignKeyConstraint(
                ['company_id', 'policy_id'],
                ['quote_nesting_spacing_policies.company_id', 'quote_nesting_spacing_policies.id'],
                name='fk_nest_spacing_revision_policy',
            ),
            sa.UniqueConstraint(
                'company_id',
                'policy_id',
                'id',
                'revision_number',
                'content_sha256',
                name='uq_nest_spacing_revision_exact',
            ),
            sa.UniqueConstraint('policy_id', 'revision_number', name='uq_nest_spacing_revision_number'),
        )
    _index(
        'ix_nest_spacing_revision_company_policy',
        'quote_nesting_spacing_revisions',
        ['company_id', 'policy_id', 'revision_number'],
    )
    _index('ix_quote_nesting_spacing_revisions_company_id', 'quote_nesting_spacing_revisions', ['company_id'])
    if not _exists('quote_nesting_spacing_events'):
        op.create_table(
            'quote_nesting_spacing_events',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('policy_id', sa.Integer(), nullable=False),
            sa.Column('policy_version', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(20), nullable=False),
            sa.Column('revision_id', sa.Integer(), nullable=False),
            sa.Column('revision_number', sa.Integer(), nullable=False),
            sa.Column('content_sha256', sa.String(64), nullable=False),
            sa.Column('publication_id', sa.Integer()),
            sa.Column('effective_at', sa.DateTime()),
            sa.Column('reason', sa.String(1000), nullable=False),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('submitted_api_token_id', sa.Integer()),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('request_key', sa.String(36), nullable=False),
            sa.Column('request_hash', sa.String(64), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
            sa.ForeignKeyConstraint(['submitted_api_token_id'], ['api_tokens.id']),
            sa.CheckConstraint(
                'length(content_sha256) = 64 AND length(request_hash) = 64 AND length(request_key) = 36',
                name='ck_nest_spacing_event_identity',
            ),
            sa.CheckConstraint(
                "kind IN ('REVISION_CREATED', 'PUBLISHED', 'WITHDRAWN')", name='ck_nest_spacing_event_kind'
            ),
            sa.CheckConstraint('length(trim(reason)) BETWEEN 1 AND 1000', name='ck_nest_spacing_event_reason'),
            sa.CheckConstraint(
                "(kind = 'PUBLISHED' AND effective_at IS NOT NULL AND effective_at >= created_at AND publication_id IS NULL) OR (kind = 'REVISION_CREATED' AND effective_at IS NULL AND publication_id IS NULL) OR (kind = 'WITHDRAWN' AND effective_at IS NULL AND publication_id IS NOT NULL)",
                name='ck_nest_spacing_event_shape',
            ),
            sa.CheckConstraint('policy_version >= 1 AND revision_number >= 1', name='ck_nest_spacing_event_version'),
            sa.ForeignKeyConstraint(
                ['company_id', 'policy_id', 'revision_id', 'revision_number', 'content_sha256'],
                [
                    'quote_nesting_spacing_revisions.company_id',
                    'quote_nesting_spacing_revisions.policy_id',
                    'quote_nesting_spacing_revisions.id',
                    'quote_nesting_spacing_revisions.revision_number',
                    'quote_nesting_spacing_revisions.content_sha256',
                ],
                name='fk_nest_spacing_event_revision',
            ),
            sa.ForeignKeyConstraint(
                ['company_id', 'policy_id', 'publication_id', 'revision_id', 'revision_number', 'content_sha256'],
                [
                    'quote_nesting_spacing_events.company_id',
                    'quote_nesting_spacing_events.policy_id',
                    'quote_nesting_spacing_events.id',
                    'quote_nesting_spacing_events.revision_id',
                    'quote_nesting_spacing_events.revision_number',
                    'quote_nesting_spacing_events.content_sha256',
                ],
                name='fk_nest_spacing_withdrawal_publication',
            ),
            sa.UniqueConstraint(
                'company_id',
                'policy_id',
                'id',
                'revision_id',
                'revision_number',
                'content_sha256',
                name='uq_nest_spacing_event_exact',
            ),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_nest_spacing_event_request'),
            sa.UniqueConstraint('policy_id', 'policy_version', name='uq_nest_spacing_event_version'),
        )
    _index(
        'ix_nest_spacing_event_company_policy',
        'quote_nesting_spacing_events',
        ['company_id', 'policy_id', 'policy_version'],
    )
    _index('ix_quote_nesting_spacing_events_company_id', 'quote_nesting_spacing_events', ['company_id'])
    _index(
        'uq_nest_spacing_publication_date',
        'quote_nesting_spacing_events',
        ['policy_id', 'effective_at'],
        unique=True,
        postgresql_where=sa.text("kind = 'PUBLISHED'"),
        sqlite_where=sa.text("kind = 'PUBLISHED'"),
    )
    _index(
        'uq_nest_spacing_revision_event',
        'quote_nesting_spacing_events',
        ['policy_id', 'revision_id'],
        unique=True,
        postgresql_where=sa.text("kind = 'REVISION_CREATED'"),
        sqlite_where=sa.text("kind = 'REVISION_CREATED'"),
    )
    _index(
        'uq_nest_spacing_withdrawal',
        'quote_nesting_spacing_events',
        ['publication_id'],
        unique=True,
        postgresql_where=sa.text("kind = 'WITHDRAWN'"),
        sqlite_where=sa.text("kind = 'WITHDRAWN'"),
    )
    statements = POSTGRES_DDL if op.get_bind().dialect.name == 'postgresql' else SQLITE_DDL
    for table_statements in statements.values():
        for statement in table_statements:
            op.execute(statement)


def downgrade():
    if op.get_context().as_sql or _exists('quote_nesting_spacing_events'):
        op.drop_table('quote_nesting_spacing_events')
    if op.get_context().as_sql or _exists('quote_nesting_spacing_revisions'):
        op.drop_table('quote_nesting_spacing_revisions')
    if op.get_context().as_sql or _exists('quote_nesting_spacing_policies'):
        op.drop_table('quote_nesting_spacing_policies')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION IF EXISTS nest_spacing_event_guard()')
        op.execute('DROP FUNCTION IF EXISTS nest_spacing_revision_guard()')
        op.execute('DROP FUNCTION IF EXISTS nest_spacing_policy_guard()')
