"""Dispatch run order, part 2: the operator-facing surface, and the rank over time.

``tests/api/test_dispatch_run_order.py`` pins the manager-side contract (the
board payload, the rewrite's validation matrix, RBAC/tenancy, the audit row).
This file covers what that one leaves open -- the things that only show up once
the rank is *lived with*:

* the CREW-STATION token path onto the same queue read (a station principal, not
  a user, is what the shop tablet actually holds);
* ordering INTERACTIONS -- rank vs priority/due date, and the fact that columns
  are independent (a rank is per-work-center, never global);
* the rank's LIFECYCLE -- an op moving columns, completing, or going on hold,
  and the column being re-ranked afterwards;
* SEQUENTIAL rewrites and a stale board (the second submission is authoritative;
  a refused one mutates nothing);
* the load-bearing product guarantee: ``run_order`` is ADVISORY. A last-ranked
  or entirely unranked operation can still be started and clocked into. If a
  future change makes the rank gate anything, these are the tests that fail.
* RANK-SURFACE PARITY for the desktop pages: ``GET /shop-floor/operations``
  (what /shop-floor and /shop-floor/operations render VERBATIM — owner decision:
  no client re-sort) serves the canonical order and the same gap-free per-column
  position the kiosk RUN chip shows, without changing its filter semantics.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    mint_badge_token,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BOARD_URL = "/api/v1/shop-floor/dispatch-board"
OPERATIONS_URL = "/api/v1/shop-floor/operations"


def run_order_url(work_center_id: int) -> str:
    return f"/api/v1/shop-floor/work-centers/{work_center_id}/run-order"


def _queue_rows(client: TestClient, work_center_id: int, headers: dict) -> list:
    resp = client.get(queue_url(work_center_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["queue"]


def _queue_ids(client: TestClient, work_center_id: int, headers: dict) -> list:
    return [row["operation_id"] for row in _queue_rows(client, work_center_id, headers)]


def _set_run_order(client: TestClient, work_center_id: int, operation_ids: list, headers: dict):
    return client.put(run_order_url(work_center_id), json={"operation_ids": operation_ids}, headers=headers)


def _column(payload: dict, work_center_id: int) -> dict:
    for column in payload["work_centers"]:
        if column["id"] == work_center_id:
            return column
    raise AssertionError(f"work center {work_center_id} missing from board")


def _operations_rows(client: TestClient, headers: dict, **params) -> list:
    resp = client.get(OPERATIONS_URL, headers=headers, params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["operations"]


# --------------------------------------------------------------------------
# 1. The operator-facing surface: the crew-station token reads the same order
# --------------------------------------------------------------------------


class TestKioskSurfacesTheRank:
    def test_crew_station_token_sees_the_manager_order(self, client: TestClient, db_session: Session):
        """The shop tablet holds a STATION token, not a user token -- and the
        station read is the same shared query, so it must show the manager's
        order and carry the rank on each row."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        desired = [ops[2].id, ops[0].id, ops[1].id]

        assert _set_run_order(client, wc.id, desired, user_headers(manager)).status_code == status.HTTP_200_OK

        rows = _queue_rows(client, wc.id, bearer(kiosk_token_for(station)))
        assert [row["operation_id"] for row in rows] == desired
        assert [row["run_order"] for row in rows] == [1, 2, 3]

    def test_operator_reads_the_rank_even_though_the_board_is_closed_to_them(
        self, client: TestClient, db_session: Session
    ):
        """The rank is information the operator is meant to SEE; setting it is
        the planner tier's job. Both halves in one test so a future RBAC change
        can't quietly take the rank away from the tablet."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)

        assert _set_run_order(client, wc.id, [op.id], user_headers(manager)).status_code == status.HTTP_200_OK

        rows = _queue_rows(client, wc.id, user_headers(operator))
        assert [row["run_order"] for row in rows] == [1]
        assert client.get(BOARD_URL, headers=user_headers(operator)).status_code == status.HTTP_403_FORBIDDEN

    def test_board_row_carries_the_lock_version_and_a_utc_z_timestamp(self, client: TestClient, db_session: Session):
        """Two envelope details the drag-and-drop client depends on: ``version``
        (the operation's optimistic-lock counter -- how a card is told stale from
        fresh) and ``generated_at`` served as UTC with a trailing Z, per the
        repo's store-UTC/serve-Z invariant."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)

        payload = client.get(BOARD_URL, headers=user_headers(manager)).json()
        row = _column(payload, wc.id)["queue"][0]
        db_session.expire_all()
        assert row["version"] == db_session.get(WorkOrderOperation, op.id).version
        assert payload["generated_at"].endswith("Z")


