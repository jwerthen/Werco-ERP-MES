"""Short count at completion (owner ruling 1, 2026-11-05) -- the office verbs, guards and read models.

The ruling: a short count at completion is reported as scrap with a required reason, or the
work stays open -- never short-closed at the counted quantity. Part A
(``test_operation_short_count*.py``) pins the accounting core and the floor close; this file
pins the rest of the PR:

1. OFFICE ``POST /work-orders/operations/{id}/complete`` -- the scrap TOTAL never lowers, more
   good than the floor counted needs ``quantity_override_reason``, a short close needs the
   open entries acknowledged (and then closes them, nothing credited), an unchanged submit
   writes nothing, and the quantity row is REQUIRED evidence when it must be.
2. FORCE ``POST /work-orders/{id}/complete`` -- bounded at the WORK-ORDER grain by what the
   floor recorded, the shortfall covered by reported scrap, and the header SET to the
   declared quantity (INT1: never ``max()``'d back up to the order).
3. MUTATOR GUARDS -- the generic operation PUT on a COMPLETE operation, the office reduce
   that would leave a COMPLETE operation short (``reopen_operation``), and the work-order PUT
   lowering ``quantity_ordered`` below what a stage accounted for.
4. READ MODELS -- every floor/office payload carries the server's figures.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_SHORT_CLOSE,
    MSG_REOPEN_IS_OFFICE_ONLY,
    MSG_SCRAP_REASON_OR_CODE_REQUIRED,
    MSG_WORK_ORDER_ON_HOLD,
    completed_counts_locked_message,
    force_complete_exceeds_physical_message,
    force_complete_exceeds_recorded_message,
    force_complete_unaccounted_message,
    office_no_change_message,
    others_clocked_in_message,
    override_reason_required_message,
    quantity_ordered_floor_message,
    reduce_would_leave_short_message,
    reopen_blocked_by_later_message,
)
from tests.api.kiosk_test_helpers import make_user, make_work_center, queue_url, user_headers
from tests.api.short_count_helpers import (
    add_entry,
    fresh,
    ledger_rows,
    make_routing,
    op_audit_rows,
    set_op,
    tie_material,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"
OFFICE_REDUCE_URL = "/api/v1/work-orders/operations/{op}/reduce-production"
OPERATOR_REDUCE_URL = "/api/v1/shop-floor/operations/{op}/reduce-production"
OPERATION_PUT_URL = "/api/v1/work-orders/operations/{op}"
WORK_ORDER_URL = "/api/v1/work-orders/{wo}"
API_TOKENS_URL = "/api/v1/api-tokens/"

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
def supervisor(db_session: Session):
    return make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sue", last_name="Visor")


def _office(client: TestClient, op, user_or_headers, **params):
    headers = user_or_headers if isinstance(user_or_headers, dict) else user_headers(user_or_headers)
    return client.post(OFFICE_COMPLETE_URL.format(op=op.id), params=params, headers=headers)


def _force(client: TestClient, wo, user_or_headers, **params):
    headers = user_or_headers if isinstance(user_or_headers, dict) else user_headers(user_or_headers)
    return client.post(FORCE_COMPLETE_URL.format(wo=wo.id), params=params, headers=headers)


def _token_headers(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "short probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _op_state(db: Session, op_id: int):
    db.expire_all()
    op = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).one()
    entries = db.query(TimeEntry).filter(TimeEntry.operation_id == op_id).order_by(TimeEntry.id).all()
    return (
        op.status,
        float(op.quantity_complete or 0),
        float(op.quantity_scrapped or 0),
        op.scrap_reason,
        op.quantity_short_declared,
        op.actual_start,
        [(e.id, e.clock_out is None, float(e.quantity_produced or 0)) for e in entries],
    )


def _wo_audit_rows(db: Session, wo_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id)
        .order_by(AuditLog.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# 1. OFFICE op /complete
# --------------------------------------------------------------------------- #
class TestOfficeComplete:
    def test_a_short_covered_by_scrap_closes_and_is_declared(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)

        resp = _office(client, op, supervisor, quantity_complete=8, quantity_scrapped=2, scrap_reason="Cracked weld")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "Operation completed"
        assert body["is_fully_complete"] is True
        assert body["operation"]["quantity_short_declared"] == 2
        assert body["operation"]["quantity_unaccounted"] == 0

        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.status == DONE
        assert (op_row.quantity_complete, op_row.quantity_scrapped, op_row.quantity_short_declared) == (8, 2, 2)
        assert op_row.scrap_reason == "Cracked weld"
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 8

        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "UPDATE"]
        assert row.new_values["quantity_short_declared"] == 2
        assert row.extra_data["closed_short"] is True
        assert row.extra_data["received"] == 10
        assert row.extra_data["unaccounted_after"] == 0

    def test_units_still_unaccounted_save_progress_and_stay_open(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=6)

        resp = _office(client, op, supervisor, quantity_complete=8)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "Progress updated"
        assert body["is_fully_complete"] is False
        assert body["operation"]["quantity_unaccounted"] == 2
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_complete, op_row.quantity_short_declared) == (OPEN, 8, None)

    def test_an_unchanged_submit_on_a_ready_operation_writes_nothing(self, client, db_session, supervisor):
        """SF9 -- no auto-start, no audit row, no work-order lift."""
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[READY])
        before = _op_state(db_session, op.id)

        resp = _office(client, op, supervisor, quantity_complete=0)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == office_no_change_message(op) == "No change — Op 10 left open"
        assert resp.json()["is_fully_complete"] is False
        assert _op_state(db_session, op.id) == before
        assert fresh(db_session, WorkOrderOperation, op.id).actual_start is None
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
        assert op_audit_rows(db_session, op.id) == []

    def test_the_scrap_total_never_lowers_and_its_reason_is_kept(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=2, quantity_scrapped=3, scrap_reason="Burr")

        resp = _office(client, op, supervisor, quantity_complete=5, quantity_scrapped=0)
        assert resp.status_code == 200, resp.text
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.quantity_scrapped == 3, "the floor's scrap is never lowered from the desk"
        assert op_row.scrap_reason == "Burr", "and its reason is never nulled"

    def test_a_scrap_rise_needs_a_reason_and_writes_nothing_without_one(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=8)
        before = _op_state(db_session, op.id)

        resp = _office(client, op, supervisor, quantity_complete=8, quantity_scrapped=2)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == MSG_SCRAP_REASON_OR_CODE_REQUIRED
        assert _op_state(db_session, op.id) == before
        assert op_audit_rows(db_session, op.id) == []

    def test_more_good_than_the_floor_counted_needs_an_override_reason(self, client, db_session, supervisor):
        """INT7 -- allowed with a reason, written as REQUIRED evidence."""
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        before = _op_state(db_session, op.id)

        refused = _office(client, op, supervisor, quantity_complete=10)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == override_reason_required_message(op, 8)
        assert "the 8 counted on the floor for Op 10" in refused.json()["detail"]
        assert _op_state(db_session, op.id) == before

        allowed = _office(
            client, op, supervisor, quantity_complete=10, quantity_override_reason="Operator forgot to report 2"
        )
        assert allowed.status_code == 200, allowed.text
        assert fresh(db_session, WorkOrderOperation, op.id).status == DONE
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "UPDATE"]
        assert row.extra_data["asserted_above_recorded"] is True
        assert row.extra_data["recorded"] == 8
        assert row.extra_data["override_reason"] == "Operator forgot to report 2"

    def test_desk_cleanup_of_never_clocked_work_needs_no_reason(self, client, db_session, supervisor):
        """The owner's floor-requires-labor / office-exempt asymmetry is untouched."""
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])

        resp = _office(client, op, supervisor, quantity_complete=10)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).status == DONE

    def test_a_short_close_must_acknowledge_who_is_still_clocked_in(self, client, db_session, supervisor):
        bob = make_user(db_session, first_name="Bob", last_name="Turner")
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        bob_entry = add_entry(db_session, bob, op, produced=8, open_entry=True)
        set_op(db_session, op, quantity_complete=8)
        before = _op_state(db_session, op.id)

        refused = _office(client, op, supervisor, quantity_complete=8, quantity_scrapped=2, scrap_reason="crack")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == others_clocked_in_message(op, ["Bob T."], office=True)
        assert _op_state(db_session, op.id) == before

        acked = _office(
            client,
            op,
            supervisor,
            quantity_complete=8,
            quantity_scrapped=2,
            scrap_reason="crack",
            acknowledge_open_entries=True,
        )
        assert acked.status_code == 200, acked.text
        bob_row = fresh(db_session, TimeEntry, bob_entry.id)
        assert bob_row.clock_out is not None, "acknowledged: Bob is clocked out"
        assert float(bob_row.quantity_produced) == 8, "with his own count, nothing credited"
        assert [e["user_id"] for e in acked.json()["closed_time_entries"]] == [bob.id]
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "UPDATE"]
        assert row.extra_data["acknowledged_open_entries"] == [bob_entry.id]

    def test_a_full_count_close_never_clocks_anyone_out(self, client, db_session, supervisor):
        bob = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        bob_entry = add_entry(db_session, bob, op, produced=10, open_entry=True)
        set_op(db_session, op, quantity_complete=10)

        resp = _office(client, op, supervisor, quantity_complete=10)
        assert resp.status_code == 200, resp.text
        assert resp.json()["closed_time_entries"] == []
        assert fresh(db_session, TimeEntry, bob_entry.id).clock_out is None

    def test_an_api_token_may_not_close_short(self, client, db_session, supervisor, admin_headers):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=8)
        token_headers = _token_headers(client, admin_headers, supervisor)
        before = _op_state(db_session, op.id)

        resp = _office(client, op, token_headers, quantity_complete=8, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE
        assert _op_state(db_session, op.id) == before

        full = _office(client, op, token_headers, quantity_complete=10)
        assert full.status_code == 200, full.text

    def test_good_above_what_can_reach_the_operation_is_refused(self, client, db_session, supervisor):
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc10, wc20], statuses=[DONE, READY])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)

        refused = _office(client, op20, supervisor, quantity_complete=9)
        assert refused.status_code == 400, refused.text
        assert (
            refused.json()["detail"]
            == "Quantity (9) exceeds the 8 units that can reach Op 20 — 2 were closed short upstream."
        )

        closed = _office(client, op20, supervisor, quantity_complete=8)
        assert closed.status_code == 200, closed.text
        assert closed.json()["operation"]["upstream_quantity_short"] == 2
        assert fresh(db_session, WorkOrderOperation, op20.id).status == DONE
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 8

    @pytest.mark.parametrize(
        "params",
        [
            # scrap rising with no reason -- would be the 422 if the input checks ran first
            {"quantity_complete": 8, "quantity_scrapped": 2, "quantity_override_reason": "desk"},
            # nothing changes -- would be the 200 'No change -- Op N left open'
            {"quantity_complete": 0},
            # a short-shaped partial -- would be the 200 'Progress updated' (an auto-start)
            {"quantity_complete": 4},
            # negative scrap -- would be the 400 'quantity_scrapped cannot be negative'
            {"quantity_complete": 8, "quantity_scrapped": -1},
        ],
        ids=["scrap-without-reason", "no-change", "partial", "negative-scrap"],
    )
    def test_a_ready_operation_on_a_held_job_is_refused_before_any_input_check(
        self, client, db_session, supervisor, params
    ):
        """Combined pin (a): the work-order start gate outranks every short-count check.

        A READY operation on an ON_HOLD work order is a STATE conflict: every short-shaped
        submit gets the gates' 409 with nothing written -- not a 200 'No change', not the
        partial branch's auto-start, and not a 422 / 400 from the short-count input checks
        that come after the rung.
        """
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[READY], wo_status=WorkOrderStatus.ON_HOLD)
        before = _op_state(db_session, op.id)

        resp = _office(client, op, supervisor, **params)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
        assert _op_state(db_session, op.id) == before
        assert op_audit_rows(db_session, op.id) == []
        assert ledger_rows(db_session) == []
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.ON_HOLD

    def test_a_required_row_that_cannot_be_saved_writes_nothing(self, client, db_session, supervisor, monkeypatch):
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        tie_material(db_session, wo, op)
        set_op(db_session, op, quantity_complete=8)

        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)
        resp = _office(client, op, supervisor, quantity_complete=8, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == "The completion audit could not be saved; nothing was changed. Please retry."
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_scrapped, op_row.quantity_short_declared) == (OPEN, 0, None)
        assert ledger_rows(db_session) == []


