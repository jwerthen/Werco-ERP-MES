"""Read-path healing of the PENDING -> READY promotion (follow-up to the pooling change).

THE GAP. Promotion runs at WO release (``release_first_ready_operation``) and at
operation completion (``release_next_ready_operation``) and nowhere else, and
``POST /work-orders/{id}/release`` refuses anything that is not DRAFT. A work order
RELEASED under the older, stricter promotion rule therefore had no way to be
re-promoted: its operations 2..N sat PENDING forever, invisible to the kiosk and the
dispatch board (both surface READY work ONLY). The owner's live case was an 18-item
press-brake batch showing 1 of 18.

The fix re-runs the promotion from the existing read-path seam
(``reconcile_work_orders_from_completion_evidence``), so a stranded work order repairs
itself the next time anyone loads a board.

What these tests pin:

* the heal itself -- a RELEASED WO with stranded same-work-center PENDING ops promotes
  them ALL, both through the service seam and through a real HTTP read;
* DRAFT IS NEVER PROMOTED (standing owner decision: Release is the authorization step
  and the record of who authorized production, so a GET must not put unreleased work
  on the floor's board);
* the rule is unchanged by the heal -- cross-work-center ordering still holds, and an
  ON_HOLD predecessor still blocks its own work center's siblings (the carve-out must
  not be bypassable by loading a page);
* idempotence -- a second read promotes nothing and emits no second ``operation_ready``;
* terminal work orders are untouched;
* ONE implementation -- all three promotion seams route through
  ``promote_ready_operations``; a fourth copy of this rule is how the office and floor
  gates drifted apart in the first place.

Fixtures mirror the sibling completion/dispatch suites: rows are created directly in
the shared ``db_session`` and requests use a directly-minted token; the ``client``
fixture overrides ``get_db`` to yield that same session.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
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
from app.services import work_order_state_service
from app.services.work_order_state_service import (
    StatusTransition,
    reconcile_work_orders_from_completion_evidence,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 902
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"heal-{n}@co{company_id}.test",
        employee_id=f"HEAL-{n:05d}",
        first_name="Heal",
        last_name="Tester",
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
        name=f"HEAL-WC-{n}",
        code=f"HEAL-WC-{n}",
        work_center_type="press_brake",
        description="promotion-heal fixture work center",
        hourly_rate=100,
        capacity_hours_per_day=8.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    *,
    work_centers: list,
    statuses: list,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    work_order_type: str = WorkOrderType.PRODUCTION.value,
    company_id: int = COMPANY_A,
) -> tuple:
    """A WO with one operation per entry of ``statuses``, sequences 10/20/30...

    ``work_centers[i]`` hosts op ``i`` -- pass the same work center repeatedly for the
    batch/pool shape, distinct ones for a conventional routing.
    """
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"HEAL-P-{n}",
        name="Braked bracket",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"HEAL-WO-{n:05d}",
        part_id=part.id,
        work_order_type=work_order_type,
        customer_name="Acme",
        quantity_ordered=len(statuses),
        status=wo_status,
        priority=3,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    ops = []
    for index, (wc, op_status) in enumerate(zip(work_centers, statuses), start=1):
        op = WorkOrderOperation(
            company_id=company_id,
            work_order_id=wo.id,
            work_center_id=wc.id,
            sequence=index * 10,
            operation_number=f"OP{index * 10}",
            name=f"Item {index}",
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


def _statuses(db: Session, ops: list) -> list:
    db.expire_all()
    return [db.get(WorkOrderOperation, op.id).status for op in ops]


def _ready_events(db: Session, operation_id: int) -> list:
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_ready", OperationalEvent.operation_id == operation_id)
        .all()
    )


def _reconcile(db: Session, work_orders: list, transitions=None) -> bool:
    """Run the read-path reconcile and persist whatever it changed (as a GET handler does)."""
    changed = reconcile_work_orders_from_completion_evidence(db, work_orders, transitions)
    db.commit()
    return changed


# --------------------------------------------------------------------------- #
# The heal: a work order released before the pooling rule repairs itself
# --------------------------------------------------------------------------- #
class TestStrandedWorkOrderHealsOnRead:
    def test_reconcile_promotes_every_stranded_same_work_center_op(self, db_session: Session):
        """The owner's case: a batch WO released under the old rule, 1 of N visible."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 5,
            statuses=[OperationStatus.READY] + [OperationStatus.PENDING] * 4,
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 5
        for op in ops[1:]:
            assert len(_ready_events(db_session, op.id)) == 1, "each promoted op emits operation_ready once"

    def test_promotion_alone_makes_the_reconcile_report_a_change(self, db_session: Session):
        """``_reconcile_and_commit`` commits ONLY when the reconcile returns True, so a
        promotion that did not report itself would be rolled back with the read.

        ``current_operation_id`` is pre-set so the RUP-1 repair cannot be what flips the
        flag: the promotion has to carry it on its own.
        """
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY, OperationStatus.PENDING, OperationStatus.PENDING],
        )
        wo.current_operation_id = ops[0].id
        db_session.commit()

        assert reconcile_work_orders_from_completion_evidence(db_session, [wo]) is True
        db_session.commit()

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3

    def test_an_http_read_heals_the_work_order_end_to_end(self, client: TestClient, db_session: Session):
        """No new endpoint: loading the work order is what repairs it."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3

    def test_stranded_ops_become_visible_on_the_shop_floor_queue(self, client: TestClient, db_session: Session):
        """The point of the heal: the work becomes VISIBLE where the floor looks for it.

        The dispatch board and kiosk surface READY/IN_PROGRESS only, so a PENDING op is
        invisible there no matter that it is legal to clock into. Before the first load
        the queue shows 1 of 4; the load itself is what repairs that.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 4,
            statuses=[OperationStatus.READY] + [OperationStatus.PENDING] * 3,
        )

        def ready_rows() -> list:
            resp = client.get(
                "/api/v1/shop-floor/operations",
                headers=headers_for(admin),
                params={"work_center_id": wc.id, "status": "ready"},
            )
            assert resp.status_code == status.HTTP_200_OK, resp.text
            return resp.json()["operations"]

        # The first load carries the stranded work order (its READY op is on the page) and
        # heals it; the status filter is applied in SQL, so the freshly promoted rows show
        # up on the next load -- which is exactly the operator's next poll.
        assert len(ready_rows()) == 1, "1 of 4 -- the bug, as the floor saw it"

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 4
        assert len(ready_rows()) == 4

    def test_a_laser_pool_promotes_every_pending_nest_through_the_heal(self, db_session: Session):
        """The laser exemption is STRICTLY FULLER and survives the heal: nests promote
        across work centers, with no predecessor gating at all."""
        wc_one = make_work_center(db_session)
        wc_two = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc_one, wc_two, wc_two],
            statuses=[OperationStatus.PENDING] * 3,
            work_order_type=WorkOrderType.LASER_CUTTING.value,
        )

        assert _reconcile(db_session, [wo]) is True
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3


