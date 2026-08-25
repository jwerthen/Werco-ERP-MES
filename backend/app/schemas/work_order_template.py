"""Request/response contracts for ``/api/v1/work-order-templates``.

A template is a NAME plus a POINTER at the work order whose plan it stands for. That
shape is what these schemas encode, and the two things they deliberately do NOT carry
are the interesting part:

* **No plan fields.** There is no ``operations``, no ``nests``, no ``material_ties``
  on any request here. The plan lives on the source work order and is copied at USE
  time by ``work_order_duplicate_service``; a request that could describe a plan would
  be the beginning of a second copy engine.
* **No status.** A template has none. What it produces always lands ``DRAFT``.

``POST /work-order-templates/{id}/use`` deliberately returns the EXISTING
``WorkOrderDuplicateResponse`` envelope rather than a new one, so the skip lists a
planner must see reach the same result view the Duplicate dialog already renders. See
that schema's docstring before treating the envelope as a formality.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.time_utils import to_utc_iso
from app.schemas.base import UTCModel

# Matches ``WorkOrderTemplate.name``'s ``String(120)``. Stated here as well so an
# over-long name is a 422 naming the field rather than a database error naming a
# column.
TEMPLATE_NAME_MAX_LENGTH = 120


class WorkOrderTemplateCreate(BaseModel):
    """Body for ``POST /work-order-templates`` — save a work order's plan under a name.

    The source work order is NOT modified by this call. It is read to confirm it
    exists and is live in the active company, and then pointed at.
    """

    model_config = ConfigDict(extra="forbid")

    source_work_order_id: int = Field(
        ...,
        description="The work order whose plan this template stands for. Must be live in the active "
        "company; a soft-deleted or cross-tenant id answers 404.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=TEMPLATE_NAME_MAX_LENGTH,
        description="What the planner calls this job — 'Miratech nest group', 'Bracket brake set'. "
        "Unique among this company's LIVE templates, compared case-insensitively; deleting a "
        "template frees its name immediately.",
    )
    notes: Optional[str] = Field(
        None,
        description="Free text for the planner. Stored verbatim.",
    )
    default_quantity: Optional[Decimal] = Field(
        None,
        gt=Decimal("0"),
        description="Prefilled quantity for a run from this template. Optional — the use endpoint "
        "falls back to the source work order's own quantity. IGNORED for a nest-bearing template, "
        "whose quantity is derived from the sum of its nests' planned runs.",
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        # ``min_length=1`` alone admits "   ", which stores as an empty name after the
        # service's whitespace collapse and leaves an unnameable row in the picker.
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class WorkOrderTemplateUpdate(BaseModel):
    """Body for ``PUT /work-order-templates/{id}`` — rename, re-note, re-default.

    Every field is optional, and ``None`` is MEANINGFUL for the two nullable ones:
    sending ``"notes": null`` clears the note, while omitting the key leaves it alone.
    The endpoint distinguishes the two through ``model_fields_set``, so a planner can
    undo a typo rather than being stuck with it.

    ``source_work_order_id`` is deliberately absent. Re-pointing a template at a
    different work order under the same name silently changes what every future click
    produces, with the only thing anyone reads unchanged. Save a new template and
    delete the old one — both halves are then on the audit chain.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=TEMPLATE_NAME_MAX_LENGTH,
        description="New name. Must stay unique among this company's live templates.",
    )
    notes: Optional[str] = Field(None, description="New note. Send null to clear it.")
    default_quantity: Optional[Decimal] = Field(
        None,
        gt=Decimal("0"),
        description="New prefill quantity. Send null to clear it.",
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value


class WorkOrderTemplateUseRequest(BaseModel):
    """Body for ``POST /work-order-templates/{id}/use`` — create a new DRAFT work order.

    Both fields are optional, which is what makes the click-once case click-once.

    ``quantity_ordered`` resolves to the first POSITIVE value of: this field, the
    template's ``default_quantity``, the source work order's ``quantity_ordered``. All
    three non-positive is a 422 rather than a fabricated 1 — a quantity of one on a job
    that should have run fifty is a plan nobody approved.

    ``due_date`` is never inherited from the source. Null means unscheduled, which
    reads as "not promised yet" everywhere; carrying the source's date forward would
    make the new job overdue the moment it exists, on the dispatch board and in OTD,
    for a promise nobody made. Like ``WorkOrderDuplicateRequest`` and unlike
    ``WorkOrderCreate``, it carries NO "not in the past" validator: a template is most
    often used to re-run something already late.
    """

    model_config = ConfigDict(extra="forbid")

    quantity_ordered: Optional[Decimal] = Field(
        None,
        gt=Decimal("0"),
        description="Quantity for the NEW work order. Omit to use the template's default, or the "
        "source work order's quantity. For a nest-bearing template this does NOT rescale the copied "
        "nests: each keeps its own planned_runs and the stored quantity is DERIVED from their sum, so "
        "read it back from the response rather than assuming this value was stored.",
    )
    due_date: Optional[date] = Field(
        None,
        description="Due date for the new work order, or null to leave it unscheduled. The source's due "
        "date and its must_ship_by promise are never carried.",
    )


class WorkOrderTemplatePlan(UTCModel):
    """What using this template would produce, read LIVE off the source work order.

    Nothing in here is stored. A template is a pointer, so the only honest summary is
    the one computed at read time: a stored ``nest_count`` goes stale the first time
    somebody soft-deletes a nest on the source, and the planner picks a template
    believing it carries 21 nests and gets 20.

    When ``available`` is false the source work order has been soft-deleted and every
    other field is null/zero. The template is still LISTED — hiding it is the mask trap
    invariant 3 documents, and the planner needs to see the cause to act on it — but
    ``POST .../use`` refuses it 409.
    """

    available: bool = Field(
        ...,
        description="False when the source work order has been deleted. The template is still listed; "
        "using it is refused 409.",
    )
    unavailable_reason: Optional[str] = Field(
        None,
        description="Machine-readable cause when available is false. Currently only "
        "'source_work_order_deleted'. Treat the set as open and tolerate an unknown value.",
    )

    source_work_order_number: Optional[str] = None
    source_status: Optional[str] = Field(
        None,
        description="The source work order's CURRENT status. Informational only — a template may "
        "legitimately point at a complete or closed job; that is the headline case.",
    )
    work_order_type: Optional[str] = Field(None, description="'production' or 'laser_cutting'.")
    sequential_operations: Optional[bool] = Field(
        None,
        description="True = a sequenced routing; false = a same-work-center dispatch pool (the "
        "press-brake / weld-sub batch shape). Carried by the copy. Inert on a laser work order, whose "
        "pooling comes from its type instead.",
    )
    priority: Optional[int] = None

    operation_count: int = 0
    nest_count: int = Field(0, description="LIVE laser nests. Non-zero means the quantity is derived, not typed.")
    planned_runs_total: int = Field(
        0,
        description="Sum of those nests' planned runs — the quantity a nest-bearing copy will be given.",
    )
    open_material_tie_count: int = Field(
        0,
        description="OPEN ties only, the exact set the copy carries. Cancelled/closed ties are "
        "tombstones and are not copied, so they are not counted.",
    )
    work_centers: List[str] = Field(
        default_factory=list,
        description="Distinct work centers the operations sit on, in sequence order.",
    )
    source_quantity_ordered: Optional[float] = None


class WorkOrderTemplateResponse(UTCModel):
    """One template: what it is called, what it points at, and what it would produce."""

    id: int
    name: str
    notes: Optional[str] = None
    source_work_order_id: int
    default_quantity: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[int] = None
    plan: WorkOrderTemplatePlan

    @field_serializer("created_at", "updated_at")
    def _serialize_datetimes(self, value: Optional[datetime], _info):
        # Store UTC, serve UTC ('Z'), display Central. The columns are naive UTC
        # (matching WorkOrder's own hand-rolled timestamps), so they go through the
        # same helper every other response schema uses.
        return to_utc_iso(value)

    class Config:
        from_attributes = True


class WorkOrderTemplateListResponse(UTCModel):
    """``GET /work-order-templates`` — the catalog.

    A flat list rather than a paged envelope: this is a curated set a planner maintains
    by hand, in the tens, not a feed. If it ever grows past that, paginate it then
    rather than shipping an envelope nothing needs now.
    """

    templates: List[WorkOrderTemplateResponse] = Field(default_factory=list)
    total: int = 0
