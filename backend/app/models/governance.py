import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class DataClassification(str, enum.Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    BUSINESS_CONFIDENTIAL = "business_confidential"
    FCI = "fci"
    CUI = "cui"
    ITAR_EXPORT_CONTROLLED = "itar_export_controlled"
    RESTRICTED_CUSTOMER = "restricted_customer"
    UNKNOWN = "unknown"


class ClassificationReviewType(str, enum.Enum):
    ASSIGNMENT = "assignment"
    CONFIRMATION = "confirmation"
    ESCALATION = "escalation"
    DOWNGRADE = "downgrade"
    OVERRIDE = "override"


class HandlingGroupRole(str, enum.Enum):
    MEMBER = "member"
    REVIEWER = "reviewer"
    MANAGER = "manager"


DEFAULT_RETENTION_POLICIES = [
    {
        "policy_key": "customer_contract_record",
        "name": "Customer Contract Record",
        "description": "Customer contracts, customer-specific handling instructions, and controlled program metadata.",
        "default_retention_days": 2555,
        "retention_basis": "Contract term plus 7 years, or customer requirement if longer.",
        "retention_trigger": "contract_close_or_expiration",
        "applies_to_record_types": ["customer_contracts", "customer_handling_instructions"],
    },
    {
        "policy_key": "rfq_quote_record",
        "name": "RFQ And Quote Record",
        "description": "RFQs, quote packages, estimates, quote lines, and generated quote exports.",
        "default_retention_days": 2555,
        "retention_basis": "7 years after quote close, no-bid, win, or loss, or customer requirement if longer.",
        "retention_trigger": "quote_close",
        "applies_to_record_types": ["rfq_packages", "rfq_package_files", "quotes", "quote_estimates"],
    },
    {
        "policy_key": "controlled_document",
        "name": "Controlled Document",
        "description": "Drawings, specifications, work instructions, controlled procedures, and customer documents.",
        "default_retention_days": 3650,
        "retention_basis": "Life of part or program plus 10 years, or customer requirement if longer.",
        "retention_trigger": "part_or_program_end",
        "applies_to_record_types": ["documents", "document_files"],
    },
    {
        "policy_key": "engineering_record",
        "name": "Engineering Record",
        "description": "Parts, revisions, BOMs, routings, ECOs, approvals, and engineering release evidence.",
        "default_retention_days": 3650,
        "retention_basis": "Life of part or program plus 10 years.",
        "retention_trigger": "part_or_program_end",
        "applies_to_record_types": ["parts", "boms", "routings", "engineering_change_orders"],
    },
    {
        "policy_key": "production_record",
        "name": "Production Record",
        "description": "Work orders, released operation snapshots, travelers, job records, and traceability records.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment or completion, or customer requirement if longer.",
        "retention_trigger": "shipment_or_completion",
        "applies_to_record_types": ["work_orders", "work_order_operations", "jobs"],
    },
    {
        "policy_key": "quality_record",
        "name": "Quality Record",
        "description": "FAIs, inspection records, NCRs, CARs, SPC, and product quality evidence.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment or closure, or customer requirement if longer.",
        "retention_trigger": "shipment_or_closure",
        "applies_to_record_types": ["fais", "fai_characteristics", "ncrs", "cars", "spc_measurements"],
    },
    {
        "policy_key": "purchasing_receiving_record",
        "name": "Purchasing And Receiving Record",
        "description": "Purchase orders, receipts, supplier quality records, and material certificate metadata.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after receipt or shipment linkage, or customer requirement if longer.",
        "retention_trigger": "receipt_or_shipment_linkage",
        "applies_to_record_types": ["purchase_orders", "purchase_order_lines", "po_receipts"],
    },
    {
        "policy_key": "shipping_record",
        "name": "Shipping Record",
        "description": "Shipments, packing records, and customer delivery evidence.",
        "default_retention_days": 3650,
        "retention_basis": "10 years after shipment, or customer requirement if longer.",
        "retention_trigger": "shipment",
        "applies_to_record_types": ["shipments"],
    },
    {
        "policy_key": "training_record",
        "name": "Training Record",
        "description": "Operator certification, training, skill matrix, and security training evidence.",
        "default_retention_days": 2555,
        "retention_basis": "Employment term plus 7 years.",
        "retention_trigger": "employment_end",
        "applies_to_record_types": ["operator_certifications", "training_records", "skill_matrix"],
    },
    {
        "policy_key": "security_audit_record",
        "name": "Security Audit Record",
        "description": "Login events, access control changes, CUI access/export logs, and admin actions.",
        "default_retention_days": 1095,
        "retention_basis": "Minimum 1 year online and 3 years retained; longer if required.",
        "retention_trigger": "event_timestamp",
        "applies_to_record_types": ["audit_logs", "controlled_access_events", "export_events"],
    },
    {
        "policy_key": "application_audit_record",
        "name": "Application Audit Record",
        "description": "Business object create, update, release, void, supersede, and archive audit records.",
        "default_retention_days": None,
        "retention_basis": "Match parent record retention when tied to retained evidence.",
        "retention_trigger": "parent_record_retention",
        "applies_to_record_types": ["audit_logs", "classification_reviews", "legal_holds"],
    },
    {
        "policy_key": "temporary_import_processing",
        "name": "Temporary Import Processing",
        "description": "Temporary parsed files, staging data, OCR/AI extraction artifacts, and intermediate import output.",
        "default_retention_days": 90,
        "retention_basis": "Delete after successful processing and verification, normally within 30-90 days.",
        "retention_trigger": "processing_complete",
        "applies_to_record_types": ["temporary_processing", "rfq_extraction_artifacts"],
        "requires_legal_review_before_purge": False,
    },
]


CLASSIFICATION_VALUES = tuple(item.value for item in DataClassification)


class RetentionPolicy(Base, TenantMixin):
    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("company_id", "policy_key", name="uq_retention_policies_company_key"),
        Index("ix_retention_policies_company_active", "company_id", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    policy_key = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    default_retention_days = Column(Integer, nullable=True)
    retention_basis = Column(Text, nullable=False)
    retention_trigger = Column(String(100), nullable=False)
    applies_to_record_types = Column(JSON, nullable=True)
    requires_legal_review_before_purge = Column(Boolean, default=True, nullable=False)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CustomerContract(Base, TenantMixin):
    __tablename__ = "customer_contracts"
    __table_args__ = (
        UniqueConstraint("company_id", "contract_number", name="uq_customer_contracts_company_number"),
        Index("ix_customer_contracts_company_customer", "company_id", "customer_id"),
        CheckConstraint(
            f"default_data_classification in {CLASSIFICATION_VALUES}",
            name="ck_customer_contracts_default_classification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    contract_number = Column(String(120), nullable=True, index=True)
    contract_name = Column(String(255), nullable=False)
    default_data_classification = Column(String(50), default=DataClassification.INTERNAL.value, nullable=False)
    contains_cui = Column(Boolean, default=False, nullable=False)
    contains_fci = Column(Boolean, default=False, nullable=False)
    export_controlled = Column(Boolean, default=False, nullable=False)
    itar_controlled = Column(Boolean, default=False, nullable=False)
    dfars_clause_reference = Column(String(255), nullable=True)
    cui_categories = Column(JSON, nullable=True)
    handling_instructions = Column(Text, nullable=True)
    retention_policy_id = Column(Integer, ForeignKey("retention_policies.id"), nullable=True)
    effective_date = Column(DateTime, nullable=True)
    expiration_date = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    retention_policy = relationship("RetentionPolicy")


class CustomerHandlingInstruction(Base, TenantMixin):
    __tablename__ = "customer_handling_instructions"
    __table_args__ = (
        Index("ix_customer_handling_instructions_company_customer", "company_id", "customer_id"),
        CheckConstraint(
            f"default_data_classification in {CLASSIFICATION_VALUES}",
            name="ck_customer_handling_instructions_default_classification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    contract_id = Column(Integer, ForeignKey("customer_contracts.id"), nullable=True, index=True)
    instruction_type = Column(String(100), nullable=False)
    instruction_text = Column(Text, nullable=False)
    default_data_classification = Column(String(50), default=DataClassification.UNKNOWN.value, nullable=False)
    requires_marking = Column(Boolean, default=False, nullable=False)
    requires_export_review = Column(Boolean, default=False, nullable=False)
    retention_policy_id = Column(Integer, ForeignKey("retention_policies.id"), nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    contract = relationship("CustomerContract")
    retention_policy = relationship("RetentionPolicy")


class HandlingGroup(Base, TenantMixin):
    __tablename__ = "handling_groups"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_handling_groups_company_name"),
        Index("ix_handling_groups_company_active", "company_id", "active"),
        CheckConstraint(
            f"classification_scope in {CLASSIFICATION_VALUES}",
            name="ck_handling_groups_classification_scope",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    classification_scope = Column(String(50), default=DataClassification.UNKNOWN.value, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    contract_id = Column(Integer, ForeignKey("customer_contracts.id"), nullable=True, index=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    contract = relationship("CustomerContract")
    members = relationship("HandlingGroupMember", back_populates="handling_group", cascade="all, delete-orphan")


class HandlingGroupMember(Base, TenantMixin):
    __tablename__ = "handling_group_members"
    __table_args__ = (
        UniqueConstraint("handling_group_id", "user_id", name="uq_handling_group_members_group_user"),
        Index("ix_handling_group_members_company_active", "company_id", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    handling_group_id = Column(Integer, ForeignKey("handling_groups.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    membership_role = Column(String(50), default=HandlingGroupRole.MEMBER.value, nullable=False)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    handling_group = relationship("HandlingGroup", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class ClassificationReview(Base, TenantMixin):
    __tablename__ = "classification_reviews"
    __table_args__ = (
        Index("ix_classification_reviews_company_record", "company_id", "record_type", "record_id"),
        CheckConstraint(
            f"new_classification in {CLASSIFICATION_VALUES}",
            name="ck_classification_reviews_new_classification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    record_type = Column(String(100), nullable=False)
    record_id = Column(Integer, nullable=False)
    previous_classification = Column(String(50), nullable=True)
    new_classification = Column(String(50), nullable=False)
    review_type = Column(String(50), default=ClassificationReviewType.ASSIGNMENT.value, nullable=False)
    justification = Column(Text, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    second_approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    second_approved_at = Column(DateTime, nullable=True)
    extra_data = Column(JSON, nullable=True)


class DocumentFile(Base, TenantMixin):
    __tablename__ = "document_files"
    __table_args__ = (
        Index("ix_document_files_company_parent", "company_id", "parent_record_type", "parent_record_id"),
        Index("ix_document_files_company_hash", "company_id", "content_sha256"),
        CheckConstraint(
            f"file_classification in {CLASSIFICATION_VALUES}",
            name="ck_document_files_file_classification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    parent_record_type = Column(String(100), nullable=False, index=True)
    parent_record_id = Column(Integer, nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)
    rfq_package_file_id = Column(Integer, ForeignKey("rfq_package_files.id"), nullable=True, index=True)
    document_revision = Column(String(50), nullable=True)
    storage_provider = Column(String(50), default="local", nullable=False)
    storage_container = Column(String(120), nullable=True)
    storage_key = Column(String(1000), nullable=False)
    original_file_name = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=True)
    mime_type = Column(String(120), nullable=True)
    content_sha256 = Column(String(64), nullable=True, index=True)
    file_purpose = Column(String(100), nullable=True)
    file_classification = Column(String(50), default=DataClassification.UNKNOWN.value, nullable=False)
    retention_policy_id = Column(Integer, ForeignKey("retention_policies.id"), nullable=True)
    legal_hold_active = Column(Boolean, default=False, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    released_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    released_at = Column(DateTime, nullable=True)
    obsolete_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    obsolete_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    retention_policy = relationship("RetentionPolicy")


class LegalHold(Base, TenantMixin):
    __tablename__ = "legal_holds"
    __table_args__ = (
        Index("ix_legal_holds_company_record", "company_id", "record_type", "record_id"),
        Index("ix_legal_holds_company_active", "company_id", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    record_type = Column(String(100), nullable=False)
    record_id = Column(Integer, nullable=False)
    hold_reason = Column(Text, nullable=False)
    hold_owner = Column(String(255), nullable=True)
    placed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    placed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    released_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    released_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)


class ExportEvent(Base, TenantMixin):
    __tablename__ = "export_events"
    __table_args__ = (
        Index("ix_export_events_company_record", "company_id", "record_type", "record_id"),
        Index("ix_export_events_company_exported_at", "company_id", "exported_at"),
        CheckConstraint(
            f"data_classification in {CLASSIFICATION_VALUES}",
            name="ck_export_events_data_classification",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    record_type = Column(String(100), nullable=False)
    record_id = Column(Integer, nullable=False)
    export_type = Column(String(100), nullable=False)
    export_format = Column(String(50), nullable=True)
    data_classification = Column(String(50), default=DataClassification.UNKNOWN.value, nullable=False)
    included_record_refs = Column(JSON, nullable=True)
    generated_file_id = Column(Integer, ForeignKey("document_files.id"), nullable=True)
    export_reason = Column(Text, nullable=True)
    exported_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    exported_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    destination_type = Column(String(100), nullable=True)
    destination_reference = Column(String(500), nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    extra_data = Column(JSON, nullable=True)

    generated_file = relationship("DocumentFile")


class ControlledAccessEvent(Base, TenantMixin):
    __tablename__ = "controlled_access_events"
    __table_args__ = (
        Index("ix_controlled_access_events_company_record", "company_id", "record_type", "record_id"),
        Index("ix_controlled_access_events_company_occurred_at", "company_id", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    record_type = Column(String(100), nullable=False)
    record_id = Column(Integer, nullable=True)
    file_id = Column(Integer, ForeignKey("document_files.id"), nullable=True)
    action = Column(String(100), nullable=False)
    allowed = Column(Boolean, nullable=False)
    denial_reason = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    request_id = Column(String(100), nullable=True)
    source_ip = Column(String(45), nullable=True)
    data_classification = Column(String(50), default=DataClassification.UNKNOWN.value, nullable=False)
    extra_data = Column(JSON, nullable=True)

    file = relationship("DocumentFile")
