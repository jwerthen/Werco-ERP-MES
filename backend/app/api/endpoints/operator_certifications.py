from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.operator_certification import (
    CertificationStatus,
    CertificationType,
    OperatorCertification,
    SkillMatrix,
    TrainingRecord,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService

# RBAC write-role sets (read-broad / write-restricted, per docs/RBAC_PERMISSIONS.md).
# The RBAC matrix has no dedicated Certifications / Training / Skill-Matrix table, so these
# default to the documented convention for these record classes:
#   * Certification + Training writes are operator-qualification / conformance records that
#     Quality owns alongside Admin/Manager.
#   * Skill-matrix writes are competency assessments performed by Supervisors (and above).
# READ endpoints stay on ``get_current_user`` (tenant-scoped, any authenticated user).
CERT_TRAINING_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]
SKILL_MATRIX_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

router = APIRouter()


# ==================== FK Validation Helpers ====================


def _require_company_user(db: Session, user_id: int, company_id: int) -> None:
    """Reject a user_id FK that does not resolve to a user in the active company.

    Guards against cross-tenant FK injection on create: a caller must not be able to attach a
    certification / training / skill-matrix row to another tenant's user. 422 (input validation)
    matches FastAPI's convention for a bad request body before insert.
    """
    exists = db.query(User.id).filter(User.id == user_id, User.company_id == company_id).first()
    if not exists:
        raise HTTPException(status_code=422, detail="user_id does not reference a user in your company")


