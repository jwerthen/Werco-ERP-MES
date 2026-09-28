"""Tenant-scoped lean improvement suggestions and append-only activity."""

from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


def utcnow():
    return datetime.now(timezone.utc)


class ImprovementSuggestion(Base, TenantMixin):
    __tablename__ = 'improvement_suggestions'
    __table_args__ = (
        CheckConstraint('version >= 1', name='ck_improvement_version'),
        CheckConstraint(
            "status IN ('new','under_review','approved','in_progress','implemented','on_hold','declined')",
            name='ck_improvement_status',
        ),
        CheckConstraint("priority IN ('low','medium','high')", name='ck_improvement_priority'),
        CheckConstraint(
            "category IN ('poka_yoke','five_s','standard_work','flow_layout','quality','safety_ergonomics','setup_reduction','equipment','inventory','other')",
            name='ck_improvement_category',
        ),
        CheckConstraint('length(trim(title)) BETWEEN 1 AND 200', name='ck_improvement_title'),
        CheckConstraint(
            "status <> 'implemented' OR (implementation_notes IS NOT NULL AND length(trim(implementation_notes)) > 0 AND implemented_at IS NOT NULL)",
            name='ck_improvement_implementation',
        ),
        Index('ix_improvement_company_status_created', 'company_id', 'status', 'created_at', 'id'),
        Index('ix_improvement_company_category', 'company_id', 'category'),
    )
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    problem = Column(Text, nullable=False)
    proposed_solution = Column(Text, nullable=False)
    expected_benefit = Column(Text, nullable=False)
    category = Column(String(40), nullable=False)
    priority = Column(String(10), nullable=False, default='medium', server_default='medium')
    area = Column(String(150), nullable=True)
    status = Column(String(30), nullable=False, default='new', server_default='new')
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    owner_name = Column(String(255), nullable=True)
    target_date = Column(Date, nullable=True)
    implementation_notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    created_by_name = Column(String(255), nullable=False)
    updated_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    updated_by_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    implemented_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default='1')
    __mapper_args__ = {'version_id_col': version}


class ImprovementActivity(Base, TenantMixin):
    __tablename__ = 'improvement_activities'
    __table_args__ = (
        CheckConstraint(
            "kind IN ('submitted','updated','status_changed','comment')", name='ck_improvement_activity_kind'
        ),
        Index('ix_improvement_activity_suggestion_created', 'suggestion_id', 'created_at', 'id'),
    )
    id = Column(Integer, primary_key=True)
    suggestion_id = Column(Integer, ForeignKey('improvement_suggestions.id'), nullable=False)
    kind = Column(String(30), nullable=False)
    actor_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    actor_name = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    changes = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


@event.listens_for(ImprovementActivity, 'before_update')
@event.listens_for(ImprovementActivity, 'before_delete')
def _immutable_activity(mapper, connection, target):
    raise ValueError('Continuous improvement activity cannot be modified or deleted')


# FastAPI owns authentication/tenancy. These are private to the backend; neither
# Supabase client role receives a direct Data API path, including create_all bootstraps.
for _model in (ImprovementSuggestion, ImprovementActivity):
    _table = _model.__tablename__
    for _statement in (
        f'ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY',
        f'REVOKE ALL ON TABLE {_table} FROM PUBLIC',
        f'REVOKE ALL ON SEQUENCE {_table}_id_seq FROM PUBLIC',
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

for _statement in (
    """CREATE OR REPLACE FUNCTION prevent_improvement_activity_change() RETURNS trigger
       LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
         RAISE EXCEPTION 'Continuous improvement activity cannot be modified or deleted';
       END $$""",
    'REVOKE ALL ON FUNCTION prevent_improvement_activity_change() FROM PUBLIC',
    """CREATE TRIGGER tr_improvement_activity_immutable BEFORE UPDATE OR DELETE ON improvement_activities
       FOR EACH ROW EXECUTE FUNCTION prevent_improvement_activity_change()""",
):
    event.listen(ImprovementActivity.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
