"""Dispatch v2: the work-order PLAN verb and the work-order-first (lanes) read.

* ``PUT /shop-floor/dispatch-board/work-order-order`` -- dense ranks, whole-list
  authority (terminal and deleted rows included), compare-and-swap with the
  "a job finishing is not a conflict" rule, 400 on unknown/foreign ids, one audit
  row (none on a no-op), Core writes that leave ``version`` / ``updated_at`` alone,
  and leaving the plan clears the job's within-plan order.
* ``POST /work-orders/{id}/restore`` -- a restored job comes back OUT of the plan.
* ``GET /shop-floor/dispatch-board/work-orders`` -- the lanes: order, shape, the
  shared predecessor gate, tombstones excluded, a pure read with a flat query cost.
* RBAC (planner tier only), the kiosk-scope fence on every new route, tenancy.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.laser_nest import LaserNest
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services import dispatch_plan_service, dispatch_service
from tests.api.dispatch_plan_helpers import (
    LANES_URL,
    PLAN_URL,
    count_statements,
    kiosk_scoped_headers,
    lane_for,
    make_plan_wo,
    operation_order_url,
    plan,
    ready,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    ensure_company,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)
from tests.api.test_dispatch_nest_details import make_nest_operation, make_sheet_part, tie_operation

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

FENCE_DETAIL = "Kiosk-scoped token cannot access this resource"


def _ranks(db: Session, *work_orders) -> list:
    db.expire_all()
    return [db.get(WorkOrder, wo.id).dispatch_rank for wo in work_orders]


def _plan_audit_rows(db: Session) -> list:
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == "dispatch_board").order_by(AuditLog.id).all()


# --------------------------------------------------------------------------
# 1. The plan verb
# --------------------------------------------------------------------------


class TestPlanVerb:
    def test_save_writes_dense_ranks_and_returns_the_board(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        b, _ = make_plan_wo(db_session, [ready(wc)])
        c, _ = make_plan_wo(db_session, [ready(wc)])

        resp = plan(client, headers, [c.id, a.id], expected=[])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert [lane["work_order_id"] for lane in body["work_orders"][:2]] == [c.id, a.id]
        assert [lane["dispatch_position"] for lane in body["work_orders"][:2]] == [1, 2]
        assert body["ranked_count"] == 2 and body["unranked_count"] == 1
        assert lane_for(body, b.id)["dispatch_position"] is None
        assert _ranks(db_session, a, b, c) == [2, None, 1]

    def test_one_audit_row_describes_the_change(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        b, _ = make_plan_wo(db_session, [ready(wc)])
        assert plan(client, headers, [a.id, b.id], expected=[]).status_code == status.HTTP_200_OK
        assert plan(client, headers, [b.id, a.id], expected=[a.id, b.id]).status_code == status.HTTP_200_OK

        rows = _plan_audit_rows(db_session)
        assert len(rows) == 2
        row = rows[-1]
        assert row.resource_id == COMPANY_A
        assert row.resource_identifier == "work-order-order"
        assert row.company_id == COMPANY_A
        assert row.extra_data["changes"]["work_order_ids"] == {"old": [a.id, b.id], "new": [b.id, a.id]}
        changed = {entry["work_order_id"]: (entry["from"], entry["to"]) for entry in row.extra_data["changed"]}
        assert changed == {b.id: (2, 1), a.id: (1, 2)}
        assert {entry["work_order_number"] for entry in row.extra_data["changed"]} == {
            a.work_order_number,
            b.work_order_number,
        }

    def test_a_renumber_only_save_is_still_audited(self, client: TestClient, db_session: Session):
        """A planned job hard-deleted out from under the plan leaves a GAP (1, 3) with
        no row holding rank 2. Re-saving the same visible order renumbers 3 -> 2: a
        real write, so it must leave a chain row even though the id list is unchanged."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        b, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=3)
        resp = plan(client, headers, [a.id, b.id], expected=[a.id, b.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _ranks(db_session, a, b) == [1, 2]
        rows = _plan_audit_rows(db_session)
        assert len(rows) == 1
        assert rows[0].extra_data["changes"]["dispatch_ranks"] == {
            "old": {str(a.id): 1, str(b.id): 3},
            "new": {str(a.id): 1, str(b.id): 2},
        }

    def test_a_no_op_save_writes_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        assert plan(client, headers, [a.id], expected=[]).status_code == status.HTTP_200_OK
        assert len(_plan_audit_rows(db_session)) == 1

        resp = plan(client, headers, [a.id], expected=[a.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert len(_plan_audit_rows(db_session)) == 1

    def test_draft_work_orders_are_plannable_and_stay_pending(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        draft, (op,) = make_plan_wo(db_session, [(wc, OperationStatus.PENDING, 10)], status=WorkOrderStatus.DRAFT)
        resp = plan(client, user_headers(manager), [draft.id], expected=[])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert lane_for(resp.json(), draft.id)["dispatch_position"] == 1
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.PENDING

    def test_the_plan_reaches_the_kiosk_queue(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        hot, (hot_op,) = make_plan_wo(db_session, [ready(wc)], priority=1)
        cold, (cold_op,) = make_plan_wo(db_session, [ready(wc)], priority=9)
        assert plan(client, headers, [cold.id], expected=[]).status_code == status.HTTP_200_OK
        queue = client.get(queue_url(wc.id), headers=headers).json()["queue"]
        assert [(row["operation_id"], row["run_order"]) for row in queue] == [(cold_op.id, 1), (hot_op.id, None)]

    def test_lock_is_taken_before_any_read(self, client: TestClient, db_session: Session, monkeypatch):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        calls = []
        monkeypatch.setattr(
            dispatch_service,
            "acquire_generator_lock",
            lambda db, namespace, company_id: calls.append((namespace, company_id)),
        )
        assert plan(client, user_headers(manager), [a.id], expected=[]).status_code == status.HTTP_200_OK
        assert calls == [("dispatch_order", COMPANY_A)]


class TestPlanCompareAndSwap:
    def test_a_stale_render_is_refused_409_and_changes_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        b, _ = make_plan_wo(db_session, [ready(wc)])
        # Planner 1 saves first...
        assert plan(client, headers, [a.id], expected=[]).status_code == status.HTTP_200_OK
        # ...planner 2 rendered the empty plan and saves on top of it.
        resp = plan(client, headers, [b.id], expected=[])
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert resp.json()["detail"] == dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL
        assert _ranks(db_session, a, b) == [1, None]
        assert len(_plan_audit_rows(db_session)) == 1

    def test_a_job_completing_between_render_and_save_is_not_a_conflict(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)])
        b, _ = make_plan_wo(db_session, [ready(wc)])
        assert plan(client, headers, [a.id, b.id], expected=[]).status_code == status.HTTP_200_OK

        a_row = db_session.get(WorkOrder, a.id)
        a_row.status = WorkOrderStatus.COMPLETE
        db_session.commit()

        # The client still lists A on both sides.
        resp = plan(client, headers, [b.id, a.id], expected=[a.id, b.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # B is now the whole plan; A (terminal) left it by whole-list authority.
        assert _ranks(db_session, a, b) == [None, 1]

    def test_unknown_foreign_and_duplicate_ids_are_400(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        mine, _ = make_plan_wo(db_session, [ready(wc)])
        foreign, _ = make_plan_wo(
            db_session, [ready(make_work_center(db_session, company_id=COMPANY_B))], company_id=COMPANY_B
        )

        for body, needle in (
            ({"work_order_ids": [mine.id, 999999], "expected_work_order_ids": []}, "999999"),
            ({"work_order_ids": [foreign.id], "expected_work_order_ids": []}, str(foreign.id)),
            ({"work_order_ids": [mine.id], "expected_work_order_ids": [foreign.id]}, str(foreign.id)),
            ({"work_order_ids": [mine.id, mine.id], "expected_work_order_ids": []}, "Duplicate"),
        ):
            resp = client.put(PLAN_URL, json=body, headers=headers)
            assert resp.status_code == status.HTTP_400_BAD_REQUEST, (body, resp.text)
            assert needle in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkOrder, foreign.id).dispatch_rank is None
        assert db_session.get(WorkOrder, mine.id).dispatch_rank is None

    def test_body_lists_are_capped(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        resp = client.put(
            PLAN_URL,
            json={"work_order_ids": list(range(1, 502)), "expected_work_order_ids": []},
            headers=user_headers(manager),
        )
        assert resp.status_code == 422


class TestWholeListAuthority:
    def test_terminal_and_deleted_work_orders_leave_the_plan(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        live, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        done, (done_op,) = make_plan_wo(
            db_session, [(wc, OperationStatus.COMPLETE, 10, 4)], status=WorkOrderStatus.COMPLETE, dispatch_rank=2
        )
        deleted, (deleted_op,) = make_plan_wo(db_session, [ready(wc, run_order=2)], dispatch_rank=3)
        deleted_row = db_session.get(WorkOrder, deleted.id)
        deleted_row.soft_delete(manager.id)
        db_session.commit()

        resp = plan(client, headers, [live.id], expected=[live.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _ranks(db_session, live, done, deleted) == [1, None, None]
        # Leaving the plan also clears their operations' within-plan order.
        assert db_session.get(WorkOrderOperation, done_op.id).run_order is None
        assert db_session.get(WorkOrderOperation, deleted_op.id).run_order is None

        row = _plan_audit_rows(db_session)[-1]
        assert row.extra_data["changes"]["work_order_ids"]["old"] == [live.id, done.id, deleted.id]
        cleared = {entry["work_order_id"] for entry in row.extra_data["changed"] if entry["to"] is None}
        assert cleared == {done.id, deleted.id}

    def test_removing_a_job_from_the_plan_clears_only_its_run_order(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        leaving, (leaving_a, leaving_b) = make_plan_wo(
            db_session, [ready(wc, 10, run_order=2), ready(wc, 20, run_order=1)], sequential=False, dispatch_rank=1
        )
        staying, (staying_op,) = make_plan_wo(db_session, [ready(wc, run_order=1)], dispatch_rank=2)
        loose, (loose_op,) = make_plan_wo(db_session, [ready(make_work_center(db_session), run_order=1)])

        resp = plan(client, headers, [staying.id], expected=[leaving.id, staying.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, leaving_a.id).run_order is None
        assert db_session.get(WorkOrderOperation, leaving_b.id).run_order is None
        assert db_session.get(WorkOrderOperation, staying_op.id).run_order == 1
        assert db_session.get(WorkOrderOperation, loose_op.id).run_order == 1

    def test_an_empty_list_clears_the_plan(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        resp = plan(client, user_headers(manager), [], expected=[a.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["ranked_count"] == 0
        assert _ranks(db_session, a) == [None]


class TestPlanWritesAreDisplayOnly:
    def test_version_and_updated_at_are_untouched(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        stamp = datetime(2026, 1, 2, 3, 4, 5)
        a, (a_op,) = make_plan_wo(db_session, [ready(wc, run_order=1)], dispatch_rank=2)
        b, (b_op,) = make_plan_wo(db_session, [ready(wc)])
        for row in (db_session.get(WorkOrder, a.id), db_session.get(WorkOrder, b.id)):
            row.updated_at = stamp
        db_session.commit()
        db_session.expire_all()
        before = {
            wo_id: (db_session.get(WorkOrder, wo_id).version, db_session.get(WorkOrder, wo_id).updated_at)
            for wo_id in (a.id, b.id)
        }
        op_before = {op_id: db_session.get(WorkOrderOperation, op_id).version for op_id in (a_op.id, b_op.id)}

        # b enters the plan, a is renumbered, then a leaves (its op's run_order clears).
        assert plan(client, user_headers(manager), [b.id, a.id], expected=[a.id]).status_code == 200
        assert plan(client, user_headers(manager), [b.id], expected=[b.id, a.id]).status_code == 200

        db_session.expire_all()
        after = {
            wo_id: (db_session.get(WorkOrder, wo_id).version, db_session.get(WorkOrder, wo_id).updated_at)
            for wo_id in (a.id, b.id)
        }
        assert after == before
        assert after[a.id][1] == stamp
        assert {op_id: db_session.get(WorkOrderOperation, op_id).version for op_id in op_before} == op_before
        assert db_session.get(WorkOrderOperation, a_op.id).run_order is None

    @pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgresql", "sqlite"])
    def test_the_rank_update_never_sets_version_and_pins_updated_at(self, dialect):
        statements = dispatch_plan_service.work_order_rank_update_statements(COMPANY_A, {11: 1, 12: 2}, [13])
        statements.append(dispatch_plan_service.operation_run_order_update_statement(COMPANY_A, {21: 1}))
        assert len(statements) == 3
        for statement in statements:
            sql = str(statement.compile(dialect=dialect)).replace("\n", " ")
            set_clause = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            assert "version" not in set_clause, sql
            table = "work_orders" if "UPDATE work_orders" in sql else "work_order_operations"
            assert f"updated_at={table}.updated_at" in set_clause.replace(" ", ""), sql
            assert "company_id" in sql.split(" WHERE ", 1)[1]


class TestRestoreReturnsTheRecordNotThePlan:
    def test_a_restored_work_order_comes_back_out_of_the_plan(self, client: TestClient, db_session: Session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        headers = user_headers(admin)
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [ready(wc, run_order=2)], dispatch_rank=3)
        row = db_session.get(WorkOrder, wo.id)
        row.soft_delete(admin.id)
        db_session.commit()

        resp = client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        restored = db_session.get(WorkOrder, wo.id)
        assert restored.is_deleted is False
        assert restored.dispatch_rank is None
        assert db_session.get(WorkOrderOperation, op.id).run_order is None
        audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id, AuditLog.action == "RESTORE")
            .one()
        )
        assert audit.extra_data["changes"]["dispatch_rank"] == {"old": 3, "new": None}

    def test_restoring_an_unplanned_work_order_is_unchanged(self, client: TestClient, db_session: Session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [ready(wc, run_order=2)])
        row = db_session.get(WorkOrder, wo.id)
        row.soft_delete(admin.id)
        db_session.commit()
        resp = client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=user_headers(admin))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        # No plan to leave, so the machine rank is the restore's business no more than before.
        assert db_session.get(WorkOrderOperation, op.id).run_order == 2
        audit = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo.id, AuditLog.action == "RESTORE")
            .one()
        )
        assert "dispatch_rank" not in audit.extra_data["changes"]


# --------------------------------------------------------------------------
# 2. RBAC, the kiosk fence, tenancy
# --------------------------------------------------------------------------


class TestAccess:
    def test_operator_is_refused_every_new_route(self, client: TestClient, db_session: Session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, _ = make_plan_wo(db_session, [ready(wc)], sequential=False, dispatch_rank=1)
        headers = user_headers(operator)
        assert client.get(LANES_URL, headers=headers).status_code == status.HTTP_403_FORBIDDEN
        assert (
            client.put(
                PLAN_URL, json={"work_order_ids": [], "expected_work_order_ids": []}, headers=headers
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )
        assert (
            client.put(
                operation_order_url(wo.id),
                json={"operation_ids": [], "expected_operation_ids": []},
                headers=headers,
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

    def test_a_kiosk_scoped_admin_token_is_fenced_off_every_new_route(self, client: TestClient, db_session: Session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_work_center(db_session)
        wo, _ = make_plan_wo(db_session, [ready(wc)], sequential=False, dispatch_rank=1)
        headers = kiosk_scoped_headers(admin)
        responses = [
            client.get(LANES_URL, headers=headers),
            client.put(PLAN_URL, json={"work_order_ids": [wo.id], "expected_work_order_ids": [wo.id]}, headers=headers),
            client.put(
                operation_order_url(wo.id),
                json={"operation_ids": [], "expected_operation_ids": []},
                headers=headers,
            ),
        ]
        for resp in responses:
            assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
            assert resp.json()["detail"] == FENCE_DETAIL
        assert _ranks(db_session, wo) == [1]

    def test_lanes_never_show_another_companys_work(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager = make_user(db_session, role=UserRole.MANAGER)
        mine, _ = make_plan_wo(db_session, [ready(make_work_center(db_session))], dispatch_rank=1)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_B)
        foreign, _ = make_plan_wo(db_session, [ready(foreign_wc)], company_id=COMPANY_B, dispatch_rank=1)
        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        ids = [lane["work_order_id"] for lane in body["work_orders"]]
        assert mine.id in ids and foreign.id not in ids
        assert foreign_wc.id not in [wc["id"] for wc in body["work_centers"]]
        # And the foreign rank does not leak into this company's positions.
        assert lane_for(body, mine.id)["dispatch_position"] == 1


# --------------------------------------------------------------------------
# 3. The lanes read
# --------------------------------------------------------------------------


class TestLanesRead:
    def test_plan_first_then_unplanned_by_priority_promise_and_number(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        today = date.today()
        second, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=9)
        first, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=2)
        no_promise, _ = make_plan_wo(db_session, [ready(wc)], priority=3)
        later, _ = make_plan_wo(db_session, [ready(wc)], priority=3, due_date=today + timedelta(days=9))
        sooner, _ = make_plan_wo(
            db_session, [ready(wc)], priority=3, due_date=today + timedelta(days=30), must_ship_by=today
        )
        urgent, _ = make_plan_wo(db_session, [ready(wc)], priority=1)

        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        assert [lane["work_order_id"] for lane in body["work_orders"]] == [
            first.id,
            second.id,
            urgent.id,
            sooner.id,
            later.id,
            no_promise.id,
        ]
        assert [lane["dispatch_position"] for lane in body["work_orders"][:2]] == [1, 2]
        assert lane_for(body, sooner.id)["promise_date"] == today.isoformat()
        assert body["ranked_count"] == 2 and body["unranked_count"] == 4

    def test_terminal_and_deleted_work_orders_are_not_lanes(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        live, _ = make_plan_wo(db_session, [ready(wc)])
        done, _ = make_plan_wo(db_session, [(wc, OperationStatus.COMPLETE, 10)], status=WorkOrderStatus.COMPLETE)
        gone, _ = make_plan_wo(db_session, [ready(wc)])
        db_session.get(WorkOrder, gone.id).soft_delete(manager.id)
        db_session.commit()
        ids = [
            lane["work_order_id"] for lane in client.get(LANES_URL, headers=user_headers(manager)).json()["work_orders"]
        ]
        assert live.id in ids and done.id not in ids and gone.id not in ids

    def test_lane_shape_modes_positions_crew_blockers_and_ties(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        welder = make_user(db_session, role=UserRole.OPERATOR, first_name="Jon", last_name="Werthen")
        saw = make_work_center(db_session)
        brake = make_work_center(db_session)
        # A sequenced routing: OP10 done, OP20 running at the brake, OP30 waiting.
        routing, (r10, r20, r30) = make_plan_wo(
            db_session,
            [
                (saw, OperationStatus.COMPLETE, 10),
                (brake, OperationStatus.IN_PROGRESS, 20),
                (saw, OperationStatus.PENDING, 30),
            ],
            dispatch_rank=1,
            unit_number="U-7",
        )
        # A pool: two ready at the brake, one held at the saw.
        pool, (p1, p2, p3) = make_plan_wo(
            db_session,
            [ready(brake, 10), ready(brake, 20), (saw, OperationStatus.ON_HOLD, 30)],
            sequential=False,
        )
        db_session.add(
            TimeEntry(
                user_id=welder.id,
                work_order_id=routing.id,
                operation_id=r20.id,
                work_center_id=brake.id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow(),
                company_id=COMPANY_A,
            )
        )
        db_session.add(
            WorkOrderBlocker(work_order_id=pool.id, operation_id=p3.id, title="Waiting on steel", company_id=COMPANY_A)
        )
        db_session.commit()
        tie_operation(db_session, p1, make_sheet_part(db_session, on_hand=0.0))

        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        r_lane = lane_for(body, routing.id)
        assert r_lane["mode"] == "routing" and r_lane["sequential_operations"] is True
        assert r_lane["unit_number"] == "U-7"
        assert [op["operation_id"] for op in r_lane["operations"]] == [r10.id, r20.id, r30.id]
        assert r_lane["ops_total"] == 3 and r_lane["ops_complete"] == 1
        by_id = {op["operation_id"]: op for op in r_lane["operations"]}
        assert by_id[r20.id]["crew"] == [{"user_id": welder.id, "display_name": "Jon W."}]
        assert by_id[r20.id]["run_position"] == 1
        assert by_id[r30.id]["blocked_by_predecessor"] is True
        assert by_id[r30.id]["run_position"] is None
        assert by_id[r10.id]["work_center_code"] == saw.code
        assert by_id[r20.id]["quantity_target"] == 10.0

        p_lane = lane_for(body, pool.id)
        assert p_lane["mode"] == "pool" and p_lane["dispatch_position"] is None
        assert p_lane["open_blocker_count"] == 1
        assert p_lane["any_material_short"] is True
        p_ops = {op["operation_id"]: op for op in p_lane["operations"]}
        assert p_ops[p3.id]["on_hold"] is True
        assert p_ops[p1.id]["material_tie"]["short_by"] > 0
        # The pool is unplanned and machine-unranked, so it carries no RUN chip.
        assert p_ops[p1.id]["run_position"] is None and p_ops[p2.id]["run_position"] is None
        # Grouped by machine in code order (the saw was created first, so it sorts
        # first), the machine queue's own order inside a machine.
        assert saw.code < brake.code
        assert [op["operation_id"] for op in p_lane["operations"]] == [p3.id, p1.id, p2.id]
        assert {wc["id"] for wc in body["work_centers"]} >= {saw.id, brake.id}

    def test_blocked_flag_is_the_shared_predecessor_rule(self, client: TestClient, db_session: Session, monkeypatch):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        # Pooled, same machine, predecessor not complete: the shared rule waives it.
        pool, (_, pool_pending) = make_plan_wo(
            db_session, [ready(wc, 10), (wc, OperationStatus.PENDING, 20)], sequential=False
        )
        # Sequenced, same machine: the shared rule blocks.
        routed, (_, routed_pending) = make_plan_wo(db_session, [ready(wc, 10), (wc, OperationStatus.PENDING, 20)])
        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        assert lane_for(body, pool.id)["operations"][1]["blocked_by_predecessor"] is False
        assert lane_for(body, routed.id)["operations"][1]["blocked_by_predecessor"] is True

        seen = []

        def _spy(candidate, incomplete, allow_same_work_center=True):
            seen.append((candidate.id, allow_same_work_center))
            return False

        monkeypatch.setattr(dispatch_plan_service, "operation_blocked_by_incomplete_predecessors", _spy)
        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        assert sorted(seen) == sorted([(pool_pending.id, True), (routed_pending.id, False)])
        assert lane_for(body, routed.id)["operations"][1]["blocked_by_predecessor"] is False

    def test_laser_lanes_are_never_predecessor_blocked(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        laser, (_, nest2) = make_plan_wo(
            db_session, [(wc, OperationStatus.ON_HOLD, 10), (wc, OperationStatus.PENDING, 20)], laser=True
        )
        lane = lane_for(client.get(LANES_URL, headers=user_headers(manager)).json(), laser.id)
        assert lane["mode"] == "laser" and lane["is_laser"] is True
        assert {op["operation_id"]: op["blocked_by_predecessor"] for op in lane["operations"]}[nest2.id] is False

    def test_cancelled_nest_tombstones_are_left_off(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        live_op, _ = make_nest_operation(db_session, work_center=wc)
        tomb_op, tomb_nest = make_nest_operation(db_session, work_center=wc, is_deleted=True)
        tomb_op = db_session.get(WorkOrderOperation, tomb_op.id)
        tomb_op.status = OperationStatus.ON_HOLD
        db_session.commit()
        body = client.get(LANES_URL, headers=user_headers(manager)).json()
        tomb_lane = lane_for(body, tomb_op.work_order_id)
        assert tomb_lane["operations"] == [] and tomb_lane["ops_total"] == 0
        live_lane = lane_for(body, live_op.work_order_id)
        assert [op["operation_id"] for op in live_lane["operations"]] == [live_op.id]
        assert live_lane["operations"][0]["laser_nest"]["cnc_number"]

    def test_the_read_writes_nothing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        nest_op, nest = make_nest_operation(db_session, work_center=wc, quantity_complete=3.0, completed_runs=0.0)
        make_plan_wo(db_session, [ready(wc), (wc, OperationStatus.PENDING, 20)], dispatch_rank=1)
        db_session.expire_all()
        audits = db_session.query(AuditLog).count()
        events = db_session.query(OperationalEvent).count()
        resp = client.get(LANES_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.query(AuditLog).count() == audits
        assert db_session.query(OperationalEvent).count() == events
        # The kiosk would have synced this counter; the board must not.
        assert db_session.get(LaserNest, nest.id).completed_runs == 0.0
        assert lane_for(resp.json(), nest_op.work_order_id)["operations"][0]["laser_nest"]["completed_runs"] == 3.0

    def test_query_count_is_flat_in_work_orders_and_operations(self, db_session: Session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        brake = make_work_center(db_session)
        saw = make_work_center(db_session)

        def _job(rank):
            wo, (op1, _op2) = make_plan_wo(
                db_session,
                [ready(brake, 10), (saw, OperationStatus.PENDING, 20)],
                dispatch_rank=rank,
            )
            tie_operation(db_session, op1, make_sheet_part(db_session))
            db_session.add(
                TimeEntry(
                    user_id=operator.id,
                    work_order_id=wo.id,
                    operation_id=op1.id,
                    work_center_id=brake.id,
                    entry_type=TimeEntryType.RUN,
                    clock_in=datetime.utcnow(),
                    clock_out=None if rank == 1 else datetime.utcnow(),
                    company_id=COMPANY_A,
                )
            )
            db_session.add(WorkOrderBlocker(work_order_id=wo.id, title="hold", company_id=COMPANY_A))
            db_session.commit()

        _job(1)
        db_session.expire_all()
        one = count_statements(db_session, lambda: dispatch_plan_service.build_work_order_board(db_session, COMPANY_A))
        for rank in range(2, 7):
            _job(rank)
        db_session.expire_all()
        six = count_statements(db_session, lambda: dispatch_plan_service.build_work_order_board(db_session, COMPANY_A))
        assert six == one
        # live WOs, ops, plan positions, queue tuples, roster, blockers, ties (3), work centers.
        assert one == 10
