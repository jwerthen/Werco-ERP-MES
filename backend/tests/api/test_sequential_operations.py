"""``WorkOrder.sequential_operations`` (081): a sequenced ROUTING vs a DISPATCH POOL.

THE REPORTED BUG. READY promotion has been pooled by work center since the batch/laser
work: operations of one work order that share a machine go READY together. That is right
for a laser nest package and for the 18-item press-brake batch WOs, where the pool IS the
job. It is wrong for a real routing. WO-20260807-006 is a 4-operation weld assembly --
10 Skid Fit, 20 Wall Fit Up, 30 Accessory Fit Up, 40 Weld Out -- whose first THREE
operations sit on one weld cell, so releasing it unlocked all three at once and the floor
lost the build order. ``sequential_operations`` is the per-work-order discriminator.

What this suite pins:

1. **The owner's case, end to end.** Released, a sequenced routing shows exactly ONE
   operation; completing it promotes exactly ONE more. The same route built as a pool is
   the control -- without it, "only op 10 is READY" could be satisfied by a gate that
   blocks everything.

2. **Promotion and clock-in are ONE rule.** The operation a sequenced work order refuses
   to promote is also refused by ``POST /shop-floor/clock-in``, by
   ``PUT /shop-floor/operations/{id}/start``, by ``POST /shop-floor/operations/{id}/complete``
   and by BOTH office verbs -- same 400, same ``MSG_PREDECESSORS_INCOMPLETE`` text. This
   is the whole doctrine: an operation invisible on the dispatch board must also be
   refused by a badge scan, and one a badge can start must be visible. The two shop-floor
   verbs carried INLINE copies of the gate until 081 collapsed them onto the shared
   ``operation_action_gates.operation_blocked_by_predecessors``, and an inline copy that
   kept hard-coding ``allow_same_work_center=True`` is exactly how the floor would have
   gone on starting work the board had already taken away.

3. **The ON_HOLD carve-out survives both modes.** A held predecessor blocks from ANY work
   center, its own included -- a quality/material stop must take the pool off the board
   rather than leave the siblings startable. 081 must not have widened or narrowed it.

4. **Laser work orders ignore the flag entirely.** ``is_laser_dispatch_work_order``
   short-circuits ABOVE it at every seam and is strictly fuller (it drops predecessor
   gating altogether, across work centers). Both flag values are asserted, because "the
   column is inert here" is a claim, not an observation.

5. **The flip, which is the only write in this system that moves an operation BACKWARDS.**
   Turning sequencing on demotes the un-started READY operations the pooled rule had
   already promoted (promotion is forward-only, so nothing else would ever take them off
   the board), writes one audit row per demoted operation (invariant 2), leaves worked /
   held / complete operations alone, and is REFUSED 409 -- leaving the row untouched --
   when work is already under way out of sequence or the work order is a laser package.
   Flipping back needs no sweep: a reconciling read re-promotes the pool.

6. **Tenant isolation on the two new queries** (``operations_worked_out_of_sequence``,
   ``demote_operations_for_sequencing``) -- invariant 1.

Fixture note: ``make_wo`` takes ``sequential_operations`` EXPLICITLY at every call site in
this file, never by default. The model's create-default (True) and the column's
server_default (False) deliberately disagree -- that asymmetry is the 081 data-migration
story -- so a fixture that leaned on either would be asserting the default rather than the
behavior.
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
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
    WorkOrderType,
)
from app.services.operation_action_gates import MSG_PREDECESSORS_INCOMPLETE
from app.services.work_order_state_service import (
    demote_operations_for_sequencing,
    operations_worked_out_of_sequence,
    work_order_allows_same_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

# The reported job, verbatim: three consecutive fit-up steps on ONE weld cell, then a
# separate weld-out bay. The shape is the point -- on a route whose steps each sit at a
# different work center the two modes are indistinguishable.
WO006_ROUTE = [
    (10, "Skid Fit", "weld"),
    (20, "Wall Fit Up", "weld"),
    (30, "Accessory Fit Up", "weld"),
    (40, "Weld Out", "finish"),
]


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
        email=f"seqops-{n}@co{company_id}.test",
        employee_id=f"SEQOPS-{n:05d}",
        first_name="Seq",
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
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"SEQOPS-WC-{n}",
        code=f"SEQOPS-WC-{n}",
        work_center_type="welding",
        description="sequential-operations fixture work center",
        hourly_rate=100.0,
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
    sequential_operations: bool,
    work_centers: list,
    statuses: list,
    names: list = None,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    work_order_type: str = WorkOrderType.PRODUCTION.value,
    company_id: int = COMPANY_A,
) -> tuple:
    """A WO with one operation per entry of ``statuses``, sequences 10/20/30...

    ``work_centers[i]`` hosts op ``i`` -- repeat a work center to put consecutive steps
    on one machine, which is the only shape where the two modes differ at all.
    ``sequential_operations`` is REQUIRED: see the module docstring.
    """
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SEQOPS-P-{n}",
        name="Weld assembly",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"SEQOPS-WO-{n:05d}",
        part_id=part.id if work_order_type != WorkOrderType.LASER_CUTTING.value else None,
        work_order_type=work_order_type,
        sequential_operations=sequential_operations,
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
            name=(names[index - 1] if names else f"Step {index}"),
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


def make_wo006(db: Session, *, sequential_operations: bool, wo_status=WorkOrderStatus.DRAFT) -> tuple:
    """The reported 4-op weld assembly: seq 10/20/30 on ONE weld cell, seq 40 elsewhere."""
    weld = make_work_center(db)
    finish = make_work_center(db)
    lookup = {"weld": weld, "finish": finish}
    return (
        *make_wo(
            db,
            sequential_operations=sequential_operations,
            work_centers=[lookup[cell] for _, _, cell in WO006_ROUTE],
            statuses=[OperationStatus.PENDING] * len(WO006_ROUTE),
            names=[name for _, name, _ in WO006_ROUTE],
            wo_status=wo_status,
        ),
        weld,
    )


def _statuses(db: Session, ops: list) -> list:
    db.expire_all()
    return [db.get(WorkOrderOperation, op.id).status for op in ops]


def add_time_entry(db: Session, *, user: User, op: WorkOrderOperation, company_id: int = COMPANY_A) -> TimeEntry:
    """A CLOSED entry -- the labor-evidence test is existence only, open or closed."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    return entry


