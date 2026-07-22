"""Dispatch run order: queue ordering, the manager board, and the rank rewrite.

Covers the surfaces that share ``app.services.dispatch_service``:

* ``GET /shop-floor/work-center-queue/{id}``   — operator/crew kiosk queue read
* ``GET /shop-floor/dispatch-board``           — manager board (all active WCs)
* ``PUT /shop-floor/work-centers/{id}/run-order`` — the rank rewrite
* ``GET /shop-floor/operations``               — desktop shop-floor list (canonical
  order + the same gap-free ``run_order`` the kiosk RUN chip shows)

Headline invariants:
1. ORDER — ranked work (``run_order`` 1..N) sorts before unranked (NULL) work,
   in the manager's dense order, with a deterministic tiebreak. NULLS-LAST is
   asserted on the DB the suite actually runs (SQLite, which sorts NULLs FIRST
   by default — the bug this replaces).
2. ADVISORY — the rank never gates anything; it only orders/labels the queue.
3. EXACTLY AS SUBMITTED — omitted operations in the column are unranked, so a
   partial submission cannot leave stale ranks behind.
4. TENANCY + RBAC — planner tier only; foreign work centers are 404, foreign
   operations never appear or become rankable.
"""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.services import dispatch_service
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    ensure_company,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BOARD_URL = "/api/v1/shop-floor/dispatch-board"


def run_order_url(work_center_id: int) -> str:
    return f"/api/v1/shop-floor/work-centers/{work_center_id}/run-order"