# --------------------------------------------------------------------------- #
# 2. FORCE-COMPLETE (INT1)
# --------------------------------------------------------------------------- #
def _two_stage(db_session: Session, *, op20_good: float, op20_scrap: float = 0.0):
    """Op 10 COMPLETE at the full 10, Op 20 open with ``op20_good`` (+ scrap) on the floor."""
    ann = make_user(db_session)
    wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
    wo, (op10, op20) = make_routing(
        db_session, work_centers=[wc10, wc20], statuses=[DONE, OPEN], wo_status=WorkOrderStatus.IN_PROGRESS
    )
    add_entry(db_session, ann, op10, produced=10)
    set_op(db_session, op10, quantity_complete=10)
    add_entry(db_session, ann, op20, produced=op20_good, scrapped=op20_scrap)
    set_op(
        db_session,
        op20,
        quantity_complete=op20_good,
        quantity_scrapped=op20_scrap,
        scrap_reason="crack" if op20_scrap else None,
    )
    set_op_wo = fresh(db_session, WorkOrder, wo.id)
    set_op_wo.quantity_complete = 10  # the pre-loop header the old max() re-inflated to
    db_session.commit()
    return wo, op10, op20


def _fg_receipt_quantity(db: Session, wo_id: int) -> float:
    from app.models.inventory import InventoryTransaction, TransactionType

    rows = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    return sum(float(row.quantity or 0) for row in rows)


