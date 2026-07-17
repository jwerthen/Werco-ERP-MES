"""Behavior locks for the operator over-count correction endpoint.

``POST /shop-floor/operations/{id}/reduce-production`` lets a shop-floor operator
walk back good-count quantity they accidentally OVER-reported on an operation they
are actively working, BEFORE it is complete. It is the inverse of the additive
``report_operation_production`` verb.

The crux is reconcile-safety: produced quantity is deliberately monotonic-up and
re-derived from durable ``TimeEntry.quantity_produced`` evidence on every work-order
read (``reconcile_work_orders_from_completion_evidence`` RAISES ``quantity_complete``
to the evidence sum and never lowers it). So a correct reduction must lower the
backing evidence, the operation total, and the WO rollup in lock-step -- otherwise
the next read would re-raise the count. ``test_reduce_then_reconcile_keeps_reduction``
and ``test_reduce_survives_direct_reconcile`` are the load-bearing proofs.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import reconcile_work_orders_from_completion_evidence

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


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"reduce-{n}@co{company_id}.test",
        employee_id=f"REDUCE-{n:05d}",
        first_name="Reduce",
        last_name="Operator",
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


def make_wo_op(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    quantity_ordered: int = 20,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """One work order with a single operation (defaults: both IN_PROGRESS, qty 20).

    The ordered qty is comfortably above the amounts these tests report so the
    additive ``/production`` seeding never trips its over-completion cap.
    """
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"REDUCE-P-{n}",
        name=f"Part {n}",
        description="reduce-production fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"REDUCE-WC-{n}",
        code=f"REDUCE-WC-{n}",
        work_center_type="welding",
        description="reduce-production fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"REDUCE-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Op 10",
        status=op_status,
        quantity_complete=0,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    return wo, op, wc


def make_open_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    quantity_produced: float = 0.0,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        quantity_produced=quantity_produced,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def report_production(client: TestClient, user: User, op: WorkOrderOperation, delta: float) -> None:
    """Seed produced quantity via the real additive endpoint (the twin verb)."""
    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        json={"quantity_complete_delta": delta},
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def reduce_url(op: WorkOrderOperation) -> str:
    return f"/api/v1/shop-floor/operations/{op.id}/reduce-production"


def make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    n = _next()
    part = Part(
        part_number=f"REDUCE-COMP-{n}",
        name=f"Component {n}",
        description="reduce-production fixture component part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def add_operation(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int,
    quantity_complete: float,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    component_part_id: int | None = None,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    """Add a second/third operation to an existing WO (multi-op rollup fixtures)."""
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=op_status,
        quantity_complete=quantity_complete,
        component_part_id=component_part_id,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def set_wo_quantity_complete(db: Session, wo: WorkOrder, value: float) -> None:
    row = db.get(WorkOrder, wo.id)
    row.quantity_complete = value
    db.commit()


# ===========================================================================
# Reconcile-safety -- the whole point of the feature
# ===========================================================================


def test_reduce_then_reconcile_keeps_reduction(client: TestClient, db_session: Session):
    """Report 12, reduce 10 -> a WO GET (which reconciles) leaves the count at 2, not 12."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 12)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 10, "reason": "double-scanned the tray"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["operation"]["quantity_complete"] == 2
    assert body["active_time_entry"]["quantity_produced"] == 2

    # The load-bearing assertion: a full WO GET runs reconcile-on-read; the reduced
    # count must NOT pop back up to 12 from the (now-lowered) evidence sum.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    wo_body = get_resp.json()
    assert wo_body["quantity_complete"] == 2
    reduced_op = next(o for o in wo_body["operations"] if o["id"] == op.id)
    assert reduced_op["quantity_complete"] == 2
    assert reduced_op["status"] == OperationStatus.IN_PROGRESS.value

    # And the durable evidence itself dropped, so nothing can re-raise it later.
    db_session.expire_all()
    entry = db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one()
    assert entry.quantity_produced == 2
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 2
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 2


