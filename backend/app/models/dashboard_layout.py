"""A person's saved dashboard layout: presentation only, never authority.

One row per (company, user). ``layout_json`` holds the ordered widget placements the
SPA renders on ``/``: ``{}`` means "use the stock default" and is written only by the
reset verb; ``{"format_version": 1, "widgets": [...]}`` is a customized layout. The
widget catalog is a frontend concern -- a stored id names a component and grants
nothing, because every data endpoint a widget reads enforces its own authorization.
Writes go through ``DashboardLayoutService`` (CAS on ``version`` + required audit).
"""

from datetime import datetime, timezone

from sqlalchemy import DDL, JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, UniqueConstraint, event

from app.db.database import Base
from app.db.mixins import TenantMixin


class DashboardLayout(Base, TenantMixin):
    __tablename__ = 'dashboard_layouts'
    __table_args__ = (
        # The unique constraint supplies the compound index for the tenant/user
        # lookup; another index on the same columns would duplicate it.
        UniqueConstraint('company_id', 'user_id', name='uq_dashboard_layout_company_user'),
        CheckConstraint('version >= 1', name='ck_dashboard_layout_version'),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    # The service performs explicit CAS with required audit in its own transaction.
    version = Column(Integer, nullable=False, default=1, server_default='1')
    layout_json = Column(JSON(none_as_null=True), nullable=False, default=dict, server_default='{}')
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


# Mirror migration 118 for model-bootstrap installations. ERP server authentication
# and tenant/user checks own access; PostgREST client roles receive no grants.
_SECURITY_SQL = (
    'ALTER TABLE dashboard_layouts ENABLE ROW LEVEL SECURITY',
    'REVOKE ALL ON TABLE dashboard_layouts FROM PUBLIC',
    'REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM PUBLIC',
    """DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE dashboard_layouts FROM anon;
        REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE dashboard_layouts FROM authenticated;
        REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM authenticated;
      END IF;
    END $$""",
)
for _statement in _SECURITY_SQL:
    event.listen(DashboardLayout.__table__, 'after_create', DDL(_statement).execute_if(dialect='postgresql'))
