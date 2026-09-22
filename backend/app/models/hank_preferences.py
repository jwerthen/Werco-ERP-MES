"""Explicit personal Hank presentation choices, never ERP production policy."""

from datetime import datetime, timezone

from sqlalchemy import DDL, JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, UniqueConstraint, event

from app.db.database import Base
from app.db.mixins import TenantMixin


class HankPreference(Base, TenantMixin):
    __tablename__ = 'hank_preferences'
    __table_args__ = (
        # The unique constraint supplies the compound index for the tenant/user
        # lookup; another index on the same columns would duplicate it.
        UniqueConstraint('company_id', 'user_id', name='uq_hank_preference_company_user'),
        CheckConstraint('version >= 1', name='ck_hank_preference_version'),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    # Services perform explicit CAS with required audit in their own transaction.
    version = Column(Integer, nullable=False, default=1, server_default='1')
    preferences_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


# Mirror migration109 for model-bootstrap installations. ERP server authentication
# and tenant/user checks own access; PostgREST client roles receive no grants.
_SECURITY_SQL = (
    'ALTER TABLE hank_preferences ENABLE ROW LEVEL SECURITY',
    'REVOKE ALL ON TABLE hank_preferences FROM PUBLIC',
    'REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM PUBLIC',
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE hank_preferences FROM anon;
        REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE hank_preferences FROM authenticated;
        REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM authenticated;
      END IF;
    END $$""",
)
for _statement in _SECURITY_SQL:
    event.listen(HankPreference.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
