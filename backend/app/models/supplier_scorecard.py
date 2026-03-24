from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class ScorecardPeriod(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMI_ANNUAL = "semi_annual"
    ANNUAL = "annual"


class SupplierScorecard(Base):
    """Performance scorecard per vendor per period - AS9100D supplier monitoring"""
    __tablename__ = "supplier_scorecards"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)

    # Period
    period_type = Column(SQLEnum(ScorecardPeriod), default=ScorecardPeriod.QUARTERLY)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    # Scores (0-100 scale)
    quality_score = Column(Float, default=0.0)
    quality_weight = Column(Float, default=0.40)
    delivery_score = Column(Float, default=0.0)
    delivery_weight = Column(Float, default=0.30)
    responsiveness_score = Column(Float, default=0.0)
    responsiveness_weight = Column(Float, default=0.15)
    price_score = Column(Float, default=0.0)
    price_weight = Column(Float, default=0.15)

    # Weighted overall
    overall_score = Column(Float, default=0.0)
    rating = Column(String(20))  # Excellent, Good, Acceptable, Probationary, Disqualified

    # Metrics
    total_pos = Column(Integer, default=0)
    total_lines = Column(Integer, default=0)
    on_time_deliveries = Column(Integer, default=0)
    late_deliveries = Column(Integer, default=0)
    total_received_qty = Column(Float, default=0.0)
    rejected_qty = Column(Float, default=0.0)
    ncr_count = Column(Integer, default=0)
    car_count = Column(Integer, default=0)

    # Notes
    notes = Column(Text)
    action_items = Column(Text)

    # Evaluation
    evaluated_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    vendor = relationship("Vendor", backref="scorecards")
    evaluator = relationship("User", foreign_keys=[evaluated_by])


class SupplierAudit(Base):
    """Supplier audit tracking for AS9100D compliance"""
    __tablename__ = "supplier_audits"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)

    audit_type = Column(String(100), nullable=False)  # Initial, Annual, For Cause, Follow-up
    audit_date = Column(Date, nullable=False)
    next_audit_date = Column(Date, nullable=True)

    auditor = Column(String(255))
    scope = Column(Text)
    findings = Column(Text)
    corrective_actions = Column(Text)

    result = Column(String(50))  # passed, conditional, failed
    score = Column(Float, nullable=True)

    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    vendor = relationship("Vendor", backref="audits")


class ApprovedSupplierList(Base):
    """Formal Approved Supplier List (ASL) entry"""
    __tablename__ = "approved_supplier_list"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, unique=True)

    approval_status = Column(String(50), default="approved")  # approved, conditional, probationary, suspended, removed
    approved_date = Column(Date, nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    scope = Column(Text)  # What they're approved to supply
    certifications_required = Column(Text)  # JSON list
    certifications_verified = Column(Boolean, default=False)

    last_review_date = Column(Date, nullable=True)
    next_review_date = Column(Date, nullable=True)
    review_frequency_months = Column(Integer, default=12)

    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    vendor = relationship("Vendor", backref="asl_entry")
    approver = relationship("User", foreign_keys=[approved_by])
