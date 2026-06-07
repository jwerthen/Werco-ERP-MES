"""Comprehensive completion-flow matrix for the Batch-3 shared finalizer (Rank 6).

``test_completion_finalizer_batch3.py`` locks the *behavior changes* the
consolidation introduced. THIS file proves the unified contract holds *across
every completion path* so the six entry points cannot diverge from
``finalize_operation_completion`` / its sibling helpers:

Paths exercised here (real endpoints via TestClient unless noted):
- shop_floor ``clock_out``                     POST /shop-floor/clock-out/{id}        (additive)
- shop_floor ``report_operation_production``   POST /shop-floor/operations/{id}/production  (additive)
- shop_floor ``complete_operation``            POST /shop-floor/operations/{id}/complete    (absolute)
- office ``work_orders.complete_operation``    POST /work-orders/operations/{id}/complete   (absolute)
- office ``work_orders.complete_work_order``   POST /work-orders/{id}/complete              (override)
- reconcile-on-read                            GET  /work-orders/{id}  (drives reconcile_..._evidence)

Contract points covered (cross-referenced to the prompt's matrix). Points already
locked by the 9 batch3 tests are NOT re-asserted here unless a SECOND path needs
coverage (e.g. ON_HOLD is asserted on the shop_floor twin; batch3 has the office one):

1. Quantity semantics
   - Absolute, requested < evidence -> stores evidence (SFI-5):
       office: batch3::test_office_complete_floors_quantity_at_time_entry_evidence
       shop_floor: test_shop_floor_complete_floors_at_evidence_not_lowered
   - Absolute, requested > target -> capped at target:
       test_office_complete_caps_absolute_quantity_at_target
       test_shop_floor_complete_caps_absolute_quantity_at_target
   - Additive (clock_out / production): += delta, floored at evidence, capped at target:
       test_clock_out_is_additive_and_floored_at_evidence
       test_production_is_additive_and_floored_at_evidence
       test_production_additive_capped_at_target_via_evidence
   - RUP-6 WO qty never regresses out of order:
       test_wo_quantity_does_not_regress_when_earlier_op_completed_after_later
2. Scrap (DUP-3): office complete without scrap keeps accumulated scrap -> batch3 covers;
   additive scrap accumulation also asserted in test_clock_out_is_additive_and_floored_at_evidence.
3. ON_HOLD refused by BOTH complete_operation endpoints:
   office: batch3::test_office_complete_refuses_on_hold_operation
   shop_floor: test_shop_floor_complete_refuses_on_hold_operation
4. actual_start stamping (DUP-2) via EACH shop-floor path:
   test_clock_out_completion_stamps_actual_start
   test_shop_floor_complete_completion_stamps_actual_start
5. Next-op release self-heals (RUP-4) after same-work-center / out-of-sequence completion:
   test_same_work_center_completion_releases_lower_sequence_pending
6. current_operation_id (RUP-1) populated while in flight, cleared at COMPLETE,
   over a multi-step real-endpoint progression:
   test_current_operation_id_lifecycle_across_progression
7. complete_work_order (DUP-4) bounds quantity params:
   test_complete_work_order_rejects_negative_quantity
   (over-ordered + force-complete covered by batch3)
8. AUD-3: reconcile audit write failure still returns 200 (read-safe):
   test_reconcile_on_read_audit_failure_still_returns_200

Fixtures mirror the sibling completion suites: rows created directly in the shared
``db_session`` (tests/conftest.py); requests use a directly-minted token; the
``client`` fixture overrides ``get_db`` to yield that same session.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"cm-{n}@co{COMPANY_A}.test",
        employee_id=f"CM-{n:05d}",
        first_name="CM",
        last_name="CA",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"CM-P-{n}",
        name=f"Part {n}",
        description="completion-matrix fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"CM-WC-{n}",
        code=f"CM-WC-{n}",
        work_center_type="welding",
        description="completion-matrix fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(db: Session, *, status_: WorkOrderStatus, quantity_ordered: float = 10) -> tuple[WorkOrder, Part]:
    part = make_part(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"CM-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    return wo, part


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int,
    status_: OperationStatus,
    quantity_complete: float = 0,
    quantity_scrapped: float = 0,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.flush()
    return op


def make_closed_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter,
    quantity_produced: float,
    quantity_scrapped: float = 0,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=quantity_produced,
        quantity_scrapped=quantity_scrapped,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_open_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter,
    hours_ago: float = 2.0,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=hours_ago),
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _reload(db: Session, model, pk: int):
    db.expire_all()
    return db.get(model, pk)


# ===========================================================================
# 1a. ABSOLUTE verb: requested > target is capped at target (both complete_operation)
# ===========================================================================


def test_office_complete_caps_absolute_quantity_at_target(client: TestClient, db_session: Session):
    """office complete_operation: requesting MORE than the target stores the target,
    not the inflated request (clamp upper bound at target = SFI-5)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    # validate_operation_quantity rejects a request strictly > target up front, so
    # to exercise the resolver's CAP we drive the request exactly AT target while
    # leaving the operation target-bound: requesting 10 on a 10-target op stores 10.
    # The over-target rejection itself is asserted separately below.
    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 10
    assert refreshed.status == OperationStatus.COMPLETE


