"""Final-review fixes on the short-count branch: the after-close scrap remedy by case.

The S14 bound refuses a scrap rise on a COMPLETE operation past what is still unaccounted, and
its text sends the supervisor to reduce-production (reopen) for pieces counted good and later
found bad. Two cases made that advice a dead end; each class pins one, against the real routes:

* ``TestFinishedWorkOrderScrapRemedy`` -- on a COMPLETE / CLOSED / CANCELLED work order
  reduce-production refuses every correction, so the 409 names the record that still exists (a
  nonconformance against the finished work) instead of the shut door. The refusal itself is
  unchanged and still writes nothing.
* ``TestOperationNobodyClockedInTo`` -- an operation the office closed with nobody ever clocked in
  (the labor-less desk close the owner kept) had no evidence for reduce-production to walk, so the
  remedy was a 400. The office verb now lowers that operation's own good count directly, bounded
  by it, with the same reopen, strand, token and audit rules -- so "found bad later" has a door,
  and tied material is consumed once, not twice. An operation with ANY labor keeps the evidence
  bound byte-for-byte.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_COUNT_JUDGMENT,
    completed_scrap_over_received_message,
    no_labor_reduction_exceeds_message,
    reduce_would_leave_short_message,
)
from app.services.production_reduction_service import MSG_TERMINAL_WORK_ORDER, perform_production_reduction
from app.services.work_order_state_service import operation_accounting
from tests.api.kiosk_test_helpers import make_user, make_work_center, user_headers
from tests.api.short_count_helpers import add_entry, fresh, make_routing, set_op, tie_material
from tests.api.test_short_count_cross_review_fixes import _reduce, _refused, _token, _world

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OFFICE_REDUCE_URL = "/api/v1/work-orders/operations/{op}/reduce-production"
OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
OPERATION_PUT_URL = "/api/v1/work-orders/operations/{op}"

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS
READY = OperationStatus.READY
PENDING = OperationStatus.PENDING


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.work_orders.enqueue_work_order_completion_signals", lambda **_: None)
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


@pytest.fixture
def supervisor(db_session: Session) -> User:
    return make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sue", last_name="Visor")


def _put_scrap(client, op, headers, scrap: float):
    return client.put(
        OPERATION_PUT_URL.format(op=op.id),
        json={"version": op.version, "quantity_scrapped": scrap, "scrap_reason": "found cracked at inspection"},
        headers=headers,
    )


def _consumed(db: Session, op_id: int) -> float:
    """Sheets the operation's tie has consumed so far (operation-scoped ledger rows, signed)."""
    db.expire_all()
    rows = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == "work_order_operation",
            InventoryTransaction.reference_id == op_id,
        )
        .all()
    )
    return -sum(float(row.quantity or 0) for row in rows)


