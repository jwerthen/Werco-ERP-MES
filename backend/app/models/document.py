import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


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
    SHIPPING_LABEL = "shipping_label"  # Purchased carrier parcel label (PDF/PNG/ZPL)
    BILL_OF_LADING = "bill_of_lading"  # Purchased LTL freight Bill of Lading
    RECEIVING_LABEL = "receiving_label"  # 4x6 thermal label for received inventory (PDF)
    OTHER = "other"


class Document(Base, TenantMixin):
    """
    Document management for ISO 9001 / AS9100D compliance.
    Tracks controlled documents and quality records.
    """

    __tablename__ = "documents"
    # Lock-step with migration 078_golive_perf_indexes: NON-unique PARTIAL indexes
    # backing the document list filters (documents.py: WHERE company_id = ? AND
    # part_id/work_order_id = ?) and the kiosk operation-open per-part
    # controlled-drawing lookup (shop_floor.py). Partial because the association FKs
    # are sparse and every serving query filters on their equality (which implies IS
    # NOT NULL). Both declare postgresql_where AND sqlite_where from the same literal
    # so the SQLite create_all path builds the same partial shape (the 076
    # dialect-parity convention, see inventory.py).
    __table_args__ = (
        Index(
            "ix_documents_company_part",
            "company_id",
            "part_id",
            postgresql_where=text("part_id IS NOT NULL"),
            sqlite_where=text("part_id IS NOT NULL"),
        ),
        Index(
            "ix_documents_company_work_order",
            "company_id",
            "work_order_id",
            postgresql_where=text("work_order_id IS NOT NULL"),
            sqlite_where=text("work_order_id IS NOT NULL"),
        ),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migration 020; skipped by the create_all+stamp bootstrap): the
        # vendor<->documents association lookups (vendor document lists).
        Index("ix_documents_vendor_id", "vendor_id"),
    )

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
