from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, JSON, BigInteger, Index
from datetime import datetime
from app.db.database import Base


class AuditLog(Base):
    """
    Comprehensive audit logging for CMMC Level 2 (AU-3.3.8) and AS9100D compliance.
    
    IMMUTABILITY FEATURES:
    - sequence_number: Monotonically increasing, gaps indicate tampering
    - integrity_hash: SHA-256 hash of record content + previous hash (hash chain)
    - previous_hash: Links to prior record for chain verification
    - Database triggers prevent UPDATE and DELETE operations
    
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
    
    # What
    action = Column(String(100), nullable=False, index=True)  # CREATE, UPDATE, DELETE, LOGIN, LOGOUT, VIEW, EXPORT, etc.
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
    __table_args__ = (
        Index('ix_audit_logs_integrity', 'sequence_number', 'integrity_hash'),
    )