class TestForceComplete:
    def test_the_header_is_set_to_the_declared_quantity_not_maxed_back_up(self, client, db_session, supervisor):
        """INT1 -- the blocker: header 8 and FG 8, never the order's 10."""
        wo, _, op20 = _two_stage(db_session, op20_good=8, op20_scrap=2)

        resp = _force(client, wo, supervisor, quantity_complete=8)
        assert resp.status_code == 200, resp.text
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert wo_row.status == WorkOrderStatus.COMPLETE
        assert wo_row.quantity_complete == 8
        assert _fg_receipt_quantity(db_session, wo.id) == 8
        (row,) = [r for r in _wo_audit_rows(db_session, wo.id) if r.action == "UPDATE"]
        assert row.extra_data["deliverable"] == 8
        assert row.extra_data["floor_loss"] == 2
        assert row.extra_data["asserted_above_recorded"] is False

    def test_more_than_the_floor_recorded_needs_an_override_reason(self, client, db_session, supervisor):
        wo, _, op20 = _two_stage(db_session, op20_good=8)

        refused = _force(client, wo, supervisor, quantity_complete=10)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == force_complete_exceeds_recorded_message(10, 8, op20)
        assert "the 8 the floor recorded at Op 20" in refused.json()["detail"]
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.IN_PROGRESS

        allowed = _force(client, wo, supervisor, quantity_complete=10, quantity_override_reason="counted at pack-out")
        assert allowed.status_code == 200, allowed.text
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 10
        (row,) = [r for r in _wo_audit_rows(db_session, wo.id) if r.action == "UPDATE"]
        assert row.extra_data["asserted_above_recorded"] is True
        assert row.extra_data["override_reason"] == "counted at pack-out"
        assert row.extra_data["binding_operation"] == "20"

    def test_a_shortfall_nobody_scrapped_is_refused(self, client, db_session, supervisor):
        wo, _, _ = _two_stage(db_session, op20_good=8)

        refused = _force(client, wo, supervisor, quantity_complete=8)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == force_complete_unaccounted_message(2, 10, 8, 0)
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.IN_PROGRESS

        covered = _force(client, wo, supervisor, quantity_complete=8, quantity_scrapped=2, scrap_reason="lost")
        assert covered.status_code == 200, covered.text
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 8

    def _declared_upstream(self, db_session):
        ann = make_user(db_session)
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(
            db_session, work_centers=[wc10, wc20], statuses=[DONE, READY], wo_status=WorkOrderStatus.IN_PROGRESS
        )
        add_entry(db_session, ann, op10, produced=8, scrapped=2)
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        return wo

    def test_a_short_the_floor_declared_covers_the_shortfall(self, client, db_session, supervisor):
        wo = self._declared_upstream(db_session)
        resp = _force(client, wo, supervisor, quantity_complete=8)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 8

    def test_an_override_still_cannot_book_units_declared_scrapped(self, client, db_session, supervisor):
        wo = self._declared_upstream(db_session)
        resp = _force(client, wo, supervisor, quantity_complete=10, quantity_override_reason="desk")
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == force_complete_exceeds_physical_message(10, 8, 2)

    def test_an_unreported_work_order_still_completes_at_the_order(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc, make_work_center(db_session)])
        resp = _force(client, wo, supervisor, quantity_complete=10)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 10

    def test_an_api_token_may_not_complete_short(self, client, db_session, supervisor, admin_headers):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc])
        token_headers = _token_headers(client, admin_headers, supervisor)

        resp = _force(client, wo, token_headers, quantity_complete=8, quantity_scrapped=2, scrap_reason="lost")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED

    def test_a_short_completion_whose_audit_row_cannot_be_saved_writes_nothing(
        self, client, db_session, supervisor, monkeypatch
    ):
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc])
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)

        resp = _force(client, wo, supervisor, quantity_complete=8, quantity_scrapped=2, scrap_reason="lost")
        assert resp.status_code == 503, resp.text
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert wo_row.status == WorkOrderStatus.RELEASED
        assert fresh(db_session, WorkOrderOperation, op.id).status == READY
        assert ledger_rows(db_session) == []


