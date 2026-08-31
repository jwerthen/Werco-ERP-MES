"""WHY an operation is held reaches the OFFICE work-order page, not just the kiosk.

A shop owner put a hold on a laser nest and could not find out why, who did it, or
when: ``GET /work-orders/{id}`` served the operation's ``status: "on_hold"`` and
nothing else. ``services/operation_hold_view.py`` already assembled the provenance for
the kiosk queue; these tests pin it onto the work-order response as ``hold_context``.

What is pinned here:

* PROVENANCE, both shapes. A hold that filed a ``WorkOrderBlocker`` renders the reason;
  a BARE hold (no note, category OTHER -- the fat-finger accident this feature exists
  for) files no blocker and renders who/when from the ``operation_hold`` event with
  ``blocker: null``. Reason and attribution are INDEPENDENT and neither may be gated on
  the other.
* ONLY ON_HOLD ROWS carry it -- every other operation serializes ``hold_context: null``.
* FREE TEXT RIDES THIS RESPONSE on purpose. ``shop_floor._hold_blocker_payload`` withholds
  ``title``/``note`` from a crew-station principal (an unattended, PIN-unlocked tablet);
  the office page is an identified session behind ``get_current_user`` -- the audience
  that gate already exempts -- so the reason is served and ``free_text_withheld`` is
  ``False``. A test that starts asserting the text is absent here has copied the station
  rule to the one screen built to act on the hold.
* TENANT ISOLATION -- a blocker owned by another company can never explain this company's
  hold, even when its ``operation_id`` points at this company's row.
* PURE READ -- the render writes nothing: no audit row, no operational event.
* NO QUERY ON THE COMMON PATH -- a work order with nothing held does not call the view at
  all, so the office read costs exactly what it did before.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
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
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 941
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.query(Company).filter(Company.id == company_id).first():
        return
    db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"co-{company_id}", is_active=True))
    db.commit()


def make_user(
    db: Session,
    *,
    role: UserRole = UserRole.ADMIN,
    company_id: int = COMPANY_A,
    first_name: str = "Dana",
    last_name: str = "Reyes",
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"hold-{n}@co{company_id}.test",
        employee_id=f"HOLD-{n:05d}",
        first_name=first_name,
        last_name=last_name,
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


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"HOLD-WC-{n}",
        code=f"HOLD-WC-{n}",
        work_center_type="laser",
        description="hold-context fixture work center",
        hourly_rate=100,
        capacity_hours_per_day=8.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(db: Session, *, work_center: WorkCenter, statuses: list, company_id: int = COMPANY_A) -> tuple:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"HOLD-P-{n}",
        name="Nested bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"HOLD-WO-{n:05d}",
        part_id=part.id,
        work_order_type=WorkOrderType.PRODUCTION.value,
        sequential_operations=False,
        customer_name="Acme",
        quantity_ordered=len(statuses),
        status=WorkOrderStatus.RELEASED,
        priority=3,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    ops = []
    for index, op_status in enumerate(statuses, start=1):
        op = WorkOrderOperation(
            company_id=company_id,
            work_order_id=wo.id,
            work_center_id=work_center.id,
            sequence=index * 10,
            operation_number=str(index * 10),
            name=f"Nest {index}",
            component_quantity=1.0,
            status=op_status,
        )
        db.add(op)
        ops.append(op)
    db.commit()
    for op in ops:
        db.refresh(op)
    db.refresh(wo)
    return wo, ops


def make_blocker(
    db: Session,
    *,
    operation: WorkOrderOperation,
    reported_by: int,
    company_id: int = COMPANY_A,
    blocker_status: str = WorkOrderBlockerStatus.OPEN.value,
    title: str = "Machine Down: Nest 1",
    note: str = "Spindle bearing noise, called service",
    reported_at: datetime = None,
) -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        category=WorkOrderBlockerCategory.MACHINE_DOWN.value,
        severity=WorkOrderBlockerSeverity.HIGH.value,
        status=blocker_status,
        title=title,
        note=note,
        reported_by=reported_by,
        reported_at=reported_at or datetime.utcnow(),
    )
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def make_hold_event(
    db: Session,
    *,
    operation: WorkOrderOperation,
    user_id: int,
    company_id: int = COMPANY_A,
    occurred_at: datetime = None,
) -> OperationalEvent:
    """The record a BARE hold leaves (no note, category OTHER) -- the accident case."""
    event = OperationalEvent(
        company_id=company_id,
        event_type="operation_hold",
        source_module="shop_floor",
        entity_type="work_order_operation",
        entity_id=operation.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        user_id=user_id,
        severity="medium",
        occurred_at=occurred_at or datetime.utcnow(),
        event_payload={},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_work_order(client: TestClient, user: User, work_order_id: int) -> dict:
    response = client.get(f"/api/v1/work-orders/{work_order_id}", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def operation_by_id(payload: dict, operation_id: int) -> dict:
    match = next((op for op in payload["operations"] if op["id"] == operation_id), None)
    assert match is not None, f"operation {operation_id} missing from response"
    return match


class TestHoldProvenanceOnTheWorkOrderPage:
    def test_blocker_backed_hold_serves_the_reason_and_the_attribution(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD, OperationStatus.READY])
        make_blocker(db_session, operation=ops[0], reported_by=user.id)

        held = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)

        context = held["hold_context"]
        assert context is not None
        assert context["held_by_user_id"] == user.id
        assert context["held_by_name"] == "Dana R."
        assert context["held_at"] is not None and context["held_at"].endswith("Z")

        blocker = context["blocker"]
        assert blocker["category"] == WorkOrderBlockerCategory.MACHINE_DOWN.value
        assert blocker["severity"] == WorkOrderBlockerSeverity.HIGH.value
        assert blocker["status"] == WorkOrderBlockerStatus.OPEN.value
        assert blocker["reported_by_name"] == "Dana R."
        assert blocker["reported_at"].endswith("Z")
        # The office page is an identified session: the reason text is SERVED, not
        # withheld. Flipping this assertion copies the crew-station gate onto the one
        # screen built to act on the hold -- see _hold_blocker_payload.
        assert blocker["title"] == "Machine Down: Nest 1"
        assert blocker["note"] == "Spindle bearing noise, called service"
        assert blocker["free_text_withheld"] is False

    def test_bare_hold_serves_who_and_when_with_no_blocker(self, client: TestClient, db_session: Session):
        """The fat-finger accident: an event, no blocker. Attribution must not vanish."""
        user = make_user(db_session, first_name="Sam", last_name="Ortiz")
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD])
        make_hold_event(db_session, operation=ops[0], user_id=user.id)

        context = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)["hold_context"]

        assert context["blocker"] is None
        assert context["held_by_user_id"] == user.id
        assert context["held_by_name"] == "Sam O."
        assert context["held_at"] is not None

    def test_hold_with_no_record_at_all_renders_as_all_null(self, client: TestClient, db_session: Session):
        """ "Held by unknown, reason not recorded" is a REAL state, not an error."""
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD])

        context = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)["hold_context"]

        assert context == {"held_at": None, "held_by_user_id": None, "held_by_name": None, "blocker": None}

    def test_resolved_blocker_is_not_the_current_reason(self, client: TestClient, db_session: Session):
        """A closed blocker on a still-held op is stale narrative -- excluded server-side."""
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD])
        make_blocker(
            db_session,
            operation=ops[0],
            reported_by=user.id,
            blocker_status=WorkOrderBlockerStatus.RESOLVED.value,
        )

        context = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)["hold_context"]

        assert context["blocker"] is None

    def test_only_held_operations_carry_a_context(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.ON_HOLD, OperationStatus.READY, OperationStatus.COMPLETE],
        )
        # A blocker on a NOT-held operation must not leak onto its row: the office page
        # renders this block as "why the job is stopped".
        make_blocker(db_session, operation=ops[1], reported_by=user.id)

        payload = get_work_order(client, user, wo.id)

        assert operation_by_id(payload, ops[0].id)["hold_context"] is not None
        assert operation_by_id(payload, ops[1].id)["hold_context"] is None
        assert operation_by_id(payload, ops[2].id)["hold_context"] is None


class TestTenantIsolation:
    def test_another_companys_blocker_never_explains_this_hold(self, client: TestClient, db_session: Session):
        user = make_user(db_session)
        other_user = make_user(db_session, company_id=COMPANY_B, first_name="Mal", last_name="Ory")
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD])
        # Same operation id, another company's row: the view is scoped to the ACTIVE
        # company, so this can only ever come back empty.
        make_blocker(
            db_session,
            operation=ops[0],
            reported_by=other_user.id,
            company_id=COMPANY_B,
            title="Cross-tenant: must not appear",
            note="Cross-tenant note",
        )
        make_hold_event(db_session, operation=ops[0], user_id=other_user.id, company_id=COMPANY_B)

        context = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)["hold_context"]

        assert context == {"held_at": None, "held_by_user_id": None, "held_by_name": None, "blocker": None}


class TestTheReadStaysPure:
    def test_rendering_a_held_operation_writes_nothing(self, client: TestClient, db_session: Session):
        """No audit row, no operational event. A poll is not an actor."""
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD])
        make_blocker(db_session, operation=ops[0], reported_by=user.id)

        audit_before = db_session.query(AuditLog).count()
        events_before = db_session.query(OperationalEvent).count()

        get_work_order(client, user, wo.id)
        get_work_order(client, user, wo.id)

        assert db_session.query(AuditLog).count() == audit_before
        assert db_session.query(OperationalEvent).count() == events_before

    def test_no_held_operation_costs_no_query(self, client: TestClient, db_session: Session, monkeypatch):
        """The common path must not pay for a feature it never renders."""
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.READY, OperationStatus.COMPLETE])

        calls = []

        def _spy(*args, **kwargs):
            calls.append(kwargs)
            return {}

        monkeypatch.setattr(work_orders_endpoint, "hold_contexts_for_operations", _spy)

        payload = get_work_order(client, user, wo.id)

        assert calls == []
        assert all(op["hold_context"] is None for op in payload["operations"])

    def test_the_hold_lookup_runs_before_the_render_loop_and_with_autoflush_suspended(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        """The one query this render step issues must not be able to become a WRITE.

        ``_enrich_work_order_operations`` assigns MAPPED columns in its loop
        (``quantity_complete``, ``actual_setup_hours``, ...). Before this feature the
        function issued no query at all, so those in-memory normalizations could never
        be flushed by it. It issues one now -- and a query is an autoflush point, so a
        lookup placed after (or inside) that loop would push the normalizations into the
        open transaction and turn a GET into a write.

        Two things keep that impossible and BOTH are pinned here, because either alone
        is one refactor away from being lost:

        * the lookup runs FIRST -- no operation is dirty when it fires;
        * it runs with autoflush OFF -- so even a future assignment moved above it could
          not be flushed by it.

        On the autoflush half, note WHAT it pins: ``SessionLocal``/``TestingSessionLocal``
        are both built ``autoflush=False``, so removing the ``db.no_autoflush`` block alone
        does NOT fail this (verified 2026-08-31). It fails when the lookup would actually
        autoflush -- i.e. an autoflushing session plus no ``no_autoflush`` block. That is
        the belt AND the braces, and this asserts the outcome rather than either mechanism,
        so replacing one with the other stays green while losing both does not.

        Structural on purpose. The effect it prevents is only externally observable at a
        call site that commits after enriching, and the point of the guard is that no
        such call site ever has to be audited for it.
        """
        user = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_center=wc, statuses=[OperationStatus.ON_HOLD, OperationStatus.READY])
        make_blocker(db_session, operation=ops[0], reported_by=user.id)

        # NULL out columns the enrich loop coerces to 0, so the loop demonstrably
        # dirties these rows: without this the assertion could pass on a no-op.
        db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).update(
            {"quantity_complete": None, "actual_setup_hours": None, "actual_run_hours": None},
            synchronize_session=False,
        )
        db_session.commit()

        real = work_orders_endpoint.hold_contexts_for_operations
        seen: dict = {}

        def _spy(session, **kwargs):
            seen["autoflush"] = session.autoflush
            seen["dirty_operations"] = sorted(
                obj.id for obj in session.dirty if isinstance(obj, WorkOrderOperation) and obj.id is not None
            )
            return real(session, **kwargs)

        monkeypatch.setattr(work_orders_endpoint, "hold_contexts_for_operations", _spy)

        held = operation_by_id(get_work_order(client, user, wo.id), ops[0].id)

        assert seen, "the hold lookup never ran -- this guard would pass vacuously"
        assert seen["autoflush"] is False, "the hold lookup ran with autoflush ENABLED -- a render can now write"
        assert seen["dirty_operations"] == [], (
            "operations were already dirty when the hold lookup ran -- the query moved below "
            "the normalizing loop, so this GET can flush mapped-column writes"
        )
        # And it still did its job.
        assert held["hold_context"]["blocker"] is not None
