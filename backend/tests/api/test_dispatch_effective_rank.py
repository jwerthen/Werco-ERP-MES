"""Dispatch v2 ordering semantics: the work-order plan inside the ONE shared queue sort.

Pinned here (spec section 2):

1. TWIN -- ``effective_dispatch_rank_expr`` (SQL) and ``effective_dispatch_rank``
   (Python) agree on every operation-status x work-order-status combination.
2. BYTE-IDENTICAL -- with every ``dispatch_rank`` NULL the queue order is exactly the
   pre-114 sort, rows and RUN chips alike.
3. WORK-ORDER ORDER WINS -- on the kiosk queue, the machine board,
   ``/shop-floor/operations`` and the scheduling RUN chip, all agreeing.
4. A HELD work order sinks out of the plan; PENDING / ON_HOLD operations of a planned
   work order do not float on ``/shop-floor/operations``.
5. Two work orders sharing a rank never interleave (work-order-id tiebreak).
6. NO N+1 -- the positions reads stay flat as the queue grows.
7. A HELD work order's stored ``run_order`` neither sorts nor labels (the
   ``effective_run_order_expr`` / ``effective_run_order`` twin), on the kiosk queue,
   the machine board, ``/shop-floor/operations`` and the lanes view; lifting the hold
   restores the stored order and its RUN chips with no write.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import dispatch_service
from app.services.work_order_state_service import TERMINAL_WO_STATUSES
from tests.api.dispatch_plan_helpers import (
    BOARD_URL,
    LANES_URL,
    count_statements,
    lane_for,
    make_plan_wo,
    operation_order_url,
    ready,
    run_order_url,
)
from tests.api.kiosk_test_helpers import COMPANY_A, make_user, make_work_center, queue_url, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OPERATIONS_URL = "/api/v1/shop-floor/operations"
SCHEDULING_URL = "/api/v1/scheduling/work-orders"


def _queue(client: TestClient, wc_id: int, headers: dict) -> list:
    resp = client.get(queue_url(wc_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return [(row["operation_id"], row["run_order"]) for row in resp.json()["queue"]]


def _column(client: TestClient, wc_id: int, headers: dict) -> dict:
    resp = client.get(BOARD_URL, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    for column in resp.json()["work_centers"]:
        if column["id"] == wc_id:
            return column
    raise AssertionError(f"work center {wc_id} missing")


def _operations(client: TestClient, wc_id: int, headers: dict, **params) -> list:
    resp = client.get(OPERATIONS_URL, params={"work_center_id": wc_id, **params}, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["operations"]


# --------------------------------------------------------------------------
# 1. The SQL / Python twin
# --------------------------------------------------------------------------


class TestEffectiveRankTwin:
    def test_sql_and_python_agree_over_the_full_status_matrix(self, db_session: Session):
        wc = make_work_center(db_session)
        cases = []
        for wo_status in WorkOrderStatus:
            for op_status in OperationStatus:
                for rank in (3, None):
                    wo, (op,) = make_plan_wo(db_session, [(wc, op_status, 10)], status=wo_status, dispatch_rank=rank)
                    cases.append((wo_status, op_status, rank, op.id))

        sql_value = dict(
            db_session.query(WorkOrderOperation.id, dispatch_service.effective_dispatch_rank_expr())
            .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
            .filter(WorkOrderOperation.id.in_([case[3] for case in cases]))
            .all()
        )
        for wo_status, op_status, rank, op_id in cases:
            operation = db_session.get(WorkOrderOperation, op_id)
            python_value = dispatch_service.effective_dispatch_rank(operation)
            expected = (
                rank
                if op_status in (OperationStatus.READY, OperationStatus.IN_PROGRESS)
                and wo_status != WorkOrderStatus.ON_HOLD
                else None
            )
            assert sql_value[op_id] == python_value == expected, (wo_status, op_status, rank)

    def test_a_null_work_order_status_yields_null_on_both_sides(self, db_session: Session):
        """SQL ``status != 'ON_HOLD'`` is NULL (not true) for a NULL status; the twin matches."""
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        wo.status = None
        db_session.commit()
        sql_value = (
            db_session.query(dispatch_service.effective_dispatch_rank_expr())
            .select_from(WorkOrderOperation)
            .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
            .filter(WorkOrderOperation.id == op.id)
            .scalar()
        )
        assert sql_value is None
        assert dispatch_service.effective_dispatch_rank(db_session.get(WorkOrderOperation, op.id)) is None


# --------------------------------------------------------------------------
# 2. All ranks NULL -> byte-identical to the pre-114 sort
# --------------------------------------------------------------------------


def _legacy_order(db: Session, wc_id: int) -> list:
    """The pre-114 ``queue_order_by`` verbatim, over the same filter set."""
    rows = (
        db.query(WorkOrderOperation.id, WorkOrderOperation.run_order)
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            WorkOrder.company_id == COMPANY_A,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
            WorkOrderOperation.status.in_(dispatch_service.QUEUE_OPERATION_STATUSES),
            WorkOrderOperation.work_center_id == wc_id,
        )
        .order_by(
            WorkOrderOperation.run_order.is_(None).asc(),
            WorkOrderOperation.run_order.asc(),
            WorkOrder.priority.asc(),
            WorkOrder.due_date.asc(),
            WorkOrderOperation.sequence.asc(),
            WorkOrderOperation.id.asc(),
        )
        .all()
    )
    return rows


class TestUnplannedOrderIsByteIdentical:
    def test_every_surface_matches_the_legacy_sort_when_no_work_order_is_planned(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        today = date.today()
        # A deliberately messy mix: run_order set/unset, priorities, NULL and set due
        # dates, a held work order, an in-progress op, shared sequences.
        make_plan_wo(db_session, [ready(wc, 10)], priority=5, due_date=today)
        make_plan_wo(db_session, [ready(wc, 20, run_order=2)], priority=9)
        make_plan_wo(db_session, [ready(wc, 10, run_order=1)], priority=1, due_date=today + timedelta(days=3))
        make_plan_wo(db_session, [(wc, OperationStatus.IN_PROGRESS, 30)], priority=1)
        make_plan_wo(db_session, [ready(wc, 10)], priority=1, due_date=today - timedelta(days=1))
        make_plan_wo(db_session, [ready(wc, 10)], status=WorkOrderStatus.ON_HOLD, priority=2)
        make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, priority=5, due_date=today)

        legacy = _legacy_order(db_session, wc.id)
        legacy_ids = [row[0] for row in legacy]
        legacy_positions = dispatch_service._dense_positions(
            (op_id, run_order is not None) for op_id, run_order in legacy
        )

        assert [row.operation_id for row in dispatch_service.queue_order_rows(db_session, COMPANY_A, [wc.id])] == (
            legacy_ids
        )
        assert [op.id for op in dispatch_service.queued_operations(db_session, COMPANY_A, [wc.id])] == legacy_ids
        expected_wire = [(op_id, legacy_positions[op_id]) for op_id in legacy_ids]
        assert _queue(client, wc.id, headers) == expected_wire
        column = _column(client, wc.id, headers)
        assert [(row["operation_id"], row["run_order"]) for row in column["queue"]] == expected_wire
        assert all(row["dispatch_position"] is None for row in column["queue"])
        ops = _operations(client, wc.id, headers)
        assert [(row["id"], row["run_order"]) for row in ops] == expected_wire


# --------------------------------------------------------------------------
# 3. Work-order order wins -- on every surface, in agreement
# --------------------------------------------------------------------------


class TestWorkOrderPrecedence:
    def test_plan_rank_beats_machine_rank_priority_and_due_date(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (hot,) = make_plan_wo(db_session, [ready(wc, run_order=1)], priority=1, due_date=date.today())
        _, (planned,) = make_plan_wo(db_session, [ready(wc)], priority=10, dispatch_rank=1)

        assert _queue(client, wc.id, user_headers(manager)) == [(planned.id, 1), (hot.id, 2)]

    def test_kiosk_board_operations_and_scheduling_agree(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        other = make_work_center(db_session)
        # P: planned 1. Q: planned 2. R: unplanned but hot and machine-ranked. S: unplanned.
        wo_p, (p_here, _) = make_plan_wo(
            db_session, [ready(wc, 10), (other, OperationStatus.PENDING, 20)], dispatch_rank=1, priority=9
        )
        wo_q, (q_here,) = make_plan_wo(db_session, [ready(wc, 10)], dispatch_rank=2, priority=9)
        wo_r, (r_here,) = make_plan_wo(db_session, [ready(wc, 10, run_order=1)], priority=1)
        wo_s, (s_here,) = make_plan_wo(db_session, [ready(wc, 10)], priority=1)

        expected = [(p_here.id, 1), (q_here.id, 2), (r_here.id, 3), (s_here.id, None)]
        assert _queue(client, wc.id, headers) == expected

        column = _column(client, wc.id, headers)
        assert [(row["operation_id"], row["run_order"]) for row in column["queue"]] == expected
        assert [row["dispatch_position"] for row in column["queue"]] == [1, 2, None, None]

        ops = _operations(client, wc.id, headers)
        assert [(row["id"], row["run_order"]) for row in ops] == expected

        rows = client.get(SCHEDULING_URL, params={"work_center_id": wc.id}, headers=headers)
        assert rows.status_code == status.HTTP_200_OK, rows.text
        chips = {row["current_operation_id"]: row["run_order"] for row in rows.json()}
        assert chips == dict(expected)

    def test_held_work_order_sinks_below_the_plan(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        _, (held,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1, status=WorkOrderStatus.ON_HOLD)
        _, (second,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=2)

        assert _queue(client, wc.id, headers) == [(second.id, 1), (held.id, None)]
        column = _column(client, wc.id, headers)
        # The held job is still IN the plan (position 1 on the lanes), but its row does
        # not claim a plan position it is not sorted by.
        assert [(row["operation_id"], row["dispatch_position"]) for row in column["queue"]] == [
            (second.id, 2),
            (held.id, None),
        ]
        assert [row["work_order_status"] for row in column["queue"]] == ["released", "on_hold"]

    def test_pending_and_held_operations_of_a_planned_work_order_do_not_float(
        self, client: TestClient, db_session: Session
    ):
        """``/shop-floor/operations`` lists PENDING and ON_HOLD rows under the shared
        sort. Without the effective-rank gate the planned job's blocked work would
        lead the list ahead of runnable work."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        upstream = make_work_center(db_session)
        _, (_, pending) = make_plan_wo(
            db_session, [ready(upstream, 10), (wc, OperationStatus.PENDING, 20)], dispatch_rank=1, priority=5
        )
        _, (held,) = make_plan_wo(db_session, [(wc, OperationStatus.ON_HOLD, 10)], dispatch_rank=2, priority=5)
        _, (runnable,) = make_plan_wo(db_session, [ready(wc)], priority=1)

        rows = _operations(client, wc.id, user_headers(operator))
        ids = [row["id"] for row in rows]
        assert ids[0] == runnable.id
        assert set(ids[1:]) == {pending.id, held.id}
        by_id = {row["id"]: row["run_order"] for row in rows}
        assert by_id[pending.id] is None and by_id[held.id] is None

    def test_work_orders_sharing_a_rank_never_interleave(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        # Same rank. Machine ranks and sequences chosen so that ANY key below the
        # work-order tiebreak would interleave the two jobs' operations.
        wo_x, (x1, x2) = make_plan_wo(
            db_session, [ready(wc, 10, run_order=1), ready(wc, 30, run_order=3)], sequential=False, dispatch_rank=4
        )
        wo_y, (y1, y2) = make_plan_wo(
            db_session, [ready(wc, 20, run_order=2), ready(wc, 40, run_order=4)], sequential=False, dispatch_rank=4
        )
        assert wo_x.id < wo_y.id
        assert [op_id for op_id, _ in _queue(client, wc.id, user_headers(manager))] == [x1.id, x2.id, y1.id, y2.id]

    def test_planned_block_rows_number_gap_free_before_the_machine_ranked_tail(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, (a1, a2) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=7)
        _, (b,) = make_plan_wo(db_session, [ready(wc, run_order=9)])
        _, (c,) = make_plan_wo(db_session, [ready(wc)])
        assert _queue(client, wc.id, user_headers(manager)) == [(a1.id, 1), (a2.id, 2), (b.id, 3), (c.id, None)]


# --------------------------------------------------------------------------
# 4. The machine view's new fields
# --------------------------------------------------------------------------


class TestMachineViewFields:
    def test_rows_carry_plan_position_sequence_status_and_the_column_its_hours(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        make_plan_wo(db_session, [ready(make_work_center(db_session))], dispatch_rank=1)  # plan #1 elsewhere
        _, (planned,) = make_plan_wo(db_session, [ready(wc, 20)], dispatch_rank=5)
        _, (loose,) = make_plan_wo(db_session, [ready(wc, 30)])

        column = _column(client, wc.id, user_headers(manager))
        rows = {row["operation_id"]: row for row in column["queue"]}
        # Gap-free over the WHOLE plan, not this column: rank 5 is plan position 2.
        assert rows[planned.id]["dispatch_position"] == 2
        assert rows[loose.id]["dispatch_position"] is None
        assert rows[planned.id]["sequence"] == 20 and rows[loose.id]["sequence"] == 30
        assert rows[planned.id]["work_order_status"] == "released"
        assert column["queued_hours"] == pytest.approx(2 * (0.5 + 1.25))

    def test_board_query_count_is_flat_in_planned_work_orders(self, db_session: Session):
        wc = make_work_center(db_session)
        make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        db_session.expire_all()
        one = count_statements(db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A))
        for rank in range(2, 7):
            make_plan_wo(db_session, [ready(wc)], dispatch_rank=rank)
        db_session.expire_all()
        six = count_statements(db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A))
        assert six == one
        # The pre-114 untied board cost (4) plus exactly ONE plan-position read.
        assert one == 5

    def test_an_unplanned_board_pays_nothing_for_plan_positions(self, db_session: Session):
        wc = make_work_center(db_session)
        make_plan_wo(db_session, [ready(wc)])
        db_session.expire_all()
        assert count_statements(db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A)) == 4


