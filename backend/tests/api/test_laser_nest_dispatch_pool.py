"""Laser-nest dispatch pools: whole-package READY, no predecessor gating, queue scope.

Covers the ready-dispatch behavior on ``laser_cutting`` nest work orders:

  - every nest op of an import is born READY (not just the first);
  - laser WOs are DISPATCH POOLS (``is_laser_dispatch_work_order``): the
    shop-floor clock-in / start / complete and office start / complete
    predecessor gates never block one nest on another -- INCLUDING when the
    nests sit on DIFFERENT work centers (the cross-WC case the old
    same-work-center exemption missed);
  - both release helpers (``release_first_ready_operation`` /
    ``release_next_ready_operation``) promote ALL PENDING nest ops to READY
    (healing laser WOs imported before whole-package-ready), emit
    ``operation_ready`` events, and return the lowest-sequence promoted op --
    while a conventional CROSS-work-center routing keeps one-at-a-time
    promotion;
  - the work-center queue surfaces every READY nest of a fresh import, and
    only the nests assigned to THAT work center when a package is spread
    across two work centers.

Non-laser CROSS-work-center gating is pinned as a regression alongside each
laser exemption so the dispatch-pool rule can never silently widen. Note what
the pins are NOT: since the general promotion rule adopted clock-in's
``allow_same_work_center=True`` semantics, ops sharing a work center promote
together on EVERY work order. What still makes a laser WO special is that it
drops cross-work-center gating too -- so every non-laser pin here uses a route
whose steps sit at DIFFERENT work centers, or it would pin nothing.

Offline by contract: CNC-file packages (filename inference) and the PDF
confirm-and-commit path (no extractor call) only; the AI extractor is patched
to fail the test if ever invoked.
"""

import io
import json
import zipfile
from datetime import datetime

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.time_entry import TimeEntry
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
    is_laser_dispatch_work_order,
    release_first_ready_operation,
    release_next_ready_operation,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
# Second tenant, used only to prove a foreign TimeEntry is not labor evidence.
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"dispatch-{n}@co{company_id}.test",
        employee_id=f"DISP-{n:05d}",
        first_name="Dispatch",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_laser_work_center(db: Session, *, company_id: int = COMPANY_A, name: str = None) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=name or f"Laser Cutter {n}",
        code=f"LASER-DP-{n}",
        work_center_type="laser",
        description="laser fixture",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _cnc_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "M30")
    return buf.getvalue()


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Keep storage + laser package roots hermetic (same as the PDF-import tests)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Every path exercised here is AI-free; any extractor call is a bug."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("dispatch-pool laser-nest tests must not call the AI extractor"),
    )


