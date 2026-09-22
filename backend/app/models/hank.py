"""Durable, actor-bound Hank previews and receipts; business records remain authoritative."""

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


class HankTask(Base, TenantMixin):
    __tablename__ = 'hank_tasks'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_hank_task_request'),
        CheckConstraint('version >= 1', name='ck_hank_task_version'),
        CheckConstraint('length(request_key) = 36 AND length(request_hash) = 64', name='ck_hank_task_request_identity'),
        CheckConstraint('length(trim(title)) BETWEEN 1 AND 300', name='ck_hank_task_title'),
        CheckConstraint(
            "status IN ('awaiting_review', 'completed', 'cancelled', 'watching', 'needs_attention', 'snoozed')",
            name='ck_hank_task_status',
        ),
        Index('ix_hank_task_company_owner_created', 'company_id', 'owner_id', 'created_at', 'id'),
        Index('ix_hank_task_status_checked', 'status', 'last_checked_at', 'id'),
    )
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    credential_key = Column(String(160), nullable=False)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    kind = Column(String(50), nullable=False)
    title = Column(String(300), nullable=False)
    status = Column(String(30), nullable=False, default='awaiting_review', server_default='awaiting_review')
    # The command service performs explicit version CAS under a row lock. Do not
    # add ORM automatic version increments on top of its transaction boundary.
    version = Column(Integer, nullable=False, default=1, server_default='1')
    input_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    preview_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    source_versions_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    result_json = Column(JSON(none_as_null=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(String(1200), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    snoozed_until = Column(DateTime(timezone=True), nullable=True)


# Match migration108 for create_all+stamp bootstrap installations. FastAPI owns
# authentication and company/actor scoping; PostgREST client roles get no grants
# or invented Supabase-auth policies. The server retains its owner connection.
_SECURITY_SQL = (
    'ALTER TABLE hank_tasks ENABLE ROW LEVEL SECURITY',
    'REVOKE ALL ON TABLE hank_tasks FROM PUBLIC',
    'REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM PUBLIC',
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE hank_tasks FROM anon;
        REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE hank_tasks FROM authenticated;
        REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM authenticated;
      END IF;
    END $$""",
)
for _statement in _SECURITY_SQL:
    event.listen(HankTask.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
