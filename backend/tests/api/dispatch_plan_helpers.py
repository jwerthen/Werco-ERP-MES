"""Shared factories for the Dispatch v2 (work-order plan) test files.

``test_dispatch_effective_rank.py``, ``test_dispatch_work_order_plan.py`` and
``test_dispatch_operation_order.py`` all need multi-operation work orders with a
chosen plan rank, sequencing mode, type and status, which the single-operation
``kiosk_test_helpers.make_wo_with_operation`` cannot express. Every natural key
routes through a module counter so rows stay unique under ``-n auto``.
"""

from datetime import date
from typing import Callable, List, Optional, Sequence, Tuple

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from tests.api.kiosk_test_helpers import COMPANY_A, ensure_company

BOARD_URL = "/api/v1/shop-floor/dispatch-board"
LANES_URL = "/api/v1/shop-floor/dispatch-board/work-orders"
PLAN_URL = "/api/v1/shop-floor/dispatch-board/work-order-order"


def operation_order_url(work_order_id: int) -> str:
    return f"/api/v1/shop-floor/dispatch-board/work-orders/{work_order_id}/operation-order"


def run_order_url(work_center_id: int) -> str:
    return f"/api/v1/shop-floor/work-centers/{work_center_id}/run-order"


def kiosk_scoped_headers(user, company_id: int = COMPANY_A) -> dict:
    token = create_access_token(subject=user.id, company_id=company_id, scope="kiosk")
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# (work center, status, sequence) -- or (work center, status, sequence, run_order)
OpSpec = Tuple


def make_plan_wo(
    db: Session,
    ops: Sequence[OpSpec],
    *,
    company_id: int = COMPANY_A,
    status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    sequential: bool = True,
    laser: bool = False,
    priority: int = 5,
    due_date: Optional[date] = None,
    must_ship_by: Optional[date] = None,
    dispatch_rank: Optional[int] = None,
    customer_name: Optional[str] = None,
    unit_number: Optional[str] = None,
) -> Tuple[WorkOrder, List[WorkOrderOperation]]:
    """A work order with one operation per ``ops`` entry, committed and refreshed."""
    ensure_company(db, company_id)
    n = _next()
    part = None
    if not laser:
        part = Part(
            part_number=f"DP-{n:05d}",
            name=f"Plan part {n}",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=company_id,
        )
        db.add(part)
        db.flush()
    work_order = WorkOrder(
        work_order_number=f"DWO-{n:05d}",
        part_id=part.id if part else None,
        work_order_type=WorkOrderType.LASER_CUTTING.value if laser else WorkOrderType.PRODUCTION.value,
        sequential_operations=sequential,
        quantity_ordered=10,
        status=status,
        priority=priority,
        due_date=due_date,
        must_ship_by=must_ship_by,
        dispatch_rank=dispatch_rank,
        customer_name=customer_name,
        unit_number=unit_number,
        company_id=company_id,
    )
    db.add(work_order)
    db.flush()
    operations = []
    for spec in ops:
        work_center, op_status, sequence = spec[0], spec[1], spec[2]
        run_order = spec[3] if len(spec) > 3 else None
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=sequence,
            operation_number=str(sequence),
            name=f"Op {sequence} of {n}",
            status=op_status,
            run_order=run_order,
            setup_time_hours=0.5,
            run_time_hours=1.25,
            company_id=company_id,
        )
        db.add(operation)
        operations.append(operation)
    db.commit()
    db.refresh(work_order)
    for operation in operations:
        db.refresh(operation)
    return work_order, operations


def plan(client: TestClient, headers: dict, work_order_ids: List[int], expected: Optional[List[int]] = None):
    """PUT the work-order plan. ``expected`` defaults to the live plan read from the GET."""
    if expected is None:
        lanes = client.get(LANES_URL, headers=headers)
        assert lanes.status_code == 200, lanes.text
        expected = [lane["work_order_id"] for lane in lanes.json()["work_orders"] if lane["dispatch_position"]]
    return client.put(
        PLAN_URL,
        json={"work_order_ids": work_order_ids, "expected_work_order_ids": expected},
        headers=headers,
    )


def lane_for(payload: dict, work_order_id: int) -> dict:
    for lane in payload["work_orders"]:
        if lane["work_order_id"] == work_order_id:
            return lane
    raise AssertionError(f"work order {work_order_id} missing from the lanes")


def count_statements(db: Session, fn: Callable[[], object]) -> int:
    """Statements executed on the fixture session's OWN engine while ``fn`` runs.

    Bound to ``db.get_bind()`` for the reason ``test_dispatch_nest_details`` spells
    out: ``tests.conftest`` is imported twice, so a module-level engine can be a
    different object from the one the session (and the test client) use.
    """
    statements: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(bind, "after_cursor_execute", _record)
    return len(statements)


def ready(work_center: WorkCenter, sequence: int = 10, run_order: Optional[int] = None) -> OpSpec:
    return (work_center, OperationStatus.READY, sequence, run_order)
