"""Company-owned quoting policies with immutable revisions and governance evidence."""

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
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


class QuoteNestingSpacingPolicy(Base, TenantMixin):
    __tablename__ = 'quote_nesting_spacing_policies'
    __table_args__ = (
        UniqueConstraint('company_id', name='uq_nest_spacing_policy_company'),
        UniqueConstraint('company_id', 'id', name='uq_nest_spacing_policy_tenant'),
        CheckConstraint(
            'version >= 0 AND latest_revision_number >= 0 AND version >= latest_revision_number',
            name='ck_nest_spacing_policy_version',
        ),
    )
    id = Column(Integer, primary_key=True)
    version = Column(Integer, nullable=False, default=0, server_default='0')
    latest_revision_number = Column(Integer, nullable=False, default=0, server_default='0')
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingSpacingRevision(Base, TenantMixin):
    __tablename__ = 'quote_nesting_spacing_revisions'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'policy_id'],
            ['quote_nesting_spacing_policies.company_id', 'quote_nesting_spacing_policies.id'],
            name='fk_nest_spacing_revision_policy',
        ),
        UniqueConstraint('policy_id', 'revision_number', name='uq_nest_spacing_revision_number'),
        UniqueConstraint(
            'company_id', 'policy_id', 'id', 'revision_number', 'content_sha256', name='uq_nest_spacing_revision_exact'
        ),
        CheckConstraint('revision_number >= 1', name='ck_nest_spacing_revision_number'),
        CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_nest_spacing_revision_name'),
        CheckConstraint('length(content_sha256) = 64', name='ck_nest_spacing_revision_hash'),
        CheckConstraint(
            'payload_schema_version >= 1 AND payload_bytes BETWEEN 1 AND 65536', name='ck_nest_spacing_revision_payload'
        ),
        Index('ix_nest_spacing_revision_company_policy', 'company_id', 'policy_id', 'revision_number'),
    )
    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, nullable=False)
    revision_number = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    content_json = Column(JSON, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    payload_schema_version = Column(Integer, nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingSpacingEvent(Base, TenantMixin):
    __tablename__ = 'quote_nesting_spacing_events'
    __table_args__ = (
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint(
            'company_id',
            'policy_id',
            'id',
            'revision_id',
            'revision_number',
            'content_sha256',
            name='uq_nest_spacing_event_exact',
        ),
        UniqueConstraint('company_id', 'request_key', name='uq_nest_spacing_event_request'),
        UniqueConstraint('policy_id', 'policy_version', name='uq_nest_spacing_event_version'),
        CheckConstraint('policy_version >= 1 AND revision_number >= 1', name='ck_nest_spacing_event_version'),
        CheckConstraint("kind IN ('REVISION_CREATED', 'PUBLISHED', 'WITHDRAWN')", name='ck_nest_spacing_event_kind'),
        CheckConstraint(
            'length(content_sha256) = 64 AND length(request_hash) = 64 AND length(request_key) = 36',
            name='ck_nest_spacing_event_identity',
        ),
        CheckConstraint('length(trim(reason)) BETWEEN 1 AND 1000', name='ck_nest_spacing_event_reason'),
        CheckConstraint(
            "(kind = 'PUBLISHED' AND effective_at IS NOT NULL AND effective_at >= created_at AND publication_id IS NULL) "
            "OR (kind = 'REVISION_CREATED' AND effective_at IS NULL AND publication_id IS NULL) "
            "OR (kind = 'WITHDRAWN' AND effective_at IS NULL AND publication_id IS NOT NULL)",
            name='ck_nest_spacing_event_shape',
        ),
        Index(
            'uq_nest_spacing_publication_date',
            'policy_id',
            'effective_at',
            unique=True,
            postgresql_where=text("kind = 'PUBLISHED'"),
            sqlite_where=text("kind = 'PUBLISHED'"),
        ),
        Index(
            'uq_nest_spacing_revision_event',
            'policy_id',
            'revision_id',
            unique=True,
            postgresql_where=text("kind = 'REVISION_CREATED'"),
            sqlite_where=text("kind = 'REVISION_CREATED'"),
        ),
        Index(
            'uq_nest_spacing_withdrawal',
            'publication_id',
            unique=True,
            postgresql_where=text("kind = 'WITHDRAWN'"),
            sqlite_where=text("kind = 'WITHDRAWN'"),
        ),
        Index('ix_nest_spacing_event_company_policy', 'company_id', 'policy_id', 'policy_version'),
    )
    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, nullable=False)
    policy_version = Column(Integer, nullable=False)
    kind = Column(String(20), nullable=False)
    revision_id = Column(Integer, nullable=False)
    revision_number = Column(Integer, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    publication_id = Column(Integer, nullable=True)
    effective_at = Column(DateTime, nullable=True)
    reason = Column(String(1000), nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    submitted_api_token_id = Column(Integer, ForeignKey('api_tokens.id'), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)


@event.listens_for(QuoteNestingSpacingRevision, 'before_update')
@event.listens_for(QuoteNestingSpacingRevision, 'before_delete')
@event.listens_for(QuoteNestingSpacingEvent, 'before_update')
@event.listens_for(QuoteNestingSpacingEvent, 'before_delete')
@event.listens_for(QuoteNestingSpacingPolicy, 'before_delete')
def _refuse_history_mutation(_mapper, _connection, _target):
    raise ValueError('Spacing policy history is immutable; append a revision or governance event.')


@event.listens_for(QuoteNestingSpacingPolicy, 'before_update')
def _refuse_policy_identity_change(_mapper, _connection, target):
    if any(
        inspect(target).attrs[field].history.has_changes() for field in ('id', 'company_id', 'created_by', 'created_at')
    ):
        raise ValueError('Spacing policy identity is immutable.')


# Frozen SQL is duplicated in migration 103; metadata bootstrap must enforce the same boundary.
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

for _table, _function in (
    (QuoteNestingSpacingPolicy.__table__, 'nest_spacing_policy_guard'),
    (QuoteNestingSpacingRevision.__table__, 'nest_spacing_revision_guard'),
    (QuoteNestingSpacingEvent.__table__, 'nest_spacing_event_guard'),
):
    for _dialect, _statements in (('postgresql', POSTGRES_DDL[_table.name]), ('sqlite', SQLITE_DDL[_table.name])):
        for _statement in _statements:
            event.listen(_table, 'after_create', DDL(_statement.replace('%', '%%')).execute_if(dialect=_dialect))
    event.listen(_table, 'after_drop', DDL(f'DROP FUNCTION IF EXISTS {_function}()').execute_if(dialect='postgresql'))