# --------------------------------------------------------------------------- #
# A finished work order: the 409 names the NCR, not reduce-production
# --------------------------------------------------------------------------- #
class TestFinishedWorkOrderScrapRemedy:
    def _finished(self, db: Session, wo_status: WorkOrderStatus):
        wo, (op10,) = make_routing(db, work_centers=[make_work_center(db)], statuses=[DONE], wo_status=wo_status)
        set_op(db, op10, quantity_complete=10)
        return wo, op10

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
    def test_the_refusal_names_a_nonconformance_and_writes_nothing(self, client, db_session, supervisor, wo_status):
        wo, op10 = self._finished(db_session, wo_status)
        acct = operation_accounting(wo, op10)
        before = _world(db_session)

        resp = _put_scrap(client, op10, user_headers(supervisor), 1)
        _refused(
            db_session,
            resp,
            before,
            status=409,
            detail=completed_scrap_over_received_message(op10, acct, wo_status.value),
        )
        assert "reduce-production" not in resp.json()["detail"], "never the door that is shut"

    def test_the_complete_work_order_text(self, client, db_session, supervisor):
        _, op10 = self._finished(db_session, WorkOrderStatus.COMPLETE)
        resp = _put_scrap(client, op10, user_headers(supervisor), 1)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "Op 10 is complete with 10 good and 0 scrapped of the 10 that reached it, so only 0 more can be recorded "
            "as scrap. This work order is complete, so its recorded production can no longer be corrected: record "
            "pieces later found bad as a nonconformance (NCR) against the finished work or its lot, not on the "
            "operation."
        )

    def test_the_old_advice_was_a_second_refusal(self, client, db_session, supervisor):
        """Why the text changed: the remedy it used to name refuses every terminal work order."""
        _, op10 = self._finished(db_session, WorkOrderStatus.COMPLETE)
        resp = _reduce(client, OFFICE_REDUCE_URL, op10, user_headers(supervisor), delta=1, reopen=True)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == MSG_TERMINAL_WORK_ORDER

    def test_a_live_work_order_keeps_the_reduce_production_text(self, client, db_session, supervisor):
        wo, (op10, _) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session), make_work_center(db_session)],
            statuses=[DONE, READY],
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        set_op(db_session, op10, quantity_complete=10)
        resp = _put_scrap(client, op10, user_headers(supervisor), 1)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == completed_scrap_over_received_message(op10, operation_accounting(wo, op10))
        assert resp.json()["detail"].endswith(
            "Pieces counted good and later found bad: lower the good count with reduce-production (reopen the "
            "operation), then complete it again with the scrap and a reason."
        )

    def test_scrap_into_an_unaccounted_remainder_still_lands_on_a_finished_job(self, client, db_session, supervisor):
        """The bound is unchanged by status: a legacy short close's remainder may still be scrapped."""
        _, op10 = self._finished(db_session, WorkOrderStatus.COMPLETE)
        set_op(db_session, op10, quantity_complete=8)
        resp = _put_scrap(client, op10, user_headers(supervisor), 2)
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrderOperation, op10.id).quantity_scrapped) == 2


