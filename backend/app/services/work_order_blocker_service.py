from __future__ import annotations

# Aliased: ``update_blocker`` iterates ``for field, value in update_data.items()``
# and an unaliased ``field`` would be shadowed by that loop variable (flake8 F402).
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.ai_learning import AIRecommendation
from app.models.quality import NonConformanceReport
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import (
    BlockerResumeWithheldReason,
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerStatus,
)
from app.schemas.work_order_blocker import WorkOrderBlockerCreate, WorkOrderBlockerUpdate
from app.services.audit_service import AuditService
from app.services.operation_action_gates import operation_blocked_by_predecessors
from app.services.operational_event_service import OperationalEventService, redact_event_payload
from app.services.work_order_state_service import TERMINAL_WO_STATUSES


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def blocker_default_title(category: str, work_order: WorkOrder, operation: Optional[WorkOrderOperation]) -> str:
    """The title this service composes when the caller supplies none.

    PUBLIC on purpose. ``work_order_blockers.title`` is ``nullable=False`` but
    caller-settable free text (``WorkOrderBlockerCreate.title``, an optional
    255-char string), so "did a human write this title, or did the server compose
    it?" cannot be answered from the column alone -- and the kiosk's free-text
    disclosure gate (``shop_floor._blocker_free_text_recorded``) has to answer
    exactly that before it can say whether a written reason EXISTS. Re-deriving
    this format at that call site would let the two drift, and the drift would be
    silent: the flag would simply start lying about whether somebody wrote
    something.
    """
    label = category.replace("_", " ").title()
    target = operation.name if operation else work_order.work_order_number
    return f"{label}: {target}"


@dataclass
class BlockerResumeOutcome:
    """What closing a blocker actually did to its operation.

    Replaces the ``(None, None)`` this service used to return, which collapsed
    four distinct situations into one silence the response could not express --
    the reason a shop owner could resolve a blocker on a held nest, get a green
    "Resolved blocker" toast, and find the operation still ON_HOLD.

    THIS IS A DISCLOSURE OBJECT, NOT A DECISION. Nothing here changes when a
    resume happens, where it lands, or what it writes. Every field is read off a
    decision the resume rule had already made; adding one must never move the
    rule.

    ``previous_status`` is set on the RESUME PATH ONLY, and that is load-bearing:
    :meth:`WorkOrderBlockerService.update_blocker_with_outcome` writes the
    operation's ``log_status_change`` row under exactly
    ``operation is not None and previous_status is not None``, unchanged from
    before this object existed. A status-change row for a transition that did not
    happen is a false entry in a tamper-evident chain (invariant 2), so
    ``operation`` alone must never be the guard -- it is now populated in the
    WITHHELD cases too, purely so the response can report the operation's status.
    """

    #: The operation id the BLOCKER names -- present even when the row could not
    #: be loaded, which is the whole point: OPERATION_MISSING has an id and no row.
    operation_id: Optional[int] = None
    #: The operation the blocker names, when the row could be loaded. Populated
    #: for disclosure even when no resume happened -- never a "did it resume?"
    #: signal on its own. ``None`` for NO_OPERATION / OPERATION_MISSING.
    operation: Optional[WorkOrderOperation] = None
    #: The status the operation held BEFORE this call, set only when it actually
    #: moved. The audit guard keys on this; see the class docstring.
    previous_status: Optional[str] = None
    #: ``None`` when a resume was not withheld -- i.e. one happened.
    withheld_reason: Optional[BlockerResumeWithheldReason] = None
    #: The OPEN/ACKNOWLEDGED blockers still naming this operation. Non-empty only
    #: for OTHER_BLOCKERS_OPEN; they are what the caller has to close next.
    other_open_blockers: List[WorkOrderBlocker] = dataclass_field(default_factory=list)

    @property
    def resumed(self) -> bool:
        """True when the operation actually moved off hold in this call."""
        return self.operation is not None and self.previous_status is not None

    @property
    def operation_still_held(self) -> bool:
        """A resume was OWED and WITHHELD -- the one thing the UI must warn about.

        This is the line the reason vocabulary alone cannot draw. ``no_operation``
        / ``operation_not_held`` / ``operation_missing`` all mean *there was
        nothing to resume*, and warning "still held" for one of those would be a
        new kind of dishonesty. ``other_blockers_open`` usually means a resume was
        owed -- but not always: another blocker can name an operation somebody
        already resumed from the kiosk without closing the record, and that
        operation is not held either.

        So the judgement is read off the operation, not off the reason: the
        operation exists and is STILL ``ON_HOLD`` after this call. That is
        equivalent to "a resume was owed and withheld" in both directions -- a
        resume that happened leaves the row not-ON_HOLD, and a row that was never
        ON_HOLD owed nothing -- and it stays correct when a fifth withheld reason
        is added (the cancelled-nest tombstone on the nest-removal branch), which
        a reason-name test would not.

        Compared with ``==`` against the enum member exactly as the resume gate
        itself compares, deliberately: if the two ever read the column
        differently, the disclosure would contradict the decision it describes.
        """
        return self.operation is not None and self.operation.status == OperationStatus.ON_HOLD

    @property
    def operation_status(self) -> Optional[str]:
        """The operation's status AFTER this call. ``None`` when there is no row."""
        return _enum_value(self.operation.status) if self.operation is not None else None


