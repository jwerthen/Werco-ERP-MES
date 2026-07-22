"""Pool-WO header rollup: laser dispatch pools SUM per-nest progress everywhere.

The bug this pins: a laser-nest WO's ``quantity_ordered`` is the SUM of
planned_runs over all nests, and each nest op's ``component_quantity`` caps that
op's ``quantity_complete`` at its OWN sheet count -- so the sequential rollup rule
(``WO qty = max over ops of min(op qty, WO target)``, where every op processes the
whole order) structurally froze the header at the LARGEST SINGLE NEST (prod: 9/149
forever) and only jumped to the full total when ALL nests flipped COMPLETE.

Covers the shared ``pooled_quantity_complete`` helper and every derivation site:

  - ``sync_work_order_quantity_complete`` sums (both branches; monotonic-up; the
    all-complete branch does NOT snap to quantity_ordered -- a nest completed
    short must not assert sheets that were never cut);
  - the walk-down mirror in ``reduce_operation_produced_quantity`` (lowering one
    nest's evidence lowers the pool sum by the same delta);
  - reconcile-on-read heals a stuck header (raise-only) WITHOUT waiting for a
    production post, via the real ``GET /work-orders/{id}``;
  - sequential (non-laser) WOs keep the max rule byte-for-byte.

Offline by contract: CNC-file standalone imports only; the AI extractor is
patched to fail the test if ever invoked.
"""

import io
import zipfile
from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
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
from app.services.work_order_state_service import (
    _sync_work_order_status_from_operations,
    pooled_quantity_complete,
    reduce_operation_produced_quantity,
    sync_work_order_quantity_complete,
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
        email=f"pool-rollup-{n}@co{company_id}.test",
        employee_id=f"PLRU-{n:05d}",
        first_name="Pool",
        last_name=f"Rollup{n}",
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


def make_laser_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"Laser Cutter PR {n}",
        code=f"LASER-PR-{n}",
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
        lambda *a, **k: pytest.fail("pool-rollup laser-nest tests must not call the AI extractor"),
    )


