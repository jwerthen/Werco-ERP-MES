"""Routing changes on a released work order (owner decision 2026-11-25).

"I need to be able to update routing for work orders once a work order releases. Sometimes
things change on the fly." The owner's decisions, each pinned here:

* a step taken off a released job is SKIPPED, never deleted -- it stays on the traveler with
  who / when / why, and the steps after it can proceed;
* every routing change on a non-DRAFT work order needs a typed reason, stored as REQUIRED audit
  evidence (503 + nothing saved when it cannot be written);
* ADD a step, REMOVE (skip) a step, REORDER steps and EDIT a step all work on a released job.

And the invariants a skipped step must not break: it clears both predecessor-gate twins, it is
FINISHED for every all-complete check but produces nothing (no good, no scrap, no share of the
finished quantity, no ceiling on the next stage), force-complete leaves it alone, and no change
may put an unfinished step in front of work already recorded.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.credentials import API_TOKEN_ROUTING_CHANGE
from app.models.audit_log import AuditLog
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operation_action_gates import MSG_OPERATION_SKIPPED, hold_blockers
from app.services.routing_change_service import ROUTING_AUDIT_UNAVAILABLE
from app.services.work_order_state_service import (
    FINISHED_OPERATION_STATUSES,
    active_work_order_operations,
    finished_quantity_at_all_complete,
    has_incomplete_predecessors,
    is_operation_finished,
    operation_accounting,
    operation_blocked_by_incomplete_predecessors,
    stage_accounting,
)
from tests.api.kiosk_test_helpers import COMPANY_A, make_user, make_work_center, user_headers
from tests.api.short_count_helpers import add_entry, fresh, make_routing, op_audit_rows, set_op

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_B = 2
API_TOKENS_URL = "/api/v1/api-tokens/"
PENDING, READY, IN_PROGRESS, COMPLETE, SKIPPED = (
    OperationStatus.PENDING,
    OperationStatus.READY,
    OperationStatus.IN_PROGRESS,
    OperationStatus.COMPLETE,
    OperationStatus.SKIPPED,
)


def skip_url(op) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/skip"


def unskip_url(op) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/unskip"


def add_url(wo) -> str:
    return f"/api/v1/work-orders/{wo.id}/operations"


def reseq_url(wo) -> str:
    return f"/api/v1/work-orders/{wo.id}/operations/resequence"


def _version(db: Session, op) -> int:
    return fresh(db, WorkOrderOperation, op.id).version


def _skip(client, db, op, headers, reason="customer dropped the paint step"):
    return client.post(skip_url(op), headers=headers, json={"version": _version(db, op), "reason": reason})


def _unskip(client, db, op, headers, reason="customer wants paint after all"):
    return client.post(unskip_url(op), headers=headers, json={"version": _version(db, op), "reason": reason})


def _statuses(db: Session, ops) -> list:
    db.expire_all()
    return [db.get(WorkOrderOperation, op.id).status for op in ops]


def _token(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "routing probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def supervisor(db_session: Session):
    from app.models.user import UserRole

    return make_user(db_session, role=UserRole.SUPERVISOR)


@pytest.fixture
def three_step(db_session: Session):
    """A released, SEQUENCED routing across three machines: 10 READY, 20 / 30 PENDING."""
    wcs = [make_work_center(db_session) for _ in range(3)]
    return make_routing(db_session, work_centers=wcs, sequential=True)


# --------------------------------------------------------------------------- skip


class TestSkip:
    def test_skipping_the_first_step_promotes_the_next_and_keeps_the_record(
        self, client, db_session, supervisor, three_step
    ):
        wo, (op10, op20, op30) = three_step
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation"]["status"] == "skipped"
        assert body["operation"]["skip_reason"] == "customer dropped the paint step"
        assert body["operation"]["skipped_by"] == supervisor.id
        assert body["operation"]["skipped_at"].endswith("Z")
        assert body["promoted_operation_ids"] == [op20.id]
        assert _statuses(db_session, [op10, op20, op30]) == [SKIPPED, READY, PENDING]
        # Still on the traveler.
        detail = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).json()
        assert {op["id"]: op["status"] for op in detail["operations"]}[op10.id] == "skipped"

    def test_the_skip_is_required_audit_evidence_carrying_the_reason(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor), reason="  not needed  ").status_code == 200
        rows = [r for r in op_audit_rows(db_session, op10.id) if r.action == "STATUS_CHANGE"]
        assert len(rows) == 1
        assert rows[0].new_values["status"] == "skipped"
        assert rows[0].extra_data["reason"] == "not needed"
        assert rows[0].extra_data["transition"] == "operation_skipped"

    def test_a_skip_whose_audit_cannot_be_written_saves_nothing(
        self, client, db_session, supervisor, three_step, monkeypatch
    ):
        _, (op10, op20, _) = three_step

        def fail(*_args, **_kwargs):
            raise AuditWriteError("no audit")

        monkeypatch.setattr(AuditService, "log_required", fail)
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 503
        assert resp.json()["detail"] == ROUTING_AUDIT_UNAVAILABLE
        assert _statuses(db_session, [op10, op20]) == [READY, PENDING]
        assert fresh(db_session, WorkOrderOperation, op10.id).skip_reason is None

    @pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
    def test_a_reason_is_required(self, client, db_session, supervisor, three_step, reason):
        _, (op10, _, _) = three_step
        resp = client.post(
            skip_url(op10), headers=user_headers(supervisor), json={"version": op10.version, "reason": reason}
        )
        assert resp.status_code == 422
        assert _statuses(db_session, [op10]) == [READY]

    def test_a_stale_version_is_refused(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        resp = client.post(
            skip_url(op10), headers=user_headers(supervisor), json={"version": op10.version + 5, "reason": "x"}
        )
        assert resp.status_code == 409

    def test_a_step_with_recorded_work_is_refused(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        add_entry(db_session, make_user(db_session), op10, produced=0)
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 409
        assert "recorded work" in resp.json()["detail"]
        assert _statuses(db_session, [op10]) == [READY]

    @pytest.mark.parametrize("status", [IN_PROGRESS, COMPLETE, OperationStatus.ON_HOLD])
    def test_only_a_not_started_step_can_be_skipped(self, client, db_session, supervisor, three_step, status):
        _, (op10, _, _) = three_step
        set_op(db_session, op10, status=status)
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 409

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
    def test_a_terminal_work_order_is_refused(self, client, db_session, supervisor, three_step, wo_status):
        wo, (op10, _, _) = three_step
        set_op(db_session, wo, status=wo_status)
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 409

    def test_a_deleted_work_order_is_404(self, client, db_session, supervisor, three_step):
        wo, (op10, _, _) = three_step
        set_op(db_session, wo, is_deleted=True)
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 404

    def test_a_laser_work_order_is_refused(self, client, db_session, supervisor, three_step):
        wo, (op10, _, _) = three_step
        set_op(db_session, wo, work_order_type=WorkOrderType.LASER_CUTTING.value)
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 409

    def test_the_last_step_that_is_not_skipped_cannot_be_skipped(self, client, db_session, supervisor):
        _, (op10, op20) = make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(2)])
        assert _skip(client, db_session, op20, user_headers(supervisor)).status_code == 200
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 409
        assert "at least one step" in resp.json()["detail"]

    def test_a_draft_work_order_can_be_skipped_and_promotes_nothing(self, client, db_session, supervisor):
        _, (op10, op20) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(2)],
            statuses=[PENDING, PENDING],
            wo_status=WorkOrderStatus.DRAFT,
        )
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert resp.json()["promoted_operation_ids"] == []
        assert _statuses(db_session, [op10, op20]) == [SKIPPED, PENDING]

    def test_operators_and_api_tokens_are_refused(self, client, db_session, admin_headers, supervisor, three_step):
        from app.models.user import UserRole

        _, (op10, _, _) = three_step
        operator = make_user(db_session, role=UserRole.OPERATOR)
        assert _skip(client, db_session, op10, user_headers(operator)).status_code == 403
        resp = _skip(client, db_session, op10, _token(client, admin_headers, supervisor))
        assert resp.status_code == 403
        assert resp.json()["detail"] == API_TOKEN_ROUTING_CHANGE
        assert _statuses(db_session, [op10]) == [READY]

    def test_another_companys_operation_is_404(self, client, db_session, three_step):
        from app.models.user import UserRole

        _, (op10, _, _) = three_step
        outsider = make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        assert _skip(client, db_session, op10, user_headers(outsider)).status_code == 404
        assert _statuses(db_session, [op10]) == [READY]


# --------------------------------------------------------------------------- a skipped step off the floor


class TestASkippedStepIsOffTheRoute:
    @pytest.fixture
    def skipped(self, client, db_session, supervisor, three_step):
        wo, ops = three_step
        assert _skip(client, db_session, ops[1], user_headers(supervisor)).status_code == 200
        return wo, ops

    def test_both_start_verbs_refuse_it(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        office = client.post(f"/api/v1/work-orders/operations/{op20.id}/start", headers=user_headers(supervisor))
        assert office.status_code == 409 and office.json()["detail"] == MSG_OPERATION_SKIPPED
        floor = client.put(f"/api/v1/shop-floor/operations/{op20.id}/start", headers=user_headers(supervisor))
        assert floor.status_code == 409 and floor.json()["detail"] == MSG_OPERATION_SKIPPED

    def test_clock_in_refuses_it(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=user_headers(supervisor),
            json={"work_order_id": op20.work_order_id, "operation_id": op20.id, "work_center_id": op20.work_center_id},
        )
        assert resp.status_code in (400, 409), resp.text
        assert _statuses(db_session, [op20]) == [SKIPPED]

    def test_the_generic_put_cannot_edit_or_revive_it(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.put(
            f"/api/v1/work-orders/operations/{op20.id}",
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op20), "status": "pending"},
        )
        assert resp.status_code == 409 and resp.json()["detail"] == MSG_OPERATION_SKIPPED

    def test_hold_is_refused(self, skipped):
        _, (_, op20, _) = skipped
        assert hold_blockers(SimpleNamespace(status=SKIPPED)) == ["Cannot put a skipped operation on hold"]

    def test_it_is_off_the_shop_floor_operations_list(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        body = client.get(
            "/api/v1/shop-floor/operations",
            headers=user_headers(supervisor),
            params={"work_center_id": op20.work_center_id},
        ).json()
        assert op20.id not in {row["id"] for row in body["operations"]}

    def test_the_generic_put_cannot_skip(self, client, db_session, supervisor, three_step):
        _, (_, _, op30) = three_step
        resp = client.put(
            f"/api/v1/work-orders/operations/{op30.id}",
            headers=user_headers(supervisor),
            json={"version": op30.version, "status": "skipped"},
        )
        assert resp.status_code == 409
        assert _statuses(db_session, [op30]) == [PENDING]


# --------------------------------------------------------------------------- unskip


class TestUnskip:
    def test_restore_returns_to_pending_and_demotes_what_it_now_blocks(
        self, client, db_session, supervisor, three_step
    ):
        _, (op10, op20, op30) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers).status_code == 200
        assert _statuses(db_session, [op10, op20, op30]) == [SKIPPED, READY, PENDING]
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["demoted_operation_ids"] == [op20.id]
        assert body["promoted_operation_ids"] == [op10.id]
        assert _statuses(db_session, [op10, op20, op30]) == [READY, PENDING, PENDING]
        restored = fresh(db_session, WorkOrderOperation, op10.id)
        assert restored.skipped_at is None and restored.skipped_by is None and restored.skip_reason is None
        # One required row for the restore, one for the demotion -- both carry the reason.
        unskip_row = [
            r for r in op_audit_rows(db_session, op10.id) if r.extra_data.get("transition") == "operation_unskipped"
        ]
        assert len(unskip_row) == 1 and unskip_row[0].extra_data["reason"] == "customer wants paint after all"
        demote_rows = [r for r in op_audit_rows(db_session, op20.id) if r.new_values == {"status": "pending"}]
        assert len(demote_rows) == 1

    def test_restore_in_front_of_recorded_work_is_refused(self, client, db_session, supervisor, three_step):
        _, (op10, op20, _) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers).status_code == 200
        add_entry(db_session, make_user(db_session), op20, open_entry=True)
        set_op(db_session, op20, status=IN_PROGRESS)
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 409
        assert "recorded work" in resp.json()["detail"]
        assert _statuses(db_session, [op10]) == [SKIPPED]

    def test_only_a_skipped_step_can_be_restored(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        assert _unskip(client, db_session, op10, user_headers(supervisor)).status_code == 409

    def test_api_token_refused(self, client, db_session, admin_headers, supervisor, three_step):
        _, (op10, _, _) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        resp = _unskip(client, db_session, op10, _token(client, admin_headers, supervisor))
        assert resp.status_code == 403 and resp.json()["detail"] == API_TOKEN_ROUTING_CHANGE


# --------------------------------------------------------------------------- add


def _add_body(wc, sequence, **extra):
    return {"work_center_id": wc.id, "sequence": sequence, "name": "Deburr", **extra}


class TestAddOnAReleasedWorkOrder:
    def test_add_with_a_reason_is_audited_and_the_new_step_waits_its_turn(
        self, client, db_session, supervisor, three_step
    ):
        wo, (op10, _, _) = three_step
        wc = make_work_center(db_session)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(wc, 40, change_reason="customer added QC")
        )
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["id"]
        assert resp.json()["status"] == "pending"
        rows = [r for r in op_audit_rows(db_session, new_id) if r.action == "CREATE"]
        assert len(rows) == 1 and rows[0].extra_data["reason"] == "customer added QC"

    def test_a_reason_is_required_once_released(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 40)
        )
        assert resp.status_code == 422
        assert len(fresh(db_session, WorkOrder, wo.id).operations) == 3

    def test_a_draft_needs_no_reason(self, client, db_session, supervisor):
        wo, _ = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 20)
        )
        assert resp.status_code == 200, resp.text

    def test_a_used_sequence_is_refused(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 20, change_reason="x"),
        )
        assert resp.status_code == 409
        assert "already used" in resp.json()["detail"]

    def test_a_skipped_steps_sequence_may_be_reused(self, client, db_session, supervisor, three_step):
        wo, (_, _, op30) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op30, headers).status_code == 200
        resp = client.post(
            add_url(wo), headers=headers, json=_add_body(make_work_center(db_session), 30, change_reason="x")
        )
        assert resp.status_code == 200, resp.text

    def test_inserting_before_a_started_step_is_refused(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op20, op30) = make_routing(db_session, work_centers=wcs, statuses=[IN_PROGRESS, PENDING])
        # make_routing numbers from 10: shift them to 20 / 30 so 10 is free.
        set_op(db_session, op20, sequence=20)
        set_op(db_session, op30, sequence=30)
        add_entry(db_session, make_user(db_session), op20, open_entry=True)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 10, change_reason="x"),
        )
        assert resp.status_code == 409
        assert "recorded work" in resp.json()["detail"]

    def test_the_new_step_demotes_a_ready_step_it_now_blocks(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op30) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, READY])
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op30, sequence=30)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 20, change_reason="x"),
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        assert fresh(db_session, WorkOrderOperation, op30.id).status == PENDING
        assert fresh(db_session, WorkOrderOperation, resp.json()["id"]).status == READY

    def test_a_token_may_add_to_a_draft_but_not_a_released_job(self, client, db_session, admin_headers, supervisor):
        token = _token(client, admin_headers, supervisor)
        draft, _ = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        assert (
            client.post(add_url(draft), headers=token, json=_add_body(make_work_center(db_session), 20)).status_code
            == 200
        )
        released, _ = make_routing(db_session, work_centers=[make_work_center(db_session)])
        resp = client.post(
            add_url(released), headers=token, json=_add_body(make_work_center(db_session), 20, change_reason="x")
        )
        assert resp.status_code == 403 and resp.json()["detail"] == API_TOKEN_ROUTING_CHANGE

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CANCELLED])
    def test_terminal_and_deleted_work_orders(self, client, db_session, supervisor, three_step, wo_status):
        wo, _ = three_step
        set_op(db_session, wo, status=wo_status)
        body = _add_body(make_work_center(db_session), 40, change_reason="x")
        assert client.post(add_url(wo), headers=user_headers(supervisor), json=body).status_code == 409
        set_op(db_session, wo, is_deleted=True)
        assert client.post(add_url(wo), headers=user_headers(supervisor), json=body).status_code == 404


# --------------------------------------------------------------------------- resequence


class TestResequence:
    def _body(self, db, moves, reason="weld before paint"):
        return {
            "reason": reason,
            "operations": [{"id": op.id, "version": _version(db, op), "sequence": seq} for op, seq in moves],
        }

    def test_swap_two_pending_steps_keeps_operation_numbers(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(op20, 30), (op30, 20)])
        )
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["changed_operation_ids"]) == {op20.id, op30.id}
        db_session.expire_all()
        moved20 = db_session.get(WorkOrderOperation, op20.id)
        moved30 = db_session.get(WorkOrderOperation, op30.id)
        assert (moved20.sequence, moved20.operation_number) == (30, "20")
        assert (moved30.sequence, moved30.operation_number) == (20, "30")
        rows = [
            r for r in op_audit_rows(db_session, op20.id) if r.extra_data.get("transition") == "operations_resequenced"
        ]
        assert len(rows) == 1 and rows[0].old_values == {"sequence": 20} and rows[0].new_values == {"sequence": 30}
        assert rows[0].extra_data["reason"] == "weld before paint"

    def test_moving_a_step_in_front_of_the_ready_one_demotes_it_and_promotes_the_mover(
        self, client, db_session, supervisor, three_step
    ):
        wo, (op10, op20, op30) = three_step
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(op10, 40), (op30, 10)])
        )
        assert resp.status_code == 200, resp.text
        assert op10.id in resp.json()["demoted_operation_ids"]
        assert op30.id in resp.json()["promoted_operation_ids"]
        assert _statuses(db_session, [op10, op20, op30]) == [PENDING, PENDING, READY]

    def test_a_duplicate_sequence_is_refused(self, client, db_session, supervisor, three_step):
        wo, (_, op20, _) = three_step
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(op20, 30)]))
        assert resp.status_code == 409

    def test_a_started_step_cannot_be_moved_and_nothing_may_land_before_one(
        self, client, db_session, supervisor, three_step
    ):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, op10, status=IN_PROGRESS)
        add_entry(db_session, make_user(db_session), op10, open_entry=True)
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(op10, 40)]))
        assert resp.status_code == 409
        set_op(db_session, op10, sequence=20)
        set_op(db_session, op20, sequence=50)
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(op30, 10)]))
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]

    def test_a_stale_version_is_refused(self, client, db_session, supervisor, three_step):
        wo, (_, op20, op30) = three_step
        body = {"reason": "x", "operations": [{"id": op20.id, "version": op20.version + 3, "sequence": 40}]}
        assert client.post(reseq_url(wo), headers=user_headers(supervisor), json=body).status_code == 409
        assert _statuses(db_session, [op20, op30]) == [PENDING, PENDING]

    def test_an_operation_of_another_work_order_is_404(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        _, (foreign, _) = make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(2)])
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=self._body(db_session, [(foreign, 90)])
        )
        assert resp.status_code == 404

    def test_api_token_refused(self, client, db_session, admin_headers, supervisor, three_step):
        wo, (_, op20, op30) = three_step
        resp = client.post(
            reseq_url(wo),
            headers=_token(client, admin_headers, supervisor),
            json=self._body(db_session, [(op20, 30), (op30, 20)]),
        )
        assert resp.status_code == 403


# --------------------------------------------------------------------------- edit


class TestEditContentOnAReleasedJob:
    def _put(self, client, db, op, headers, **fields):
        return client.put(
            f"/api/v1/work-orders/operations/{op.id}",
            headers=headers,
            json={"version": _version(db, op), **fields},
        )

    def test_a_content_edit_needs_a_reason_and_is_audited(self, client, db_session, supervisor, three_step):
        _, (_, op20, _) = three_step
        headers = user_headers(supervisor)
        assert self._put(client, db_session, op20, headers, run_instructions="new torque spec").status_code == 422
        resp = self._put(client, db_session, op20, headers, run_instructions="new torque spec", change_reason="ECO 41")
        assert resp.status_code == 200, resp.text
        rows = [
            r
            for r in op_audit_rows(db_session, op20.id)
            if r.extra_data.get("transition") == "operation_content_changed"
        ]
        assert len(rows) == 1 and rows[0].extra_data["reason"] == "ECO 41"

    def test_a_work_center_move_alone_needs_no_reason(self, client, db_session, supervisor, three_step):
        _, (_, op20, _) = three_step
        resp = self._put(
            client, db_session, op20, user_headers(supervisor), work_center_id=make_work_center(db_session).id
        )
        assert resp.status_code == 200, resp.text

    def test_a_draft_needs_no_reason(self, client, db_session, supervisor):
        _, (op,) = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        assert self._put(client, db_session, op, user_headers(supervisor), name="Renamed").status_code == 200

    def test_a_completed_steps_content_is_locked(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        set_op(db_session, op10, status=COMPLETE, quantity_complete=10)
        resp = self._put(client, db_session, op10, user_headers(supervisor), name="Renamed", change_reason="x")
        assert resp.status_code == 409

    @pytest.mark.parametrize(
        "fields",
        [
            {"name": "Renamed"},
            {"name": "Renamed", "change_reason": "ECO 7"},
            {"requires_inspection": False, "change_reason": "ECO 7"},
            {"run_instructions": "skip the torque check", "change_reason": "ECO 7"},
            {"run_time_hours": 0.1, "change_reason": "ECO 7"},
        ],
    )
    def test_a_token_cannot_edit_a_released_steps_content(
        self, client, db_session, admin_headers, supervisor, three_step, fields
    ):
        """Owner decision 2026-11-25: a routing change on a released job is a person's judgment,
        so a standing token is refused 403 -- reason or not -- and the step is left untouched
        (in particular its inspection requirement cannot be dropped by an agent)."""
        _, (_, op20, _) = three_step
        set_op(db_session, op20, requires_inspection=True, run_instructions="torque to spec", run_time_hours=1.0)
        token = _token(client, admin_headers, supervisor)
        resp = self._put(client, db_session, op20, token, **fields)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_ROUTING_CHANGE
        row = fresh(db_session, WorkOrderOperation, op20.id)
        assert (row.name, row.requires_inspection, row.run_instructions, float(row.run_time_hours)) == (
            "Step 20",
            True,
            "torque to spec",
            1.0,
        )

    def test_a_token_may_still_move_a_step_or_edit_a_draft(self, client, db_session, admin_headers, supervisor):
        token = _token(client, admin_headers, supervisor)
        _, (released_op, _) = make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(2)])
        moved = self._put(client, db_session, released_op, token, work_center_id=make_work_center(db_session).id)
        assert moved.status_code == 200, moved.text
        _, (draft_op,) = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        assert self._put(client, db_session, draft_op, token, name="Renamed").status_code == 200


# --------------------------------------------------------------------------- accounting / completion


class TestASkippedStepProducesNothing:
    def test_both_predecessor_twins_treat_skipped_as_cleared(self, db_session, three_step):
        wo, (op10, op20, _) = three_step
        set_op(db_session, op10, status=SKIPPED)
        assert not has_incomplete_predecessors(
            db_session, wo.id, op20.sequence, op20.id, op20.work_center_id, allow_same_work_center=False
        )
        incomplete = [op for op in wo.operations if not is_operation_finished(op.status)]
        assert not operation_blocked_by_incomplete_predecessors(op20, incomplete, allow_same_work_center=False)
        # And the in-memory twin agrees even when handed the skipped row itself.
        assert not operation_blocked_by_incomplete_predecessors(op20, list(wo.operations), allow_same_work_center=False)
        assert set(FINISHED_OPERATION_STATUSES) == {COMPLETE, SKIPPED}

    def test_a_skipped_step_is_not_an_obligation(self, db_session, three_step):
        wo, (op10, _, _) = three_step
        set_op(db_session, op10, status=SKIPPED)
        db_session.refresh(wo)
        assert op10.id not in {op.id for op in active_work_order_operations(wo)}
        assert op10.id in {op.id for op in active_work_order_operations(wo, include_skipped=True)}

    def test_a_skipped_step_does_not_reduce_the_next_ceiling(self, db_session):
        wcs = [make_work_center(db_session) for _ in range(3)]
        wo, (op10, op20, op30) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, SKIPPED, PENDING])
        # op10 closed short by 2 (covered by scrap); op20 skipped; op30 receives 8.
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        db_session.refresh(wo)
        acct = operation_accounting(wo, op30)
        assert acct.ceiling == pytest.approx(8)
        assert op20.id not in stage_accounting(wo, list(wo.operations))

    def test_a_skipped_parallel_twin_does_not_hold_back_a_completed_stage(self, db_session):
        """DECISIVE for the skipped-step transparency of the main-chain walk: a skipped step
        treated as an unfinished stage member, or as COMPLETE at 0 good, changes op30's answer.

        Sequenced, ordered 10. Component op 15 (part P, 10 pieces) closed 2 short, so 2 assemblies
        cannot get a P. Stage 20 is two parallel steps: 20a COMPLETE at 10 good (P was
        replenished), 20b SKIPPED. Stage 20 is therefore fully complete, it passed on the 10 it
        made, and the component loss before it is absorbed: op30 receives all 10. Were 20b in the
        walk as a not-complete member, stage 20 would not absorb and op30 would receive 8; were it
        COMPLETE at 0 good, ``min(good)`` would pass on 8 -- either way 8, not 10.
        """
        from tests.api.short_count_helpers import make_part

        wcs = [make_work_center(db_session) for _ in range(4)]
        component = make_part(db_session)
        wo, (op15, op20a, op20b, op30) = make_routing(
            db_session,
            work_centers=wcs,
            statuses=[COMPLETE, COMPLETE, SKIPPED, PENDING],
            component_parts=[component, None, None, None],
            component_quantities=[10, None, None, None],
        )
        set_op(db_session, op15, sequence=15, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        set_op(db_session, op20a, sequence=20, quantity_complete=10)
        set_op(db_session, op20b, sequence=20)
        set_op(db_session, op30, sequence=30)
        db_session.refresh(wo)
        acct = operation_accounting(wo, op30)
        assert acct.ceiling == pytest.approx(10)
        assert acct.received == pytest.approx(10)

    def test_finished_quantity_ignores_a_skipped_final_stage(self, db_session):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op20) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, SKIPPED])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        db_session.refresh(wo)
        assert finished_quantity_at_all_complete(wo, list(wo.operations), 0.0) == pytest.approx(8)

    def test_skipping_the_last_open_step_completes_the_work_order(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op20) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, READY])
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.IN_PROGRESS)
        resp = _skip(client, db_session, op20, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert resp.json()["work_order_status"] == "complete"
        done = fresh(db_session, WorkOrder, wo.id)
        assert done.status == WorkOrderStatus.COMPLETE
        assert float(done.quantity_complete) == pytest.approx(10)
        assert fresh(db_session, WorkOrderOperation, op20.id).status == SKIPPED
        assert float(fresh(db_session, WorkOrderOperation, op20.id).quantity_complete or 0) == 0

    def test_force_complete_leaves_a_skipped_step_alone(self, client, db_session, supervisor):
        from app.models.user import UserRole

        manager = make_user(db_session, role=UserRole.MANAGER)
        wcs = [make_work_center(db_session) for _ in range(3)]
        wo, (op10, op20, op30) = make_routing(db_session, work_centers=wcs, statuses=[READY, PENDING, PENDING])
        assert _skip(client, db_session, op20, user_headers(supervisor)).status_code == 200
        resp = client.post(
            f"/api/v1/work-orders/{wo.id}/complete",
            headers=user_headers(manager),
            params={"quantity_complete": 10},
        )
        assert resp.status_code == 200, resp.text
        assert _statuses(db_session, [op10, op20, op30]) == [COMPLETE, SKIPPED, COMPLETE]


def test_audit_log_rows_are_tenant_tagged(client, db_session, supervisor, three_step):
    _, (op10, _, _) = three_step
    assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
    row = db_session.query(AuditLog).filter(AuditLog.resource_id == op10.id, AuditLog.action == "STATUS_CHANGE").one()
    assert row.company_id == COMPANY_A


# --------------------------------------------------------------------------- twin parity


@pytest.mark.parametrize("predecessor_status", list(OperationStatus))
@pytest.mark.parametrize("same_work_center", [True, False])
@pytest.mark.parametrize("allow_same_work_center", [True, False])
def test_the_predecessor_twins_agree_over_every_status(
    db_session, predecessor_status, same_work_center, allow_same_work_center
):
    """``has_incomplete_predecessors`` (SQL) and its in-memory twin must answer identically,
    SKIPPED included, over every predecessor status, both modes and both work-center shapes."""
    first = make_work_center(db_session)
    second = first if same_work_center else make_work_center(db_session)
    wo, (op10, op20) = make_routing(
        db_session, work_centers=[first, second], statuses=[predecessor_status, OperationStatus.PENDING]
    )
    sql = has_incomplete_predecessors(
        db_session,
        wo.id,
        op20.sequence,
        op20.id,
        op20.work_center_id,
        allow_same_work_center=allow_same_work_center,
    )
    incomplete = [op for op in (op10, op20) if not is_operation_finished(op.status)]
    memory = operation_blocked_by_incomplete_predecessors(
        op20, incomplete, allow_same_work_center=allow_same_work_center
    )
    assert sql == memory
    if predecessor_status in FINISHED_OPERATION_STATUSES:
        assert sql is False


# --------------------------------------------------------------------------- review fixes (2026-11-25)


class TestASkippedComponentStepIssuesNoBackflush:
    """A skipped step consumes no material. The routing leg of the backflush reads
    ``component_part_id`` off the operations, so a SKIPPED operation must state no demand (the
    step never ran). (A tie on a skipped step cannot consume through the per-run engine at all:
    skip refuses a step with any good, scrap or start, so ``qty_per_run x (good + scrap)`` is 0
    by construction -- the end-to-end test below still asserts it.)"""

    def _armed(self, db_session, *, replica: bool = False):
        from app.models.inventory import InventoryItem
        from tests.api.short_count_helpers import make_part

        bracket = make_part(db_session, part_type="purchased")
        db_session.add(
            InventoryItem(
                part_id=bracket.id,
                location="RAW-A",
                warehouse="MAIN",
                quantity_on_hand=100.0,
                quantity_allocated=0.0,
                quantity_available=100.0,
                lot_number=f"BRK-{bracket.id}",
                unit_cost=1.0,
                status="available",
                is_active=True,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()
        count = 3 if replica else 2
        wo, ops = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(count)],
            statuses=[COMPLETE] + [READY] * (count - 1),
            component_parts=[None] + [bracket] * (count - 1),
            component_quantities=[None] + [10] * (count - 1),
        )
        set_op(db_session, ops[0], quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.IN_PROGRESS)
        set_op(db_session, wo.part, backflush_components=True)
        return wo, ops, bracket

    def test_the_resolver_drops_a_component_only_a_skipped_step_names(self, db_session):
        from app.services.completion_inventory_service import _resolve_backflush_demand

        wo, (_, op20), bracket = self._armed(db_session)
        set_op(db_session, wo, quantity_complete=10)
        assert bracket.id in _resolve_backflush_demand(db_session, wo, COMPANY_A).demand
        set_op(db_session, op20, status=SKIPPED)
        resolution = _resolve_backflush_demand(db_session, wo, COMPANY_A)
        assert bracket.id not in resolution.demand
        assert not [d for d in resolution.diagnostics if d.code == "incomplete_operation_demand"]

    def test_a_skipped_replica_leaves_the_live_steps_demand_alone(self, db_session):
        from app.services.completion_inventory_service import _resolve_backflush_demand

        wo, (_, op20, op30), bracket = self._armed(db_session, replica=True)
        set_op(db_session, wo, quantity_complete=10)
        set_op(db_session, op20, status=SKIPPED)
        set_op(db_session, op30, status=COMPLETE, quantity_complete=10)
        assert _resolve_backflush_demand(db_session, wo, COMPANY_A).demand.get(bracket.id) == pytest.approx(10)

    @staticmethod
    def _issued(db_session, part):
        from app.models.inventory import InventoryTransaction

        db_session.expire_all()
        return (
            db_session.query(InventoryTransaction)
            .filter(InventoryTransaction.company_id == COMPANY_A, InventoryTransaction.part_id == part.id)
            .all()
        )

    def test_skipping_the_last_component_step_completes_the_job_without_issuing_it(
        self, client, db_session, supervisor
    ):
        from tests.api.short_count_helpers import tie_material

        wo, (_, op20), bracket = self._armed(db_session)
        sheet = tie_material(db_session, wo, op20)
        resp = _skip(client, db_session, op20, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert resp.json()["work_order_status"] == "complete"
        assert self._issued(db_session, bracket) == []
        assert self._issued(db_session, sheet) == []

    def test_control_the_same_job_completing_the_step_does_issue_it(self, client, db_session, supervisor):
        """Proves the setup above reaches the backflush: completing (not skipping) the step issues."""
        wo, (_, op20), bracket = self._armed(db_session)
        set_op(db_session, op20, status=COMPLETE, quantity_complete=10)
        assert client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).status_code == 200
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
        assert sum(abs(float(row.quantity)) for row in self._issued(db_session, bracket)) == pytest.approx(10)


class TestASkippedStepsTiesAreNotDemand:
    def test_readiness_and_reservations_drop_the_tie_while_skipped_and_restore_it(
        self, client, db_session, supervisor, three_step
    ):
        from datetime import date

        from app.services.material_readiness_service import material_readiness
        from app.services.owned_property_service import _open_ties
        from tests.api.short_count_helpers import tie_material

        wo, (_, op20, _) = three_step
        sheet = tie_material(db_session, wo, op20)
        headers = user_headers(supervisor)

        def readiness_parts():
            db_session.expire_all()
            job = material_readiness(db_session, COMPANY_A, [fresh(db_session, WorkOrder, wo.id)], date.today())
            return {line["part_id"] for line in job["jobs"][wo.id]["lines"]}

        def reserved():
            db_session.expire_all()
            return [
                allocation.work_order_operation_id for allocation, _, _ in _open_ties(db_session, COMPANY_A, sheet.id)
            ]

        assert sheet.id in readiness_parts() and reserved() == [op20.id]
        assert _skip(client, db_session, op20, headers).status_code == 200
        assert sheet.id not in readiness_parts()
        assert reserved() == []
        # The tie itself is untouched (no irreversible status change) ...
        from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation

        tie = (
            db_session.query(WorkOrderMaterialAllocation)
            .filter(WorkOrderMaterialAllocation.work_order_operation_id == op20.id)
            .one()
        )
        assert tie.status == AllocationStatus.OPEN and float(tie.qty_planned) == pytest.approx(10)
        # ... so restoring the step makes it demand again.
        assert _unskip(client, db_session, op20, headers).status_code == 200
        assert sheet.id in readiness_parts() and reserved() == [op20.id]


class TestASkippedLineItemIsNotOwed:
    """A pool / batch work order whose line items split the order: a skipped line's pieces were
    never made, so the header (and the FG receipt read from it) must not count them."""

    def _lines(self, db_session, statuses):
        return make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(2)],
            sequential=False,
            statuses=statuses,
            component_quantities=[6, 4],
        )

    def test_the_header_is_what_the_live_lines_made(self, db_session):
        wo, (line_a, line_b) = self._lines(db_session, [COMPLETE, SKIPPED])
        set_op(db_session, line_a, quantity_complete=6)
        db_session.refresh(wo)
        assert finished_quantity_at_all_complete(wo, active_work_order_operations(wo), 0.0) == pytest.approx(6)
        set_op(db_session, line_a, quantity_complete=5, quantity_scrapped=1, quantity_short_declared=1)
        db_session.refresh(wo)
        assert finished_quantity_at_all_complete(wo, active_work_order_operations(wo), 0.0) == pytest.approx(5)

    def test_force_complete_bounds_do_not_count_the_skipped_line(self, db_session):
        from app.services.work_order_state_service import work_order_completion_bounds

        wo, (line_a, _) = self._lines(db_session, [COMPLETE, SKIPPED])
        set_op(db_session, line_a, quantity_complete=6)
        db_session.refresh(wo)
        bounds = work_order_completion_bounds(wo, active_work_order_operations(wo), {line_a.id: 6.0}, {line_a.id: True})
        assert bounds.deliverable == pytest.approx(6)
        assert bounds.physical_max == pytest.approx(6)

    def test_skipping_the_open_line_completes_the_job_at_the_made_quantity(self, client, db_session, supervisor):
        wo, (line_a, line_b) = self._lines(db_session, [COMPLETE, READY])
        set_op(db_session, line_a, quantity_complete=6)
        set_op(db_session, wo, status=WorkOrderStatus.IN_PROGRESS)
        resp = _skip(client, db_session, line_b, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert resp.json()["work_order_status"] == "complete"
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_complete) == pytest.approx(6)


class TestRestoreNeverDuplicatesASequence:
    def test_restore_onto_a_replacements_sequence_is_refused(self, client, db_session, supervisor, three_step):
        wo, (_, op20, _) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op20, headers).status_code == 200
        added = client.post(
            add_url(wo), headers=headers, json=_add_body(make_work_center(db_session), 20, change_reason="replace")
        )
        assert added.status_code == 200, added.text
        resp = _unskip(client, db_session, op20, headers)
        assert resp.status_code == 409, resp.text
        assert "Sequence 20 is now used by" in resp.json()["detail"]
        assert _statuses(db_session, [op20]) == [SKIPPED]


class TestAChangeMayNotReinterpretADeclaredShort:
    """Pooled work order, one machine, a component chain for part P: op10 COMPLETE at 8 good and
    2 declared short, op20 PENDING credited the 2 (component chains are always credited). Moving
    op20 -- or adding a P step -- in front of op10 would credit it nothing for units that no
    longer exist; the same-work-center waiver on a pooled job means no other gate refuses it."""

    def _chain(self, db_session):
        from tests.api.short_count_helpers import make_part

        wc = make_work_center(db_session)
        component = make_part(db_session)
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc, wc],
            sequential=False,
            statuses=[COMPLETE, PENDING],
            component_parts=[component, component],
            component_quantities=[10, 10],
        )
        set_op(db_session, op10, sequence=20, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        set_op(db_session, op20, sequence=30)
        db_session.refresh(wo)
        assert operation_accounting(wo, op20).received == pytest.approx(8)
        return wo, (op10, op20), wc, component

    def test_moving_a_step_in_front_of_the_declaring_stage_is_refused(self, client, db_session, supervisor):
        wo, (op10, op20), _, _ = self._chain(db_session)
        body = {"reason": "x", "operations": [{"id": op20.id, "version": _version(db_session, op20), "sequence": 10}]}
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=body)
        assert resp.status_code == 409, resp.text
        assert "already closed short of 2" in resp.json()["detail"]
        assert fresh(db_session, WorkOrderOperation, op20.id).sequence == 30

    def test_moving_it_further_back_is_allowed(self, client, db_session, supervisor):
        wo, (_, op20), _, _ = self._chain(db_session)
        body = {"reason": "x", "operations": [{"id": op20.id, "version": _version(db_session, op20), "sequence": 40}]}
        assert client.post(reseq_url(wo), headers=user_headers(supervisor), json=body).status_code == 200

    def test_adding_a_chain_step_in_front_of_it_is_refused(self, client, db_session, supervisor):
        wo, _, wc, component = self._chain(db_session)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(wc, 10, component_part_id=component.id, component_quantity=10, change_reason="x"),
        )
        assert resp.status_code == 409, resp.text
        assert "already closed short" in resp.json()["detail"]
        assert len(fresh(db_session, WorkOrder, wo.id).operations) == 2


class TestAddingToADraftStillProtectsRecordedWork:
    def test_inserting_before_running_labor_on_a_draft_is_refused(self, client, db_session, supervisor):
        """A DRAFT can carry a worked step (PUT back to draft / resume with no release record
        demote only un-worked ones). A new step in front of it would make its running labor
        uncompletable, so the recorded-work guard applies on a DRAFT too (no reason needed)."""
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op20, op30) = make_routing(
            db_session, work_centers=wcs, statuses=[IN_PROGRESS, PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        set_op(db_session, op20, sequence=20)
        set_op(db_session, op30, sequence=30)
        add_entry(db_session, make_user(db_session), op20, open_entry=True)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 10)
        )
        assert resp.status_code == 409, resp.text
        assert "recorded work" in resp.json()["detail"]
        assert len(fresh(db_session, WorkOrder, wo.id).operations) == 2
        # After the worked step a draft add still needs no reason.
        ok = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 40)
        )
        assert ok.status_code == 200, ok.text


class TestEveryRoutingVerbLocksTheWorkOrderFirst:
    """SQLite cannot show two transactions racing on a row lock, so this pins the mechanism:
    each verb issues a ``SELECT ... FOR UPDATE`` of the work order row before it reads the
    route, and that statement really compiles to FOR UPDATE on Postgres."""

    def test_the_lock_query_compiles_to_for_update_on_postgres(self, db_session):
        from sqlalchemy.dialects import postgresql

        from app.services.routing_change_service import work_order_lock_query

        sql = str(work_order_lock_query(db_session, 1, COMPANY_A).statement.compile(dialect=postgresql.dialect()))
        assert "FROM work_orders" in sql and sql.rstrip().endswith("FOR UPDATE")
        assert "work_orders.company_id" in sql

    @pytest.fixture
    def statements(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session as OrmSession

        seen: list[tuple[str, bool]] = []

        def record(state):
            if not state.is_select:
                return
            descriptions = getattr(state.statement, "column_descriptions", None) or []
            # Whole-row reads only (``load_operation``'s ``work_order_id`` peek that finds which
            # work order to lock is a single-column read, not a read of the route).
            name = descriptions[0].get("name") if descriptions else None
            if name in ("WorkOrder", "WorkOrderOperation"):
                seen.append((name, state.statement._for_update_arg is not None))

        event.listen(OrmSession, "do_orm_execute", record)
        yield seen
        event.remove(OrmSession, "do_orm_execute", record)

    @staticmethod
    def _locked_before_route_read(seen) -> bool:
        lock = next((i for i, (name, locked) in enumerate(seen) if name == "WorkOrder" and locked), None)
        route = next((i for i, (name, locked) in enumerate(seen) if name == "WorkOrderOperation" and not locked), None)
        return lock is not None and (route is None or lock < route)

    def test_skip_and_unskip(self, client, db_session, supervisor, three_step, statements):
        _, (_, _, op30) = three_step
        headers = user_headers(supervisor)
        version = _version(db_session, op30)
        statements.clear()
        assert client.post(skip_url(op30), headers=headers, json={"version": version, "reason": "x"}).status_code == 200
        assert self._locked_before_route_read(statements), statements
        version = _version(db_session, op30)
        statements.clear()
        assert (
            client.post(unskip_url(op30), headers=headers, json={"version": version, "reason": "x"}).status_code == 200
        )
        assert self._locked_before_route_read(statements), statements

    def test_add_and_resequence(self, client, db_session, supervisor, three_step, statements):
        wo, (_, op20, op30) = three_step
        headers = user_headers(supervisor)
        statements.clear()
        body = _add_body(make_work_center(db_session), 40, change_reason="x")
        assert client.post(add_url(wo), headers=headers, json=body).status_code == 200
        assert self._locked_before_route_read(statements), statements
        moves = [
            {"id": op.id, "version": _version(db_session, op), "sequence": seq} for op, seq in ((op20, 30), (op30, 20))
        ]
        statements.clear()
        assert client.post(reseq_url(wo), headers=headers, json={"reason": "x", "operations": moves}).status_code == 200
        assert self._locked_before_route_read(statements), statements


class TestSkippedByName:
    """The work-order read names who skipped a step (``skipped_by_name``), from one batched,
    tenant-scoped user read -- non-admin viewers cannot resolve a bare user id."""

    def test_the_read_names_each_skipper_in_one_user_query(self, client, db_session, supervisor):
        from sqlalchemy import event
        from sqlalchemy.orm import Session as OrmSession

        from app.models.user import UserRole
        from app.services.wallboard_service import operator_display_name

        other = make_user(db_session, role=UserRole.MANAGER)
        wo, (op10, op20, op30) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(3)],
            statuses=[READY, PENDING, PENDING],
        )
        assert _skip(client, db_session, op20, user_headers(supervisor)).status_code == 200
        assert _skip(client, db_session, op30, user_headers(other)).status_code == 200

        user_reads: list[int] = []

        def record(state):
            # The name lookup's own shape (id + first + last name); the viewer's auth reads load
            # whole user rows and are not counted.
            sql = " ".join(str(state.statement).split())
            if state.is_select and sql.startswith(
                "SELECT users.id AS users_id, users.first_name AS users_first_name, users.last_name"
            ):
                user_reads.append(sql[:120])

        event.listen(OrmSession, "do_orm_execute", record)
        try:
            body = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).json()
            with_skips = len(user_reads)
            user_reads.clear()
            set_op(db_session, op20, status=PENDING, skipped_by=None, skipped_at=None, skip_reason=None)
            set_op(db_session, op30, status=PENDING, skipped_by=None, skipped_at=None, skip_reason=None)
            assert client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).status_code == 200
            without_skips = len(user_reads)
        finally:
            event.remove(OrmSession, "do_orm_execute", record)
        names = {op["id"]: op["skipped_by_name"] for op in body["operations"]}
        assert names == {
            op10.id: None,
            op20.id: operator_display_name(supervisor.first_name, supervisor.last_name),
            op30.id: operator_display_name(other.first_name, other.last_name),
        }
        assert names[op20.id]
        # Two skippers, ONE user read; none when nothing is skipped.
        assert (with_skips, without_skips) == (1, 0)

    def test_a_skipper_outside_the_active_company_is_not_resolved(self, client, db_session, supervisor):
        from app.models.user import UserRole

        outsider = make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
        wo, (_, op20) = make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(2)])
        set_op(db_session, op20, status=SKIPPED, skipped_by=outsider.id, skip_reason="legacy")
        body = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).json()
        row = next(op for op in body["operations"] if op["id"] == op20.id)
        assert row["skipped_by"] == outsider.id and row["skipped_by_name"] is None