# --------------------------------------------------------------------------- #
# An operation nobody clocked in to: the office corrects its own good count
# --------------------------------------------------------------------------- #
class TestOperationNobodyClockedInTo:
    def _desk_closed(self, client, db: Session, supervisor: User):
        """op10 closed at the desk at 10 good with no labor at all; one sheet per run tied to it."""
        wo, (op10, op20) = make_routing(
            db,
            work_centers=[make_work_center(db), make_work_center(db)],
            statuses=[READY, PENDING],
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        tie_material(db, wo, op10)
        closed = client.post(
            OFFICE_COMPLETE_URL.format(op=op10.id), params={"quantity_complete": 10}, headers=user_headers(supervisor)
        )
        assert closed.status_code == 200, closed.text
        row = fresh(db, WorkOrderOperation, op10.id)
        assert (row.status, float(row.quantity_complete)) == (DONE, 10)
        assert db.query(TimeEntry).filter(TimeEntry.operation_id == op10.id).count() == 0
        assert _consumed(db, op10.id) == 10
        return wo, row, op20

    def test_found_bad_pieces_can_be_moved_from_good_to_scrap(self, client, db_session, supervisor):
        """The probe: PUT scrap 2 -> 409 'reduce-production', reduce-production -> 400 'Only 0 piece(s)'.
        Now: reduce with reopen lowers the desk's count, the re-close records the scrap, and the
        tied material stays consumed exactly once (10 sheets for 10 runs)."""
        _, op10, op20 = self._desk_closed(client, db_session, supervisor)
        headers = user_headers(supervisor)

        refused = _put_scrap(client, op10, headers, 2)
        assert refused.status_code == 409, refused.text
        assert "reduce-production" in refused.json()["detail"]

        reduced = _reduce(client, OFFICE_REDUCE_URL, op10, headers, delta=2, reopen=True)
        assert reduced.status_code == 200, reduced.text
        assert reduced.json()["reduced_time_entries"] == []
        row = fresh(db_session, WorkOrderOperation, op10.id)
        assert (row.status, float(row.quantity_complete), row.quantity_short_declared) == (OPEN, 8, None)
        assert fresh(db_session, WorkOrderOperation, op20.id).status == PENDING

        reduction_row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op10.id,
                AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
            )
            .one()
        )
        assert reduction_row.extra_data["no_labor_recorded"] is True
        assert reduction_row.old_values["quantity_complete"] == 10
        assert reduction_row.new_values["quantity_complete"] == 8
        reopen_rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op10.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )
        assert any((r.extra_data or {}).get("transition") == "reopened_by_correction" for r in reopen_rows)

        reclosed = client.post(
            OFFICE_COMPLETE_URL.format(op=op10.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "found cracked at inspection"},
            headers=headers,
        )
        assert reclosed.status_code == 200, reclosed.text
        row = fresh(db_session, WorkOrderOperation, op10.id)
        assert (row.status, float(row.quantity_complete), float(row.quantity_scrapped)) == (DONE, 8, 2)
        assert _consumed(db_session, op10.id) == 10, "good + scrap unchanged: no sheet consumed twice"

    def test_without_reopen_it_is_the_leave_short_409(self, client, db_session, supervisor):
        _, op10, _ = self._desk_closed(client, db_session, supervisor)
        before = _world(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op10, user_headers(supervisor), delta=2)
        _refused(db_session, resp, before, status=409, detail=reduce_would_leave_short_message(op10, 2, 2))

    def test_the_correction_cannot_exceed_the_desk_count(self, client, db_session, supervisor):
        _, op10, _ = self._desk_closed(client, db_session, supervisor)
        before = _world(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op10, user_headers(supervisor), delta=11, reopen=True)
        _refused(db_session, resp, before, status=400, detail=no_labor_reduction_exceeds_message(op10, 10))
        assert resp.json()["detail"] == (
            "Only 10 good piece(s) are recorded on Op 10, which nobody clocked in to; the correction cannot exceed them."
        )

    def test_an_api_token_is_still_refused(self, client, db_session, admin_headers, supervisor):
        _, op10, _ = self._desk_closed(client, db_session, supervisor)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op10, headers, delta=2, reopen=True)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_an_operation_with_labor_keeps_the_evidence_bound(self, client, db_session, supervisor):
        """A clock-in with zero pieces is labor: the office's surplus over it is not walkable (unchanged)."""
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=0)
        set_op(db_session, op, quantity_complete=4)
        before = _world(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op, user_headers(supervisor), delta=1)
        _refused(
            db_session,
            resp,
            before,
            status=400,
            detail=(
                "Only 0 piece(s) are recorded on this operation's time entries; "
                "the correction cannot exceed the recorded evidence."
            ),
        )

    def test_approved_labor_still_needs_unapprove_first(self, client, db_session, supervisor):
        """Approved entries are excluded from the walk, but they are labor: not a desk-only count."""
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=4, approved=True)
        set_op(db_session, op, quantity_complete=4)
        before = _world(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op, user_headers(supervisor), delta=1)
        _refused(
            db_session,
            resp,
            before,
            status=400,
            detail=(
                "Only 0 piece(s) on this operation are unapproved and correctable; "
                "4 piece(s) are on approved labor -- unapprove it first."
            ),
        )

    @pytest.mark.parametrize("race", ["clock_in", "lowered"])
    def test_the_shared_core_rechecks_both_facts_under_the_lock(self, client, db_session, supervisor, race):
        """A clock-in, or another correction, between the route's read and the lock is a 409 with
        nothing written -- the core never lowers a count that labor now backs, or below zero."""
        _, op10, _ = self._desk_closed(client, db_session, supervisor)
        if race == "clock_in":
            add_entry(db_session, make_user(db_session), op10, open_entry=True)
        else:
            set_op(db_session, op10, quantity_complete=1)
        before = _world(db_session)
        with pytest.raises(HTTPException) as refused:
            perform_production_reduction(
                db_session,
                operation_id=op10.id,
                company_id=op10.company_id,
                actor=supervisor,
                audit=AuditService(db_session, supervisor),
                entries=[],
                delta=2,
                reason="core probe",
                notes=None,
                recorded_source=None,
                notes_entry=None,
                event_source_module="work_orders",
                path="office",
                allow_completed_operation=True,
                reopen_operation=True,
                no_labor_recorded=True,
            )
        assert refused.value.status_code == 409
        assert refused.value.detail == "This operation was modified concurrently. Refresh and retry."
        db_session.rollback()
        assert _world(db_session) == before
