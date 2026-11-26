"""Which OPEN material ties are live DEMAND right now (owner decision 2026-11-25).

A step taken off a released job is SKIPPED, never deleted, and a skipped step consumes
nothing. Its operation-scoped OPEN ties are therefore not demand while it stays skipped: the
consumption engine can never draw against them (their target is ``qty_per_run x (good +
scrap)`` = 0), so a reader that counted ``qty_planned - qty_consumed`` for them would report a
phantom shortage and hold stock away from other jobs.

The ties themselves are deliberately left alone -- no status change, no ``qty_planned``
edit. ``AllocationStatus`` has no reversible "suspended" member (``CANCELLED`` is the
tombstone, ``CLOSED`` is reserved), and a restore (``POST /work-orders/operations/{id}/unskip``)
must bring the tie back as demand exactly as it was. So the rule is applied where demand is
READ, through this one predicate, never by rewriting the tie:

* ``tie_is_live_demand_clause()`` -- the SQL half, for reservation reads that select ties
  with a query (``owned_property_service._open_ties``, the combine's reservation read);
* ``live_demand_ties()`` -- the Python half, for a reader that already holds the ties and
  the operations (``material_readiness_service``).

A work-order-scoped tie (``work_order_operation_id IS NULL``) is never affected.
Pure reads; nothing here writes.
"""

from typing import Iterable, Sequence

from sqlalchemy import and_, exists, or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.work_order import OperationStatus, WorkOrderOperation
from app.models.work_order_material import WorkOrderMaterialAllocation


def tie_is_live_demand_clause() -> ColumnElement[bool]:
    """SQL: the tie is work-order-scoped, or its operation is not SKIPPED (same company)."""
    return or_(
        WorkOrderMaterialAllocation.work_order_operation_id.is_(None),
        ~exists().where(
            and_(
                WorkOrderOperation.id == WorkOrderMaterialAllocation.work_order_operation_id,
                WorkOrderOperation.company_id == WorkOrderMaterialAllocation.company_id,
                WorkOrderOperation.status == OperationStatus.SKIPPED,
            )
        ),
    )


def live_demand_ties(
    ties: Iterable[WorkOrderMaterialAllocation], operations: Sequence[WorkOrderOperation]
) -> list[WorkOrderMaterialAllocation]:
    """``ties`` minus the operation-scoped ones whose operation (in ``operations``) is SKIPPED."""
    skipped_ids = {op.id for op in operations if op.status == OperationStatus.SKIPPED}
    return [
        tie for tie in ties if tie.work_order_operation_id is None or tie.work_order_operation_id not in skipped_ids
    ]