# --------------------------------------------------------------------------
# 2. Ordering interactions
# --------------------------------------------------------------------------


class TestOrderingInteractions:
    def test_rank_outranks_priority_and_due_date(self, client: TestClient, db_session: Session):
        """A ranked job leads its column even when every fallback key argues
        against it: worst priority, latest due date, highest id."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, hot = make_wo_with_operation(db_session, work_center=wc)
        _, ranked = make_wo_with_operation(db_session, work_center=wc)

        hot.work_order.priority = 1
        hot.work_order.due_date = date.today() - timedelta(days=10)
        ranked.work_order.priority = 9
        ranked.work_order.due_date = date.today() + timedelta(days=90)
        db_session.commit()

        assert _set_run_order(client, wc.id, [ranked.id], user_headers(manager)).status_code == status.HTTP_200_OK
        assert _queue_ids(client, wc.id, user_headers(manager)) == [ranked.id, hot.id]

    def test_priority_never_reshuffles_two_ranked_rows(self, client: TestClient, db_session: Session):
        """Between two RANKED rows the manager's order is final -- the priority
        fallback must not leak in and promote the hotter job."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, cold = make_wo_with_operation(db_session, work_center=wc)
        _, hot = make_wo_with_operation(db_session, work_center=wc)

        cold.work_order.priority = 9
        hot.work_order.priority = 1
        db_session.commit()

        assert _set_run_order(client, wc.id, [cold.id, hot.id], user_headers(manager)).status_code == status.HTTP_200_OK
        assert _queue_ids(client, wc.id, user_headers(manager)) == [cold.id, hot.id]

    def test_ranks_are_per_work_center_and_columns_do_not_interleave(self, client: TestClient, db_session: Session):
        """Rank 1 exists once PER COLUMN, not once per shop. Two columns each
        holding a rank 1/2 stay separate on the board and on each kiosk."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        left = make_work_center(db_session)
        right = make_work_center(db_session)
        left_ops = [make_wo_with_operation(db_session, work_center=left)[1] for _ in range(2)]
        right_ops = [make_wo_with_operation(db_session, work_center=right)[1] for _ in range(2)]
        left_order = [left_ops[1].id, left_ops[0].id]
        right_order = [right_ops[1].id, right_ops[0].id]

        assert _set_run_order(client, left.id, left_order, user_headers(manager)).status_code == status.HTTP_200_OK
        assert _set_run_order(client, right.id, right_order, user_headers(manager)).status_code == status.HTTP_200_OK

        payload = client.get(BOARD_URL, headers=user_headers(manager)).json()
        left_column = _column(payload, left.id)
        right_column = _column(payload, right.id)
        assert [row["operation_id"] for row in left_column["queue"]] == left_order
        assert [row["operation_id"] for row in right_column["queue"]] == right_order
        # Both columns carry a rank 1 and a rank 2 -- ranks are scoped, not global.
        assert [row["run_order"] for row in left_column["queue"]] == [1, 2]
        assert [row["run_order"] for row in right_column["queue"]] == [1, 2]
        # And neither kiosk sees the other machine's work.
        assert _queue_ids(client, left.id, user_headers(manager)) == left_order
        assert _queue_ids(client, right.id, user_headers(manager)) == right_order


# --------------------------------------------------------------------------
# 3. The rank's lifecycle
# --------------------------------------------------------------------------


class TestRankLifecycle:
    def test_move_clears_the_rank_and_the_destination_can_be_re_ranked(self, client: TestClient, db_session: Session):
        """Full loop: rank the source column, move an op out of it, then rank the
        DESTINATION column with the newcomer in the lead. The source column
        re-densifies on its next rewrite -- no stale 1..N holes anywhere."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        stayers = [make_wo_with_operation(db_session, work_center=source)[1] for _ in range(2)]
        _, mover = make_wo_with_operation(db_session, work_center=source)
        _, incumbent = make_wo_with_operation(db_session, work_center=target)
        stayer_ids = [op.id for op in stayers]
        mover_id, incumbent_id = mover.id, incumbent.id

        # Rank the source column: mover sits at rank 2, between the two stayers.
        source_order = [stayer_ids[0], mover_id, stayer_ids[1]]
        assert _set_run_order(client, source.id, source_order, user_headers(manager)).status_code == status.HTTP_200_OK
        assert (
            _set_run_order(client, target.id, [incumbent_id], user_headers(manager)).status_code == status.HTTP_200_OK
        )

        resp = client.put(
            f"/api/v1/scheduling/operations/{mover_id}/work-center",
            json={"work_center_id": target.id},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # It arrives unranked, behind the destination's ranked incumbent...
        assert _queue_ids(client, target.id, user_headers(manager)) == [incumbent_id, mover_id]
        # ...and the source keeps ranks 1 and 3 until it is rewritten (the hole is
        # invisible: order is what the operator sees, and it is unchanged).
        assert _queue_ids(client, source.id, user_headers(manager)) == stayer_ids

        # Now promote the newcomer at its new machine, and re-densify the source.
        promoted = [mover_id, incumbent_id]
        body = _set_run_order(client, target.id, promoted, user_headers(manager))
        assert body.status_code == status.HTTP_200_OK, body.text
        assert [row["run_order"] for row in body.json()["queue"]] == [1, 2]
        assert [row["operation_id"] for row in body.json()["queue"]] == promoted

        resurvey = _set_run_order(client, source.id, stayer_ids, user_headers(manager))
        assert resurvey.status_code == status.HTTP_200_OK, resurvey.text
        assert [row["run_order"] for row in resurvey.json()["queue"]] == [1, 2]

    def test_completed_operation_leaves_the_queue_and_survivors_re_rank_dense(
        self, client: TestClient, db_session: Session
    ):
        """Work finishing is the normal way a column shrinks. The survivors keep
        their relative order immediately, and the next rewrite closes the gap."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ranked = [ops[0].id, ops[1].id, ops[2].id]
        assert _set_run_order(client, wc.id, ranked, user_headers(manager)).status_code == status.HTTP_200_OK

        ops[1].status = OperationStatus.COMPLETE
        db_session.commit()

        rows = _queue_rows(client, wc.id, user_headers(manager))
        assert [row["operation_id"] for row in rows] == [ranked[0], ranked[2]]
        # STORAGE went sparse (1, 3) when the middle job completed, but what the
        # shop sees is renumbered densely -- "RUN 3" on a two-job queue would read
        # as a missing job. See dispatch_service.display_positions.
        assert [row["run_order"] for row in rows] == [1, 2]
        db_session.expire_all()
        assert [db_session.get(WorkOrderOperation, oid).run_order for oid in (ranked[0], ranked[2])] == [1, 3]

        redense = _set_run_order(client, wc.id, [ranked[0], ranked[2]], user_headers(manager))
        assert redense.status_code == status.HTTP_200_OK, redense.text
        assert [row["run_order"] for row in redense.json()["queue"]] == [1, 2]

    def test_hold_drops_the_row_and_resume_restores_it_at_its_rank(self, client: TestClient, db_session: Session):
        """ON_HOLD is not a queue status, so a held op vanishes from the column --
        but the hold does NOT erase the rank, so resuming puts it right back
        where the manager had it rather than at the tail."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ranked = [ops[0].id, ops[1].id, ops[2].id]
        assert _set_run_order(client, wc.id, ranked, user_headers(manager)).status_code == status.HTTP_200_OK

        held_id = ranked[1]
        hold = client.put(f"/api/v1/shop-floor/operations/{held_id}/hold", headers=user_headers(manager))
        assert hold.status_code == status.HTTP_200_OK, hold.text
        assert _queue_ids(client, wc.id, user_headers(manager)) == [ranked[0], ranked[2]]

        resume = client.put(f"/api/v1/shop-floor/operations/{held_id}/resume", headers=user_headers(manager))
        assert resume.status_code == status.HTTP_200_OK, resume.text
        assert _queue_ids(client, wc.id, user_headers(manager)) == ranked

    def test_rewrite_while_held_unranks_the_held_row_so_resume_lands_it_at_the_tail(
        self, client: TestClient, db_session: Session
    ):
        """The counterpart to the test above: a hold on its own preserves the
        rank, but a REWRITE that happens while the row is off the queue does not.

        A rewrite is authoritative for the WHOLE column, not just its live rows.
        Otherwise the held op keeps a stale rank the manager could not even see
        on the board, and on resume it silently outranks the jobs he ordered
        afterwards -- it would come back at RUN 2 here, ahead of the job he put
        second."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ranked = [op.id for op in ops]
        assert _set_run_order(client, wc.id, ranked, user_headers(manager)).status_code == status.HTTP_200_OK

        held_id = ranked[1]
        hold = client.put(f"/api/v1/shop-floor/operations/{held_id}/hold", headers=user_headers(manager))
        assert hold.status_code == status.HTTP_200_OK, hold.text

        # The manager re-ranks the two rows still on the board; the held one is
        # not on it, so it is not in the payload.
        rewritten = [ranked[2], ranked[0]]
        assert _set_run_order(client, wc.id, rewritten, user_headers(manager)).status_code == status.HTTP_200_OK
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, held_id).run_order is None

        resume = client.put(f"/api/v1/shop-floor/operations/{held_id}/resume", headers=user_headers(manager))
        assert resume.status_code == status.HTTP_200_OK, resume.text

        rows = _queue_rows(client, wc.id, user_headers(manager))
        assert [row["operation_id"] for row in rows] == rewritten + [held_id]
        assert [row["run_order"] for row in rows] == [1, 2, None]

    def test_held_operation_cannot_be_ranked_while_off_the_queue(self, client: TestClient, db_session: Session):
        """The rewrite's "live queued operation" rule covers ON_HOLD too, not just
        COMPLETE -- a held row is not on the board, so it is not rankable."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, live = make_wo_with_operation(db_session, work_center=wc)
        _, held = make_wo_with_operation(db_session, work_center=wc)
        held_id = held.id
        assert (
            client.put(f"/api/v1/shop-floor/operations/{held_id}/hold", headers=user_headers(manager)).status_code
            == status.HTTP_200_OK
        )

        resp = _set_run_order(client, wc.id, [held_id, live.id], user_headers(manager))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(held_id) in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, live.id).run_order is None