# --------------------------------------------------------------------------- #
# 3. MUTATOR GUARDS
# --------------------------------------------------------------------------- #
class TestOperationPutOnACompleteOperation:
    @pytest.mark.parametrize(
        "fields",
        [
            {"quantity_complete": 8},
            {"quantity_scrapped": 1, "scrap_reason": "late note"},
            {"scrap_reason": "late note"},
            {"status": "in_progress"},
        ],
    )
    def test_count_and_status_edits_are_refused(self, client, db_session, supervisor, fields):
        wc = make_work_center(db_session)
        _, (op, _) = make_routing(db_session, work_centers=[wc, make_work_center(db_session)], statuses=[DONE, READY])
        set_op(db_session, op, quantity_complete=10)
        before = _op_state(db_session, op.id)

        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, **fields},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == completed_counts_locked_message(op)
        assert _op_state(db_session, op.id) == before

    def test_resending_the_stored_values_and_other_fields_still_land(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op, _) = make_routing(db_session, work_centers=[wc, make_work_center(db_session)], statuses=[DONE, READY])
        set_op(db_session, op, quantity_complete=10)
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_complete": 10, "status": "complete", "name": "Renamed step"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).name == "Renamed step"

    def test_an_open_operation_keeps_its_editable_counts(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_complete": 3},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).quantity_complete == 3


def _reduce(client: TestClient, op, user, *, delta: float, reopen: bool = False, url: str = OFFICE_REDUCE_URL):
    body = {"quantity_delta": delta, "reason": "counted the tray twice"}
    if reopen:
        body["reopen_operation"] = True
    return client.post(url.format(op=op.id), json=body, headers=user_headers(user))


class TestOfficeReduceReopen:
    def _completed_first_stage(self, db_session, *, sequential=True, same_work_center=False, op10_scrap=0.0):
        ann = make_user(db_session)
        wc10 = make_work_center(db_session)
        wc20 = wc10 if same_work_center else make_work_center(db_session)
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc10, wc20],
            statuses=[DONE, READY],
            sequential=sequential,
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        entry = add_entry(db_session, ann, op10, produced=10)
        set_op(db_session, op10, quantity_complete=10, quantity_scrapped=op10_scrap)
        return wo, op10, op20, entry

    def test_leaving_a_complete_operation_short_needs_reopen(self, client, db_session, supervisor):
        wo, op10, op20, entry = self._completed_first_stage(db_session)
        before = _op_state(db_session, op10.id)

        refused = _reduce(client, op10, supervisor, delta=1)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == reduce_would_leave_short_message(op10, 1, 1)
        assert _op_state(db_session, op10.id) == before

    def test_reopen_returns_it_to_the_floor_and_demotes_what_it_now_blocks(self, client, db_session, supervisor):
        wo, op10, op20, entry = self._completed_first_stage(db_session)
        set_op(db_session, op10, quantity_short_declared=None)

        resp = _reduce(client, op10, supervisor, delta=1, reopen=True)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"]["status"] == "in_progress"
        op10_row = fresh(db_session, WorkOrderOperation, op10.id)
        assert op10_row.status == OPEN
        assert op10_row.quantity_complete == 9
        assert op10_row.actual_end is None and op10_row.completed_by is None
        assert op10_row.quantity_short_declared is None
        assert fresh(db_session, WorkOrderOperation, op20.id).status == PENDING
        assert fresh(db_session, WorkOrder, wo.id).current_operation_id == op10.id

        reopen_rows = [
            r
            for r in op_audit_rows(db_session, op10.id)
            if r.action == "STATUS_CHANGE" and r.extra_data.get("transition") == "reopened_by_correction"
        ]
        assert len(reopen_rows) == 1
        assert reopen_rows[0].new_values == {"status": "in_progress"}
        assert reopen_rows[0].extra_data["reason"] == "counted the tray twice"
        demotions = [r for r in op_audit_rows(db_session, op20.id) if r.action == "STATUS_CHANGE"]
        assert [r.new_values for r in demotions] == [{"status": "pending"}]

    def test_a_pooled_job_demotes_across_work_centers_only(self, client, db_session, supervisor):
        _, op10, op20, _ = self._completed_first_stage(db_session, sequential=False)
        assert _reduce(client, op10, supervisor, delta=1, reopen=True).status_code == 200
        assert fresh(db_session, WorkOrderOperation, op20.id).status == PENDING

        _, op10b, op20b, _ = self._completed_first_stage(db_session, sequential=False, same_work_center=True)
        assert _reduce(client, op10b, supervisor, delta=1, reopen=True).status_code == 200
        assert fresh(db_session, WorkOrderOperation, op20b.id).status == READY, "a pool's sibling is not blocked"

    def test_a_correction_scrap_still_covers_keeps_it_complete(self, client, db_session, supervisor):
        _, op10, _, _ = self._completed_first_stage(db_session, op10_scrap=2)
        resp = _reduce(client, op10, supervisor, delta=1)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op10.id).status == DONE

    def test_a_later_completed_stage_must_be_corrected_first(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wcs = [make_work_center(db_session) for _ in range(3)]
        _, (op10, op20, _) = make_routing(
            db_session, work_centers=wcs, statuses=[DONE, DONE, READY], wo_status=WorkOrderStatus.IN_PROGRESS
        )
        add_entry(db_session, ann, op10, produced=10)
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op20, quantity_complete=10)

        resp = _reduce(client, op10, supervisor, delta=1, reopen=True)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == reopen_blocked_by_later_message(op20)
        assert resp.json()["detail"] == "Op 20 is already complete with these units; correct Op 20 first."
        assert fresh(db_session, WorkOrderOperation, op10.id).status == DONE

    def test_the_operator_verb_refuses_reopen(self, client, db_session):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=4, open_entry=True)
        resp = _reduce(client, op, ann, delta=1, reopen=True, url=OPERATOR_REDUCE_URL)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == MSG_REOPEN_IS_OFFICE_ONLY

    def test_a_reopen_whose_audit_cannot_be_saved_writes_nothing(self, client, db_session, supervisor, monkeypatch):
        _, op10, op20, entry = self._completed_first_stage(db_session)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)

        resp = _reduce(client, op10, supervisor, delta=1, reopen=True)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == "The correction audit could not be saved; nothing was changed. Please retry."
        op10_row = fresh(db_session, WorkOrderOperation, op10.id)
        assert (op10_row.status, op10_row.quantity_complete) == (DONE, 10)
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_produced) == 10
        assert fresh(db_session, WorkOrderOperation, op20.id).status == READY


