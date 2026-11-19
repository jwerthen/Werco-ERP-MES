from datetime import date
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
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
    CAREffectivenessRequest,
    CARResponse,
    CARUpdate,
    CARVoidRequest,
    FAICharacteristicCreate,
    FAICharacteristicResponse,
    FAICharacteristicUpdate,
    FAICreate,
    FAICustomerApprovalRequest,
    FAILogRow,
    FAIPrefillResponse,
    FAIResponse,
    FAIUpdate,
    NCRCreate,
    NCRResponse,
    NCRReverificationRequest,
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

    ncrs = query.order_by(NonConformanceReport.created_at.desc()).offset(skip).limit(limit).all()
    return quality_record_service.ncr_responses(db, company_id, ncrs)


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
    ncr = quality_record_service.create_ncr(db, company_id, ncr_in, current_user, audit)
    return quality_record_service.ncr_response(db, company_id, ncr)


@router.get("/ncr/{ncr_id}", response_model=NCRResponse)
def get_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get NCR details, including the disposition evidence and resolved display names."""
    ncr = quality_record_service.get_ncr(db, company_id, ncr_id)
    return quality_record_service.ncr_response(db, company_id, ncr)


@router.put("/ncr/{ncr_id}", response_model=NCRResponse)
def update_ncr(
    ncr_id: int,
    ncr_in: NCRUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an NCR: content, disposition, action taken, customer concession, status (incl.
    closure), assignment or CAR link.

    Only fields whose value actually changes are written, on one audit row (old -> new).
    A CLOSED NCR refuses every change (409) except a status-only reopen. ``assigned_to``
    must be an active user of the company and ``car_id`` a non-void CAR of it (404 / 409).
    Voiding goes through ``DELETE /quality/ncr/{id}``, never this route.

    Changing ``disposition`` records who decided it and when (``dispositioned_by`` /
    ``dispositioned_at``, stamped by the server; back to ``pending`` clears them).
    ``concession_date`` cannot be in the future (422). A standing API token may not change
    the disposition or any ``concession_*`` field (403) -- a person records that judgment.

    Closing (``status: closed``) also requires, on the record as it would be saved: a
    disposition other than pending, ``action_taken``, a ``concession_reference`` for a
    use-as-is or repair disposition, and -- for rework or repair -- a ``conforming``
    re-verification recorded with ``POST /quality/ncr/{id}/reverification`` (422 naming
    everything missing). The re-verification is never set through this route.
    """
    ncr = quality_record_service.update_ncr(db, company_id, ncr_id, ncr_in, current_user, audit)
    return quality_record_service.ncr_response(db, company_id, ncr)


