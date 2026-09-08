"""Immutable saved-input server runs, guarded lifecycle and append-only checkpoints.

Revision ID: 102_quote_nesting_runs
Revises: 101_quote_nesting_drafts
"""

import sqlalchemy as sa

from alembic import op

revision = '102_quote_nesting_runs'
down_revision = '101_quote_nesting_drafts'
branch_labels = None
depends_on = None


def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _index(name, table, columns, **kwargs):
    if op.get_context().as_sql or name not in {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns, **kwargs)


POSTGRES_DDL = {
    'quote_nesting_runs': (
        """ALTER TABLE quote_nesting_runs ENABLE ROW LEVEL SECURITY""",
        """REVOKE ALL ON TABLE quote_nesting_runs FROM PUBLIC""",
        """REVOKE ALL ON SEQUENCE quote_nesting_runs_id_seq FROM PUBLIC""",
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE quote_nesting_runs FROM anon;
            REVOKE ALL ON SEQUENCE quote_nesting_runs_id_seq FROM anon;
          END IF;
        END $$""",
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE quote_nesting_runs FROM authenticated;
            REVOKE ALL ON SEQUENCE quote_nesting_runs_id_seq FROM authenticated;
          END IF;
        END $$""",
        """CREATE OR REPLACE FUNCTION quote_nest_run_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'Nesting run history is immutable.' USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'QUEUED' OR NEW.version <> 1 OR NEW.completed_count <> 0 OR NEW.evaluated_count <> 0
OR NEW.checkpoint_bytes <> 0 OR NEW.lease_token IS NOT NULL OR NEW.started_at IS NOT NULL OR
NEW.finished_at IS NOT NULL OR NEW.solver_version IS NOT NULL OR NEW.bundle_sha256 IS NOT NULL OR
NEW.node_version IS NOT NULL THEN
      RAISE EXCEPTION 'Nesting runs must begin queued with no results.' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status NOT IN ('QUEUED', 'RUNNING') THEN
    RAISE EXCEPTION 'Terminal nesting runs are immutable.' USING ERRCODE = '23514';
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR
        NEW.company_id IS DISTINCT FROM OLD.company_id OR
        NEW.draft_id IS DISTINCT FROM OLD.draft_id OR
        NEW.revision_id IS DISTINCT FROM OLD.revision_id OR
        NEW.revision_number IS DISTINCT FROM OLD.revision_number OR
        NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256 OR
        NEW.request_key IS DISTINCT FROM OLD.request_key OR
        NEW.request_hash IS DISTINCT FROM OLD.request_hash OR
        NEW.created_by IS DISTINCT FROM OLD.created_by OR
        NEW.submitted_api_token_id IS DISTINCT FROM OLD.submitted_api_token_id OR
        NEW.created_at IS DISTINCT FROM OLD.created_at OR
        NEW.settings_json::text IS DISTINCT FROM OLD.settings_json::text OR
        NEW.release_identity IS DISTINCT FROM OLD.release_identity THEN
    RAISE EXCEPTION 'Nesting run inputs are immutable.' USING ERRCODE = '23514';
  END IF;
  IF NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'Nesting run updates require the next version.' USING ERRCODE = '23514';
  END IF;
  IF (OLD.status = 'QUEUED' AND NEW.status NOT IN ('QUEUED', 'RUNNING', 'CANCELLED', 'FAILED'))
    OR (OLD.status = 'RUNNING' AND NEW.status NOT IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'CANCELLED',
'FAILED')) THEN
    RAISE EXCEPTION 'Invalid nesting run transition.' USING ERRCODE = '23514';
  END IF;
  IF (OLD.lease_token IS NOT NULL AND NEW.lease_token IS DISTINCT FROM OLD.lease_token) OR
        (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at) OR
        (OLD.solver_version IS NOT NULL AND NEW.solver_version IS DISTINCT FROM OLD.solver_version) OR
        (OLD.bundle_sha256 IS NOT NULL AND NEW.bundle_sha256 IS DISTINCT FROM OLD.bundle_sha256) OR
        (OLD.node_version IS NOT NULL AND NEW.node_version IS DISTINCT FROM OLD.node_version) THEN
    RAISE EXCEPTION 'Nesting run execution identity is immutable once assigned.' USING ERRCODE = '23514';
  END IF;
  IF NEW.completed_count < OLD.completed_count OR NEW.evaluated_count < OLD.evaluated_count
    OR NEW.checkpoint_bytes < OLD.checkpoint_bytes OR (OLD.cancel_requested AND NOT NEW.cancel_requested)
THEN
    RAISE EXCEPTION 'Nesting run progress cannot go backwards.' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_run_insert ON quote_nesting_runs""",
        """CREATE TRIGGER tr_quote_nest_run_insert BEFORE INSERT ON quote_nesting_runs FOR EACH ROW EXECUTE FUNCTION
quote_nest_run_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_run_update ON quote_nesting_runs""",
        """CREATE TRIGGER tr_quote_nest_run_update BEFORE UPDATE ON quote_nesting_runs FOR EACH ROW EXECUTE FUNCTION
quote_nest_run_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_run_delete ON quote_nesting_runs""",
        """CREATE TRIGGER tr_quote_nest_run_delete BEFORE DELETE ON quote_nesting_runs FOR EACH ROW EXECUTE FUNCTION
quote_nest_run_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_run_truncate ON quote_nesting_runs""",
        """CREATE TRIGGER tr_quote_nest_run_truncate BEFORE TRUNCATE ON quote_nesting_runs FOR EACH STATEMENT EXECUTE
FUNCTION quote_nest_run_guard()""",
    ),
    'quote_nesting_run_checkpoints': (
        """ALTER TABLE quote_nesting_run_checkpoints ENABLE ROW LEVEL SECURITY""",
        """REVOKE ALL ON TABLE quote_nesting_run_checkpoints FROM PUBLIC""",
        """REVOKE ALL ON SEQUENCE quote_nesting_run_checkpoints_id_seq FROM PUBLIC""",
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE quote_nesting_run_checkpoints FROM anon;
            REVOKE ALL ON SEQUENCE quote_nesting_run_checkpoints_id_seq FROM anon;
          END IF;
        END $$""",
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE quote_nesting_run_checkpoints FROM authenticated;
            REVOKE ALL ON SEQUENCE quote_nesting_run_checkpoints_id_seq FROM authenticated;
          END IF;
        END $$""",
        """CREATE OR REPLACE FUNCTION quote_nest_checkpoint_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = '' AS $$ DECLARE parent_status text; BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'Nesting run checkpoints are immutable.' USING ERRCODE = '23514';
  END IF;
  EXECUTE format('SELECT status FROM %I.quote_nesting_runs WHERE company_id = $1 AND id = $2 AND
lease_token = $3 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO parent_status USING NEW.company_id, NEW.run_id, NEW.lease_token;
  IF parent_status IS NOT NULL AND parent_status <> 'RUNNING' THEN
    RAISE EXCEPTION 'Checkpoint requires a running nesting lease.' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_checkpoint_insert ON quote_nesting_run_checkpoints""",
        """CREATE TRIGGER tr_quote_nest_checkpoint_insert BEFORE INSERT ON quote_nesting_run_checkpoints FOR EACH ROW
EXECUTE FUNCTION quote_nest_checkpoint_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_checkpoint_update ON quote_nesting_run_checkpoints""",
        """CREATE TRIGGER tr_quote_nest_checkpoint_update BEFORE UPDATE ON quote_nesting_run_checkpoints FOR EACH ROW
EXECUTE FUNCTION quote_nest_checkpoint_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_checkpoint_delete ON quote_nesting_run_checkpoints""",
        """CREATE TRIGGER tr_quote_nest_checkpoint_delete BEFORE DELETE ON quote_nesting_run_checkpoints FOR EACH ROW
EXECUTE FUNCTION quote_nest_checkpoint_guard()""",
        """DROP TRIGGER IF EXISTS tr_quote_nest_checkpoint_truncate ON quote_nesting_run_checkpoints""",
        """CREATE TRIGGER tr_quote_nest_checkpoint_truncate BEFORE TRUNCATE ON quote_nesting_run_checkpoints FOR EACH
STATEMENT EXECUTE FUNCTION quote_nest_checkpoint_guard()""",
    ),
}

