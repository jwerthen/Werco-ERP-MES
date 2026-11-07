"""Short count (owner ruling 1) on the ADDITIVE floor verbs and the read-time reconcile.

* ``POST /shop-floor/clock-out/{id}`` auto-completes on a full GOOD count of the units that
  REACHED the operation (``is_full_count``) -- never on good + scrap, so it never closes a
  short -- caps good at the operation's ceiling, says the resulting ``operation_status``,
  and on an operation someone ALREADY completed is labor-only (pieces refused 409, a
  0-piece clock-out closes the entry and re-finalizes nothing).
* ``POST /shop-floor/operations/{id}/production`` caps at the same ceiling.
* The reconcile-on-read completes a stage at what reached it after an upstream declared
  short, never closes a short itself, and stamps the accounting on its audit row.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operation_action_gates import ceiling_exceeded_message, completed_operation_clock_out_message
from app.services.work_order_state_service import OperationAccounting
from tests.api.kiosk_test_helpers import make_user, make_work_center, user_headers
from tests.api.short_count_helpers import (
    CLOCK_OUT_URL,
    PRODUCTION_URL,
    add_entry,
    fresh,
    ledger_rows,
    make_routing,
    op_audit_rows,
    set_op,
    tie_material,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


def _untouched(db: Session, op_id: int, entry_id: int):
    """Op + entry + audit + ledger, read only after checking nothing is pending in the session."""
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified and not db.new, f"pending changes in the session: {modified} {db.new}"
    op = fresh(db, WorkOrderOperation, op_id)
    entry = db.query(TimeEntry).filter(TimeEntry.id == entry_id).one()
    return (
        op.status,
        float(op.quantity_complete or 0),
        float(op.quantity_scrapped or 0),
        op.actual_end,
        op.version,
        entry.clock_out,
        float(entry.quantity_produced or 0),
        float(entry.quantity_scrapped or 0),
        len(op_audit_rows(db, op_id)),
        len(ledger_rows(db)),
    )


def _clock_out(client: TestClient, user, entry, **body):
    return client.post(CLOCK_OUT_URL.format(entry=entry.id), json=body, headers=user_headers(user))


def _upstream_declared(db: Session, *, sequential: bool = True):
    """op 10 COMPLETE at 8 good + 2 scrapped (declared 2); op 20 open at its own work center."""
    wc1, wc2 = make_work_center(db), make_work_center(db)
    wo, (op10, op20) = make_routing(db, work_centers=[wc1, wc2], statuses=[DONE, OPEN], sequential=sequential)
    set_op(db, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
    return wo, op10, op20


class TestClockOut:
    def test_a_full_count_completes_and_says_so(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        entry = add_entry(db_session, ann, op, open_entry=True)
        resp = _clock_out(client, ann, entry, quantity_produced=10)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation_status"] == "complete"
        assert fresh(db_session, WorkOrderOperation, op.id).status == DONE

    def test_good_plus_scrap_never_closes_a_short(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        entry = add_entry(db_session, ann, op, open_entry=True)
        resp = _clock_out(client, ann, entry, quantity_produced=8, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation_status"] == "in_progress"
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_complete, op_row.quantity_scrapped) == (OPEN, 8, 2)
        assert op_row.quantity_short_declared is None, "only an explicit close declares a short"

    def test_a_downstream_stage_auto_completes_at_what_reached_it(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        _, _, op20 = _upstream_declared(db_session)
        entry = add_entry(db_session, ann, op20, open_entry=True)
        resp = _clock_out(client, ann, entry, quantity_produced=8)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation_status"] == "complete"
        (row,) = [r for r in op_audit_rows(db_session, op20.id) if r.action == "STATUS_CHANGE"]
        assert row.extra_data["target"] == 10
        assert row.extra_data["received"] == 8
        assert row.extra_data["upstream_quantity_short"] == 2

    def test_good_is_capped_at_the_ceiling_on_a_sequenced_routing(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        _, _, op20 = _upstream_declared(db_session)
        entry = add_entry(db_session, ann, op20, open_entry=True)
        before = _untouched(db_session, op20.id, entry.id)
        resp = _clock_out(client, ann, entry, quantity_produced=9)
        assert resp.status_code == 400, resp.text
        assert _untouched(db_session, op20.id, entry.id) == before
        expected = ceiling_exceeded_message(
            op20, 9, OperationAccounting(target=10, good=0, scrapped=0, chain_upstream=2)
        )
        assert resp.json()["detail"] == expected
        assert expected == "Quantity (9) exceeds the 8 units that can reach Op 20 — 2 were closed short upstream."
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None, "refused before any mutation"

    def test_a_pooled_work_order_has_no_ceiling_credit(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        _, _, op20 = _upstream_declared(db_session, sequential=False)
        entry = add_entry(db_session, ann, op20, open_entry=True)
        resp = _clock_out(client, ann, entry, quantity_produced=9)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation_status"] == "in_progress"


class TestClockOutOnAnAlreadyCompleteOperation:
    """SF4: the office (or a crew lead) closed the operation while Bob was clocked in."""

    def _closed_with_bob_in(self, db: Session):
        bob = make_user(db, role=UserRole.OPERATOR)
        wc = make_work_center(db)
        wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
        tie_material(db, wo, op)
        entry = add_entry(db, bob, op, open_entry=True)
        closer = make_user(db, role=UserRole.SUPERVISOR)
        set_op(db, op, status=DONE, quantity_complete=10, completed_by=closer.id)
        op = fresh(db, WorkOrderOperation, op.id)
        return bob, op, entry

    def test_pieces_are_refused_and_the_entry_stays_open(self, client, db_session):
        bob, op, entry = self._closed_with_bob_in(db_session)
        before = _untouched(db_session, op.id, entry.id)
        resp = _clock_out(client, bob, entry, quantity_produced=2)
        assert resp.status_code == 409, resp.text
        assert _untouched(db_session, op.id, entry.id) == before
        assert resp.json()["detail"] == completed_operation_clock_out_message(op)
        assert resp.json()["detail"] == (
            "Op 10 is already complete; clock out with 0 pieces and tell a supervisor about any extra pieces."
        )
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None

    def test_a_zero_piece_clock_out_is_labor_only(self, client, db_session):
        bob, op, entry = self._closed_with_bob_in(db_session)
        before_end, before_by = op.actual_end, op.completed_by
        before_hours = float(op.actual_run_hours or 0)
        resp = _clock_out(client, bob, entry)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation_status"] == "complete"
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert fresh(db_session, TimeEntry, entry.id).clock_out is not None
        assert (op_row.actual_end, op_row.completed_by) == (before_end, before_by)
        assert float(op_row.actual_run_hours or 0) > before_hours, "the hours still roll up"
        assert float(op_row.quantity_complete) == 10
        assert [r for r in op_audit_rows(db_session, op.id) if r.action == "STATUS_CHANGE"] == []
        assert ledger_rows(db_session) == [], "no re-finalize, so no consumption"


class TestProductionReport:
    def test_production_is_capped_at_the_ceiling(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        _, _, op20 = _upstream_declared(db_session)
        entry = add_entry(db_session, ann, op20, open_entry=True)
        set_op(db_session, op20, quantity_complete=8)
        before = _untouched(db_session, op20.id, entry.id)
        resp = client.post(
            PRODUCTION_URL.format(op=op20.id), json={"quantity_complete_delta": 1}, headers=user_headers(ann)
        )
        assert resp.status_code == 400, resp.text
        assert _untouched(db_session, op20.id, entry.id) == before
        assert resp.json()["detail"].startswith("Quantity (9) exceeds the 8 units that can reach Op 20")
        assert float(fresh(db_session, WorkOrderOperation, op20.id).quantity_complete) == 8


class TestReconcileOnRead:
    def test_a_read_never_closes_a_short(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8, scrapped=2)
        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.status == OPEN
        assert (op_row.quantity_complete, op_row.quantity_scrapped) == (8, 2)

    def test_a_read_completes_a_stage_at_what_reached_it_and_audits_the_accounting(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        wo, _, op20 = _upstream_declared(db_session)
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=WorkOrderStatus.IN_PROGRESS)
        add_entry(db_session, ann, op20, produced=8)

        resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op20.id).status == DONE
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert wo_row.status == WorkOrderStatus.COMPLETE
        assert wo_row.quantity_complete == 8, "the header is what left the last stage"
        (row,) = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op20.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )
        assert row.extra_data["source"] == "reconcile_on_read"
        assert (row.extra_data["target"], row.extra_data["received"], row.extra_data["upstream_quantity_short"]) == (
            10,
            8,
            2,
        )
