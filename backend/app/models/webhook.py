from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, Text
from sqlalchemy.sql import func
from app.db.base_class import Base


class Webhook(Base):
    """Webhook subscription"""
    __tablename__ = "webhooks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    url = Column(String(500), nullable=False)

    # Events this webhook subscribes to (array of event names)
    events = Column(JSON, nullable=False)  # ["work_order.created", "work_order.released", ...]

    # Authentication
    secret = Column(String(500), nullable=False)  # Encrypted webhook secret for signing

    # Status
    is_active = Column(Boolean, default=True)
    failed_deliveries = Column(Integer, default=0)
    last_failure = Column(DateTime(timezone=True), nullable=True)

    # Metadata
    description = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class WebhookDelivery(Base):
    """Webhook delivery log"""
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, nullable=False, index=True)
    event = Column(String(100), index=True, nullable=False)

    # Payload
    payload = Column(JSON, nullable=False)

    # Delivery attempt
    attempt = Column(Integer, default=1)
    max_attempts = Column(Integer, default=3)

    # Response
    status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    # Status
    delivered = Column(Boolean, default=False)

    # Timing
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