def _standalone_import(client, headers, zip_bytes, *, rows=None, work_center_id=None, name="nests.zip"):
    data = {}
    if rows is not None:
        data["rows"] = json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _import_three_nest_wo(client, admin, wc) -> dict:
    """Standalone 3-nest CNC import (planned runs 2/3/4); returns the WO dict."""
    resp = _standalone_import(
        client,
        headers_for(admin),
        _cnc_zip("N1_QTY2.nc", "N2_QTY3.nc", "N3_QTY4.nc"),
        work_center_id=wc.id,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


def _import_cross_wc_wo(client, admin, wc_main, wc_other) -> dict:
    """PDF confirm-and-commit import: nests 1-2 on ``wc_main``, nest 3 on ``wc_other``."""
    rows = [
        {"source_file": "n1.pdf", "cnc_number": "N1", "planned_runs": 2},
        {"source_file": "n2.pdf", "cnc_number": "N2", "planned_runs": 3},
        {"source_file": "n3.pdf", "cnc_number": "N3", "planned_runs": 4, "work_center_id": wc_other.id},
    ]
    resp = _standalone_import(
        client,
        headers_for(admin),
        _pdf_zip("n1.pdf", "n2.pdf", "n3.pdf"),
        rows=rows,
        work_center_id=wc_main.id,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


def _ops_by_sequence(child: dict) -> list[dict]:
    return sorted(child["operations"], key=lambda op: op["sequence"])


def make_routed_wo(
    db: Session,
    *,
    work_centers: list[WorkCenter],
    statuses: list[OperationStatus],
    company_id: int = COMPANY_A,
) -> tuple[WorkOrder, list[WorkOrderOperation]]:
    """A NON-laser (production) WO with one op per status, sequences 10/20/30...

    ``work_centers[i]`` hosts op ``i`` (repeat the same WC for a same-WC route).
    """
    n = _next()
    part = Part(
        part_number=f"PRT-DP-{n}",
        name="Routed part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-DP-{n}",
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.RELEASED,
        priority=3,
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
            name=f"Routed step {index}",
            component_quantity=5.0,
            status=op_status,
        )
        db.add(op)
        ops.append(op)
    db.commit()
    for op in ops:
        db.refresh(op)
    db.refresh(wo)
    return wo, ops


def make_laser_pool_wo(
    db: Session,
    *,
    work_center: WorkCenter,
    statuses: list[OperationStatus],
    company_id: int = COMPANY_A,
) -> tuple[WorkOrder, list[WorkOrderOperation]]:
    """A part-less ``laser_cutting`` WO built directly in the DB (service-level tests)."""
    n = _next()
    wo = WorkOrder(
        work_order_number=f"WO-LP-{n}",
        part_id=None,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        quantity_ordered=len(statuses),
        status=WorkOrderStatus.RELEASED,
        priority=3,
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
            operation_number=f"Nest {index}",
            name=f"Laser Cut - N{index}",
            component_quantity=1.0,
            status=op_status,
            operation_group="LASER",
        )
        db.add(op)
        ops.append(op)
    db.commit()
    for op in ops:
        db.refresh(op)
    db.refresh(wo)
    return wo, ops


def _clock_in(client, user, *, wo_id: int, op: dict | WorkOrderOperation):
    op_id = op["id"] if isinstance(op, dict) else op.id
    wc_id = op["work_center_id"] if isinstance(op, dict) else op.work_center_id
    return client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers_for(user),
        json={"work_order_id": wo_id, "operation_id": op_id, "work_center_id": wc_id, "entry_type": "run"},
    )


def _ready_events(db: Session, operation_id: int) -> list[OperationalEvent]:
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_ready", OperationalEvent.operation_id == operation_id)
        .all()
    )