def test_reduce_survives_direct_reconcile(client: TestClient, db_session: Session):
    """Belt-and-suspenders: invoking the reconcile service directly does not re-raise."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 8)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "miscount"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    changed = reconcile_work_orders_from_completion_evidence(db_session, [wo])
    db_session.commit()

    op_row = db_session.get(WorkOrderOperation, op.id)
    assert op_row.quantity_complete == 3, "reconcile must not re-raise a corrected count"
    assert not changed or op_row.quantity_complete == 3


# ===========================================================================
# Multi-op WO rollup -- the WO qty is RECOMPUTED from the siblings (max over
# non-component ops of min(op.quantity_complete, target)), only ever LOWERED,
# never blindly subtracted. A blind subtract corrupts a multi-op WO.
# ===========================================================================


def test_reduce_non_max_op_leaves_wo_unchanged(client: TestClient, db_session: Session):
    """Reviewer's repro: opA=100, opB=50, WO=100; reduce opB by 10 -> WO stays 100, opB=40.

    Another op (opA) still holds the higher count, so the WO rollup must NOT drop -- and
    the invariant WO >= max(op) must survive a GET-driven reconcile.
    """
    operator = make_user(db_session)
    wo, op_a, wc = make_wo_op(db_session, quantity_ordered=100)
    op_a.status = OperationStatus.COMPLETE  # an earlier COMPLETE op is the WO definer
    op_a.quantity_complete = 100
    db_session.commit()
    op_b = add_operation(db_session, wo, wc, sequence=20, quantity_complete=50)
    set_wo_quantity_complete(db_session, wo, 100)
    make_open_entry(db_session, operator, wo, op_b, quantity_produced=50)

    resp = client.post(
        reduce_url(op_b),
        json={"quantity_delta": 10, "reason": "over-counted op B"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op_b.id).quantity_complete == 40
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 100, "opA still defines the rollup"

    # Survives reconcile-on-read with WO >= max(op) intact.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    body = get_resp.json()
    assert body["quantity_complete"] == 100
    assert body["quantity_complete"] >= max(o["quantity_complete"] for o in body["operations"])


def test_reduce_max_op_drops_wo_to_next_highest(client: TestClient, db_session: Session):
    """Reduce the op that DEFINES the rollup -> WO drops to the next-highest op's capped qty."""
    operator = make_user(db_session)
    wo, op_a, wc = make_wo_op(db_session, quantity_ordered=100)
    op_a.quantity_complete = 100
    db_session.commit()
    add_operation(db_session, wo, wc, sequence=20, quantity_complete=50)  # sibling opB (the next-highest)
    set_wo_quantity_complete(db_session, wo, 100)
    make_open_entry(db_session, operator, wo, op_a, quantity_produced=100)

    resp = client.post(
        reduce_url(op_a),
        json={"quantity_delta": 60, "reason": "over-counted op A"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op_a.id).quantity_complete == 40
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 50, "opB (50) is now the definer"

    # And it stays put across a reconcile with WO >= max(op).
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    body = get_resp.json()
    assert body["quantity_complete"] == 50
    assert body["quantity_complete"] >= max(o["quantity_complete"] for o in body["operations"])


def test_reduce_component_op_leaves_parent_wo_unchanged(client: TestClient, db_session: Session):
    """Component-op production never rolled INTO the WO rollup -> reducing it can't lower the WO."""
    operator = make_user(db_session)
    wo, op_main, wc = make_wo_op(db_session, quantity_ordered=100)
    op_main.quantity_complete = 100
    db_session.commit()
    comp_part = make_part(db_session)
    op_comp = add_operation(db_session, wo, wc, sequence=20, quantity_complete=50, component_part_id=comp_part.id)
    set_wo_quantity_complete(db_session, wo, 100)
    make_open_entry(db_session, operator, wo, op_comp, quantity_produced=50)

    resp = client.post(
        reduce_url(op_comp),
        json={"quantity_delta": 10, "reason": "over-counted the component"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op_comp.id).quantity_complete == 40
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 100, "component op never defined the rollup"


# ===========================================================================
# Preconditions
# ===========================================================================


def test_reduce_delta_exceeds_recorded_is_400(client: TestClient, db_session: Session):
    """You may only walk back what YOU recorded on this clock-in (crew-safe bound)."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 3)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "oops"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "up to the 3" in resp.json()["detail"]

    # Nothing mutated on the rejected request.
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 3
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 3


def test_reduce_bound_is_per_caller_not_operation_total(client: TestClient, db_session: Session):
    """A second operator can't walk back the FIRST operator's evidence."""
    op1 = make_user(db_session)
    op2 = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, op1, wo, op, quantity_produced=9)  # op1 already booked 9
    make_open_entry(db_session, op2, wo, op, quantity_produced=1)  # op2 booked 1
    op.quantity_complete = 10
    db_session.commit()

    # op2 tries to remove 5 but only recorded 1 -> bounded to their own evidence.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "not mine to remove"},
        headers=headers_for(op2),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "up to the 1" in resp.json()["detail"]


def test_reduce_without_open_entry_is_400(client: TestClient, db_session: Session):
    """No open clock-in of the caller on this operation -> 400."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    # No TimeEntry created.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 1, "reason": "no clock-in"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "clocked in" in resp.json()["detail"]


def test_reduce_completed_operation_is_409(client: TestClient, db_session: Session):
    """A COMPLETE operation is corrected by a supervisor, not self-service -> 409."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session, op_status=OperationStatus.COMPLETE)
    make_open_entry(db_session, operator, wo, op, quantity_produced=10)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 1, "reason": "too late"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "supervisor" in resp.json()["detail"]


@pytest.mark.parametrize(
    "wo_status",
    [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
)
def test_reduce_terminal_work_order_is_409(client: TestClient, db_session: Session, wo_status: WorkOrderStatus):
    """A terminal work order (COMPLETE/CLOSED/CANCELLED) cannot be corrected here -> 409."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session, wo_status=wo_status)
    make_open_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 1, "reason": "job is done"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text


def test_reduce_cross_company_operation_is_404(client: TestClient, db_session: Session):
    """An operation in another company is invisible -> 404 (tenant isolation)."""
    _ensure_company(db_session, COMPANY_B)
    caller = make_user(db_session, company_id=COMPANY_A)
    # Operation + its operator belong to company B.
    other_operator = make_user(db_session, company_id=COMPANY_B)
    wo_b, op_b, _wc = make_wo_op(db_session, company_id=COMPANY_B)
    make_open_entry(db_session, other_operator, wo_b, op_b, quantity_produced=5, company_id=COMPANY_B)

    resp = client.post(
        reduce_url(op_b),
        json={"quantity_delta": 1, "reason": "cross-tenant"},
        headers=headers_for(caller),  # company A token
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ===========================================================================
# Schema validation
# ===========================================================================


def test_reduce_missing_reason_is_422(client: TestClient, db_session: Session):
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(reduce_url(op), json={"quantity_delta": 1}, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_reduce_blank_reason_is_422(client: TestClient, db_session: Session):
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 1, "reason": "   "},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


@pytest.mark.parametrize("bad_delta", [0, -1, "nan", "inf"])
def test_reduce_non_positive_or_non_finite_delta_is_422(client: TestClient, db_session: Session, bad_delta: object):
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": bad_delta, "reason": "bad number"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


# ===========================================================================
# Side effects: audit + scrap-untouched
# ===========================================================================


def test_reduce_writes_tamper_evident_audit_row(client: TestClient, db_session: Session):
    """The correction lands one AuditService.log_update row carrying the reason."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 6)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 4, "reason": "counted the rejects by mistake"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None, "reduction must be on the tamper-evident audit chain"
    assert "counted the rejects by mistake" in (audit_row.description or "")
    assert audit_row.extra_data.get("reason") == "counted the rejects by mistake"
    assert audit_row.extra_data.get("time_entry_id") is not None


def test_reduce_audit_diff_captures_time_entry_produced(client: TestClient, db_session: Session):
    """The audited diff carries the always-changing TimeEntry.quantity_produced (un-skippable).

    ``log_update`` skips writing when its computed diff is empty; coupling the audit to
    the produced-qty before->after (which a positive delta ALWAYS lowers) guarantees the
    diff can never collapse, so a correction can never commit unaudited.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 9)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "diff must not collapse"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    # Persisted old/new value columns carry the produced-qty change (9 -> 4).
    assert audit_row.old_values["time_entry_quantity_produced"] == 9
    assert audit_row.new_values["time_entry_quantity_produced"] == 4
    # And the computed diff (extra_data["changes"]) can never be empty.
    changes = audit_row.extra_data["changes"]
    assert "time_entry_quantity_produced" in changes
    assert changes["time_entry_quantity_produced"] == {"old": 9, "new": 4}


def test_reduce_refuses_approved_labor(client: TestClient, db_session: Session):
    """G5-A: APPROVED evidence is excluded from the self-service allowance -> 400, nothing mutated.

    Cross-session semantics: approval (not clock-out) is the immutability boundary.
    The operator's ONLY evidence here is their open entry, and it is approved, so the
    unapproved allowance is 0 and the 400 names the real remedy (a supervisor).
    """
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 6)

    # A supervisor signs off on the (still-open) labor.
    entry = db_session.get(TimeEntry, entry.id)
    entry.approved = datetime.utcnow()
    entry.approved_by = supervisor.id
    db_session.commit()

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "already approved"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    detail = resp.json()["detail"]
    assert "up to the 0" in detail
    assert "approved labor needs a supervisor" in detail

    # Nothing mutated on the refused request.
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 6
    assert db_session.get(TimeEntry, entry.id).quantity_produced == 6


def test_reduce_does_not_touch_scrap(client: TestClient, db_session: Session):
    """A good-count correction never moves scrap counters or the reason fields."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 7)
    # Stamp some pre-existing scrap so we can prove it is untouched.
    op.quantity_scrapped = 2
    op.scrap_reason = "pre-existing scrap"
    entry.quantity_scrapped = 2
    db_session.commit()

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 3, "reason": "good-count only"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op_row = db_session.get(WorkOrderOperation, op.id)
    entry_row = db_session.get(TimeEntry, entry.id)
    assert op_row.quantity_scrapped == 2
    assert op_row.scrap_reason == "pre-existing scrap"
    assert op_row.quantity_complete == 4  # 7 - 3
    assert op_row.status == OperationStatus.IN_PROGRESS  # status unchanged
    assert entry_row.quantity_scrapped == 2
    assert entry_row.quantity_produced == 4


def test_reduce_on_rework_entry_decrements_quantity_reworked(client: TestClient, db_session: Session):
    """True inverse of the twin: reducing a REWORK clock-in drops quantity_reworked by the delta."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op, entry_type=TimeEntryType.REWORK)
    # The additive twin raises quantity_reworked because the active entry is REWORK.
    report_production(client, operator, op, 6)
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_reworked == 6

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 4, "reason": "over-counted the rework"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op_row = db_session.get(WorkOrderOperation, op.id)
    assert op_row.quantity_reworked == 2  # 6 - 4, stays >= 0
    assert op_row.quantity_complete == 2  # good count reduced in lock-step


def test_reduce_on_run_entry_leaves_quantity_reworked_untouched(client: TestClient, db_session: Session):
    """A RUN-entry correction must NOT touch quantity_reworked (twin only touches it for REWORK)."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op, entry_type=TimeEntryType.RUN)
    report_production(client, operator, op, 6)
    # Stamp a pre-existing rework total from some earlier rework pass; a RUN correction
    # must leave it exactly as-is.
    op.quantity_reworked = 3
    db_session.commit()

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 4, "reason": "good-run miscount"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op_row = db_session.get(WorkOrderOperation, op.id)
    assert op_row.quantity_reworked == 3  # unchanged
    assert op_row.quantity_complete == 2  # 6 - 4


def test_reduce_translates_stale_version_to_409(client: TestClient, db_session: Session, monkeypatch):
    """A concurrent stale write (StaleDataError on commit) surfaces as 409, not 500."""
    from sqlalchemy.orm.exc import StaleDataError

    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 6)

    original_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit(*args, **kwargs):
        # Fail only the reduce endpoint's commit (the first commit after patching).
        if calls["n"] == 0:
            calls["n"] += 1
            raise StaleDataError("simulated concurrent version bump")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", flaky_commit)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "raced"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "concurrently" in resp.json()["detail"]


# ===========================================================================
# Gap-fill: boundary (reduce-to-0), cross-operator success path, reduce after a
# partial /complete, import-source parity, and the audit old->new capture. These
# fill holes the 21 originals leave: they only reduce PARTIAL amounts, only prove
# a cross-operator OVER-reduction is REJECTED (never that a valid one leaves the
# crewmate's evidence alone), never reduce an op whose total was raised ABOVE the
# caller's evidence by the absolute /complete verb, don't assert the loader-source
# 422 that the additive twins get, and assert the audit reason but not old->new.
# ===========================================================================


def test_reduce_entire_recorded_amount_to_zero(client: TestClient, db_session: Session):
    """delta == recorded (inclusive boundary): report 5, reduce 5 -> 0 everywhere and it stays 0 on read.

    Also proves a now-ZERO good count does NOT auto-complete the still-open operation
    on the reconcile-on-read path.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "voided the whole tray"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["operation"]["quantity_complete"] == 0
    assert body["active_time_entry"]["quantity_produced"] == 0

    # A WO GET reconciles from evidence; a now-zero evidence sum must not re-raise,
    # and a 0 good-count must not flip the still-open operation to COMPLETE.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    wo_body = get_resp.json()
    assert wo_body["quantity_complete"] == 0
    zeroed_op = next(o for o in wo_body["operations"] if o["id"] == op.id)
    assert zeroed_op["quantity_complete"] == 0
    assert zeroed_op["status"] == OperationStatus.IN_PROGRESS.value

    db_session.expire_all()
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 0
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 0
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 0


def test_reduce_only_affects_callers_entry_and_survives_reconcile(client: TestClient, db_session: Session):
    """Two operators book on one op; operator B's VALID walk-back touches only B's evidence.

    The complement of ``test_reduce_bound_is_per_caller_not_operation_total`` (which only
    proves B cannot OVER-reduce): here B removes their own full amount, A's durable
    evidence is left intact, and because the operation total still exceeds B's own entry
    going in, the WO GET reconcile is the load-bearing proof it does NOT pop back up.
    """
    op1 = make_user(db_session)
    op2 = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, op1, wo, op)
    make_open_entry(db_session, op2, wo, op)
    report_production(client, op1, op, 9)  # op1's own open entry = 9
    report_production(client, op2, op, 3)  # op2's own open entry = 3; op total = 12

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 12
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 12

    # op2 walks back their own full 3 (delta == op2's recorded; op1's 9 is untouchable).
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 3, "reason": "my miscount only"},
        headers=headers_for(op2),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["operation"]["quantity_complete"] == 9

    db_session.expire_all()
    entry1 = db_session.query(TimeEntry).filter(TimeEntry.user_id == op1.id, TimeEntry.operation_id == op.id).one()
    entry2 = db_session.query(TimeEntry).filter(TimeEntry.user_id == op2.id, TimeEntry.operation_id == op.id).one()
    assert entry1.quantity_produced == 9, "operator A's durable evidence must be untouched"
    assert entry2.quantity_produced == 0

    # op total (9) exceeded op2's own entry going in; the WO GET reconcile must leave the
    # count at 9 (op1's surviving 9), NOT re-raise it to the pre-reduction 12.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(op1))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    assert get_resp.json()["quantity_complete"] == 9
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 9
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 9