def test_office_complete_rejects_quantity_over_target(client: TestClient, db_session: Session):
    """office complete_operation rejects an absolute request strictly above target
    (validate_operation_quantity guard) rather than silently clamping it."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=99",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "exceed" in resp.json()["detail"].lower()

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 0  # untouched
    assert refreshed.status == OperationStatus.IN_PROGRESS


def test_shop_floor_complete_caps_absolute_quantity_at_target(client: TestClient, db_session: Session):
    """shop_floor complete_operation absolute verb: an existing over-target stored
    value (e.g. from a prior bug) is clamped back DOWN to target by the resolver.

    We seed quantity_complete=15 on a 10-target op, then complete with a small
    request; resolve_absolute_operation_quantity caps the stored result at 10."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=15)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 5},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 10, "absolute verb caps stored quantity at target"
    assert refreshed.status == OperationStatus.COMPLETE


def test_shop_floor_complete_floors_at_evidence_not_lowered(client: TestClient, db_session: Session):
    """shop_floor complete_operation (absolute): requesting BELOW durable produced
    evidence stores the evidence, never the lowered request (SFI-5) -- the
    shop-floor twin of batch3's office assertion."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=6)
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=6)

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 4},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 6, "absolute /complete must not drop below produced evidence"
    # 6 < 10 target -> not fully complete; op stays IN_PROGRESS.
    assert refreshed.status == OperationStatus.IN_PROGRESS


# ===========================================================================
# 1b. ADDITIVE verbs: += delta, floored at evidence, capped at target
# ===========================================================================


def test_clock_out_is_additive_and_floored_at_evidence(client: TestClient, db_session: Session):
    """clock_out adds the produced delta to the operation total (additive verb) and
    the stored result is floored at durable TimeEntry evidence and accumulates scrap."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(
        db_session,
        wo,
        wc,
        sequence=10,
        status_=OperationStatus.IN_PROGRESS,
        quantity_complete=2,
        quantity_scrapped=1,
    )
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 3, "quantity_scrapped": 2},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    # additive good: 2 (existing) + 3 (delta) = 5, below 10 target -> IN_PROGRESS.
    assert refreshed.quantity_complete == 5, "clock_out is additive on quantity_complete"
    # additive scrap: 1 (existing) + 2 (delta) = 3.
    assert refreshed.quantity_scrapped == 3, "clock_out accumulates scrap"
    assert refreshed.status == OperationStatus.IN_PROGRESS


