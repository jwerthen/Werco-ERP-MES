"""Read-only purchase-order suggestions, retaining the uploaded source evidence."""

from datetime import date

from pydantic import Field

from app.schemas.hank_intake import IntakeLine, StrictModel


class IntakePurchaseOrderVendor(StrictModel):
    id: int
    code: str
    name: str
    reason: str


class IntakePurchaseOrderPart(StrictModel):
    id: int
    part_number: str
    name: str
    unit_of_measure: str


class IntakePurchaseOrderLine(IntakeLine):
    source_line_index: int
    part_id: int | None = None
    candidates: list[IntakePurchaseOrderPart] = Field(default_factory=list)
    quantity_ordered: float | None = None
    unit_price_amount: float | None = None
    warnings: list[str] = Field(default_factory=list)


class IntakeExistingPurchaseOrder(StrictModel):
    id: int
    po_number: str
    href: str


class IntakePurchaseOrderDraft(StrictModel):
    file_id: int
    file_version: int
    company_id: int
    filename: str
    po_number: str | None = None
    order_date: date | None = None
    required_date: date | None = None
    vendor_id: int | None = None
    vendors: list[IntakePurchaseOrderVendor] = Field(default_factory=list)
    lines: list[IntakePurchaseOrderLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    has_duplicates: bool = False
    blocked_reason: str | None = None
    existing_purchase_orders: list[IntakeExistingPurchaseOrder] = Field(default_factory=list)
    can_ready_for_receiving: bool = False
