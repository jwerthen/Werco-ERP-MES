"""Server-calculated, unapproved run records and immutable option checkpoints."""

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    inspect,
    text,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class QuoteNestingRun(Base, TenantMixin):
    __tablename__ = 'quote_nesting_runs'
    __table_args__ = (
        ForeignKeyConstraint(
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
        UniqueConstraint('company_id', 'id', name='uq_quote_nest_run_company_id'),
        UniqueConstraint('company_id', 'id', 'lease_token', name='uq_quote_nest_run_company_lease'),
        UniqueConstraint('company_id', 'request_key', name='uq_quote_nest_run_request'),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','CANCELLED','FAILED')", name='ck_quote_nest_run_status'
        ),
        CheckConstraint('version >= 1 AND revision_number >= 1', name='ck_quote_nest_run_version'),
        CheckConstraint(
            'length(input_sha256) = 64 AND length(request_hash) = 64 AND length(request_key) = 36',
            name='ck_quote_nest_run_identity',
        ),
        CheckConstraint(
            'completed_count >= 0 AND evaluated_count >= completed_count AND evaluated_count <= 36',
            name='ck_quote_nest_run_counts',
        ),
        CheckConstraint('checkpoint_bytes >= 0 AND checkpoint_bytes <= 25165824', name='ck_quote_nest_run_bytes'),
        CheckConstraint(
            "status <> 'RUNNING' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            'AND started_at IS NOT NULL)',
            name='ck_quote_nest_run_running_lease',
        ),
        CheckConstraint('lease_token IS NULL OR length(lease_token) = 36', name='ck_quote_nest_run_lease_token'),
        CheckConstraint('bundle_sha256 IS NULL OR length(bundle_sha256) = 64', name='ck_quote_nest_run_bundle'),
        Index(
            'ix_quote_nest_run_active_company',
            'company_id',
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
            sqlite_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index('ix_quote_nest_run_company_created', 'company_id', 'created_at', 'id'),
        Index('ix_quote_nest_run_company_revision', 'company_id', 'draft_id', 'revision_number'),
        Index('ix_quote_nest_run_status_lease', 'status', 'lease_expires_at'),
    )
    id = Column(Integer, primary_key=True)
    draft_id = Column(Integer, nullable=False)
    revision_id = Column(Integer, nullable=False)
    revision_number = Column(Integer, nullable=False)
    input_sha256 = Column(String(64), nullable=False)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    submitted_api_token_id = Column(Integer, ForeignKey('api_tokens.id'), nullable=True)
    status = Column(String(20), nullable=False, default='QUEUED', server_default='QUEUED')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    cancel_requested = Column(Boolean, nullable=False, default=False, server_default='false')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    lease_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    settings_json = Column(JSON, nullable=False)
    release_identity = Column(String(200), nullable=True)
    solver_version = Column(String(100), nullable=True)
    bundle_sha256 = Column(String(64), nullable=True)
    node_version = Column(String(100), nullable=True)
    completed_count = Column(Integer, nullable=False, default=0, server_default='0')
    evaluated_count = Column(Integer, nullable=False, default=0, server_default='0')
    checkpoint_bytes = Column(Integer, nullable=False, default=0, server_default='0')
    summary_json = Column(JSON, nullable=True)
    error_code = Column(String(100), nullable=True)
    error_message = Column(String(500), nullable=True)


class QuoteNestingRunCheckpoint(Base, TenantMixin):
    __tablename__ = 'quote_nesting_run_checkpoints'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'run_id', 'lease_token'],
            ['quote_nesting_runs.company_id', 'quote_nesting_runs.id', 'quote_nesting_runs.lease_token'],
            name='fk_quote_nest_checkpoint_lease',
        ),
        UniqueConstraint('run_id', 'sequence', name='uq_quote_nest_checkpoint_sequence'),
        UniqueConstraint('run_id', 'group_id', 'stock_option_id', name='uq_quote_nest_checkpoint_option'),
        CheckConstraint('sequence >= 1 AND sequence <= 36', name='ck_quote_nest_checkpoint_sequence'),
        CheckConstraint('payload_bytes > 0 AND payload_bytes <= 8388608', name='ck_quote_nest_checkpoint_bytes'),
        CheckConstraint(
            'length(content_sha256) = 64 AND length(lease_token) = 36', name='ck_quote_nest_checkpoint_identity'
        ),
        CheckConstraint(
            'length(group_id) BETWEEN 1 AND 200 AND length(stock_option_id) BETWEEN 1 AND 200',
            name='ck_quote_nest_checkpoint_keys',
        ),
        Index('ix_quote_nest_checkpoint_company_run', 'company_id', 'run_id', 'sequence'),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=False)
    lease_token = Column(String(36), nullable=False)
    sequence = Column(Integer, nullable=False)
    group_id = Column(String(200), nullable=False)
    stock_option_id = Column(String(200), nullable=False)
    result_json = Column(JSON, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


IMMUTABLE_RUN_FIELDS = (
    'id',
    'company_id',
    'draft_id',
    'revision_id',
    'revision_number',
    'input_sha256',
    'request_key',
    'request_hash',
    'created_by',
    'submitted_api_token_id',
    'created_at',
    'settings_json',
    'release_identity',
)


@event.listens_for(QuoteNestingRun, 'before_update')
def _refuse_run_input_mutation(_mapper, _connection, target):
    state = inspect(target)
    if any(state.attrs[field].history.has_changes() for field in IMMUTABLE_RUN_FIELDS):
        raise ValueError('Nesting run inputs are immutable.')


@event.listens_for(QuoteNestingRun, 'before_delete')
@event.listens_for(QuoteNestingRunCheckpoint, 'before_update')
@event.listens_for(QuoteNestingRunCheckpoint, 'before_delete')
def _refuse_run_history_mutation(_mapper, _connection, _target):
    raise ValueError('Nesting run history is immutable.')


# DDL is mirrored literally in migration 102. In particular, privilege blocks
# contain no interpolated SELECT, and functions pin search_path.

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

for _table in (QuoteNestingRun.__table__, QuoteNestingRunCheckpoint.__table__):
    for _dialect, _statements in (('postgresql', POSTGRES_DDL[_table.name]), ('sqlite', SQLITE_DDL[_table.name])):
        for _statement in _statements:
            event.listen(_table, 'after_create', DDL(_statement.replace('%', '%%')).execute_if(dialect=_dialect))
for _table, _function in (
    (QuoteNestingRun.__table__, 'quote_nest_run_guard'),
    (QuoteNestingRunCheckpoint.__table__, 'quote_nest_checkpoint_guard'),
):
    event.listen(_table, 'after_drop', DDL(f'DROP FUNCTION IF EXISTS {_function}()').execute_if(dialect='postgresql'))