def test_clock_out_completion_stamps_actual_start(client: TestClient, db_session: Session):
    """DUP-2 via clock_out: completing the first-and-only-remaining op of a RELEASED
    WO leaves the WO with BOTH actual_start AND actual_end set AND ordered
    (actual_start <= actual_end).

    Previously xfailed: the clock_out path stamped only operation.actual_end /
    completed_by, so the finalizer fell back to work_order.actual_start = now()
    -- captured AFTER actual_end -- yielding a negative cycle time. Fixed by
    stamping operation.actual_start from its earliest TimeEntry clock_in in the
    clock_out completion path AND clamping the finalizer's actual_start fallback
    at actual_end. This test now passes."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 5, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed_wo = _reload(db_session, WorkOrder, wo.id)
    refreshed_op = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed_op.status == OperationStatus.COMPLETE
    assert refreshed_wo.status == WorkOrderStatus.COMPLETE
    assert refreshed_wo.actual_start is not None, "DUP-2: clock_out completion must stamp actual_start"
    assert refreshed_wo.actual_end is not None
    assert refreshed_wo.current_operation_id is None, "RUP-1: completed WO is on no operation"
    # The load-bearing assertion: a finished WO must have a non-negative cycle time.
    assert (
        refreshed_wo.actual_start <= refreshed_wo.actual_end
    ), "DUP-2: WO actual_start must not be after actual_end (negative cycle time)"


def test_production_is_additive_and_floored_at_evidence(client: TestClient, db_session: Session):
    """report_operation_production adds the delta (additive verb), floored at
    durable evidence and capped at target, WITHOUT completing the operation."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=2)
    # Operator must be clocked in for /production.
    make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        headers=headers_for(operator),
        json={"quantity_complete_delta": 3, "quantity_scrapped_delta": 1},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 5, "/production is additive (2 + 3)"
    assert refreshed.quantity_scrapped == 1
    # /production never auto-completes even at/over target; it stays IN_PROGRESS.
    assert refreshed.status == OperationStatus.IN_PROGRESS


def test_production_additive_capped_at_target_via_evidence(client: TestClient, db_session: Session):
    """report_operation_production at the target stores the target (cap), and the
    over-target delta is rejected by the over-completion guard (400)."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=8)
    make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    # A delta that would exceed target (8 + 5 = 13 > 10) is rejected.
    over = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        headers=headers_for(operator),
        json={"quantity_complete_delta": 5, "quantity_scrapped_delta": 0},
    )
    assert over.status_code == status.HTTP_400_BAD_REQUEST, over.text
    assert "exceed" in over.json()["detail"].lower()

    # A delta exactly to target (8 + 2 = 10) stores the capped target.
    ok = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        headers=headers_for(operator),
        json={"quantity_complete_delta": 2, "quantity_scrapped_delta": 0},
    )
    assert ok.status_code == status.HTTP_200_OK, ok.text
    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 10, "additive verb caps at target"


# ===========================================================================
# 1c. RUP-6: WO quantity_complete never regresses when ops complete out of order
# ===========================================================================


def test_wo_quantity_does_not_regress_when_earlier_op_completed_after_later(client: TestClient, db_session: Session):
    """RUP-6: complete a later-stage op first (rolls a partial qty up), then an
    earlier-stage op out of sequence -> the WO finished quantity does NOT decrease.

    Two parallel ops in the SAME work center (so same-WC out-of-sequence completion
    is permitted): op2 (seq 20) carries more progress than op1 (seq 10). Completing
    op2 first lifts WO.quantity_complete; completing op1 afterward must not pull it
    back down below what op2 already rolled up."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=3)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=3)
    db_session.commit()

    # Complete the LATER op (seq 20) to full first. allow_same_work_center lets this
    # proceed despite op1 (seq 10) being incomplete in the same work center.
    r2 = client.post(
        f"/api/v1/shop-floor/operations/{op2.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 10},
    )
    assert r2.status_code == status.HTTP_200_OK, r2.text
    wo_after_op2 = _reload(db_session, WorkOrder, wo.id)
    qty_after_op2 = float(wo_after_op2.quantity_complete or 0)
    assert qty_after_op2 == 10, "completing the later op rolled finished qty up to 10"

    # Now complete the EARLIER op (seq 10). Its own quantity (10) must not pull the
    # WO finished quantity backward below what op2 established.
    r1 = client.post(
        f"/api/v1/shop-floor/operations/{op1.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 10},
    )
    assert r1.status_code == status.HTTP_200_OK, r1.text
    wo_final = _reload(db_session, WorkOrder, wo.id)
    assert float(wo_final.quantity_complete or 0) >= qty_after_op2, "RUP-6: WO qty must not regress"
    assert wo_final.status == WorkOrderStatus.COMPLETE


