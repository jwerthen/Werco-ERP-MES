from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import Base
from app.db.mixins import TenantMixin


class NotificationPreference(Base, TenantMixin):
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


class Notification(Base, TenantMixin):
    """Canonical per-user in-app inbox row — one row per user per notified event.

    Distinct from ``NotificationLog`` (a per-channel delivery-attempt record for
    email/SMS): this is the bell/popover/``/notifications`` inbox state. ``event_key``
    is the catalog key (``notification_catalog``); ``link`` is a relative SPA route.
    ``company_id`` (TenantMixin) is stamped from the triggering event — every read is
    self+tenant scoped (compliance §8).
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_unread", "user_id", "is_read"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_key = Column(String(80), nullable=False, index=True)
    severity = Column(String(20), nullable=False, default="info")
    title = Column(String(500), nullable=False)
    body = Column(Text, nullable=True)
    link = Column(String(500), nullable=True)  # relative SPA route, e.g. /work-orders/42

    related_type = Column(String(100), nullable=True)
    related_id = Column(Integer, nullable=True)

    is_read = Column(Boolean, nullable=False, default=False, server_default="false")
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User")


class NotificationLog(Base, TenantMixin):
    """Log of sent notifications"""

    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_type = Column(String(100), index=True, nullable=False)
    channel = Column(String(20), nullable=False)  # email, sms, in_app, webhook, etc

    # Notification details
    subject = Column(String(500), nullable=True)
    body = Column(Text, nullable=True)

    # Status
    sent = Column(Boolean, default=False)
    error = Column(Text, nullable=True)

    # Related entity
    related_type = Column(String(100), nullable=True)  # WorkOrder, PurchaseOrder, etc
    related_id = Column(Integer, nullable=True)

    # Back-link to the canonical in-app Notification row (email/SMS delivery attempts
    # link to the inbox row they delivered). Nullable — a log row may exist without an
    # in-app row (email-only prefs).
    notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True, index=True)

    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User")
    notification = relationship("Notification")


class DigestQueue(Base, TenantMixin):
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
