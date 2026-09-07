"""Shared triage state; source records remain authoritative for issue resolution."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin


class OperationalInboxState(Base, TenantMixin):
    __tablename__ = 'operational_inbox_states'
    __table_args__ = (
        UniqueConstraint('company_id', 'source_kind', 'source_id', name='uq_inbox_company_source'),
        Index('ix_inbox_company_owner', 'company_id', 'owner_id'),
    )
    id = Column(Integer, primary_key=True)
    source_kind = Column(String(30), nullable=False)
    source_id = Column(Integer, nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    next_action = Column(String(500), nullable=False, default='', server_default='')
    acknowledged_occurrence = Column(String(64), nullable=True)
    snoozed_occurrence = Column(String(64), nullable=True)
    snoozed_until = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default='1')
    updated_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