# --------------------------------------------------------------------------- #
# Whole-package READY at import
# --------------------------------------------------------------------------- #
class TestWholePackageReady:
    def test_import_creates_every_nest_op_ready(self, client, db_session):
        """All nest ops -- not just the first -- are born READY on import."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)

        child = _import_three_nest_wo(client, admin, wc)

        assert len(child["operations"]) == 3
        assert [op["status"] for op in _ops_by_sequence(child)] == ["ready"] * 3

        db_ops = (
            db_session.query(WorkOrderOperation)
            .filter(WorkOrderOperation.work_order_id == child["id"])
            .order_by(WorkOrderOperation.sequence)
            .all()
        )
        assert [op.status for op in db_ops] == [OperationStatus.READY] * 3

    def test_pdf_rows_import_also_creates_every_op_ready(self, client, db_session):
        """The PDF confirm-and-commit path births all ops READY too -- including
        a nest routed to a DIFFERENT work center by a per-row override."""
        admin = make_user(db_session)
        wc_main = make_laser_work_center(db_session)
        wc_other = make_laser_work_center(db_session)

        child = _import_cross_wc_wo(client, admin, wc_main, wc_other)

        ops = _ops_by_sequence(child)
        assert [op["status"] for op in ops] == ["ready"] * 3
        assert [op["work_center_id"] for op in ops] == [wc_main.id, wc_main.id, wc_other.id]


# --------------------------------------------------------------------------- #
# Dispatch-pool gating: nests never predecessor-block each other
# --------------------------------------------------------------------------- #
class TestDispatchPoolGating:
    def test_clock_in_on_last_nest_with_earlier_nests_incomplete(self, client, db_session):
        """Same-WC pool: nest 3 is clock-in-able while nests 1-2 have no progress."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)
        last_op = _ops_by_sequence(child)[-1]

        resp = _clock_in(client, operator, wo_id=child["id"], op=last_op)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        op = db_session.get(WorkOrderOperation, last_op["id"])
        assert op.status == OperationStatus.IN_PROGRESS
        earlier = (
            db_session.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.work_order_id == child["id"],
                WorkOrderOperation.id != last_op["id"],
            )
            .all()
        )
        assert all(o.status == OperationStatus.READY for o in earlier)

    def test_clock_in_on_cross_wc_nest_not_blocked(self, client, db_session):
        """THE cross-WC case: nest 3 sits on a different laser than nests 1-2.
        The old exemption only skipped SAME-work-center predecessors, so this
        clock-in used to 400; the dispatch-pool rule allows it."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc_main = make_laser_work_center(db_session)
        wc_other = make_laser_work_center(db_session)
        child = _import_cross_wc_wo(client, admin, wc_main, wc_other)
        cross_op = _ops_by_sequence(child)[-1]
        assert cross_op["work_center_id"] == wc_other.id

        resp = _clock_in(client, operator, wo_id=child["id"], op=cross_op)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, cross_op["id"]).status == OperationStatus.IN_PROGRESS

    def test_shop_floor_start_on_cross_wc_nest_not_blocked(self, client, db_session):
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc_main = make_laser_work_center(db_session)
        wc_other = make_laser_work_center(db_session)
        child = _import_cross_wc_wo(client, admin, wc_main, wc_other)
        cross_op = _ops_by_sequence(child)[-1]

        resp = client.put(f"/api/v1/shop-floor/operations/{cross_op['id']}/start", headers=headers_for(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, cross_op["id"]).status == OperationStatus.IN_PROGRESS

    def test_office_start_and_complete_on_cross_wc_nest_not_blocked(self, client, db_session):
        """The office twins skip the gate for laser WOs too (they never had the
        same-WC exemption, so pre-change even a same-WC nest was blocked here)."""
        admin = make_user(db_session)
        wc_main = make_laser_work_center(db_session)
        wc_other = make_laser_work_center(db_session)
        child = _import_cross_wc_wo(client, admin, wc_main, wc_other)
        cross_op = _ops_by_sequence(child)[-1]

        resp = client.post(f"/api/v1/work-orders/operations/{cross_op['id']}/start", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        resp = client.post(
            f"/api/v1/work-orders/operations/{cross_op['id']}/complete",
            params={"quantity_complete": 4},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, cross_op["id"]).status == OperationStatus.COMPLETE
        # Nests 1-2 are untouched (still dispatchable) and the WO stays open.
        others = [op for op in _ops_by_sequence(child)[:-1]]
        for other in others:
            assert db_session.get(WorkOrderOperation, other["id"]).status == OperationStatus.READY
        assert db_session.get(WorkOrder, child["id"]).status == WorkOrderStatus.IN_PROGRESS

    def test_non_laser_cross_wc_gating_unchanged(self, client, db_session):
        """Regression: a production WO's op 2 on ANOTHER work center is still
        predecessor-blocked on every path the laser exemption touched."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc_a = make_laser_work_center(db_session)
        wc_b = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc_a, wc_b],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        blocked_op = ops[1]

        assert is_laser_dispatch_work_order(wo) is False

        resp = _clock_in(client, operator, wo_id=wo.id, op=blocked_op)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["detail"] == "Previous operations must be completed first"

        resp = client.put(f"/api/v1/shop-floor/operations/{blocked_op.id}/start", headers=headers_for(operator))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["detail"] == "Previous operations must be completed first"

        resp = client.post(f"/api/v1/work-orders/operations/{blocked_op.id}/start", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

        resp = client.post(
            f"/api/v1/work-orders/operations/{blocked_op.id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, blocked_op.id).status == OperationStatus.READY

    def test_office_and_floor_agree_on_the_same_work_center_exemption(self, client, db_session):
        """REPLACES ``test_non_laser_same_wc_exemption_unchanged``, which pinned the office
        verbs at 400 on a same-work-center operation.

        That pin was deliberately retired, not deleted: the office start/complete verbs used
        to carry their own inline ``allow_same_work_center=False`` copy of the predecessor
        gate while the shop-floor twins passed ``True``, so the office refused an operation
        the floor would happily start. Both now route through the one shared predicate
        (``operation_action_gates.operation_blocked_by_predecessors``). This test pins the
        NEW contract -- office and floor give the same answer -- so a future divergence
        fails here rather than surfacing as work that is visible, clock-in-able, and
        un-completable from the office.
        """
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)

        # One work order per verb: starting an operation moves it to IN_PROGRESS, so the
        # two calls would otherwise contend over the same row and the second would be
        # refused for a reason that has nothing to do with the predecessor gate.
        _, office_ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        _, floor_ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )

        # Office start: same work center, out of sequence -> now ALLOWED.
        resp = client.post(f"/api/v1/work-orders/operations/{office_ops[1].id}/start", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # Shop-floor start agrees (it always did).
        resp = client.put(f"/api/v1/shop-floor/operations/{floor_ops[1].id}/start", headers=headers_for(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_office_complete_allows_same_work_center_out_of_sequence(self, client, db_session):
        """The office COMPLETE verb's half of the same contract."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[1].id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.COMPLETE
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.READY

    def test_office_verbs_still_block_across_work_centers(self, client, db_session):
        """The cross-work-center gate is untouched on BOTH office verbs.

        This is the half of the old pin that still means something: routing the office
        verbs through the shared predicate widened the same-work-center case ONLY.
        """
        admin = make_user(db_session)
        wc_a = make_laser_work_center(db_session)
        wc_b = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc_a, wc_b],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        blocked_op = ops[1]

        resp = client.post(f"/api/v1/work-orders/operations/{blocked_op.id}/start", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["detail"] == "Previous operations must be completed first"

        resp = client.post(
            f"/api/v1/work-orders/operations/{blocked_op.id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["detail"] == "Previous operations must be completed first"

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, blocked_op.id).status == OperationStatus.READY

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    def test_office_start_refuses_read_only_and_operator_roles(self, client, db_session, role):
        """The office START verb is role-gated to match its office COMPLETE twin.

        It previously took ``get_current_user`` alone, so ANY authenticated tenant user --
        a Viewer included -- could stamp actual_start / started_by, move the work order to
        IN_PROGRESS and write the audit chain. The predecessor gate hid it: almost nothing
        a read-only user could reach was startable. Same-work-center promotion made exactly
        those operations reachable, so the hole had to close with it.

        OPERATOR is refused here too and that is correct, not collateral: operators start
        work through the shop-floor verb, which stays open to them (asserted above).
        """
        user = make_user(db_session, role=role)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )

        resp = client.post(f"/api/v1/work-orders/operations/{ops[1].id}/start", headers=headers_for(user))
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY

    def test_a_pending_operation_still_cannot_be_completed(self, client, db_session):
        """Pins the OTHER side of the line promotion moves work across.

        A READY operation can be completed by a single office call, at full quantity, with
        no TimeEntry and no labor evidence behind it. Promotion means a batch work order
        now offers 18 such operations where it offered 1. That behavior is DELIBERATE on the
        office verb -- the owner kept supervisors/quality able to close an operation from the
        desk, including one the floor never clocked, and the floor-only labor gate records the
        asymmetry (see complete_blockers). What is pinned here is the refusal of the same call
        on a PENDING operation, so both sides of the boundary are covered and any future move
        of it has to be deliberate.
        """
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING],
        )

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0].id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "pending" in resp.json()["detail"].lower()

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.PENDING


# --------------------------------------------------------------------------- #
# Labor-evidence gate on the FLOOR completion verb
# --------------------------------------------------------------------------- #
class TestFloorCompletionNeedsLaborEvidence:
    """``POST /shop-floor/operations/{id}/complete`` refuses an operation nobody worked.

    The gate is ANY ``TimeEntry`` on the operation, open or closed -- deliberately not
    "the caller is clocked in right now", which would refuse the two flows below. Both
    are pinned here precisely because they are the reason this shape was chosen.
    """

    def test_operation_with_no_labor_cannot_be_completed_from_the_floor(self, client, db_session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc],
            statuses=[OperationStatus.READY],
        )

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[0].id}/complete",
            headers=headers_for(operator),
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == (
            "Clock in to this operation before completing it — no one has clocked in to it yet."
        )

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.READY

    def test_kiosk_shaped_flow_still_completes(self, client, db_session):
        """FLOW 1 (``OperatorKiosk.tsx::handleComplete``): clock in -> clock OUT -> complete.

        The operator's entry is CLOSED by the time the completion lands, which is exactly
        what an open-entry check would have refused. A closed entry is still labor.
        """
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc],
            statuses=[OperationStatus.READY],
        )

        clock_in = _clock_in(client, operator, wo_id=wo.id, op=ops[0])
        assert clock_in.status_code == status.HTTP_200_OK, clock_in.text
        entry_id = clock_in.json()["id"]

        # The kiosk books the SESSION's pieces on the clock-out, then asserts the target
        # on the completion call. Booking the full target here would let the clock-out
        # finish the operation on its own and the completion would never reach the gate.
        clock_out = client.post(
            f"/api/v1/shop-floor/clock-out/{entry_id}",
            headers=headers_for(operator),
            json={"quantity_produced": 2},
        )
        assert clock_out.status_code == status.HTTP_200_OK, clock_out.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status != OperationStatus.COMPLETE
        assert db_session.get(TimeEntry, entry_id).clock_out is not None, "the caller's entry is CLOSED"

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[0].id}/complete",
            headers=headers_for(operator),
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.COMPLETE

    def test_crew_shaped_completion_by_an_operator_holding_no_entry(self, client, db_session):
        """FLOW 2 (``CrewStationKiosk.tsx::handleCompleteBadge``): the completing badge
        holds no entry of its own while the CREW's entries are open.

        Closing out the crew's work is what this endpoint is for -- it auto-closes their
        entries and reports who was clocked out -- so the completer is routinely not one
        of them.
        """
        crew_member = make_user(db_session, role=UserRole.OPERATOR)
        lead = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc],
            statuses=[OperationStatus.READY],
        )

        # The crew member is clocked in; the lead never is.
        assert _clock_in(client, crew_member, wo_id=wo.id, op=ops[0]).status_code == status.HTTP_200_OK

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[0].id}/complete",
            headers=headers_for(lead),
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # The crew member was auto-clocked-out by the completion, as designed.
        assert [entry["user_id"] for entry in resp.json()["closed_time_entries"]] == [crew_member.id]

    def test_office_completion_is_deliberately_not_gated(self, client, db_session):
        """The asymmetry is intentional: a supervisor may close an operation from the desk
        with no labor recorded at all (cleanup). Pinned so nobody 'aligns' the two paths.
        """
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc],
            statuses=[OperationStatus.READY],
        )

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0].id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(supervisor),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.COMPLETE

    def test_another_tenants_time_entry_does_not_satisfy_the_gate(self, client, db_session):
        """The evidence lookup is company-scoped: a foreign TimeEntry is not evidence."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        foreign = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc],
            statuses=[OperationStatus.READY],
        )

        db_session.add(
            TimeEntry(
                company_id=COMPANY_B,
                user_id=foreign.id,
                work_order_id=wo.id,
                operation_id=ops[0].id,
                work_center_id=wc.id,
                clock_in=datetime.utcnow(),
            )
        )
        db_session.commit()

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[0].id}/complete",
            headers=headers_for(operator),
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "Clock in to this operation" in resp.json()["detail"]

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.READY


# --------------------------------------------------------------------------- #
# The hold carve-out: an ON_HOLD predecessor blocks its own work center too
# --------------------------------------------------------------------------- #
class TestHeldPredecessorBlocksThePool:
    """A quality/material hold outranks the same-work-center pooling exemption.

    Without the carve-out, holding item 3 of an 18-item batch would leave the other 17 on
    the dispatch board and clock-in-able -- the shop would keep building past the exact
    problem the hold was placed to stop.
    """

    def test_a_held_op_blocks_its_same_work_center_siblings_from_promoting(self, db_session):
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is None, "a held predecessor blocks promotion at its OWN work center"
        db_session.expire_all()
        for op in ops[1:]:
            assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.PENDING
            assert _ready_events(db_session, op.id) == []

    def test_a_held_op_blocks_its_same_work_center_siblings_from_clocking_in(self, client, db_session):
        """The carve-out reaches clock-in too -- deliberately, since the predicate is shared.

        A hold that took the pool off the board but still let a badge scan start a sibling
        would not be a stop at all.
        """
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.ON_HOLD, OperationStatus.READY],
        )

        resp = _clock_in(client, operator, wo_id=wo.id, op=ops[1])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == "Previous operations must be completed first"

    def test_clearing_the_hold_lets_the_siblings_promote(self, db_session):
        """Once the hold is lifted the pool is startable again.

        Note what this does NOT assert: resuming an operation does not itself re-run
        promotion (neither resume path calls a release helper), so the siblings flip on the
        next lifecycle event rather than the instant the hold clears.
        """
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        ops[0].status = OperationStatus.READY  # the resume paths' "previous state"
        db_session.commit()

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is not None and promoted.id == ops[1].id
        db_session.expire_all()
        for op in ops[1:]:
            assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.READY

    def test_a_held_op_at_another_work_center_still_blocks(self, db_session):
        """Regression: the cross-work-center block was never conditional on status."""
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[make_laser_work_center(db_session), make_laser_work_center(db_session)],
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING],
        )

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is None
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.PENDING