class WorkOrderBlockerService:
    """First-class shop-floor blocker service with notifications and AI signals."""

    def __init__(self, db: Session):
        self.db = db

    def list_blockers(
        self,
        *,
        company_id: int,
        work_order_id: Optional[int] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[WorkOrderBlocker]:
        query = (
            self.db.query(WorkOrderBlocker)
            .options(
                joinedload(WorkOrderBlocker.work_order),
                joinedload(WorkOrderBlocker.operation),
                joinedload(WorkOrderBlocker.material_part),
            )
            .filter(WorkOrderBlocker.company_id == company_id)
        )
        if work_order_id is not None:
            query = query.filter(WorkOrderBlocker.work_order_id == work_order_id)
        if status:
            query = query.filter(WorkOrderBlocker.status == status)
        if category:
            query = query.filter(WorkOrderBlocker.category == category)
        return query.order_by(WorkOrderBlocker.reported_at.desc()).limit(limit).all()

    def create_blocker(
        self,
        *,
        company_id: int,
        user: User,
        work_order_id: int,
        data: WorkOrderBlockerCreate,
        audit: Optional[AuditService] = None,
        source: Optional[str] = None,
    ) -> WorkOrderBlocker:
        # ``source`` is the A0.1 adoption-telemetry client channel
        # (kiosk/desktop/scanner/import/backfill) when the triggering request
        # supplied one; None means unknown/not reported (e.g. office paths).
        # Passed through to the work_order_blocker_created event payload only.
        work_order = (
            self.db.query(WorkOrder)
            .options(joinedload(WorkOrder.operations))
            .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
            .first()
        )
        if not work_order:
            raise ValueError("Work order not found")

        operation = None
        if data.operation_id:
            operation = (
                self.db.query(WorkOrderOperation)
                .filter(
                    WorkOrderOperation.id == data.operation_id,
                    WorkOrderOperation.work_order_id == work_order.id,
                    WorkOrderOperation.company_id == company_id,
                )
                .first()
            )
            if not operation:
                raise ValueError("Operation not found for this work order")

        # PR 4 (process sheets): a QUALITY_HOLD blocker may carry the NCR it was
        # raised with. Tenant-validated — a cross-tenant/unknown NCR id never links.
        if data.ncr_id is not None:
            ncr_exists = (
                self.db.query(NonConformanceReport.id)
                .filter(
                    NonConformanceReport.id == data.ncr_id,
                    NonConformanceReport.company_id == company_id,
                    NonConformanceReport.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if not ncr_exists:
                raise ValueError("NCR not found")

        category = _enum_value(data.category)
        severity = _enum_value(data.severity)
        blocker = WorkOrderBlocker(
            company_id=company_id,
            work_order_id=work_order.id,
            operation_id=operation.id if operation else None,
            material_part_id=data.material_part_id,
            ncr_id=data.ncr_id,
            category=category,
            severity=severity,
            status=WorkOrderBlockerStatus.OPEN.value,
            title=data.title or blocker_default_title(category, work_order, operation),
            note=redact_event_payload(data.note),
            reported_by=user.id,
            assigned_to=data.assigned_to,
        )
        self.db.add(blocker)
        self.db.flush()

        operation_previous_status = None
        if data.put_operation_on_hold and operation and operation.status != OperationStatus.COMPLETE:
            operation_previous_status = _enum_value(operation.status)
            operation.status = OperationStatus.ON_HOLD
            operation.updated_at = datetime.utcnow()

        OperationalEventService(self.db).emit_best_effort(
            company_id=company_id,
            event_type="work_order_blocker_created",
            source_module="work_orders",
            entity_type="work_order_blocker",
            entity_id=blocker.id,
            work_order_id=work_order.id,
            operation_id=operation.id if operation else None,
            user_id=user.id,
            severity=severity,
            event_payload={
                "category": category,
                "title": blocker.title,
                "work_order_number": work_order.work_order_number,
                "operation_name": operation.name if operation else None,
                # A0.1 adoption telemetry: client channel (None = not reported).
                "source": source,
            },
        )
        # NOTE: the wo.blocker_created in-app/email notification is now owned by the
        # transactional-outbox notification pipeline (driven by the emitted
        # ``work_order_blocker_created`` / ``operation_hold`` OperationalEvents above);
        # the legacy direct NotificationLog write was removed to avoid a double-fire.
        self._create_blocker_recommendation(company_id=company_id, user=user, blocker=blocker, work_order=work_order)

        # Tamper-evident audit trail (hash chain): the blocker creation and any
        # operation status mutation it triggers. Flushed (not committed) so the
        # audit rows commit atomically with the blocker via the caller's commit.
        if audit is not None:
            audit.log_create(
                "work_order_blocker",
                blocker.id,
                blocker.title,
                new_values=blocker,
                description=(
                    f"Reported {category} blocker on work order {work_order.work_order_number}"
                    + (f" operation {operation.name}" if operation else "")
                ),
            )
            if operation is not None and operation_previous_status is not None:
                audit.log_status_change(
                    "work_order_operation",
                    operation.id,
                    operation.name or str(operation.id),
                    operation_previous_status,
                    _enum_value(operation.status),
                    description=(
                        f"Operation {operation.name} put on hold by blocker {blocker.title} "
                        f"on work order {work_order.work_order_number}"
                    ),
                )

        self.db.flush()
        return blocker

    def update_blocker(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        data: WorkOrderBlockerUpdate,
        audit: Optional[AuditService] = None,
    ) -> WorkOrderBlocker:
        """Backwards-compatible wrapper -- the blocker only, no resume outcome.

        Kept for the caller that cannot act on the outcome anyway --
        ``ai_action_applier``'s escalate/acknowledge, the only non-HTTP caller of
        either wrapper (``copilot_service`` and ``process_sheet_service`` reach
        this service, but only through ``list_blockers`` / ``create_blocker``).
        The two HTTP verbs use
        :meth:`update_blocker_with_outcome`, because a caller who can be MISLED
        about whether the operation came off hold is exactly who needs the answer.
        """
        return self.update_blocker_with_outcome(
            company_id=company_id,
            user=user,
            blocker_id=blocker_id,
            data=data,
            audit=audit,
        )[0]

    def update_blocker_with_outcome(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        data: WorkOrderBlockerUpdate,
        audit: Optional[AuditService] = None,
    ) -> Tuple[WorkOrderBlocker, Optional[BlockerResumeOutcome]]:
        """Update the blocker, and report what that did to its operation.

        The second element is ``None`` when NO RESUME WAS EVEN ATTEMPTED -- this
        call did not leave the blocker RESOLVED/DISMISSED, so the operation was
        never a candidate. That is a third state, distinct from "attempted and
        withheld", and collapsing it into a withheld reason would have the UI warn
        about a hold on an acknowledge.

        Note the attempt condition tests the blocker's status AFTER the update,
        not the transition: re-PUTting an already-resolved blocker re-attempts the
        resume. That is pre-existing behavior and is left exactly as it was.
        """
        blocker = self._get_blocker(company_id=company_id, blocker_id=blocker_id)
        previous_status = blocker.status
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is None:
                continue
            setattr(blocker, field, _enum_value(value) if hasattr(value, "value") else value)

        resume: Optional[BlockerResumeOutcome] = None
        if blocker.status == WorkOrderBlockerStatus.ACKNOWLEDGED.value and previous_status != blocker.status:
            blocker.acknowledged_at = datetime.utcnow()
        if blocker.status in {WorkOrderBlockerStatus.RESOLVED.value, WorkOrderBlockerStatus.DISMISSED.value}:
            blocker.resolved_at = blocker.resolved_at or datetime.utcnow()
            blocker.resolved_by = blocker.resolved_by or user.id
            resume = self._resume_operation_if_no_open_blockers(blocker)
        blocker.updated_at = datetime.utcnow()

        OperationalEventService(self.db).emit_best_effort(
            company_id=company_id,
            event_type="work_order_blocker_updated",
            source_module="work_orders",
            entity_type="work_order_blocker",
            entity_id=blocker.id,
            work_order_id=blocker.work_order_id,
            operation_id=blocker.operation_id,
            user_id=user.id,
            severity=blocker.severity,
            event_payload={"status": blocker.status, "previous_status": previous_status},
        )

        # Tamper-evident audit trail (hash chain): the blocker status transition
        # and any operation resume it triggers. Flushed (not committed) so the
        # audit rows commit atomically with the blocker via the caller's commit.
        if audit is not None and blocker.status != previous_status:
            audit.log_status_change(
                "work_order_blocker",
                blocker.id,
                blocker.title,
                previous_status,
                blocker.status,
                description=f"Blocker '{blocker.title}' status changed from '{previous_status}' to '{blocker.status}'",
            )
        # INVARIANT 2, UNCHANGED BY THE DISCLOSURE REFACTOR. The operation
        # status-change row is written under exactly the condition it always was:
        # an operation AND a previous status, both of which only the resume path
        # sets. ``resume.operation`` alone would NOT do -- it is now populated in
        # the withheld cases too, so that a response can report the operation's
        # status, and keying the row on it would write a status change for a
        # transition that never happened: a false entry in a tamper-evident chain.
        # ``resume is not None`` is the "no resume was attempted" case, where both
        # locals used to be None and this branch was equally False.
        resumed_operation = resume.operation if resume is not None else None
        resumed_operation_previous_status = resume.previous_status if resume is not None else None
        if audit is not None and resumed_operation is not None and resumed_operation_previous_status is not None:
            audit.log_status_change(
                "work_order_operation",
                resumed_operation.id,
                resumed_operation.name or str(resumed_operation.id),
                resumed_operation_previous_status,
                _enum_value(resumed_operation.status),
                description=(
                    f"Operation {resumed_operation.name} resumed after blocker '{blocker.title}' was "
                    f"{blocker.status}"
                ),
            )

        self.db.flush()
        return blocker, resume

    def resolve_blocker(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        resolution_note: Optional[str] = None,
        audit: Optional[AuditService] = None,
    ) -> WorkOrderBlocker:
        """Backwards-compatible wrapper. See :meth:`resolve_blocker_with_outcome`."""
        return self.resolve_blocker_with_outcome(
            company_id=company_id,
            user=user,
            blocker_id=blocker_id,
            resolution_note=resolution_note,
            audit=audit,
        )[0]

    def resolve_blocker_with_outcome(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        resolution_note: Optional[str] = None,
        audit: Optional[AuditService] = None,
    ) -> Tuple[WorkOrderBlocker, Optional[BlockerResumeOutcome]]:
        return self.update_blocker_with_outcome(
            company_id=company_id,
            user=user,
            blocker_id=blocker_id,
            data=WorkOrderBlockerUpdate(
                status=WorkOrderBlockerStatus.RESOLVED,
                resolution_note=resolution_note,
            ),
            audit=audit,
        )

    def stale_open_blockers(
        self, *, company_id: Optional[int] = None, older_than_hours: int = 24
    ) -> List[WorkOrderBlocker]:
        cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
        query = self.db.query(WorkOrderBlocker).filter(
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
            WorkOrderBlocker.reported_at <= cutoff,
        )
        if company_id is not None:
            query = query.filter(WorkOrderBlocker.company_id == company_id)
        return query.order_by(WorkOrderBlocker.reported_at.asc()).all()

    def _get_blocker(self, *, company_id: int, blocker_id: int) -> WorkOrderBlocker:
        blocker = (
            self.db.query(WorkOrderBlocker)
            .options(
                joinedload(WorkOrderBlocker.work_order),
                joinedload(WorkOrderBlocker.operation),
                joinedload(WorkOrderBlocker.material_part),
            )
            .filter(WorkOrderBlocker.id == blocker_id, WorkOrderBlocker.company_id == company_id)
            .first()
        )
        if not blocker:
            raise ValueError("Blocker not found")
        return blocker

    def _resume_operation_if_no_open_blockers(self, blocker: WorkOrderBlocker) -> BlockerResumeOutcome:
        """Resume the operation if no other open blockers remain -- and SAY what happened.

        SAME RESUMES, SAME CASES, SAME LANDING STATUSES, SAME AUDIT ROWS as before
        the outcome object existed. This returns a :class:`BlockerResumeOutcome`
        where it used to return a bare ``(None, None)`` that four distinct
        situations shared; nothing about WHEN it resumes changed.

        Two reads are new and both are DISCLOSURE ONLY:

        * the operation row is now loaded BEFORE the open-blocker probe, so every
          branch can report the operation's status. That reorders which *reason*
          a doubly-disqualified call reports (a missing row now reads
          ``operation_missing`` rather than ``other_blockers_open``) and reports
          the more fundamental fact; it cannot change the resume, because the
          resume needs all four conditions to hold regardless of the order they
          are tested in.
        * the open-blocker probe is ``.all()`` where it was ``.count()``, on the
          identical predicate, so the response can name the blockers that are
          still in the way. Ordered by id so the list is stable between calls.

        Both are tenant-scoped to ``blocker.company_id`` -- the company the
        blocker was already resolved under (:meth:`_get_blocker` scopes it to the
        caller's active company), never a company re-derived here.
        """
        if not blocker.operation_id:
            return BlockerResumeOutcome(withheld_reason=BlockerResumeWithheldReason.NO_OPERATION)
        operation_id = int(blocker.operation_id)
        operation = (
            self.db.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.id == blocker.operation_id,
                WorkOrderOperation.company_id == blocker.company_id,
            )
            .first()
        )
        if operation is None:
            return BlockerResumeOutcome(
                operation_id=operation_id,
                withheld_reason=BlockerResumeWithheldReason.OPERATION_MISSING,
            )
        other_open_blockers = (
            self.db.query(WorkOrderBlocker)
            .filter(
                WorkOrderBlocker.company_id == blocker.company_id,
                WorkOrderBlocker.operation_id == blocker.operation_id,
                WorkOrderBlocker.id != blocker.id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .order_by(WorkOrderBlocker.id.asc())
            .all()
        )
        if other_open_blockers:
            return BlockerResumeOutcome(
                operation_id=operation_id,
                operation=operation,
                withheld_reason=BlockerResumeWithheldReason.OTHER_BLOCKERS_OPEN,
                other_open_blockers=other_open_blockers,
            )
        if operation.status == OperationStatus.ON_HOLD:
            previous_status = _enum_value(operation.status)
            parent = operation.work_order
            if operation.actual_start:
                # Already started before the hold -- clearing the blocker returns it to
                # live work regardless of sequencing. Never demote worked labor.
                operation.status = OperationStatus.IN_PROGRESS
            elif (
                parent is None
                or parent.status == WorkOrderStatus.DRAFT
                or parent.status in TERMINAL_WO_STATUSES
                or operation_blocked_by_predecessors(self.db, operation)
            ):
                # Floor at PENDING rather than lift to READY. READY is exactly what the
                # dispatch board and the kiosk queue surface
                # (``dispatch_service.QUEUE_OPERATION_STATUSES``), so every case here would
                # otherwise put a card on the floor that all four start verbs refuse -- and
                # nothing would heal it, because the read-path promotion only ever reads
                # PENDING rows. Three distinct reasons, all landing in the same place:
                #
                # * DRAFT parent: release is the authorization step and the record of who
                #   authorized production, so clearing a blocker must not put unreleased
                #   work on the board. DRAFT is NOT in TERMINAL_WO_STATUSES, so the queue
                #   query does not filter it out -- this branch is the only thing stopping
                #   it. Same carve-out ``shop_floor.resume_operation`` makes.
                # * TERMINAL parent: a finished or cancelled job never returns to the board.
                # * Predecessors incomplete: on a SEQUENCED routing this is any open
                #   lower-sequence operation; on a POOLED work order it is still an open
                #   lower-sequence operation at a DIFFERENT work center, or an ON_HOLD one
                #   at any. So this genuinely tightens pooled work orders too -- that is
                #   intended (the card it used to produce was refused on arrival), not a
                #   side effect of 081. Laser nests are unaffected: the shared gate
                #   short-circuits them to "not blocked".
                operation.status = OperationStatus.PENDING
            else:
                operation.status = OperationStatus.READY
            operation.updated_at = datetime.utcnow()
            # THE FIFTH OUTCOME, and it is not a withheld reason: the hold really
            # did clear, but PENDING means the job did NOT come back to the
            # dispatch board or the kiosk (both surface READY only). Nothing is
            # named here -- the landing status IS the disclosure, and the caller
            # reads it off ``operation_status``.
            return BlockerResumeOutcome(operation_id=operation_id, operation=operation, previous_status=previous_status)
        return BlockerResumeOutcome(
            operation_id=operation_id,
            operation=operation,
            withheld_reason=BlockerResumeWithheldReason.OPERATION_NOT_HELD,
        )

    def _create_blocker_recommendation(
        self,
        *,
        company_id: int,
        user: User,
        blocker: WorkOrderBlocker,
        work_order: WorkOrder,
    ) -> None:
        recommendation_type = (
            "material_blocker_triage"
            if blocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value
            else "shop_floor_blocker_triage"
        )
        existing = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.source_module == "shop_floor",
                AIRecommendation.recommendation_type == recommendation_type,
                AIRecommendation.target_entity_type == "work_order_blocker",
                AIRecommendation.target_entity_id == blocker.id,
            )
            .first()
        )
        if existing:
            return

        self.db.add(
            AIRecommendation(
                company_id=company_id,
                source_module="shop_floor",
                recommendation_type=recommendation_type,
                status="pending",
                priority="high" if blocker.severity in {"high", "critical"} else "medium",
                title=f"Clear blocker on {work_order.work_order_number}",
                summary=(
                    "Material is blocking this work order. Review inventory, open POs, and alternate stock."
                    if blocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value
                    else "A shop-floor blocker was reported. Review ownership and next action."
                ),
                rationale=blocker.note,
                target_entity_type="work_order_blocker",
                target_entity_id=blocker.id,
                suggested_action={
                    "type": "escalate_blocker",
                    "work_order_id": work_order.id,
                    "blocker_id": blocker.id,
                    "category": blocker.category,
                    "href": f"/work-orders/{work_order.id}",
                    "autonomy": "auto_execute",
                },
                evidence=[
                    {
                        "type": "operator_signal",
                        "label": blocker.title,
                        "detail": blocker.note,
                    }
                ],
                impact={"expected": "Reduce stuck WIP and improve schedule reliability."},
                confidence_score=0.78,
                created_by=user.id,
            )
        )