# --------------------------------------------------------------------------- #
# The owner decision: RELEASE is the authorization step
# --------------------------------------------------------------------------- #
class TestDraftIsNeverPromoted:
    def test_reconcile_does_not_promote_a_draft_work_order(self, db_session: Session):
        """Standing owner decision. A read must never put unreleased work on the board:
        Release is the authorization step and the record of WHO authorized production."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
            wo_status=WorkOrderStatus.DRAFT,
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [OperationStatus.PENDING] * 3
        for op in ops:
            assert _ready_events(db_session, op.id) == []

    def test_a_draft_work_order_read_over_http_stays_pending(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
            wo_status=WorkOrderStatus.DRAFT,
        )

        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.PENDING] * 3

    def test_releasing_the_draft_then_reading_promotes_the_whole_pool(self, client: TestClient, db_session: Session):
        """The carve-out delays promotion to the authorized moment; it does not lose it."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
            wo_status=WorkOrderStatus.DRAFT,
        )

        resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3


# --------------------------------------------------------------------------- #
# The heal runs THE rule -- it does not relax it
# --------------------------------------------------------------------------- #
class TestTheHealDoesNotWidenTheRule:
    def test_cross_work_center_ordering_is_preserved(self, db_session: Session):
        """A conventional routing -- one op per work center -- promotes exactly one."""
        wo, ops = make_wo(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(3)],
            statuses=[OperationStatus.PENDING] * 3,
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [
            OperationStatus.READY,
            OperationStatus.PENDING,
            OperationStatus.PENDING,
        ]

    def test_a_later_op_is_not_promoted_past_an_in_progress_predecessor(self, db_session: Session):
        wc_one = make_work_center(db_session)
        wc_two = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc_one, wc_two],
            statuses=[OperationStatus.IN_PROGRESS, OperationStatus.PENDING],
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [OperationStatus.IN_PROGRESS, OperationStatus.PENDING]

    def test_a_held_predecessor_still_blocks_its_own_work_center_siblings(self, db_session: Session):
        """The ON_HOLD carve-out outranks the pooling exemption -- and a page load must
        not be a way around it. Holding item 1 of a batch takes the pool off the board;
        if the heal ignored the carve-out the shop would keep building past the stop."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 4,
            statuses=[OperationStatus.ON_HOLD] + [OperationStatus.PENDING] * 3,
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [OperationStatus.ON_HOLD] + [OperationStatus.PENDING] * 3
        for op in ops[1:]:
            assert _ready_events(db_session, op.id) == []

    def test_clearing_the_hold_lets_the_next_read_promote_the_pool(self, db_session: Session):
        """Resuming an operation does not itself re-run promotion -- the read does."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.ON_HOLD] + [OperationStatus.PENDING] * 2,
        )

        ops[0].status = OperationStatus.READY  # the resume paths' "previous state"
        db_session.commit()

        assert _reconcile(db_session, [wo]) is True
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3

    def test_only_pending_operations_are_touched(self, db_session: Session):
        """The heal is not a status reset: COMPLETE / IN_PROGRESS / ON_HOLD rows keep
        their status, so it can never resurrect finished or held work."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 4,
            statuses=[
                OperationStatus.COMPLETE,
                OperationStatus.IN_PROGRESS,
                OperationStatus.ON_HOLD,
                OperationStatus.PENDING,
            ],
        )

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops)[:3] == [
            OperationStatus.COMPLETE,
            OperationStatus.IN_PROGRESS,
            OperationStatus.ON_HOLD,
        ]


# --------------------------------------------------------------------------- #
# Read-path safety properties
# --------------------------------------------------------------------------- #
class TestReadPathSafety:
    def test_a_second_reconcile_promotes_nothing_and_emits_no_duplicate_event(self, db_session: Session):
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
        )

        _reconcile(db_session, [wo])
        first_pass_event_ids = {ev.id for op in ops for ev in _ready_events(db_session, op.id)}

        assert _reconcile(db_session, [wo]) is False, "a fully-reconciled WO changes NOTHING on the next read"

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3
        assert {ev.id for op in ops for ev in _ready_events(db_session, op.id)} == first_pass_event_ids

    def test_terminal_work_orders_are_untouched(self, db_session: Session):
        """COMPLETE / CLOSED / CANCELLED are final -- a read must not put a finished or
        cancelled job's leftover operations back on the board."""
        for terminal in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED):
            wc = make_work_center(db_session)
            wo, ops = make_wo(
                db_session,
                work_centers=[wc] * 3,
                statuses=[OperationStatus.PENDING] * 3,
                wo_status=terminal,
            )

            _reconcile(db_session, [wo])

            assert _statuses(db_session, ops) == [OperationStatus.PENDING] * 3, terminal
            for op in ops:
                assert _ready_events(db_session, op.id) == [], terminal

    def test_promotion_is_not_reported_as_an_audited_transition(self, db_session: Session):
        """The chosen policy: PENDING -> READY stays unaudited, as it is at the other two
        promotion seams. ``transitions`` carries COMPLETION semantics that five consumers
        act on (audit rows, completion events, FG receipt/backflush, cost rollup,
        scheduling refresh), so a READY entry there would be a wrong signal to all of
        them as well as a fourth policy."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
        )
        transitions: list[StatusTransition] = []

        _reconcile(db_session, [wo], transitions)

        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3
        assert transitions == []

    def test_one_work_order_failing_does_not_stop_the_others(self, db_session: Session, monkeypatch):
        """This runs from GET handlers whose guard catches only SQLAlchemyError, so the
        heal swallows its own failures per work order rather than 500'ing a board load."""
        wc = make_work_center(db_session)
        broken, broken_ops = make_wo(db_session, work_centers=[wc] * 2, statuses=[OperationStatus.PENDING] * 2)
        healthy, healthy_ops = make_wo(db_session, work_centers=[wc] * 2, statuses=[OperationStatus.PENDING] * 2)

        real_promote = work_order_state_service.promote_ready_operations

        def explode_for_the_broken_one(work_order, operations, db=None, user_id=None):
            if work_order.id == broken.id:
                raise RuntimeError("boom")
            return real_promote(work_order, operations, db=db, user_id=user_id)

        monkeypatch.setattr(work_order_state_service, "promote_ready_operations", explode_for_the_broken_one)

        _reconcile(db_session, [broken, healthy])

        assert _statuses(db_session, broken_ops) == [OperationStatus.PENDING] * 2
        assert _statuses(db_session, healthy_ops) == [OperationStatus.READY] * 2

    def test_another_tenants_operation_row_is_never_promoted(self, db_session: Session):
        """Invariant 1: the promotion's write set is scoped to the work order's own
        company, so a mis-parented row can never be written by a read."""
        wc = make_work_center(db_session)
        wo, ops = make_wo(db_session, work_centers=[wc] * 2, statuses=[OperationStatus.PENDING] * 2)
        _ensure_company(db_session, COMPANY_B)
        ops[1].company_id = COMPANY_B
        db_session.commit()

        _reconcile(db_session, [wo])

        assert _statuses(db_session, ops) == [OperationStatus.READY, OperationStatus.PENDING]