def _queue_ids(client: TestClient, work_center_id: int, headers: dict) -> list:
    resp = client.get(queue_url(work_center_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return [row["operation_id"] for row in resp.json()["queue"]]


def _column(payload: dict, work_center_id: int) -> dict:
    for column in payload["work_centers"]:
        if column["id"] == work_center_id:
            return column
    raise AssertionError(f"work center {work_center_id} missing from board")


# --------------------------------------------------------------------------
# 1. Queue ordering
# --------------------------------------------------------------------------


class TestQueueOrdering:
    def test_ranked_operations_sort_before_unranked(self, client: TestClient, db_session: Session):
        """NULLS LAST, asserted on SQLite (which sorts NULLs FIRST by default).

        The unranked op is deliberately the *lower* id and the *earlier* due
        date, so only an explicit NULLS-LAST key can put it behind the ranked
        one — a plain ``ORDER BY run_order`` would sort it first here.
        """
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, unranked = make_wo_with_operation(db_session, work_center=wc)
        _, ranked = make_wo_with_operation(db_session, work_center=wc)

        unranked.work_order.due_date = date.today()
        ranked.work_order.due_date = date.today() + timedelta(days=30)
        ranked.run_order = 1
        db_session.commit()

        assert _queue_ids(client, wc.id, user_headers(manager)) == [ranked.id, unranked.id]

    def test_dense_rank_order_is_respected(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        # Rank them in reverse id order: the sort must follow run_order, not id.
        for rank, op in enumerate(reversed(ops), start=1):
            op.run_order = rank
        db_session.commit()

        assert _queue_ids(client, wc.id, user_headers(manager)) == [ops[2].id, ops[1].id, ops[0].id]

    def test_unranked_tail_falls_back_to_priority_due_date_then_id(self, client: TestClient, db_session: Session):
        """Unranked work keeps the repo's canonical fallback, id as final tiebreak."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, low_priority = make_wo_with_operation(db_session, work_center=wc)
        _, hot = make_wo_with_operation(db_session, work_center=wc)
        _, tie_a = make_wo_with_operation(db_session, work_center=wc)
        _, tie_b = make_wo_with_operation(db_session, work_center=wc)

        hot.work_order.priority = 1
        low_priority.work_order.priority = 5
        low_priority.work_order.due_date = date.today()
        # tie_a / tie_b tie on priority, due_date and sequence (all 10) -> only
        # the id tiebreak makes the result deterministic.
        for op in (tie_a, tie_b):
            op.work_order.priority = 5
            op.work_order.due_date = date.today() + timedelta(days=5)
        db_session.commit()

        ordered = _queue_ids(client, wc.id, user_headers(manager))
        assert ordered[0] == hot.id
        assert ordered.index(low_priority.id) < ordered.index(tie_a.id)
        assert ordered.index(tie_a.id) < ordered.index(tie_b.id)

    def test_queue_row_exposes_run_order_as_a_gap_free_position(self, client: TestClient, db_session: Session):
        """The wire value is the job's POSITION in the queue, not the raw stored
        rank: a lone ranked job is "RUN 1" even if its stored rank drifted to 7
        (jobs ahead of it completed or moved). See dispatch_service.display_positions."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        op.run_order = 7
        db_session.commit()

        resp = client.get(queue_url(wc.id), headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["queue"][0]["run_order"] == 1
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order == 7  # storage untouched


# --------------------------------------------------------------------------
# 2. GET /shop-floor/dispatch-board
# --------------------------------------------------------------------------


class TestDispatchBoard:
    def test_includes_every_active_work_center_including_empty_ones(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        busy = make_work_center(db_session)
        idle = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=busy)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK
        payload = resp.json()

        assert [row["operation_id"] for row in _column(payload, busy.id)["queue"]] == [op.id]
        # An idle machine must still render a column so work can be dragged onto it.
        assert _column(payload, idle.id)["queue"] == []
        # Active columns say so -- the client's read-only rendering keys off this.
        assert _column(payload, busy.id)["is_active"] is True
        assert _column(payload, idle.id)["is_active"] is True

    def test_inactive_work_center_with_empty_queue_is_omitted(self, client: TestClient, db_session: Session):
        """A deactivated machine with NOTHING queued has no reason to be on the
        board. Only a deactivated machine still HOLDING queued work earns a
        flagged column (next test)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        retired = make_work_center(db_session)
        retired.is_active = False
        db_session.commit()

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        ids = [c["id"] for c in resp.json()["work_centers"]]
        assert retired.id not in ids

    def test_deactivated_work_center_with_queued_work_is_a_flagged_column(
        self, client: TestClient, db_session: Session
    ):
        """Deactivating a machine must not hide queued work from the planner:
        the column stays on the board flagged ``is_active: false``, and its rows
        are exactly what the kiosk still serves for that machine (one shared
        query -- the operator's tablet and the flagged column cannot disagree)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        wc.is_active = False
        db_session.commit()

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        column = _column(resp.json(), wc.id)
        assert column["is_active"] is False
        board_ids = [row["operation_id"] for row in column["queue"]]
        assert board_ids == [op.id]
        assert board_ids == _queue_ids(client, wc.id, user_headers(manager))

    def test_legacy_null_is_active_with_queued_work_surfaces_flagged(self, client: TestClient, db_session: Session):
        """A legacy SQL-NULL ``is_active`` row must surface as a flagged column,
        not vanish from both board halves (``.isnot(True)``, not ``== False``).
        The update endpoint drops explicit nulls, so NULL is only ever
        pre-existing data -- but pre-existing data is exactly what the flagged
        column exists to repair."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        wc.is_active = None
        db_session.commit()

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        column = _column(resp.json(), wc.id)
        assert column["is_active"] is False
        assert [row["operation_id"] for row in column["queue"]] == [op.id]

    def test_queue_filters_match_the_kiosk_queue(self, client: TestClient, db_session: Session):
        """Pending ops, completed ops, terminal WOs and deleted WOs stay off the board."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, live = make_wo_with_operation(db_session, work_center=wc)
        _, pending = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.PENDING)
        _, done = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.COMPLETE)
        cancelled_wo, cancelled_op = make_wo_with_operation(db_session, work_center=wc)
        cancelled_wo.status = WorkOrderStatus.CANCELLED
        deleted_wo, deleted_op = make_wo_with_operation(db_session, work_center=wc)
        deleted_wo.is_deleted = True
        db_session.commit()

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        board_ids = [row["operation_id"] for row in _column(resp.json(), wc.id)["queue"]]
        assert board_ids == [live.id]
        for excluded in (pending.id, done.id, cancelled_op.id, deleted_op.id):
            assert excluded not in board_ids
        assert board_ids == _queue_ids(client, wc.id, user_headers(manager))

    def test_tenant_isolation(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        wc_a = make_work_center(db_session, company_id=COMPANY_A)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        _, op_a = make_wo_with_operation(db_session, company_id=COMPANY_A, work_center=wc_a)
        _, op_b = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=wc_b)

        payload = client.get(BOARD_URL, headers=user_headers(manager_a)).json()
        wc_ids = [c["id"] for c in payload["work_centers"]]
        assert wc_a.id in wc_ids
        assert wc_b.id not in wc_ids
        all_op_ids = [row["operation_id"] for c in payload["work_centers"] for row in c["queue"]]
        assert op_a.id in all_op_ids
        assert op_b.id not in all_op_ids

    def test_operator_is_forbidden(self, client: TestClient, db_session: Session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        resp = client.get(BOARD_URL, headers=user_headers(operator))
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------
# 3. PUT /shop-floor/work-centers/{id}/run-order
# --------------------------------------------------------------------------


class TestSetRunOrder:
    def test_assigns_dense_ranks_and_returns_refreshed_queue(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        desired = [ops[2].id, ops[0].id, ops[1].id]

        resp = client.put(run_order_url(wc.id), json={"operation_ids": desired}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert [row["operation_id"] for row in body["queue"]] == desired
        assert [row["run_order"] for row in body["queue"]] == [1, 2, 3]
        assert body["id"] == wc.id
        assert body["code"] == wc.code

        db_session.expire_all()
        assert [db_session.get(WorkOrderOperation, op_id).run_order for op_id in desired] == [1, 2, 3]
        # The kiosk sees the same order — one shared query, no drift.
        assert _queue_ids(client, wc.id, user_headers(manager)) == desired

    def test_omitted_operations_are_unranked(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        for rank, op in enumerate(ops, start=1):
            op.run_order = rank
        db_session.commit()

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [ops[1].id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).run_order == 1
        assert db_session.get(WorkOrderOperation, ops[0].id).run_order is None
        assert db_session.get(WorkOrderOperation, ops[2].id).run_order is None

    def test_empty_list_clears_the_column(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        op.run_order = 1
        db_session.commit()

        resp = client.put(run_order_url(wc.id), json={"operation_ids": []}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order is None

    def test_duplicate_ids_rejected(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [op.id, op.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(op.id) in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order is None

    def test_operation_at_another_work_center_rejected(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        other = make_work_center(db_session)
        _, mine = make_wo_with_operation(db_session, work_center=wc)
        _, elsewhere = make_wo_with_operation(db_session, work_center=other)

        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [mine.id, elsewhere.id]},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(elsewhere.id) in resp.json()["detail"]
        # Refused wholesale: nothing partially applied.
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, mine.id).run_order is None

    def test_non_queued_operation_rejected(self, client: TestClient, db_session: Session):
        """A completed op is off the queue, so it can't be ranked (stale board)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, done = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.COMPLETE)

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [done.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(done.id) in resp.json()["detail"]

    def test_cross_tenant_operation_rejected(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        wc_a = make_work_center(db_session, company_id=COMPANY_A)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        _, op_b = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=wc_b)

        resp = client.put(run_order_url(wc_a.id), json={"operation_ids": [op_b.id]}, headers=user_headers(manager_a))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op_b.id).run_order is None

    def test_cross_tenant_work_center_is_404(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)

        resp = client.put(run_order_url(wc_b.id), json={"operation_ids": []}, headers=user_headers(manager_a))
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_inactive_work_center_is_404(self, client: TestClient, db_session: Session):
        """Holds even when the deactivated machine still HAS queued work: that
        queue renders as a flagged read-only board column, and read-only means
        the rewrite refuses it -- ordering work on a machine that can't run it
        is planning theatre."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        wc.is_active = False
        db_session.commit()

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [op.id]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order is None

    def test_payload_length_is_capped(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": list(range(1, 502))},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_operator_is_forbidden(self, client: TestClient, db_session: Session):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [op.id]}, headers=user_headers(operator))
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order is None

    def test_writes_one_work_center_audit_row_with_old_and_new_order(self, client: TestClient, db_session: Session):
        """Invariant 2: one manager action, ONE audit row against the work center."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(2)]
        ops[0].run_order = 1
        ops[1].run_order = 2
        db_session.commit()
        before = [ops[0].id, ops[1].id]
        after = [ops[1].id, ops[0].id]

        resp = client.put(run_order_url(wc.id), json={"operation_ids": after}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_center", AuditLog.resource_id == wc.id)
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_identifier == wc.code
        assert row.company_id == COMPANY_A
        assert row.extra_data["changes"]["run_order"]["old"] == before
        assert row.extra_data["changes"]["run_order"]["new"] == after


class TestRewriteDoesNotDisturbOptimisticLocking:
    """A rank is DISPLAY metadata: rewriting it must not invalidate anybody's
    in-flight edit.

    The ranks are written with Core ``update()`` statements precisely so the
    ORM's ``version_id_col`` counter is left alone. If someone "simplifies" that
    back to ``operation.run_order = rank``, a manager tidying a column would bump
    ``version`` on every card -- 409-ing an operator's concurrent production post
    or clock-out on a job that is running right now, and staling every card
    version the board just handed the client.
    """

    def test_rewrite_leaves_every_operation_version_untouched(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        # Include a RUNNING job: that is the case where a spurious version bump
        # actually costs an operator their clock-out.
        ops[0].status = OperationStatus.IN_PROGRESS
        db_session.commit()
        ids = [op.id for op in ops]
        versions_before = {op.id: op.version for op in ops}

        resp = client.put(run_order_url(wc.id), json={"operation_ids": [ids[2], ids[0]]}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        # Both the ranked rows AND the one that was unranked keep their version.
        assert {op_id: db_session.get(WorkOrderOperation, op_id).version for op_id in ids} == versions_before
        # ...and the version the response hands back is the live one, so the
        # board's cards stay usable for the cross-machine move straight after.
        for row in resp.json()["queue"]:
            assert row["version"] == versions_before[row["operation_id"]]

    def test_response_reflects_the_new_ranks_not_the_identity_map(self, client: TestClient, db_session: Session):
        """The Core UPDATEs bypass the identity map, so the service expires
        before re-reading. Without that, the PUT would echo the PRE-update ranks
        back at the client."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(2)]
        ids = [op.id for op in ops]
        assert (
            client.put(run_order_url(wc.id), json={"operation_ids": ids}, headers=user_headers(manager)).status_code
            == status.HTTP_200_OK
        )

        reversed_ids = list(reversed(ids))
        resp = client.put(run_order_url(wc.id), json={"operation_ids": reversed_ids}, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [row["operation_id"] for row in resp.json()["queue"]] == reversed_ids
        assert [row["run_order"] for row in resp.json()["queue"]] == [1, 2]
        db_session.expire_all()
        assert [db_session.get(WorkOrderOperation, op_id).run_order for op_id in reversed_ids] == [1, 2]

    def test_service_turns_a_stale_write_into_409_not_500(self, db_session: Session, monkeypatch):
        """The documented 409 is exercised directly against the service.

        The rank write itself no longer emits a versioned UPDATE, so a
        ``StaleDataError`` can only arrive from something else pending on the
        request session at the same flush. The handler must still be there, must
        roll back, and must surface a 409 rather than letting the error escape as
        a 500 with a dirty session (the bug this replaces: the endpoint's handler
        wrapped only ``db.commit()``, which this transaction never reaches first).
        """
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)

        def _stale_flush(*args, **kwargs):
            raise StaleDataError("simulated concurrent write")

        monkeypatch.setattr(db_session, "flush", _stale_flush)

        with pytest.raises(HTTPException) as excinfo:
            dispatch_service.apply_run_order_or_http(db_session, COMPANY_A, wc, [op.id])

        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert excinfo.value.detail == dispatch_service.RUN_ORDER_CONFLICT_DETAIL
        # Rolled back: the half-written rank never survives the failure.
        monkeypatch.undo()
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).run_order is None


# --------------------------------------------------------------------------
# 4. The rank clears when the operation leaves the column
# --------------------------------------------------------------------------


class TestRankClearedOnWorkCenterChange:
    def test_cleared_by_work_orders_update_operation(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=source)
        op.run_order = 3
        db_session.commit()
        op_id, op_version = op.id, op.version

        resp = client.put(
            f"/api/v1/work-orders/operations/{op_id}",
            json={"version": op_version, "work_center_id": target.id},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        moved = db_session.get(WorkOrderOperation, op_id)
        assert moved.work_center_id == target.id
        assert moved.run_order is None

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id == op_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row.extra_data["changes"]["run_order"] == {"old": 3, "new": None}

    def test_cleared_by_scheduling_work_center_move(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=source)
        op.run_order = 2
        db_session.commit()
        op_id = op.id

        resp = client.put(
            f"/api/v1/scheduling/operations/{op_id}/work-center",
            json={"work_center_id": target.id},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        moved = db_session.get(WorkOrderOperation, op_id)
        assert moved.work_center_id == target.id
        assert moved.run_order is None

        audit_row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id == op_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row.extra_data["changes"]["run_order"] == {"old": 2, "new": None}

    def test_cleared_by_scheduling_schedule_route(self, client: TestClient, db_session: Session):
        """``PUT /scheduling/work-orders/{id}/schedule`` may carry a
        ``work_center_id`` (the Scheduling page's Schedule modal does), which
        makes it a MOVE as well as a reschedule. The rank must not ride along:
        the destination column's displayed order has to stay exactly the
        manager's, with the newcomer unranked at the tail rather than wedged in
        as an intruder that pushes the real rank 2 down to RUN 3."""
        self._assert_schedule_route_drops_the_rank(client, db_session, route="schedule")

    def test_cleared_by_scheduling_schedule_earliest_route(self, client: TestClient, db_session: Session):
        """Same for ``POST /scheduling/work-orders/{id}/schedule-earliest``,
        the other reschedule path that reassigns the current operation."""
        self._assert_schedule_route_drops_the_rank(client, db_session, route="schedule-earliest")

    @staticmethod
    def _assert_schedule_route_drops_the_rank(client: TestClient, db_session: Session, *, route: str):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        # The destination is a column the manager already ordered: 1 then 2.
        _, dest_first = make_wo_with_operation(db_session, work_center=target)
        _, dest_second = make_wo_with_operation(db_session, work_center=target)
        # ...and the mover is rank 1 in ITS column, so a carried rank would tie
        # with dest_first and land the intruder at RUN 2.
        mover_wo, mover = make_wo_with_operation(db_session, work_center=source)
        dest_ids = [dest_first.id, dest_second.id]
        mover_id, mover_wo_id = mover.id, mover_wo.id

        assert (
            client.put(run_order_url(target.id), json={"operation_ids": dest_ids}, headers=headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            client.put(run_order_url(source.id), json={"operation_ids": [mover_id]}, headers=headers).status_code
            == status.HTTP_200_OK
        )

        if route == "schedule":
            resp = client.put(
                f"/api/v1/scheduling/work-orders/{mover_wo_id}/schedule",
                json={"scheduled_start": date.today().isoformat(), "work_center_id": target.id},
                headers=headers,
            )
        else:
            resp = client.post(
                f"/api/v1/scheduling/work-orders/{mover_wo_id}/schedule-earliest",
                json={"work_center_id": target.id},
                headers=headers,
            )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        moved = db_session.get(WorkOrderOperation, mover_id)
        assert moved.work_center_id == target.id
        assert moved.run_order is None

        board = client.get(BOARD_URL, headers=headers)
        assert board.status_code == status.HTTP_200_OK, board.text
        column = _column(board.json(), target.id)
        assert [row["operation_id"] for row in column["queue"]] == dest_ids + [mover_id]
        # The manager's ranks, unchanged, and no duplicate stored rank in the column.
        assert [row["run_order"] for row in column["queue"]] == [1, 2, None]

    def test_moved_operation_lands_unranked_at_the_tail(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        _, incumbent = make_wo_with_operation(db_session, work_center=target)
        _, mover = make_wo_with_operation(db_session, work_center=source)
        incumbent.run_order = 1
        mover.run_order = 1
        db_session.commit()
        mover_id = mover.id

        resp = client.put(
            f"/api/v1/scheduling/operations/{mover_id}/work-center",
            json={"work_center_id": target.id},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert _queue_ids(client, target.id, user_headers(manager)) == [incumbent.id, mover_id]


class TestSchedulingRoutesAreTenantScoped:
    """The scheduling reschedule routes load the WO and target work center
    through helpers that had NO company filter, so a planner in company A could
    reschedule -- and reassign the machine of -- company B's work order."""

    def test_foreign_work_order_cannot_be_rescheduled(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        attacker = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
        victim_wc = make_work_center(db_session, company_id=COMPANY_B)
        _, victim_op = make_wo_with_operation(db_session, work_center=victim_wc, company_id=COMPANY_B)
        before = victim_op.work_center_id

        resp = client.put(
            f"/api/v1/scheduling/work-orders/{victim_op.work_order_id}/schedule",
            json={"scheduled_start": str(date.today() + timedelta(days=1))},
            headers=user_headers(attacker),
        )

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, victim_op.id).work_center_id == before

    def test_foreign_work_center_cannot_be_targeted(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        manager = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
        own_wc = make_work_center(db_session, company_id=COMPANY_A)
        foreign_wc = make_work_center(db_session, company_id=COMPANY_B)
        _, op = make_wo_with_operation(db_session, work_center=own_wc, company_id=COMPANY_A)

        resp = client.put(
            f"/api/v1/scheduling/work-orders/{op.work_order_id}/schedule",
            json={"scheduled_start": str(date.today() + timedelta(days=1)), "work_center_id": foreign_wc.id},
            headers=user_headers(manager),
        )

        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).work_center_id == own_wc.id


# --------------------------------------------------------------------------
# 5. GET /shop-floor/operations — the desktop shop-floor list
# --------------------------------------------------------------------------

OPERATIONS_URL = "/api/v1/shop-floor/operations"


def _operations_rows(client: TestClient, headers: dict, **params) -> list:
    resp = client.get(OPERATIONS_URL, headers=headers, params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["operations"]


class TestDesktopOperationsRunOrder:
    """The desktop pages render the SERVER order verbatim (owner decision), so the
    /operations payload must be in canonical dispatch order and carry the same
    gap-free ``run_order`` the kiosk RUN chip shows."""

    def test_single_work_center_filter_matches_kiosk_queue_order(self, client: TestClient, db_session: Session):
        """Ranked-first, manager's order, unranked tail — byte-for-byte the kiosk
        queue order. The unranked op gets the earliest due date so only the
        NULLS-LAST run_order key can put it behind the ranked ones."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        _, unranked = make_wo_with_operation(db_session, work_center=wc)
        unranked.work_order.due_date = date.today()
        # Rank in reverse id order: the sort must follow run_order, not id.
        for rank, op in enumerate(reversed(ops), start=1):
            op.run_order = rank
        db_session.commit()

        kiosk_order = _queue_ids(client, wc.id, user_headers(manager))
        desktop_order = [row["id"] for row in _operations_rows(client, user_headers(manager), work_center_id=wc.id)]
        assert desktop_order == kiosk_order == [ops[2].id, ops[1].id, ops[0].id, unranked.id]

    def test_run_order_is_the_kiosk_gap_free_position(self, client: TestClient, db_session: Session):
        """Sparse stored ranks (1, 3) surface as positions (1, 2); unranked is null —
        identical numbers to the kiosk payload for the same operations."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, sparse_high = make_wo_with_operation(db_session, work_center=wc)
        _, sparse_low = make_wo_with_operation(db_session, work_center=wc)
        _, unranked = make_wo_with_operation(db_session, work_center=wc)
        sparse_high.run_order = 3
        sparse_low.run_order = 1
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id)
        assert [(row["id"], row["run_order"]) for row in rows] == [
            (sparse_low.id, 1),
            (sparse_high.id, 2),
            (unranked.id, None),
        ]

        kiosk = client.get(queue_url(wc.id), headers=user_headers(manager)).json()["queue"]
        assert [(row["operation_id"], row["run_order"]) for row in kiosk] == [
            (row["id"], row["run_order"]) for row in rows
        ]

    def test_rows_group_by_work_center_code_across_centers(self, client: TestClient, db_session: Session):
        """Unfiltered, the list follows the board's column order — WorkCenter.code —
        not creation/id order (codes are overridden to invert creation order)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc_created_first = make_work_center(db_session)
        wc_created_second = make_work_center(db_session)
        wc_created_first.code = "ZZZ-LAST"
        wc_created_second.code = "AAA-FIRST"
        _, op_zzz = make_wo_with_operation(db_session, work_center=wc_created_first)
        _, op_aaa = make_wo_with_operation(db_session, work_center=wc_created_second)
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager))
        assert [row["id"] for row in rows] == [op_aaa.id, op_zzz.id]

    def test_not_queued_operation_carries_no_run_order(self, client: TestClient, db_session: Session):
        """A PENDING op is on the desktop list (it excludes only COMPLETE by default)
        but NOT on the kiosk queue — its run_order must be null even when a stale
        stored rank exists, and it must not steal a position from queued work."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, pending = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.PENDING)
        _, ready = make_wo_with_operation(db_session, work_center=wc)
        pending.run_order = 1  # stale rank left behind by a status change
        ready.run_order = 2
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id)
        by_id = {row["id"]: row["run_order"] for row in rows}
        assert by_id[pending.id] is None
        # The READY op is the queue's only ranked member: position 1, not raw rank 2.
        assert by_id[ready.id] == 1