def _require_company_work_center(db: Session, work_center_id: int, company_id: int) -> None:
    """Reject a work_center_id FK that does not resolve to a work center in the active company."""
    exists = (
        db.query(WorkCenter.id).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not exists:
        raise HTTPException(status_code=422, detail="work_center_id does not reference a work center in your company")


# ==================== Pydantic Schemas ====================


class CertificationCreate(BaseModel):
    user_id: int
    certification_type: str
    certification_name: str
    issuing_authority: Optional[str] = None
    certificate_number: Optional[str] = None
    issue_date: Optional[date] = None
    expiration_date: Optional[date] = None
    status: str = "active"
    level: Optional[str] = None
    scope: Optional[str] = None
    document_reference: Optional[str] = None
    notes: Optional[str] = None


class CertificationUpdate(BaseModel):
    certification_type: Optional[str] = None
    certification_name: Optional[str] = None
    issuing_authority: Optional[str] = None
    certificate_number: Optional[str] = None
    issue_date: Optional[date] = None
    expiration_date: Optional[date] = None
    status: Optional[str] = None
    level: Optional[str] = None
    scope: Optional[str] = None
    document_reference: Optional[str] = None
    notes: Optional[str] = None
    verified_by: Optional[int] = None
    verified_date: Optional[date] = None


class TrainingCreate(BaseModel):
    user_id: int
    training_name: str
    training_type: Optional[str] = None
    description: Optional[str] = None
    trainer: Optional[str] = None
    training_date: date
    completion_date: Optional[date] = None
    hours: Optional[float] = None
    passed: bool = True
    score: Optional[float] = None
    certificate_number: Optional[str] = None
    expiration_date: Optional[date] = None
    work_center_id: Optional[int] = None
    notes: Optional[str] = None


class TrainingUpdate(BaseModel):
    training_name: Optional[str] = None
    training_type: Optional[str] = None
    description: Optional[str] = None
    trainer: Optional[str] = None
    training_date: Optional[date] = None
    completion_date: Optional[date] = None
    hours: Optional[float] = None
    passed: Optional[bool] = None
    score: Optional[float] = None
    certificate_number: Optional[str] = None
    expiration_date: Optional[date] = None
    work_center_id: Optional[int] = None
    notes: Optional[str] = None


class SkillMatrixCreate(BaseModel):
    user_id: int
    work_center_id: int
    skill_level: int
    qualified_date: Optional[date] = None
    last_assessment_date: Optional[date] = None
    next_assessment_date: Optional[date] = None
    notes: Optional[str] = None


class SkillMatrixUpdate(BaseModel):
    skill_level: Optional[int] = None
    qualified_date: Optional[date] = None
    last_assessment_date: Optional[date] = None
    next_assessment_date: Optional[date] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


# ==================== Helper Functions ====================


def compute_cert_status(cert: OperatorCertification) -> str:
    """Compute the effective status of a certification based on expiration date."""
    if cert.status == CertificationStatus.REVOKED:
        return "revoked"
    if cert.status == CertificationStatus.PENDING:
        return "pending"
    if cert.expiration_date is None:
        return "active"
    today = date.today()
    if cert.expiration_date < today:
        return "expired"
    if cert.expiration_date <= today + timedelta(days=30):
        return "expiring_soon"
    return "active"


def serialize_cert(cert: OperatorCertification, db: Session) -> dict:
    """Serialize a certification to a dict with user info."""
    user = db.query(User).filter(User.id == cert.user_id).first()
    verifier = None
    if cert.verified_by:
        verifier = db.query(User).filter(User.id == cert.verified_by).first()

    effective_status = compute_cert_status(cert)

    return {
        "id": cert.id,
        "user_id": cert.user_id,
        "user_name": f"{user.first_name} {user.last_name}" if user else "Unknown",
        "employee_id": user.employee_id if user else None,
        "certification_type": (
            cert.certification_type.value if hasattr(cert.certification_type, 'value') else cert.certification_type
        ),
        "certification_name": cert.certification_name,
        "issuing_authority": cert.issuing_authority,
        "certificate_number": cert.certificate_number,
        "issue_date": cert.issue_date.isoformat() if cert.issue_date else None,
        "expiration_date": cert.expiration_date.isoformat() if cert.expiration_date else None,
        "status": effective_status,
        "level": cert.level,
        "scope": cert.scope,
        "document_reference": cert.document_reference,
        "notes": cert.notes,
        "verified_by": cert.verified_by,
        "verified_by_name": f"{verifier.first_name} {verifier.last_name}" if verifier else None,
        "verified_date": cert.verified_date.isoformat() if cert.verified_date else None,
        "days_until_expiry": (cert.expiration_date - date.today()).days if cert.expiration_date else None,
        "created_at": cert.created_at.isoformat() if cert.created_at else None,
        "updated_at": cert.updated_at.isoformat() if cert.updated_at else None,
    }


def serialize_training(record: TrainingRecord, db: Session) -> dict:
    """Serialize a training record to a dict with user info."""
    user = db.query(User).filter(User.id == record.user_id).first()
    recorder = None
    if record.recorded_by:
        recorder = db.query(User).filter(User.id == record.recorded_by).first()
    wc = None
    if record.work_center_id:
        wc = db.query(WorkCenter).filter(WorkCenter.id == record.work_center_id).first()

    return {
        "id": record.id,
        "user_id": record.user_id,
        "user_name": f"{user.first_name} {user.last_name}" if user else "Unknown",
        "employee_id": user.employee_id if user else None,
        "training_name": record.training_name,
        "training_type": record.training_type,
        "description": record.description,
        "trainer": record.trainer,
        "training_date": record.training_date.isoformat() if record.training_date else None,
        "completion_date": record.completion_date.isoformat() if record.completion_date else None,
        "hours": record.hours,
        "passed": record.passed,
        "score": record.score,
        "certificate_number": record.certificate_number,
        "expiration_date": record.expiration_date.isoformat() if record.expiration_date else None,
        "work_center_id": record.work_center_id,
        "work_center_name": f"{wc.code} - {wc.name}" if wc else None,
        "notes": record.notes,
        "recorded_by": record.recorded_by,
        "recorded_by_name": f"{recorder.first_name} {recorder.last_name}" if recorder else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def serialize_skill(entry: SkillMatrix, db: Session) -> dict:
    """Serialize a skill matrix entry to a dict."""
    user = db.query(User).filter(User.id == entry.user_id).first()
    wc = db.query(WorkCenter).filter(WorkCenter.id == entry.work_center_id).first()
    approver = None
    if entry.approved_by:
        approver = db.query(User).filter(User.id == entry.approved_by).first()

    return {
        "id": entry.id,
        "user_id": entry.user_id,
        "user_name": f"{user.first_name} {user.last_name}" if user else "Unknown",
        "employee_id": user.employee_id if user else None,
        "work_center_id": entry.work_center_id,
        "work_center_code": wc.code if wc else None,
        "work_center_name": wc.name if wc else None,
        "skill_level": entry.skill_level,
        "qualified_date": entry.qualified_date.isoformat() if entry.qualified_date else None,
        "last_assessment_date": entry.last_assessment_date.isoformat() if entry.last_assessment_date else None,
        "next_assessment_date": entry.next_assessment_date.isoformat() if entry.next_assessment_date else None,
        "notes": entry.notes,
        "approved_by": entry.approved_by,
        "approved_by_name": f"{approver.first_name} {approver.last_name}" if approver else None,
        "is_active": entry.is_active,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


# ==================== Certification Endpoints ====================


@router.get("/certifications/dashboard")
def certification_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Dashboard: expiring certs count, expired count, operators without certs, compliance rate"""
    today = date.today()
    soon = today + timedelta(days=30)

    all_certs = db.query(OperatorCertification).filter(OperatorCertification.company_id == company_id).all()

    expired_count = 0
    expiring_soon_count = 0
    active_count = 0
    revoked_count = 0

    for cert in all_certs:
        status = compute_cert_status(cert)
        if status == "expired":
            expired_count += 1
        elif status == "expiring_soon":
            expiring_soon_count += 1
        elif status == "active":
            active_count += 1
        elif status == "revoked":
            revoked_count += 1

    total_certs = len(all_certs)
    compliance_rate = round((active_count / total_certs * 100), 1) if total_certs > 0 else 100.0

    # Operators with at least one certification
    operators_with_certs = (
        db.query(func.count(func.distinct(OperatorCertification.user_id)))
        .filter(OperatorCertification.company_id == company_id)
        .scalar()
        or 0
    )
    total_operators = (
        db.query(func.count(User.id)).filter(User.company_id == company_id, User.is_active == True).scalar() or 0
    )
    operators_without_certs = total_operators - operators_with_certs

    # Training hours this month
    first_of_month = today.replace(day=1)
    training_hours_month = (
        db.query(func.coalesce(func.sum(TrainingRecord.hours), 0.0))
        .filter(
            TrainingRecord.company_id == company_id,
            TrainingRecord.training_date >= first_of_month,
            TrainingRecord.training_date <= today,
        )
        .scalar()
    )

    # Certifications by type
    certs_by_type = {}
    for cert in all_certs:
        ct = cert.certification_type.value if hasattr(cert.certification_type, 'value') else cert.certification_type
        certs_by_type[ct] = certs_by_type.get(ct, 0) + 1

    # Expiring certifications detail
    expiring_certs = []
    for cert in all_certs:
        if cert.expiration_date and cert.expiration_date <= soon and cert.expiration_date >= today:
            user = db.query(User).filter(User.id == cert.user_id).first()
            expiring_certs.append(
                {
                    "id": cert.id,
                    "user_name": f"{user.first_name} {user.last_name}" if user else "Unknown",
                    "certification_name": cert.certification_name,
                    "expiration_date": cert.expiration_date.isoformat(),
                    "days_until_expiry": (cert.expiration_date - today).days,
                }
            )
    expiring_certs.sort(key=lambda x: x["days_until_expiry"])

    return {
        "total_certifications": total_certs,
        "active_count": active_count,
        "expiring_soon_count": expiring_soon_count,
        "expired_count": expired_count,
        "revoked_count": revoked_count,
        "compliance_rate": compliance_rate,
        "total_operators": total_operators,
        "operators_with_certs": operators_with_certs,
        "operators_without_certs": operators_without_certs,
        "training_hours_this_month": round(float(training_hours_month), 1),
        "certifications_by_type": certs_by_type,
        "expiring_certifications": expiring_certs,
    }


@router.get("/certifications/expiring")
def get_expiring_certifications(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get certifications expiring within N days"""
    today = date.today()
    cutoff = today + timedelta(days=days)

    certs = (
        db.query(OperatorCertification)
        .filter(
            OperatorCertification.company_id == company_id,
            OperatorCertification.expiration_date != None,
            OperatorCertification.expiration_date <= cutoff,
            OperatorCertification.expiration_date >= today,
            OperatorCertification.status != CertificationStatus.REVOKED,
        )
        .order_by(OperatorCertification.expiration_date)
        .all()
    )

    return [serialize_cert(c, db) for c in certs]


@router.get("/certifications/user/{user_id}")
def get_user_certifications(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all certifications for a user"""
    certs = (
        db.query(OperatorCertification)
        .filter(
            OperatorCertification.company_id == company_id,
            OperatorCertification.user_id == user_id,
        )
        .order_by(OperatorCertification.expiration_date)
        .all()
    )

    return [serialize_cert(c, db) for c in certs]


@router.get("/certifications/")
def list_certifications(
    user_id: Optional[int] = None,
    certification_type: Optional[str] = None,
    status: Optional[str] = None,
    expiring_within_days: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all certifications with filters"""
    query = db.query(OperatorCertification).filter(OperatorCertification.company_id == company_id)

    if user_id:
        query = query.filter(OperatorCertification.user_id == user_id)
    if certification_type:
        query = query.filter(OperatorCertification.certification_type == certification_type)
    if expiring_within_days:
        cutoff = date.today() + timedelta(days=expiring_within_days)
        query = query.filter(
            OperatorCertification.expiration_date != None,
            OperatorCertification.expiration_date <= cutoff,
        )

    certs = query.order_by(OperatorCertification.expiration_date).all()

    result = [serialize_cert(c, db) for c in certs]

    # Post-filter by computed status if requested
    if status:
        result = [r for r in result if r["status"] == status]

    return result


@router.get("/certifications/{cert_id}")
def get_certification(
    cert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get single certification"""
    cert = (
        db.query(OperatorCertification)
        .filter(OperatorCertification.id == cert_id, OperatorCertification.company_id == company_id)
        .first()
    )
    if not cert:
        raise HTTPException(status_code=404, detail="Certification not found")
    return serialize_cert(cert, db)


@router.post("/certifications/")
def create_certification(
    cert_in: CertificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CERT_TRAINING_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create certification"""
    # Reject a cross-tenant user_id FK before insert.
    _require_company_user(db, cert_in.user_id, company_id)

    data = cert_in.model_dump()
    data["certification_type"] = CertificationType(data["certification_type"])
    data["status"] = CertificationStatus(data["status"])

    cert = OperatorCertification(**data)
    cert.company_id = company_id
    db.add(cert)

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically; flush for the PK.
    db.flush()
    audit.log_create(
        resource_type="operator_certification",
        resource_id=cert.id,
        resource_identifier=str(cert.id),
        new_values=cert,
        description=f"Created operator certification {cert.id}",
    )
    db.commit()
    db.refresh(cert)
    return serialize_cert(cert, db)


@router.put("/certifications/{cert_id}")
def update_certification(
    cert_id: int,
    cert_in: CertificationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CERT_TRAINING_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update certification"""
    cert = (
        db.query(OperatorCertification)
        .filter(OperatorCertification.id == cert_id, OperatorCertification.company_id == company_id)
        .first()
    )
    if not cert:
        raise HTTPException(status_code=404, detail="Certification not found")

    # Snapshot pre-mutation values for the audit diff (the live model is mutated in place).
    old_values = {c.key: getattr(cert, c.key) for c in cert.__table__.columns}

    update_data = cert_in.model_dump(exclude_unset=True)
    if "certification_type" in update_data:
        update_data["certification_type"] = CertificationType(update_data["certification_type"])
    if "status" in update_data:
        update_data["status"] = CertificationStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(cert, field, value)

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    db.flush()
    audit.log_update(
        resource_type="operator_certification",
        resource_id=cert.id,
        resource_identifier=str(cert.id),
        old_values=old_values,
        new_values=cert,
        description=f"Updated operator certification {cert.id}",
    )
    db.commit()
    db.refresh(cert)
    return serialize_cert(cert, db)


@router.delete("/certifications/{cert_id}")
def delete_certification(
    cert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CERT_TRAINING_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete certification"""
    cert = (
        db.query(OperatorCertification)
        .filter(OperatorCertification.id == cert_id, OperatorCertification.company_id == company_id)
        .first()
    )
    if not cert:
        raise HTTPException(status_code=404, detail="Certification not found")

    # Snapshot for the audit row before the row is physically removed (no SoftDeleteMixin here).
    old_values = {c.key: getattr(cert, c.key) for c in cert.__table__.columns}
    deleted_id = cert.id

    db.delete(cert)
    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    audit.log_delete(
        resource_type="operator_certification",
        resource_id=deleted_id,
        resource_identifier=str(deleted_id),
        old_values=old_values,
        description=f"Deleted operator certification {deleted_id}",
    )
    db.commit()
    return {"message": "Certification deleted"}


# ==================== Training Endpoints ====================


@router.get("/training/user/{user_id}")
def get_user_training(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get all training for a user"""
    records = (
        db.query(TrainingRecord)
        .filter(
            TrainingRecord.company_id == company_id,
            TrainingRecord.user_id == user_id,
        )
        .order_by(TrainingRecord.training_date.desc())
        .all()
    )
    return [serialize_training(r, db) for r in records]


@router.get("/training/")
def list_training(
    user_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    training_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List training records with filters"""
    query = db.query(TrainingRecord).filter(TrainingRecord.company_id == company_id)

    if user_id:
        query = query.filter(TrainingRecord.user_id == user_id)
    if date_from:
        query = query.filter(TrainingRecord.training_date >= date_from)
    if date_to:
        query = query.filter(TrainingRecord.training_date <= date_to)
    if training_type:
        query = query.filter(TrainingRecord.training_type == training_type)

    records = query.order_by(TrainingRecord.training_date.desc()).all()
    return [serialize_training(r, db) for r in records]


@router.post("/training/")
def create_training(
    training_in: TrainingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CERT_TRAINING_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create training record"""
    # Reject cross-tenant FKs before insert (user_id required; work_center_id optional).
    _require_company_user(db, training_in.user_id, company_id)
    if training_in.work_center_id is not None:
        _require_company_work_center(db, training_in.work_center_id, company_id)

    data = training_in.model_dump()
    data["recorded_by"] = current_user.id
    record = TrainingRecord(**data)
    record.company_id = company_id
    db.add(record)

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically; flush for the PK.
    db.flush()
    audit.log_create(
        resource_type="training_record",
        resource_id=record.id,
        resource_identifier=str(record.id),
        new_values=record,
        description=f"Created training record {record.id}",
    )
    db.commit()
    db.refresh(record)
    return serialize_training(record, db)


@router.put("/training/{training_id}")
def update_training(
    training_id: int,
    training_in: TrainingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CERT_TRAINING_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update training record"""
    record = (
        db.query(TrainingRecord)
        .filter(TrainingRecord.id == training_id, TrainingRecord.company_id == company_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Training record not found")

    # A re-pointed work_center_id must stay in-company (cross-tenant FK guard on update).
    update_data = training_in.model_dump(exclude_unset=True)
    if update_data.get("work_center_id") is not None:
        _require_company_work_center(db, update_data["work_center_id"], company_id)

    # Snapshot pre-mutation values for the audit diff (the live model is mutated in place).
    old_values = {c.key: getattr(record, c.key) for c in record.__table__.columns}

    for field, value in update_data.items():
        setattr(record, field, value)

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    db.flush()
    audit.log_update(
        resource_type="training_record",
        resource_id=record.id,
        resource_identifier=str(record.id),
        old_values=old_values,
        new_values=record,
        description=f"Updated training record {record.id}",
    )
    db.commit()
    db.refresh(record)
    return serialize_training(record, db)


# ==================== Skill Matrix Endpoints ====================


@router.get("/skill-matrix/check/{user_id}/{work_center_id}")
def check_operator_qualification(
    user_id: int,
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Check if operator is qualified for a work center"""
    entry = (
        db.query(SkillMatrix)
        .filter(
            SkillMatrix.company_id == company_id,
            SkillMatrix.user_id == user_id,
            SkillMatrix.work_center_id == work_center_id,
            SkillMatrix.is_active == True,
        )
        .first()
    )

    if not entry:
        return {"qualified": False, "skill_level": 0, "detail": None}

    return {
        "qualified": entry.skill_level >= 2,  # At least Basic level
        "skill_level": entry.skill_level,
        "detail": serialize_skill(entry, db),
    }


@router.get("/skill-matrix/user/{user_id}")
def get_user_skills(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Skill matrix for specific user"""
    entries = db.query(SkillMatrix).filter(SkillMatrix.company_id == company_id, SkillMatrix.user_id == user_id).all()
    return [serialize_skill(e, db) for e in entries]


@router.get("/skill-matrix/work-center/{work_center_id}")
def get_work_center_operators(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Qualified operators for a work center"""
    entries = (
        db.query(SkillMatrix)
        .filter(
            SkillMatrix.company_id == company_id,
            SkillMatrix.work_center_id == work_center_id,
            SkillMatrix.is_active == True,
        )
        .order_by(SkillMatrix.skill_level.desc())
        .all()
    )
    return [serialize_skill(e, db) for e in entries]


@router.get("/skill-matrix/")
def list_skill_matrix(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Full skill matrix (all users x work centers)"""
    entries = db.query(SkillMatrix).filter(SkillMatrix.company_id == company_id, SkillMatrix.is_active == True).all()

    # Also return available users and work centers for the grid (tenant-scoped)
    users = db.query(User).filter(User.company_id == company_id, User.is_active == True).order_by(User.last_name).all()
    work_centers = (
        db.query(WorkCenter)
        .filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)
        .order_by(WorkCenter.code)
        .all()
    )

    return {
        "entries": [serialize_skill(e, db) for e in entries],
        "users": [{"id": u.id, "name": f"{u.first_name} {u.last_name}", "employee_id": u.employee_id} for u in users],
        "work_centers": [{"id": wc.id, "code": wc.code, "name": wc.name} for wc in work_centers],
    }


@router.post("/skill-matrix/")
def create_skill_entry(
    entry_in: SkillMatrixCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(SKILL_MATRIX_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add skill matrix entry"""
    if entry_in.skill_level < 1 or entry_in.skill_level > 5:
        raise HTTPException(status_code=400, detail="Skill level must be between 1 and 5")

    # Reject cross-tenant FKs before any lookup/insert (both required on this model).
    _require_company_user(db, entry_in.user_id, company_id)
    _require_company_work_center(db, entry_in.work_center_id, company_id)

    existing = (
        db.query(SkillMatrix)
        .filter(
            SkillMatrix.company_id == company_id,
            SkillMatrix.user_id == entry_in.user_id,
            SkillMatrix.work_center_id == entry_in.work_center_id,
        )
        .first()
    )

    if existing:
        # Update existing entry instead of creating duplicate
        old_values = {c.key: getattr(existing, c.key) for c in existing.__table__.columns}
        existing.skill_level = entry_in.skill_level
        existing.qualified_date = entry_in.qualified_date
        existing.last_assessment_date = entry_in.last_assessment_date
        existing.next_assessment_date = entry_in.next_assessment_date
        existing.notes = entry_in.notes
        existing.approved_by = current_user.id
        existing.is_active = True
        # Audit (tamper-evident) the upsert-as-update BEFORE the terminal commit so it commits atomically.
        db.flush()
        audit.log_update(
            resource_type="skill_matrix",
            resource_id=existing.id,
            resource_identifier=str(existing.id),
            old_values=old_values,
            new_values=existing,
            description=f"Updated skill matrix entry {existing.id}",
        )
        db.commit()
        db.refresh(existing)
        return serialize_skill(existing, db)

    data = entry_in.model_dump()
    data["approved_by"] = current_user.id
    entry = SkillMatrix(**data)
    entry.company_id = company_id
    db.add(entry)
    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically; flush for the PK.
    db.flush()
    audit.log_create(
        resource_type="skill_matrix",
        resource_id=entry.id,
        resource_identifier=str(entry.id),
        new_values=entry,
        description=f"Created skill matrix entry {entry.id}",
    )
    db.commit()
    db.refresh(entry)
    return serialize_skill(entry, db)


@router.put("/skill-matrix/{entry_id}")
def update_skill_entry(
    entry_id: int,
    entry_in: SkillMatrixUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(SKILL_MATRIX_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update skill matrix entry"""
    entry = db.query(SkillMatrix).filter(SkillMatrix.id == entry_id, SkillMatrix.company_id == company_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Skill matrix entry not found")

    update_data = entry_in.model_dump(exclude_unset=True)
    if "skill_level" in update_data and (update_data["skill_level"] < 1 or update_data["skill_level"] > 5):
        raise HTTPException(status_code=400, detail="Skill level must be between 1 and 5")

    # Snapshot pre-mutation values for the audit diff (the live model is mutated in place).
    old_values = {c.key: getattr(entry, c.key) for c in entry.__table__.columns}

    for field, value in update_data.items():
        setattr(entry, field, value)

    entry.approved_by = current_user.id
    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    db.flush()
    audit.log_update(
        resource_type="skill_matrix",
        resource_id=entry.id,
        resource_identifier=str(entry.id),
        old_values=old_values,
        new_values=entry,
        description=f"Updated skill matrix entry {entry.id}",
    )
    db.commit()
    db.refresh(entry)
    return serialize_skill(entry, db)