def demotion_audit_rows(db: Session, operation_id: int) -> list:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == operation_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def put_work_order(client: TestClient, user: User, wo: WorkOrder, db: Session, **fields):
    """PUT /work-orders/{id}, echoing the current optimistic-lock version."""
    db.expire_all()
    current = db.get(WorkOrder, wo.id)
    return client.put(
        f"/api/v1/work-orders/{wo.id}",
        headers=headers_for(user),
        json={"version": current.version, **fields},
    )


# --------------------------------------------------------------------------- #
# 1. The owner's case: WO-20260807-006
# --------------------------------------------------------------------------- #
class TestTheReportedWeldAssembly:
    """WO-20260807-006: 10 Skid Fit / 20 Wall Fit Up / 30 Accessory Fit Up all on one
    weld cell, 40 Weld Out on another. The bug was all three fit-ups going READY at once.
    """

    def test_releasing_a_sequenced_routing_promotes_only_the_first_operation(
        self, client: TestClient, db_session: Session
    ):
        """THE reported case. Release shows Skid Fit and nothing else."""
        admin = make_user(db_session)
        wo, ops, _ = make_wo006(db_session, sequential_operations=True)

        resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,  # 10 Skid Fit
            OperationStatus.PENDING,  # 20 Wall Fit Up  -- same weld cell, still blocked
            OperationStatus.PENDING,  # 30 Accessory Fit Up
            OperationStatus.PENDING,  # 40 Weld Out
        ]

    def test_completing_the_first_operation_promotes_only_the_second(self, client: TestClient, db_session: Session):
        """One step at a time, all the way down a route that shares one machine."""
        admin = make_user(db_session)
        wo, ops, _ = make_wo006(db_session, sequential_operations=True)
        assert (
            client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(admin)).status_code
            == status.HTTP_200_OK
        )

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0].id}/complete",
            params={"quantity_complete": 1},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.COMPLETE,  # 10 Skid Fit
            OperationStatus.READY,  # 20 Wall Fit Up -- promoted, and ONLY this one
            OperationStatus.PENDING,  # 30 Accessory Fit Up
            OperationStatus.PENDING,  # 40 Weld Out
        ]

    def test_the_same_route_pooled_promotes_the_whole_weld_cell(self, client: TestClient, db_session: Session):
        """THE CONTROL. Identical route, flag flipped: all three fit-ups unlock together.

        Without this, "only op 10 is READY" above would be satisfied by a gate that
        refuses everything, and the batch WOs the pooled rule exists for would be
        silently broken.
        """
        admin = make_user(db_session)
        wo, ops, _ = make_wo006(db_session, sequential_operations=False)

        resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,  # the three weld-cell steps are one unordered pool
            OperationStatus.READY,
            OperationStatus.READY,
            OperationStatus.PENDING,  # cross-work-center ordering still holds
        ]

    def test_the_resolver_reports_the_mode(self, db_session: Session):
        """``work_order_allows_same_work_center`` is THE single reader of the column."""
        sequenced, _, _ = make_wo006(db_session, sequential_operations=True)
        pooled, _, _ = make_wo006(db_session, sequential_operations=False)

        assert work_order_allows_same_work_center(sequenced) is False
        assert work_order_allows_same_work_center(pooled) is True
        # Unresolvable -> POOLED, never a silently TIGHTENED gate on work the floor was
        # already allowed to start.
        assert work_order_allows_same_work_center(None) is True