def test_reduce_after_partial_complete_that_raised_quantity(client: TestClient, db_session: Session):
    """A partial /complete raises the op total ABOVE the caller's evidence but leaves it open + IN_PROGRESS.

    The absolute /complete verb clamps to max(existing, requested, evidence); a partial
    completion (< target) neither closes the clock-in nor credits the entry. A later
    reduction must still walk the op total DOWN by the full delta (from 10, not just from
    the 6 of evidence) and stay reconcile-safe on the next read.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session, quantity_ordered=20)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 6)  # entry=6, op=6

    comp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 10},  # < target 20 -> partial: op stays IN_PROGRESS
        headers=headers_for(operator),
    )
    assert comp.status_code == status.HTTP_200_OK, comp.text
    db_session.expire_all()
    op_row = db_session.get(WorkOrderOperation, op.id)
    assert op_row.status == OperationStatus.IN_PROGRESS
    assert op_row.quantity_complete == 10
    entry = db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one()
    assert entry.clock_out is None, "a partial complete must leave the clock-in open"
    assert entry.quantity_produced == 6, "a partial complete must not credit the entry"

    # Remove 4 (bounded by the entry's recorded 6): op 10 -> 6, entry 6 -> 2.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 4, "reason": "over-reported on the partial complete"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["operation"]["quantity_complete"] == 6

    # Reconcile-on-read: evidence sum is now 2 and op=6, so 6 >= 2 -> nothing to re-raise.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    reduced_op = next(o for o in get_resp.json()["operations"] if o["id"] == op.id)
    assert reduced_op["quantity_complete"] == 6
    assert reduced_op["status"] == OperationStatus.IN_PROGRESS.value
    db_session.expire_all()
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 2


def test_reduce_rejects_import_source_422_without_mutating(client: TestClient, db_session: Session):
    """source='import' is loader-reserved -> 422 like the additive twins, and nothing is walked back.

    The additive endpoints (clock-out / production / complete / hold) are covered in
    test_adoption_source_tagging; the reduce twin needs the same guard proven: the 422 is
    raised before any mutation, so the entry and operation totals are unchanged.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 5)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "should not apply", "source": "import"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 5
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 5