# --------------------------------------------------------------------------- #
# ONE implementation of the rule, not four
# --------------------------------------------------------------------------- #
class TestSinglePromotionImplementation:
    """Three seams promote; all three must route through ``promote_ready_operations``.

    Three copies of this rule is how the office and floor gates drifted apart, which is
    what the pooling change had to repair. A future seam that re-inlines the gate fails
    here.
    """

    @pytest.mark.parametrize(
        "seam",
        ["release", "completion", "reconcile"],
    )
    def test_every_promotion_seam_routes_through_the_shared_rule(self, db_session: Session, monkeypatch, seam):
        wc = make_work_center(db_session)
        wo_status = WorkOrderStatus.DRAFT if seam == "release" else WorkOrderStatus.RELEASED
        wo, ops = make_wo(
            db_session,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.COMPLETE] + [OperationStatus.PENDING] * 2,
            wo_status=wo_status,
        )
        calls = []
        real_promote = work_order_state_service.promote_ready_operations

        def counting_promote(work_order, operations, db=None, user_id=None):
            calls.append(work_order.id)
            return real_promote(work_order, operations, db=db, user_id=user_id)

        monkeypatch.setattr(work_order_state_service, "promote_ready_operations", counting_promote)

        if seam == "release":
            work_order_state_service.release_first_ready_operation(wo, db_session)
        elif seam == "completion":
            work_order_state_service.release_next_ready_operation(db_session, wo, ops[0])
        else:
            reconcile_work_orders_from_completion_evidence(db_session, [wo])
        db_session.commit()

        assert calls == [wo.id], f"the {seam} seam must delegate to promote_ready_operations"
        assert _statuses(db_session, ops)[1:] == [OperationStatus.READY] * 2