# --------------------------------------------------------------------------- #
# 2. Promotion and clock-in are ONE rule
# --------------------------------------------------------------------------- #
class TestPromotionAndTheActionVerbsAgree:
    """The operation a sequenced routing will not PROMOTE, every verb must also REFUSE.

    This is the parity doctrine, and the two shop-floor verbs are where it could drift:
    ``PUT /shop-floor/operations/{id}/start`` and
    ``POST /shop-floor/operations/{id}/complete`` each carried an INLINE copy of the gate
    with ``allow_same_work_center=True`` hard-coded until 081 collapsed them onto the
    shared predicate. Left inline, the operator's two primary verbs would have kept
    pooling while the dispatch board and the kiosk queue gated them -- an operation
    refused on the board but startable by badge scan.
    """

    def _blocked_pair(self, db: Session):
        """A SEQUENCED WO on ONE work center: op 10 READY, op 20 blocked behind it."""
        wc = make_work_center(db)
        wo, ops = make_wo(
            db,
            sequential_operations=True,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.PENDING],
        )
        return wo, ops, wc

    def test_the_blocked_operation_is_refused_by_clock_in(self, client: TestClient, db_session: Session):
        user = make_user(db_session, role=UserRole.OPERATOR)
        wo, ops, wc = self._blocked_pair(db_session)

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(user),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[1].id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE

    def test_the_blocked_operation_is_refused_by_the_shop_floor_start_verb(
        self, client: TestClient, db_session: Session
    ):
        """One of the two collapsed inline copies."""
        user = make_user(db_session, role=UserRole.OPERATOR)
        _, ops, _ = self._blocked_pair(db_session)

        resp = client.put(f"/api/v1/shop-floor/operations/{ops[1].id}/start", headers=headers_for(user))

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE
        assert _statuses(db_session, ops)[1] == OperationStatus.PENDING

    def test_the_blocked_operation_is_refused_by_the_shop_floor_complete_verb(
        self, client: TestClient, db_session: Session
    ):
        """Refused -- but by the STATUS gate, which on this verb sits ABOVE the
        predecessor gate.

        Stated rather than papered over: ``COMPLETE_ALLOWED_STATUSES`` is
        ``[IN_PROGRESS, READY]``, and a sequenced work order never promotes the blocked
        operation in the first place, so on this verb the PENDING row is turned away one
        gate earlier. The parity property still holds -- the operation the board will not
        show cannot be completed from the floor -- and the predecessor gate underneath is
        exercised by the stale-READY test below, which is the state where it is the only
        thing standing in the way.

        Labor is recorded on the operation first so the refusal cannot be the no-labor
        gate wearing either gate's clothes.
        """
        user = make_user(db_session, role=UserRole.OPERATOR)
        _, ops, _ = self._blocked_pair(db_session)
        add_time_entry(db_session, user=user, op=ops[1])

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[1].id}/complete",
            headers=headers_for(user),
            json={"quantity_complete": 1},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Cannot complete operation with status: pending"
        assert _statuses(db_session, ops)[1] == OperationStatus.PENDING

    def test_a_stale_ready_row_is_still_refused_by_the_shop_floor_complete_verb(
        self, client: TestClient, db_session: Session
    ):
        """The other collapsed inline copy, at the one state that reaches it.

        A READY row on a sequenced routing whose predecessor is still open is the state
        the predecessor gate exists for -- it survives an import, a hand-edited row, or
        any future promotion path that has not been taught the flag. The gate must refuse
        it on evidence, not trust that promotion got there first: this verb hard-coded
        ``allow_same_work_center=True`` inline until 081, which would have completed this
        operation out of order while the dispatch board showed the route sequenced.
        """
        user = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, ops = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[wc, wc],
            statuses=[OperationStatus.IN_PROGRESS, OperationStatus.READY],
        )
        add_time_entry(db_session, user=user, op=ops[1])

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[1].id}/complete",
            headers=headers_for(user),
            json={"quantity_complete": 1},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE
        assert _statuses(db_session, ops)[1] == OperationStatus.READY

    def test_the_same_stale_ready_row_completes_fine_when_the_work_order_is_pooled(
        self, client: TestClient, db_session: Session
    ):
        """THE CONTROL for the test above: identical rows, flag flipped, and the floor
        may finish it -- which is the batch-WO behavior 081 had to preserve."""
        user = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc, wc],
            statuses=[OperationStatus.IN_PROGRESS, OperationStatus.READY],
        )
        add_time_entry(db_session, user=user, op=ops[1])

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[1].id}/complete",
            headers=headers_for(user),
            json={"quantity_complete": 1},
        )

        assert resp.status_code == 200, resp.text
        assert _statuses(db_session, ops)[1] == OperationStatus.COMPLETE

    def test_the_blocked_operation_is_refused_by_the_office_start_verb(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        _, ops, _ = self._blocked_pair(db_session)

        resp = client.post(f"/api/v1/work-orders/operations/{ops[1].id}/start", headers=headers_for(admin))

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE

    def test_the_blocked_operation_is_refused_by_the_office_complete_verb(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        _, ops, _ = self._blocked_pair(db_session)

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[1].id}/complete",
            params={"quantity_complete": 1},
            headers=headers_for(admin),
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE

    def test_the_promoted_operation_is_accepted_by_the_same_verbs(self, client: TestClient, db_session: Session):
        """THE CONTROL for all five refusals above: the operation sequencing DID promote
        is startable and completable. A gate that refused everything would pass every
        assertion in this class but this one."""
        admin = make_user(db_session)
        wo, ops, wc = self._blocked_pair(db_session)

        clock_in = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(admin),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[0].id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )
        assert clock_in.status_code == 200, clock_in.text

        complete = client.post(
            f"/api/v1/shop-floor/operations/{ops[0].id}/complete",
            headers=headers_for(admin),
            json={"quantity_complete": 1},
        )
        assert complete.status_code == 200, complete.text

        # And completing it is what promotes the one behind it -- the same rule, forward.
        assert _statuses(db_session, ops) == [OperationStatus.COMPLETE, OperationStatus.READY]

    def test_the_pooled_twin_of_every_refusal_is_allowed(self, client: TestClient, db_session: Session):
        """The same two operations on the same machine, pooled: op 20 is startable while
        op 10 sits open. This is the behavior every pre-081 row backfilled to."""
        user = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(user),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[1].id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )

        assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 3. The ON_HOLD carve-out, under BOTH modes