class TestQuantityOrderedFloor:
    def test_the_order_cannot_drop_below_what_a_stage_accounted_for(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=6, quantity_scrapped=1, scrap_reason="crack")
        version = fresh(db_session, WorkOrder, wo.id).version

        refused = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": version, "quantity_ordered": 6},
            headers=user_headers(supervisor),
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == quantity_ordered_floor_message(op, 7)
        assert fresh(db_session, WorkOrder, wo.id).quantity_ordered == 10

        allowed = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": version, "quantity_ordered": 7},
            headers=user_headers(supervisor),
        )
        assert allowed.status_code == 200, allowed.text
        assert fresh(db_session, WorkOrder, wo.id).quantity_ordered == 7


# --------------------------------------------------------------------------- #
# 4. READ MODELS
# --------------------------------------------------------------------------- #
@pytest.fixture
def declared_upstream(db_session: Session):
    """Sequential: Op 10 closed 8 + 2 (declared 2); Op 20 running with 5 good."""
    ann = make_user(db_session)
    wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
    wo, (op10, op20) = make_routing(
        db_session, work_centers=[wc10, wc20], statuses=[DONE, OPEN], wo_status=WorkOrderStatus.IN_PROGRESS
    )
    add_entry(db_session, ann, op10, produced=8, scrapped=2)
    set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2, scrap_reason="x")
    add_entry(db_session, ann, op20, produced=5, open_entry=True)
    set_op(db_session, op20, quantity_complete=5)
    return ann, wo, op10, op20, wc20


