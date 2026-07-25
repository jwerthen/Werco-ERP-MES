"""Request/response contracts for work-order material allocations (the material tie).

Response schemas inherit ``UTCModel`` so ``datetime`` fields serialize as UTC ISO-8601
with a trailing ``Z`` (invariant: store UTC, serve UTC, display Central).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.work_order_material import AllocationSource, AllocationStatus
from app.schemas.base import UTCModel


class MaterialAllocationCreate(BaseModel):
    """Tie a material part to a work order (or to one of its operations)."""

    part_id: int = Field(..., description="The MATERIAL part consumed — never the part being produced")
    work_order_operation_id: Optional[int] = Field(
        None,
        description=(
            "Set => the tie is OPERATION-scoped and depletes per completed run "
            "(the laser-nest case). Omit for a work-order-scoped, one-shot tie."
        ),
    )
    source: AllocationSource = Field(AllocationSource.MANUAL, description="How the tie was created")
    qty_per_run: Optional[float] = Field(
        None,
        gt=0,
        description=(
            "Material consumed per completed run. Applies to OPERATION-scoped ties only, where it "
            "defaults to 1.0 when omitted. Supplying it on a work-order-scoped tie (no "
            "``work_order_operation_id``) is a 422 — there are no runs to scale by."
        ),
    )
    qty_planned: float = Field(..., gt=0, description="Total material planned for this tie")
    pinned_inventory_item_id: Optional[int] = Field(
        None, description="Consume from THIS lot. Omit to pick FIFO at consume time."
    )
    notes: Optional[str] = None


class MaterialAllocationUpdate(BaseModel):
    """Edit an OPEN tie's quantities, lot pin, or notes.

    Every field is optional; omitted fields are left untouched. ``part_id``,
    ``work_order_operation_id`` and ``source`` are deliberately NOT editable — changing
    what a tie points at after consumption has posted would rewrite genealogy. Untie and
    re-tie instead.
    """

    qty_per_run: Optional[float] = Field(None, gt=0)
    qty_planned: Optional[float] = Field(None, gt=0)
    pinned_inventory_item_id: Optional[int] = None
    clear_pinned_inventory_item: bool = Field(False, description="Set true to drop the lot pin and fall back to FIFO")
    notes: Optional[str] = None


class MaterialAllocationResponse(UTCModel):
    """One material tie, with the display fields the tie table needs."""

    id: int
    work_order_id: int
    work_order_operation_id: Optional[int] = None
    operation_number: Optional[str] = None
    part_id: int
    part_number: Optional[str] = None
    part_name: Optional[str] = None

    source: AllocationSource
    status: AllocationStatus

    qty_per_run: Optional[float] = None
    qty_planned: float
    unit_of_measure: str
    # CACHE. The ledger (inventory_transactions.allocation_id) is authoritative.
    qty_consumed: float

    pinned_inventory_item_id: Optional[int] = None
    pinned_lot_number: Optional[str] = None

    notes: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
