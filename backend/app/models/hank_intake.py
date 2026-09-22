"""Durable private PDF intake, extraction evidence and reviewed filing receipts."""

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class HankIntakeBatch(Base, TenantMixin):
    __tablename__ = 'hank_intake_batches'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_hank_intake_batch_request'),
        Index('ix_hank_intake_batch_owner', 'company_id', 'owner_id', 'id'),
    )
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    credential_key = Column(String(160), nullable=False)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class HankIntakeFile(Base, TenantMixin):
    __tablename__ = 'hank_intake_files'
    __table_args__ = (
        UniqueConstraint('batch_id', 'ordinal', name='uq_hank_intake_file_ordinal'),
        CheckConstraint('version >= 1', name='ck_hank_intake_file_version'),
        CheckConstraint('ordinal BETWEEN 0 AND 4', name='ck_hank_intake_file_ordinal'),
        CheckConstraint('file_size > 0 AND file_size <= 10485760', name='ck_hank_intake_file_size'),
        CheckConstraint(
            "status IN ('queued','analyzing','awaiting_review','planned','completed','failed','cancelled')",
            name='ck_hank_intake_file_status',
        ),
        Index('ix_hank_intake_file_hash', 'company_id', 'content_sha256'),
        Index('ix_hank_intake_file_batch', 'batch_id', 'id'),
        Index('ix_hank_intake_file_status', 'status', 'updated_at', 'id'),
    )
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('hank_intake_batches.id'), nullable=False)
    ordinal = Column(Integer, nullable=False)
    filename = Column(String(255), nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    storage_ref = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=False)
    page_count = Column(Integer, nullable=True)
    status = Column(String(30), nullable=False, default='queued', server_default='queued')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    analysis_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    plan_json = Column(JSON(none_as_null=True), nullable=True)
    result_json = Column(JSON(none_as_null=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(String(1200), nullable=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


for _model in (HankIntakeBatch, HankIntakeFile):
    _table = _model.__tablename__
    for _statement in (
        f'ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY',
        f'REVOKE ALL ON TABLE {_table} FROM PUBLIC',
        f'REVOKE ALL ON SEQUENCE {_table}_id_seq FROM PUBLIC',
        # Table identifiers come only from the fixed model tuple above.
        f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE {_table} FROM anon;
            REVOKE ALL ON SEQUENCE {_table}_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE {_table} FROM authenticated;
            REVOKE ALL ON SEQUENCE {_table}_id_seq FROM authenticated;
          END IF;
        END $$""",  # nosec B608
    ):
        event.listen(_model.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