def _assert_figures(row: dict, *, upstream: float, ceiling: float, unaccounted: float):
    assert row["upstream_quantity_short"] == upstream
    assert row["quantity_good_ceiling"] == ceiling
    assert row["quantity_unaccounted"] == unaccounted


class TestReadModels:
    def test_work_order_detail_carries_the_operation_and_header_figures(self, client, db_session, declared_upstream):
        ann, wo, op10, op20, _ = declared_upstream
        supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
        body = client.get(WORK_ORDER_URL.format(wo=wo.id), headers=user_headers(supervisor)).json()
        ops = {op["id"]: op for op in body["operations"]}
        _assert_figures(ops[op20.id], upstream=2, ceiling=8, unaccounted=3)
        assert ops[op10.id]["quantity_short_declared"] == 2
        assert ops[op10.id]["quantity_unaccounted"] == 0
        assert body["quantity_deliverable"] == 5
        assert body["quantity_accounted_loss"] == 2

    def test_the_kiosk_queue_and_held_rows_carry_them_in_one_read(
        self, client, db_session, declared_upstream, monkeypatch
    ):
        ann, _, _, op20, wc20 = declared_upstream
        _, (held,) = make_routing(db_session, work_centers=[wc20], statuses=[OperationStatus.ON_HOLD])
        import app.api.endpoints.shop_floor as shop_floor

        calls = []
        real = shop_floor.upstream_figures_by_operation

        def _spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(shop_floor, "upstream_figures_by_operation", _spy)
        body = client.get(queue_url(wc20.id), headers=user_headers(ann)).json()
        assert len(calls) == 1, "ONE accounting read per poll, whatever the card count"
        (row,) = [r for r in body["queue"] if r["operation_id"] == op20.id]
        _assert_figures(row, upstream=2, ceiling=8, unaccounted=3)
        (held_row,) = [r for r in body["held"] if r["operation_id"] == held.id]
        _assert_figures(held_row, upstream=0, ceiling=10, unaccounted=10)

    def test_the_shop_floor_operations_page_carries_them_in_one_read(
        self, client, db_session, declared_upstream, monkeypatch
    ):
        ann, _, _, op20, wc20 = declared_upstream
        import app.api.endpoints.shop_floor as shop_floor

        calls = []
        real = shop_floor.upstream_figures_by_operation
        monkeypatch.setattr(
            shop_floor, "upstream_figures_by_operation", lambda *a, **k: calls.append(a) or real(*a, **k)
        )
        body = client.get(
            "/api/v1/shop-floor/operations", params={"work_center_id": wc20.id}, headers=user_headers(ann)
        ).json()
        assert len(calls) == 1
        (row,) = [r for r in body["operations"] if r["id"] == op20.id]
        _assert_figures(row, upstream=2, ceiling=8, unaccounted=3)

    def test_the_running_job_carries_them(self, client, db_session, declared_upstream):
        ann, _, _, op20, _ = declared_upstream
        body = client.get("/api/v1/shop-floor/my-active-job", headers=user_headers(ann)).json()
        (job,) = [j for j in body["active_jobs"] if j["operation_id"] == op20.id]
        _assert_figures(job, upstream=2, ceiling=8, unaccounted=3)

    def test_the_scanner_summary_carries_them(self, client, db_session, declared_upstream):
        ann, _, _, op20, _ = declared_upstream
        resp = client.post("/api/v1/scanner/resolve-action", json={"code": f"OP:{op20.id}"}, headers=user_headers(ann))
        assert resp.status_code == 200, resp.text
        summary = resp.json()["operation"]
        assert summary["upstream_quantity_short"] == 2
        assert summary["quantity_unaccounted"] == 3


def test_the_blocked_by_scope_needs_the_reopened_operation(db_session: Session):
    """The one demotion core refuses a ``blocked_by`` demotion with nothing to be blocked by."""
    from app.services.work_order_state_service import DEMOTION_SCOPES, demote_ready_operations

    assert DEMOTION_SCOPES == ("sequencing", "all_unworked", "blocked_by")
    wc = make_work_center(db_session)
    wo, _ = make_routing(db_session, work_centers=[wc])
    with pytest.raises(ValueError):
        demote_ready_operations(db_session, wo, scope="blocked_by")
