"""Operator-qualification gate at clock-in / operation start (Batch 11C, G5-B).

WARN-AND-RECORD posture (mirrors ``quality_gate_service``): clock-in / start still
SUCCEEDS when the operator is not qualified, but every unqualified start leaves a
tamper-evident audit row + a warning operational event so it is discoverable. This
module owns ONLY the *detection* (read-only, tenant-scoped) and the recording helper;
the shop-floor endpoints call it and never block on the result.

"Qualified" for a work center means BOTH legs pass:

* SKILL leg: an active ``SkillMatrix`` entry for (operator, work_center) with
  ``skill_level >= MIN_SKILL_LEVEL`` (2 = Basic). No entry, or a below-Basic entry,
  is an ``operator_not_skill_qualified`` exception.
* CERTIFICATION leg: where ``WorkCenter.required_certification_type`` is set, the
  operator must hold a current (active / expiring_soon) ``OperatorCertification`` of
  that type. Otherwise it is an ``operator_certification_missing_or_expired``
  exception. When the work center has no required cert type (the common case) this
  leg is skipped.

Detection is strictly read-only -- it never mutates a row. Every query filters
``company_id`` (unlike the legacy ``check_operator_qualification`` endpoint helper,
which is NOT tenant-scoped).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.operator_certification import (
    CertificationStatus,
    OperatorCertification,
    SkillMatrix,
)
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

# Audit action verb stamped on the tamper-evident chain when a clock-in / start
# proceeds despite an unsatisfied operator-qualification gate. Distinct verb so the
# bypass is greppable in the audit trail.
OPERATOR_QUALIFICATION_AUDIT_ACTION = "OPERATOR_QUALIFICATION_EXCEPTION"
OPERATOR_QUALIFICATION_EVENT_TYPE = "operator_qualification_exception"

# Minimum SkillMatrix skill_level (2 = "Basic") an operator must hold for the op's
# work center. Matches the legacy ``check_operator_qualification`` endpoint threshold.
MIN_SKILL_LEVEL = 2

# Effective certification statuses that count as qualifying (current / not expired).
_QUALIFYING_CERT_STATUSES = ("active", "expiring_soon")


@dataclass(frozen=True)
class QualificationException:
    """One unsatisfied operator-qualification gate detected at clock-in / start.

    ``severity`` is advisory metadata for the warn-and-record posture -- it never
    blocks the start. ``reference_type`` / ``reference_id`` point at the offending
    record (the SkillMatrix entry or the work center) so an auditor can jump to it.
    """

    code: str
    severity: str
    message: str
    reference_type: str
    reference_id: Optional[int]

    def as_dict(self) -> dict:
        """Serializable shape for the API response + audit ``extra_data``."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "reference_type": self.reference_type,
            "reference_id": self.reference_id,
        }


def _effective_cert_status(cert: OperatorCertification) -> str:
    """Compute a certification's effective status from its expiration date.

    SOURCE OF TRUTH: this replicates ``compute_cert_status`` in
    ``app/api/endpoints/operator_certifications.py`` (the endpoint module). We copy
    the ~12-line logic here rather than import an endpoint into a service. Keep the
    two in sync if the expiry policy changes:
      REVOKED -> "revoked"; PENDING -> "pending"; no expiration_date -> "active";
      expired (expiration_date < today) -> "expired"; expiring within 30 days
      (<= today + 30d) -> "expiring_soon"; otherwise -> "active".
    """
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


def evaluate_operator_qualification(
    db: Session,
    *,
    user_id: int,
    work_center_id: int,
    company_id: int,
) -> list[QualificationException]:
    """Detect every unsatisfied operator-qualification gate for (operator, work center).

    Read-only and tenant-scoped (every query filters ``company_id``). Returns a
    structured list -- the caller records each exception (audit + event) and returns
    them in the response. Never raises on a "gate failed"; only the presence of an
    exception in the list signals it.
    """
    exceptions: list[QualificationException] = []

    # SKILL leg: an active SkillMatrix entry at >= Basic level for this work center.
    entry = (
        db.query(SkillMatrix)
        .filter(
            SkillMatrix.company_id == company_id,
            SkillMatrix.user_id == user_id,
            SkillMatrix.work_center_id == work_center_id,
            SkillMatrix.is_active == True,  # noqa: E712
        )
        .first()
    )
    if entry is None or entry.skill_level < MIN_SKILL_LEVEL:
        exceptions.append(
            QualificationException(
                code="operator_not_skill_qualified",
                severity="medium",
                message=(
                    f"Operator is not skill-qualified (level {entry.skill_level if entry else 0} "
                    f"< {MIN_SKILL_LEVEL}) for this work center."
                ),
                reference_type="skill_matrix",
                reference_id=(entry.id if entry else None),
            )
        )

    # CERTIFICATION leg: only when the work center declares a required cert type.
    wc = (
        db.query(WorkCenter)
        .filter(
            WorkCenter.id == work_center_id,
            WorkCenter.company_id == company_id,
        )
        .first()
    )
    if wc is not None and wc.required_certification_type is not None:
        certs = (
            db.query(OperatorCertification)
            .filter(
                OperatorCertification.company_id == company_id,
                OperatorCertification.user_id == user_id,
                OperatorCertification.certification_type == wc.required_certification_type,
            )
            .all()
        )
        qualified = any(_effective_cert_status(cert) in _QUALIFYING_CERT_STATUSES for cert in certs)
        if not qualified:
            exceptions.append(
                QualificationException(
                    code="operator_certification_missing_or_expired",
                    severity="high",
                    message=(
                        f"Operator lacks a current {wc.required_certification_type.value} "
                        "certification required by this work center."
                    ),
                    reference_type="work_center",
                    reference_id=wc.id,
                )
            )

    return exceptions


