"""Retained personal handoffs and explicitly approved, versioned shop procedures."""

from datetime import datetime, timezone

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


def utcnow():
    return datetime.now(timezone.utc)


class HankHandoff(Base, TenantMixin):
    __tablename__ = 'hank_handoffs'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_hank_handoff_request'),
        CheckConstraint('version >= 1', name='ck_hank_handoff_version'),
        CheckConstraint("status IN ('open','acknowledged','completed','cancelled')", name='ck_hank_handoff_status'),
        CheckConstraint('sender_id <> recipient_id', name='ck_hank_handoff_participants'),
        Index('ix_hank_handoff_sender_status', 'company_id', 'sender_id', 'status', 'id'),
        Index('ix_hank_handoff_recipient_status', 'company_id', 'recipient_id', 'status', 'id'),
    )
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    recipient_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    sender_name = Column(String(255), nullable=False)
    recipient_name = Column(String(255), nullable=False)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False, index=True)
    work_order_number = Column(String(100), nullable=False)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default='open', server_default='open')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    content_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    attachments_json = Column(JSON(none_as_null=True), nullable=False, default=list, server_default='[]')
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class HankRoutine(Base, TenantMixin):
    __tablename__ = 'hank_routines'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_hank_routine_request'),
        CheckConstraint('version >= 1', name='ck_hank_routine_version'),
        CheckConstraint("status IN ('draft','approved','archived')", name='ck_hank_routine_status'),
        Index('ix_hank_routine_company_status', 'company_id', 'status', 'id'),
    )
    id = Column(Integer, primary_key=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    approved_by = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    title = Column(String(160), nullable=False)
    description = Column(String(2000), nullable=False, default='', server_default='')
    status = Column(String(30), nullable=False, default='draft', server_default='draft')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    steps_json = Column(JSON(none_as_null=True), nullable=False, default=list, server_default='[]')
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    approved_at = Column(DateTime(timezone=True), nullable=True)


class HankRoutineRun(Base, TenantMixin):
    __tablename__ = 'hank_routine_runs'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_hank_routine_run_request'),
        CheckConstraint('version >= 1 AND current_step >= 0', name='ck_hank_routine_run_version'),
        CheckConstraint("status IN ('active','completed','cancelled')", name='ck_hank_routine_run_status'),
        Index('ix_hank_routine_run_owner_status', 'company_id', 'owner_id', 'status', 'id'),
    )
    id = Column(Integer, primary_key=True)
    routine_id = Column(Integer, ForeignKey('hank_routines.id'), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default='active', server_default='active')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    current_step = Column(Integer, nullable=False, default=0, server_default='0')
    snapshot_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    context_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    results_json = Column(JSON(none_as_null=True), nullable=False, default=list, server_default='[]')
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


for _model in (HankHandoff, HankRoutine, HankRoutineRun):
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