# --------------------------------------------------------------------------- #
class TestHeldPredecessorBlocksUnderBothModes:
    """A held predecessor blocks from ANY work center, its own included.

    The carve-out is what makes a quality/material stop a stop: without it, holding item
    1 of a batch would leave every sibling on the dispatch board and startable by badge.
    081 changed whether a RUNNING same-work-center predecessor blocks; it must not have
    touched whether a HELD one does, in either mode.
    """

    @pytest.mark.parametrize("sequential", [True, False], ids=["sequenced", "pooled"])
    def test_a_held_operation_blocks_its_same_work_center_siblings_from_promoting(
        self, client: TestClient, db_session: Session, sequential
    ):
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=sequential,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.ON_HOLD] + [OperationStatus.PENDING] * 2,
        )

        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.ON_HOLD] + [OperationStatus.PENDING] * 2

    @pytest.mark.parametrize("sequential", [True, False], ids=["sequenced", "pooled"])
    def test_a_held_operation_blocks_its_same_work_center_siblings_from_clocking_in(
        self, client: TestClient, db_session: Session, sequential
    ):
        user = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=sequential,
            work_centers=[wc, wc],
            statuses=[OperationStatus.ON_HOLD, OperationStatus.READY],
        )

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(user),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[1].id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE


# --------------------------------------------------------------------------- #
# 4. Laser work orders ignore the flag
# --------------------------------------------------------------------------- #
class TestLaserWorkOrdersAreUnaffected:
    """``is_laser_dispatch_work_order`` short-circuits ABOVE this flag at every seam and
    is STRICTLY FULLER: it drops predecessor gating entirely, across work centers. Both
    values are asserted, because "the column is inert here" is a claim, not an
    observation -- and a laser WO can carry ``sequential_operations=True`` only by
    accident (the child-WO constructors pin it False), which must still change nothing.
    """

    @pytest.mark.parametrize("sequential", [True, False], ids=["flag-on", "flag-off"])
    def test_every_nest_promotes_across_two_lasers_whatever_the_flag_says(
        self, client: TestClient, db_session: Session, sequential
    ):
        admin = make_user(db_session)
        laser_one = make_work_center(db_session)
        laser_two = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=sequential,
            work_centers=[laser_one, laser_two, laser_two],
            statuses=[OperationStatus.PENDING] * 3,
            work_order_type=WorkOrderType.LASER_CUTTING.value,
        )

        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3

    @pytest.mark.parametrize("sequential", [True, False], ids=["flag-on", "flag-off"])
    def test_a_later_nest_is_clock_in_able_whatever_the_flag_says(
        self, client: TestClient, db_session: Session, sequential
    ):
        user = make_user(db_session, role=UserRole.OPERATOR)
        laser = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=sequential,
            work_centers=[laser, laser],
            statuses=[OperationStatus.READY, OperationStatus.READY],
            work_order_type=WorkOrderType.LASER_CUTTING.value,
        )

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(user),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[1].id,
                "work_center_id": laser.id,
                "entry_type": "run",
            },
        )

        assert resp.status_code == 200, resp.text


