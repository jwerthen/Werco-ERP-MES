"""Batch 5 / rank 8 -- outbound-dispatch + failure-isolation matrix.

``tests/api/test_completion_matrix.py`` (section 9) already proves the IN-PROCESS
``operation_completed`` / ``work_order_completed`` OperationalEvents fire on every
live op-completion path and on reconcile-on-read. ``tests/services/
test_completion_signals_batch5.py`` proves the tenant-scoped webhook/notification
machinery inside the ARQ task. THIS file closes the remaining matrix gaps at the
ENDPOINT seam -- the boundary between a committed completion and its outbound
signal enqueue:

Point 1 (events) -- the one path the matrix file omits:
  * shipping ``mark_shipped`` emits a tenant-tagged ``work_order_closed`` event.
  * cross-tenant event isolation: a completion in company A writes no event tagged
    company B.

Point 2 (ARQ enqueue, OUTBOUND): each WO-COMPLETE path AND the WO-CLOSED
  ``mark_shipped`` path calls ``enqueue_work_order_completion_signals`` EXACTLY once
  per transition with the right ``company_id`` + ``status``; reconcile-on-read does
  NOT enqueue (in-process events only).

Point 4 (idempotency): a PARTIAL clock-out enqueues nothing and emits no event.

Point 5 (failure isolation): when the enqueue raises, the completion STILL returns
  200 and the op/WO is STILL COMPLETE (best-effort outbound).

Point 6 (read-safety): a reconcile-on-read whose event emit raises STILL returns
  200 and still materializes the op COMPLETE.

Point 7 (scheduling MS-2): ``complete_work_order`` triggers ``update_availability_rates``.

The enqueue is patched AS REFERENCED BY EACH ENDPOINT MODULE (the live handlers
import ``enqueue_work_order_completion_signals`` by name into their own namespace),
so these tests assert the real call the handler makes, not a re-implementation.
Fixtures mirror ``test_completion_matrix.py``: rows in the shared ``db_session``,
directly-minted tokens, the ``client`` fixture overriding ``get_db``.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _seed_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"disp-co-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _seed_company(db, company_id)
    n = _next()
    user = User(
        email=f"disp-{n}@co{company_id}.test",
        employee_id=f"DISP-{n:05d}",
        first_name="Disp",
        last_name=f"C{company_id}",
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
    _seed_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"DISP-P-{n}",
        name=f"Part {n}",
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
    _seed_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"DISP-WC-{n}",
        code=f"DISP-WC-{n}",
        work_center_type="welding",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session, *, status_: WorkOrderStatus, quantity_ordered: float = 10, company_id: int = COMPANY_A
) -> tuple[WorkOrder, Part]:
    part = make_part(db, company_id=company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"DISP-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
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
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def make_open_time_entry(
    db: Session, *, user: User, wo: WorkOrder, op: WorkOrderOperation, wc: WorkCenter, company_id: int = COMPANY_A
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_closed_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter,
    quantity_produced: float,
    company_id: int = COMPANY_A,
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
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_shipment(db: Session, *, wo: WorkOrder, company_id: int = COMPANY_A) -> Shipment:
    n = _next()
    shipment = Shipment(
        shipment_number=f"DISP-SHP-{n:05d}",
        work_order_id=wo.id,
        status=ShipmentStatus.PENDING,
        quantity_shipped=float(wo.quantity_ordered or 0),
        num_packages=1,
        company_id=company_id,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def _reload(db: Session, model, pk: int):
    db.expire_all()
    return db.get(model, pk)


class _EnqueueSpy:
    """Records every ``enqueue_work_order_completion_signals(**kwargs)`` call."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        # The helper is always invoked with keyword args by the live handlers.
        self.calls.append(kwargs)


# ===========================================================================
# Point 2: ARQ enqueue (outbound) -- correct company_id + status, once per path
# ===========================================================================


def test_office_complete_operation_enqueues_signals_on_wo_complete(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """office complete_operation: completing the WO's only op enqueues the
    completion-signal job exactly once with this company + status=COMPLETE."""
    import app.api.endpoints.work_orders as wo_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "COMPLETE"}]