# --------------------------------------------------------------------------
# 5. No N+1 on the positions reads
# --------------------------------------------------------------------------


class TestPositionsReadsStayFlat:
    def test_shop_floor_operations_positions_do_not_lazy_load_work_orders(
        self, client: TestClient, db_session: Session
    ):
        """One row on the page, a whole queue behind it: the positions are computed
        over the FULL queue, and reading each row's plan rank must not cost a SELECT."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        for rank in (1, 2):
            make_plan_wo(db_session, [ready(wc)], dispatch_rank=rank)

        def _measure() -> int:
            # One unmeasured warm-up read first: the first read of a work order runs
            # its reconcile-on-read, which is not what this test is about.
            _operations(client, wc.id, headers, page_size=1)
            db_session.expire_all()
            return count_statements(db_session, lambda: _operations(client, wc.id, headers, page_size=1))

        small = _measure()
        for rank in range(3, 9):
            make_plan_wo(db_session, [ready(wc)], dispatch_rank=rank)
        large = _measure()
        assert large == small

    def test_scheduling_work_orders_query_count_is_flat(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        elsewhere = make_work_center(db_session)
        make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        for rank in (2, 3):
            make_plan_wo(db_session, [ready(elsewhere)], dispatch_rank=rank)

        def _get():
            resp = client.get(SCHEDULING_URL, params={"work_center_id": wc.id}, headers=headers)
            assert resp.status_code == status.HTTP_200_OK, resp.text

        def _measure() -> int:
            _get()  # unmeasured warm-up, as above
            db_session.expire_all()
            return count_statements(db_session, _get)

        small = _measure()
        for rank in range(4, 10):
            make_plan_wo(db_session, [ready(wc)], dispatch_rank=rank)
        large = _measure()
        assert large == small

    def test_display_positions_reads_no_query_on_shared_queue_rows(self, db_session: Session):
        wc = make_work_center(db_session)
        for rank in (1, 2, 3):
            make_plan_wo(db_session, [ready(wc)], dispatch_rank=rank)
        db_session.expire_all()
        operations = dispatch_service.queued_operations(db_session, COMPANY_A, [wc.id])
        cost = count_statements(db_session, lambda: dispatch_service.display_positions(operations))
        assert cost == 0
        assert list(dispatch_service.display_positions(operations).values()) == [1, 2, 3]


# --------------------------------------------------------------------------
# 6. A held work order's stored run_order neither sorts nor labels (BE-3)
# --------------------------------------------------------------------------


def _set_wo_status(client: TestClient, headers: dict, db: Session, work_order_id: int, new_status: str) -> None:
    """Change a work order's status through the real generic PUT (the path a supervisor uses)."""
    db.expire_all()
    version = db.get(WorkOrder, work_order_id).version
    resp = client.put(
        f"/api/v1/work-orders/{work_order_id}", json={"status": new_status, "version": version}, headers=headers
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def _lane_run_positions(client: TestClient, headers: dict, work_order_id: int) -> dict:
    resp = client.get(LANES_URL, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    lane = lane_for(resp.json(), work_order_id)
    return {
        "dispatch_position": lane["dispatch_position"],
        **{op["operation_id"]: op["run_position"] for op in lane["operations"]},
    }


def _stored_run_orders(db: Session, *operation_ids: int) -> list:
    db.expire_all()
    return [db.get(WorkOrderOperation, op_id).run_order for op_id in operation_ids]


class TestEffectiveRunOrderTwin:
    def test_sql_and_python_agree_over_the_full_status_matrix(self, db_session: Session):
        wc = make_work_center(db_session)
        cases = []
        for wo_status in WorkOrderStatus:
            for op_status in OperationStatus:
                for run_order in (2, None):
                    _, (op,) = make_plan_wo(db_session, [(wc, op_status, 10, run_order)], status=wo_status)
                    cases.append((wo_status, op_status, run_order, op.id))

        sql_value = dict(
            db_session.query(WorkOrderOperation.id, dispatch_service.effective_run_order_expr())
            .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
            .filter(WorkOrderOperation.id.in_([case[3] for case in cases]))
            .all()
        )
        for wo_status, op_status, run_order, op_id in cases:
            python_value = dispatch_service.effective_run_order(db_session.get(WorkOrderOperation, op_id))
            # The work order's hold is the ONLY gate: the operation's own status does not
            # matter (a PENDING row's stored rank orders /shop-floor/operations as before).
            expected = None if wo_status == WorkOrderStatus.ON_HOLD else run_order
            assert sql_value[op_id] == python_value == expected, (wo_status, op_status, run_order)

    def test_a_null_work_order_status_yields_null_on_both_sides(self, db_session: Session):
        wc = make_work_center(db_session)
        wo, (op,) = make_plan_wo(db_session, [ready(wc, run_order=1)])
        wo.status = None
        db_session.commit()
        sql_value = (
            db_session.query(dispatch_service.effective_run_order_expr())
            .select_from(WorkOrderOperation)
            .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
            .filter(WorkOrderOperation.id == op.id)
            .scalar()
        )
        assert sql_value is None
        assert dispatch_service.effective_run_order(db_session.get(WorkOrderOperation, op.id)) is None

    def test_queue_rows_carry_the_stored_and_the_effective_value(self, db_session: Session):
        wc = make_work_center(db_session)
        _, (held,) = make_plan_wo(db_session, [ready(wc, run_order=3)], status=WorkOrderStatus.ON_HOLD)
        _, (live,) = make_plan_wo(db_session, [ready(wc, run_order=4)])
        rows = {row.operation_id: row for row in dispatch_service.queue_order_rows(db_session, COMPANY_A, [wc.id])}
        assert (rows[held.id].run_order, rows[held.id].effective_run_order) == (3, None)
        assert (rows[live.id].run_order, rows[live.id].effective_run_order) == (4, 4)


class TestHeldWorkOrderRunOrder:
    """The planner's operation order (or a machine-column rank) is stored on the held
    work order's READY operations; a hold leaves both untouched. While the hold stands
    those rows must sort and label exactly like never-ranked work on every surface, and
    lifting the hold must bring the stored order and the RUN chips back with no write."""

    @pytest.fixture(autouse=True)
    def _no_outbox_enqueue(self, monkeypatch):
        import app.services.notification_outbox as notification_outbox

        monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", lambda event_id: None)

    def test_planned_held_work_order_with_an_operation_order_reads_no_run_chip_anywhere(
        self, client: TestClient, db_session: Session
    ):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        wo_a, (a1, a2) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=1)
        _, (u1,) = make_plan_wo(db_session, [ready(wc)], priority=1)
        resp = client.put(
            operation_order_url(wo_a.id),
            json={"operation_ids": [a2.id, a1.id], "expected_operation_ids": [a1.id, a2.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _stored_run_orders(db_session, a2.id, a1.id) == [1, 2]
        assert _queue(client, wc.id, headers) == [(a2.id, 1), (a1.id, 2), (u1.id, None)]

        _set_wo_status(client, headers, db_session, wo_a.id, "on_hold")

        # The priority-1 unplanned job leads; the held work order's rows follow it with
        # no RUN chip, in the priority/sequence fallback order (a1 seq 10, a2 seq 20).
        expected = [(u1.id, None), (a1.id, None), (a2.id, None)]
        assert _queue(client, wc.id, headers) == expected
        column = _column(client, wc.id, headers)
        assert [(row["operation_id"], row["run_order"]) for row in column["queue"]] == expected
        assert all(row["dispatch_position"] is None for row in column["queue"])
        ops = _operations(client, wc.id, headers)
        assert [(row["id"], row["run_order"]) for row in ops] == expected
        lane = _lane_run_positions(client, headers, wo_a.id)
        # Still IN the plan on the lanes view (CONTRACT 1.2), but no RUN position.
        assert lane == {"dispatch_position": 1, a1.id: None, a2.id: None}
        # Read-side only: the hold changed no stored value.
        assert _stored_run_orders(db_session, a2.id, a1.id) == [1, 2]

        _set_wo_status(client, headers, db_session, wo_a.id, "released")

        restored = [(a2.id, 1), (a1.id, 2), (u1.id, None)]
        assert _queue(client, wc.id, headers) == restored
        column = _column(client, wc.id, headers)
        assert [(row["operation_id"], row["run_order"]) for row in column["queue"]] == restored
        assert [(row["id"], row["run_order"]) for row in _operations(client, wc.id, headers)] == restored
        assert _lane_run_positions(client, headers, wo_a.id) == {"dispatch_position": 1, a1.id: 2, a2.id: 1}

    def test_unplanned_held_work_order_loses_its_machine_rank_until_the_hold_lifts(
        self, client: TestClient, db_session: Session
    ):
        """No plan rank involved: the machine-column ``run_order`` alone is what is gated."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        wo_h, (h1,) = make_plan_wo(db_session, [ready(wc, run_order=1)], priority=9)
        _, (u1,) = make_plan_wo(db_session, [ready(wc)], priority=1)
        assert _queue(client, wc.id, headers) == [(h1.id, 1), (u1.id, None)]

        _set_wo_status(client, headers, db_session, wo_h.id, "on_hold")
        expected = [(u1.id, None), (h1.id, None)]
        assert _queue(client, wc.id, headers) == expected
        assert [(row["operation_id"], row["run_order"]) for row in _column(client, wc.id, headers)["queue"]] == (
            expected
        )
        assert [(row["id"], row["run_order"]) for row in _operations(client, wc.id, headers)] == expected
        assert _lane_run_positions(client, headers, wo_h.id) == {"dispatch_position": None, h1.id: None}
        assert _stored_run_orders(db_session, h1.id) == [1]

        _set_wo_status(client, headers, db_session, wo_h.id, "released")
        assert _queue(client, wc.id, headers) == [(h1.id, 1), (u1.id, None)]
        assert _lane_run_positions(client, headers, wo_h.id) == {"dispatch_position": None, h1.id: 1}

    def test_a_column_rewrite_accepts_a_held_row_and_its_rank_takes_effect_on_unhold(
        self, client: TestClient, db_session: Session
    ):
        """Held rows interleave with never-ranked rows by priority, so refusing "held
        before runnable" would make the rendered column unsaveable. The held row's
        submitted rank is stored and only counts once the hold lifts."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        wo_h, (h1,) = make_plan_wo(db_session, [ready(wc)], priority=1, status=WorkOrderStatus.ON_HOLD)
        _, (u1,) = make_plan_wo(db_session, [ready(wc)], priority=5)
        rendered = [row["operation_id"] for row in _column(client, wc.id, headers)["queue"]]
        assert rendered == [h1.id, u1.id]

        resp = client.put(
            run_order_url(wc.id), json={"operation_ids": rendered, "expected_operation_ids": rendered}, headers=headers
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [(row["operation_id"], row["run_order"]) for row in resp.json()["queue"]] == [
            (u1.id, 1),
            (h1.id, None),
        ]
        assert _stored_run_orders(db_session, h1.id, u1.id) == [1, 2]

        _set_wo_status(client, headers, db_session, wo_h.id, "released")
        assert _queue(client, wc.id, headers) == [(h1.id, 1), (u1.id, 2)]

    def test_nothing_ranked_or_held_stays_byte_identical(self, client: TestClient, db_session: Session):
        """The gate changes nothing unless a HELD work order carries a stored rank: a held
        work order with no rank, and ranked work orders that are not held, sort exactly
        as the legacy (raw run_order) sort did."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        make_plan_wo(db_session, [ready(wc, run_order=2)], priority=9)
        make_plan_wo(db_session, [ready(wc)], status=WorkOrderStatus.ON_HOLD, priority=1)
        make_plan_wo(db_session, [ready(wc, run_order=1)], status=WorkOrderStatus.IN_PROGRESS)
        make_plan_wo(db_session, [ready(wc)], priority=3)
        legacy = _legacy_order(db_session, wc.id)
        positions = dispatch_service._dense_positions((op_id, run_order is not None) for op_id, run_order in legacy)
        assert _queue(client, wc.id, user_headers(manager)) == [(op_id, positions[op_id]) for op_id, _ in legacy]