class TestSchedulingIsSafelyUngated:
    """``scheduling._apply_work_order_schedule`` promotes PENDING -> READY and is
    deliberately NOT gated on ``sequential_operations``. Pinned, because "it is
    unblocked by construction" is a proof about OTHER code that a future edit can break
    silently.

    The proof: ``_get_current_operation`` returns the LOWEST-sequence non-COMPLETE
    operation, so by construction every operation below it IS complete -- which is
    exactly the strict rule's condition. The ON_HOLD carve-out cannot sneak past either,
    because ON_HOLD is itself non-COMPLETE, so a held lower step would BE the returned
    operation and the promotion branch (guarded on PENDING) would not fire.

    If someone ever changes that selection to "the lowest READY-eligible op" or teaches
    it to skip a held step, scheduling becomes a fourth promotion seam that does not know
    about the flag -- and these tests fail.
    """

    def _sequenced_route_on_one_cell(self, db: Session):
        wc = make_work_center(db)
        wo, ops = make_wo(
            db,
            sequential_operations=True,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.PENDING] * 3,
        )
        return wo, ops, wc

    def test_scheduling_promotes_only_the_lowest_operation_of_a_sequenced_routing(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        wo, ops, wc = self._sequenced_route_on_one_cell(db_session)

        resp = client.put(
            f"/api/v1/scheduling/work-orders/{wo.id}/schedule",
            json={"scheduled_start": date.today().isoformat(), "work_center_id": wc.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,
            OperationStatus.PENDING,
            OperationStatus.PENDING,
        ], "scheduling must not put a blocked operation on the board"

    def test_scheduling_never_promotes_past_a_held_step(self, client: TestClient, db_session: Session):
        """The ON_HOLD carve-out's half of the proof: a held step IS the current
        operation, so the PENDING-guarded promotion cannot fire at all."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        resp = client.put(
            f"/api/v1/scheduling/work-orders/{wo.id}/schedule",
            json={"scheduled_start": date.today().isoformat(), "work_center_id": wc.id},
            headers=headers_for(admin),
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.ON_HOLD,
            OperationStatus.PENDING,
            OperationStatus.PENDING,
        ]

    def test_the_operation_scheduling_did_promote_is_startable(self, client: TestClient, db_session: Session):
        """THE CONTROL, and the reason the un-gated promotion is safe rather than merely
        harmless: the row scheduling flips to READY is one the action verbs accept."""
        admin = make_user(db_session)
        wo, ops, wc = self._sequenced_route_on_one_cell(db_session)
        assert (
            client.put(
                f"/api/v1/scheduling/work-orders/{wo.id}/schedule",
                json={"scheduled_start": date.today().isoformat(), "work_center_id": wc.id},
                headers=headers_for(admin),
            ).status_code
            == status.HTTP_200_OK
        )

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=headers_for(admin),
            json={
                "work_order_id": wo.id,
                "operation_id": ops[0].id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )

        assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 5. The flip: PUT /work-orders/{id} with sequential_operations
# --------------------------------------------------------------------------- #
class TestFlippingAPooledWorkOrderToSequenced:
    """Turning sequencing ON must fix THIS work order, not just the next one.

    Every promotion seam is forward-only (PENDING -> READY, never back), so the
    operations the pooled rule already promoted would otherwise sit READY on the dispatch
    board and the kiosk under a rule that forbids them -- the exact rows the owner is
    trying to take off the board.
    """

    def test_the_flip_demotes_the_unstarted_ready_operations(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY] * 3,
        )

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["sequential_operations"] is True
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,  # the lowest startable step stays on the board
            OperationStatus.PENDING,
            OperationStatus.PENDING,
        ]

    def test_each_demotion_writes_exactly_one_audit_row(self, client: TestClient, db_session: Session):
        """Invariant 2: a rule-driven status change is still a status change, and this
        endpoint HAS an actor, so it is attributed rather than left to a read path."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY] * 3,
        )

        assert put_work_order(client, admin, wo, db_session, sequential_operations=True).status_code == 200

        assert demotion_audit_rows(db_session, ops[0].id) == [], "the undemoted op gets no row"
        for demoted in ops[1:]:
            rows = demotion_audit_rows(db_session, demoted.id)
            assert len(rows) == 1, f"op {demoted.sequence}: exactly one STATUS_CHANGE row"
            row = rows[0]
            assert row.old_values == {"status": OperationStatus.READY.value}
            assert row.new_values == {"status": OperationStatus.PENDING.value}
            assert row.extra_data["transition"] == "sequential_operations_enabled"
            assert row.user_id == admin.id, "attributed to the actor who flipped the flag"

    def test_the_flip_leaves_complete_in_progress_and_held_operations_alone(
        self, client: TestClient, db_session: Session
    ):
        """Only un-worked READY rows are the flip's to move. COMPLETE / IN_PROGRESS /
        ON_HOLD are somebody else's state to own, and demoting worked material would
        strand real labor behind a gate the operator already passed."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc] * 5,
            statuses=[
                OperationStatus.COMPLETE,
                OperationStatus.IN_PROGRESS,
                OperationStatus.ON_HOLD,
                OperationStatus.READY,
                OperationStatus.READY,
            ],
        )
        # The IN_PROGRESS op is genuinely started, so it is not "worked out of sequence"
        # (nothing lower is open) and the flip is allowed to proceed.
        ops[1].actual_start = datetime.utcnow() - timedelta(hours=1)
        db_session.commit()

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.COMPLETE,
            OperationStatus.IN_PROGRESS,
            OperationStatus.ON_HOLD,
            OperationStatus.PENDING,  # demoted
            OperationStatus.PENDING,  # demoted
        ]
        for untouched in ops[:3]:
            assert demotion_audit_rows(db_session, untouched.id) == []

    def test_the_flip_is_refused_409_when_work_is_under_way_out_of_sequence(
        self, client: TestClient, db_session: Session
    ):
        """Refuse rather than brick live work.

        Every completion verb re-evaluates the predecessor gate at ACTION time, so an
        operation running ahead of its predecessors becomes uncompletable the instant the
        flag flips -- and no read path heals it, because promotion only moves forward.
        The job would sit on a tablet with every button refusing it.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.IN_PROGRESS],
        )

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        detail = resp.json()["detail"]
        assert "already under way out of sequence" in detail
        assert "OP20" in detail, "the refusal NAMES the operations, so it is actionable"

    def test_the_refused_flip_leaves_the_row_completely_unchanged(self, client: TestClient, db_session: Session):
        """The refusal runs BEFORE the first setattr, so nothing at all is written."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc, wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.IN_PROGRESS, OperationStatus.READY],
        )
        version_before = wo.version
        priority_before = wo.priority

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True, priority=9)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text

        db_session.expire_all()
        fresh = db_session.get(WorkOrder, wo.id)
        assert fresh.sequential_operations is False, "the flag never moved"
        assert fresh.priority == priority_before, "no OTHER field of the same PUT landed either"
        assert fresh.version == version_before, "the optimistic-lock counter never moved"
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,
            OperationStatus.IN_PROGRESS,
            OperationStatus.READY,
        ], "no operation moved"
        for op in ops:
            assert demotion_audit_rows(db_session, op.id) == []

    def test_the_flip_is_refused_409_on_a_laser_work_order(self, client: TestClient, db_session: Session):
        """A nest package pools by TYPE. Accepting True would persist a claim the work
        order's own behavior contradicts and put "sequential" on a screen showing every
        nest at once -- refuse rather than store a lie."""
        admin = make_user(db_session)
        laser = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[laser, laser],
            statuses=[OperationStatus.READY, OperationStatus.READY],
            work_order_type=WorkOrderType.LASER_CUTTING.value,
        )

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "dispatch pool" in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkOrder, wo.id).sequential_operations is False
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 2

    def test_a_put_that_never_mentions_sequencing_demotes_nothing(self, client: TestClient, db_session: Session):
        """``sequential_operations`` is Optional on the update schema for this reason."""
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY] * 3,
        )

        resp = put_work_order(client, admin, wo, db_session, priority=7)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3
        db_session.expire_all()
        assert db_session.get(WorkOrder, wo.id).sequential_operations is False

    def test_re_flipping_an_already_sequenced_work_order_is_a_no_op(self, client: TestClient, db_session: Session):
        """False -> True is what needs repair; True -> True is not a transition, so a
        READY op that is legitimately startable must not be swept off the board."""
        admin = make_user(db_session)
        wc_one = make_work_center(db_session)
        wc_two = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[wc_one, wc_two],
            statuses=[OperationStatus.READY, OperationStatus.PENDING],
        )

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [OperationStatus.READY, OperationStatus.PENDING]


class TestFlippingBackToPooled:
    def test_flipping_back_needs_no_sweep_because_a_read_re_promotes_the_pool(
        self, client: TestClient, db_session: Session
    ):
        """The other direction heals itself. Promotion is forward-only, so turning
        sequencing OFF needs no backwards write at all -- the read-path heal
        (``_promote_stranded_ready_operations``) re-promotes the pool on the next board
        or work-order load, which is the same seam that repaired the stranded batch WOs.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        flip = put_work_order(client, admin, wo, db_session, sequential_operations=False)
        assert flip.status_code == status.HTTP_200_OK, flip.text

        # A reconciling read -- no new endpoint, no migration, no owner action.
        read = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
        assert read.status_code == status.HTTP_200_OK, read.text
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 3

    def test_flipping_back_is_never_refused_by_the_out_of_sequence_check(self, client: TestClient, db_session: Session):
        """Work under way out of sequence is only a problem for the TIGHTENING direction.
        Loosening can never make a running operation uncompletable."""
        admin = make_user(db_session)
        wc_one = make_work_center(db_session)
        wc_two = make_work_center(db_session)
        wo, _ = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[wc_one, wc_two],
            statuses=[OperationStatus.IN_PROGRESS, OperationStatus.IN_PROGRESS],
        )

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=False)

        assert resp.status_code == status.HTTP_200_OK, resp.text


