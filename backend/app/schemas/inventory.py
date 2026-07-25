"""Pydantic v2 response contracts for the inventory domain.

Only the ledger read is formalized here so far. ``GET /inventory/transactions``
previously returned raw ORM rows with no ``response_model``, which meant FastAPI's
``jsonable_encoder`` dumped every loaded column — including the *entire* joined
``Part`` row (costs and all) — and serialized ``created_at`` **without** the trailing
``Z``, against the "store UTC, serve UTC (``Z``), display Central" invariant.

``InventoryTransactionResponse`` inherits ``UTCModel`` so datetimes serialize as UTC
ISO-8601 with a ``Z``. The nested ``part`` object is preserved (that shape is what
callers already receive) but narrowed to the identifying fields — a ledger read has no
business publishing a part's standard/material/labor/overhead cost.
"""

from datetime import datetime
from typing import Optional

from app.models.inventory import TransactionType
from app.schemas.base import UTCModel


class InventoryTransactionPart(UTCModel):
    """The identifying slice of the joined ``Part`` row on a ledger transaction."""

    id: int
    part_number: str
    name: Optional[str] = None
    description: Optional[str] = None
    revision: Optional[str] = None
    unit_of_measure: Optional[str] = None


class InventoryTransactionResponse(UTCModel):
    """One row of the inventory ledger (``inventory_transactions``).

    Sign convention (see ``GET /inventory/transactions`` for the full note):
    ``receive`` is positive, ``issue`` is negative, ``adjust`` / ``count`` carry the
    signed delta, and ``transfer`` carries a POSITIVE quantity representing a zero net
    change in on-hand — so a naive ``SUM(quantity)`` over a mixed set over-counts.
    """

    id: int
    company_id: int
    inventory_item_id: Optional[int] = None
    part_id: int
    transaction_type: TransactionType
    quantity: float

    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    reference_number: Optional[str] = None

    from_location: Optional[str] = None
    to_location: Optional[str] = None

    lot_number: Optional[str] = None
    serial_number: Optional[str] = None

    unit_cost: Optional[float] = None
    total_cost: Optional[float] = None

    notes: Optional[str] = None
    reason_code: Optional[str] = None

    created_at: Optional[datetime] = None
    created_by: Optional[int] = None

    part: Optional[InventoryTransactionPart] = None