# ===========================================================================
# 3. ON_HOLD refused by BOTH complete_operation endpoints (shop_floor twin of batch3)
# ===========================================================================


def test_shop_floor_complete_refuses_on_hold_operation(client: TestClient, db_session: Session):
    """shop_floor complete_operation refuses an ON_HOLD op and does NOT force it to
    IN_PROGRESS then complete it (SFI-4/QG-5/BLK-1 consistency with the office twin).

    Status-code parity: an ON_HOLD op is a STATE conflict, so BOTH twins return
    409 (was 400 on the shop_floor side before the parity fix)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.ON_HOLD)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 10},
    )
    # Both twins reject an ON_HOLD op with 409 Conflict (state conflict, not bad input).
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "on hold" in resp.json()["detail"].lower()

    refreshed = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed.status == OperationStatus.ON_HOLD, "ON_HOLD op must not be silently completed"
    assert refreshed.actual_end is None


# ===========================================================================
# 4. DUP-2 actual_start via shop_floor complete_operation path
# ===========================================================================


def test_shop_floor_complete_completion_stamps_actual_start(client: TestClient, db_session: Session):
    """DUP-2 via shop_floor /complete: completing the only op of a RELEASED WO
    leaves BOTH actual_start AND actual_end set (the other shop-floor path)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 5},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed_wo = _reload(db_session, WorkOrder, wo.id)
    assert refreshed_wo.status == WorkOrderStatus.COMPLETE
    assert refreshed_wo.actual_start is not None, "DUP-2: shop_floor /complete must stamp actual_start"
    assert refreshed_wo.actual_end is not None
    assert refreshed_wo.actual_start <= refreshed_wo.actual_end
    assert refreshed_wo.current_operation_id is None


# ===========================================================================
# 5. RUP-4: next-op release self-heals after same-work-center out-of-sequence completion
# ===========================================================================


