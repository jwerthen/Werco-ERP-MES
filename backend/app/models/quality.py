import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


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


class CARInitiationReason(str, enum.Enum):
    """The "Reason for initiation" boxes on Werco's controlled CAR form (one per CAR).

    Stored as a plain ``String(40)`` held to these eight values by the CHECK
    ``ck_cars_initiation_reason`` (migration 122_car_form_quote_rev) rather than a
    native PostgreSQL enum, so adding a reason later is a CHECK swap, not an
    ``ALTER TYPE`` (precedent ``continuous_improvement.py``). A different concept from
    ``CARType`` (corrective / preventive / improvement).
    """

    MANAGEMENT_REVIEW = "management_review"
    NONCONFORMANCE_TREND = "nonconformance_trend"
    CUSTOMER_FEEDBACK = "customer_feedback"
    INTERNAL_AUDIT = "internal_audit"
    PRODUCTION_PROBLEMS = "production_problems"
    EXTERNAL_AUDIT = "external_audit"
    CONTINUOUS_IMPROVEMENT = "continuous_improvement"
    OTHER = "other"


# Lock-step with migration 122_car_form_quote_rev, which creates this CHECK with the
# identical text (the fresh-bootstrap round-trip compares the two).
CAR_INITIATION_REASON_CHECK = "initiation_reason IN ({})".format(
    ", ".join(f"'{reason.value}'" for reason in CARInitiationReason)
)


class FAIStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    CONDITIONAL = "conditional"


class NonConformanceReport(Base, SoftDeleteMixin, TenantMixin):
    """NCR - Non-Conformance Report for AS9100D compliance"""

    __tablename__ = "ncrs"
    # Lock-step with migration 079_restore_stamped_over_idx (originally migration
    # 001; skipped by the create_all+stamp bootstrap): open-NCR lists/gates,
    # newest-first status lists, and source/disposition rollups. 003's
    # ix_ncrs_status_source is deliberately NOT restored -- no query filters
    # status AND source together, and each column is covered below.
    # ``ncr_number`` is unique PER COMPANY (quality_numbering mints NCR-YYYYMMDD-NNN per
    # company), never globally; ``ix_ncrs_ncr_number`` stays as a plain lookup index.
    # Lock-step with migration 116_business_keys_per_company.
    __table_args__ = (
        Index("ix_ncrs_status", "status"),
        Index("ix_ncrs_status_created", "status", "created_at"),
        Index("ix_ncrs_source", "source"),
        Index("ix_ncrs_disposition", "disposition"),
        UniqueConstraint("company_id", "ncr_number", name="uq_ncrs_company_ncr_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ncr_number = Column(String(50), index=True, nullable=False)

    # What's affected
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    receipt_id = Column(Integer, ForeignKey("po_receipts.id"), nullable=True)  # Link to incoming inspection
    lot_number = Column(String(100), index=True)
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
    receipt = relationship("POReceipt")
    car = relationship("CorrectiveActionRequest", back_populates="ncrs", foreign_keys=[car_id])


class CorrectiveActionRequest(Base, TenantMixin):
    """CAR - Corrective Action Request for AS9100D compliance"""

    __tablename__ = "cars"
    # Lock-step with migration 079_restore_stamped_over_idx (originally migration
    # 001; skipped by the create_all+stamp bootstrap): open-CAR lists and
    # overdue-CAR reads.
    # ``car_number`` is unique PER COMPANY (quality_numbering mints CAR-YYYYMMDD-NNN per
    # company), never globally; ``ix_cars_car_number`` stays as a plain lookup index.
    # Lock-step with migration 116_business_keys_per_company.
    __table_args__ = (
        Index("ix_cars_status", "status"),
        Index("ix_cars_due_date", "due_date"),
        UniqueConstraint("company_id", "car_number", name="uq_cars_company_car_number"),
        # Lock-step with migration 122_car_form_quote_rev.
        CheckConstraint(CAR_INITIATION_REASON_CHECK, name="ck_cars_initiation_reason"),
    )

    id = Column(Integer, primary_key=True, index=True)
    car_number = Column(String(50), index=True, nullable=False)

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

    # Werco's controlled CAR form ("AS9100 PROCESS - Corrective Action Report"). Every column
    # below is nullable with no default and no backfill: a pre-122 CAR simply never recorded
    # them. Lock-step with migration 122_car_form_quote_rev (names, types, FK names, the one
    # index). Writes go through app/services/quality_record_service.py, which resolves each
    # reference tenant-scoped and records every change on the required audit row.
    initiation_reason = Column(String(40), nullable=True)  # one CARInitiationReason value
    initiation_reason_other = Column(String(255), nullable=True)  # required when the reason is "other"
    rma_number = Column(String(50), nullable=True)  # free text: a Werco RMA, a customer's, or paper back-entry
    submitted_date = Column(Date, nullable=True)  # "Date Submitted"; defaults to today (Central) at create
    part_id = Column(Integer, ForeignKey("parts.id", name="fk_cars_part_id"), nullable=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", name="fk_cars_customer_id"), nullable=True)
    customer_name = Column(String(255), nullable=True)  # snapshot of the customer at selection
    vendor_id = Column(Integer, ForeignKey("vendors.id", name="fk_cars_vendor_id"), nullable=True)
    vendor_po_number = Column(String(100), nullable=True)  # "P.O number (if C/A applies to a vendor)"
    # Cost of the corrective action. Blank is not zero, so each stays NULL until entered; the
    # total is computed at read time and never stored.
    cost_labor = Column(Numeric(12, 2), nullable=True)
    cost_material = Column(Numeric(12, 2), nullable=True)
    cost_outside_services = Column(Numeric(12, 2), nullable=True)
    cost_shipping = Column(Numeric(12, 2), nullable=True)
    management_action = Column(Text, nullable=True)
    vendor_approval_name = Column(String(255), nullable=True)
    vendor_approval_title = Column(String(255), nullable=True)
    vendor_approval_date = Column(Date, nullable=True)
    customer_approval_name = Column(String(255), nullable=True)
    customer_approval_date = Column(Date, nullable=True)
    # Quality Manager approval: server-stamped by POST /quality/car/{id}/quality-approval only,
    # never accepted from a request body.
    quality_approved_by = Column(Integer, ForeignKey("users.id", name="fk_cars_quality_approved_by"), nullable=True)
    quality_approved_at = Column(DateTime, nullable=True)

    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ncrs = relationship("NonConformanceReport", back_populates="car", foreign_keys="NonConformanceReport.car_id")


class FirstArticleInspection(Base, TenantMixin):
    """FAI - First Article Inspection for AS9100D compliance (AS9102)"""

    __tablename__ = "fais"
    # Lock-step with migration 079_restore_stamped_over_idx (originally migration
    # 001; skipped by the create_all+stamp bootstrap): open-FAI lists.
    # ``fai_number`` is unique PER COMPANY (fai_service mints FAI-YYYYMMDD-NNN per
    # company), never globally; ``ix_fais_fai_number`` stays as a plain lookup index.
    # Lock-step with migration 116_business_keys_per_company.
    __table_args__ = (
        Index("ix_fais_status", "status"),
        UniqueConstraint("company_id", "fai_number", name="uq_fais_company_fai_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    fai_number = Column(String(50), index=True, nullable=False)

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


class FAICharacteristic(Base, TenantMixin):
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
