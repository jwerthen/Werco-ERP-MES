"""Dispatch v2: ordering operations INSIDE the plan, and the machine column under it.

* ``PUT /shop-floor/dispatch-board/work-orders/{id}/operation-order`` -- per-machine
  block order for a planned pool/laser work order; touches nothing else; the
  routing/unplanned/closed refusals; 400s; compare-and-swap; display-only writes;
  one audit row (none on a no-op); tenancy.
* ``PUT /shop-floor/work-centers/{id}/run-order`` under "work-order order wins" --
  the 409 on a precedence contradiction, the optional compare-and-swap (and that
  omitting it keeps the pre-v2 contract), ``updated_at`` now preserved.
* The four machine-move paths take the dispatch lock and call the notice seam with
  a before-snapshot of both machines; a moved operation lands at the bottom of its
  own work order's block at the destination.
"""

from datetime import date, datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.dispatch import DispatchChangeKind
from app.services import dispatch_plan_service, dispatch_service
from tests.api.dispatch_plan_helpers import make_plan_wo, operation_order_url, ready, run_order_url
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    ensure_company,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _queue_ids(client: TestClient, wc_id: int, headers: dict) -> list:
    resp = client.get(queue_url(wc_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return [row["operation_id"] for row in resp.json()["queue"]]


def _order(client: TestClient, headers: dict, wo_id: int, ids: list, expected: list):
    return client.put(
        operation_order_url(wo_id),
        json={"operation_ids": ids, "expected_operation_ids": expected},
        headers=headers,
    )


def _run_orders(db: Session, *ops) -> list:
    db.expire_all()
    return [db.get(WorkOrderOperation, op.id).run_order for op in ops]


def _pool(db: Session, brake, saw, *, rank=1, **kwargs):
    """A planned pool: two ready at the brake (10, 20), one ready at the saw (30)."""
    return make_plan_wo(
        db, [ready(brake, 10), ready(brake, 20), ready(saw, 30)], sequential=False, dispatch_rank=rank, **kwargs
    )


# --------------------------------------------------------------------------
# 1. The operation-order verb
# --------------------------------------------------------------------------


class TestOperationOrderVerb:
    def test_sets_block_order_per_machine_and_returns_the_lane(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        brake = make_work_center(db_session)
        saw = make_work_center(db_session)
        wo, (b10, b20, s30) = _pool(db_session, brake, saw)
        # Lane order: brake first (created first -> lower code), queue order inside it.
        expected = [b10.id, b20.id, s30.id]

        resp = _order(client, headers, wo.id, [s30.id, b20.id, b10.id], expected)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        lane = resp.json()
        assert lane["work_order_id"] == wo.id
        assert [op["operation_id"] for op in lane["operations"]] == [b20.id, b10.id, s30.id]
        assert _run_orders(db_session, b10, b20, s30) == [2, 1, 1]
        assert _queue_ids(client, brake.id, headers) == [b20.id, b10.id]

    def test_touches_nothing_but_this_work_orders_queued_operations(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b, held) = make_plan_wo(
            db_session,
            [ready(brake, 10), ready(brake, 20), (brake, OperationStatus.ON_HOLD, 30, 7)],
            sequential=False,
            dispatch_rank=1,
        )
        _, (other_planned,) = make_plan_wo(db_session, [ready(brake, run_order=1)], dispatch_rank=2)
        _, (loose,) = make_plan_wo(db_session, [ready(brake, run_order=3)])

        resp = _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _run_orders(db_session, a, b, held, other_planned, loose) == [2, 1, 7, 1, 3]

    def test_laser_work_orders_are_orderable_whatever_their_sequencing_flag(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        laser_wc = make_work_center(db_session)
        wo, (n1, n2) = make_plan_wo(
            db_session, [ready(laser_wc, 10), ready(laser_wc, 20)], laser=True, sequential=True, dispatch_rank=1
        )
        resp = _order(client, user_headers(manager), wo.id, [n2.id, n1.id], [n1.id, n2.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["mode"] == "laser"
        assert _run_orders(db_session, n1, n2) == [2, 1]

    def test_one_audit_row_and_none_for_a_no_op(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1)

        def _rows():
            db_session.expire_all()
            return (
                db_session.query(AuditLog)
                .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id)
                .all()
            )

        assert _order(client, headers, wo.id, [b.id, a.id], [a.id, b.id]).status_code == status.HTTP_200_OK
        rows = _rows()
        assert len(rows) == 1
        assert rows[0].description == "Set dispatch operation order"
        assert rows[0].extra_data["changes"]["dispatch_operation_order"] == {"old": [a.id, b.id], "new": [b.id, a.id]}
        # Re-sending the same order changes nothing and records nothing.
        assert _order(client, headers, wo.id, [b.id, a.id], [b.id, a.id]).status_code == status.HTTP_200_OK
        assert len(_rows()) == 1

    def test_a_renumber_only_save_is_still_audited(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(
            db_session, [ready(brake, 10, 1), ready(brake, 20, 5)], sequential=False, dispatch_rank=1
        )
        resp = _order(client, user_headers(manager), wo.id, [a.id, b.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _run_orders(db_session, a, b) == [1, 2]
        row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id)
            .one()
        )
        assert row.extra_data["changes"]["run_order"] == {
            "old": {str(a.id): 1, str(b.id): 5},
            "new": {str(a.id): 1, str(b.id): 2},
        }

    def test_writes_leave_version_and_updated_at_alone(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1)
        stamp = datetime(2026, 2, 3, 4, 5, 6)
        for op_id in (a.id, b.id):
            db_session.get(WorkOrderOperation, op_id).updated_at = stamp
        db_session.commit()
        db_session.expire_all()
        before = {op_id: db_session.get(WorkOrderOperation, op_id).version for op_id in (a.id, b.id)}
        wo_version = db_session.get(WorkOrder, wo.id).version

        assert _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id]).status_code == 200
        db_session.expire_all()
        assert {op_id: db_session.get(WorkOrderOperation, op_id).version for op_id in before} == before
        assert {db_session.get(WorkOrderOperation, op_id).updated_at for op_id in before} == {stamp}
        assert db_session.get(WorkOrder, wo.id).version == wo_version

    def test_lock_is_taken(self, client: TestClient, db_session: Session, monkeypatch):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1)
        calls = []
        monkeypatch.setattr(dispatch_service, "acquire_generator_lock", lambda db, ns, cid: calls.append((ns, cid)))
        assert _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id]).status_code == 200
        assert calls == [("dispatch_order", COMPANY_A)]


class TestOperationOrderRefusals:
    def test_a_sequenced_routing_is_409(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=True, dispatch_rank=1)
        resp = _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == "This work order runs in routing sequence."
        assert _run_orders(db_session, a, b) == [None, None]

    def test_an_unplanned_work_order_is_409(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False)
        resp = _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == "Add this work order to the plan first."

    def test_a_closed_work_order_is_409(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, _ = make_plan_wo(
            db_session,
            [(brake, OperationStatus.COMPLETE, 10)],
            sequential=False,
            dispatch_rank=1,
            status=WorkOrderStatus.COMPLETE,
        )
        resp = _order(client, user_headers(manager), wo.id, [], [])
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == dispatch_plan_service.WORK_ORDER_CLOSED_DETAIL

    def test_a_held_work_order_is_409(self, client: TestClient, db_session: Session):
        # A held work order's queued operations carry no effective rank, so giving them a
        # run_order would only push them up the unplanned tail of every machine.
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(
            db_session,
            [ready(brake, 10), ready(brake, 20)],
            sequential=False,
            dispatch_rank=1,
            status=WorkOrderStatus.ON_HOLD,
        )
        resp = _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == dispatch_plan_service.WORK_ORDER_ON_HOLD_DETAIL
        assert _run_orders(db_session, a, b) == [None, None]

    def test_deleted_and_foreign_work_orders_are_404(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        brake = make_work_center(db_session)
        deleted, _ = make_plan_wo(db_session, [ready(brake)], sequential=False, dispatch_rank=1)
        db_session.get(WorkOrder, deleted.id).soft_delete(manager.id)
        db_session.commit()
        foreign, (foreign_op,) = make_plan_wo(
            db_session,
            [ready(make_work_center(db_session, company_id=COMPANY_B))],
            sequential=False,
            dispatch_rank=1,
            company_id=COMPANY_B,
        )
        assert _order(client, headers, deleted.id, [], []).status_code == status.HTTP_404_NOT_FOUND
        resp = _order(client, headers, foreign.id, [foreign_op.id], [foreign_op.id])
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert _run_orders(db_session, foreign_op) == [None]

    def test_an_operation_of_another_work_order_is_400(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1)
        _, (stranger,) = make_plan_wo(db_session, [ready(brake)], dispatch_rank=2)
        resp = _order(client, user_headers(manager), wo.id, [b.id, stranger.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(stranger.id) in resp.json()["detail"]
        resp = _order(client, user_headers(manager), wo.id, [b.id, a.id], [a.id, b.id, 999999])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_list_that_omits_a_queued_operation_is_400(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b) = make_plan_wo(db_session, [ready(brake, 10), ready(brake, 20)], sequential=False, dispatch_rank=1)
        resp = _order(client, user_headers(manager), wo.id, [b.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        resp = _order(client, user_headers(manager), wo.id, [b.id, b.id, a.id], [a.id, b.id])
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert _run_orders(db_session, a, b) == [None, None]

    def test_a_stale_lane_is_409(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b, c) = make_plan_wo(
            db_session, [ready(brake, 10), ready(brake, 20), ready(brake, 30)], sequential=False, dispatch_rank=1
        )
        resp = _order(client, user_headers(manager), wo.id, [c.id, b.id, a.id], [b.id, a.id, c.id])
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL
        assert _run_orders(db_session, a, b, c) == [None, None, None]

    def test_an_operation_completing_since_the_render_is_not_a_conflict(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        brake = make_work_center(db_session)
        wo, (a, b, c) = make_plan_wo(
            db_session, [ready(brake, 10), ready(brake, 20), ready(brake, 30)], sequential=False, dispatch_rank=1
        )
        db_session.get(WorkOrderOperation, a.id).status = OperationStatus.COMPLETE
        db_session.commit()
        resp = _order(client, user_headers(manager), wo.id, [c.id, a.id, b.id], [a.id, b.id, c.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _run_orders(db_session, a, b, c) == [None, 2, 1]


# --------------------------------------------------------------------------
# 2. The column rewrite under "work-order order wins"
# --------------------------------------------------------------------------


class TestColumnRewriteUnderThePlan:
    def test_contradicting_the_plan_is_409_naming_the_pair(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        first, (x,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        second, (y,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=2)
        _, (loose,) = make_plan_wo(db_session, [ready(wc)])

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [y.id, x.id]}, headers=headers)
        assert resp.status_code == status.HTTP_409_CONFLICT
        detail = resp.json()["detail"]
        assert str(x.id) in detail and str(y.id) in detail and first.work_order_number in detail
        # The message names the pair the right way round: the job the planner put FIRST
        # (y, plan #2) is the one that cannot run before the earlier-planned job (x, #1).
        assert detail.startswith(
            f"Operation {y.id} ({second.work_order_number}) cannot run before operation "
            f"{x.id} ({first.work_order_number})"
        )
        # Unplanned work ahead of planned work is the same contradiction.
        resp = client.put(run_order_url(wc.id), json={"operation_ids": [loose.id, x.id]}, headers=headers)
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert _run_orders(db_session, x, y, loose) == [None, None, None]

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [x.id, y.id, loose.id]}, headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [row["operation_id"] for row in resp.json()["queue"]] == [x.id, y.id, loose.id]
        assert [row["dispatch_position"] for row in resp.json()["queue"]] == [1, 2, None]

    def test_reordering_within_a_planned_block_is_allowed(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (a1, a2) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=1)
        resp = client.put(run_order_url(wc.id), json={"operation_ids": [a2.id, a1.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _queue_ids(client, wc.id, user_headers(manager)) == [a2.id, a1.id]

    def test_compare_and_swap_refuses_a_stale_column(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        # Another planner put b on top since this client rendered [a, b].
        assert client.put(run_order_url(wc.id), json={"operation_ids": [b.id]}, headers=headers).status_code == 200
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [a.id], "expected_operation_ids": [a.id, b.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["detail"] == dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL
        assert _run_orders(db_session, a, b) == [None, 1]
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [a.id, b.id], "expected_operation_ids": [b.id, a.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _run_orders(db_session, a, b) == [1, 2]

    def test_a_job_leaving_the_column_since_the_render_is_not_a_conflict(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        db_session.get(WorkOrderOperation, a.id).status = OperationStatus.COMPLETE
        db_session.commit()
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [b.id, a.id], "expected_operation_ids": [a.id, b.id]},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [row["operation_id"] for row in resp.json()["queue"]] == [b.id]

    def test_unknown_expected_ids_and_unrendered_dead_ids_stay_400(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (elsewhere,) = make_plan_wo(db_session, [ready(make_work_center(db_session))])
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [a.id], "expected_operation_ids": [a.id, 999999]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        # A live op of ANOTHER column, never rendered here, is still the old 400.
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [a.id, elsewhere.id], "expected_operation_ids": [a.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(elsewhere.id) in resp.json()["detail"]

    def test_the_rewrite_now_preserves_updated_at(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (held,) = make_plan_wo(db_session, [(wc, OperationStatus.ON_HOLD, 10, 4)])
        stamp = datetime(2026, 3, 4, 5, 6, 7)
        for op_id in (a.id, held.id):
            db_session.get(WorkOrderOperation, op_id).updated_at = stamp
        db_session.commit()
        resp = client.put(run_order_url(wc.id), json={"operation_ids": [a.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, a.id).run_order == 1
        assert db_session.get(WorkOrderOperation, held.id).run_order is None  # whole-column reset still applies
        assert {db_session.get(WorkOrderOperation, op_id).updated_at for op_id in (a.id, held.id)} == {stamp}

    def test_the_rewrite_takes_the_lock_and_calls_the_notice_seam(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        locks, seams = [], []
        monkeypatch.setattr(dispatch_service, "acquire_generator_lock", lambda db, ns, cid: locks.append((ns, cid)))
        monkeypatch.setattr(
            dispatch_service,
            "record_dispatch_change",
            lambda db, cid, actor, before, kind: seams.append((cid, actor.id, before, kind)) or [],
        )
        resp = client.put(run_order_url(wc.id), json={"operation_ids": [b.id, a.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert locks == [("dispatch_order", COMPANY_A)]
        assert seams == [(COMPANY_A, manager.id, {wc.id: [a.id, b.id]}, DispatchChangeKind.MACHINE_ORDER)]


# --------------------------------------------------------------------------
# 3. The four machine-move paths
# --------------------------------------------------------------------------


MOVE_PATHS = ("work_orders_update", "scheduling_move", "schedule", "schedule_earliest")


def _move(client: TestClient, headers: dict, route: str, op: WorkOrderOperation, wo_id: int, target_id: int):
    if route == "work_orders_update":
        return client.put(
            f"/api/v1/work-orders/operations/{op.id}",
            json={"version": op.version, "work_center_id": target_id},
            headers=headers,
        )
    if route == "scheduling_move":
        return client.put(
            f"/api/v1/scheduling/operations/{op.id}/work-center", json={"work_center_id": target_id}, headers=headers
        )
    if route == "schedule":
        return client.put(
            f"/api/v1/scheduling/work-orders/{wo_id}/schedule",
            json={"scheduled_start": date.today().isoformat(), "work_center_id": target_id},
            headers=headers,
        )
    return client.post(
        f"/api/v1/scheduling/work-orders/{wo_id}/schedule-earliest",
        json={"work_center_id": target_id},
        headers=headers,
    )


class TestMovePaths:
    @pytest.mark.parametrize("route", MOVE_PATHS)
    def test_a_move_locks_snapshots_both_machines_and_calls_the_seam(
        self, client: TestClient, db_session: Session, monkeypatch, route
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        _, (incumbent,) = make_plan_wo(db_session, [ready(target)])
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])

        locks, seams = [], []
        monkeypatch.setattr(dispatch_service, "acquire_generator_lock", lambda db, ns, cid: locks.append((ns, cid)))

        def _seam(db, cid, actor, before, kind):
            # The seam runs AFTER the move is flushed: the after-state is visible.
            after = dispatch_service.snapshot_queue_orders(db, cid, before.keys())
            seams.append((before, after, kind))
            return []

        monkeypatch.setattr(dispatch_service, "record_dispatch_change", _seam)
        resp = _move(client, user_headers(manager), route, mover, mover_wo.id, target.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert locks == [("dispatch_order", COMPANY_A)]
        assert len(seams) == 1
        before, after, kind = seams[0]
        assert before == {source.id: [mover.id], target.id: [incumbent.id]}
        assert after == {source.id: [], target.id: [incumbent.id, mover.id]}
        assert kind == {source.id: DispatchChangeKind.MOVED_OUT, target.id: DispatchChangeKind.MOVED_IN}

    @pytest.mark.parametrize("route", ("work_orders_update", "scheduling_move"))
    def test_a_non_move_does_not_take_the_lock(self, client: TestClient, db_session: Session, monkeypatch, route):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [ready(wc)])
        locks = []
        monkeypatch.setattr(dispatch_service, "acquire_generator_lock", lambda db, ns, cid: locks.append(ns))
        resp = _move(client, user_headers(manager), route, op, wo.id, wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert locks == []

    def test_a_moved_operation_lands_at_the_bottom_of_its_own_block(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        planned, (a_there, a_here) = make_plan_wo(
            db_session, [ready(target, 10, 1), ready(source, 20, 1)], sequential=False, dispatch_rank=1
        )
        _, (later,) = make_plan_wo(db_session, [ready(target)], dispatch_rank=2)
        _, (loose,) = make_plan_wo(db_session, [ready(target, run_order=1)], priority=1)

        resp = _move(client, headers, "scheduling_move", a_here, planned.id, target.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _queue_ids(client, target.id, headers) == [a_there.id, a_here.id, later.id, loose.id]
