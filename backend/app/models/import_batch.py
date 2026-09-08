"""Durable import input identities and receipts; never a replacement for business audit rows."""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin


class ImportBatch(Base, TenantMixin):
    __tablename__ = 'import_batches'
    __table_args__ = (
        UniqueConstraint('company_id', 'request_key', name='uq_import_batch_request'),
        UniqueConstraint('company_id', 'entity', 'source_hash', name='uq_import_batch_source'),
        Index('ix_import_batch_company_created', 'company_id', 'created_at'),
    )
    id = Column(Integer, primary_key=True)
    entity = Column(String(30), nullable=False)
    filename = Column(String(255), nullable=False)
    source_hash = Column(String(64), nullable=False)
    request_key = Column(String(100), nullable=False)
    headers = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ImportBatchRow(Base, TenantMixin):
    __tablename__ = 'import_batch_rows'
    __table_args__ = (
        UniqueConstraint('batch_id', 'row_key', name='uq_import_batch_row_key'),
        UniqueConstraint('batch_id', 'source_row', name='uq_import_batch_source_row'),
        Index('ix_import_row_company_batch', 'company_id', 'batch_id', 'source_row'),
    )
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('import_batches.id'), nullable=False)
    row_key = Column(String(36), nullable=False)
    group_key = Column(String(36), nullable=False)
    source_row = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)  # passwords are stripped before persistence
    status = Column(String(20), nullable=False)
    error = Column(Text)
    result = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
