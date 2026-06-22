"""Request/response contracts for the routing CSV/XLSX import wizard (A0.2).

Shapes mirror the established import-response conventions (``total_rows`` /
created counts / ``created_ids`` / row-level ``errors`` with a row number, an
identifier, and a reason) so the frontend Import Center can render routings the
same way it renders the open-WO / open-PO loaders. ``dry_run`` distinguishes a
preview (everything rolled back) from a commit. One result entry is produced per
part/routing — not per operation row — because rows are grouped by part number
into a single draft routing plus its operations.

``work_center_code`` is OPTIONAL in the upload: a blank/missing code means
"assign the work center in the UI after upload". The preview therefore returns
per-OPERATION detail (``RoutingImportRowResult.operations``) so the frontend can
render one row per operation with a work-center dropdown, flagging the ones that
still ``needs_work_center``. On commit the UI sends back the chosen assignments
(see the ``assignments`` form field on the commit endpoint).
"""

from typing import List, Optional

from pydantic import BaseModel


class RoutingImportError(BaseModel):
    row: int
    part_number: Optional[str] = None
    reason: str


class RoutingImportOperation(BaseModel):
    """One operation within a previewed routing — drives the per-op WC-assignment UI."""

    row: int  # the file row number this operation came from
    sequence: int
    operation_name: str
    work_center_code: Optional[str] = None  # the raw code from the file, if any
    work_center_id: Optional[int] = None  # resolved id (from a valid code or an assignment)
    work_center_name: Optional[str] = None  # resolved work center name, if any
    needs_work_center: bool  # True when no valid work center is resolved yet
    setup_hours: float
    run_hours_per_unit: float
    is_inspection_point: bool
    is_outside_operation: bool


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
    operations: List[RoutingImportOperation] = []  # per-operation detail for the WC-assignment UI


class RoutingImportResponse(BaseModel):
    dry_run: bool
    total_rows: int  # data rows read from the file
    parts_detected: int  # distinct part_numbers grouped
    routings_created: int  # routings created (== len(results))
    total_operations: int  # operations across all created routings
    operations_needing_work_center: int  # operations with no work center resolved yet
    skipped_count: int  # input rows that did not become an operation
    created_ids: List[int]
    results: List[RoutingImportRowResult]
    errors: List[RoutingImportError]
