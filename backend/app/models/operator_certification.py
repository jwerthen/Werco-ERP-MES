from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base


class CertificationType(str, enum.Enum):
    WELDING = "welding"
    NDT = "ndt"
    CNC_OPERATION = "cnc_operation"
    INSPECTION = "inspection"
    FORKLIFT = "forklift"
    CRANE = "crane"
    SAFETY = "safety"
    PROCESS_SPECIFIC = "process_specific"
    QUALITY_SYSTEM = "quality_system"
    OTHER = "other"


class CertificationStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    REVOKED = "revoked"
    PENDING = "pending"


class OperatorCertification(Base):
    """Operator certification records"""
    __tablename__ = "operator_certifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    certification_type = Column(SQLEnum(CertificationType), nullable=False)
    certification_name = Column(String(255), nullable=False)
    issuing_authority = Column(String(255))
    certificate_number = Column(String(100))
    issue_date = Column(Date)
    expiration_date = Column(Date, nullable=True)  # null means no expiry
    status = Column(SQLEnum(CertificationStatus), default=CertificationStatus.ACTIVE)
    level = Column(String(50))  # e.g. "Level I", "Level II", "Journeyman"
    scope = Column(Text)  # what exactly are they certified for
    document_reference = Column(String(255))  # link to uploaded cert document
    notes = Column(Text)
    verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_date = Column(Date, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
    verifier = relationship("User", foreign_keys=[verified_by])


class TrainingRecord(Base):
    """Training records for operators"""
    __tablename__ = "training_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    training_name = Column(String(255), nullable=False)
    training_type = Column(String(100))  # "Initial", "Refresher", "OJT", "Classroom", "Online"
    description = Column(Text)
    trainer = Column(String(255))
    training_date = Column(Date, nullable=False)
    completion_date = Column(Date, nullable=True)
    hours = Column(Float)
    passed = Column(Boolean, default=True)
    score = Column(Float, nullable=True)  # test score if applicable
    certificate_number = Column(String(100), nullable=True)
    expiration_date = Column(Date, nullable=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=True)
    notes = Column(Text)
    recorded_by = Column(Integer, ForeignKey("users.id"))

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
    recorder = relationship("User", foreign_keys=[recorded_by])
    work_center = relationship("WorkCenter")


class SkillMatrix(Base):
    """Links operators to work centers they are qualified for"""
    __tablename__ = "skill_matrix"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False, index=True)
    skill_level = Column(Integer, nullable=False)  # 1=Trainee, 2=Basic, 3=Proficient, 4=Advanced, 5=Expert
    qualified_date = Column(Date)
    last_assessment_date = Column(Date, nullable=True)
    next_assessment_date = Column(Date, nullable=True)
    notes = Column(Text)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
    work_center = relationship("WorkCenter")
    approver = relationship("User", foreign_keys=[approved_by])

    __table_args__ = (
        UniqueConstraint('user_id', 'work_center_id', name='uq_user_work_center'),
    )
