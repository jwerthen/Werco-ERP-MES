"""Request/response contracts for the scanner resolve-action endpoint (A0.4).

``POST /api/v1/scanner/resolve-action`` is the keystone of the QR traveler /
badge plumbing: every scan surface (kiosk phase 1, wedge scanners, phones)
posts the raw scanned text here and gets back a *parseable* discriminated
result -- including a structured miss (``kind="unknown"`` with HTTP 200),
because scanners hit unknown codes constantly and the client needs data, not
an exception path.

Code formats (prefix-tagged plain text, wedge-scanner friendly):
- ``OP:{operation_id}``      -- a routing-step QR on the printed traveler
- ``WO:{work_order_number}`` -- the traveler header QR
- anything else              -- treated as an employee badge id (digits per the
                                badge spec, but any unprefixed code is probed
                                against ``users.employee_id`` so legacy
                                alphanumeric ids keep working)
"""

from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

# The five shop-floor actions the resolver evaluates. Names match the kiosk verbs
# and the gate helpers in app/services/operation_action_gates.py.
ScanAction = Literal["clock_in", "report_production", "complete", "hold", "resume"]


class ScanResolveRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=255, description="Raw scanned text (whitespace is stripped).")
    work_center_id: Optional[int] = Field(
        None,
        description="The scanning station's work center, when known. Used only to flag "
        "work_center_match on operation scans; never widens access.",
    )


class RoutingRevisionCheck(BaseModel):
    """Honest routing-staleness signal for an operation scan.

    Work orders do NOT snapshot the routing revision their operations were
    generated from (no routing_id / revision column on WorkOrder or
    WorkOrderOperation), and traveler prints are not recorded server-side. So
    this check is a documented PROXY: it compares the part's current released
    routing's release timestamps against the work order's release/creation
    time. ``released_routing_changed_after_wo_creation`` is None when either
    side lacks a usable timestamp.
    """

    current_released_revision: Optional[str] = None
    released_routing_changed_after_wo_creation: Optional[bool] = None
    checked_against: Optional[str] = Field(
        None, description="ISO timestamp baseline used (work order released_at, else created_at)."
    )
    note: str


class OperationScanSummary(BaseModel):
    id: int
    sequence: int
    operation_number: Optional[str] = None
    name: str
    status: str
    work_order_id: int
    work_order_number: str
    work_order_status: str
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    work_center_id: Optional[int] = None
    work_center_name: Optional[str] = None
    work_center_match: Optional[bool] = Field(
        None, description="True/False when the request carried a work_center_id; None otherwise."
    )
    quantity_complete: float = 0.0
    target_quantity: float = 0.0


class OperationScanResult(BaseModel):
    kind: Literal["operation"] = "operation"
    code: str
    operation: OperationScanSummary
    legal_actions: List[ScanAction] = Field(
        default_factory=list,
        description="Actions the CALLING USER could perform right now, derived from the same "
        "gate predicates the shop-floor write endpoints enforce.",
    )
    blockers: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="action -> human-readable reasons, present only for actions NOT in legal_actions. "
        "Messages match the corresponding endpoint's error text verbatim.",
    )
    warning: Optional[Literal["routing_revision_changed"]] = None
    routing_revision_check: Optional[RoutingRevisionCheck] = None


class WorkOrderOperationBrief(BaseModel):
    id: int
    sequence: int
    operation_number: Optional[str] = None
    name: str
    status: str


class WorkOrderScanSummary(BaseModel):
    id: int
    work_order_number: str
    status: str
    quantity_ordered: float = 0.0
    quantity_complete: float = 0.0
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    current_operation_id: Optional[int] = Field(
        None, description="First non-complete operation by sequence (computed, not the stale column)."
    )


class WorkOrderScanResult(BaseModel):
    kind: Literal["work_order"] = "work_order"
    code: str
    work_order: WorkOrderScanSummary
    operations: List[WorkOrderOperationBrief] = Field(default_factory=list)


class EmployeeScanResult(BaseModel):
    """Badge lookup ONLY -- no auth side effects. Badge LOGIN stays exclusively
    on POST /auth/employee-login."""

    kind: Literal["employee"] = "employee"
    code: str
    employee_id: str
    first_name: str
    last_initial: str


class UnknownScanResult(BaseModel):
    """Structured miss: returned with HTTP 200 so scan clients get a parseable
    result instead of an exception path."""

    kind: Literal["unknown"] = "unknown"
    code: str
    reason: str


ScanResolveResult = Annotated[
    Union[OperationScanResult, WorkOrderScanResult, EmployeeScanResult, UnknownScanResult],
    Field(discriminator="kind"),
]