@router.post("/ncr/{ncr_id}/reverification", response_model=NCRResponse)
def record_ncr_reverification(
    ncr_id: int,
    reverification_in: NCRReverificationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record the re-verification of an NCR's reworked or repaired product (Admin, Manager or Quality).

    Send ``result`` (``conforming`` or ``nonconforming``) and ``notes`` describing what was
    re-inspected or re-tested (required). The server stamps who recorded it and when; neither
    is taken from the request. Only for an NCR dispositioned ``rework`` or ``repair`` (409
    otherwise) that is not closed (409 -- reopen it first) or void. Recording again replaces
    the earlier re-verification; the audit row keeps what it replaced. A ``conforming`` result
    is required before a rework or repair NCR can be closed with ``PUT /quality/ncr/{id}``.
    A standing API token is refused (403) -- a person records it. The audit row commits with
    the re-verification, or neither is saved (503).
    """
    ncr = quality_record_service.record_ncr_reverification(
        db, company_id, ncr_id, reverification_in, current_user, audit
    )
    return quality_record_service.ncr_response(db, company_id, ncr)


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
    The new CAR is prefilled from the NCR: its part (the form's "Part number") and its PO
    number (the vendor "P.O number"), with today's Central date as "Date Submitted".
    """
    car = quality_record_service.create_car_from_ncr(db, company_id, ncr_id, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


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

    cars = query.order_by(CorrectiveActionRequest.created_at.desc()).offset(skip).limit(limit).all()
    return quality_record_service.car_responses(db, company_id, cars)


@router.post("/car", response_model=CARResponse)
def create_car(
    car_in: CARCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new CAR (its CREATE audit row commits with it).

    Carries the fields of Werco's controlled CAR form: reason for initiation (one value;
    ``other`` requires ``initiation_reason_other``, else 422), RMA #, Date Submitted (defaults
    to today in Central; a past date back-enters a paper CAR), part, customer, vendor and
    vendor PO, the four cost buckets, management action and the vendor / customer approvals.
    A part or customer must be a live record of your company and a vendor must be your
    company's (it may be inactive or removed -- a supplier CAR may document the removal);
    anything else is a 404 and nothing is written. The customer's name is snapshotted.
    Date Submitted and the approval dates cannot be in the future (422). A standing API token
    may not fill the vendor or customer approval boxes (403): a person records a sign-off.
    """
    car = quality_record_service.create_car(db, company_id, car_in, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


@router.get("/car/{car_id}", response_model=CARResponse)
def get_car(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get CAR details, including the CAR form fields and resolved display names."""
    car = quality_record_service.get_car(db, company_id, car_id)
    return quality_record_service.car_response(db, company_id, car)


@router.get("/car/{car_id}/pdf")
def get_car_pdf(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Download the printable CAR (PDF) in the layout of Werco's controlled CAR form.

    Same read access as ``GET /quality/car/{id}``; a CAR of another company is a 404. The
    header is your company's own name, address and phone. Writes nothing.
    """
    car_number, pdf_bytes = quality_record_service.render_car_pdf(db, company_id, car_id)
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{car_number}.pdf"'},
    )


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
    corrective action, verification method and results, and a corrective CAR (``car_type``
    corrective) also requires ``human_factors_review``, ``similar_nonconformities_review``
    and an ``effective`` effectiveness verification recorded with
    ``POST /quality/car/{id}/effectiveness`` (422 naming everything missing). Once an
    effectiveness result is recorded, changing ``effectiveness_check`` here is refused (409):
    record a new verification instead. A CLOSED CAR refuses every change (409) except a
    status-only reopen; a VOID CAR refuses all. Voiding goes through
    ``POST /quality/car/{id}/void``.

    The CAR form fields are editable here too: a CHANGED part or customer must be a live
    record of your company (an unchanged one is not re-checked), a changed vendor must be
    your company's, and the reason ``other`` requires ``initiation_reason_other`` (422);
    moving the reason off ``other`` clears that text unless the request sends it. Date
    Submitted and the approval dates cannot be in the future (422). A standing API token may
    not change the vendor or customer approval boxes (403). The Quality Manager approval is
    not editable here -- record it with ``POST /quality/car/{id}/quality-approval``.
    """
    car = quality_record_service.update_car(db, company_id, car_id, car_in, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


@router.post("/car/{car_id}/quality-approval", response_model=CARResponse)
def approve_car_quality(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record the CAR form's Quality Manager approval (Admin, Manager or Quality).

    The server stamps the approver (you) and the time; neither is taken from the request.
    Recorded once: an already-approved CAR is refused (409), as is a VOID one (409). A
    standing API token is refused (403) -- the approval names a person. It does not gate
    closure and does not freeze the CAR's other fields. The APPROVE audit row commits with
    the stamp, or neither is saved (503).
    """
    car = quality_record_service.approve_car_quality(db, company_id, car_id, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


@router.post("/car/{car_id}/effectiveness", response_model=CARResponse)
def record_car_effectiveness(
    car_id: int,
    effectiveness_in: CAREffectivenessRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record the effectiveness verification of a CAR's corrective action (Admin, Manager or Quality).

    Send ``result`` (``effective`` or ``not_effective``) and ``evidence``: what was reviewed to
    judge it (required; stored as the CAR's ``effectiveness_check``). The server stamps who
    recorded it and when; neither is taken from the request. Only while the CAR is in
    ``verification`` (409 otherwise). ``not_effective`` returns the CAR to
    ``corrective_action``; ``effective`` leaves the status alone and is required before a
    corrective CAR can be closed. Recording again replaces the earlier verification; the audit
    row keeps what it replaced. A standing API token is refused (403) -- a person records it.
    The audit row commits with the verification, or neither is saved (503).
    """
    car = quality_record_service.record_car_effectiveness(db, company_id, car_id, effectiveness_in, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


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
    every later edit until restored (``POST /quality/car/{id}/restore``). A CLOSED or
    already-void CAR is refused (409). The reason is recorded on the status-change audit
    row, written in the same transaction.
    """
    car = quality_record_service.void_car(db, company_id, car_id, void_in.reason, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


@router.post("/car/{car_id}/restore", response_model=CARResponse)
def restore_car(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(quality_record_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Undo a CAR void: the CAR reopens to ``open`` (its pre-void status is not preserved).

    A CAR that is not void is refused (409). Any closure fields a legacy void row still
    carries are cleared. The RESTORE audit row is written in the same transaction.
    """
    car = quality_record_service.restore_car(db, company_id, car_id, current_user, audit)
    return quality_record_service.car_response(db, company_id, car)


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
    fais = query.order_by(FirstArticleInspection.created_at.desc()).offset(skip).limit(limit).all()
    return fai_service.fai_responses(db, company_id, fais)


# Declared before ``/fai/{fai_id}`` so "log" is never parsed as an FAI id.
@router.get("/fai/log", response_model=List[FAILogRow])
def list_fai_log(
    start_date: Optional[date] = Query(None, description="Log date from (inclusive, Central)"),
    end_date: Optional[date] = Query(None, description="Log date to (inclusive, Central)"),
    status: Optional[FAIStatus] = Query(None, description="Filter by FAI status (result)"),
    customer: Optional[str] = Query(None, max_length=255, description="Customer name contains"),
    part_id: Optional[int] = Query(None, gt=0),
    work_order_id: Optional[int] = Query(None, gt=0),
    search: Optional[str] = Query(
        None, max_length=255, description="FAI #, part # or description, job #, customer PO or serial contains"
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """The FAI log of record: one line per first article inspection, newest first.

    The ERP is the company's FAI log of record; the old spreadsheet log (form QF 7.5-3.4) is a
    frozen archive. Each line carries what that log did -- FAI Report #, date, part #,
    product description, customer, job # and whether it is complete -- plus the revision
    inspected, customer PO, result, type, reason and the customer approval. Customer, PO and
    job come from the linked work order (customer falls back to the part's). A line keeps
    naming a since-deleted part or work order: this is a historical record.

    Offset-paged (``limit`` / ``offset``): request one row more than a page to learn whether
    another page exists. The date filters are inclusive Central dates on the log's date (the
    completion date, else the inspection date, else the day the FAI was created).
    """
    query = fai_service.fai_log_query(
        db,
        company_id,
        start_date=start_date,
        end_date=end_date,
        status=status,
        customer=customer,
        part_id=part_id,
        work_order_id=work_order_id,
        search=search,
    )
    return fai_service.fai_log_rows(query.offset(offset).limit(limit).all())


@router.post("/fai", response_model=FAIResponse)
def create_fai(
    fai_in: FAICreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create an inspection with validated tenant-owned references.

    ``part_id`` and ``work_order_id`` must be live records of your company (404 otherwise),
    and the work order must build that part -- as its own part, or as a component of one of its
    operations -- or the FAI log would name another part's job, customer and PO (422). When
    ``part_revision`` is omitted the part's current revision is recorded as the revision
    inspected.
    """
    fai = fai_service.create_fai(db, company_id, fai_in, current_user, audit)
    return fai_service.fai_response(db, company_id, fai)


@router.get("/fai/{fai_id}", response_model=FAIResponse)
def get_fai(
    fai_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get FAI details."""
    return fai_service.fai_response(db, company_id, fai_service.get_fai(db, company_id, fai_id))


@router.put("/fai/{fai_id}", response_model=FAIResponse)
def update_fai(
    fai_id: int,
    fai_in: FAIUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit an unfinished FAI; final disposition requires approval authority.

    Setting ``status`` to passed, failed or conditional is the final disposition (Admin,
    Manager or Quality; 403 otherwise, and 403 for a standing API token -- a person records
    it): it stamps the completion date (Central) and the approver and freezes the inspection
    evidence (later edits are 409). ``inspection_date`` cannot be in the future (422), and a
    disposition is refused (409) while the stored inspection date is in the future -- correct
    it first, or in the same request. The customer's approval is recorded afterwards with
    ``POST /quality/fai/{id}/customer-approval``.
    """
    fai = fai_service.update_fai(db, company_id, fai_id, fai_in, current_user, audit)
    return fai_service.fai_response(db, company_id, fai)


@router.post("/fai/{fai_id}/customer-approval", response_model=FAIResponse)
def record_fai_customer_approval(
    fai_id: int,
    approval_in: Optional[FAICustomerApprovalRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(fai_service.APPROVE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record the customer's approval of a completed FAI (Admin, Manager or Quality).

    Writes only ``customer_approved`` and ``customer_approval_date`` (default today, Central --
    the body is optional; a past date back-enters an earlier approval, but not one before the
    FAI's inspection date unless that stored date is itself in the future; a future date is
    422). Only a PASSED or CONDITIONAL FAI can carry it: an FAI without its final
    disposition, or a FAILED one, is refused (409). Recorded once (409 if already recorded).
    A standing API token is refused (403) -- a person must record it. The APPROVE audit row
    commits with the approval, or neither is saved (503).
    """
    fai = fai_service.record_customer_approval(db, company_id, fai_id, approval_in, current_user, audit)
    return fai_service.fai_response(db, company_id, fai)


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
