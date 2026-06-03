from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class AIInteractionEvent(Base, TenantMixin):
    """Tenant-scoped signal captured when users interact with AI suggestions."""

    __tablename__ = "ai_interaction_events"
    __table_args__ = (
        Index("ix_ai_events_company_module_created", "company_id", "source_module", "created_at"),
        Index("ix_ai_events_company_entity", "company_id", "entity_type", "entity_id"),
        Index("ix_ai_events_company_type", "company_id", "event_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    source_module = Column(String(80), nullable=False, index=True)
    ai_feature = Column(String(120), nullable=True)
    surface = Column(String(120), nullable=True)

    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    recommendation_id = Column(Integer, ForeignKey("ai_recommendations.id"), nullable=True, index=True)

    context_summary = Column(Text, nullable=True)
    event_payload = Column(JSON, nullable=True, default=dict)
    confidence_score = Column(Float, nullable=True)
    prompt_version = Column(String(120), nullable=True)
    model_version = Column(String(120), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)

    recommendation = relationship("AIRecommendation", back_populates="events")
    corrections = relationship("AICorrection", back_populates="event")
    creator = relationship("User")


class AICorrection(Base, TenantMixin):
    """Field-level difference between an AI proposal and the human-approved value."""

    __tablename__ = "ai_corrections"
    __table_args__ = (
        Index("ix_ai_corrections_company_module_created", "company_id", "source_module", "created_at"),
        Index("ix_ai_corrections_company_field", "company_id", "field_path"),
        Index("ix_ai_corrections_company_entity", "company_id", "entity_type", "entity_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("ai_interaction_events.id"), nullable=True, index=True)
    recommendation_id = Column(Integer, ForeignKey("ai_recommendations.id"), nullable=True, index=True)

    source_module = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    field_path = Column(String(255), nullable=False)

    proposed_value = Column(JSON, nullable=True)
    final_value = Column(JSON, nullable=True)
    correction_reason = Column(Text, nullable=True)
    confidence_score = Column(Float, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)

    event = relationship("AIInteractionEvent", back_populates="corrections")
    recommendation = relationship("AIRecommendation", back_populates="corrections")
    creator = relationship("User")


class AIRecommendation(Base, TenantMixin):
    """Suggest-only AI recommendation that never mutates controlled records by itself."""

    __tablename__ = "ai_recommendations"
    __table_args__ = (
        Index("ix_ai_recommendations_company_status_priority", "company_id", "status", "priority"),
        Index("ix_ai_recommendations_company_module_status", "company_id", "source_module", "status"),
        Index("ix_ai_recommendations_company_target", "company_id", "target_entity_type", "target_entity_id"),
        Index("ix_ai_recommendations_company_type", "company_id", "recommendation_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_module = Column(String(80), nullable=False, index=True)
    recommendation_type = Column(String(80), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="pending", index=True)
    priority = Column(String(20), nullable=False, default="medium", index=True)

    title = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)

    target_entity_type = Column(String(80), nullable=True, index=True)
    target_entity_id = Column(Integer, nullable=True, index=True)
    suggested_action = Column(JSON, nullable=True, default=dict)
    evidence = Column(JSON, nullable=True, default=list)
    impact = Column(JSON, nullable=True, default=dict)

    confidence_score = Column(Float, nullable=False, default=0.5)
    prompt_version = Column(String(120), nullable=True)
    model_version = Column(String(120), nullable=True)
    status_reason = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    accepted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    dismissed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    acted_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    events = relationship("AIInteractionEvent", back_populates="recommendation")
    corrections = relationship("AICorrection", back_populates="recommendation")
    outcomes = relationship("AIOutcome", back_populates="recommendation")


class AIOutcome(Base, TenantMixin):
    """Downstream business result used to score whether AI suggestions helped."""

    __tablename__ = "ai_outcomes"
    __table_args__ = (
        Index("ix_ai_outcomes_company_module_observed", "company_id", "source_module", "observed_at"),
        Index("ix_ai_outcomes_company_entity", "company_id", "entity_type", "entity_id"),
        Index("ix_ai_outcomes_company_metric", "company_id", "metric_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(Integer, ForeignKey("ai_recommendations.id"), nullable=True, index=True)

    source_module = Column(String(80), nullable=False, index=True)
    outcome_type = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(Integer, nullable=True, index=True)

    metric_name = Column(String(120), nullable=True, index=True)
    metric_value = Column(Float, nullable=True)
    baseline_value = Column(Float, nullable=True)
    target_value = Column(Float, nullable=True)
    outcome_payload = Column(JSON, nullable=True, default=dict)

    observed_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)

    recommendation = relationship("AIRecommendation", back_populates="outcomes")
    creator = relationship("User")
