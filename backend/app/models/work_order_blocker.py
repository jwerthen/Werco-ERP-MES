import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import TenantMixin


class WorkOrderBlockerCategory(str, enum.Enum):
    MATERIAL_MISSING = "material_missing"
    MACHINE_DOWN = "machine_down"
    TOOLING_MISSING = "tooling_missing"
    QUALITY_HOLD = "quality_hold"
    LABOR_UNAVAILABLE = "labor_unavailable"
    ENGINEERING_QUESTION = "engineering_question"
    PREVIOUS_OPERATION = "previous_operation"
    OTHER = "other"


class WorkOrderBlockerSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WorkOrderBlockerStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class BlockerResumeWithheldReason(str, enum.Enum):
    """Why closing a blocker did NOT take its operation off hold. CLOSED VOCABULARY.

    Closing a blocker resumes its operation as a side effect
    (``WorkOrderBlockerService._resume_operation_if_no_open_blockers``) -- but only
    sometimes, and the four situations where it does not used to collapse into a
    bare ``(None, None)`` that the response could not express. A shop owner
    resolved a blocker on a held nest, got a green "Resolved blocker" toast, and
    the operation was still ON_HOLD. These names are what the response says now.

    ``other_blockers_open`` is the one that matters, and it is categorically
    different from the other three: there a resume was OWED -- the operation IS on
    hold and the caller reasonably expects their click to lift it -- and something
    withheld it. The other three mean there was nothing to resume in the first
    place, and warning "still held" for one of those would be a NEW kind of
    dishonesty, not a fix. The wire field that draws that line is
    ``BlockerOperationOutcome.operation_still_held``; see
    ``BlockerResumeOutcome.operation_still_held`` for why the two are one fact.

    NOT PERSISTED and not a column -- it lives here, beside the status vocabulary
    it explains, to break an import cycle: the response schema has to name these
    values, ``app/schemas/work_order_blocker.py`` is imported BY
    ``work_order_blocker_service``, and having that schema import the service back
    is a cycle. Models import neither, so this is the one home both can reach. The
    service re-exports it, so it still reads as co-located at the call sites that
    raise it.

    STABLE: the UI and ``docs/API.md`` key on these strings. Add members rather
    than renaming, and add them fail-closed -- a new member must describe a
    situation that genuinely withheld a resume, because every consumer reads an
    unknown reason as "something stopped it". The laser-nest tombstone guard (a
    cancelled nest's operation must never be resumed) lives on the unmerged
    nest-removal branch and slots in here as a fifth member.

    The FIFTH OUTCOME is deliberately NOT a member: an operation that DID resume
    but landed at PENDING instead of READY. Nothing was withheld there -- the hold
    genuinely cleared -- so it is reported as ``operation_resumed=True`` with
    ``operation_status="pending"``, and the UI warns off the status. Folding it in
    here would make "withheld" mean two different things.
    """

    NO_OPERATION = "no_operation"
    OTHER_BLOCKERS_OPEN = "other_blockers_open"
    OPERATION_NOT_HELD = "operation_not_held"
    OPERATION_MISSING = "operation_missing"


class WorkOrderBlocker(Base, TenantMixin):
    """Operator-reported blocker that explains why a job or operation is stuck."""

    __tablename__ = "work_order_blockers"
    __table_args__ = (
        Index("ix_work_order_blockers_company_status", "company_id", "status", "severity"),
        Index("ix_work_order_blockers_company_category", "company_id", "category", "status"),
        Index("ix_work_order_blockers_company_work_order", "company_id", "work_order_id", "status"),
        Index("ix_work_order_blockers_company_operation", "company_id", "operation_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    material_part_id = Column(Integer, ForeignKey("parts.id"), nullable=True, index=True)
    # The NCR a QUALITY_HOLD blocker was raised with (process-sheets OOT escape hatch, PR 4).
    # Nullable — the link was previously "cultural"; no behavior here.
    ncr_id = Column(Integer, ForeignKey("ncrs.id"), nullable=True, index=True)

    category = Column(String(40), nullable=False, default=WorkOrderBlockerCategory.OTHER.value, index=True)
    severity = Column(String(20), nullable=False, default=WorkOrderBlockerSeverity.MEDIUM.value, index=True)
    status = Column(String(20), nullable=False, default=WorkOrderBlockerStatus.OPEN.value, index=True)

    title = Column(String(255), nullable=False)
    note = Column(Text, nullable=True)
    resolution_note = Column(Text, nullable=True)

    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    reported_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    work_order = relationship("WorkOrder")
    operation = relationship("WorkOrderOperation")
    material_part = relationship("Part")
    ncr = relationship("NonConformanceReport")
    reporter = relationship("User", foreign_keys=[reported_by])
    assignee = relationship("User", foreign_keys=[assigned_to])
    resolver = relationship("User", foreign_keys=[resolved_by])
