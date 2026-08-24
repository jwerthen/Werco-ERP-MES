"""Live Shop Activity's fraction must keep BOTH halves on ONE scope.

``GET /shop-floor/dashboard`` emits an ``active_assignments`` list, and each entry is
one operator clocked into ONE operation. The dashboard panel that renders it divides a
numerator by a denominator -- and until this field there was no operation-scoped
denominator in the payload to divide by, so the client reached for the sibling
``work_order.quantity_ordered``.

On production WO-20260821-001 (a laser job: 21 nests, ``quantity_ordered`` = 102 = the
sum of every nest's ``planned_runs``, ``quantity_complete`` = 38) that printed a nest
sitting at 0 of 2 runs as **0/102** -- neither the nest's fraction (0/2, what Dispatch,
the WO routing table and Shop Floor Operations all showed) nor the job's (38/102).

So the payload now carries ``operation.quantity_ordered``, resolved by the ONE server
rule ``operation_target_quantity``. These tests pin the three cases that make it usable:

* a LASER NEST assignment is targeted at **that nest's own** ``planned_runs`` -- proven
  with two nests of DIFFERENT size (2 and 16) on one WO ordered 18, so a per-nest number
  can't be confused with a shared one;
* an ordinary routing operation still inherits the work order's figure (no regression);
* an entry with **no operation** (indirect/setup labor) yields ``None``, **not** ``0.0``
  -- that null-vs-zero distinction is exactly what lets the client tell "this row has no
  operation, fall back to the work order's OWN pair" apart from "this operation's target
  is zero", and so is what keeps it from mixing scopes again.

The work-order figure is asserted UNCHANGED beside each of them: the whole-job number is
still there to be shown, it just is not the operation's denominator.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.laser_nest import LaserNest, LaserNestPackage
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

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
DASHBOARD = "/api/v1/shop-floor/dashboard"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int = COMPANY_A) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"dash-scope-{n}@co{company_id}.test",
        employee_id=f"DASHSCOPE-{n:05d}",
        first_name="Dash",
        last_name=f"Scope{n}",
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


def make_work_center(db: Session, *, wc_type: str = "laser", company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"DASHSCOPE-WC-{n}",
        code=f"DASHSCOPE-WC-{n}",
        work_center_type=wc_type,
        description="dashboard operation-scope fixture work center",
        hourly_rate=100.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_laser_nest_wo(
    db: Session,
    wc: WorkCenter,
    *,
    planned_runs: list[int],
    company_id: int = COMPANY_A,
) -> tuple[WorkOrder, list[WorkOrderOperation], list[LaserNest]]:
    """A laser dispatch-pool WO shaped exactly like the production one.

    ``quantity_ordered`` is the SUM of every nest's ``planned_runs`` (that is what the
    importer derives it from), and each nest's backing operation carries its OWN sheet
    count in ``component_quantity`` -- which is what ``laser_nest_service`` writes
    (``component_quantity=float(nest.planned_runs)``) and what the target rule reads
    first. Real ``LaserNest``/``LaserNestPackage`` rows are created alongside so the
    fixture cannot silently drift from the shape the importer produces.
    """
    _ensure_company(db, company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"DASHSCOPE-LASER-{n:05d}",
        customer_name="Miratech",
        # Part-less standalone laser WO -- allowed only for work_order_type
        # 'laser_cutting' (see the WorkOrder CHECK constraint).
        part_id=None,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        quantity_ordered=float(sum(planned_runs)),
        quantity_complete=0.0,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=3,
        due_date=date.today() + timedelta(days=14),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()

    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=None,
        child_work_order_id=wo.id,
        package_name=f"DASHSCOPE-PKG-{n}",
        import_status="imported",
    )
    db.add(package)
    db.flush()

    ops: list[WorkOrderOperation] = []
    nests: list[LaserNest] = []
    for index, runs in enumerate(planned_runs, start=1):
        op = WorkOrderOperation(
            work_order_id=wo.id,
            work_center_id=wc.id,
            company_id=company_id,
            sequence=index * 10,
            operation_number=f"Nest {index}",
            name=f"Laser Cut - N{index}",
            component_quantity=float(runs),
            quantity_complete=0.0,
            # Laser nest ops are born READY (dispatch pool); the ones an operator is
            # clocked into are moved to IN_PROGRESS by the caller.
            status=OperationStatus.READY,
        )
        db.add(op)
        db.flush()
        nest = LaserNest(
            company_id=company_id,
            package_id=package.id,
            work_order_operation_id=op.id,
            nest_name=f"N{index}",
            cnc_file_name=f"N{index}_QTY{runs}.nc",
            planned_runs=runs,
            completed_runs=0.0,
        )
        db.add(nest)
        ops.append(op)
        nests.append(nest)
    db.commit()
    for op in ops:
        db.refresh(op)
    db.refresh(wo)
    return wo, ops, nests


def make_routed_wo(
    db: Session,
    wc: WorkCenter,
    *,
    quantity_ordered: float = 100.0,
    company_id: int = COMPANY_A,
) -> tuple[WorkOrder, WorkOrderOperation]:
    """An ordinary (non-laser) routing WO: one op, NO component_quantity."""
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"DASHSCOPE-P-{n}",
        name=f"Bracket {n}",
        description="dashboard operation-scope fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"DASHSCOPE-WO-{n:05d}",
        customer_name="Acme Aero",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_complete=0.0,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=3,
        due_date=date.today() + timedelta(days=14),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        company_id=company_id,
        sequence=10,
        operation_number="10",
        name="Deburr",
        quantity_complete=0.0,
        status=OperationStatus.IN_PROGRESS,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    db.refresh(wo)
    return wo, op


def open_entry(
    db: Session,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation | None,
    *,
    entry_type: TimeEntryType = TimeEntryType.RUN,
    work_center_id: int | None = None,
    company_id: int = COMPANY_A,
) -> TimeEntry:
    """An OPEN (clock_out NULL) labor row -- the only kind the dashboard surfaces."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id if op else None,
        work_center_id=work_center_id if work_center_id is not None else (op.work_center_id if op else None),
        entry_type=entry_type,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        quantity_produced=0.0,
        company_id=company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def assignments_by_entry_id(client: TestClient, viewer: User) -> dict[int, dict]:
    resp = client.get(DASHBOARD, headers=headers_for(viewer))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {a["time_entry_id"]: a for a in resp.json()["active_assignments"]}