# --------------------------------------------------------------------------
# 4. Sequential rewrites and a stale board
# --------------------------------------------------------------------------


class TestSequentialRewrites:
    def test_second_rewrite_is_authoritative_and_unranks_what_it_omits(self, client: TestClient, db_session: Session):
        """Two rewrites in a row through the real endpoint: the second body is
        the whole truth for the column, so the dropped id ends up NULL rather
        than keeping a rank from the first call."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        first = [ops[0].id, ops[1].id, ops[2].id]
        second = [ops[2].id, ops[0].id]

        assert _set_run_order(client, wc.id, first, user_headers(manager)).status_code == status.HTTP_200_OK
        body = _set_run_order(client, wc.id, second, user_headers(manager))
        assert body.status_code == status.HTTP_200_OK, body.text

        assert [row["operation_id"] for row in body.json()["queue"]][:2] == second
        assert [row["run_order"] for row in body.json()["queue"]] == [1, 2, None]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, ops[1].id).run_order is None
        assert _queue_ids(client, wc.id, user_headers(manager)) == second + [ops[1].id]

    def test_stale_board_submission_is_refused_without_touching_any_rank(self, client: TestClient, db_session: Session):
        """The realistic race: the manager loaded the board, an operator finished
        a job, then the manager dragged. The whole submission is refused and the
        column is left EXACTLY as it was -- no half-applied reorder."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ids = [op.id for op in ops]
        assert _set_run_order(client, wc.id, ids, user_headers(manager)).status_code == status.HTTP_200_OK

        # ...operator completes the middle job between board load and submit.
        ops[1].status = OperationStatus.COMPLETE
        db_session.commit()

        resp = _set_run_order(client, wc.id, [ids[2], ids[1], ids[0]], user_headers(manager))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert str(ids[1]) in resp.json()["detail"]

        db_session.expire_all()
        assert [db_session.get(WorkOrderOperation, op_id).run_order for op_id in ids] == [1, 2, 3]

        # The refusal is recoverable: resubmitting what is actually live works.
        retry = _set_run_order(client, wc.id, [ids[2], ids[0]], user_headers(manager))
        assert retry.status_code == status.HTTP_200_OK, retry.text
        assert [row["run_order"] for row in retry.json()["queue"]] == [1, 2]


