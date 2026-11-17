"""
Auto-Evidence Discovery Service

Scans live ERP/MES records and maps them to QMS clauses as compliance evidence.
Uses keyword-based matching rules so it works across AS9100D, ISO 9001, IATF 16949, etc.
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

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
from app.services.company_time import company_today
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


def _query_audit_log(db: Session, company_id: int) -> dict:
    cutoff = _recent_cutoff()
    total = db.query(func.count(AuditLog.id)).filter(AuditLog.company_id == company_id).scalar() or 0
    recent = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.company_id == company_id, AuditLog.timestamp >= cutoff)
        .scalar()
        or 0
    )

    if total == 0:
        health = "no_data"
        health_detail = "No audit log entries found"
        suggested = "not_assessed"
    else:
        health = "healthy"
        health_detail = f"Immutable audit trail with {total} entries, integrity hash chain active"
        suggested = "compliant"

    return dict(
        evidence_type="module",
        title="Audit Trail (Immutable Hash-Chain Log)",
        description=f"{total} audit log entries ({recent} in last 12 months), SHA-256 integrity chain",
        module_reference="/audit",
        total_count=total,
        recent_count=recent,
        health_status=health,
        health_detail=health_detail,
        examples=[],
        suggested_compliance=suggested,
    )


# Internal key on a result row that reports the ERP holds NO record of what its clause asks
# for. Like ``_rule_id`` it is never serialized. The row is listed and rolls up (its
# ``not_assessed`` keeps the clause from ever reaching ``compliant``), but it is not evidence:
# ``POST /qms-standards/{id}/auto-link`` never persists it and does not count its clause as a
# clause with evidence.
NOT_TRACKED_KEY = "_not_tracked"

# The text says only what auto-evidence can see. A person may well have linked an audit report
# or review minutes to the clause, and a CAR may cite an internal audit, so the row must not
# claim the ERP holds nothing about either -- only that it has no record type to count.
INTERNAL_AUDIT_REVIEW_TITLE = "Internal Audit & Management Review (Not Assessed Automatically)"
INTERNAL_AUDIT_REVIEW_DESCRIPTION = (
    "Auto-evidence cannot assess this clause: the ERP has no internal-audit, audit-program or "
    "management-review record to count. Assess it from the audit program, audit reports and "
    "management review records, and link those to this clause as evidence."
)
INTERNAL_AUDIT_REVIEW_DETAIL = (
    "Not assessed automatically. The ERP's change log records system changes; it is not evidence "
    "that an internal audit was run or a management review held."
)


def _query_internal_audit_review(db: Session, company_id: int) -> dict:
    """The row for a clause about internal audit (AS9100D / ISO 9001 9.2) or management review (9.3).

    The ERP has no internal-audit, audit-program or management-review record, so there is
    nothing to count and the honest answer is ``not_assessed``. This replaced routing those
    clauses to ``_query_audit_log``: the ERP's change log has entries on any working system,
    so it rolled 9.2 / 9.3 up ``compliant`` with no evidence that an audit was run or a review
    held. NCRs with source ``internal_audit`` and CARs initiated by an internal audit or a
    management review are deliberately NOT counted here: they show a finding was raised, not
    that the program ran, and their absence would say nothing at all.

    Runs no query (the signature matches the other rules). Its zero counts mean "no record of
    this kind exists to count", which is true; ``no_data`` says the same.
    """
    return {
        "evidence_type": "other",
        "title": INTERNAL_AUDIT_REVIEW_TITLE,
        "description": INTERNAL_AUDIT_REVIEW_DESCRIPTION,
        "module_reference": "/qms-standards",
        "total_count": 0,
        "recent_count": 0,
        "health_status": "no_data",
        "health_detail": INTERNAL_AUDIT_REVIEW_DETAIL,
        "examples": [],
        "suggested_compliance": "not_assessed",
        NOT_TRACKED_KEY: True,
    }


def is_not_tracked(result: dict) -> bool:
    """True for a row reporting that the ERP holds no record of what its clause asks for."""
    return bool(result.get(NOT_TRACKED_KEY))


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _query_maintenance(db: Session, company_id: int, today: Optional[date] = None) -> dict:
    """Preventive-maintenance evidence (AS9100D / ISO 9001 7.1.3 infrastructure).

    The verdict is built from the PM *schedules*, not just maintenance work orders, because a
    schedule is where a PM comes due: a shop can hold thirty schedules and zero work orders, and
    the old rule (every schedule counted, overdue read off work orders only) called exactly that
    "healthy / compliant" before a single PM had been performed -- and kept saying so with every
    schedule deactivated. It errs restrictive on purpose; a false "compliant" in front of a
    registrar is worse than "not assessed":

    * only ``is_active`` schedules count -- the same definition the Maintenance page lists by;
    * a schedule whose ``next_due_date`` is before the company's today is overdue, as is an open
      maintenance work order past its ``due_date``; any overdue item -> critical / non_compliant
      (the calibration rule's verdict for an overdue gauge);
    * a schedule has performance evidence only with a ``last_completed_date`` or a COMPLETED
      work order of this company linked to it. None of the active schedules performed ->
      not_assessed; some unperformed, or some with no due date -> partial;
    * "compliant" needs every active schedule current, dated and performed at least once.

    ``today`` is injectable for tests; it defaults to the company's local date (America/Chicago
    unless the company says otherwise). Response shape is unchanged: ``total_count`` is still
    the maintenance work-order count and ``recent_count`` the (now active-only) schedule count.
    """
    if today is None:
        today = company_today(db, company_id)

    schedules = (
        db.query(
            MaintenanceSchedule.id,
            MaintenanceSchedule.next_due_date,
            MaintenanceSchedule.last_completed_date,
        )
        .filter(
            MaintenanceSchedule.company_id == company_id,
            MaintenanceSchedule.is_active == True,  # noqa: E712
        )
        .all()
    )
    # Schedules this company has closed a work order against. Scoped on the WORK ORDER's
    # company: a pre-fix row owned by another tenant can still carry this tenant's schedule_id
    # (see create_work_order in api/endpoints/maintenance.py), and it must not count as our PM.
    performed_schedule_ids = {
        schedule_id
        for (schedule_id,) in db.query(MaintenanceWorkOrder.schedule_id)
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status == MaintenanceStatus.COMPLETED,
            MaintenanceWorkOrder.schedule_id.isnot(None),
        )
        .distinct()
        .all()
    }

    active_schedules = len(schedules)
    overdue_schedules = sum(1 for s in schedules if s.next_due_date is not None and s.next_due_date < today)
    undated_schedules = sum(1 for s in schedules if s.next_due_date is None)
    unperformed_schedules = sum(
        1 for s in schedules if s.last_completed_date is None and s.id not in performed_schedule_ids
    )

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
    # depending on whether a human had loaded the Maintenance page today. Same
    # status set as GET /maintenance/dashboard, against the company's local today.
    overdue_wos = (
        db.query(func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_([MaintenanceStatus.SCHEDULED, MaintenanceStatus.OVERDUE]),
            MaintenanceWorkOrder.due_date < today,
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

    if active_schedules == 0 and total_wos == 0:
        health = "no_data"
        health_detail = "No active PM schedules or maintenance records found"
        suggested = "not_assessed"
    elif overdue_schedules or overdue_wos:
        overdue_parts = []
        if overdue_schedules:
            overdue_parts.append(f"{_plural(overdue_schedules, 'active PM schedule', 'active PM schedules')} past due")
        if overdue_wos:
            overdue_parts.append(f"{_plural(overdue_wos, 'maintenance work order', 'maintenance work orders')} overdue")
        health = "critical"
        health_detail = "; ".join(overdue_parts)
        suggested = "non_compliant"
    elif active_schedules == 0:
        health = "warning"
        health_detail = (
            f"No active PM schedules: {_plural(total_wos, 'maintenance work order', 'maintenance work orders')} "
            "on record but no preventive maintenance plan"
        )
        suggested = "partial"
    elif unperformed_schedules == active_schedules:
        health = "warning"
        health_detail = (
            f"No PM performed yet: {_plural(active_schedules, 'active PM schedule', 'active PM schedules')}, "
            "none with a completion on record"
        )
        suggested = "not_assessed"
    elif unperformed_schedules or undated_schedules:
        gap_parts = []
        if unperformed_schedules:
            gap_parts.append(f"{unperformed_schedules} of {active_schedules} active PM schedules never performed")
        if undated_schedules:
            gap_parts.append(
                f"{_plural(undated_schedules, 'active PM schedule has', 'active PM schedules have')} no next due date"
            )
        health = "warning"
        health_detail = "; ".join(gap_parts)
        suggested = "partial"
    else:
        health = "healthy"
        health_detail = (
            f"All {_plural(active_schedules, 'active PM schedule', 'active PM schedules')} current and performed; "
            f"{completed}/{total_wos} maintenance work orders completed"
        )
        suggested = "compliant"

    return dict(
        evidence_type="module",
        title="Preventive Maintenance & Infrastructure",
        description=(
            f"{active_schedules} active PM schedules ({overdue_schedules} past due, "
            f"{unperformed_schedules} never performed), "
            f"{total_wos} maintenance WOs ({completed} completed, {overdue_wos} overdue)"
        ),
        module_reference="/maintenance",
        total_count=total_wos,
        recent_count=active_schedules,
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
        # The ERP's own change log. Only for a clause that names it: a clause about internal
        # audit or management review goes to "internal_audit_review" below, because change-log
        # entries exist on any working system and are no evidence that an audit was run.
        "id": "audit",
        "keywords": ["audit trail", "audit log"],
        "query_fn": _query_audit_log,
    },
    {
        # The ERP has no record of these; the row always says so (not_assessed), which keeps
        # the clause from ever rolling up compliant. "audit program" also matches the
        # standards' "audit programme". The phrases after the first three catch common
        # paraphrases (a company quality manual's "Internal Quality Audits", "review by top
        # management") and are kept specific on purpose: a bare "quality audit" or "audit plan"
        # would also hit supplier-audit clauses.
        "id": "internal_audit_review",
        "evidence_type": "other",
        "title": INTERNAL_AUDIT_REVIEW_TITLE,
        "module_reference": "/qms-standards",
        "keywords": [
            "internal audit",
            "audit program",
            "management review",
            "internal quality audit",
            "audits at planned intervals",
            "audit at planned intervals",
            "top management shall review",
            "review by top management",
        ],
        "query_fn": _query_internal_audit_review,
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


def matched_rule_ids(clause: QMSClause) -> Set[str]:
    """Ids of every rule whose keywords match the clause's current text.

    Taken from the keywords, not from ``discover_evidence_for_clause``'s result rows, so a rule
    that matched but whose query failed this run still counts as matched: auto-link retires an
    auto-linked evidence row only when its rule no longer matches, never because of a
    transient query failure.
    """
    return {rule["id"] for rule in _match_rules(clause)}


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