class TestDemotionRefusesToTouchWorkedOperations:
    """``demote_operations_for_sequencing`` guards on labor evidence in its own right.

    Defense in depth rather than dead code: through ``PUT /work-orders/{id}`` the
    endpoint's 409 fires first on exactly the same predicate, so these states are
    unreachable there -- but the demotion is a public service function, and demoting
    worked material would strand labor an operator already recorded.
    """

    def _pooled_pair_with_a_blocked_ready_op(self, db: Session):
        wc = make_work_center(db)
        wo, ops = make_wo(
            db,
            sequential_operations=False,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        # The service reads the flag off the row, so arm it the way the endpoint does.
        wo.sequential_operations = True
        db.commit()
        return wo, ops

    def test_a_ready_operation_carrying_a_time_entry_is_not_demoted(self, db_session: Session):
        user = make_user(db_session)
        wo, ops = self._pooled_pair_with_a_blocked_ready_op(db_session)
        add_time_entry(db_session, user=user, op=ops[1])

        demoted = demote_operations_for_sequencing(db_session, wo)

        assert demoted == []
        assert _statuses(db_session, ops) == [OperationStatus.READY, OperationStatus.READY]

    @pytest.mark.parametrize(
        "field, value",
        [
            ("actual_start", datetime(2026, 8, 1, 12, 0, 0)),
            ("quantity_complete", 3.0),
            ("quantity_scrapped", 1.0),
        ],
    )
    def test_a_ready_operation_showing_any_other_evidence_is_not_demoted(self, db_session: Session, field, value):
        wo, ops = self._pooled_pair_with_a_blocked_ready_op(db_session)
        setattr(ops[1], field, value)
        db_session.commit()

        demoted = demote_operations_for_sequencing(db_session, wo)

        assert demoted == [], f"{field} is labor evidence"
        assert _statuses(db_session, ops)[1] == OperationStatus.READY

    def test_an_untouched_blocked_ready_operation_is_demoted(self, db_session: Session):
        """THE CONTROL: with no evidence at all, the same op does move."""
        wo, ops = self._pooled_pair_with_a_blocked_ready_op(db_session)

        demoted = demote_operations_for_sequencing(db_session, wo)

        assert [op.id for op in demoted] == [ops[1].id]
        db_session.commit()
        assert _statuses(db_session, ops) == [OperationStatus.READY, OperationStatus.PENDING]

    def test_a_pooled_work_order_is_a_no_op(self, db_session: Session):
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )

        assert demote_operations_for_sequencing(db_session, wo) == []
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 2

    def test_a_laser_work_order_is_a_no_op(self, db_session: Session):
        laser = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=True,
            work_centers=[laser, laser],
            statuses=[OperationStatus.READY, OperationStatus.READY],
            work_order_type=WorkOrderType.LASER_CUTTING.value,
        )

        assert demote_operations_for_sequencing(db_session, wo) == []
        assert _statuses(db_session, ops) == [OperationStatus.READY] * 2


