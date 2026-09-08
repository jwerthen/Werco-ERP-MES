"""Unapproved nesting snapshots, separate from operational quotes and inventory."""

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
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class QuoteNestingDraft(Base, TenantMixin):
    __tablename__ = 'quote_nesting_drafts'
    __table_args__ = (
        UniqueConstraint('company_id', 'id', name='uq_quote_nest_draft_company_id'),
        CheckConstraint("status = 'DRAFT'", name='ck_quote_nest_draft_unapproved'),
        CheckConstraint('version >= 1 AND version = latest_revision_number', name='ck_quote_nest_draft_version'),
        CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_quote_nest_draft_name'),
        Index('ix_quote_nest_draft_company_updated', 'company_id', 'updated_at', 'id'),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default='DRAFT', server_default='DRAFT')
    # The service owns explicit company/id/version compare-and-swap; no mapper
    # auto-increment can silently turn an old revision into a current write.
    version = Column(Integer, nullable=False, default=1, server_default='1')
    latest_revision_number = Column(Integer, nullable=False, default=1, server_default='1')
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingRevision(Base, TenantMixin):
    __tablename__ = 'quote_nesting_revisions'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'draft_id'],
            ['quote_nesting_drafts.company_id', 'quote_nesting_drafts.id'],
            name='fk_quote_nest_revision_tenant_draft',
        ),
        UniqueConstraint('company_id', 'request_key', name='uq_quote_nest_revision_request'),
        UniqueConstraint('draft_id', 'revision_number', name='uq_quote_nest_revision_number'),
        CheckConstraint(
            'revision_number >= 1 AND draft_version = revision_number', name='ck_quote_nest_revision_version'
        ),
        CheckConstraint('payload_bytes > 0 AND payload_schema_version > 0', name='ck_quote_nest_revision_payload'),
        CheckConstraint(
            'length(content_sha256) = 64 AND length(request_hash) = 64', name='ck_quote_nest_revision_hashes'
        ),
        CheckConstraint('length(request_key) = 36', name='ck_quote_nest_revision_request_key'),
        CheckConstraint('length(trim(name)) BETWEEN 1 AND 200', name='ck_quote_nest_revision_name'),
        Index('ix_quote_nest_revision_company_draft', 'company_id', 'draft_id', 'revision_number'),
    )
    id = Column(Integer, primary_key=True)
    draft_id = Column(Integer, nullable=False)
    revision_number = Column(Integer, nullable=False)
    draft_version = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    estimate_json = Column(JSON, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    payload_schema_version = Column(Integer, nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    review_issues_json = Column(JSON, nullable=False, default=list)


@event.listens_for(QuoteNestingRevision, 'before_update')
@event.listens_for(QuoteNestingRevision, 'before_delete')
def _refuse_revision_mutation(_mapper, _connection, _target):
    raise ValueError('Nesting draft revisions are immutable; append a new revision.')


# Lock-step with migration 101. create_all is a supported bootstrap path, and
# mapper events alone do not protect bulk SQL or another database connection.
for _table, _role_statements in (
    (
        QuoteNestingDraft.__table__,
        (
            """DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE quote_nesting_drafts FROM anon;
                REVOKE ALL ON SEQUENCE quote_nesting_drafts_id_seq FROM anon;
              END IF;
            END $$""",
            """DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE quote_nesting_drafts FROM authenticated;
                REVOKE ALL ON SEQUENCE quote_nesting_drafts_id_seq FROM authenticated;
              END IF;
            END $$""",
        ),
    ),
    (
        QuoteNestingRevision.__table__,
        (
            """DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE quote_nesting_revisions FROM anon;
                REVOKE ALL ON SEQUENCE quote_nesting_revisions_id_seq FROM anon;
              END IF;
            END $$""",
            """DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE quote_nesting_revisions FROM authenticated;
                REVOKE ALL ON SEQUENCE quote_nesting_revisions_id_seq FROM authenticated;
              END IF;
            END $$""",
        ),
    ),
):
    for _statement in (
        f'ALTER TABLE {_table.name} ENABLE ROW LEVEL SECURITY',
        f'REVOKE ALL ON TABLE {_table.name} FROM PUBLIC',
        f'REVOKE ALL ON SEQUENCE {_table.name}_id_seq FROM PUBLIC',
        *_role_statements,
    ):
        event.listen(_table, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))

_revision_table = QuoteNestingRevision.__table__
event.listen(
    _revision_table,
    'after_create',
    DDL("""CREATE OR REPLACE FUNCTION quote_nest_revision_immutable() RETURNS TRIGGER
      LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN
        RAISE EXCEPTION 'Nesting draft revisions are immutable; append a new revision.' USING ERRCODE = '23514';
        RETURN NULL;
      END; $$""").execute_if(dialect='postgresql'),
)
for _operation in ('UPDATE', 'DELETE', 'TRUNCATE'):
    _trigger = f'tr_quote_nest_revision_no_{_operation.lower()}'
    _level = 'STATEMENT' if _operation == 'TRUNCATE' else 'ROW'
    event.listen(
        _revision_table,
        'after_create',
        DDL(
            f'CREATE TRIGGER {_trigger} BEFORE {_operation} ON quote_nesting_revisions '
            f'FOR EACH {_level} EXECUTE FUNCTION quote_nest_revision_immutable()'
        ).execute_if(dialect='postgresql'),
    )
    if _operation != 'TRUNCATE':
        event.listen(
            _revision_table,
            'after_create',
            DDL(
                f'CREATE TRIGGER IF NOT EXISTS {_trigger} BEFORE {_operation} ON quote_nesting_revisions '
                "BEGIN SELECT RAISE(ABORT, 'Nesting draft revisions are immutable; append a new revision.'); END"
            ).execute_if(dialect='sqlite'),
        )
event.listen(
    _revision_table,
    'after_drop',
    DDL('DROP FUNCTION IF EXISTS quote_nest_revision_immutable()').execute_if(dialect='postgresql'),
)
