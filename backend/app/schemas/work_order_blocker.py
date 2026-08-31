from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.work_order_blocker import (
    BlockerResumeWithheldReason,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.schemas.base import UTCModel


class WorkOrderBlockerCreate(BaseModel):
    operation_id: Optional[int] = Field(None, gt=0)
    material_part_id: Optional[int] = Field(None, gt=0)
    category: WorkOrderBlockerCategory = WorkOrderBlockerCategory.OTHER
    severity: WorkOrderBlockerSeverity = WorkOrderBlockerSeverity.MEDIUM
    title: Optional[str] = Field(None, max_length=255)
    note: Optional[str] = Field(None, max_length=2000)
    # The NCR this blocker was raised with (QUALITY_HOLD one-tap, process sheets PR 4).
    # Tenant-validated in the service.
    ncr_id: Optional[int] = Field(None, gt=0)
    assigned_to: Optional[int] = Field(None, gt=0)
    put_operation_on_hold: bool = True


class WorkOrderBlockerUpdate(BaseModel):
    status: Optional[WorkOrderBlockerStatus] = None
    severity: Optional[WorkOrderBlockerSeverity] = None
    assigned_to: Optional[int] = Field(None, gt=0)
    resolution_note: Optional[str] = Field(None, max_length=2000)


class WorkOrderBlockerResolve(BaseModel):
    resolution_note: Optional[str] = Field(None, max_length=2000)


class WorkOrderBlockerResponse(UTCModel):
    id: int
    company_id: int
    work_order_id: int
    operation_id: Optional[int] = None
    material_part_id: Optional[int] = None
    ncr_id: Optional[int] = None
    category: WorkOrderBlockerCategory
    severity: WorkOrderBlockerSeverity
    status: WorkOrderBlockerStatus
    title: str
    note: Optional[str] = None
    resolution_note: Optional[str] = None
    reported_by: Optional[int] = None
    assigned_to: Optional[int] = None
    resolved_by: Optional[int] = None
    reported_at: datetime
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    work_order_number: Optional[str] = None
    operation_name: Optional[str] = None
    material_part_number: Optional[str] = None

    class Config:
        from_attributes = True


class OperationOpenBlocker(UTCModel):
    """One blocker still in the way of the operation -- the SAME SHAPE the resume returns.

    Deliberately the field set of ``shop_floor._resume_open_blocker_payload``, so
    the client reuses its existing ``ResumeOpenBlocker`` type verbatim rather than
    learning a second vocabulary for the same fact. Two of that shape's fields are
    omitted, and the omission is a decision, not an oversight:

    ``has_note`` / ``free_text_withheld`` exist ONLY to describe the crew-station
    free-text gate, and that gate CANNOT APPLY on this router. Verified rather
    than assumed, three independent ways:

    1. A station token is minted ``type == "kiosk"`` and ``security.verify_token``
       returns ``None`` for anything whose type is not ``"access"``, so a station
       token never reaches ``get_current_user`` on ANY router.
    2. A badge-minted OPERATOR token is ``type == "access"`` with
       ``scope == "kiosk"``, and ``deps._is_kiosk_scope_allowed_path`` honors it
       only under ``KIOSK_TOKEN_PATH_PREFIXES`` (``/api/v1/shop-floor``) plus the
       employee-logout exact path. This router mounts at
       ``/api/v1/work-order-blockers`` (``api/router.py``), so such a token is
       403 here no matter whose badge it is.
    3. Both verbs that return this shape are
       ``require_role([ADMIN, MANAGER, SUPERVISOR])``.

    So ``title`` -- the caller-supplied free text the gate protects -- always
    rides this response, which makes ``free_text_withheld`` a constant ``false``
    and ``has_note`` a fact the reader can already see. Sending either would be
    machinery describing a policy that is not in force. If the reachability of
    this router ever widens, the gate has to be reintroduced here first, and
    ``shop_floor._resume_open_blocker_payload`` is the implementation to reuse.
    """

    id: int = Field(..., description="The blocker still in the way -- the id a caller closes next.")
    title: Optional[str] = Field(
        None,
        description="Caller-supplied free text when a human wrote one, otherwise the title the server "
        "composed (``blocker_default_title``). Render VERBATIM.",
    )
    category: Optional[str] = Field(None, description="A ``WorkOrderBlockerCategory`` value, e.g. ``machine_down``.")
    severity: Optional[str] = Field(None, description="A ``WorkOrderBlockerSeverity`` value, e.g. ``high``.")
    status: Optional[str] = Field(None, description='Only "open" or "acknowledged" ever appear here.')


class BlockerOperationOutcome(UTCModel):
    """What closing a blocker did to its OPERATION -- the fact the 200 could not carry.

    ``POST /work-order-blockers/{id}/resolve`` and ``PUT /work-order-blockers/
    {id}`` both returned a blocker row and nothing about the operation, so a
    caller could not tell a resolve that took a job off hold from one that left
    it exactly where it was. The page fired an unconditional green toast, and a
    shop owner read "Resolved blocker" over a nest that was still ON_HOLD.

    ``operation_still_held`` is THE field a client warns on. It means a resume was
    OWED and WITHHELD -- not merely that one did not happen, which is also true
    whenever there was nothing to resume. Warning on the reason alone would put a
    "still held" notice on a blocker that never held anything, which is a new kind
    of dishonesty rather than a fix.

    The second warnable case carries NO withheld reason at all: ``operation_
    resumed`` is true and ``operation_status`` is ``"pending"``. The hold cleared,
    but PENDING is off the dispatch board and off the kiosk (both surface READY
    only), so "resumed" alone sends the shop looking for a card that will not
    appear. Read the status, not just the boolean.
    """

    operation_id: Optional[int] = Field(
        None,
        description="The operation the blocker names. Read off the BLOCKER, so it is still present when "
        "the operation row itself could not be loaded. ``null`` when the blocker named no operation.",
    )
    operation_status: Optional[str] = Field(
        None,
        description="The operation's status AFTER this call. ``null`` when the row could not be loaded.",
    )
    operation_resumed: bool = Field(
        False,
        description="True only when this call actually moved the operation off hold. True with "
        "``operation_status == \"pending\"`` means the hold cleared but the job did NOT return to the "
        "dispatch board or the kiosk, which both surface READY only.",
    )
    resume_withheld_reason: Optional[BlockerResumeWithheldReason] = Field(
        None,
        description="Why no resume happened; ``null`` exactly when one did. Closed vocabulary. Do NOT warn "
        "off this field -- three of its four values mean there was nothing to resume. Warn off "
        "``operation_still_held``.",
    )
    operation_still_held: bool = Field(
        False,
        description="A resume was OWED and WITHHELD: the operation is still ON_HOLD after this call. "
        "This is the field a client warns on.",
    )
    open_blockers: List[OperationOpenBlocker] = Field(
        default_factory=list,
        description="The OPEN/ACKNOWLEDGED blockers still naming this operation -- what to close next.",
    )


class WorkOrderBlockerWriteResponse(WorkOrderBlockerResponse):
    """The blocker row PLUS what the write did to its operation.

    A SUPERSET of ``WorkOrderBlockerResponse``, so every existing client field is
    unchanged; ``operation_outcome`` is purely additive.

    A SEPARATE MODEL rather than fields on the shared response, because the list
    and create verbs cannot honestly answer it. A row in ``GET /work-order-
    blockers/`` would have to report ``operation_resumed: false``, which reads as
    "a resume was withheld" on a row where nothing was ever attempted -- exactly
    the class of false statement this whole change exists to remove.

    ``operation_outcome`` is ``None`` when NO RESUME WAS ATTEMPTED: the call left
    the blocker OPEN or ACKNOWLEDGED, so the operation was never a candidate.
    Absent means not-applicable; present means a full account. A client must not
    warn on absence.
    """

    operation_outcome: Optional[BlockerOperationOutcome] = Field(
        None,
        description="What this write did to the blocker's OPERATION. ``null`` when NO resume was attempted "
        "(the call left the blocker open or acknowledged) -- absence means not-applicable, so a client must "
        "not warn on it.",
    )
