from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class NCRStatus(str, enum.Enum):
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    PENDING_DISPOSITION = "pending_disposition"
    CLOSED = "closed"
    VOID = "void"


class NCRDisposition(str, enum.Enum):
    USE_AS_IS = "use_as_is"
    REWORK = "rework"
    REPAIR = "repair"
    SCRAP = "scrap"
    RETURN_TO_VENDOR = "return_to_vendor"
    PENDING = "pending"


class NCRSource(str, enum.Enum):
    INCOMING_INSPECTION = "incoming_inspection"
    IN_PROCESS = "in_process"
    FINAL_INSPECTION = "final_inspection"
    CUSTOMER_RETURN = "customer_return"
    INTERNAL_AUDIT = "internal_audit"


class CARStatus(str, enum.Enum):
    OPEN = "open"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    CORRECTIVE_ACTION = "corrective_action"
    VERIFICATION = "verification"
    CLOSED = "closed"
    VOID = "void"


class CARType(str, enum.Enum):
    CORRECTIVE = "corrective"
    PREVENTIVE = "preventive"
    IMPROVEMENT = "improvement"


class FAIStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    CONDITIONAL = "conditional"


class NonConformanceReport(Base):
    """NCR - Non-Conformance Report for AS9100D compliance"""
    __tablename__ = "ncrs"
    
    id = Column(Integer, primary_key=True, index=True)
    ncr_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # What's affected
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    lot_number = Column(String(100))
    serial_number = Column(String(100))
    
    # Quantity
    quantity_affected = Column(Float, default=1.0)
    quantity_rejected = Column(Float, default=0.0)
    
    # Source and status
    source = Column(SQLEnum(NCRSource), nullable=False)
    status = Column(SQLEnum(NCRStatus), default=NCRStatus.OPEN)
    disposition = Column(SQLEnum(NCRDisposition), default=NCRDisposition.PENDING)
    
    # Description
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    root_cause = Column(Text)
    containment_action = Column(Text)
    
    # Inspection details
    specification = Column(String(255))  # What spec was violated
    actual_value = Column(String(255))  # What was measured
    required_value = Column(String(255))  # What it should be
    
    # Supplier info (if incoming)
    supplier_name = Column(String(255))
    supplier_lot = Column(String(100))
    po_number = Column(String(100))
    
    # Cost tracking
    estimated_cost = Column(Float, default=0.0)
    actual_cost = Column(Float, default=0.0)
    
    # Dates
    detected_date = Column(Date, default=datetime.utcnow)
    closed_date = Column(Date, nullable=True)
    
    # Responsibility
    detected_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    closed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Links to CAR
    car_required = Column(Boolean, default=False)
    car_id = Column(Integer, ForeignKey("cars.id"), nullable=True)
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    part = relationship("Part")
    work_order = relationship("WorkOrder")
    car = relationship("CorrectiveActionRequest", back_populates="ncrs", foreign_keys=[car_id])


class CorrectiveActionRequest(Base):
    """CAR - Corrective Action Request for AS9100D compliance"""
    __tablename__ = "cars"
    
    id = Column(Integer, primary_key=True, index=True)
    car_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # Classification
    car_type = Column(SQLEnum(CARType), default=CARType.CORRECTIVE)
    status = Column(SQLEnum(CARStatus), default=CARStatus.OPEN)
    priority = Column(Integer, default=3)  # 1=Critical, 2=Major, 3=Minor
    
    # Description
    title = Column(String(255), nullable=False)
    problem_description = Column(Text, nullable=False)
    
    # Root cause analysis (5-Why, Fishbone, etc.)
    root_cause_analysis = Column(Text)
    root_cause = Column(Text)
    
    # Actions
    containment_action = Column(Text)
    corrective_action = Column(Text)
    preventive_action = Column(Text)
    
    # Verification
    verification_method = Column(Text)
    verification_results = Column(Text)
    effectiveness_check = Column(Text)
    
    # Dates
    due_date = Column(Date, nullable=True)
    containment_due = Column(Date, nullable=True)
    corrective_due = Column(Date, nullable=True)
    verification_due = Column(Date, nullable=True)
    closed_date = Column(Date, nullable=True)
    
    # Responsibility
    initiated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    closed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    ncrs = relationship("NonConformanceReport", back_populates="car", foreign_keys="NonConformanceReport.car_id")


class FirstArticleInspection(Base):
    """FAI - First Article Inspection for AS9100D compliance (AS9102)"""
    __tablename__ = "fais"
    
    id = Column(Integer, primary_key=True, index=True)
    fai_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # What's being inspected
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    part_revision = Column(String(20))
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    serial_number = Column(String(100))
    
    # FAI Type
    fai_type = Column(String(50), default="full")  # full, partial, delta
    reason = Column(String(50))  # new_part, design_change, process_change, new_supplier
    
    # Status
    status = Column(SQLEnum(FAIStatus), default=FAIStatus.PENDING)
    
    # Results
    total_characteristics = Column(Integer, default=0)
    characteristics_passed = Column(Integer, default=0)
    characteristics_failed = Column(Integer, default=0)
    
    # Notes
    notes = Column(Text)
    deviations = Column(Text)
    
    # Dates
    inspection_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    completed_date = Column(Date, nullable=True)
    
    # Responsibility
    inspector_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Customer approval
    customer_approval_required = Column(Boolean, default=False)
    customer_approved = Column(Boolean, default=False)
    customer_approval_date = Column(Date, nullable=True)
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    part = relationship("Part")
    work_order = relationship("WorkOrder")
    characteristics = relationship("FAICharacteristic", back_populates="fai", cascade="all, delete-orphan")


class FAICharacteristic(Base):
    """Individual characteristic/dimension in FAI"""
    __tablename__ = "fai_characteristics"
    
    id = Column(Integer, primary_key=True, index=True)
    fai_id = Column(Integer, ForeignKey("fais.id"), nullable=False)
    
    # Characteristic info
    char_number = Column(Integer, nullable=False)  # Balloon number
    characteristic = Column(String(255), nullable=False)  # Description
    
    # Specification
    nominal = Column(String(100))
    tolerance_plus = Column(String(50))
    tolerance_minus = Column(String(50))
    specification = Column(String(255))  # Drawing callout
    
    # Measurement
    actual_value = Column(String(100))
    measuring_device = Column(String(255))
    
    # Result
    is_conforming = Column(Boolean, nullable=True)
    notes = Column(Text)
    
    # Designators
    is_critical = Column(Boolean, default=False)  # Critical characteristic
    is_major = Column(Boolean, default=False)  # Major characteristic
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    fai = relationship("FirstArticleInspection", back_populates="characteristics")
