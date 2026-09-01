"""Request/response contracts for ``/api/v1/work-order-templates``.

A template is a NAME plus a POINTER at the work order whose plan it stands for. That
shape is what these schemas encode, and the two things they deliberately do NOT carry
are the interesting part:

* **No plan fields.** There is no ``operations``, no ``nests``, no ``material_ties``
  on any request here. The plan lives on the source work order and is copied at USE
  time by ``work_order_duplicate_service``; a request that could describe a plan would
  be the beginning of a second copy engine.
* **No status.** A template has none. What it produces always lands ``DRAFT``.

``POST /work-order-templates/{id}/use`` returns
:class:`WorkOrderTemplateUseResponse`, which SUBCLASSES the existing
``WorkOrderDuplicateResponse`` rather than replacing it, so the skip lists a planner
must see reach the same result view the Duplicate dialog already renders. See that
schema's docstring before treating the envelope as a formality, and this one's before
"simplifying" the batch envelope down to its list.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from app.core.time_utils import to_utc_iso
from app.core.validation import UnitNumber
from app.schemas.base import UTCModel
from app.schemas.work_order import WorkOrderDuplicateResponse, WorkOrderResponse

# Matches ``WorkOrderTemplate.name``'s ``String(120)``. Stated here as well so an
# over-long name is a 422 naming the field rather than a database error naming a
# column.
TEMPLATE_NAME_MAX_LENGTH = 120

# How many DRAFT work orders one ``POST /{id}/use`` may create. A cap rather than an
# open count, and 20 rather than a round 100, because the batch is ONE transaction and
# each copy is not cheap: it writes a header, its operations and their re-snapshotted
# process-sheet steps, any nests, any material ties, plus 2 + n audit rows -- and every
# audit row is serialised behind the audit chain's GLOBAL advisory lock, which is held
# across tenants for the life of the transaction. A batch of hundreds would stall every
# other company's writes while it ran.
#
# It is also the shape of the work: a weld assembly is one unit per work order and the
# planner types one unit number per row. Twenty rows is a long form; two hundred is a
# spreadsheet import, which is a different feature with a different failure mode.
MAX_TEMPLATE_USE_COUNT = 20


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
    """Body for ``POST /work-order-templates/{id}/use`` — create DRAFT work orders.

    Every field is optional, which is what makes the click-once case click-once: an
    empty body still means "one draft, quantity from the template, no due date".

    ``quantity_ordered`` resolves to the first POSITIVE value of: this field, the
    template's ``default_quantity``, the source work order's ``quantity_ordered``. All
    three non-positive is a 422 rather than a fabricated 1 — a quantity of one on a job
    that should have run fifty is a plan nobody approved. It is the quantity of EACH
    work order, never a total to divide: a weld assembly is built one unit per work
    order, so "five of these" means five work orders of the same quantity, not one
    quantity split five ways.

    ``due_date`` is never inherited from the source. Null means unscheduled, which
    reads as "not promised yet" everywhere; carrying the source's date forward would
    make the new job overdue the moment it exists, on the dispatch board and in OTD,
    for a promise nobody made. Like ``WorkOrderDuplicateRequest`` and unlike
    ``WorkOrderCreate``, it carries NO "not in the past" validator: a template is most
    often used to re-run something already late. **One date for the whole batch** —
    per-unit dates were considered and deliberately not built for v1; every unit in a
    batch is promised together, and a batch that needed staggered dates would be
    several batches.

    ``unit_numbers`` is a LIST THE PLANNER SUPPLIES, one entry per work order, in
    creation order. There is deliberately **no generator, no auto-increment and no
    fill-down**: the owner was asked directly, and real unit numbers are not a trailing
    digit that walks. Inventing the second one from the first would put a fabricated
    build identity on the kiosk, the dispatch board and the TV wall, where it is
    indistinguishable from one somebody typed.
    """

    model_config = ConfigDict(extra="forbid")

    quantity_ordered: Optional[Decimal] = Field(
        None,
        gt=Decimal("0"),
        description="Quantity for EACH new work order — not a total to split across the batch. Omit to use "
        "the template's default, or the source work order's quantity. For a nest-bearing template this does "
        "NOT rescale the copied nests: each keeps its own planned_runs and the stored quantity is DERIVED "
        "from their sum, so read it back from the response rather than assuming this value was stored.",
    )
    due_date: Optional[date] = Field(
        None,
        description="Due date for every work order in the batch, or null to leave them unscheduled. The "
        "source's due date and its must_ship_by promise are never carried.",
    )
    count: int = Field(
        1,
        ge=1,
        le=MAX_TEMPLATE_USE_COUNT,
        description=(
            "How many separate DRAFT work orders to create, each with its own work order number. "
            f"1-{MAX_TEMPLATE_USE_COUNT}; above that is a 422. A count above 1 is refused 409 on a "
            "nest-bearing template, whose quantity is the sum of its nests' planned runs."
        ),
    )
    unit_numbers: Optional[List[Optional[UnitNumber]]] = Field(
        None,
        description=(
            "One unit number per created work order, in creation order. Omit to leave every draft "
            "without one. Values are trimmed, and a blank or whitespace-only entry stores as null so a "
            "gap in the list is expressible — the third of five units may not be known yet. Must hold "
            "exactly `count` entries when present."
        ),
    )

    @model_validator(mode="after")
    def _unit_numbers_line_up_with_count(self) -> "WorkOrderTemplateUseRequest":
        """One unit number per work order, and no two work orders claiming the same unit.

        The length check is what stops a silent misalignment. The list is positional —
        entry 3 becomes the third work order's unit — so a list one entry short would
        otherwise either drop a unit or, worse, shift every unit after the gap onto the
        wrong job. Both are invisible afterwards: the drafts look correct, and the
        wrong build identity travels to the kiosk and the TV wall.

        The duplicate check is scoped to the BATCH and compares trimmed values
        case-insensitively (the entries are already trimmed, and blanks are already
        ``None``, by ``UnitNumber``). Two work orders in one batch claiming unit
        ``2410048`` is a typo in a form the planner just filled in, and it is
        correctable before anything is written.

        **Duplicates against EXISTING work orders are deliberately NOT checked**, here
        or in the service. ``work_orders.unit_number`` carries a plain index and no
        unique constraint precisely because a rework or replacement work order
        legitimately re-uses the unit it is rebuilding, so a uniqueness gate here would
        refuse the case the column was designed for.
        """
        if self.unit_numbers is None:
            return self

        if len(self.unit_numbers) != self.count:
            raise ValueError(
                f"unit_numbers has {len(self.unit_numbers)} entries but count is {self.count}: send exactly "
                "one entry per work order (null for a work order whose unit is not known yet), or omit "
                "unit_numbers entirely"
            )

        first_seen: dict[str, int] = {}
        for position, value in enumerate(self.unit_numbers, start=1):
            if value is None:
                continue
            key = value.lower()
            if key in first_seen:
                raise ValueError(
                    f"unit_numbers repeats '{value}' at entries {first_seen[key]} and {position}: each work "
                    "order in a batch builds a different unit. Leave an entry blank if its unit number is "
                    "not known yet"
                )
            first_seen[key] = position
        return self


class WorkOrderTemplateUseResponse(WorkOrderDuplicateResponse):
    """``POST /work-order-templates/{id}/use``: every draft the batch created.

    **A strict SUPERSET of the duplicate envelope, by subclassing it**, and that is a
    compatibility decision rather than a stylistic one. The result view this endpoint
    shares with the Duplicate dialog dereferences the SINGULAR ``work_order`` — the
    stored-quantity note and the skip report both read it directly — so an envelope
    that carried only ``work_orders`` would not merely change a shape, it would fail to
    type-check against the client that renders it. Subclassing means the existing
    fields keep their existing meaning and the batch fields are additive, so a client
    that has not been updated still shows the first draft and its skips correctly.

    ``work_orders[0]`` IS ``work_order``. The singular field is the batch's first
    element, not a summary of it and not a separate copy.

    ``skipped_operations`` / ``skipped_material_allocations`` are the UNION across every
    copy in the batch, deduplicated by ``source_operation_id`` /
    ``source_allocation_id``. Every copy reads the SAME source work order, so an
    omission the source causes is reported identically by all of them; listing it once
    per copy would read as five separate missing material ties rather than one, which is
    the opposite of what a skip list is for. Deduplicating by the SOURCE row's id is
    what makes "reported once" mean "omitted once" — the thing that was left behind is
    a row on the source, and it was left behind once no matter how many drafts were cut
    from it.
    """

    created_count: int = Field(
        ...,
        description="How many DRAFT work orders this call created. Always equals len(work_orders), and "
        "equals the request's count — the batch is all-or-nothing, so a partial batch is never returned.",
    )
    work_orders: List[WorkOrderResponse] = Field(
        # REQUIRED, not ``default_factory=list``: the docstring above makes
        # "work_orders[0] IS work_order" load-bearing, and a defaulted empty list is a
        # constructible envelope that states a singular work order and no batch
        # containing it — the one shape the promise forbids. Required means the schema
        # enforces what it documents instead of trusting every call site to pass it.
        ...,
        description="Every created work order, in creation order (so their work order numbers ascend). "
        "work_orders[0] is the same object as work_order.",
    )


class WorkOrderTemplatePlan(UTCModel):
    """What using this template would produce, read LIVE off the source work order.

    Nothing in here is stored. A template is a pointer, so the only honest summary is
    the one computed at read time: a stored ``nest_count`` goes stale the first time
    somebody soft-deletes a nest on the source, and the planner picks a template
    believing it carries 21 nests and gets 20.

    **A soft-deleted source does NOT make a template unavailable.** It is summarised
    in full, ``available`` stays true, ``POST .../use`` produces the same DRAFT, and
    the deletion is disclosed through ``source_work_order_deleted``. That is an owner
    decision — a catalog entry must not stop working because somebody deleted a job —
    and the service module docstring carries the invariant-3 argument behind it.

    ``available`` is false only when the source ROW could not be resolved at all, which
    the NOT NULL foreign key makes near-unreachable; every other field is then
    null/zero. Such a template is still LISTED — hiding it is the mask trap invariant 3
    documents — and ``POST .../use`` refuses it 409.
    """

    available: bool = Field(
        ...,
        description="False only when the source work order row could not be resolved at all — NOT when "
        "it has been deleted (see source_work_order_deleted). The template is still listed; using it is "
        "refused 409.",
    )
    unavailable_reason: Optional[str] = Field(
        None,
        description="Machine-readable cause when available is false. Currently only "
        "'source_work_order_missing'. Treat the set as open and tolerate an unknown value. Note that "
        "'source_work_order_deleted' was retired as a value here: deletion is now a flag, not a refusal.",
    )
    source_work_order_deleted: bool = Field(
        False,
        description="True when the work order this template was saved from has been soft-deleted. The "
        "template still WORKS — this is disclosure, not a gate — but the exemplar is in the archive, so "
        "surface it rather than dropping it silently.",
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