SQLITE_DDL = {
    'quote_nesting_runs': (
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_run_insert BEFORE INSERT ON quote_nesting_runs BEGIN SELECT
CASE WHEN NEW.status <> 'QUEUED' OR NEW.version <> 1 OR NEW.completed_count <> 0 OR NEW.evaluated_count <>
0 OR NEW.checkpoint_bytes <> 0 OR NEW.lease_token IS NOT NULL OR NEW.started_at IS NOT NULL OR
NEW.finished_at IS NOT NULL OR NEW.solver_version IS NOT NULL OR NEW.bundle_sha256 IS NOT NULL OR
NEW.node_version IS NOT NULL THEN RAISE(ABORT, 'Nesting runs must begin queued with no results.') END; END""",
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_run_update BEFORE UPDATE ON quote_nesting_runs BEGIN
  SELECT CASE WHEN OLD.status NOT IN ('QUEUED', 'RUNNING') THEN RAISE(ABORT, 'Terminal nesting runs are
immutable.') END;
  SELECT CASE WHEN NEW.id IS NOT OLD.id OR NEW.company_id IS NOT OLD.company_id OR NEW.draft_id IS NOT
OLD.draft_id OR NEW.revision_id IS NOT OLD.revision_id OR NEW.revision_number IS NOT OLD.revision_number
OR NEW.input_sha256 IS NOT OLD.input_sha256 OR NEW.request_key IS NOT OLD.request_key OR NEW.request_hash
IS NOT OLD.request_hash OR NEW.created_by IS NOT OLD.created_by OR NEW.submitted_api_token_id IS NOT
OLD.submitted_api_token_id OR NEW.created_at IS NOT OLD.created_at OR NEW.settings_json IS NOT
OLD.settings_json OR NEW.release_identity IS NOT OLD.release_identity THEN RAISE(ABORT, 'Nesting run
inputs are immutable.') END;
  SELECT CASE WHEN NEW.version <> OLD.version + 1 THEN RAISE(ABORT, 'Nesting run updates require the next
version.') END;
  SELECT CASE WHEN (OLD.status = 'QUEUED' AND NEW.status NOT IN ('QUEUED', 'RUNNING', 'CANCELLED',
'FAILED')) OR (OLD.status = 'RUNNING' AND NEW.status NOT IN ('RUNNING', 'COMPLETED', 'PARTIAL',
'CANCELLED', 'FAILED')) THEN RAISE(ABORT, 'Invalid nesting run transition.') END;
  SELECT CASE WHEN (OLD.lease_token IS NOT NULL AND NEW.lease_token IS NOT OLD.lease_token) OR
(OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at) OR (OLD.solver_version IS NOT NULL
AND NEW.solver_version IS NOT OLD.solver_version) OR (OLD.bundle_sha256 IS NOT NULL AND NEW.bundle_sha256
IS NOT OLD.bundle_sha256) OR (OLD.node_version IS NOT NULL AND NEW.node_version IS NOT OLD.node_version)
THEN RAISE(ABORT, 'Nesting run execution identity is immutable once assigned.') END;
  SELECT CASE WHEN NEW.completed_count < OLD.completed_count OR NEW.evaluated_count < OLD.evaluated_count
OR NEW.checkpoint_bytes < OLD.checkpoint_bytes OR (OLD.cancel_requested AND NOT NEW.cancel_requested) THEN
RAISE(ABORT, 'Nesting run progress cannot go backwards.') END;
END""",
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_run_delete BEFORE DELETE ON quote_nesting_runs BEGIN SELECT
RAISE(ABORT, 'Nesting run history is immutable.'); END""",
    ),
    'quote_nesting_run_checkpoints': (
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_checkpoint_insert BEFORE INSERT ON
quote_nesting_run_checkpoints BEGIN SELECT CASE WHEN EXISTS (SELECT 1 FROM quote_nesting_runs WHERE
company_id=NEW.company_id AND id=NEW.run_id AND lease_token=NEW.lease_token AND status <> 'RUNNING') THEN
RAISE(ABORT, 'Checkpoint requires a running nesting lease.') END; END""",
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_checkpoint_update BEFORE UPDATE ON
quote_nesting_run_checkpoints BEGIN SELECT RAISE(ABORT, 'Nesting run checkpoints are immutable.'); END""",
        """CREATE TRIGGER IF NOT EXISTS tr_quote_nest_checkpoint_delete BEFORE DELETE ON
quote_nesting_run_checkpoints BEGIN SELECT RAISE(ABORT, 'Nesting run checkpoints are immutable.'); END""",
    ),
}


def upgrade():
    _index(
        'uq_quote_nest_revision_exact_input',
        'quote_nesting_revisions',
        ['company_id', 'id', 'draft_id', 'revision_number', 'content_sha256'],
        unique=True,
    )
    if not _exists('quote_nesting_runs'):
        op.create_table(
            'quote_nesting_runs',
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('draft_id', sa.Integer, nullable=False),
            sa.Column('revision_id', sa.Integer, nullable=False),
            sa.Column('revision_number', sa.Integer, nullable=False),
            sa.Column('input_sha256', sa.String(64), nullable=False),
            sa.Column('request_key', sa.String(36), nullable=False),
            sa.Column('request_hash', sa.String(64), nullable=False),
            sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
            sa.Column('submitted_api_token_id', sa.Integer, sa.ForeignKey('api_tokens.id'), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='QUEUED'),
            sa.Column('version', sa.Integer, nullable=False, server_default='1'),
            sa.Column('cancel_requested', sa.Boolean, nullable=False, server_default='false'),
            sa.Column('created_at', sa.DateTime, nullable=False),
            sa.Column('updated_at', sa.DateTime, nullable=False),
            sa.Column('started_at', sa.DateTime, nullable=True),
            sa.Column('finished_at', sa.DateTime, nullable=True),
            sa.Column('lease_token', sa.String(36), nullable=True),
            sa.Column('lease_expires_at', sa.DateTime, nullable=True),
            sa.Column('settings_json', sa.JSON, nullable=False),
            sa.Column('release_identity', sa.String(200), nullable=True),
            sa.Column('solver_version', sa.String(100), nullable=True),
            sa.Column('bundle_sha256', sa.String(64), nullable=True),
            sa.Column('node_version', sa.String(100), nullable=True),
            sa.Column('completed_count', sa.Integer, nullable=False, server_default='0'),
            sa.Column('evaluated_count', sa.Integer, nullable=False, server_default='0'),
            sa.Column('checkpoint_bytes', sa.Integer, nullable=False, server_default='0'),
            sa.Column('summary_json', sa.JSON, nullable=True),
            sa.Column('error_code', sa.String(100), nullable=True),
            sa.Column('error_message', sa.String(500), nullable=True),
            sa.ForeignKeyConstraint(
                ['company_id', 'revision_id', 'draft_id', 'revision_number', 'input_sha256'],
                [
                    'quote_nesting_revisions.company_id',
                    'quote_nesting_revisions.id',
                    'quote_nesting_revisions.draft_id',
                    'quote_nesting_revisions.revision_number',
                    'quote_nesting_revisions.content_sha256',
                ],
                name='fk_quote_nest_run_exact_input',
            ),
            sa.UniqueConstraint('company_id', 'id', name='uq_quote_nest_run_company_id'),
            sa.UniqueConstraint('company_id', 'id', 'lease_token', name='uq_quote_nest_run_company_lease'),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_quote_nest_run_request'),
            sa.CheckConstraint(
                "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','CANCELLED','FAILED')",
                name='ck_quote_nest_run_status',
            ),
            sa.CheckConstraint('version >= 1 AND revision_number >= 1', name='ck_quote_nest_run_version'),
            sa.CheckConstraint(
                'length(input_sha256) = 64 AND length(request_hash) = 64 AND length(request_key) = 36',
                name='ck_quote_nest_run_identity',
            ),
            sa.CheckConstraint(
                'completed_count >= 0 AND evaluated_count >= completed_count AND evaluated_count <= 36',
                name='ck_quote_nest_run_counts',
            ),
            sa.CheckConstraint(
                'checkpoint_bytes >= 0 AND checkpoint_bytes <= 25165824', name='ck_quote_nest_run_bytes'
            ),
            sa.CheckConstraint(
                "status <> 'RUNNING' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)",
                name='ck_quote_nest_run_running_lease',
            ),
            sa.CheckConstraint('lease_token IS NULL OR length(lease_token) = 36', name='ck_quote_nest_run_lease_token'),
            sa.CheckConstraint('bundle_sha256 IS NULL OR length(bundle_sha256) = 64', name='ck_quote_nest_run_bundle'),
        )
    if not _exists('quote_nesting_run_checkpoints'):
        op.create_table(
            'quote_nesting_run_checkpoints',
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('run_id', sa.Integer, nullable=False),
            sa.Column('lease_token', sa.String(36), nullable=False),
            sa.Column('sequence', sa.Integer, nullable=False),
            sa.Column('group_id', sa.String(200), nullable=False),
            sa.Column('stock_option_id', sa.String(200), nullable=False),
            sa.Column('result_json', sa.JSON, nullable=False),
            sa.Column('content_sha256', sa.String(64), nullable=False),
            sa.Column('payload_bytes', sa.Integer, nullable=False),
            sa.Column('created_at', sa.DateTime, nullable=False),
            sa.ForeignKeyConstraint(
                ['company_id', 'run_id', 'lease_token'],
                ['quote_nesting_runs.company_id', 'quote_nesting_runs.id', 'quote_nesting_runs.lease_token'],
                name='fk_quote_nest_checkpoint_lease',
            ),
            sa.UniqueConstraint('run_id', 'sequence', name='uq_quote_nest_checkpoint_sequence'),
            sa.UniqueConstraint('run_id', 'group_id', 'stock_option_id', name='uq_quote_nest_checkpoint_option'),
            sa.CheckConstraint('sequence >= 1 AND sequence <= 36', name='ck_quote_nest_checkpoint_sequence'),
            sa.CheckConstraint('payload_bytes > 0 AND payload_bytes <= 8388608', name='ck_quote_nest_checkpoint_bytes'),
            sa.CheckConstraint(
                'length(content_sha256) = 64 AND length(lease_token) = 36', name='ck_quote_nest_checkpoint_identity'
            ),
            sa.CheckConstraint(
                'length(group_id) BETWEEN 1 AND 200 AND length(stock_option_id) BETWEEN 1 AND 200',
                name='ck_quote_nest_checkpoint_keys',
            ),
        )
    _index(
        'ix_quote_nest_run_active_company',
        'quote_nesting_runs',
        ['company_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    _index('ix_quote_nest_run_company_created', 'quote_nesting_runs', ['company_id', 'created_at', 'id'])
    _index('ix_quote_nest_run_company_revision', 'quote_nesting_runs', ['company_id', 'draft_id', 'revision_number'])
    _index('ix_quote_nest_run_status_lease', 'quote_nesting_runs', ['status', 'lease_expires_at'])
    _index('ix_quote_nesting_runs_company_id', 'quote_nesting_runs', ['company_id'])
    _index(
        'ix_quote_nest_checkpoint_company_run', 'quote_nesting_run_checkpoints', ['company_id', 'run_id', 'sequence']
    )
    _index('ix_quote_nesting_run_checkpoints_company_id', 'quote_nesting_run_checkpoints', ['company_id'])
    dialect = op.get_bind().dialect.name
    statements = POSTGRES_DDL if dialect == 'postgresql' else SQLITE_DDL if dialect == 'sqlite' else {}
    for commands in statements.values():
        for command in commands:
            op.execute(command)


def downgrade():
    # Explicit removal of this feature only. Normal application rollback retains
    # these records; a schema downgrade deliberately discards run history.
    for table in ('quote_nesting_run_checkpoints', 'quote_nesting_runs'):
        if op.get_context().as_sql or _exists(table):
            op.drop_table(table)
    if op.get_context().as_sql or (
        'uq_quote_nest_revision_exact_input'
        in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes('quote_nesting_revisions')}
    ):
        op.drop_index('uq_quote_nest_revision_exact_input', table_name='quote_nesting_revisions')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION IF EXISTS quote_nest_checkpoint_guard()')
        op.execute('DROP FUNCTION IF EXISTS quote_nest_run_guard()')
