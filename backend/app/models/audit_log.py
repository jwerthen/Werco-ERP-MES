from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, JSON
from datetime import datetime
from app.db.database import Base


class AuditLog(Base):
    """
    Comprehensive audit logging for CMMC Level 2 and AS9100D compliance.
    Tracks all user actions and data changes.
    """
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # When
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    
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
