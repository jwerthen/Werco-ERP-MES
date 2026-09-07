"""Immutable reviewed PDF plus durable, explicitly requested SMTP attempt."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import deferred

from app.db.database import Base
from app.db.mixins import TenantMixin


class DocumentDelivery(Base, TenantMixin):
    __tablename__ = 'document_deliveries'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_document_delivery_request'),
        Index('ix_delivery_company_source', 'company_id', 'entity_type', 'entity_id'),
        CheckConstraint('length(attachment) <= 5242880', name='ck_delivery_attachment_size'),
        CheckConstraint('length(body) <= 10000', name='ck_delivery_body_size'),
        CheckConstraint("entity_type IN ('quote','purchase_order')", name='ck_delivery_entity'),
        CheckConstraint("status IN ('prepared','sending','accepted','failed','unknown')", name='ck_delivery_status'),
    )
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(30), nullable=False)
    entity_id = Column(Integer, nullable=False)
    document_number = Column(String(100), nullable=False)
    issue_date = Column(Date, nullable=True)
    source_hash = Column(String(64), nullable=False)
    attachment_name = Column(String(150), nullable=False)
    attachment_sha256 = Column(String(64), nullable=False)
    attachment = deferred(Column(LargeBinary, nullable=False))
    attachment_size = Column(Integer, nullable=False)
    recipient = Column(String(320), nullable=False, default='')
    subject = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='prepared')
    version = Column(Integer, nullable=False, default=1)
    request_key = Column(String(100), nullable=True)
    request_hash = Column(String(64), nullable=True)
    provider_message_id = Column(String(150), nullable=True)
    status_detail = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    sent_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    attempted_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    verified_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    verification_note = Column(Text, nullable=True)