def test_reduce_audit_row_records_old_and_new_quantity_complete(client: TestClient, db_session: Session):
    """The audit row captures the op good-count transition old->new (not just the reason).

    ``test_reduce_writes_tamper_evident_audit_row`` locks the reason + time_entry_id in
    extra_data; this locks the other half AUD-3 needs -- the before/after quantities on
    ``old_values``/``new_values`` (the operation) and the WO/entry before/after in
    extra_data -- so a reviewer can read the exact correction off the tamper-evident chain.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 9)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 4, "reason": "counted setup scrap as good"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    # old -> new on the audited resource itself (the operation good count): 9 -> 5.
    # time_entry_quantity_produced rides along in old/new_values too (compliance
    # hardening): it is ALWAYS lowered by a positive delta, so log_update's empty-diff
    # skip can never fire and a correction can never commit unaudited.
    assert audit_row.old_values == {"quantity_complete": 9, "time_entry_quantity_produced": 9}
    assert audit_row.new_values == {"quantity_complete": 5, "time_entry_quantity_produced": 5}
    # The full paper trail for the entry and the WO rollup rides extra_data.
    assert audit_row.extra_data["quantity_delta"] == 4
    assert audit_row.extra_data["time_entry_quantity_produced_before"] == 9
    assert audit_row.extra_data["time_entry_quantity_produced_after"] == 5
    assert audit_row.extra_data["work_order_quantity_complete_before"] == 9
    assert audit_row.extra_data["work_order_quantity_complete_after"] == 5


# ===========================================================================
# Cross-session walk-back (v2): eligibility = the caller's own UNAPPROVED
# evidence -- open clock-in first, then their closed unapproved sessions
# newest-first. Approval (not clock-out) is the immutability boundary.
# ===========================================================================


def make_closed_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    *,
    quantity_produced: float,
    hours_ago: float = 2.0,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    approved_by: int | None = None,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    """A CLOSED session on the operation (clock_out set; optionally approved)."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=entry_type,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago + 1),
        clock_out=datetime.utcnow() - timedelta(hours=hours_ago),
        quantity_produced=quantity_produced,
        approved=(datetime.utcnow() if approved_by is not None else None),
        approved_by=approved_by,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def test_reduce_own_closed_entry_production_repro(client: TestClient, db_session: Session):
    """The production repro: over-count noticed AFTER check-out.

    Session 1: report 12, clock out. Session 2: fresh clock-in (0 produced), reduce 3.
    Must succeed -- the walk skips the empty open entry and lowers the CLOSED session's
    evidence -- and must survive the WO GET reconcile.
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    session1 = make_open_entry(db_session, operator, wo, op)
    report_production(client, operator, op, 12)

    # Clock out session 1 (evidence stays on the closed row), then clock in fresh.
    entry1 = db_session.get(TimeEntry, session1.id)
    entry1.clock_out = datetime.utcnow()
    db_session.commit()
    session2 = make_open_entry(db_session, operator, wo, op)

    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 3, "reason": "noticed the double-count after checkout"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["operation"]["quantity_complete"] == 9
    assert body["active_time_entry"]["quantity_produced"] == 0  # fresh session untouched
    # The walk lowered the CLOSED session's evidence.
    assert body["reduced_time_entries"] == [
        {
            "time_entry_id": session1.id,
            "entry_type": "run",
            "quantity_produced_before": 12,
            "quantity_produced_after": 9,
        }
    ]

    db_session.expire_all()
    assert db_session.get(TimeEntry, session1.id).quantity_produced == 9
    assert db_session.get(TimeEntry, session2.id).quantity_produced == 0
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 9
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 9

    # Cross-SESSION reconcile-safety: the WO GET re-derives from evidence; the reduced
    # count must stay reduced (evidence sum is now 9).
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(operator))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    assert get_resp.json()["quantity_complete"] == 9
    reduced_op = next(o for o in get_resp.json()["operations"] if o["id"] == op.id)
    assert reduced_op["quantity_complete"] == 9


def test_reduce_walks_open_first_then_closed_newest_first(client: TestClient, db_session: Session):
    """Walk order: open entry, then closed sessions newest-first; no entry goes below 0."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    older_closed = make_closed_entry(db_session, operator, wo, op, quantity_produced=4, hours_ago=8)
    newer_closed = make_closed_entry(db_session, operator, wo, op, quantity_produced=5, hours_ago=2)
    open_entry = make_open_entry(db_session, operator, wo, op, quantity_produced=2)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 11
    db_session.commit()

    # Remove 8: open 2 -> 0 (takes 2), newest closed 5 -> 0 (takes 5), older 4 -> 3 (takes 1).
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 8, "reason": "systemic double-scan across shifts"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    walked = resp.json()["reduced_time_entries"]
    assert [w["time_entry_id"] for w in walked] == [open_entry.id, newer_closed.id, older_closed.id]

    db_session.expire_all()
    assert db_session.get(TimeEntry, open_entry.id).quantity_produced == 0
    assert db_session.get(TimeEntry, newer_closed.id).quantity_produced == 0
    assert db_session.get(TimeEntry, older_closed.id).quantity_produced == 3
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 3