# --------------------------------------------------------------------------
# 5. ADVISORY: the rank never gates work
# --------------------------------------------------------------------------


class TestRunOrderNeverGates:
    """The load-bearing product guarantee.

    ``run_order`` tells an operator what management WANTS run next; it must
    never decide what an operator MAY run. Gating stays with the predecessor
    rules and ``operation_action_gates``. If any of these start failing, the
    advisory rank has become an enforcement point.
    """

    def test_last_ranked_operation_can_still_be_started(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ids = [op.id for op in ops]
        assert _set_run_order(client, wc.id, ids, user_headers(manager)).status_code == status.HTTP_200_OK

        last = ids[2]
        resp = client.put(f"/api/v1/shop-floor/operations/{last}/start", headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        started = db_session.get(WorkOrderOperation, last)
        assert started.status == OperationStatus.IN_PROGRESS
        # Starting neither consumes nor rewrites the rank: the column is unchanged.
        assert [db_session.get(WorkOrderOperation, op_id).run_order for op_id in ids] == [1, 2, 3]
        assert _queue_ids(client, wc.id, user_headers(manager)) == ids

    def test_unranked_operation_can_still_be_clocked_into_ahead_of_rank_one(
        self, client: TestClient, db_session: Session
    ):
        """The strongest form: an op the manager did not rank AT ALL, at a work
        center that has a rank 1 sitting untouched, still clocks in."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, first = make_wo_with_operation(db_session, work_center=wc)
        unranked_wo, unranked = make_wo_with_operation(db_session, work_center=wc)
        assert _set_run_order(client, wc.id, [first.id], user_headers(manager)).status_code == status.HTTP_200_OK

        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=user_headers(operator),
            json={
                "work_order_id": unranked_wo.id,
                "operation_id": unranked.id,
                "work_center_id": wc.id,
                "entry_type": "run",
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, unranked.id).run_order is None
        assert db_session.get(WorkOrderOperation, first.id).run_order == 1

    def test_crew_station_operator_can_start_the_bottom_of_the_column(self, client: TestClient, db_session: Session):
        """Same guarantee through the crew-station path: a badge-minted operator
        token starting the LAST-ranked job at the station's own work center."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        badge_user = make_user(db_session, role=UserRole.OPERATOR)
        wos = [make_wo_with_operation(db_session, work_center=wc) for _ in range(2)]
        ids = [op.id for _, op in wos]
        assert _set_run_order(client, wc.id, ids, user_headers(manager)).status_code == status.HTTP_200_OK

        minted = mint_badge_token(client, kiosk_token_for(station), badge_user.employee_id)
        assert minted.status_code == status.HTTP_200_OK, minted.text
        token = minted.json()["access_token"]

        last_wo, last_op = wos[1]
        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=bearer(token),
            json={
                "work_order_id": last_wo.id,
                "operation_id": last_op.id,
                "work_center_id": wc.id,
                "entry_type": "run",
                "source": "kiosk",
            },
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, last_op.id).run_order == 2


class TestDisplayedRankIsGapFree:
    """Stored ranks go sparse as jobs leave a column (complete, or move to
    another machine). What the shop SEES must stay 1..N with no gaps, or a
    three-job queue showing "RUN 4" reads as a missing job."""

    def test_rank_gap_from_a_move_is_not_shown_to_the_shop(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        headers = user_headers(admin)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        brake = make_work_center(db_session, name="Press Brake 1")
        ops = [make_wo_with_operation(db_session, work_center=laser)[1] for _ in range(4)]
        op_ids = [op.id for op in ops]

        resp = client.put(run_order_url(laser.id), json={"operation_ids": op_ids}, headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [r["run_order"] for r in resp.json()["queue"]] == [1, 2, 3, 4]

        # Move the rank-2 job away: storage now holds 1, 3, 4 at the laser.
        moved = db_session.get(WorkOrderOperation, op_ids[1])
        resp = client.put(
            f"/api/v1/work-orders/operations/{moved.id}",
            json={"version": moved.version, "work_center_id": brake.id},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        stored = [db_session.get(WorkOrderOperation, oid).run_order for oid in (op_ids[0], op_ids[2], op_ids[3])]
        assert stored == [1, 3, 4], "precondition: storage is expected to go sparse"

        # ...but both operator-facing surfaces renumber densely.
        assert [row["run_order"] for row in _queue_rows(client, laser.id, headers)] == [1, 2, 3]

        board = client.get(BOARD_URL, headers=headers)
        assert board.status_code == status.HTTP_200_OK, board.text
        column = next(c for c in board.json()["work_centers"] if c["id"] == laser.id)
        assert [row["run_order"] for row in column["queue"]] == [1, 2, 3]

        # The moved job is unranked at its destination, not carrying a stale rank.
        dest = next(c for c in board.json()["work_centers"] if c["id"] == brake.id)
        assert [row["run_order"] for row in dest["queue"]] == [None]

    def test_unranked_tail_stays_null_and_ranked_lead_is_dense(self, client, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        headers = user_headers(admin)
        laser = make_work_center(db_session, name="Ermaksan Fiber Laser")
        ops = [make_wo_with_operation(db_session, work_center=laser)[1] for _ in range(3)]
        op_ids = [op.id for op in ops]

        # Rank only the last two; the first is deliberately omitted.
        resp = client.put(run_order_url(laser.id), json={"operation_ids": op_ids[1:]}, headers=headers)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        rows = _queue_rows(client, laser.id, headers)
        assert [row["run_order"] for row in rows] == [1, 2, None]
        assert rows[-1]["operation_id"] == op_ids[0]


# --------------------------------------------------------------------------
# 6. Rank-surface parity: GET /shop-floor/operations (the desktop pages)
# --------------------------------------------------------------------------


class TestDesktopOperationsRankSurfaceParity:
    """The two desktop pages (/shop-floor and /shop-floor/operations) render the
    ``GET /shop-floor/operations`` payload VERBATIM — owner decision, no client
    re-sort — so this endpoint IS the desktop's run order. These tests pin the
    contract end to end through the REAL rewrite endpoint, plus the pre-existing
    filter semantics the ordering change must not disturb.
    (``test_dispatch_run_order.py::TestDesktopOperationsRunOrder`` pins the
    direct-DB variants: kiosk-order parity, sparse ranks, code grouping, and the
    stale-rank-on-a-PENDING-op case.)"""

    def test_ranked_cold_job_leads_the_unranked_hot_job(self, client: TestClient, db_session: Session):
        """The exact divergence class the desktop fix closes: the old client
        dispatch-score sort would promote an unranked OVERDUE priority-1 job to
        the top; the manager's rank on a far-due priority-9 job must win —
        pairwise identical to what the kiosk serves."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, hot = make_wo_with_operation(db_session, work_center=wc)
        _, cold = make_wo_with_operation(db_session, work_center=wc)
        hot.work_order.priority = 1
        hot.work_order.due_date = date.today() - timedelta(days=7)  # overdue
        cold.work_order.priority = 9
        cold.work_order.due_date = date.today() + timedelta(days=60)
        db_session.commit()

        # The manager ranks ONLY the cold job — through the real board endpoint.
        assert _set_run_order(client, wc.id, [cold.id], user_headers(manager)).status_code == status.HTTP_200_OK

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id)
        assert [(row["id"], row["run_order"]) for row in rows] == [(cold.id, 1), (hot.id, None)]

        kiosk = _queue_rows(client, wc.id, user_headers(manager))
        assert [(row["operation_id"], row["run_order"]) for row in kiosk] == [
            (row["id"], row["run_order"]) for row in rows
        ]

    def test_multi_work_center_rows_group_by_code_with_per_column_positions(
        self, client: TestClient, db_session: Session
    ):
        """Unfiltered, rows group by WorkCenter.code (the board's column order),
        keep the canonical order within each group, and each group's positions
        are its own gap-free 1..N: a rank stored as 7, alone in its column,
        displays as 1 — and two columns may both show a RUN 1."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        first_col = make_work_center(db_session)
        second_col = make_work_center(db_session)
        first_col.code = "AAA-0001"
        second_col.code = "ZZZ-9999"
        # AAA column: sparse stored ranks (2, 5) plus an unranked tail row.
        _, aaa_second = make_wo_with_operation(db_session, work_center=first_col)
        _, aaa_first = make_wo_with_operation(db_session, work_center=first_col)
        _, aaa_unranked = make_wo_with_operation(db_session, work_center=first_col)
        aaa_first.run_order = 2
        aaa_second.run_order = 5
        # ZZZ column: a lone job whose stored rank drifted to 7.
        _, zzz_lone = make_wo_with_operation(db_session, work_center=second_col)
        zzz_lone.run_order = 7
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager))
        assert [(row["id"], row["run_order"]) for row in rows] == [
            (aaa_first.id, 1),
            (aaa_second.id, 2),
            (aaa_unranked.id, None),
            (zzz_lone.id, 1),  # per-column position, not the stored 7
        ]

    def test_positions_come_from_the_full_queue_not_the_page(self, client: TestClient, db_session: Session):
        """A paginated read must still label the third-ranked job RUN 3: the
        position is computed over the work center's FULL live queue, never the
        LIMIT/OFFSET page, so the number always matches the kiosk chip."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        ops = [make_wo_with_operation(db_session, work_center=wc)[1] for _ in range(3)]
        ids = [op.id for op in ops]
        assert _set_run_order(client, wc.id, ids, user_headers(manager)).status_code == status.HTTP_200_OK

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id, page=3, page_size=1)
        assert [(row["id"], row["run_order"]) for row in rows] == [(ids[2], 3)]

    def test_default_view_still_excludes_complete_ops_and_terminal_or_deleted_work_orders(
        self, client: TestClient, db_session: Session
    ):
        """Filter-semantics pin: the ordering change must return the SAME rows as
        before — the default view keeps excluding COMPLETE operations and
        cancelled/soft-deleted work orders."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, live = make_wo_with_operation(db_session, work_center=wc)
        _, done = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.COMPLETE)
        cancelled_wo, cancelled_op = make_wo_with_operation(db_session, work_center=wc)
        cancelled_wo.status = WorkOrderStatus.CANCELLED
        deleted_wo, deleted_op = make_wo_with_operation(db_session, work_center=wc)
        deleted_wo.is_deleted = True
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id)
        assert [row["id"] for row in rows] == [live.id]
        for excluded in (done.id, cancelled_op.id, deleted_op.id):
            assert excluded not in {row["id"] for row in rows}

    def test_status_filter_still_returns_only_that_status_and_off_queue_rows_carry_no_position(
        self, client: TestClient, db_session: Session
    ):
        """Filter-semantics pin #2: ``status=on_hold`` still subsets to held rows
        only — and a held row is off the live queue, so its ``run_order`` is null
        even when a stale stored rank survives underneath."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, ready = make_wo_with_operation(db_session, work_center=wc)
        _, held = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
        held.run_order = 1  # stale rank left behind by the hold
        db_session.commit()

        rows = _operations_rows(client, user_headers(manager), work_center_id=wc.id, status="on_hold")
        assert [(row["id"], row["run_order"]) for row in rows] == [(held.id, None)]
        assert ready.id not in {row["id"] for row in rows}
