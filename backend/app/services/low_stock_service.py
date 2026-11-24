"""The one low-stock rule (plan alerts_health/2 step 2).

A part is LOW when it is a live part (active, not deleted) with a reorder point above zero and
the company's OWN on-hand -- summed per part over every active stock row, in any status -- is at
or below that reorder point. A part with no stock row at all reads 0 on hand, so a part that is
completely out of stock is low.

Four readers used to spell this four ways (per stock row against a reorder point that defaults
to zero; strictly below; reorder point OR safety stock), so the alert, the Inventory filter, the
inbox and the AI sensor disagreed on the count. They all read it from here now:

* ``GET /inventory/low-stock`` -- the Inventory page's low-stock view and chip;
* the ``stock.low`` cron (``check_low_stock_task``);
* the operations inbox's ``low_stock`` source;
* the AI inventory-risk sensor.

Company-owned stock only (AS9100D 8.5.3, PR #374): a customer's or supplier's material on the
rack is not the shop's supply, so it never keeps a part off the list
(``owned_property_service.COMPANY_OWNED_ITEM_CLAUSES``). On-order supply is NOT netted here --
that is MRP's question.

Pure reads: nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from app.models.inventory import InventoryItem
from app.models.part import Part
from app.services.owned_property_service import COMPANY_OWNED_ITEM_CLAUSES


def company_owned_stock_subquery(db: Session, company_id: int):
    """Per part: the company's own on-hand over its active rows, and when any of them last changed."""
    return (
        db.query(
            InventoryItem.part_id.label("part_id"),
            func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0).label("quantity"),
            func.max(InventoryItem.updated_at).label("updated"),
        )
        .filter(InventoryItem.company_id == company_id, InventoryItem.is_active.is_(True), *COMPANY_OWNED_ITEM_CLAUSES)
        .group_by(InventoryItem.part_id)
        .subquery()
    )


def low_stock_query(db: Session, company_id: int) -> Query:
    """Rows of ``(Part, on_hand, last_stock_change)`` for every low part, unordered and unbounded.

    A query, not a list, for the readers that page or lock it themselves (the inbox)."""
    stock = company_owned_stock_subquery(db, company_id)
    on_hand = func.coalesce(stock.c.quantity, 0)
    return (
        db.query(Part, on_hand.label("on_hand"), stock.c.updated.label("last_stock_change"))
        .outerjoin(stock, Part.id == stock.c.part_id)
        .filter(
            Part.company_id == company_id,
            Part.is_active.is_(True),
            # The deletion record, not just the mask a delete happens to clear (invariant 3).
            Part.is_deleted.is_(False),
            Part.reorder_point > 0,
            on_hand <= Part.reorder_point,
        )
    )


@dataclass(frozen=True)
class LowStockPart:
    part: Part
    quantity_on_hand: float
    last_stock_change: Optional[datetime]

    @property
    def reorder_point(self) -> float:
        return float(self.part.reorder_point or 0)

    @property
    def shortage(self) -> float:
        return self.reorder_point - self.quantity_on_hand

    @property
    def is_critical(self) -> bool:
        """At or below safety stock (0 when none is set, so an empty bin is critical)."""
        return self.quantity_on_hand <= float(self.part.safety_stock or 0)

    @property
    def out_of_stock(self) -> bool:
        return self.quantity_on_hand <= 0


def low_stock_parts(db: Session, company_id: int, *, limit: Optional[int] = None) -> List[LowStockPart]:
    """Every low part of the company, critical first, then by largest shortage, then part number."""
    query = low_stock_query(db, company_id).order_by(Part.part_number, Part.id)
    if limit is not None:
        query = query.limit(limit)
    parts = [
        LowStockPart(part=part, quantity_on_hand=float(on_hand or 0), last_stock_change=changed)
        for part, on_hand, changed in query.all()
    ]
    parts.sort(key=lambda row: (not row.is_critical, -row.shortage))
    return parts


def low_stock_count(db: Session, company_id: int) -> int:
    return low_stock_query(db, company_id).order_by(None).count()