def test_reduce_allowance_spans_sessions_but_excludes_approved(client: TestClient, db_session: Session):
    """Approved sessions don't count toward the allowance, and the 400 says so."""
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=10, approved_by=supervisor.id)
    make_open_entry(db_session, operator, wo, op, quantity_produced=2)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 12
    db_session.commit()

    # Allowance = 2 (the open unapproved entry); the approved 10 needs a supervisor.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 5, "reason": "too much"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    detail = resp.json()["detail"]
    assert "up to the 2" in detail
    assert "approved labor needs a supervisor" in detail

    # Within the unapproved allowance it succeeds and never touches the approved row.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "just my open miscount"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    approved_row = db_session.query(TimeEntry).filter(TimeEntry.approved.isnot(None)).one()
    assert approved_row.quantity_produced == 10, "approved evidence must never be walked"
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 10


def test_reduce_never_walks_another_operators_closed_evidence(client: TestClient, db_session: Session):
    """Cross-operator x cross-session: A's CLOSED unapproved evidence is invisible to B's walk.

    v2 widened the self-service allowance to span the caller's own SESSIONS -- it must
    not have widened it across OPERATORS. Operator A's closed unapproved entry neither
    counts toward B's allowance (the 400 bound) nor is ever touched by B's valid reduce.
    """
    op_a = make_user(db_session)
    op_b = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    a_closed = make_closed_entry(db_session, op_a, wo, op, quantity_produced=9, hours_ago=3)
    b_open = make_open_entry(db_session, op_b, wo, op, quantity_produced=2)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 11
    wo_row = db_session.get(WorkOrder, wo.id)
    wo_row.quantity_complete = 11
    db_session.commit()

    # B's allowance is their own 2 -- A's closed 9 is unapproved but NOT B's to remove.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 3, "reason": "trying to reach A's evidence"},
        headers=headers_for(op_b),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "up to the 2" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.get(TimeEntry, a_closed.id).quantity_produced == 9

    # Within B's own allowance the walk touches ONLY B's entry.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 2, "reason": "my own miscount"},
        headers=headers_for(op_b),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [w["time_entry_id"] for w in resp.json()["reduced_time_entries"]] == [b_open.id]

    db_session.expire_all()
    assert db_session.get(TimeEntry, a_closed.id).quantity_produced == 9, "A's closed evidence must be untouched"
    assert db_session.get(TimeEntry, b_open.id).quantity_produced == 0
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 9

    # Reconcile-on-read keeps the corrected count (A's surviving 9 is the evidence sum).
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(op_a))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    reduced_op = next(o for o in get_resp.json()["operations"] if o["id"] == op.id)
    assert reduced_op["quantity_complete"] == 9


