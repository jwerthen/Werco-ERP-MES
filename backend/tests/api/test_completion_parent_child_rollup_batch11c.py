"""Behavior locks for the Batch-11C parent/child WO completion rollup (G1, laser-nest).

POSTURE: warn-and-record (mirrors Batch-4). A parent WO that COMPLETEs while a
laser-nest child WO is still non-terminal does NOT block -- it still completes, but
records a ``child_work_orders_incomplete`` QualityException (audit row
``COMPLETED_WITH_QUALITY_EXCEPTION`` + warning event + on the API response). When the
LAST laser child reaches a terminal status, the system records a
``child_work_orders_complete`` signal on the PARENT (audit row
``CHILD_WORK_ORDERS_COMPLETE`` + ``child_work_orders_complete`` OperationalEvent). The
signal is NOT an auto-complete: parent/child WOs are not operation-coupled.

Scope (chosen): only ``WorkOrderType.LASER_CUTTING`` children with ``parent_work_order_id``
set are tracked. TERMINAL_WO_STATUSES = {COMPLETE, CLOSED, CANCELLED}; a CANCELLED child
is treated as RESOLVED, not a blocker.

Covered:
- Unit: ``incomplete_child_work_orders`` / ``find_parent_to_advance`` across child
  states + tenant isolation.
- (a) parent completes with a non-terminal laser child -> child_work_orders_incomplete
  exception + audit + warning event, and the parent STILL completes.
- (b) all children terminal -> no such exception.
- (c) completing the LAST laser child emits the child_work_orders_complete event + a
  CHILD_WORK_ORDERS_COMPLETE audit row attributed to the PARENT, and does NOT fire while
  another child is still open (no-double-fire -- fires exactly once on the last child).
- (d) tenant isolation -- a child/parent in another company is never picked up.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
    WorkOrderType,
)
from app.services.work_order_state_service import (
    find_parent_to_advance,
    incomplete_child_work_orders,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"b11c-g1-{n}@co{company_id}.test",
        employee_id=f"B11CG1-{n:05d}",
        first_name="B11C",
        last_name="G1",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11CG1-P-{n}",
        name=f"Part {n}",
        description="batch11c G1 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"B11CG1-WC-{n}",
        code=f"B11CG1-WC-{n}",
        work_center_type="laser",
        description="batch11c G1 fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    quantity_ordered: float = 5,
    work_order_type: WorkOrderType = WorkOrderType.PRODUCTION,
    parent_work_order_id: int = None,
    company_id: int = COMPANY_A,
    is_deleted: bool = False,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B11CG1-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        work_order_type=work_order_type.value,
        parent_work_order_id=parent_work_order_id,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    status_: OperationStatus = OperationStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def _child_incomplete_audit(db: Session, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "COMPLETED_WITH_QUALITY_EXCEPTION", AuditLog.company_id == company_id)
        .all()
    )


def _children_complete_audit(db: Session, parent_id: int, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "CHILD_WORK_ORDERS_COMPLETE",
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == parent_id,
        )
        .all()
    )


def _children_complete_events(db: Session, parent_id: int) -> list[OperationalEvent]:
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == "child_work_orders_complete",
            OperationalEvent.work_order_id == parent_id,
        )
        .all()
    )


def complete_op_office(client: TestClient, user: User, op: WorkOrderOperation, qty: float):
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete={qty}",
        headers=headers_for(user),
    )


# ---------------------------------------------------------------------------
# Unit: incomplete_child_work_orders / find_parent_to_advance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_incomplete_child_work_orders_only_counts_nonterminal_laser_children(db_session: Session):
    part = make_part(db_session)
    parent = make_wo(db_session, part, work_order_type=WorkOrderType.PRODUCTION)
    # Two laser children: one open (in progress), one cancelled (terminal => resolved).
    open_child = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.CANCELLED,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    # A non-laser child must NOT be tracked even if open.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.PRODUCTION,
        parent_work_order_id=parent.id,
    )
    # A soft-deleted open laser child must NOT be tracked.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
        is_deleted=True,
    )
    db_session.commit()

    incomplete = incomplete_child_work_orders(db_session, parent, COMPANY_A)
    assert [c.id for c in incomplete] == [open_child.id]


@pytest.mark.unit
def test_incomplete_child_work_orders_empty_when_all_terminal(db_session: Session):
    part = make_part(db_session)
    parent = make_wo(db_session, part)
    for st in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED):
        make_wo(
            db_session,
            part,
            status_=st,
            work_order_type=WorkOrderType.LASER_CUTTING,
            parent_work_order_id=parent.id,
        )
    db_session.commit()

    assert incomplete_child_work_orders(db_session, parent, COMPANY_A) == []


@pytest.mark.unit
def test_find_parent_to_advance_only_when_last_child_terminal(db_session: Session):
    part = make_part(db_session)
    parent = make_wo(db_session, part)
    child_a = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    child_b = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    db_session.commit()

    # child_a completed but child_b is still open -> no advance.
    assert find_parent_to_advance(db_session, child_a, COMPANY_A) is None

    # Now child_b flips terminal -> the LAST child -> advance returns the parent.
    child_b.status = WorkOrderStatus.COMPLETE
    db_session.flush()
    advanced = find_parent_to_advance(db_session, child_b, COMPANY_A)
    assert advanced is not None and advanced.id == parent.id


@pytest.mark.unit
def test_find_parent_to_advance_none_for_orphan_wo(db_session: Session):
    part = make_part(db_session)
    orphan = make_wo(db_session, part, work_order_type=WorkOrderType.LASER_CUTTING)
    db_session.commit()
    assert find_parent_to_advance(db_session, orphan, COMPANY_A) is None


@pytest.mark.unit
def test_incomplete_children_tenant_isolation(db_session: Session):
    """A laser child in company B with company-B parent_work_order_id matching a company-A
    parent id must never be surfaced for the company-A parent."""
    part_a = make_part(db_session, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=COMPANY_B)
    parent_a = make_wo(db_session, part_a, company_id=COMPANY_A)
    db_session.commit()
    # A company-B child that points (by id) at the company-A parent.
    make_wo(
        db_session,
        part_b,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent_a.id,
        company_id=COMPANY_B,
    )
    db_session.commit()

    # Scoped to company A: the company-B child is invisible.
    assert incomplete_child_work_orders(db_session, parent_a, COMPANY_A) == []


# ---------------------------------------------------------------------------
# (a) parent completes with a non-terminal laser child -> warns, still completes
# ---------------------------------------------------------------------------


def test_parent_complete_with_open_laser_child_warns_not_blocks(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    parent = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    parent_op = make_op(db_session, parent, wc, sequence=10)
    # An open laser child of the parent.
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    db_session.commit()

    resp = complete_op_office(client, admin, parent_op, 5)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "child_work_orders_incomplete" in codes

    db_session.expire_all()
    refreshed = db_session.get(WorkOrder, parent.id)
    assert refreshed.status == WorkOrderStatus.COMPLETE  # still completed (warn, not block)

    audit = _child_incomplete_audit(db_session)
    assert any("child_work_orders_incomplete" in (a.new_values or {}).get("quality_exceptions", []) for a in audit)
    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == "quality_exception_on_completion",
            OperationalEvent.work_order_id == parent.id,
        )
        .all()
    )
    assert events and events[0].severity == "warning"


# ---------------------------------------------------------------------------
# (b) all children terminal -> no child_work_orders_incomplete exception
# ---------------------------------------------------------------------------


def test_parent_complete_with_all_children_terminal_no_exception(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    parent = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    parent_op = make_op(db_session, parent, wc, sequence=10)
    # All children terminal (COMPLETE + CANCELLED -- the cancelled one counts as resolved).
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.COMPLETE,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.CANCELLED,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    db_session.commit()

    resp = complete_op_office(client, admin, parent_op, 5)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    codes = [e["code"] for e in resp.json()["quality_exceptions"]]
    assert "child_work_orders_incomplete" not in codes


# ---------------------------------------------------------------------------
# (c) completing the LAST laser child emits child_work_orders_complete (once)
# ---------------------------------------------------------------------------


def test_last_laser_child_completion_signals_parent_exactly_once(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    parent = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)

    # Two laser children, each a one-op WO so completing the op completes the child WO.
    child_a = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        quantity_ordered=3,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    child_a_op = make_op(db_session, child_a, wc, sequence=10)
    child_b = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        quantity_ordered=3,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    child_b_op = make_op(db_session, child_b, wc, sequence=10)
    db_session.commit()

    # Complete the FIRST child -> child_b still open -> NO parent signal yet.
    resp_a = complete_op_office(client, admin, child_a_op, 3)
    assert resp_a.status_code == status.HTTP_200_OK, resp_a.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, child_a.id).status == WorkOrderStatus.COMPLETE
    assert _children_complete_audit(db_session, parent.id) == []
    assert _children_complete_events(db_session, parent.id) == []

    # Complete the LAST child -> now ALL laser children terminal -> parent signal fires.
    resp_b = complete_op_office(client, admin, child_b_op, 3)
    assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, child_b.id).status == WorkOrderStatus.COMPLETE

    audit = _children_complete_audit(db_session, parent.id)
    assert len(audit) == 1, "exactly one CHILD_WORK_ORDERS_COMPLETE audit row on the parent"
    assert audit[0].resource_id == parent.id
    assert (audit[0].extra_data or {}).get("child_work_order_id") == child_b.id

    events = _children_complete_events(db_session, parent.id)
    assert len(events) == 1, "exactly one child_work_orders_complete event on the parent"
    assert events[0].severity == "info"
    assert events[0].entity_id == parent.id


def test_last_laser_child_via_shop_floor_complete_signals_parent(client: TestClient, db_session: Session):
    """The shop-floor complete_operation path (operator) also fires the parent advance."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    part = make_part(db_session)
    parent = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    child = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        quantity_ordered=4,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent.id,
    )
    child_op = make_op(db_session, child, wc, sequence=10)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{child_op.id}/complete",
        json={"quantity_complete": 4},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, child.id).status == WorkOrderStatus.COMPLETE
    assert len(_children_complete_audit(db_session, parent.id)) == 1
    assert len(_children_complete_events(db_session, parent.id)) == 1


