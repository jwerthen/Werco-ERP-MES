from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.db.database import Base


class AuditLog(Base):
    """
    Comprehensive audit logging for CMMC Level 2 (AU-3.3.8) and AS9100D compliance.

    IMMUTABILITY FEATURES:
    - Database triggers prevent UPDATE and DELETE operations (migrations 008/060).
      ALWAYS in force; no application setting affects them.
    - sequence_number: Monotonically increasing; gaps indicate tampering *while the
      hash chain is enabled*.
    - integrity_hash: SHA-256 hash of record content + previous hash (hash chain)
    - previous_hash: Links to prior record for chain verification

    The hash chain is on by default but PAUSABLE at runtime via
    settings.AUDIT_HASH_CHAIN_ENABLED. While paused, rows are written with identical
    content but previous_hash is NULL and integrity_hash is the placeholder
    'LEGACY_CHAIN_PAUSED' (see audit_service.PAUSED_CHAIN_PLACEHOLDER); sequence_number
    then comes from a Postgres sequence, so gaps in that window are NORMAL and are not
    tamper indicators. Such rows are permanently unverifiable after the fact. Details:
    docs/AUDIT_LOG_RETENTION_RUNBOOK.md -> Pausing the hash chain.

    Tracks all user actions and data changes with tamper detection.
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)

    # Integrity fields for tamper detection (AU-3.3.8)
    sequence_number = Column(BigInteger, nullable=False, unique=True, index=True)
    integrity_hash = Column(String(64), nullable=False)  # SHA-256 hex digest
    previous_hash = Column(String(64), nullable=True)  # Hash chain link (null for first record)

    # When
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Who
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user_email = Column(String(255))  # Denormalized for historical record
    user_name = Column(String(255))

    # Tenant
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)

    # What
    action = Column(
        String(100), nullable=False, index=True
    )  # CREATE, UPDATE, DELETE, LOGIN, LOGOUT, VIEW, EXPORT, etc.
    resource_type = Column(String(100), nullable=False, index=True)  # work_order, part, user, etc.
    resource_id = Column(Integer, nullable=True)
    resource_identifier = Column(String(255))  # Human readable identifier (WO-001, PART-123)

    # Details
    description = Column(Text)
    old_values = Column(JSON)  # Previous state for updates
    new_values = Column(JSON)  # New state for creates/updates

    # Context
    ip_address = Column(String(45))  # Supports IPv6
    user_agent = Column(String(500))
    session_id = Column(String(255))

    # Result
    success = Column(String(10), default="true")  # true, false
    error_message = Column(Text)

    # Additional context data
    extra_data = Column(JSON)  # Flexible additional context

    # Composite index for integrity verification queries
    __table_args__ = (Index('ix_audit_logs_integrity', 'sequence_number', 'integrity_hash'),)