def test_reduce_audit_slices_match_walk_order_and_stop_at_exhaustion(client: TestClient, db_session: Session):
    """The audit row's extra_data["time_entries"] IS the walk: same order, same amounts.

    Also locks the exact-exhaustion boundary: delta == open + newest-closed consumes
    both exactly and the walk STOPS -- the older closed entry is untouched and gets
    NO zero-take slice (neither in the response nor on the audit trail).
    """
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    older_closed = make_closed_entry(db_session, operator, wo, op, quantity_produced=4, hours_ago=8)
    newer_closed = make_closed_entry(db_session, operator, wo, op, quantity_produced=5, hours_ago=2)
    open_entry = make_open_entry(db_session, operator, wo, op, quantity_produced=2)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 11
    db_session.commit()

    # Remove 7 == open 2 + newest closed 5, both exactly exhausted; older closed untouched.
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 7, "reason": "double-scan across two sessions"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    expected_slices = [
        {
            "time_entry_id": open_entry.id,
            "entry_type": "run",
            "quantity_produced_before": 2,
            "quantity_produced_after": 0,
        },
        {
            "time_entry_id": newer_closed.id,
            "entry_type": "run",
            "quantity_produced_before": 5,
            "quantity_produced_after": 0,
        },
    ]
    assert resp.json()["reduced_time_entries"] == expected_slices

    db_session.expire_all()
    assert db_session.get(TimeEntry, older_closed.id).quantity_produced == 4, "walk must stop at exhaustion"
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 4

    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    assert audit_row.extra_data["time_entries"] == expected_slices, "audit slices must mirror the walk exactly"
    # The summed before/after on the audited diff covers only the WALKED entries (2+5).
    assert audit_row.extra_data["time_entry_quantity_produced_before"] == 7
    assert audit_row.extra_data["time_entry_quantity_produced_after"] == 0