def test_shop_floor_complete_operation_enqueues_signals_on_wo_complete(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """shop_floor complete_operation: the WO-COMPLETE transition enqueues once."""
    import app.api.endpoints.shop_floor as sf_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(sf_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(admin),
        json={"quantity_complete": 10},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "COMPLETE"}]


def test_clock_out_completion_enqueues_signals_on_wo_complete(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """clock_out: a completing clock-out enqueues the signal job once with COMPLETE."""
    import app.api.endpoints.shop_floor as sf_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(sf_module, "enqueue_work_order_completion_signals", spy)

    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 4, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "COMPLETE"}]


def test_complete_work_order_enqueues_signals(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """complete_work_order override enqueues the signal job once with COMPLETE."""
    import app.api.endpoints.work_orders as wo_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "COMPLETE"}]


def test_mark_shipped_enqueues_closed_signal(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """shipping mark_shipped enqueues the signal job once with status=CLOSED
    (the WO-CLOSED transition -- the only path that fires the closed event)."""
    import app.api.endpoints.shipping as shipping_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(shipping_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.COMPLETE, quantity_ordered=5)
    db_session.commit()
    shipment = make_shipment(db_session, wo=wo)

    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "CLOSED"}]
    assert _reload(db_session, WorkOrder, wo.id).status == WorkOrderStatus.CLOSED


def test_reconcile_on_read_does_not_enqueue_outbound_signals(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """EVT-4: a reconcile-on-read that drives a completion emits ONLY in-process
    events -- a GET must never have outbound side-effects, so the signal job is
    NOT enqueued (contrast the live write paths above)."""
    import app.api.endpoints.work_orders as wo_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # The reconcile materialized the op COMPLETE...
    assert _reload(db_session, WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    # ...but enqueued NO outbound signal (in-process events only).
    assert spy.calls == [], "reconcile-on-read must not enqueue outbound completion signals"


# ===========================================================================
# Point 4: idempotency -- a partial clock-out emits nothing AND enqueues nothing
# ===========================================================================


def test_partial_clock_out_enqueues_nothing(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """A clock-out that does NOT finish the op fires no completion transition, so
    it must enqueue no outbound signal (the signal is once-per-transition)."""
    import app.api.endpoints.shop_floor as sf_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(sf_module, "enqueue_work_order_completion_signals", spy)

    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 3, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert spy.calls == [], "a partial (non-completing) clock-out enqueues no signal"


# ===========================================================================
# Point 5: failure isolation -- a FAILING enqueue must not fail the completion
#
# The live handlers call ``enqueue_work_order_completion_signals`` (-> the
# best-effort ``enqueue_job_best_effort``) AFTER the terminal commit and rely on
# its no-raise contract. We exercise the REAL guard by making the underlying Redis
# enqueue blow up (patch ``create_pool`` in ``app.core.queue`` -- the seam the
# best-effort helper opens) and assert the already-committed completion still
# returns 200 with the op/WO COMPLETE. (Patching the helper itself to raise would
# test an impossible state -- the helper is contractually non-raising -- and would
# only prove the handler doesn't double-guard, not that completions survive a real
# Redis outage; this patches the actual outage point.)
# ===========================================================================


def test_office_complete_survives_redis_enqueue_outage(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """With Redis down (``create_pool`` raises), the office completion STILL returns
    200 and the WO is STILL COMPLETE -- the best-effort enqueue swallows the outage."""
    import app.core.queue as queue_module

    monkeypatch.setattr(queue_module, "create_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down")))

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    refreshed_op = _reload(db_session, WorkOrderOperation, op.id)
    refreshed_wo = _reload(db_session, WorkOrder, wo.id)
    assert refreshed_op.status == OperationStatus.COMPLETE, "completion committed despite enqueue outage"
    assert refreshed_wo.status == WorkOrderStatus.COMPLETE


def test_clock_out_survives_redis_enqueue_outage(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """clock_out completion survives a Redis enqueue outage: 200 + op/WO COMPLETE."""
    import app.core.queue as queue_module

    monkeypatch.setattr(queue_module, "create_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down")))

    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 4, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    assert _reload(db_session, WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    assert _reload(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE


def test_mark_shipped_survives_redis_enqueue_outage(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """mark_shipped survives a Redis enqueue outage: 200 + the WO is still CLOSED."""
    import app.core.queue as queue_module

    monkeypatch.setattr(queue_module, "create_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down")))

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.COMPLETE, quantity_ordered=5)
    db_session.commit()
    shipment = make_shipment(db_session, wo=wo)

    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    assert _reload(db_session, WorkOrder, wo.id).status == WorkOrderStatus.CLOSED


# ===========================================================================
# Point 1 (events): shipping mark_shipped emits a tenant-tagged work_order_closed
# ===========================================================================


def _events(db: Session, *, event_type: str, work_order_id: int, company_id: int) -> list[OperationalEvent]:
    db.expire_all()
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == event_type,
            OperationalEvent.work_order_id == work_order_id,
            OperationalEvent.company_id == company_id,
        )
        .all()
    )


