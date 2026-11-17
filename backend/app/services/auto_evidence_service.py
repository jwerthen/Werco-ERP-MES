"""
Auto-Evidence Discovery Service

Scans live ERP/MES records and maps them to QMS clauses as compliance evidence.
Uses keyword-based matching rules so it works across AS9100D, ISO 9001, IATF 16949, etc.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, case, func, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.calibration import CalibrationRecord, CalibrationStatus, Equipment
from app.models.customer_complaint import ComplaintStatus, CustomerComplaint
from app.models.document import Document, DocumentType
from app.models.engineering_change import ECOStatus, EngineeringChangeOrder
from app.models.maintenance import MaintenanceSchedule, MaintenanceStatus, MaintenanceWorkOrder
from app.models.operator_certification import CertificationStatus, OperatorCertification, TrainingRecord
from app.models.purchasing import Vendor
from app.models.qms_standard import QMSClause
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    FAIStatus,
    FirstArticleInspection,
    NCRStatus,
    NonConformanceReport,
)
from app.models.spc import SPCCharacteristic
from app.models.supplier_scorecard import SupplierScorecard
from app.models.work_order import WorkOrder
from app.services.audit_service import PAUSED_CHAIN_PLACEHOLDER
from app.services.supplier_approval_service import APPROVED_STATUSES

logger = logging.getLogger(__name__)

# Time window for "recent" records
RECENT_MONTHS = 12


def _recent_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(days=RECENT_MONTHS * 30)


# ---------------------------------------------------------------------------
# Evidence query functions
# Each returns a dict matching AutoEvidenceResult schema fields
# ---------------------------------------------------------------------------


def _query_ncrs(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = (
        db.query(func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .scalar()
        or 0
    )
    recent = (
        db.query(func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
            NonConformanceReport.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    open_count = (
        db.query(func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
            NonConformanceReport.status.in_([NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION]),
        )
        .scalar()
        or 0
    )
    closed = total - open_count

    examples = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .order_by(NonConformanceReport.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No NCR records found in system"
        suggested = "not_assessed"
    elif open_count > 5:
        health = "warning"
        health_detail = f"{open_count} open NCRs require attention"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{closed} NCRs resolved, {open_count} currently open"
        suggested = "compliant"

    return dict(
        evidence_type="ncr",
        title="Non-Conformance Reports (NCR)",
        description=f"{total} total NCRs ({recent} in last 12 months), {open_count} currently open",
        module_reference="/quality/ncr",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=n.id,
                record_identifier=n.ncr_number,
                record_type="ncr",
                summary=n.title,
                status=n.status.value if n.status else "unknown",
                date=n.created_at or datetime.utcnow(),
                module_link=f"/quality/ncr/{n.id}",
            )
            for n in examples
        ],
        suggested_compliance=suggested,
    )


def _query_cars(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(CorrectiveActionRequest.company_id == company_id)
        .scalar()
        or 0
    )
    recent = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(
            CorrectiveActionRequest.company_id == company_id,
            CorrectiveActionRequest.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    open_count = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(
            CorrectiveActionRequest.company_id == company_id,
            CorrectiveActionRequest.status.in_(
                [
                    CARStatus.OPEN,
                    CARStatus.ROOT_CAUSE_ANALYSIS,
                    CARStatus.CORRECTIVE_ACTION,
                    CARStatus.VERIFICATION,
                ]
            ),
        )
        .scalar()
        or 0
    )
    verified = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(
            CorrectiveActionRequest.company_id == company_id,
            CorrectiveActionRequest.status == CARStatus.CLOSED,
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(CorrectiveActionRequest)
        .filter(CorrectiveActionRequest.company_id == company_id)
        .order_by(CorrectiveActionRequest.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No CAR records found in system"
        suggested = "not_assessed"
    elif open_count > 3:
        health = "warning"
        health_detail = f"{open_count} CARs still open"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{verified} CARs verified closed, {open_count} in progress"
        suggested = "compliant"

    return dict(
        evidence_type="car",
        title="Corrective/Preventive Action Reports (CAR)",
        description=f"{total} total CARs ({recent} in last 12 months), {verified} verified closed",
        module_reference="/quality/car",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=c.id,
                record_identifier=c.car_number,
                record_type="car",
                summary=c.title,
                status=c.status.value if c.status else "unknown",
                date=c.created_at or datetime.utcnow(),
                module_link=f"/quality/car/{c.id}",
            )
            for c in examples
        ],
        suggested_compliance=suggested,
    )


def _query_fais(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = (
        db.query(func.count(FirstArticleInspection.id)).filter(FirstArticleInspection.company_id == company_id).scalar()
        or 0
    )
    recent = (
        db.query(func.count(FirstArticleInspection.id))
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    passed = (
        db.query(func.count(FirstArticleInspection.id))
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.status == FAIStatus.PASSED,
        )
        .scalar()
        or 0
    )
    failed = (
        db.query(func.count(FirstArticleInspection.id))
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.status == FAIStatus.FAILED,
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(FirstArticleInspection)
        .filter(FirstArticleInspection.company_id == company_id)
        .order_by(FirstArticleInspection.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No FAI records found"
        suggested = "not_assessed"
    elif failed > 0 and passed == 0:
        health = "critical"
        health_detail = f"{failed} FAIs failed, none passed"
        suggested = "non_compliant"
    elif failed > 0:
        health = "warning"
        health_detail = f"{passed} passed, {failed} failed"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"All {passed} FAIs passed"
        suggested = "compliant"

    return dict(
        evidence_type="fai",
        title="First Article Inspections (FAI / AS9102)",
        description=f"{total} total FAIs ({recent} in last 12 months), {passed} passed, {failed} failed",
        module_reference="/quality/fai",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=f.id,
                record_identifier=f.fai_number,
                record_type="fai",
                summary=f"FAI {f.fai_type or 'full'} - {f.reason or 'new part'}",
                status=f.status.value if f.status else "unknown",
                date=f.created_at or datetime.utcnow(),
                module_link=f"/quality/fai/{f.id}",
            )
            for f in examples
        ],
        suggested_compliance=suggested,
    )


def _query_calibration(db: Session, company_id: int) -> dict:
    total_equip = (
        db.query(func.count(Equipment.id))
        .filter(Equipment.company_id == company_id, Equipment.is_active == True)
        .scalar()
        or 0
    )
    overdue = (
        db.query(func.count(Equipment.id))
        .filter(
            Equipment.company_id == company_id,
            Equipment.is_active == True,
            Equipment.status == CalibrationStatus.OVERDUE,
        )
        .scalar()
        or 0
    )
    due_soon = (
        db.query(func.count(Equipment.id))
        .filter(
            Equipment.company_id == company_id,
            Equipment.is_active == True,
            Equipment.status == CalibrationStatus.DUE,
        )
        .scalar()
        or 0
    )

    cutoff = _recent_cutoff()
    recent_cals = (
        db.query(func.count(CalibrationRecord.id))
        .filter(
            CalibrationRecord.company_id == company_id,
            CalibrationRecord.calibration_date >= cutoff.date(),
        )
        .scalar()
        or 0
    )

    examples_eq = (
        db.query(Equipment)
        .filter(Equipment.company_id == company_id, Equipment.is_active == True)
        .order_by(Equipment.next_calibration_date.asc().nullslast())
        .limit(5)
        .all()
    )

    if total_equip == 0:
        health = "no_data"
        health_detail = "No active equipment in calibration system"
        suggested = "not_assessed"
    elif overdue > 0:
        health = "critical"
        health_detail = f"{overdue} equipment overdue for calibration"
        suggested = "non_compliant"
    elif due_soon > 3:
        health = "warning"
        health_detail = f"{due_soon} equipment due for calibration soon"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"All {total_equip} active equipment current on calibration"
        suggested = "compliant"

    return dict(
        evidence_type="calibration",
        title="Calibration & Measurement Equipment",
        description=f"{total_equip} active equipment, {recent_cals} calibrations in last 12 months, {overdue} overdue",
        module_reference="/calibration",
        total_count=total_equip,
        recent_count=recent_cals,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=e.id,
                record_identifier=e.equipment_id,
                record_type="equipment",
                summary=f"{e.name} ({e.equipment_type or 'equipment'})",
                status=e.status.value if e.status else "active",
                date=e.updated_at or e.created_at or datetime.utcnow(),
                module_link=f"/calibration/{e.id}",
            )
            for e in examples_eq
        ],
        suggested_compliance=suggested,
    )


def _query_documents(db: Session, company_id: int) -> dict:
    procedures = (
        db.query(func.count(Document.id))
        .filter(
            Document.company_id == company_id,
            Document.document_type.in_([DocumentType.PROCEDURE, DocumentType.WORK_INSTRUCTION]),
        )
        .scalar()
        or 0
    )
    approved = (
        db.query(func.count(Document.id))
        .filter(
            Document.company_id == company_id,
            Document.document_type.in_([DocumentType.PROCEDURE, DocumentType.WORK_INSTRUCTION]),
            Document.status.in_(["approved", "released"]),
        )
        .scalar()
        or 0
    )
    total = db.query(func.count(Document.id)).filter(Document.company_id == company_id).scalar() or 0
    controlled = (
        db.query(func.count(Document.id))
        .filter(Document.company_id == company_id, Document.is_controlled == True)
        .scalar()
        or 0
    )

    examples = (
        db.query(Document)
        .filter(
            Document.company_id == company_id,
            Document.document_type.in_([DocumentType.PROCEDURE, DocumentType.WORK_INSTRUCTION]),
        )
        .order_by(Document.updated_at.desc().nullslast())
        .limit(5)
        .all()
    )

    if procedures == 0:
        health = "no_data"
        health_detail = "No procedures or work instructions found"
        suggested = "not_assessed"
    elif approved < procedures:
        unapproved = procedures - approved
        health = "warning"
        health_detail = f"{unapproved} procedures not yet approved/released"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"All {procedures} procedures approved/released, {controlled} total controlled docs"
        suggested = "compliant"

    return dict(
        evidence_type="document",
        title="Document Control (Procedures & Work Instructions)",
        description=f"{procedures} procedures/WIs ({approved} approved), {total} total documents, {controlled} controlled",
        module_reference="/documents",
        total_count=total,
        recent_count=procedures,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=d.id,
                record_identifier=d.document_number,
                record_type="document",
                summary=f"{d.title} (Rev {d.revision})",
                status=d.status or "draft",
                date=d.updated_at or d.created_at or datetime.utcnow(),
                module_link=f"/documents/{d.id}",
            )
            for d in examples
        ],
        suggested_compliance=suggested,
    )


def _query_complaints(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = db.query(func.count(CustomerComplaint.id)).filter(CustomerComplaint.company_id == company_id).scalar() or 0
    recent = (
        db.query(func.count(CustomerComplaint.id))
        .filter(
            CustomerComplaint.company_id == company_id,
            CustomerComplaint.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    resolved = (
        db.query(func.count(CustomerComplaint.id))
        .filter(
            CustomerComplaint.company_id == company_id,
            CustomerComplaint.status.in_([ComplaintStatus.RESOLVED, ComplaintStatus.CLOSED]),
        )
        .scalar()
        or 0
    )
    open_count = total - resolved

    examples = (
        db.query(CustomerComplaint)
        .filter(CustomerComplaint.company_id == company_id)
        .order_by(CustomerComplaint.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "healthy"
        health_detail = "No customer complaints recorded (zero complaints)"
        suggested = "compliant"
    elif open_count > 3:
        health = "warning"
        health_detail = f"{open_count} unresolved complaints"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{resolved}/{total} complaints resolved"
        suggested = "compliant"

    return dict(
        evidence_type="other",
        title="Customer Complaints & Satisfaction",
        description=f"{total} complaints ({recent} in last 12 months), {resolved} resolved",
        module_reference="/customer-complaints",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=c.id,
                record_identifier=c.complaint_number,
                record_type="complaint",
                summary=c.title,
                status=c.status.value if c.status else "unknown",
                date=c.created_at or datetime.utcnow(),
                module_link=f"/customer-complaints/{c.id}",
            )
            for c in examples
        ],
        suggested_compliance=suggested,
    )


def _query_training(db: Session, company_id: int) -> dict:
    total_certs = (
        db.query(func.count(OperatorCertification.id)).filter(OperatorCertification.company_id == company_id).scalar()
        or 0
    )
    active_certs = (
        db.query(func.count(OperatorCertification.id))
        .filter(
            OperatorCertification.company_id == company_id,
            OperatorCertification.status == CertificationStatus.ACTIVE,
        )
        .scalar()
        or 0
    )
    expired = (
        db.query(func.count(OperatorCertification.id))
        .filter(
            OperatorCertification.company_id == company_id,
            OperatorCertification.status == CertificationStatus.EXPIRED,
        )
        .scalar()
        or 0
    )
    expiring_soon = (
        db.query(func.count(OperatorCertification.id))
        .filter(
            OperatorCertification.company_id == company_id,
            OperatorCertification.status == CertificationStatus.EXPIRING_SOON,
        )
        .scalar()
        or 0
    )

    cutoff = _recent_cutoff()
    recent_training = (
        db.query(func.count(TrainingRecord.id))
        .filter(
            TrainingRecord.company_id == company_id,
            TrainingRecord.training_date >= cutoff.date(),
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(OperatorCertification)
        .filter(OperatorCertification.company_id == company_id)
        .order_by(OperatorCertification.updated_at.desc().nullslast())
        .limit(5)
        .all()
    )

    if total_certs == 0 and recent_training == 0:
        health = "no_data"
        health_detail = "No certification or training records found"
        suggested = "not_assessed"
    elif expired > 0:
        health = "critical"
        health_detail = f"{expired} expired certifications, {expiring_soon} expiring soon"
        suggested = "non_compliant"
    elif expiring_soon > 3:
        health = "warning"
        health_detail = f"{expiring_soon} certifications expiring soon"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{active_certs} active certifications, {recent_training} training sessions in last 12 months"
        suggested = "compliant"

    return dict(
        evidence_type="training",
        title="Training & Operator Certifications",
        description=f"{total_certs} certifications ({active_certs} active, {expired} expired), {recent_training} recent trainings",
        module_reference="/operator-certifications",
        total_count=total_certs,
        recent_count=recent_training,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=c.id,
                record_identifier=c.certificate_number or f"CERT-{c.id}",
                record_type="certification",
                summary=f"{c.certification_name} ({c.certification_type.value if c.certification_type else 'general'})",
                status=c.status.value if c.status else "active",
                date=c.updated_at or c.created_at or datetime.utcnow(),
                module_link=f"/operator-certifications/{c.id}",
            )
            for c in examples
        ],
        suggested_compliance=suggested,
    )


def _query_work_orders(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = (
        db.query(func.count(WorkOrder.id))
        .filter(WorkOrder.company_id == company_id, WorkOrder.is_deleted == False)
        .scalar()
        or 0
    )
    recent = (
        db.query(func.count(WorkOrder.id))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,
            WorkOrder.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    with_lot = (
        db.query(func.count(WorkOrder.id))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,
            WorkOrder.lot_number != None,
            WorkOrder.lot_number != "",
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,
            WorkOrder.lot_number != None,
            WorkOrder.lot_number != "",
        )
        .order_by(WorkOrder.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No work orders found"
        suggested = "not_assessed"
    elif with_lot == 0:
        health = "warning"
        health_detail = "No work orders have lot/serial tracking"
        suggested = "partial"
    else:
        pct = round(with_lot / total * 100) if total > 0 else 0
        health = "healthy" if pct > 80 else "warning"
        health_detail = f"{with_lot}/{total} work orders ({pct}%) have lot traceability"
        suggested = "compliant" if pct > 80 else "partial"

    return dict(
        evidence_type="module",
        title="Work Order Traceability (Lot/Serial Tracking)",
        description=f"{total} work orders ({recent} in last 12 months), {with_lot} with lot/serial tracking",
        module_reference="/work-orders",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=w.id,
                record_identifier=w.work_order_number,
                record_type="work_order",
                summary=f"Lot: {w.lot_number} | {w.customer_name or 'internal'}",
                status=w.status.value if w.status else "unknown",
                date=w.created_at or datetime.utcnow(),
                module_link=f"/work-orders/{w.id}",
            )
            for w in examples
        ],
        suggested_compliance=suggested,
    )


def _query_spc(db: Session, company_id: int) -> dict:
    total = db.query(func.count(SPCCharacteristic.id)).filter(SPCCharacteristic.company_id == company_id).scalar() or 0
    active = (
        db.query(func.count(SPCCharacteristic.id))
        .filter(
            SPCCharacteristic.company_id == company_id,
            SPCCharacteristic.is_active == True,
        )
        .scalar()
        or 0
    )
    critical = (
        db.query(func.count(SPCCharacteristic.id))
        .filter(
            SPCCharacteristic.company_id == company_id,
            SPCCharacteristic.is_critical == True,
            SPCCharacteristic.is_active == True,
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(SPCCharacteristic)
        .filter(
            SPCCharacteristic.company_id == company_id,
            SPCCharacteristic.is_active == True,
        )
        .order_by(SPCCharacteristic.updated_at.desc().nullslast())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No SPC characteristics defined"
        suggested = "not_assessed"
    else:
        health = "healthy"
        health_detail = f"{active} active control charts, {critical} critical characteristics monitored"
        suggested = "compliant"

    return dict(
        evidence_type="spc",
        title="Statistical Process Control (SPC)",
        description=f"{active} active SPC charts, {critical} critical characteristics monitored",
        module_reference="/spc",
        total_count=total,
        recent_count=active,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=s.id,
                record_identifier=f"SPC-{s.id}",
                record_type="spc",
                summary=f"{s.name} ({s.characteristic_type})",
                status="active" if s.is_active else "inactive",
                date=s.updated_at or s.created_at or datetime.utcnow(),
                module_link=f"/spc/{s.id}",
            )
            for s in examples
        ],
        suggested_compliance=suggested,
    )


# The two row-level triggers migrations 008/060 put on public.audit_logs. They are what keeps
# an entry from being altered or removed through SQL, and the only such control left while the
# hash chain is paused -- but prod has run without them before (lost at the 2026-07-07
# bootstrap, restored by 060), so the evidence row reads the catalog rather than assuming them.
AUDIT_LOG_TRIGGER_NAMES = ("tr_audit_log_no_update", "tr_audit_log_no_delete")

# tgenabled 'O' (origin) and 'A' (always) fire in a normal session; 'D' is disabled and 'R'
# fires only under session_replication_role = replica. A literal, not an f-string over
# AUDIT_LOG_TRIGGER_NAMES, so bandit B608 has nothing to flag; a test holds the two together.
_AUDIT_LOG_TRIGGER_CHECK_SQL = (
    "SELECT count(*) FROM pg_trigger "
    "WHERE tgrelid = to_regclass('public.audit_logs') "
    "AND tgname IN ('tr_audit_log_no_update', 'tr_audit_log_no_delete') "
    "AND tgenabled IN ('O', 'A')"
)

AUDIT_TRIGGERS_IN_FORCE_SENTENCE = "Database triggers refuse UPDATE and DELETE statements on audit entries."
AUDIT_TRIGGERS_MISSING_SENTENCE = (
    "The database triggers that refuse UPDATE and DELETE statements on audit entries are missing or disabled."
)
AUDIT_TRIGGERS_UNCHECKED_SENTENCE = "Database UPDATE/DELETE trigger status could not be checked."


def _dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return bind.dialect.name if bind is not None else ""


def _audit_triggers_in_force(db: Session) -> Optional[bool]:
    """Whether both audit-log triggers exist and fire, read from ``pg_trigger``.

    ``True`` / ``False`` on PostgreSQL; ``None`` when it cannot be told -- another dialect
    (SQLite in dev and tests has no triggers to read) or a failed catalog read. The read runs
    in its own savepoint so a failure cannot abort the caller's transaction; ``begin_nested()``
    sits outside the ``try`` because a flush failure there is the caller's, not this check's.
    Not detectable here: a session that sets ``session_replication_role = replica``, or a
    TRUNCATE, neither of which fires a row trigger.
    """
    if _dialect_name(db) != "postgresql":
        return None
    savepoint = db.begin_nested()
    try:
        found = db.execute(text(_AUDIT_LOG_TRIGGER_CHECK_SQL)).scalar_one()
    except Exception:  # noqa: BLE001 - an unreadable catalog is "not checked", never a false claim
        savepoint.rollback()
        logger.warning("Could not read audit-log trigger state from pg_trigger", exc_info=True)
        return None
    savepoint.commit()
    return int(found) == len(AUDIT_LOG_TRIGGER_NAMES)


def _entries(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def _query_audit_log(db: Session, company_id: int) -> dict:
    """Evidence row for the audit trail. It must not claim protection the system lacks.

    Three facts decide it, each read rather than assumed:

    * whether the SHA-256 hash chain is on NOW -- ``settings.AUDIT_HASH_CHAIN_ENABLED``, read
      per call (CLAUDE.md invariant 2; docs/AUDIT_LOG_RETENTION_RUNBOOK.md -> Pausing the hash
      chain);
    * how many of the company's entries, over its WHOLE history, carry
      ``PAUSED_CHAIN_PLACEHOLDER`` -- written while the chain was paused, so they can never be
      verified. Whole history, not a window: the row speaks for every entry it counts, and a
      paused span never becomes verifiable by ageing. (The figure for the last 12 months is
      reported alongside it.) All four counts come from one conditional aggregate over the
      company's rows -- the same scan the total already needed;
    * whether the 008/060 UPDATE/DELETE triggers exist and fire (``_audit_triggers_in_force``).

    Chain paused now, any paused entry, or triggers missing -- or unreadable on PostgreSQL --
    caps the row at ``warning`` / ``partial``, and the reason goes into ``description`` and
    ``health_detail``. Only chain on, no paused entry and triggers in force (or, off PostgreSQL,
    not checkable at all) is ``healthy`` / ``compliant``.
    The chain being ENABLED is all this row knows about it: chain integrity is verified by the
    platform-admin integrity endpoints, not here, and the text says so rather than "active".
    """
    cutoff = _recent_cutoff()
    chain_enabled = settings.AUDIT_HASH_CHAIN_ENABLED
    is_paused = AuditLog.integrity_hash == PAUSED_CHAIN_PLACEHOLDER
    is_recent = AuditLog.timestamp >= cutoff
    total, recent, paused_total, paused_recent = (
        db.query(
            func.count(AuditLog.id),
            func.coalesce(func.sum(case((is_recent, 1), else_=0)), 0),
            func.coalesce(func.sum(case((is_paused, 1), else_=0)), 0),
            func.coalesce(func.sum(case((and_(is_paused, is_recent), 1), else_=0)), 0),
        )
        .filter(AuditLog.company_id == company_id)
        .one()
    )
    total, recent, paused_total, paused_recent = (
        int(total or 0),
        int(recent or 0),
        int(paused_total or 0),
        int(paused_recent or 0),
    )
    triggers = _audit_triggers_in_force(db)
    # On PostgreSQL an unreadable catalog is an unknown about the one control that stands behind a
    # paused span, so it caps the row like a missing trigger. Off PostgreSQL (SQLite in dev/test)
    # there is nothing to read, and the text says "could not be checked" without capping.
    triggers_unknown = triggers is None and _dialect_name(db) == "postgresql"

    counts = f"{total} audit log {'entry' if total == 1 else 'entries'} ({recent} in last 12 months)"
    paused_split = f"{paused_total} of {total} ({paused_recent} in the last 12 months)"

    if total == 0:
        health = "no_data"
        suggested = "not_assessed"
        if chain_enabled:
            description = f"{counts}, SHA-256 integrity chain"
            detail = ["No audit log entries found"]
        else:
            description = f"{counts}; SHA-256 integrity chain paused"
            detail = [
                "No audit log entries found. The integrity hash chain is paused, "
                "so entries written now will not be hash-chained."
            ]
    else:
        if not chain_enabled:
            written_without = (
                f"{_entries(paused_total)} written without it, {paused_recent} in last 12 months"
                if paused_total
                else "no entries written without it yet"
            )
            description = f"{counts}; SHA-256 integrity chain paused ({written_without})"
            detail = [
                "The integrity hash chain is paused (AUDIT_HASH_CHAIN_ENABLED is off): entries written "
                "while it is paused carry no integrity hash and can never be cryptographically verified.",
                f"Entries written while paused: {paused_split}.",
            ]
        elif paused_total:
            description = (
                f"{counts}; SHA-256 integrity chain enabled, but {_entries(paused_total)} "
                f"{'was' if paused_total == 1 else 'were'} written while it was paused "
                f"({paused_recent} in last 12 months)"
            )
            detail = [
                f"Entries written while the integrity hash chain was paused: {paused_split}. They carry no "
                "integrity hash and can never be cryptographically verified. The chain setting is on again.",
            ]
        else:
            description = f"{counts}, SHA-256 integrity chain"
            detail = [
                f"Audit trail with {_entries(total)}. Integrity hash chain enabled and no entry was written "
                "while it was paused (chain integrity is not re-verified by this check).",
            ]
        if triggers is False:
            description += "; database UPDATE/DELETE triggers missing or disabled"
        elif triggers_unknown:
            description += "; database UPDATE/DELETE trigger status could not be checked"

        problem = (not chain_enabled) or paused_total > 0 or triggers is False or triggers_unknown
        health = "warning" if problem else "healthy"
        suggested = "partial" if problem else "compliant"

    if total or not chain_enabled:
        if triggers is True:
            detail.append(AUDIT_TRIGGERS_IN_FORCE_SENTENCE)
        elif triggers is False:
            detail.append(AUDIT_TRIGGERS_MISSING_SENTENCE)
        else:
            detail.append(AUDIT_TRIGGERS_UNCHECKED_SENTENCE)

    return dict(
        evidence_type="module",
        # Names what the row reports and asserts nothing: it is the only text the QMS page's
        # linked-evidence list shows for a stored link, and it cannot vary with the state (auto-link
        # rewrites stored titles by rule id). Formerly "Audit Trail (Immutable Hash-Chain Log)", untrue for
        # every entry written while the chain was paused.
        title="Audit Trail (Audit Log & Hash-Chain Status)",
        description=description,
        module_reference="/audit",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=" ".join(detail),
        examples=[],
        suggested_compliance=suggested,
    )


def _query_maintenance(db: Session, company_id: int) -> dict:
    total_schedules = (
        db.query(func.count(MaintenanceSchedule.id)).filter(MaintenanceSchedule.company_id == company_id).scalar() or 0
    )
    _recent_cutoff()
    total_wos = (
        db.query(func.count(MaintenanceWorkOrder.id)).filter(MaintenanceWorkOrder.company_id == company_id).scalar()
        or 0
    )
    completed = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
        )
        .scalar()
        or 0
    )
    # Overdue is DERIVED from due_date, not read off a stored OVERDUE flag.
    # Nothing writes that flag by itself any more: the two GETs in
    # api/endpoints/maintenance.py that used to persist SCHEDULED -> OVERDUE were
    # unaudited status changes committed from a poll, and were removed. Counting
    # the stored value here would leave an AS9100D evidence health verdict
    # depending on whether a human had loaded the Maintenance page today. This
    # predicate matches the one GET /maintenance/dashboard uses.
    overdue = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
            MaintenanceWorkOrder.due_date < datetime.utcnow().date(),
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(MaintenanceWorkOrder)
        .filter(MaintenanceWorkOrder.company_id == company_id)
        .order_by(MaintenanceWorkOrder.created_at.desc())
        .limit(5)
        .all()
    )

    if total_schedules == 0 and total_wos == 0:
        health = "no_data"
        health_detail = "No maintenance schedules or records found"
        suggested = "not_assessed"
    elif overdue > 0:
        health = "warning"
        health_detail = f"{overdue} maintenance tasks overdue"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{total_schedules} PM schedules, {completed}/{total_wos} work orders completed"
        suggested = "compliant"

    return dict(
        evidence_type="module",
        title="Preventive Maintenance & Infrastructure",
        description=f"{total_schedules} PM schedules, {total_wos} maintenance WOs ({completed} completed, {overdue} overdue)",
        module_reference="/maintenance",
        total_count=total_wos,
        recent_count=total_schedules,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=m.id,
                record_identifier=m.wo_number,
                record_type="maintenance_wo",
                summary=m.title,
                status=m.status.value if m.status else "unknown",
                date=m.created_at or datetime.utcnow(),
                module_link=f"/maintenance/{m.id}",
            )
            for m in examples
        ],
        suggested_compliance=suggested,
    )


def _query_ecos(db: Session, company_id: int) -> dict:
    total = (
        db.query(func.count(EngineeringChangeOrder.id)).filter(EngineeringChangeOrder.company_id == company_id).scalar()
        or 0
    )
    cutoff = _recent_cutoff()
    recent = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.created_at >= cutoff,
        )
        .scalar()
        or 0
    )
    implemented = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status == ECOStatus.COMPLETED,
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(EngineeringChangeOrder)
        .filter(EngineeringChangeOrder.company_id == company_id)
        .order_by(EngineeringChangeOrder.created_at.desc())
        .limit(5)
        .all()
    )

    if total == 0:
        health = "no_data"
        health_detail = "No engineering changes recorded"
        suggested = "not_assessed"
    else:
        health = "healthy"
        health_detail = f"{implemented}/{total} ECOs implemented"
        suggested = "compliant"

    return dict(
        evidence_type="module",
        title="Engineering Change Orders (ECO)",
        description=f"{total} ECOs ({recent} in last 12 months), {implemented} implemented",
        module_reference="/engineering-changes",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=e.id,
                record_identifier=e.eco_number,
                record_type="eco",
                summary=e.title,
                status=e.status.value if e.status else "unknown",
                date=e.created_at or datetime.utcnow(),
                module_link=f"/engineering-changes/{e.id}",
            )
            for e in examples
        ],
        suggested_compliance=suggested,
    )


def _query_suppliers(db: Session, company_id: int) -> dict:
    # Both counts filter is_deleted, and they must move together: ``total`` had no mask at
    # all while ``approved`` was masked by is_active, so a company with removed suppliers
    # reported an approval gap that does not exist -- in an artifact handed to a registrar.
    total_vendors = (
        db.query(func.count(Vendor.id))
        .filter(Vendor.company_id == company_id, Vendor.is_deleted == False)  # noqa: E712
        .scalar()
        or 0
    )
    total_scorecards = (
        db.query(func.count(SupplierScorecard.id)).filter(SupplierScorecard.company_id == company_id).scalar() or 0
    )
    active = (
        db.query(func.count(Vendor.id))
        .filter(
            Vendor.company_id == company_id,
            Vendor.is_active == True,  # noqa: E712
            Vendor.is_deleted == False,  # noqa: E712
        )
        .scalar()
        or 0
    )
    # "Approved" means approved ON THE APPROVED SUPPLIER LIST (vendors.approval_status, migration
    # 123) -- not merely switched on. ``active`` above used to be reported as the approved count,
    # which overstated it to a registrar by every active vendor that is Conditional, Disqualified
    # or not on the ASL at all.
    approved = (
        db.query(func.count(Vendor.id))
        .filter(
            Vendor.company_id == company_id,
            Vendor.is_active == True,  # noqa: E712
            Vendor.is_deleted == False,  # noqa: E712
            Vendor.approval_status.in_(sorted(APPROVED_STATUSES)),
        )
        .scalar()
        or 0
    )

    examples = (
        db.query(SupplierScorecard)
        .filter(SupplierScorecard.company_id == company_id)
        .order_by(SupplierScorecard.period_end.desc())
        .limit(5)
        .all()
    )

    if total_vendors == 0:
        health = "no_data"
        health_detail = "No suppliers in system"
        suggested = "not_assessed"
    elif total_scorecards == 0:
        health = "warning"
        health_detail = f"{active} active suppliers but no performance scorecards"
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = f"{approved} suppliers approved on the ASL, {total_scorecards} performance evaluations"
        suggested = "compliant"

    return dict(
        evidence_type="module",
        title="Supplier Management & Evaluation",
        description=(
            f"{total_vendors} suppliers ({active} active), {approved} approved on the ASL, "
            f"{total_scorecards} performance scorecards"
        ),
        module_reference="/purchasing/vendors",
        total_count=total_vendors,
        recent_count=total_scorecards,
        health_status=health,
        health_detail=health_detail,
        examples=[
            dict(
                record_id=s.id,
                record_identifier=f"SC-{s.id}",
                record_type="scorecard",
                summary=f"Score: {s.overall_score}/100 ({s.rating or 'unrated'})",
                status=s.rating or "unrated",
                date=datetime.combine(s.period_end, datetime.min.time()) if s.period_end else datetime.utcnow(),
                module_link=f"/purchasing/vendors/{s.vendor_id}",
            )
            for s in examples
        ],
        suggested_compliance=suggested,
    )


# ---------------------------------------------------------------------------
# Mapping rules: keyword patterns → query functions
# ---------------------------------------------------------------------------

EVIDENCE_MAPPING_RULES: List[dict] = [
    {
        "id": "ncr",
        "keywords": [
            "nonconform",
            "control of nonconforming",
            "nonconforming output",
            "nonconforming product",
            "reject",
            "scrap",
            "rework",
        ],
        "query_fn": _query_ncrs,
    },
    {
        "id": "car",
        "keywords": [
            "corrective action",
            "preventive action",
            "improvement",
            "continual improvement",
            "root cause",
            "capa",
        ],
        "query_fn": _query_cars,
    },
    {
        "id": "fai",
        "keywords": [
            "first article",
            "production process verification",
            "initial inspection",
            "as9102",
            "product verification",
        ],
        "query_fn": _query_fais,
    },
    {
        "id": "calibration",
        "keywords": [
            "calibration",
            "monitoring and measuring",
            "measurement",
            "measuring equipment",
            "metrolog",
            "gauge",
            "instrument",
        ],
        "query_fn": _query_calibration,
    },
    {
        "id": "document_control",
        "keywords": [
            "document control",
            "documented information",
            "procedure",
            "work instruction",
            "controlled document",
            "document approval",
            "records control",
            "retention",
        ],
        "query_fn": _query_documents,
    },
    {
        "id": "complaints",
        "keywords": [
            "customer satisfaction",
            "customer complaint",
            "customer feedback",
            "customer communication",
            "complaint handling",
        ],
        "query_fn": _query_complaints,
    },
    {
        "id": "training",
        "keywords": ["training", "competence", "awareness", "qualification", "personnel", "human resource", "skill"],
        "query_fn": _query_training,
    },
    {
        "id": "traceability",
        "keywords": [
            "traceability",
            "identification and traceability",
            "lot track",
            "serial number",
            "batch",
            "product identification",
        ],
        "query_fn": _query_work_orders,
    },
    {
        "id": "spc",
        "keywords": [
            "statistical",
            "spc",
            "process control",
            "statistical technique",
            "data analysis",
            "process capability",
        ],
        "query_fn": _query_spc,
    },
    {
        "id": "audit",
        "keywords": ["internal audit", "audit program", "audit trail", "management review", "audit log"],
        "query_fn": _query_audit_log,
    },
    {
        "id": "maintenance",
        "keywords": [
            "maintenance",
            "infrastructure",
            "preventive maintenance",
            "work environment",
            "facility",
            "equipment maintenance",
        ],
        "query_fn": _query_maintenance,
    },
    {
        "id": "eco",
        "keywords": [
            "engineering change",
            "design change",
            "design control",
            "configuration management",
            "change management",
            "design and development",
        ],
        "query_fn": _query_ecos,
    },
    {
        "id": "suppliers",
        "keywords": [
            "purchasing",
            "external provider",
            "supplier",
            "vendor",
            "procurement",
            "outsource",
            "supply chain",
            "evaluation of supplier",
            "approved supplier",
        ],
        "query_fn": _query_suppliers,
    },
]


def _match_rules(clause: QMSClause) -> List[dict]:
    """Match clause text against mapping rules using keyword search."""
    text = ""
    if clause.title:
        text += clause.title.lower()
    if clause.description:
        text += " " + clause.description.lower()

    matched = []
    for rule in EVIDENCE_MAPPING_RULES:
        for kw in rule["keywords"]:
            if kw.lower() in text:
                matched.append(rule)
                break
    return matched


def discover_evidence_for_clause(db: Session, clause: QMSClause, company_id: int) -> List[dict]:
    """
    Discover live ERP/MES evidence for a single QMS clause.
    Returns a list of AutoEvidenceResult-compatible dicts.
    All evidence counts and examples are scoped to the given company.
    """
    matched_rules = _match_rules(clause)
    results = []

    # Track which query functions we've already called (avoid duplicates)
    seen_ids = set()
    for rule in matched_rules:
        if rule["id"] in seen_ids:
            continue
        seen_ids.add(rule["id"])
        try:
            result = rule["query_fn"](db, company_id)
            result["_rule_id"] = rule["id"]
            results.append(result)
        except Exception as e:
            logger.error(f"Error querying {rule['id']} for clause {clause.clause_number}: {e}")

    return results


def compute_overall_compliance(evidence_results: List[dict]) -> str:
    """Compute overall suggested compliance from multiple evidence results."""
    if not evidence_results:
        return "not_assessed"

    statuses = [r.get("suggested_compliance", "not_assessed") for r in evidence_results]

    if "non_compliant" in statuses:
        return "non_compliant"
    if "partial" in statuses:
        return "partial"
    if all(s == "compliant" for s in statuses):
        return "compliant"
    if all(s == "not_assessed" for s in statuses):
        return "not_assessed"
    return "partial"