def test_reduce_rework_portioning_across_mixed_entries(client: TestClient, db_session: Session):
    """quantity_reworked drops only by the portion walked off REWORK-typed entries."""
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    rework_closed = make_closed_entry(
        db_session, operator, wo, op, quantity_produced=5, hours_ago=2, entry_type=TimeEntryType.REWORK
    )
    open_run = make_open_entry(db_session, operator, wo, op, quantity_produced=3)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 8
    op_row.quantity_reworked = 5  # the twin credited the REWORK session's 5
    db_session.commit()

    # Remove 6: open RUN 3 -> 0 (rework untouched), then REWORK closed 5 -> 2 (takes 3).
    resp = client.post(
        reduce_url(op),
        json={"quantity_delta": 6, "reason": "mixed-session miscount"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    op_after = db_session.get(WorkOrderOperation, op.id)
    assert op_after.quantity_complete == 2  # 8 - 6
    assert op_after.quantity_reworked == 2  # 5 - the 3 walked off the REWORK entry only
    assert db_session.get(TimeEntry, open_run.id).quantity_produced == 0
    assert db_session.get(TimeEntry, rework_closed.id).quantity_produced == 2


# ===========================================================================
# Real optimistic-lock conflict -> 409 (not 500), and deterministic walk order
# on clock_out ties. Service-level: these exercise the shared core directly.
# ===========================================================================


def test_real_stale_entry_version_conflict_is_409_and_rolls_back(client: TestClient, db_session: Session):
    """A GENUINE concurrent version bump on a walked entry surfaces as 409 with a full rollback.

    Deterministic reproduction of the real interleaving (no monkeypatched commit): the
    eligible entries are SELECTed into the session (caching version=1), then a
    concurrent writer bumps the row's version out-of-band -- a Core UPDATE that
    bypasses the ORM identity map, exactly like another request's committed
    approve/clock-out/report would. The walk then mutates the stale ORM object and the
    service's guarded flush emits ``UPDATE ... WHERE version = 1`` -> 0 rows ->
    StaleDataError -> HTTP 409. Without the pre-emit flush guard this error was
    swallowed inside emit_best_effort and resurfaced as PendingRollbackError (500).
    Rollback must be complete: entry/op/WO quantities unchanged, no audit rows.
    """
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.services.audit_service import AuditService
    from app.services.production_reduction_service import (
        eligible_reduction_entries,
        perform_production_reduction,
    )

    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_open_entry(db_session, operator, wo, op, quantity_produced=6)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 6
    wo_row = db_session.get(WorkOrder, wo.id)
    wo_row.quantity_complete = 6
    db_session.commit()

    # Load the eligible evidence into the session (ORM now holds version=1)...
    entries = eligible_reduction_entries(db_session, operation_id=op.id, company_id=COMPANY_A, user_id=operator.id)
    assert [e.id for e in entries] == [entry.id]
    stale_version = entries[0].version

    # ...then a "concurrent writer" bumps the row's version out-of-band (Core UPDATE --
    # invisible to the identity map, so the ORM object goes stale for real).
    db_session.execute(
        text("UPDATE time_entries SET version = version + 1 WHERE id = :id"),
        {"id": entry.id},
    )

    with pytest.raises(HTTPException) as exc_info:
        perform_production_reduction(
            db_session,
            operation_id=op.id,
            company_id=COMPANY_A,
            actor=operator,
            audit=AuditService(db_session, operator),
            entries=entries,
            delta=2,
            reason="raced by a concurrent approve",
            notes=None,
            recorded_source=None,
            notes_entry=None,
            event_source_module="shop_floor",
            path="shop_floor",
        )
    assert exc_info.value.status_code == 409
    assert "concurrently" in exc_info.value.detail

    # Full rollback: nothing persisted -- quantities unchanged (the out-of-band bump
    # rolled back with the transaction too, back to the committed version).
    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).quantity_produced == 6
    assert db_session.get(TimeEntry, entry.id).version == stale_version
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 6
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 6
    reduce_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "REDUCE_OPERATION_PRODUCTION")
        .filter(AuditLog.resource_id.in_([op.id, entry.id]))
        .all()
    )
    assert reduce_rows == [], "the audit row must roll back with the failed reduction"


def test_eligibility_walk_order_breaks_clock_out_ties_newest_row_first(client: TestClient, db_session: Session):
    """Two closed entries sharing one clock_out (crew-wide auto-close) walk id-descending."""
    from app.services.production_reduction_service import eligible_reduction_entries

    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    shared_clock_out = datetime.utcnow() - timedelta(hours=1)
    first_row = make_closed_entry(db_session, operator, wo, op, quantity_produced=3)
    second_row = make_closed_entry(db_session, operator, wo, op, quantity_produced=4)
    for row_id in (first_row.id, second_row.id):
        db_session.get(TimeEntry, row_id).clock_out = shared_clock_out
    db_session.commit()
    assert second_row.id > first_row.id  # newest row = higher id

    entries = eligible_reduction_entries(db_session, operation_id=op.id, company_id=COMPANY_A, user_id=operator.id)
    assert [e.id for e in entries] == [second_row.id, first_row.id], "tie-break must be newest row (id desc)"