# --------------------------------------------------------------------------- #
# 6. Tenant isolation on the two new queries (invariant 1)
# --------------------------------------------------------------------------- #
class TestTenantIsolationOnTheNewQueries:
    """Both new queries read rows that decide whether live work gets taken off the board.

    ``operations_worked_out_of_sequence`` reads TimeEntry (labor evidence) and
    ``demote_operations_for_sequencing`` reads AND WRITES operation rows, so a leak here
    is not only a disclosure -- another tenant's clock-in could block this tenant's flip,
    or this tenant's flip could write another tenant's operation.
    """

    def _sequenced_wo_with_a_blocked_op(self, db: Session):
        wc = make_work_center(db)
        wo, ops = make_wo(
            db,
            sequential_operations=True,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        return wo, ops

    def test_another_companys_time_entry_is_not_labor_evidence(self, db_session: Session):
        """A company-B clock-in against a company-A operation must not be read at all --
        if it were, another tenant could refuse this tenant's flip."""
        _ensure_company(db_session, COMPANY_B)
        other_tenant_user = make_user(db_session, company_id=COMPANY_B)
        wo, ops = self._sequenced_wo_with_a_blocked_op(db_session)
        add_time_entry(db_session, user=other_tenant_user, op=ops[1], company_id=COMPANY_B)

        assert operations_worked_out_of_sequence(db_session, wo) == []

    def test_this_companys_time_entry_is_labor_evidence(self, db_session: Session):
        """THE CONTROL: the identical row under company A IS read, so the test above is
        measuring the tenant filter and not a broken query."""
        user = make_user(db_session)
        wo, ops = self._sequenced_wo_with_a_blocked_op(db_session)
        add_time_entry(db_session, user=user, op=ops[1], company_id=COMPANY_A)

        stranded = operations_worked_out_of_sequence(db_session, wo)

        assert [op.id for op in stranded] == [ops[1].id]

    def test_operations_worked_out_of_sequence_reads_no_other_companys_operation(self, db_session: Session):
        """A mis-parented operation row is dropped from the read, so it can neither be
        reported as stranded nor supply a predecessor snapshot."""
        _ensure_company(db_session, COMPANY_B)
        wo, ops = self._sequenced_wo_with_a_blocked_op(db_session)
        ops[1].status = OperationStatus.IN_PROGRESS
        ops[1].company_id = COMPANY_B
        db_session.commit()

        assert operations_worked_out_of_sequence(db_session, wo) == []

    def test_demotion_never_writes_another_companys_operation_row(self, db_session: Session):
        """The write set is scoped to the work order's OWN company_id."""
        _ensure_company(db_session, COMPANY_B)
        wo, ops = self._sequenced_wo_with_a_blocked_op(db_session)
        ops[1].company_id = COMPANY_B
        db_session.commit()

        demoted = demote_operations_for_sequencing(db_session, wo)
        db_session.commit()

        assert demoted == []
        assert _statuses(db_session, ops) == [OperationStatus.READY, OperationStatus.READY]

    def test_the_flip_over_http_never_touches_another_companys_operation(self, client: TestClient, db_session: Session):
        """End to end, through the endpoint that actually performs the flip."""
        _ensure_company(db_session, COMPANY_B)
        admin = make_user(db_session)
        wc = make_work_center(db_session)
        wo, ops = make_wo(
            db_session,
            sequential_operations=False,
            work_centers=[wc] * 3,
            statuses=[OperationStatus.READY] * 3,
        )
        ops[2].company_id = COMPANY_B
        db_session.commit()

        resp = put_work_order(client, admin, wo, db_session, sequential_operations=True)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _statuses(db_session, ops) == [
            OperationStatus.READY,
            OperationStatus.PENDING,  # company A's own blocked op: demoted
            OperationStatus.READY,  # company B's row: never read, never written
        ]
        assert demotion_audit_rows(db_session, ops[2].id) == []
