"""Request/response contracts for the A0.2 Excel-migration import kit.

Shapes follow the established import-response conventions
(``total_rows`` / ``created`` counts / ``created_ids`` / row-level ``errors``
with a row number, an identifier, and a reason) so the frontend Import Center
can render every entity the same way. ``dry_run`` + ``results`` are additive:
commit responses stay backward compatible with the existing CSV imports.
"""

from datetime import date
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.base import UTCModel


class WorkOrderImportError(BaseModel):
    row: int
    wo_number: Optional[str] = None
    part_number: Optional[str] = None
    reason: str


class WorkOrderImportRowResult(UTCModel):
    """Would-be/created work order for one accepted row (preview and commit)."""

    row: int
    wo_number: Optional[str] = None  # None in dry-run when the number would be generated at commit
    part_number: str
    quantity: float
    due_date: Optional[date] = None
    customer_name: Optional[str] = None
    status: str
    operation_count: int
    completed_operation_count: int
    next_operation_sequence: Optional[int] = None


class WorkOrderImportResponse(BaseModel):
    dry_run: bool
    total_rows: int
    created_count: int
    skipped_count: int
    created_ids: List[int]
    results: List[WorkOrderImportRowResult]
    errors: List[WorkOrderImportError]


class PurchaseOrderImportError(BaseModel):
    row: int
    po_number: Optional[str] = None
    part_number: Optional[str] = None
    reason: str


class PurchaseOrderImportRowResult(BaseModel):
    """Would-be/created purchase order (one entry per PO, not per line)."""

    rows: List[int]
    po_number: Optional[str] = None  # None in dry-run when the number would be generated at commit
    vendor_code: str
    line_count: int
    total: float
    status: str


class PurchaseOrderImportResponse(BaseModel):
    dry_run: bool
    total_rows: int
    created_count: int  # purchase orders created
    created_line_count: int
    skipped_count: int  # input rows that did not result in a PO line
    created_ids: List[int]
    results: List[PurchaseOrderImportRowResult]
    errors: List[PurchaseOrderImportError]