class TestHeldNestDoesNotBlockItsSiblings:
    """The LASER mirror of the class above -- and the answer to a production question.

    An operator held one nest of a live laser job by accident and it disappeared
    from every shop-floor screen (ON_HOLD is not a queue status). The question
    that mattered next was whether the REST of the package stalled with it: on a
    conventional routing it would, because the hold carve-out in
    ``has_incomplete_predecessors`` makes a held predecessor block its own work
    center too.

    It does NOT stall, and the reason is ordering:
    ``operation_action_gates.operation_blocked_by_predecessors`` returns False
    for a laser WO BEFORE ``has_incomplete_predecessors`` (and therefore the hold
    carve-out) is ever reached, and ``promote_ready_operations`` takes the laser
    branch before the predecessor filter. The exemption is strictly fuller than
    the same-work-center allowance and must not be collapsed into it; these tests
    pin the consequence so a future "simplification" of either branch shows up as
    a failure here rather than as a stalled package on the floor.
    """

    def test_a_held_nest_does_not_block_its_siblings_from_promoting(self, db_session):
        wc = make_laser_work_center(db_session)
        wo, ops = make_laser_pool_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is not None and promoted.id == ops[1].id
        db_session.expire_all()
        for op in ops[1:]:
            assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.READY
        # The held nest itself is untouched -- promotion never lifts a hold.
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.ON_HOLD

    def test_a_held_nest_does_not_block_its_siblings_from_clocking_in(self, client, db_session):
        """The floor keeps cutting the rest of the package while one nest is held."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_laser_pool_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.ON_HOLD, OperationStatus.READY],
        )

        resp = _clock_in(client, operator, wo_id=wo.id, op=ops[1])

        assert resp.status_code == status.HTTP_200_OK, resp.text

    def test_a_held_nest_at_another_laser_does_not_block_either(self, db_session):
        """A package spread across two lasers: still no stall.

        The same-work-center allowance would not cover this case even without the
        hold; the laser exemption is what does.
        """
        wo, ops = make_laser_pool_wo(
            db_session,
            work_center=make_laser_work_center(db_session),
            statuses=[OperationStatus.ON_HOLD, OperationStatus.PENDING],
        )
        ops[1].work_center_id = make_laser_work_center(db_session).id
        db_session.commit()

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is not None and promoted.id == ops[1].id
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY

    def test_the_held_nest_still_leaves_the_queue_while_its_siblings_stay_on_it(self, client, db_session):
        """The whole shape of the incident, end to end.

        The held nest drops off ``queue`` (and onto ``held``, so the operator can
        find it again); the siblings stay queued and keep running.
        """
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        _, ops = make_laser_pool_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.ON_HOLD, OperationStatus.READY, OperationStatus.READY],
        )

        body = client.get(
            f"/api/v1/shop-floor/work-center-queue/{wc.id}",
            headers=headers_for(operator),
        ).json()

        assert [row["operation_id"] for row in body["queue"]] == [ops[1].id, ops[2].id]
        assert [row["operation_id"] for row in body["held"]] == [ops[0].id]


# --------------------------------------------------------------------------- #
# Promotion healing: PENDING nest ops all promote to READY
# --------------------------------------------------------------------------- #
class TestPromotionHealing:
    def test_release_first_ready_promotes_all_pending_on_laser_wo(self, db_session):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_laser_pool_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_first_ready_operation(wo, db_session, user_id=admin.id)
        db_session.commit()

        assert promoted is not None and promoted.id == ops[0].id  # lowest sequence returned
        db_session.expire_all()
        for op in ops:
            assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.READY
            assert len(_ready_events(db_session, op.id)) == 1

    def test_release_first_ready_non_laser_promotes_only_first(self, db_session):
        """A conventional ROUTING -- one op per work center -- still promotes exactly one.

        This is the regression pin for the laser exemption's remaining edge. Since the
        general promotion rule adopted clock-in's ``allow_same_work_center=True``
        semantics, same-work-center ops promote together everywhere; what still separates
        a laser WO is that it drops CROSS-work-center gating too. So the pin has to be a
        cross-work-center route, or it pins nothing.
        """
        admin = make_user(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
            ],
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_first_ready_operation(wo, db_session, user_id=admin.id)
        db_session.commit()

        assert promoted is not None and promoted.id == ops[0].id
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.READY
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.PENDING
        assert db_session.get(WorkOrderOperation, ops[2].id).status == OperationStatus.PENDING
        assert len(_ready_events(db_session, ops[0].id)) == 1
        assert _ready_events(db_session, ops[1].id) == []

    def test_release_first_ready_non_laser_same_work_center_promotes_all(self, db_session):
        """The general rule: unordered items at ONE work center all become READY.

        The motivating record is a batch WO carrying ~18 press-brake items as one
        operation each on one machine. Clock-in always allowed any of them
        (``allow_same_work_center=True``); only the promotion rule disagreed, so 17 of 18
        never reached READY and the dispatch board / kiosk (READY-only) never showed them.
        This is NOT the laser exemption -- see the cross-work-center pin above, which
        still promotes exactly one.
        """
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_first_ready_operation(wo, db_session, user_id=admin.id)
        db_session.commit()

        assert promoted is not None and promoted.id == ops[0].id  # lowest sequence returned
        db_session.expire_all()
        for op in ops:
            assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.READY
            assert len(_ready_events(db_session, op.id)) == 1

    def test_release_first_ready_mixed_route_promotes_only_the_first_cell(self, db_session):
        """Mixed route: two ops share a work center, a third sits downstream elsewhere.

        Cross-work-center ordering is PRESERVED -- the downstream op waits.
        """
        admin = make_user(db_session)
        cell = make_laser_work_center(db_session)
        downstream = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[cell, cell, downstream],
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        release_first_ready_operation(wo, db_session, user_id=admin.id)
        db_session.commit()

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0].id).status == OperationStatus.READY
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY
        assert (
            db_session.get(WorkOrderOperation, ops[2].id).status == OperationStatus.PENDING
        ), "an op at a DIFFERENT work center still waits for the lower-sequence ops"
        assert _ready_events(db_session, ops[2].id) == []

    def test_release_next_ready_promotes_all_pending_on_laser_wo(self, db_session):
        """Healing path: a pre-change laser WO (nests 2-3 stranded PENDING) is
        fully promoted by the next lifecycle event's release helper."""
        wc = make_laser_work_center(db_session)
        wo, ops = make_laser_pool_wo(
            db_session,
            work_center=wc,
            statuses=[OperationStatus.COMPLETE, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is not None and promoted.id == ops[1].id  # lowest-sequence promoted op
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY
        assert db_session.get(WorkOrderOperation, ops[2].id).status == OperationStatus.READY
        assert len(_ready_events(db_session, ops[1].id)) == 1
        assert len(_ready_events(db_session, ops[2].id)) == 1

    def test_release_next_ready_non_laser_promotes_next_in_sequence_only(self, db_session):
        """Cross-work-center route: only the next op promotes (laser would promote all)."""
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
            ],
            statuses=[OperationStatus.COMPLETE, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        promoted = release_next_ready_operation(db_session, wo, ops[0])
        db_session.commit()

        assert promoted is not None and promoted.id == ops[1].id
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY
        assert db_session.get(WorkOrderOperation, ops[2].id).status == OperationStatus.PENDING
        assert _ready_events(db_session, ops[2].id) == []

    def test_completing_a_nest_heals_stranded_pending_nests(self, client, db_session):
        """End-to-end healing: an imported laser WO whose nests 2-3 were left
        PENDING (pre-whole-package-ready data) gets them all promoted to READY
        when nest 1 completes through the office endpoint."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)
        ops = _ops_by_sequence(child)

        # Simulate pre-change data: only the first nest op is READY.
        for stranded in ops[1:]:
            db_session.get(WorkOrderOperation, stranded["id"]).status = OperationStatus.PENDING
        db_session.commit()

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0]['id']}/complete",
            params={"quantity_complete": 2},  # N1's planned runs (full completion)
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[0]["id"]).status == OperationStatus.COMPLETE
        for healed in ops[1:]:
            assert db_session.get(WorkOrderOperation, healed["id"]).status == OperationStatus.READY
            assert len(_ready_events(db_session, healed["id"])) == 1

    def test_non_laser_completion_promotes_only_next(self, client, db_session):
        """End-to-end cross-work-center route: completing op1 promotes op2 only."""
        admin = make_user(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
                make_laser_work_center(db_session),
            ],
            statuses=[OperationStatus.READY, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0].id}/complete",
            params={"quantity_complete": 5},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).status == OperationStatus.READY
        assert db_session.get(WorkOrderOperation, ops[2].id).status == OperationStatus.PENDING


# --------------------------------------------------------------------------- #
# Work-center queue (kiosk) visibility
# --------------------------------------------------------------------------- #
class TestWorkCenterQueueVisibility:
    def test_queue_shows_every_nest_of_a_fresh_import(self, client, db_session):
        """All nests satisfy the queue's READY filter immediately after import."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)

        resp = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        mine = [item for item in resp.json()["queue"] if item["work_order_id"] == child["id"]]
        assert {item["operation_id"] for item in mine} == {op["id"] for op in child["operations"]}
        assert all(item["status"] == "ready" for item in mine)

    def test_queue_shows_every_same_work_center_item_of_a_batch_wo(self, client, db_session):
        """The defect this rule fixes, end to end, on an ordinary (non-laser) WO.

        A batch work order carrying several unordered items as one operation each, all on
        one machine. Every one of them was ALWAYS legal to clock into -- asserted here
        against the real endpoint, not assumed -- but only the lowest-sequence op reached
        READY, and the work-center queue filters to READY/IN_PROGRESS, so the rest were
        invisible to the operator. All of them now queue.
        """
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
            statuses=[OperationStatus.PENDING, OperationStatus.PENDING, OperationStatus.PENDING],
        )

        release_first_ready_operation(wo, db_session, user_id=admin.id)
        db_session.commit()

        resp = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        mine = [item for item in resp.json()["queue"] if item["work_order_id"] == wo.id]
        assert {item["operation_id"] for item in mine} == {op.id for op in ops}

        # And the gate that always allowed it: the LAST item is clock-in-able, which is
        # why promoting it grants nothing new.
        assert _clock_in(client, operator, wo_id=wo.id, op=ops[2]).status_code == status.HTTP_200_OK

    def test_queue_scoped_to_each_work_center_when_nests_spread(self, client, db_session):
        """A package spread across two lasers queues each nest ONLY at its own
        work center."""
        admin = make_user(db_session)
        wc_main = make_laser_work_center(db_session)
        wc_other = make_laser_work_center(db_session)
        child = _import_cross_wc_wo(client, admin, wc_main, wc_other)
        ops = _ops_by_sequence(child)

        resp = client.get(f"/api/v1/shop-floor/work-center-queue/{wc_main.id}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        main_queue = [item for item in resp.json()["queue"] if item["work_order_id"] == child["id"]]
        assert {item["operation_id"] for item in main_queue} == {ops[0]["id"], ops[1]["id"]}

        resp = client.get(f"/api/v1/shop-floor/work-center-queue/{wc_other.id}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        other_queue = [item for item in resp.json()["queue"] if item["work_order_id"] == child["id"]]
        assert {item["operation_id"] for item in other_queue} == {ops[2]["id"]}
