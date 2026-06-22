"""Request/response contracts for the routing CSV/XLSX import wizard (A0.2).

Shapes mirror the established import-response conventions (``total_rows`` /
created counts / ``created_ids`` / row-level ``errors`` with a row number, an
identifier, and a reason) so the frontend Import Center can render routings the
same way it renders the open-WO / open-PO loaders. ``dry_run`` distinguishes a
preview (everything rolled back) from a commit. One result entry is produced per
part/routing — not per operation row — because rows are grouped by part number
into a single draft routing plus its operations.
"""

from typing import List, Optional

from pydantic import BaseModel


class RoutingImportError(BaseModel):
    row: int
    part_number: Optional[str] = None
    reason: str


class RoutingImportRowResult(BaseModel):
    """One accepted routing (preview or commit) — one entry per part/routing, not per operation."""

    rows: List[int]  # the file row numbers that became this routing's operations
    part_number: str
    routing_revision: str
    routing_id: Optional[int] = None  # None in dry-run (rolled back)
    operation_count: int
    total_setup_hours: float
    total_run_hours_per_unit: float
    status: str  # always "draft"


class RoutingImportResponse(BaseModel):
    dry_run: bool
    total_rows: int  # data rows read from the file
    parts_detected: int  # distinct part_numbers grouped
    routings_created: int  # routings created (== len(results))
    total_operations: int  # operations across all created routings
    skipped_count: int  # input rows that did not become an operation
    created_ids: List[int]
    results: List[RoutingImportRowResult]
    errors: List[RoutingImportError]
