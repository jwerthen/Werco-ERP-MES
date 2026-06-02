from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class RoutingGenerationSession(Base, TenantMixin):
    """A drawing-to-routing proposal and the final user-approved edits."""

    __tablename__ = "routing_generation_sessions"

    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)
    routing_id = Column(Integer, ForeignKey("routings.id"), nullable=True, index=True)

    file_name = Column(String(255), nullable=True)
    file_type = Column(String(20), nullable=True, index=True)
    file_size = Column(Integer, nullable=True)
    file_path = Column(String(500), nullable=True)

    drawing_text = Column(Text, nullable=True)
    geometry = Column(JSON, nullable=True)
    drawing_info = Column(JSON, nullable=True)
    proposed_operations = Column(JSON, nullable=True)
    approved_operations = Column(JSON, nullable=True)
    correction_summary = Column(JSON, nullable=True)
    learned_context = Column(JSON, nullable=True)
    warnings = Column(JSON, nullable=True)

    extraction_confidence = Column(String(20), nullable=True)
    source_was_ocr = Column(Boolean, default=False)
    status = Column(String(30), default="proposed", index=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)

    part = relationship("Part")
    routing = relationship("Routing")


class RoutingLearnedAlias(Base, TenantMixin):
    """Tenant-specific operation/work-center terminology learned from approved edits."""

    __tablename__ = "routing_learned_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "alias", "work_center_type", name="uq_routing_alias_company_alias_type"),
        Index("ix_routing_alias_company_alias", "company_id", "alias"),
        Index("ix_routing_alias_company_type", "company_id", "work_center_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    alias = Column(String(120), nullable=False)
    work_center_type = Column(String(80), nullable=False)
    source = Column(String(50), default="approved_edit")
    usage_count = Column(Integer, default=1)
    confidence_score = Column(Float, default=0.55)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RoutingWorkCenterPreference(Base, TenantMixin):
    """Preferred concrete work center for a feature signature and work center type."""

    __tablename__ = "routing_work_center_preferences"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "feature_key",
            "work_center_type",
            "work_center_id",
            name="uq_routing_wc_pref_company_feature_type_wc",
        ),
        Index("ix_routing_wc_pref_company_feature", "company_id", "feature_key"),
        Index("ix_routing_wc_pref_company_type", "company_id", "work_center_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    feature_key = Column(String(255), nullable=False)
    part_type = Column(String(50), nullable=True)
    material = Column(String(120), nullable=True)
    thickness = Column(String(60), nullable=True)
    finish = Column(String(120), nullable=True)
    work_center_type = Column(String(80), nullable=False)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)
    usage_count = Column(Integer, default=1)
    confidence_score = Column(Float, default=0.55)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    work_center = relationship("WorkCenter")


class RoutingOperationPattern(Base, TenantMixin):
    """Reusable approved routing pattern for similar future drawings."""

    __tablename__ = "routing_operation_patterns"
    __table_args__ = (
        UniqueConstraint("company_id", "pattern_key", name="uq_routing_pattern_company_key"),
        Index("ix_routing_pattern_company_key", "company_id", "pattern_key"),
        Index("ix_routing_pattern_company_part_type", "company_id", "part_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    pattern_key = Column(String(255), nullable=False)
    part_type = Column(String(50), nullable=True)
    material = Column(String(120), nullable=True)
    thickness = Column(String(60), nullable=True)
    finish = Column(String(120), nullable=True)
    feature_signature = Column(JSON, nullable=True)
    operations = Column(JSON, nullable=False)
    usage_count = Column(Integer, default=1)
    confidence_score = Column(Float, default=0.55)
    last_used_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