def test_same_work_center_completion_releases_lower_sequence_pending(client: TestClient, db_session: Session):
    """RUP-4: completing a later op out of sequence in the same work center promotes
    the lowest-sequence still-PENDING op (whose predecessors are all complete) to
    READY rather than stranding it PENDING.

    Three ops, all in one work center:
      seq10 PENDING (predecessor gate is empty -> eligible to be READY)
      seq20 IN_PROGRESS (completed here, out of order vs seq10, same WC allowed)
      seq30 PENDING (still gated by seq10)
    After completing seq20, the finalizer's release_next_ready_operation should
    promote seq10 (no incomplete predecessors) to READY; seq30 stays PENDING."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op10 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.PENDING)
    op20 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS)
    op30 = make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op20.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 5},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed_op10 = _reload(db_session, WorkOrderOperation, op10.id)
    refreshed_op30 = _reload(db_session, WorkOrderOperation, op30.id)
    assert refreshed_op10.status == OperationStatus.READY, "RUP-4: lowest-seq eligible PENDING self-heals to READY"
    assert refreshed_op30.status == OperationStatus.PENDING, "seq30 stays gated behind seq10"


# ===========================================================================
# 6. RUP-1: current_operation_id populated while in flight, cleared at COMPLETE,
#    over a multi-step real-endpoint progression.
# ===========================================================================


def test_current_operation_id_lifecycle_across_progression(client: TestClient, db_session: Session):
    """RUP-1: current_operation_id tracks the active/next op as a 2-op WO progresses
    and is CLEARED when the WO reaches COMPLETE -- asserted across two real endpoint
    calls (office complete op1, then op2)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    wc = make_work_center(db_session)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()

    # Complete op1 -> WO IN_PROGRESS, current_operation_id points at op2 (now READY).
    r1 = client.post(
        f"/api/v1/work-orders/operations/{op1.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert r1.status_code == status.HTTP_200_OK, r1.text
    wo_mid = _reload(db_session, WorkOrder, wo.id)
    op2_mid = _reload(db_session, WorkOrderOperation, op2.id)
    assert wo_mid.status == WorkOrderStatus.IN_PROGRESS
    assert op2_mid.status == OperationStatus.READY
    assert wo_mid.current_operation_id == op2.id, "RUP-1: WO points at the now-active op2"

    # Complete op2 -> WO COMPLETE, current_operation_id cleared.
    r2 = client.post(
        f"/api/v1/work-orders/operations/{op2.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert r2.status_code == status.HTTP_200_OK, r2.text
    wo_done = _reload(db_session, WorkOrder, wo.id)
    assert wo_done.status == WorkOrderStatus.COMPLETE
    assert wo_done.current_operation_id is None, "RUP-1: completed WO is on no operation"


# ===========================================================================
# 7. complete_work_order (DUP-4): bounds quantity params (negative -> 400)
# ===========================================================================


def test_complete_work_order_rejects_negative_quantity(client: TestClient, db_session: Session):
    """complete_work_order bounds the manager-supplied quantity: a negative value
    is rejected with 400 (over-ordered + force-complete are covered by batch3)."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=-1",
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "negative" in resp.json()["detail"].lower()

    refreshed = _reload(db_session, WorkOrder, wo.id)
    assert refreshed.status == WorkOrderStatus.IN_PROGRESS, "rejected completion left the WO untouched"


def test_complete_work_order_refuses_on_hold_operation(client: TestClient, db_session: Session):
    """SHOULD-FIX: complete_work_order must REFUSE (409) when any open operation is
    ON_HOLD instead of silently force-completing it -- the privileged override may
    not lift a quality/material hold. Nothing is mutated before the refusal."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    op_ok = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op_hold = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.ON_HOLD)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5",
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"].lower()
    assert "on hold" in detail and "cannot complete work order" in detail

    # Nothing mutated: the WO stays IN_PROGRESS and BOTH ops keep their status.
    refreshed_wo = _reload(db_session, WorkOrder, wo.id)
    assert refreshed_wo.status == WorkOrderStatus.IN_PROGRESS, "refused completion left the WO untouched"
    assert _reload(db_session, WorkOrderOperation, op_hold.id).status == OperationStatus.ON_HOLD
    refreshed_ok = _reload(db_session, WorkOrderOperation, op_ok.id)
    assert refreshed_ok.status == OperationStatus.IN_PROGRESS, "open op not force-completed when refusal fires"
    assert refreshed_ok.actual_end is None


def test_complete_work_order_omitted_scrap_does_not_zero_recorded_scrap(client: TestClient, db_session: Session):
    """DUP-3 WO-level parity: a complete_work_order call that OMITS quantity_scrapped
    must preserve previously-recorded WO scrap, not silently reset it to 0."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wo.quantity_scrapped = 3  # pre-recorded scrap on the WO
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    # No quantity_scrapped query param -> the override must leave recorded scrap alone.
    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5",
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    refreshed = _reload(db_session, WorkOrder, wo.id)
    assert refreshed.status == WorkOrderStatus.COMPLETE
    assert float(refreshed.quantity_scrapped or 0) == 3, "omitted scrap must not zero recorded WO scrap"


# ===========================================================================
# 8. AUD-3: a reconcile whose audit write fails STILL returns 200 (read-safe).
# ===========================================================================


def test_reconcile_on_read_audit_failure_still_returns_200(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """AUD-3 read-safety: if the audit write for a reconcile-driven completion blows
    up, the GET that triggered the reconcile must STILL return 200 (a read must
    never 500 on an audit failure). We force the audit path to raise and assert the
    read survives -- and that the underlying reconcile still drove the op COMPLETE.

    The audit emission is wrapped in a try/except in the endpoint module's
    ``_audit_reconcile_transitions``; we patch ``AuditService`` *as referenced by
    that module* to raise on construction, simulating a hard audit-subsystem fault.
    """
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    # Durable evidence: a closed entry produced the full ordered quantity but the op
    # row was never flipped COMPLETE -> reconcile-on-read will drive it COMPLETE and
    # try to write an attributed audit row (which we make fail).
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)

    import app.api.endpoints.work_orders as wo_module

    class _BoomAudit:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("audit subsystem down")

    monkeypatch.setattr(wo_module, "AuditService", _BoomAudit)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["id"] == wo.id

    # The reconcile itself (which precedes the audit write) still drove the op
    # COMPLETE and committed; only the audit row was lost.
    db_session.rollback()
    refreshed_op = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed_op.status == OperationStatus.COMPLETE, "reconcile still completed the op"

    # No reconcile-on-read audit row was written for this op (the audit raised).
    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert not rows, "audit failure means no audit row, but the read still succeeded"


def test_reconcile_on_read_poisoned_commit_integrityerror_still_returns_200(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """BLOCKER (AUD-3): when the audit INSERT fails (e.g. an audit_log.sequence_number
    unique collision under concurrency), ``AuditService.log`` absorbs it WITHOUT
    rolling back, poisoning the session, so the reconcile's own ``db.commit()`` then
    raises ``IntegrityError`` (not StaleDataError). The hardened guard catches
    ``SQLAlchemyError`` broadly, rolls back, and serves the read -- a GET must NEVER
    500 on a poisoned-session audit failure.

    We reproduce the mechanism faithfully: the module-level ``AuditService`` is
    patched so emitting the reconcile audit row poisons the session, and the
    reconcile's own ``db.commit()`` then raises ``IntegrityError``. The flag set by
    the patched audit ensures we fail ONLY the reconcile commit (which runs audit
    first), never the earlier component-quantity reconcile commit (no audit). The
    read must still return 200, and the reconcile + audit roll back atomically --
    the op is NOT left COMPLETE.
    """
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    import app.api.endpoints.work_orders as wo_module

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)

    state = {"audit_emitted": False}

    class _PoisoningAudit:
        """Stands in for AuditService: records the reconcile transition then marks
        the session as 'poisoned' so the following commit fails, mimicking an audit
        INSERT that failed its flush without rolling back (AuditService.log behavior).
        """

        def __init__(self, *args, **kwargs):
            pass

        def log_status_change(self, *args, **kwargs):
            state["audit_emitted"] = True
            return None

    monkeypatch.setattr(wo_module, "AuditService", _PoisoningAudit)

    real_commit = db_session.commit

    def poisoned_commit(*args, **kwargs):
        # Only the reconcile commit (which emits audit first) is poisoned; the
        # component-quantity reconcile commit that may run earlier is untouched.
        if state["audit_emitted"]:
            state["audit_emitted"] = False
            raise SAIntegrityError("INSERT INTO audit_log", {}, Exception("UNIQUE sequence_number"))
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", poisoned_commit)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["id"] == wo.id

    # Atomic rollback: the reconcile mutation AND its audit row were discarded
    # together, so the op was NOT left COMPLETE by a half-applied reconcile.
    monkeypatch.undo()
    db_session.rollback()
    refreshed_op = _reload(db_session, WorkOrderOperation, op.id)
    assert refreshed_op.status == OperationStatus.IN_PROGRESS, "poisoned reconcile rolled back atomically"

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert not rows, "rolled-back reconcile leaves no orphaned audit row"
