from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime, date
import enum
from app.db.database import Base


class ComplaintStatus(str, enum.Enum):
    RECEIVED = "received"
    UNDER_INVESTIGATION = "under_investigation"
    PENDING_RESOLUTION = "pending_resolution"
    RESOLVED = "resolved"
    CLOSED = "closed"
    VOID = "void"


class ComplaintSeverity(str, enum.Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    COSMETIC = "cosmetic"


class RMAStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    MATERIAL_RECEIVED = "material_received"
    UNDER_INSPECTION = "under_inspection"
    DISPOSITION_DECIDED = "disposition_decided"
    COMPLETED = "completed"
    DENIED = "denied"


class CustomerComplaint(Base):
    """Customer Complaint tracking for AS9100D compliance"""
    __tablename__ = "customer_complaints"

    id = Column(Integer, primary_key=True, index=True)
    complaint_number = Column(String(50), unique=True, index=True, nullable=False)

    # Customer info
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    customer_name = Column(String(255), nullable=False)
    customer_po_number = Column(String(100), nullable=True)
    customer_contact = Column(String(255), nullable=True)

    # Affected product
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    lot_number = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)
    quantity_affected = Column(Float, default=1)

    # Classification
    severity = Column(SQLEnum(ComplaintSeverity), default=ComplaintSeverity.MINOR)
    status = Column(SQLEnum(ComplaintStatus), default=ComplaintStatus.RECEIVED)

    # Description
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)

    # Dates
    date_received = Column(Date, default=date.today)
    date_of_occurrence = Column(Date, nullable=True)

    # Investigation & resolution
    investigation_findings = Column(Text, nullable=True)
    root_cause = Column(Text, nullable=True)
    containment_action = Column(Text, nullable=True)
    corrective_action = Column(Text, nullable=True)
    preventive_action = Column(Text, nullable=True)
    resolution_description = Column(Text, nullable=True)

    # Links to NCR / CAR
    ncr_id = Column(Integer, ForeignKey("ncrs.id"), nullable=True)
    car_id = Column(Integer, ForeignKey("cars.id"), nullable=True)

    # Cost tracking
    estimated_cost = Column(Float, default=0)
    actual_cost = Column(Float, default=0)

    # Responsibility
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    received_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Resolution
    resolved_date = Column(Date, nullable=True)
    closed_date = Column(Date, nullable=True)

    # Customer satisfaction
    customer_satisfied = Column(Boolean, nullable=True)
    satisfaction_notes = Column(Text, nullable=True)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    part = relationship("Part", foreign_keys=[part_id])
    customer = relationship("Customer", foreign_keys=[customer_id])
    work_order = relationship("WorkOrder", foreign_keys=[work_order_id])
    ncr = relationship("NonConformanceReport", foreign_keys=[ncr_id])
    car = relationship("CorrectiveActionRequest", foreign_keys=[car_id])
    rmas = relationship("ReturnMaterialAuthorization", back_populates="complaint")


class ReturnMaterialAuthorization(Base):
    """RMA - Return Material Authorization tracking"""
    __tablename__ = "return_material_authorizations"

    id = Column(Integer, primary_key=True, index=True)
    rma_number = Column(String(50), unique=True, index=True, nullable=False)

    # Link to complaint
    complaint_id = Column(Integer, ForeignKey("customer_complaints.id"), nullable=True)

    # Customer info
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    customer_name = Column(String(255), nullable=False)

    # Product
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)

    # Status
    status = Column(SQLEnum(RMAStatus), default=RMAStatus.REQUESTED)

    # Details
    quantity = Column(Float, nullable=False)
    lot_number = Column(String(100), nullable=True)
    reason = Column(Text, nullable=False)

    # Disposition: replace, repair, credit, scrap, return_to_customer
    disposition = Column(String(100), nullable=True)

    # Shipping
    shipping_tracking = Column(String(255), nullable=True)

    # Inspection
    received_date = Column(Date, nullable=True)
    inspection_date = Column(Date, nullable=True)
    inspection_findings = Column(Text, nullable=True)

    # Resolution
    replacement_wo_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    credit_amount = Column(Float, default=0)

    # Authorization
    authorized_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    authorized_date = Column(Date, nullable=True)

    # Notes
    notes = Column(Text, nullable=True)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    complaint = relationship("CustomerComplaint", back_populates="rmas", foreign_keys=[complaint_id])
    customer = relationship("Customer", foreign_keys=[customer_id])
    part = relationship("Part", foreign_keys=[part_id])
    replacement_wo = relationship("WorkOrder", foreign_keys=[replacement_wo_id])