# ---------------------------------------------------------------------------
# (d) tenant isolation on the live completion path
# ---------------------------------------------------------------------------


def test_parent_advance_tenant_isolation_on_completion(client: TestClient, db_session: Session):
    """A company-A laser child whose parent_work_order_id points at a company-B parent id
    must NOT signal that company-B parent on completion -- the advance is company-scoped."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=COMPANY_B)
    # A company-B "parent".
    parent_b = make_wo(db_session, part_b, status_=WorkOrderStatus.IN_PROGRESS, company_id=COMPANY_B)
    db_session.commit()

    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    # A company-A child that (maliciously/erroneously) points at the company-B parent id.
    child_a = make_wo(
        db_session,
        part_a,
        status_=WorkOrderStatus.IN_PROGRESS,
        quantity_ordered=2,
        work_order_type=WorkOrderType.LASER_CUTTING,
        parent_work_order_id=parent_b.id,
        company_id=COMPANY_A,
    )
    child_a_op = make_op(db_session, child_a, wc_a, sequence=10, company_id=COMPANY_A)
    db_session.commit()

    resp = complete_op_office(client, admin_a, child_a_op, 2)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, child_a.id).status == WorkOrderStatus.COMPLETE

    # No signal landed on the company-B parent (cross-tenant) under either company tag.
    assert _children_complete_audit(db_session, parent_b.id, company_id=COMPANY_A) == []
    assert _children_complete_audit(db_session, parent_b.id, company_id=COMPANY_B) == []
    assert _children_complete_events(db_session, parent_b.id) == []