# --------------------------------------------------------------------------- #
# A. Laser nest: the denominator is THAT nest, not the whole job
# --------------------------------------------------------------------------- #
class TestLaserNestAssignmentIsNestScoped:
    def test_each_nest_assignment_carries_its_own_planned_runs(self, client: TestClient, db_session: Session):
        """Two nests of different size on ONE WO: each assignment gets its own number."""
        wc = make_work_center(db_session)
        wo, ops, nests = make_laser_nest_wo(db_session, wc, planned_runs=[2, 16])
        small_op, big_op = ops
        # Mirror the floor: two operators, one on each nest, both clocked in.
        op_a = make_user(db_session)
        op_b = make_user(db_session)
        small_op.status = OperationStatus.IN_PROGRESS
        big_op.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        entry_small = open_entry(db_session, op_a, wo, small_op)
        entry_big = open_entry(db_session, op_b, wo, big_op)

        viewer = make_user(db_session, role=UserRole.MANAGER)
        found = assignments_by_entry_id(client, viewer)

        # The WO is ordered for the SUM of the nests -- the production shape.
        assert float(wo.quantity_ordered) == 18.0
        assert [n.planned_runs for n in nests] == [2, 16]

        small = found[entry_small.id]
        big = found[entry_big.id]

        # THE FIX: nest scope, per assignment -- not 18 on both.
        assert small["operation"]["quantity_ordered"] == 2.0
        assert big["operation"]["quantity_ordered"] == 16.0
        assert small["operation"]["quantity_ordered"] != float(wo.quantity_ordered)
        assert big["operation"]["quantity_ordered"] != float(wo.quantity_ordered)

        # The raw input the client mirror of the same rule reads is carried too.
        assert small["operation"]["component_quantity"] == 2.0
        assert big["operation"]["component_quantity"] == 16.0

        # The whole-job figure is UNCHANGED and still available beside it -- it just
        # is not this operation's denominator any more.
        assert small["work_order"]["quantity_ordered"] == 18.0
        assert big["work_order"]["quantity_ordered"] == 18.0
        assert small["work_order"]["id"] == big["work_order"]["id"] == wo.id

    def test_the_production_shape_a_nest_at_zero_of_two_reads_zero_of_two(
        self, client: TestClient, db_session: Session
    ):
        """WO-20260821-001, reduced: 102 ordered, 38 done, the running nest at 0 of 2.

        The panel divides ``operation.quantity_complete`` by this denominator, so the
        assertion below is the byte the defect got wrong: 2, not 102.
        """
        wc = make_work_center(db_session)
        # 21 nests summing to 102, with nest 11 planned for 2 runs (the production row).
        planned = [4] * 10 + [2] + [6] * 10  # 40 + 2 + 60 = 102
        wo, ops, _ = make_laser_nest_wo(db_session, wc, planned_runs=planned)
        assert float(wo.quantity_ordered) == 102.0
        nest_11 = ops[10]
        assert float(nest_11.component_quantity) == 2.0
        nest_11.status = OperationStatus.IN_PROGRESS
        # Whole-job progress, as on the real job. (Set directly: the header figure is
        # a rollup this test does not exercise -- the point is only that it is NOT the
        # operation's denominator.)
        wo.quantity_complete = 38.0
        db_session.commit()
        operator = make_user(db_session)
        entry = open_entry(db_session, operator, wo, nest_11)

        viewer = make_user(db_session, role=UserRole.MANAGER)
        assignment = assignments_by_entry_id(client, viewer)[entry.id]

        assert assignment["operation"]["quantity_complete"] == 0.0
        assert assignment["operation"]["quantity_ordered"] == 2.0  # NOT 102
        # Both halves of the WO-level pair survive, unchanged.
        assert assignment["work_order"]["quantity_ordered"] == 102.0
        assert assignment["work_order"]["quantity_complete"] == 38.0


