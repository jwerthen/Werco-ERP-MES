"""Immutable original-byte evidence; separate from CAD approval and generic documents."""

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


class QuoteNestingSourceIntent(Base, TenantMixin):
    __tablename__ = 'quote_nesting_source_intents'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'revision_id', 'draft_id', 'revision_number', 'input_sha256'],
            [
                'quote_nesting_revisions.' + name
                for name in ('company_id', 'id', 'draft_id', 'revision_number', 'content_sha256')
            ],
            name='fk_nest_source_exact_revision',
        ),
        UniqueConstraint('company_id', 'id', name='uq_nest_source_tenant_intent'),
        UniqueConstraint('company_id', 'id', 'revision_id', name='uq_nest_source_intent_revision'),
        UniqueConstraint('company_id', 'id', 'source_sha256', 'byte_count', name='uq_nest_source_intent_bytes'),
        UniqueConstraint('company_id', 'request_key', name='uq_nest_source_request'),
        CheckConstraint('byte_count > 0 AND byte_count < 5000000', name='ck_nest_source_size'),
        CheckConstraint('target_count BETWEEN 1 AND 1000', name='ck_nest_source_targets'),
        CheckConstraint('revision_number > 0', name='ck_nest_source_revision'),
        CheckConstraint(
            'length(source_sha256)=64 AND length(input_sha256)=64 AND length(targets_sha256)=64 '
            'AND length(request_hash)=64 AND length(request_key)=36',
            name='ck_nest_source_hashes',
        ),
        CheckConstraint('length(source_name) BETWEEN 1 AND 1024', name='ck_nest_source_name'),
        CheckConstraint('length(mime_type) BETWEEN 1 AND 128', name='ck_nest_source_mime'),
        Index('ix_nest_source_revision', 'company_id', 'revision_id', 'id'),
    )
    id = Column(Integer, primary_key=True)
    draft_id = Column(Integer, nullable=False)
    revision_id = Column(Integer, nullable=False)
    revision_number = Column(Integer, nullable=False)
    input_sha256 = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    byte_count = Column(Integer, nullable=False)
    source_name = Column(String(1024), nullable=False)
    mime_type = Column(String(128), nullable=False)
    targets_json = Column(JSON, nullable=False)
    targets_sha256 = Column(String(64), nullable=False)
    target_count = Column(Integer, nullable=False)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    submitted_api_token_id = Column(Integer, ForeignKey('api_tokens.id'), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingSourceAttempt(Base, TenantMixin):
    __tablename__ = 'quote_nesting_source_attempts'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'intent_id'],
            ['quote_nesting_source_intents.company_id', 'quote_nesting_source_intents.id'],
            name='fk_nest_source_attempt_intent',
        ),
        UniqueConstraint('company_id', 'intent_id', 'id', name='uq_nest_source_attempt_identity'),
        UniqueConstraint('company_id', 'intent_id', 'ordinal', name='uq_nest_source_attempt_ordinal'),
        UniqueConstraint('object_key', name='uq_nest_source_object_key'),
        CheckConstraint('ordinal BETWEEN 1 AND 8', name='ck_nest_source_attempt_limit'),
        CheckConstraint('length(object_key)=36 AND length(provider_sha256)=64', name='ck_nest_source_attempt_hashes'),
        CheckConstraint('length(storage_ref) BETWEEN 1 AND 2048', name='ck_nest_source_attempt_reference'),
        Index('ix_nest_source_attempt_intent', 'company_id', 'intent_id', 'id'),
    )
    id = Column(Integer, primary_key=True)
    intent_id = Column(Integer, nullable=False)
    ordinal = Column(Integer, nullable=False)
    # UUID identity, not a user filename; storage_ref contains its generated scoped path.
    object_key = Column(String(36), nullable=False)
    storage_ref = Column(String(2048), nullable=False)
    provider_json = Column(JSON, nullable=False)
    provider_sha256 = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    submitted_api_token_id = Column(Integer, ForeignKey('api_tokens.id'), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingSourceReceipt(Base, TenantMixin):
    __tablename__ = 'quote_nesting_source_receipts'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'intent_id', 'attempt_id'],
            [
                'quote_nesting_source_attempts.company_id',
                'quote_nesting_source_attempts.intent_id',
                'quote_nesting_source_attempts.id',
            ],
            name='fk_nest_source_receipt_attempt',
        ),
        ForeignKeyConstraint(
            ['company_id', 'intent_id', 'source_sha256', 'byte_count'],
            [
                'quote_nesting_source_intents.company_id',
                'quote_nesting_source_intents.id',
                'quote_nesting_source_intents.source_sha256',
                'quote_nesting_source_intents.byte_count',
            ],
            name='fk_nest_source_receipt_bytes',
        ),
        UniqueConstraint('company_id', 'intent_id', name='uq_nest_source_receipt_intent'),
        UniqueConstraint('company_id', 'id', 'intent_id', name='uq_nest_source_receipt_identity'),
        CheckConstraint(
            'byte_count > 0 AND byte_count < 5000000 AND length(source_sha256)=64', name='ck_nest_source_receipt_bytes'
        ),
    )
    id = Column(Integer, primary_key=True)
    intent_id = Column(Integer, nullable=False)
    attempt_id = Column(Integer, nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    byte_count = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    submitted_api_token_id = Column(Integer, ForeignKey('api_tokens.id'), nullable=True)
    verified_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuoteNestingSourceBinding(Base, TenantMixin):
    __tablename__ = 'quote_nesting_source_bindings'
    __table_args__ = (
        ForeignKeyConstraint(
            ['company_id', 'receipt_id', 'intent_id'],
            [
                'quote_nesting_source_receipts.company_id',
                'quote_nesting_source_receipts.id',
                'quote_nesting_source_receipts.intent_id',
            ],
            name='fk_nest_source_binding_receipt',
        ),
        ForeignKeyConstraint(
            ['company_id', 'intent_id', 'revision_id'],
            [
                'quote_nesting_source_intents.company_id',
                'quote_nesting_source_intents.id',
                'quote_nesting_source_intents.revision_id',
            ],
            name='fk_nest_source_binding_revision',
        ),
        UniqueConstraint('company_id', 'revision_id', 'group_id', 'part_id', name='uq_nest_source_revision_part'),
        CheckConstraint(
            'length(group_id) BETWEEN 1 AND 200 AND length(part_id) BETWEEN 1 AND 200',
            name='ck_nest_source_binding_target',
        ),
        Index('ix_nest_source_binding_receipt', 'company_id', 'receipt_id'),
    )
    id = Column(Integer, primary_key=True)
    receipt_id = Column(Integer, nullable=False)
    intent_id = Column(Integer, nullable=False)
    revision_id = Column(Integer, nullable=False)
    group_id = Column(String(200), nullable=False)
    part_id = Column(String(200), nullable=False)
    provenance_json = Column(JSON, nullable=False)


SOURCE_TABLES = (
    QuoteNestingSourceIntent.__table__,
    QuoteNestingSourceAttempt.__table__,
    QuoteNestingSourceReceipt.__table__,
    QuoteNestingSourceBinding.__table__,
)

# Frozen in migration105 too. Service owns complete binding sets and required audit.
POSTGRES_DDL = {}
SQLITE_DDL = {}
for _table in SOURCE_TABLES:
    _name = _table.name
    _function = _name + '_immutable'
    _postgres = [
        f'ALTER TABLE {_name} ENABLE ROW LEVEL SECURITY',
        f'REVOKE ALL ON TABLE {_name} FROM PUBLIC',
        f'REVOKE ALL ON SEQUENCE {_name}_id_seq FROM PUBLIC',
    ]
    for _role, _existence in (
        ('anon', "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "),
        ('authenticated', "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN "),
    ):
        _postgres.append(
            _existence
            + f'REVOKE ALL ON TABLE {_name} FROM {_role}; REVOKE ALL ON SEQUENCE {_name}_id_seq FROM {_role}; '
            + 'END IF; END $$'
        )
    _postgres.append(
        f'CREATE OR REPLACE FUNCTION {_function}() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = \'\' AS $$ '
        "BEGIN RAISE EXCEPTION 'Original CAD evidence is immutable.' USING ERRCODE='23514'; RETURN NULL; END; $$"
    )
    _sqlite = []
    for _operation in ('UPDATE', 'DELETE', 'TRUNCATE'):
        _trigger = f'tr_{_name}_{_operation.lower()}'
        _level = 'STATEMENT' if _operation == 'TRUNCATE' else 'ROW'
        _postgres.extend(
            (
                f'DROP TRIGGER IF EXISTS {_trigger} ON {_name}',
                f'CREATE TRIGGER {_trigger} BEFORE {_operation} ON {_name} FOR EACH {_level} '
                f'EXECUTE FUNCTION {_function}()',
            )
        )
        if _operation != 'TRUNCATE':
            _sqlite.append(
                f'CREATE TRIGGER IF NOT EXISTS {_trigger} BEFORE {_operation} ON {_name} '
                "BEGIN SELECT RAISE(ABORT, 'Original CAD evidence is immutable.'); END"
            )
    POSTGRES_DDL[_name] = tuple(_postgres)
    SQLITE_DDL[_name] = tuple(_sqlite)
    for _dialect, _statements in (('postgresql', _postgres), ('sqlite', _sqlite)):
        for _statement in _statements:
            event.listen(_table, 'after_create', DDL(_statement).execute_if(dialect=_dialect))
    event.listen(_table, 'after_drop', DDL(f'DROP FUNCTION IF EXISTS {_function}()').execute_if(dialect='postgresql'))
