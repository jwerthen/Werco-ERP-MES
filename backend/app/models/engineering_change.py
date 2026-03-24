from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class ECOStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    IN_IMPLEMENTATION = "in_implementation"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ECOPriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ECOType(str, enum.Enum):
    DESIGN_CHANGE = "design_change"
    PROCESS_CHANGE = "process_change"
    MATERIAL_CHANGE = "material_change"
    DOCUMENTATION = "documentation"
    SUPPLIER_CHANGE = "supplier_change"
    COST_REDUCTION = "cost_reduction"
    QUALITY_IMPROVEMENT = "quality_improvement"


class EngineeringChangeOrder(Base):
    """Engineering Change Order (ECO/ECN) for AS9100D compliance"""
    __tablename__ = "engineering_change_orders"

    id = Column(Integer, primary_key=True, index=True)
    eco_number = Column(String(50), unique=True, index=True, nullable=False)

    # Core fields
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    eco_type = Column(SQLEnum(ECOType), nullable=False)
    priority = Column(SQLEnum(ECOPriority), default=ECOPriority.MEDIUM)
    status = Column(SQLEnum(ECOStatus), default=ECOStatus.DRAFT)

    # Change details
    reason_for_change = Column(Text, nullable=False)
    proposed_solution = Column(Text)
    impact_analysis = Column(Text)
    risk_assessment = Column(Text)

    # Affected items (stored as JSON text)
    affected_parts = Column(Text)  # JSON list of part IDs
    affected_work_orders = Column(Text)  # JSON list of work order IDs
    affected_documents = Column(Text)  # JSON list of document IDs

    # Cost tracking
    estimated_cost = Column(Float, default=0)
    actual_cost = Column(Float, default=0)

    # Effectivity
    effectivity_type = Column(String(50))  # "date", "serial_number", "lot_number"
    effectivity_date = Column(Date, nullable=True)
    effectivity_serial = Column(String(100), nullable=True)

    # People
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_date = Column(DateTime, nullable=True)

    # Dates
    target_date = Column(Date, nullable=True)
    completed_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    requester = relationship("User", foreign_keys=[requested_by], backref="requested_ecos")
    assignee = relationship("User", foreign_keys=[assigned_to], backref="assigned_ecos")
    approver = relationship("User", foreign_keys=[approved_by], backref="approved_ecos")
    approvals = relationship("ECOApproval", back_populates="eco", cascade="all, delete-orphan")
    implementation_tasks = relationship("ECOImplementationTask", back_populates="eco", cascade="all, delete-orphan")


class ECOApproval(Base):
    """Approval workflow for ECOs"""
    __tablename__ = "eco_approvals"

    id = Column(Integer, primary_key=True, index=True)
    eco_id = Column(Integer, ForeignKey("engineering_change_orders.id"), nullable=False)
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String(100), nullable=False)  # "Engineering", "Quality", "Production", "Management"
    status = Column(String(50), default="pending")  # "pending", "approved", "rejected"
    comments = Column(Text)
    decision_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    eco = relationship("EngineeringChangeOrder", back_populates="approvals")
    approver = relationship("User", backref="eco_approvals")


class ECOImplementationTask(Base):
    """Implementation tasks for ECOs"""
    __tablename__ = "eco_implementation_tasks"

    id = Column(Integer, primary_key=True, index=True)
    eco_id = Column(Integer, ForeignKey("engineering_change_orders.id"), nullable=False)
    task_number = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    department = Column(String(100))
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(50), default="pending")  # "pending", "in_progress", "completed", "skipped"
    due_date = Column(Date, nullable=True)
    completed_date = Column(Date, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    eco = relationship("EngineeringChangeOrder", back_populates="implementation_tasks")
    assignee = relationship("User", backref="eco_tasks")
