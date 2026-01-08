from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.database import Base


class NotificationPreference(Base):
    """User notification preferences"""
    __tablename__ = "notification_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)

    # Preferences stored as JSON:
    # {
    #   "WO_RELEASED": {"email": true, "digest": false},
    #   "WO_LATE": {"email": true, "digest": true},
    #   ...
    # }
    preferences = Column(JSON, nullable=False, default={})

    # Digest settings
    digest_enabled = Column(Boolean, default=True)
    digest_frequency = Column(String(20), default="DAILY")  # NONE, DAILY, WEEKLY
    digest_time = Column(String(5), default="08:00")  # HH:MM format

    # Relationships
    user = relationship("User", back_populates="notification_preference")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class NotificationLog(Base):
    """Log of sent notifications"""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_type = Column(String(100), index=True, nullable=False)
    channel = Column(String(20), nullable=False)  # email, webhook, etc

    # Notification details
    subject = Column(String(500), nullable=True)
    body = Column(Text, nullable=True)

    # Status
    sent = Column(Boolean, default=False)
    error = Column(Text, nullable=True)

    # Related entity
    related_type = Column(String(100), nullable=True)  # WorkOrder, PurchaseOrder, etc
    related_id = Column(Integer, nullable=True)

    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User")


class DigestQueue(Base):
    """Queue for digest notifications"""
    __tablename__ = "digest_queue"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_type = Column(String(100), nullable=False)

    # Event data
    event_data = Column(JSON, nullable=False)

    # Processing
    processed = Column(Boolean, default=False)
    digest_date = Column(DateTime(timezone=True), nullable=True)  # When to include in digest

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User")