def test_mark_shipped_emits_work_order_closed_event(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """EVT-1: mark_shipped emits exactly one ``work_order_closed`` OperationalEvent,
    tenant-tagged to the WO's company, from source_module='shipping'."""
    import app.api.endpoints.shipping as shipping_module

    # Isolate the event assertion from the outbound enqueue (Redis-free test).
    monkeypatch.setattr(shipping_module, "enqueue_work_order_completion_signals", lambda *a, **k: None)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.COMPLETE, quantity_ordered=5)
    db_session.commit()
    shipment = make_shipment(db_session, wo=wo)

    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    closed = _events(db_session, event_type="work_order_closed", work_order_id=wo.id, company_id=COMPANY_A)
    assert len(closed) == 1, "mark_shipped emits exactly one work_order_closed event"
    assert closed[0].source_module == "shipping"
    assert closed[0].company_id == COMPANY_A


def test_completion_events_are_not_tagged_to_another_tenant(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Tenant tagging (invariant #1): a completion in company A writes NO completion
    event tagged company B -- a cross-tenant event row would leak the transition into
    another tenant's operational feed."""
    import app.api.endpoints.work_orders as wo_module

    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", lambda *a, **k: None)

    # Seed company B with its own WO so a stray B-tagged event would be findable.
    _seed_company(db_session, COMPANY_B)
    admin_a = make_user(db_session, company_id=COMPANY_A)
    wo_a, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op_a.id}/complete?quantity_complete=10",
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # Company A got its events...
    assert _events(db_session, event_type="operation_completed", work_order_id=wo_a.id, company_id=COMPANY_A)
    assert _events(db_session, event_type="work_order_completed", work_order_id=wo_a.id, company_id=COMPANY_A)

    # ...and NOTHING was written under company B for this WO.
    db_session.expire_all()
    b_events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_B,
            OperationalEvent.work_order_id == wo_a.id,
        )
        .all()
    )
    assert b_events == [], "no completion event may be tagged to another tenant"


# ===========================================================================
# Point 6: read-safety -- a reconcile whose EVENT emit raises still returns 200
# ===========================================================================


def test_reconcile_on_read_event_emit_failure_still_returns_200(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """A reconcile-on-read whose in-process event emission blows up must STILL return
    200 (a GET may never 500 on a best-effort signal failure) and still materialize
    the op COMPLETE -- the event is the LAST step and is wrapped in try/except.

    We patch ``OperationalEventService`` as referenced by the work_orders endpoint
    module so constructing it (inside ``_emit_reconcile_events``) raises, simulating
    a hard event-subsystem fault on the reconcile path."""
    import app.api.endpoints.work_orders as wo_module

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)

    class _BoomEvents:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("operational-event subsystem down")

    monkeypatch.setattr(wo_module, "OperationalEventService", _BoomEvents)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["id"] == wo.id

    # The reconcile (which precedes the event emit) still drove + committed the op
    # COMPLETE; only the in-process event was lost.
    db_session.rollback()
    assert _reload(db_session, WorkOrderOperation, op.id).status == OperationStatus.COMPLETE


# ===========================================================================
# Point 7: scheduling MS-2 -- complete_work_order refreshes availability rates
# ===========================================================================


class _SchedulingSpy:
    """Stands in for SchedulingService; records update_availability_rates calls."""

    calls: list[dict] = []

    def __init__(self, db, company_id):
        self.company_id = company_id

    def update_availability_rates(self, *, work_center_ids, horizon_days=90, commit=True):
        _SchedulingSpy.calls.append(
            {"company_id": self.company_id, "work_center_ids": list(work_center_ids), "commit": commit}
        )