def _import_three_nest_wo(client, admin, wc) -> dict:
    """Standalone 3-nest CNC import (planned runs 2/3/4 -> ordered 9); returns the WO dict."""
    resp = client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers_for(admin),
        data={"work_center_id": str(wc.id)},
        files={
            "file": ("nests.zip", io.BytesIO(_cnc_zip("N1_QTY2.nc", "N2_QTY3.nc", "N3_QTY4.nc")), "application/zip")
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


def _ops_by_sequence(child: dict) -> list[dict]:
    return sorted(child["operations"], key=lambda op: op["sequence"])


def _pool_wo(
    *,
    planned: list[float],
    done: list[float],
    ordered: float | None = None,
    existing: float = 0.0,
    statuses: list[OperationStatus] | None = None,
) -> tuple[WorkOrder, list[WorkOrderOperation]]:
    """Transient (no-DB) laser dispatch pool WO: one nest op per planned/done pair."""
    wo = WorkOrder(
        work_order_number=f"WO-POOL-{_next()}",
        quantity_ordered=ordered if ordered is not None else sum(planned),
        quantity_complete=existing,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=3,
        company_id=COMPANY_A,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
    )
    ops = []
    for index, (planned_runs, done_runs) in enumerate(zip(planned, done), start=1):
        op = WorkOrderOperation(
            company_id=COMPANY_A,
            sequence=index,
            operation_number=f"Nest {index}",
            name=f"Laser Cut - N{index}",
            component_quantity=planned_runs,
            quantity_complete=done_runs,
            status=(statuses[index - 1] if statuses else OperationStatus.READY),
        )
        # Distinct ids like persisted rows -- the reduce mirror's corrected-op
        # override compares sibling.id == operation.id (None == None would match
        # every sibling on unsaved transient rows).
        op.id = _next()
        ops.append(op)
    wo.operations = ops
    return wo, ops


def _routed_wo(*, done: list[float], ordered: float = 5.0, existing: float = 0.0):
    """Transient NON-laser routing WO (each op processes the whole order)."""
    wo = WorkOrder(
        work_order_number=f"WO-SEQ-{_next()}",
        quantity_ordered=ordered,
        quantity_complete=existing,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=3,
        company_id=COMPANY_A,
    )
    ops = []
    for index, done_qty in enumerate(done, start=1):
        op = WorkOrderOperation(
            company_id=COMPANY_A,
            sequence=index * 10,
            operation_number=f"OP{index * 10}",
            name=f"Step {index}",
            quantity_complete=done_qty,
            status=OperationStatus.IN_PROGRESS,
        )
        op.id = _next()  # distinct ids like persisted rows (see _pool_wo)
        ops.append(op)
    wo.operations = ops
    return wo, ops


# --------------------------------------------------------------------------- #
# pooled_quantity_complete (the shared helper)
# --------------------------------------------------------------------------- #
class TestPooledQuantityComplete:
    def test_sums_per_nest_progress_capped_at_each_nest_target(self):
        # Nest 1 over-counted in memory (5 > planned 2): caps at its OWN target.
        wo, ops = _pool_wo(planned=[2, 3, 4], done=[5, 3, 0])
        assert pooled_quantity_complete(wo, ops) == 5.0  # 2 + 3 + 0

    def test_capped_at_wo_quantity_ordered(self):
        wo, ops = _pool_wo(planned=[2, 3], done=[2, 3], ordered=4)
        assert pooled_quantity_complete(wo, ops) == 4.0

    def test_component_ops_do_not_contribute(self):
        wo, ops = _pool_wo(planned=[2, 3], done=[2, 3])
        ops[0].component_part_id = 12345
        assert pooled_quantity_complete(wo, ops) == 3.0


# --------------------------------------------------------------------------- #
# sync_work_order_quantity_complete (live write rollup)
# --------------------------------------------------------------------------- #
class TestSyncPoolRollup:
    def test_pool_header_is_the_sum_not_the_largest_nest(self):
        wo, ops = _pool_wo(planned=[2, 3, 4], done=[2, 3, 0])
        sync_work_order_quantity_complete(wo, ops[1], all_operations_complete=False)
        assert float(wo.quantity_complete) == 5.0  # old max() rule froze this at 3

    def test_pool_rollup_is_monotonic_up(self):
        wo, ops = _pool_wo(planned=[2, 3, 4], done=[2, 3, 0], existing=6.0)
        sync_work_order_quantity_complete(wo, ops[0], all_operations_complete=False)
        assert float(wo.quantity_complete) == 6.0  # pooled sum 5 never lowers 6

    def test_pool_all_complete_does_not_snap_to_ordered(self):
        # Nest 3 completed SHORT (3 of 4 sheets): snapping to quantity_ordered
        # would assert a sheet that was never cut (AS9100D records honesty).
        wo, ops = _pool_wo(
            planned=[2, 3, 4],
            done=[2, 3, 3],
            statuses=[OperationStatus.COMPLETE] * 3,
        )
        sync_work_order_quantity_complete(wo, ops[2], all_operations_complete=True)
        assert float(wo.quantity_complete) == 8.0  # NOT 9

    def test_sequential_wo_keeps_the_max_rule(self):
        wo, ops = _routed_wo(done=[5, 2], ordered=5)
        sync_work_order_quantity_complete(wo, ops[1], all_operations_complete=False)
        assert float(wo.quantity_complete) == 2.0
        sync_work_order_quantity_complete(wo, ops[0], all_operations_complete=False)
        assert float(wo.quantity_complete) == 5.0

    def test_sequential_all_complete_still_snaps_to_ordered(self):
        wo, ops = _routed_wo(done=[5, 4], ordered=5)
        sync_work_order_quantity_complete(wo, ops[1], all_operations_complete=True)
        assert float(wo.quantity_complete) == 5.0


# --------------------------------------------------------------------------- #
# reduce_operation_produced_quantity (walk-down mirror)
# --------------------------------------------------------------------------- #
class TestReduceMirrorPool:
    def _entry(self, produced: float) -> TimeEntry:
        return TimeEntry(
            company_id=COMPANY_A,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow(),
            quantity_produced=produced,
        )

    def test_pool_reduction_lowers_the_sum_by_the_same_delta(self):
        wo, ops = _pool_wo(planned=[2, 3, 4], done=[2, 3, 4], existing=9.0)
        entry = self._entry(3.0)
        reduction = reduce_operation_produced_quantity(ops[1], wo, [entry], 2.0, ops)
        assert float(ops[1].quantity_complete) == 1.0
        assert float(entry.quantity_produced) == 1.0
        # Pool sum 2 + 1 + 4 = 7 -- the old max() mirror would have left the
        # header pinned at the largest untouched nest instead of dropping by 2.
        assert float(wo.quantity_complete) == 7.0
        assert reduction.work_order_quantity_complete_before == 9.0
        assert reduction.work_order_quantity_complete_after == 7.0

    def test_pool_reduction_never_raises_the_header(self):
        # Stale-low header (4 < pooled 9): the min(wo_before, ...) guard holds.
        wo, ops = _pool_wo(planned=[2, 3, 4], done=[2, 3, 4], existing=4.0)
        entry = self._entry(3.0)
        reduce_operation_produced_quantity(ops[1], wo, [entry], 1.0, ops)
        assert float(wo.quantity_complete) == 4.0

    def test_sequential_reduction_keeps_the_max_mirror(self):
        wo, ops = _routed_wo(done=[5, 5], ordered=5, existing=5.0)
        entry = self._entry(5.0)
        reduce_operation_produced_quantity(ops[1], wo, [entry], 2.0, ops)
        assert float(ops[1].quantity_complete) == 3.0
        # Sibling op still holds 5, so the sequential max-mirror keeps the WO at 5.
        assert float(wo.quantity_complete) == 5.0


# --------------------------------------------------------------------------- #
# Reconcile-on-read healing
# --------------------------------------------------------------------------- #
class TestReconcileHealsPoolHeader:
    def test_stuck_pool_header_heals_on_get(self, client, db_session):
        """The prod-observed shape: per-nest counters healthy, header frozen at
        the largest single nest. A plain GET must raise it to the pooled sum."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)
        ops = _ops_by_sequence(child)

        quantities = {ops[0]["id"]: 2.0, ops[1]["id"]: 1.0, ops[2]["id"]: 4.0}
        for op_id, qty in quantities.items():
            db_session.get(WorkOrderOperation, op_id).quantity_complete = qty
        stuck = db_session.get(WorkOrder, child["id"])
        stuck.quantity_complete = 4.0  # frozen at max(single nest) by the old rule
        db_session.commit()

        resp = client.get(f"/api/v1/work-orders/{child['id']}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert float(resp.json()["quantity_complete"]) == 7.0  # 2 + 1 + 4

        db_session.expire_all()
        assert float(db_session.get(WorkOrder, child["id"]).quantity_complete) == 7.0

    def test_reconcile_heal_is_raise_only(self, client, db_session):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)
        ops = _ops_by_sequence(child)

        db_session.get(WorkOrderOperation, ops[0]["id"]).quantity_complete = 2.0
        db_session.get(WorkOrder, child["id"]).quantity_complete = 8.0
        db_session.commit()

        resp = client.get(f"/api/v1/work-orders/{child['id']}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert float(resp.json()["quantity_complete"]) == 8.0  # never lowered

    def test_all_complete_reconcile_does_not_snap_to_ordered(self):
        """Unit-level pin of the no-snap rule inside the reconcile's WO sync
        (the API read path layers _copy_slot_completion_evidence on top, which
        raises a COMPLETE-short op to its own target -- pre-existing op-level
        behavior outside this rule)."""
        wo, _ops = _pool_wo(
            planned=[2, 3, 4],
            done=[2, 3, 3],
            statuses=[OperationStatus.COMPLETE] * 3,
        )
        changed = _sync_work_order_status_from_operations(wo)
        assert changed is True
        assert wo.status == WorkOrderStatus.COMPLETE
        assert float(wo.quantity_complete) == 8.0  # NOT snapped to 9

    def test_sequential_all_complete_reconcile_still_snaps(self):
        wo, _ops = _routed_wo(done=[5, 4], ordered=5)
        for op in _ops:
            op.status = OperationStatus.COMPLETE
        _sync_work_order_status_from_operations(wo)
        assert wo.status == WorkOrderStatus.COMPLETE
        assert float(wo.quantity_complete) == 5.0


# --------------------------------------------------------------------------- #
# End-to-end through the office completion endpoint
# --------------------------------------------------------------------------- #
class TestOfficeCompletionRollsUpTheSum:
    def test_completing_nests_accumulates_the_header(self, client, db_session):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_three_nest_wo(client, admin, wc)
        ops = _ops_by_sequence(child)

        # Complete nest 1 fully (2 sheets): header = 2.
        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[0]['id']}/complete",
            params={"quantity_complete": 2},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert float(db_session.get(WorkOrder, child["id"]).quantity_complete) == 2.0

        # Complete nest 2 fully (3 sheets): header = 5 (the old rule froze it at 3).
        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[1]['id']}/complete",
            params={"quantity_complete": 3},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        wo = db_session.get(WorkOrder, child["id"])
        assert float(wo.quantity_complete) == 5.0
        assert wo.status == WorkOrderStatus.IN_PROGRESS  # nest 3 still open

        # Partial progress on nest 3 (1 of 4): header = 6.
        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[2]['id']}/complete",
            params={"quantity_complete": 1},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert float(db_session.get(WorkOrder, child["id"]).quantity_complete) == 6.0


# --------------------------------------------------------------------------- #
# Independent test-engineer pins: the rollup driven through the REAL verbs
# (kiosk /production, /reduce-production, office /complete) on the owner's
# 9/5/7-shaped pool, plus the live-endpoint sequential regression guard.
# --------------------------------------------------------------------------- #
def _import_pool_wo(client, admin, wc, qtys: list[int]) -> dict:
    """Standalone CNC import with one nest per entry of ``qtys`` (planned runs)."""
    names = [f"N{i}_QTY{q}.nc" for i, q in enumerate(qtys, start=1)]
    resp = client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers_for(admin),
        data={"work_center_id": str(wc.id)},
        files={"file": ("nests.zip", io.BytesIO(_cnc_zip(*names)), "application/zip")},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["child_work_order"]


def _clock_into_op(db: Session, operator: User, op_id: int) -> TimeEntry:
    """Put ``operator`` on the nest the way the kiosk leaves them: op IN_PROGRESS
    with an open RUN entry (the /production verb's two preconditions)."""
    op = db.get(WorkOrderOperation, op_id)
    op.status = OperationStatus.IN_PROGRESS
    entry = TimeEntry(
        user_id=operator.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        quantity_produced=0.0,
        company_id=operator.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _post_production(client, operator: User, op_id: int, delta: float):
    return client.post(
        f"/api/v1/shop-floor/operations/{op_id}/production",
        json={"quantity_complete_delta": delta},
        headers=headers_for(operator),
    )


def _header(db: Session, wo_id: int) -> float:
    db.expire_all()
    return float(db.get(WorkOrder, wo_id).quantity_complete or 0)


class TestKioskProductionPoolRollup:
    """Scenario 1+2: the real kiosk verb SUMS the header across nests, and the
    per-nest cap (component_quantity) is enforced at the wire."""

    def test_header_advances_as_the_sum_across_nests(self, client, db_session):
        """The owner's shape (9/5/7 -> ordered 21): 4 on A -> 4; 3 on B -> 7;
        5 more on A (A lands exactly at its 9-sheet cap) -> 12."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        assert float(child["quantity_ordered"]) == 21.0
        ops = _ops_by_sequence(child)
        # The op<->nest mapping the caps hang off: component_quantity per op.
        rows = [db_session.get(WorkOrderOperation, op["id"]) for op in ops]
        assert [float(r.component_quantity) for r in rows] == [9.0, 5.0, 7.0]
        for op in ops:
            _clock_into_op(db_session, operator, op["id"])

        resp = _post_production(client, operator, ops[0]["id"], 4)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _header(db_session, child["id"]) == 4.0

        resp = _post_production(client, operator, ops[1]["id"], 3)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _header(db_session, child["id"]) == 7.0  # 4 + 3, old max() rule said 4

        resp = _post_production(client, operator, ops[0]["id"], 5)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["operation"]["quantity_complete"] == 9.0
        assert _header(db_session, child["id"]) == 12.0  # 9 + 3 + 0

    def test_over_report_refused_at_the_nest_cap_and_header_untouched(self, client, db_session):
        """Nest B's target is ITS OWN 5 planned runs, not the WO's 21: a 6th
        sheet is a 400 (component_quantity-first target), nothing mutates."""
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        ops = _ops_by_sequence(child)
        _clock_into_op(db_session, operator, ops[1]["id"])

        resp = _post_production(client, operator, ops[1]["id"], 5)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _header(db_session, child["id"]) == 5.0

        resp = _post_production(client, operator, ops[1]["id"], 1)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "cannot exceed quantity ordered (5" in resp.json()["detail"]
        db_session.expire_all()
        assert float(db_session.get(WorkOrderOperation, ops[1]["id"]).quantity_complete) == 5.0
        assert _header(db_session, child["id"]) == 5.0


class TestPoolAllCompleteThroughEndpoints:
    """Scenario 3: all-complete honesty through the real completion verb."""

    def test_completing_every_nest_fully_lands_header_at_the_ordered_sum(self, client, db_session):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        ops = _ops_by_sequence(child)

        running_total = 0.0
        for op, planned in zip(ops, [9, 5, 7]):
            resp = client.post(
                f"/api/v1/work-orders/operations/{op['id']}/complete",
                params={"quantity_complete": planned},
                headers=headers_for(admin),
            )
            assert resp.status_code == status.HTTP_200_OK, resp.text
            running_total += planned
            assert _header(db_session, child["id"]) == running_total

        wo = db_session.get(WorkOrder, child["id"])
        assert wo.status == WorkOrderStatus.COMPLETE
        assert float(wo.quantity_complete) == 21.0 == float(wo.quantity_ordered)

    def test_no_endpoint_path_completes_a_nest_short(self, client, db_session):
        """The office /complete with quantity below the nest's planned runs is
        PARTIAL progress -- the op stays open, so the all-complete branch can
        only ever be reached with real per-nest counts (the honesty rule's
        no-snap unit pins cover the sync itself)."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        ops = _ops_by_sequence(child)

        resp = client.post(
            f"/api/v1/work-orders/operations/{ops[1]['id']}/complete",
            params={"quantity_complete": 3},
            headers=headers_for(admin),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        op = db_session.get(WorkOrderOperation, ops[1]["id"])
        assert op.status == OperationStatus.IN_PROGRESS  # NOT COMPLETE-short
        assert float(op.quantity_complete) == 3.0
        assert _header(db_session, child["id"]) == 3.0
        assert db_session.get(WorkOrder, child["id"]).status != WorkOrderStatus.COMPLETE


class TestStuckHeaderHealsOnNextProductionPost:
    """Scenario 4 (write-path angle): the owner's frozen header must ALSO heal on
    the next kiosk post -- the sync recomputes the full pooled sum, not just
    header + delta. (The read-path GET heal is pinned above.)"""

    def test_production_post_lifts_frozen_header_to_the_full_pooled_sum(self, client, db_session):
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        ops = _ops_by_sequence(child)

        # Frozen prod shape: per-nest counters healthy (4/3/0), header stuck at
        # the largest single nest's count (the pre-fix max() rollup).
        db_session.get(WorkOrderOperation, ops[0]["id"]).quantity_complete = 4.0
        db_session.get(WorkOrderOperation, ops[1]["id"]).quantity_complete = 3.0
        db_session.get(WorkOrder, child["id"]).quantity_complete = 4.0
        db_session.commit()

        _clock_into_op(db_session, operator, ops[2]["id"])
        resp = _post_production(client, operator, ops[2]["id"], 1)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # 4 + 3 + 1 -- not the frozen 4 + 1.
        assert _header(db_session, child["id"]) == 8.0


class TestReduceEndpointWalksDownPool:
    """Scenario 5: the real shop-floor reduce verb lowers the pool header by
    exactly the walked-back delta."""

    def test_reduce_lowers_header_by_the_same_delta(self, client, db_session):
        admin = make_user(db_session)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_laser_work_center(db_session)
        child = _import_pool_wo(client, admin, wc, [9, 5, 7])
        ops = _ops_by_sequence(child)
        for op in ops[:2]:
            _clock_into_op(db_session, operator, op["id"])

        assert _post_production(client, operator, ops[0]["id"], 4).status_code == status.HTTP_200_OK
        assert _post_production(client, operator, ops[1]["id"], 3).status_code == status.HTTP_200_OK
        assert _header(db_session, child["id"]) == 7.0

        resp = client.post(
            f"/api/v1/shop-floor/operations/{ops[0]['id']}/reduce-production",
            json={"quantity_delta": 2, "reason": "miscounted a dropped sheet"},
            headers=headers_for(operator),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["operation"]["quantity_complete"] == 2.0
        assert body["active_time_entry"]["quantity_produced"] == 2.0
        # Pool sum 2 + 3 = 5: dropped by exactly the walked-back delta. The old
        # sequential mirror would have collapsed the header to max(2, 3) = 3,
        # erasing nest A's remaining 2 sheets from the pool total.
        assert _header(db_session, child["id"]) == 5.0

        # And the walk-down survives reconcile-on-read (the reduce feature's crux).
        resp = client.get(f"/api/v1/work-orders/{child['id']}", headers=headers_for(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert float(resp.json()["quantity_complete"]) == 5.0


class TestSequentialRegressionGuardThroughKiosk:
    """Scenario 6: a NORMAL routed (non-laser) WO on the same live kiosk verb
    keeps the max-over-ops rule -- the SUM must not leak out of the pool."""

    def _routed_wo_in_db(self, db: Session) -> tuple[WorkOrder, list[WorkOrderOperation]]:
        n = _next()
        part = Part(
            part_number=f"PLRU-SEQ-{n}",
            name=f"Routed Part {n}",
            description="sequential regression fixture",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=COMPANY_A,
        )
        db.add(part)
        db.flush()
        wc = WorkCenter(
            name=f"PLRU-MILL-{n}",
            code=f"PLRU-MILL-{n}",
            work_center_type="machining",
            description="sequential regression fixture",
            hourly_rate=100.0,
            is_active=True,
            company_id=COMPANY_A,
        )
        db.add(wc)
        db.flush()
        wo = WorkOrder(
            work_order_number=f"PLRU-SEQ-WO-{n:05d}",
            customer_name="Acme",
            part_id=part.id,
            quantity_ordered=8,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=5,
            due_date=date.today() + timedelta(days=30),
            company_id=COMPANY_A,
        )
        db.add(wo)
        db.flush()
        assert wo.work_order_type != WorkOrderType.LASER_CUTTING.value  # the guard's premise
        ops = []
        for sequence in (10, 20):
            op = WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=wc.id,
                sequence=sequence,
                operation_number=f"OP{sequence}",
                name=f"Step {sequence}",
                status=OperationStatus.IN_PROGRESS,
                quantity_complete=0,
                company_id=COMPANY_A,
            )
            db.add(op)
            ops.append(op)
        db.commit()
        for op in ops:
            db.refresh(op)
        return wo, ops

    def test_routed_wo_keeps_the_max_rule_on_live_posts(self, client, db_session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wo, ops = self._routed_wo_in_db(db_session)
        for op in ops:
            _clock_into_op(db_session, operator, op.id)

        resp = _post_production(client, operator, ops[0].id, 5)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _header(db_session, wo.id) == 5.0

        resp = _post_production(client, operator, ops[1].id, 3)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # Every op processes the whole order: 5 pieces through op1, 3 of those
        # through op2 -> 5 finished at the furthest point, NEVER 5 + 3 = 8.
        assert _header(db_session, wo.id) == 5.0
