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
    while non-laser WOs keep one-at-a-time promotion;
  - the work-center queue surfaces every READY nest of a fresh import, and
    only the nests assigned to THAT work center when a package is spread
    across two work centers.

Non-laser sequential gating is pinned as a regression alongside each laser
exemption so the dispatch-pool rule can never silently widen.

Offline by contract: CNC-file packages (filename inference) and the PDF
confirm-and-commit path (no extractor call) only; the AI extractor is patched
to fail the test if ever invoked.
"""

import io
import json
import zipfile

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
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
from app.services.work_order_state_service import (
    is_laser_dispatch_work_order,
    release_first_ready_operation,
    release_next_ready_operation,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
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

    def test_non_laser_same_wc_exemption_unchanged(self, client, db_session):
        """Regression on the OTHER side: the shop-floor same-work-center
        exemption (allow_same_work_center=True) still applies to non-laser WOs,
        while the office start path still blocks regardless of work center."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc],
            statuses=[OperationStatus.READY, OperationStatus.READY],
        )
        second_op = ops[1]

        # Office start has no same-WC exemption: still 400.
        resp = client.post(f"/api/v1/work-orders/operations/{second_op.id}/start", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

        # Shop-floor start allows out-of-sequence WITHIN the same work center.
        resp = client.put(f"/api/v1/shop-floor/operations/{second_op.id}/start", headers=headers_for(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text


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
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
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
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
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
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        wo, ops = make_routed_wo(
            db_session,
            work_centers=[wc, wc, wc],
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
