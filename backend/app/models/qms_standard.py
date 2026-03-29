from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Float, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base


class QMSStandard(Base):
    """
    QMS Standard document (e.g., AS9100D, ISO 9001:2015, internal Quality Manual).
    Stores the top-level standard with its clauses for audit readiness mapping.
    """
    __tablename__ = "qms_standards"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)  # e.g. "AS9100D", "ISO 9001:2015"
    version = Column(String(50))  # e.g. "Rev D", "2015"
    description = Column(Text)
    standard_body = Column(String(255))  # e.g. "SAE International", "ISO"

    # Optional uploaded PDF/document reference
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)

    # Status
    is_active = Column(Boolean, default=True)

    # Audit
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    clauses = relationship("QMSClause", back_populates="standard", cascade="all, delete-orphan",
                           order_by="QMSClause.sort_order")
    document = relationship("Document")


class QMSClause(Base):
    """
    Individual clause/requirement within a QMS standard.
    Hierarchical structure supports sub-clauses (e.g., 8.5, 8.5.1, 8.5.2).
    """
    __tablename__ = "qms_clauses"

    id = Column(Integer, primary_key=True, index=True)
    standard_id = Column(Integer, ForeignKey("qms_standards.id"), nullable=False)

    # Clause identification
    clause_number = Column(String(50), nullable=False)  # e.g. "8.5.2"
    title = Column(String(500), nullable=False)  # e.g. "Identification and Traceability"
    description = Column(Text)  # Full clause text/requirements
    parent_clause_id = Column(Integer, ForeignKey("qms_clauses.id"), nullable=True)
    sort_order = Column(Integer, default=0)

    # Compliance tracking
    compliance_status = Column(String(50), default="not_assessed")
    # not_assessed, compliant, partial, non_compliant, not_applicable
    compliance_notes = Column(Text)
    last_assessed_date = Column(DateTime, nullable=True)
    last_assessed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    next_review_date = Column(DateTime, nullable=True)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    standard = relationship("QMSStandard", back_populates="clauses")
    parent_clause = relationship("QMSClause", remote_side=[id], backref="sub_clauses")
    evidence_links = relationship("QMSClauseEvidence", back_populates="clause",
                                  cascade="all, delete-orphan")


class QMSClauseEvidence(Base):
    """
    Links a QMS clause to evidence within the system (documents, modules, records).
    This is the key table that makes audit preparation seamless — it maps each
    standard requirement to concrete evidence in the ERP/MES system.
    """
    __tablename__ = "qms_clause_evidence"

    id = Column(Integer, primary_key=True, index=True)
    clause_id = Column(Integer, ForeignKey("qms_clauses.id"), nullable=False)

    # Evidence type — what kind of system evidence supports this clause
    evidence_type = Column(String(50), nullable=False)
    # document, module, ncr, car, fai, calibration, training, procedure, spc, other

    # Reference to the evidence
    title = Column(String(500), nullable=False)  # Human-readable description
    description = Column(Text)  # How this evidence satisfies the clause
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)

    # For linking to system modules (not a FK, just a reference)
    module_reference = Column(String(255))  # e.g. "/quality/ncr", "/calibration"
    record_type = Column(String(100))  # e.g. "ncr", "car", "fai", "calibration_record"
    record_id = Column(Integer, nullable=True)  # Specific record ID if applicable

    # Verification
    is_verified = Column(Boolean, default=False)
    verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_date = Column(DateTime, nullable=True)
    verification_notes = Column(Text)

    # Auto-link fields
    is_auto_linked = Column(Boolean, default=False)  # Distinguishes auto vs manual evidence
    auto_link_query = Column(String(255), nullable=True)  # Which mapping rule matched
    last_refreshed = Column(DateTime, nullable=True)  # When live counts were last updated
    live_count = Column(Integer, nullable=True)  # Cached count of matching records

    # Audit
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    clause = relationship("QMSClause", back_populates="evidence_links")
    document = relationship("Document")
