from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    FAIStatus,
    FirstArticleInspection,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User
from app.schemas.quality import (
    CARCreate,
    CARResponse,
    CARUpdate,
    CARVoidRequest,
    FAICharacteristicCreate,
    FAICharacteristicResponse,
    FAICharacteristicUpdate,
    FAICreate,
    FAIPrefillResponse,
    FAIResponse,
    FAIUpdate,
    NCRCreate,
    NCRResponse,
    NCRUpdate,
    NCRVoidRequest,
)
from app.services import fai_service, process_sheet_service, quality_record_service
from app.services.audit_service import AuditService
from app.services.quality_numbering import generate_ncr_number as _generate_ncr_number

router = APIRouter()


# ============== NCR Endpoints ==============
#
# Every NCR / CAR write delegates to ``app.services.quality_record_service``, which
# resolves each tenant reference, refuses edits to CLOSED / VOID records, and commits
# the change together with its required audit row (503 + rollback when that row cannot
# be written). See that module's docstring for the rules.


def generate_ncr_number(db: Session, company_id: int = None) -> str:
    return _generate_ncr_number(db, company_id, lock=acquire_generator_lock)


# CAR numbering moved into the service with the CAR writers; kept importable here.
generate_car_number = quality_record_service.generate_car_number


@router.get("/ncr", response_model=List[NCRResponse])
def list_ncrs(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[NCRStatus] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all NCRs"""
    query = quality_record_service.ncr_query(db, company_id)

    if status:
        query = query.filter(NonConformanceReport.status == status)
    if part_id:
        query = query.filter(NonConformanceReport.part_id == part_id)

    return query.order_by(NonConformanceReport.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/ncr", response_model=NCRResponse)
def create_ncr(
    ncr_in: NCRCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new NCR.

    ``part_id`` and ``work_order_id`` must name live records of the active company (404
    otherwise). The NCR and its CREATE audit row commit together.
    """
    return quality_record_service.create_ncr(db, company_id, ncr_in, current_user, audit)


@router.get("/ncr/{ncr_id}", response_model=NCRResponse)
def get_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get NCR details"""
    return quality_record_service.get_ncr(db, company_id, ncr_id)


@router.put("/ncr/{ncr_id}", response_model=NCRResponse)
def update_ncr(
    ncr_id: int,
    ncr_in: NCRUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an NCR: content, disposition, status (incl. closure), assignment or CAR link.

    Only fields whose value actually changes are written, on one audit row (old -> new).
    A CLOSED NCR refuses every change (409) except a status-only reopen. ``assigned_to``
    must be an active user of the company and ``car_id`` a non-void CAR of it (404 / 409).
    Voiding goes through ``DELETE /quality/ncr/{id}``, never this route.
    """
    return quality_record_service.update_ncr(db, company_id, ncr_id, ncr_in, current_user, audit)


@router.delete("/ncr/{ncr_id}")
def void_ncr(
    ncr_id: int,
    void_in: NCRVoidRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Void (soft-delete) an NCR.

    Voiding is the quality-record form of a soft delete: the NCR is retained for the
    tamper-evident audit trail and AS9100D traceability, but marked ``is_deleted`` and
    moved to ``VOID`` status. Refused while the NCR still actively gates a work order.
    The void writes a status-change and a delete audit row carrying the reason, in the
    same transaction as the void.
    """
    number = quality_record_service.void_ncr(db, company_id, ncr_id, void_in.reason, current_user, audit)
    return {"message": f"NCR {number} voided", "can_restore": True}


@router.post("/ncr/{ncr_id}/restore", summary="Restore a voided NCR")
def restore_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Restore a voided NCR.

    The pre-void status is not preserved, so the NCR reopens to ``OPEN`` (the safe reset).
    """
    number = quality_record_service.restore_ncr(db, company_id, ncr_id, audit)
    return {"message": f"NCR {number} restored"}


@router.post("/ncr/{ncr_id}/create-car", response_model=CARResponse)
def create_car_from_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a CAR from an NCR and link it.

    Idempotent: when the NCR already links a CAR that is not void, answers 409 naming
    that CAR instead of creating a second one.
    """
    return quality_record_service.create_car_from_ncr(db, company_id, ncr_id, current_user, audit)


# ============== CAR Endpoints ==============


@router.get("/car", response_model=List[CARResponse])
def list_cars(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[CARStatus] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all CARs"""
    query = db.query(CorrectiveActionRequest).filter(CorrectiveActionRequest.company_id == company_id)

    if status:
        query = query.filter(CorrectiveActionRequest.status == status)

    return query.order_by(CorrectiveActionRequest.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/car", response_model=CARResponse)
def create_car(
    car_in: CARCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new CAR (its CREATE audit row commits with it)."""
    return quality_record_service.create_car(db, company_id, car_in, current_user, audit)


@router.get("/car/{car_id}", response_model=CARResponse)
def get_car(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get CAR details"""
    return quality_record_service.get_car(db, company_id, car_id)


@router.put("/car/{car_id}", response_model=CARResponse)
def update_car(
    car_id: int,
    car_in: CARUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a CAR: analysis, actions, verification, status (incl. closure) or assignment.

    Only fields whose value actually changes are written, on one audit row (old -> new).
    Moving to ``verification`` records the verifier; closing requires root cause,
    corrective action, verification method and results. A CLOSED CAR refuses every change
    (409) except a status-only reopen; a VOID CAR refuses all. Voiding goes through
    ``POST /quality/car/{id}/void``.
    """
    return quality_record_service.update_car(db, company_id, car_id, car_in, current_user, audit)


@router.post("/car/{car_id}/void", response_model=CARResponse)
def void_car(
    car_id: int,
    void_in: CARVoidRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Void a CAR (a duplicate, or one raised in error) with a required reason.

    The CAR stays readable with status ``void``, leaves the open-CAR count, and refuses
    every later edit. A CLOSED or already-void CAR is refused (409). The reason is
    recorded on the status-change audit row, written in the same transaction.
    """
    return quality_record_service.void_car(db, company_id, car_id, void_in.reason, current_user, audit)


# ============== FAI Endpoints ==============


@router.get("/fai", response_model=List[FAIResponse])
def list_fais(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[FAIStatus] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the active company's FAIs, including only its related evidence."""
    query = fai_service.fai_query(db, company_id)
    if status:
        query = query.filter(FirstArticleInspection.status == status)
    if part_id:
        query = query.filter(FirstArticleInspection.part_id == part_id)
    return query.order_by(FirstArticleInspection.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/fai", response_model=FAIResponse)
def create_fai(
    fai_in: FAICreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create an inspection with validated tenant-owned references."""
    return fai_service.create_fai(db, company_id, fai_in, current_user, audit)


@router.get("/fai/{fai_id}", response_model=FAIResponse)
def get_fai(
    fai_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get FAI details."""
    return fai_service.get_fai(db, company_id, fai_id)


@router.put("/fai/{fai_id}", response_model=FAIResponse)
def update_fai(
    fai_id: int,
    fai_in: FAIUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit an unfinished FAI; final disposition requires approval authority."""
    return fai_service.update_fai(db, company_id, fai_id, fai_in, current_user, audit)


@router.post("/fai/{fai_id}/prefill-from-steps", response_model=FAIPrefillResponse)
def prefill_fai_from_steps(
    fai_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Pre-fill unfinished FAI measurements from matching conforming process-step records.

    Existing values are preserved; ambiguous labels and specification mismatches are
    reported unmatched. Conformance disposition remains the inspector's decision.
    """
    return process_sheet_service.prefill_fai_from_step_records(
        db, company_id, fai_id=fai_id, user=current_user, audit=audit
    )


@router.post("/fai/{fai_id}/characteristics", response_model=FAICharacteristicResponse)
def add_fai_characteristic(
    fai_id: int,
    char_in: FAICharacteristicCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add a characteristic to an unfinished FAI."""
    return fai_service.add_characteristic(db, company_id, fai_id, char_in, audit)


@router.put("/fai/{fai_id}/characteristics/{char_id}", response_model=FAICharacteristicResponse)
def update_fai_characteristic(
    fai_id: int,
    char_id: int,
    char_in: FAICharacteristicUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record or correct a measurement on an unfinished inspection."""
    return fai_service.update_characteristic(db, company_id, fai_id, char_id, char_in, current_user, audit)


@router.delete("/fai/{fai_id}/characteristics/{char_id}")
def delete_fai_characteristic(
    fai_id: int,
    char_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Remove an unfinished characteristic, retaining its contents in the audit log."""
    return fai_service.delete_characteristic(db, company_id, fai_id, char_id, audit)


# ============== Dashboard/Summary ==============


@router.get("/summary")
def get_quality_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get quality metrics summary"""
    open_ncrs = (
        db.query(func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.status.in_([NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION]),
        )
        .scalar()
    )

    open_cars = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(
            CorrectiveActionRequest.company_id == company_id,
            CorrectiveActionRequest.status.in_(
                [CARStatus.OPEN, CARStatus.ROOT_CAUSE_ANALYSIS, CARStatus.CORRECTIVE_ACTION, CARStatus.VERIFICATION]
            ),
        )
        .scalar()
    )

    pending_fais = (
        db.query(func.count(FirstArticleInspection.id))
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.status.in_([FAIStatus.PENDING, FAIStatus.IN_PROGRESS]),
        )
        .scalar()
    )

    # NCRs by disposition this month
    month_start = date.today().replace(day=1)
    ncr_dispositions = (
        db.query(NonConformanceReport.disposition, func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
            NonConformanceReport.created_at >= month_start,
        )
        .group_by(NonConformanceReport.disposition)
        .all()
    )

    return {
        "open_ncrs": open_ncrs,
        "open_cars": open_cars,
        "pending_fais": pending_fais,
        "ncr_dispositions": {d.value: c for d, c in ncr_dispositions},
    }