def test_complete_work_order_refreshes_availability_rates(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """MS-2: the complete_work_order override force-completes its open operations and
    must refresh cached work-center availability (``update_availability_rates``) for
    the affected work centers -- a completed WO drops out of the scheduled load, so a
    stale availability_rate would otherwise understate free capacity."""
    import app.api.endpoints.work_orders as wo_module

    _SchedulingSpy.calls = []
    monkeypatch.setattr(wo_module, "SchedulingService", _SchedulingSpy)
    # Keep the outbound enqueue inert so this test isolates the scheduling refresh.
    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", lambda *a, **k: None)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    assert _SchedulingSpy.calls, "complete_work_order must refresh availability rates (MS-2)"
    call = _SchedulingSpy.calls[0]
    assert call["company_id"] == COMPANY_A, "scheduling refresh is tenant-scoped to the active company"
    assert wc.id in call["work_center_ids"], "the affected work center is refreshed"


# ===========================================================================
# Idempotency (e2 / e3): a re-submitted terminal transition fires the
# close/audit/event/enqueue block EXACTLY ONCE.
# ===========================================================================


def _wo_status_change_audits(db: Session, *, wo_id: int) -> list[AuditLog]:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == wo_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def test_mark_shipped_is_idempotent(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """e2: calling mark_shipped twice must close/audit/emit/enqueue ONLY ONCE. The
    second call sees an already-SHIPPED shipment / already-CLOSED WO and returns a
    clean no-op -- it must not re-flip the WO, write a second CLOSED->CLOSED audit row
    on the tamper-evident chain, re-emit work_order_closed, or re-enqueue the signal."""
    import app.api.endpoints.shipping as shipping_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(shipping_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.COMPLETE, quantity_ordered=5)
    db_session.commit()
    shipment = make_shipment(db_session, wo=wo)

    first = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(admin))
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(admin))
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json().get("already_shipped") is True, "second ship is a recognized idempotent no-op"

    # The WO closed exactly once.
    assert _reload(db_session, WorkOrder, wo.id).status == WorkOrderStatus.CLOSED
    # ENQUEUE fired exactly once.
    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "CLOSED"}]
    # EVENT emitted exactly once.
    closed = _events(db_session, event_type="work_order_closed", work_order_id=wo.id, company_id=COMPANY_A)
    assert len(closed) == 1, "work_order_closed emitted exactly once across two ship calls"
    # AUDIT status-change row written exactly once.
    audits = _wo_status_change_audits(db_session, wo_id=wo.id)
    assert len(audits) == 1, "exactly one work_order status-change audit row across two ship calls"


def test_complete_work_order_is_idempotent_on_terminal_wo(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """e3: re-invoking complete_work_order on an already-COMPLETE WO is a clean no-op
    -- it must NOT re-fire work_order_completed, re-enqueue the outbound signal, or
    write another COMPLETE status-change audit row."""
    import app.api.endpoints.work_orders as wo_module

    spy = _EnqueueSpy()
    monkeypatch.setattr(wo_module, "enqueue_work_order_completion_signals", spy)

    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    first = client.post(f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10", headers=headers_for(admin))
    assert first.status_code == status.HTTP_200_OK, first.text
    assert _reload(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE

    # Re-invoke on the now-terminal WO.
    second = client.post(f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10", headers=headers_for(admin))
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json().get("already_completed") is True, "second complete is a recognized idempotent no-op"

    # ENQUEUE fired exactly once (first call only).
    assert spy.calls == [{"work_order_id": wo.id, "company_id": COMPANY_A, "status": "COMPLETE"}]
    # EVENT emitted exactly once.
    completed = _events(db_session, event_type="work_order_completed", work_order_id=wo.id, company_id=COMPANY_A)
    assert len(completed) == 1, "work_order_completed emitted exactly once across two complete calls"
    # AUDIT status-change to COMPLETE written exactly once.
    audits = _wo_status_change_audits(db_session, wo_id=wo.id)
    assert len(audits) == 1, "exactly one work_order status-change audit row across two complete calls"