# --------------------------------------------------------------------------- #
# B. No regression, and the null-vs-zero distinction
# --------------------------------------------------------------------------- #
class TestOrdinaryOperationAndNoOperation:
    def test_plain_routing_operation_still_inherits_the_work_order_figure(
        self, client: TestClient, db_session: Session
    ):
        wc = make_work_center(db_session, wc_type="welding")
        wo, op = make_routed_wo(db_session, wc, quantity_ordered=100.0)
        operator = make_user(db_session)
        entry = open_entry(db_session, operator, wo, op)

        viewer = make_user(db_session, role=UserRole.MANAGER)
        assignment = assignments_by_entry_id(client, viewer)[entry.id]

        # An op with no component_quantity processes the whole order -- the rule's
        # second tier. This is the shape every non-laser row on the panel has, and it
        # must read exactly as it did before the field existed.
        assert assignment["operation"]["component_quantity"] in (None, 0, 0.0)
        assert assignment["operation"]["quantity_ordered"] == 100.0
        assert assignment["work_order"]["quantity_ordered"] == 100.0

    def test_entry_with_no_operation_yields_null_not_zero(self, client: TestClient, db_session: Session):
        """Indirect / setup labor: ``quantity_ordered`` must be NULL, never 0.0.

        The client tells the two apart to keep both halves of its fraction on one
        scope: NULL means "no operation here, use the work order's OWN pair", while
        0.0 would mean "this operation is targeted at zero" and would leave the panel
        dividing an operation numerator by a work-order denominator all over again.
        """
        wc = make_work_center(db_session, wc_type="welding")
        wo, _op = make_routed_wo(db_session, wc, quantity_ordered=100.0)
        operator = make_user(db_session)
        entry = open_entry(
            db_session,
            operator,
            wo,
            None,  # no operation
            entry_type=TimeEntryType.SETUP,
            work_center_id=wc.id,
        )

        viewer = make_user(db_session, role=UserRole.MANAGER)
        assignment = assignments_by_entry_id(client, viewer)[entry.id]

        assert assignment["operation"]["id"] is None
        assert assignment["operation"]["quantity_ordered"] is None
        # Explicit: NULL, not the falsy zero that reads the same in a truthiness test
        # but NOT in the client's ``??`` fallback.
        assert assignment["operation"]["quantity_ordered"] != 0
        assert assignment["operation"]["quantity_ordered"] != 0.0
        assert assignment["operation"]["component_quantity"] is None
        # The work-order pair is still there -- it is what such a row falls back to.
        assert assignment["work_order"]["quantity_ordered"] == 100.0


# --------------------------------------------------------------------------- #
# C. Through the real production-posting path
# --------------------------------------------------------------------------- #
class TestAfterRealProductionPosting:
    def test_one_run_of_two_reads_one_over_two(self, client: TestClient, db_session: Session):
        """Post 1 run through the REAL verb, then read the dashboard: 1 over 2.

        ``POST /shop-floor/operations/{id}/production`` is the additive floor verb --
        it credits produced quantity and leaves the operator clocked in, which is what
        keeps the row on this panel at all (the dashboard surfaces OPEN entries only,
        so completing the operation would remove the row instead of updating it).
        """
        wc = make_work_center(db_session)
        wo, ops, _ = make_laser_nest_wo(db_session, wc, planned_runs=[2, 16])
        nest = ops[0]
        nest.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        operator = make_user(db_session)
        entry = open_entry(db_session, operator, wo, nest)

        resp = client.post(
            f"/api/v1/shop-floor/operations/{nest.id}/production",
            json={"quantity_complete_delta": 1},
            headers=headers_for(operator),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        viewer = make_user(db_session, role=UserRole.MANAGER)
        assignment = assignments_by_entry_id(client, viewer)[entry.id]

        assert assignment["operation"]["quantity_complete"] == 1.0
        assert assignment["operation"]["quantity_ordered"] == 2.0
        # …and the whole job did NOT become the denominator on the way through.
        assert assignment["work_order"]["quantity_ordered"] == 18.0

    def test_posting_past_the_nest_target_is_refused_at_the_nest_number(self, client: TestClient, db_session: Session):
        """Corroborates the denominator from the write side: the cap is 2, not 18.

        ``report_operation_production`` guards over-completion with the same
        ``operation_target_quantity`` the dashboard now serves, so a 3rd run on a
        2-run nest is refused even though the WO is ordered for 18. If this ever
        returned 200, the number the panel prints would not be the number the floor
        is held to.
        """
        wc = make_work_center(db_session)
        wo, ops, _ = make_laser_nest_wo(db_session, wc, planned_runs=[2, 16])
        nest = ops[0]
        nest.status = OperationStatus.IN_PROGRESS
        db_session.commit()
        operator = make_user(db_session)
        open_entry(db_session, operator, wo, nest)

        resp = client.post(
            f"/api/v1/shop-floor/operations/{nest.id}/production",
            json={"quantity_complete_delta": 3},
            headers=headers_for(operator),
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "2" in resp.json()["detail"]
        assert float(wo.quantity_ordered) == 18.0
