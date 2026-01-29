from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Boolean, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class DocumentType(str, enum.Enum):
    DRAWING = "drawing"
    SPECIFICATION = "specification"
    WORK_INSTRUCTION = "work_instruction"
    INSPECTION_PLAN = "inspection_plan"
    CERTIFICATE = "certificate"
    MATERIAL_CERT = "material_cert"
    PROCEDURE = "procedure"
    QUALITY_RECORD = "quality_record"
    NCR = "ncr"  # Non-Conformance Report
    CAR = "car"  # Corrective Action Report
    FAI = "fai"  # First Article Inspection
    OTHER = "other"


class Document(Base):
    """
    Document management for ISO 9001 / AS9100D compliance.
    Tracks controlled documents and quality records.
    """
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Document identification
    document_number = Column(String(100), unique=True, index=True, nullable=False)
    revision = Column(String(20), default="A")
    title = Column(String(255), nullable=False)
    document_type = Column(SQLEnum(DocumentType), nullable=False)
    description = Column(Text)
    
    # Associated records
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    
    # File storage
    file_name = Column(String(255))
    file_path = Column(String(500))  # S3 path or local path
    file_size = Column(Integer)
    mime_type = Column(String(100))
    
    # Control status (ISO 9001 document control)
    status = Column(String(50), default="draft")  # draft, pending_approval, approved, released, obsolete
    is_controlled = Column(Boolean, default=True)  # Controlled vs uncontrolled copy
    
    # Effectivity
    effective_date = Column(DateTime, nullable=True)
    obsolete_date = Column(DateTime, nullable=True)
    review_date = Column(DateTime, nullable=True)  # Next review due
    
    # Approval workflow
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    released_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    released_at = Column(DateTime, nullable=True)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Change history notes
    revision_notes = Column(Text)
    
    # Relationships
    part = relationship("Part", back_populates="documents")
    vendor = relationship("Vendor", back_populates="documents")