def record_operator_qualification_exceptions(
    db: Session,
    *,
    company_id: int,
    user: User,
    operation: WorkOrderOperation,
    work_center_id: int,
    exceptions: list[QualificationException],
    audit: AuditService,
    source: str,
) -> None:
    """Leave a tamper-evident record + a realtime signal for an unqualified start.

    WARN-AND-RECORD: the clock-in / start has already mutated state and is about to
    commit in the same unit of work; this records the bypass so it is discoverable. It
    does NOT block and does NOT commit -- it only flushes via ``AuditService.log`` /
    ``OperationalEventService.emit`` (both flush, never commit), so the rows commit
    atomically with the operation via the caller's single commit.

    * Writes ONE ``audit_log`` row (action ``OPERATOR_QUALIFICATION_EXCEPTION``)
      against the operation, carrying the exception codes + references in
      ``extra_data``. Never writes ``audit_log`` directly.
    * Emits ONE ``OperationalEvent`` (severity ``warning``) for AI / realtime context.

    Best-effort and defensive: a failure to record a warning must never break the
    start itself. ``AuditService.log`` already swallows its own failures; the
    ``OperationalEvent`` emit is guarded here.
    """
    if not exceptions:
        return

    codes = [exc.code for exc in exceptions]
    payload_exceptions = [exc.as_dict() for exc in exceptions]
    work_order_id = operation.work_order_id

    audit.log(
        action=OPERATOR_QUALIFICATION_AUDIT_ACTION,
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=operation.operation_number or str(operation.id),
        description=(
            f"Operator {user.id} started operation {operation.operation_number or operation.id} "
            f"with {len(exceptions)} unsatisfied qualification gate(s): {', '.join(codes)}"
        ),
        new_values={"qualification_exceptions": codes},
        extra_data={
            "source": source,
            "work_order_id": work_order_id,
            "operation_id": operation.id,
            "work_center_id": work_center_id,
            "qualification_exceptions": payload_exceptions,
        },
        company_id=company_id,
    )

    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=OPERATOR_QUALIFICATION_EVENT_TYPE,
            source_module="operator_qualification",
            entity_type="work_order_operation",
            entity_id=operation.id,
            work_order_id=work_order_id,
            operation_id=operation.id,
            user_id=user.id,
            severity="warning",
            event_payload={
                "source": source,
                "work_center_id": work_center_id,
                "qualification_exception_codes": codes,
                "qualification_exceptions": payload_exceptions,
            },
        )
    except Exception:  # pragma: no cover - a warning signal must never break the start
        # The audit row above is the compliance record; the operational event is an
        # AI/realtime convenience. If emitting it fails, swallow so the start the
        # caller is committing is unaffected.
        pass


def evaluate_and_record_operator_qualification(
    db: Session,
    *,
    company_id: int,
    user: User,
    operation: WorkOrderOperation,
    work_center_id: int,
    audit: AuditService,
    source: str,
) -> list[QualificationException]:
    """Detect + record in one call; returns the exceptions for the API response.

    The single entry point the clock-in / start endpoints use: it runs the read-only
    evaluator, records each bypass (audit + event), and hands the list back so the
    response can surface ``qualification_exceptions`` to the client. Warn-only -- it
    never raises on a failed gate.
    """
    exceptions = evaluate_operator_qualification(
        db,
        user_id=user.id,
        work_center_id=work_center_id,
        company_id=company_id,
    )
    if exceptions:
        record_operator_qualification_exceptions(
            db,
            company_id=company_id,
            user=user,
            operation=operation,
            work_center_id=work_center_id,
            exceptions=exceptions,
            audit=audit,
            source=source,
        )
    return exceptions
