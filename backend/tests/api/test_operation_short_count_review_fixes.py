"""Short count at completion (owner ruling 1) -- the review-stage fixes, pinned.

Every test here reproduces a hole four reviewers found in the first build and asserts the
corrected behavior, through the real endpoints where a flow is involved:

* the all-complete header on a routing made only of component operations (no FG of 0);
* the hard ceiling holds on every write: the floor close, the reconcile, the generic
  operation PUT, the clock-out (which never traps a 0-piece entry), and a re-close upstream
  that would leave a later stage over its new ceiling;
* soft component feed never auto-completes an assembly stage below its hard ceiling, and a
  replenished stage stops carrying the feed shortfall;
* every change to a declaration -- a reopen (across chains too), an office correction that
  re-stamps it, a sequencing flip -- is judged by its effect on the OTHER operations;
* a pooled work order's floor is told not to scrap units an earlier stage already lost;
* ``quantity_ordered`` cannot be raised under a completed stage, and restated whole-order
  targets move with it;
* force-complete: the API-token refusal comes first, and quantity + hard loss never exceeds
  the order;
* the office: a stale scrap rise, an approved open entry under an acknowledged close;
* standing API tokens may not make count judgments; the evidence read is tenant-scoped.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.inventory import InventoryTransaction, TransactionType
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_COUNT_JUDGMENT,
    API_TOKEN_SCRAP,
    API_TOKEN_SHORT_CLOSE,
    others_clocked_in_message,
    override_reason_required_message,
    reduce_would_leave_short_message,
)
from app.services.work_order_state_service import (
    _operation_produced_evidence,
    finished_quantity_at_all_complete,
    operation_accounting,
    reconcile_work_orders_from_completion_evidence,
    work_order_completion_bounds,
)
from tests.api.kiosk_test_helpers import COMPANY_B, make_user, make_work_center, user_headers
from tests.api.short_count_helpers import (
    CLOCK_OUT_URL,
    COMPLETE_URL,
    add_entry,
    fresh,
    make_part,
    make_routing,
    op_audit_rows,
    set_op,
)
from tests.api.test_operation_short_count import _op, _wo

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"
OFFICE_REDUCE_URL = "/api/v1/work-orders/operations/{op}/reduce-production"
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


def _token_headers(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "review probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _clean(db: Session) -> None:
    """A refusal leaves nothing pending in the shared session (looked at BEFORE a re-read)."""
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified and not db.new, f"pending changes in the session: {modified} {db.new}"


def _op_tuple(db: Session, op_id: int):
    row = fresh(db, WorkOrderOperation, op_id)
    return (
        row.status,
        float(row.quantity_complete or 0),
        float(row.quantity_scrapped or 0),
        row.quantity_short_declared,
        float(row.component_quantity or 0),
    )


def _fg_receipts(db: Session, wo: WorkOrder) -> list[float]:
    return [
        float(row.quantity)
        for row in db.query(InventoryTransaction).filter(
            InventoryTransaction.company_id == wo.company_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
        )
    ]


# --------------------------------------------------------------------------- #
# BLOCKER: a routing made only of component operations
# --------------------------------------------------------------------------- #
class TestComponentOnlyHeader:
    def test_unit_sequential_component_only_books_the_worst_chain(self):
        wo = _wo()
        brackets = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        plates = _op(20, good=10, status=DONE, component_part_id=8, component_quantity=10)
        assert finished_quantity_at_all_complete(wo, [brackets, plates], 0) == 9

    def test_unit_pooled_batch_shape_books_the_order_less_the_lost_line(self):
        """The Miratech batch shape: pooled, one shared sequence, a distinct component per op."""
        wo = _wo(ordered=8, sequential=False)
        line_a = _op(10, good=7, scrap=1, declared=1, status=DONE, component_part_id=7, component_quantity=8)
        line_b = _op(10, good=16, status=DONE, component_part_id=8, component_quantity=16)
        assert finished_quantity_at_all_complete(wo, [line_a, line_b], 0) == 7

    def test_force_complete_bounds_take_the_same_loss(self):
        wo = _wo()
        brackets = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        plates = _op(20, good=10, status=DONE, component_part_id=8, component_quantity=10)
        bounds = work_order_completion_bounds(wo, [brackets, plates], {}, {})
        assert (bounds.deliverable, bounds.physical_max, bounds.floor_loss, bounds.hard_loss) == (9, 9, 1, 1)

    def test_end_to_end_the_last_close_books_9_and_receives_9(self, client, db_session):
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        brackets, plates = make_part(db_session, part_type="purchased"), make_part(db_session, part_type="purchased")
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc1, wc2],
            statuses=[OPEN, OPEN],
            component_parts=[brackets, plates],
            component_quantities=[20, 10],
        )
        add_entry(db_session, ann, op10, produced=18)
        set_op(db_session, op10, quantity_complete=18)
        closed = client.post(
            COMPLETE_URL.format(op=op10.id),
            json={"quantity_scrapped": 2, "scrap_reason": "bent"},
            headers=user_headers(ann),
        )
        assert closed.status_code == 200, closed.text
        assert fresh(db_session, WorkOrderOperation, op10.id).quantity_short_declared == 2

        entry = add_entry(db_session, ann, op20, open_entry=True)
        out = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 10}, headers=user_headers(ann)
        )
        assert out.status_code == 200, out.text
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert wo_row.status == WorkOrderStatus.COMPLETE
        assert float(wo_row.quantity_complete) == 9, "never 0: the header is the order less the worst chain's loss"
        assert _fg_receipts(db_session, wo_row) == [9.0]

    def test_the_reconcile_twin_agrees(self, db_session):
        wc = make_work_center(db_session)
        brackets, plates = make_part(db_session, part_type="purchased"), make_part(db_session, part_type="purchased")
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc, wc],
            statuses=[DONE, DONE],
            component_parts=[brackets, plates],
            component_quantities=[20, 10],
        )
        set_op(db_session, op10, quantity_complete=18, quantity_scrapped=2, quantity_short_declared=2)
        set_op(db_session, op20, quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.IN_PROGRESS)
        reconcile_work_orders_from_completion_evidence(db_session, [wo], [])
        assert wo.status == WorkOrderStatus.COMPLETE
        assert float(wo.quantity_complete) == 9


# --------------------------------------------------------------------------- #
# The hard ceiling holds on every write
# --------------------------------------------------------------------------- #
@pytest.fixture
def op20_over_ceiling(db_session: Session):
    """Sequential 10: op10 closed 8 + 2 (declared 2), so op20's ceiling is 8 -- but op20's
    stored good is 10 (a legacy / out-of-band write the guards below now refuse)."""
    ann = make_user(db_session, first_name="Ann")
    wc1, wc2, wc3 = make_work_center(db_session), make_work_center(db_session), make_work_center(db_session)
    wo, (op10, op20, op30) = make_routing(db_session, work_centers=[wc1, wc2, wc3], statuses=[DONE, OPEN, PENDING])
    set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
    add_entry(db_session, ann, op20, produced=10)  # closed evidence AT the over-count, so a read would act on it
    set_op(db_session, op20, quantity_complete=10)
    return ann, wo, op10, op20, op30


class TestCeilingHoldsEverywhere:
    def test_the_floor_close_refuses_good_above_the_ceiling(self, client, db_session, op20_over_ceiling):
        ann, wo, _, op20, _ = op20_over_ceiling
        before = _op_tuple(db_session, op20.id)
        resp = client.post(COMPLETE_URL.format(op=op20.id), json={}, headers=user_headers(ann))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 20 records 10 good, but only 8 can have reached it")
        _clean(db_session)
        assert _op_tuple(db_session, op20.id) == before
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
        assert _fg_receipts(db_session, wo) == []

    def test_a_zero_piece_clock_out_still_closes_the_entry(self, client, db_session, op20_over_ceiling):
        ann, _, _, op20, _ = op20_over_ceiling
        entry = add_entry(db_session, ann, op20, open_entry=True)
        out = client.post(CLOCK_OUT_URL.format(entry=entry.id), json={}, headers=user_headers(ann))
        assert out.status_code == 200, out.text
        assert out.json()["operation_status"] == "in_progress", "never auto-completed above the ceiling"
        assert fresh(db_session, TimeEntry, entry.id).clock_out is not None

    def test_a_clock_out_adding_pieces_is_still_refused(self, client, db_session, op20_over_ceiling):
        ann, _, _, op20, _ = op20_over_ceiling
        entry = add_entry(db_session, ann, op20, open_entry=True)
        out = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 1}, headers=user_headers(ann)
        )
        assert out.status_code == 400, out.text
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None

    def test_the_reconcile_never_completes_above_the_ceiling(self, client, db_session, op20_over_ceiling, supervisor):
        _, wo, _, op20, _ = op20_over_ceiling
        assert client.get(WORK_ORDER_URL.format(wo=wo.id), headers=user_headers(supervisor)).status_code == 200
        assert fresh(db_session, WorkOrderOperation, op20.id).status == OPEN
        assert fresh(db_session, WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE

    def test_the_header_is_clamped_at_the_final_stage_ceiling(self):
        wo = _wo()
        ops = [
            _op(10, good=8, scrap=2, declared=2, status=DONE),
            _op(20, good=10, status=DONE),
        ]
        assert finished_quantity_at_all_complete(wo, ops, 10) == 8

    def test_a_parallel_final_stage_that_lost_units_binds(self):
        wo = _wo()
        ops = [
            _op(10, good=10, status=DONE),
            _op(20, good=8, scrap=2, declared=2, status=DONE),
            _op(20, good=10, status=DONE),
        ]
        assert finished_quantity_at_all_complete(wo, ops, 10) == 8


class TestOperationPutOnAnOpenOperation:
    def test_good_above_the_ceiling_is_refused(self, client, db_session, supervisor):
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        _, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        resp = client.put(
            OPERATION_PUT_URL.format(op=op20.id),
            json={"version": op20.version, "quantity_complete": 10},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert "exceeds the 8 units that can reach Op 20" in resp.json()["detail"]
        _clean(db_session)
        assert float(fresh(db_session, WorkOrderOperation, op20.id).quantity_complete) == 0

    def test_good_above_the_floor_labor_record_is_refused(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=5)
        set_op(db_session, op, quantity_complete=5)
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_complete": 7},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert "more than the 5 counted on the floor for Op 10" in resp.json()["detail"]
        assert "quantity_override_reason" in resp.json()["detail"]
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 5

    def test_an_operation_nobody_clocked_into_stays_editable(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_complete": 4},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text

    def test_an_api_token_may_not_edit_counts(self, client, db_session, supervisor, admin_headers):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        token = _token_headers(client, admin_headers, supervisor)
        refused = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_scrapped": 1, "scrap_reason": "agent"},
            headers=token,
        )
        assert refused.status_code == 403, refused.text
        # Owner decision 2026-11-07: a scrap rise gets the scrap text ...
        assert refused.json()["detail"] == API_TOKEN_SCRAP
        # ... and any other count change keeps the count-judgment text.
        good_edit = client.put(
            OPERATION_PUT_URL.format(op=op.id), json={"version": op.version, "quantity_complete": 4}, headers=token
        )
        assert good_edit.status_code == 403, good_edit.text
        assert good_edit.json()["detail"] == API_TOKEN_COUNT_JUDGMENT
        renamed = client.put(
            OPERATION_PUT_URL.format(op=op.id), json={"version": op.version, "name": "Renamed"}, headers=token
        )
        assert renamed.status_code == 200, "a non-count edit stays open to a token"


class TestReCloseUpstreamCannotStrandALaterStage:
    def test_a_short_close_refuses_while_the_next_stage_holds_more_good(self, client, db_session):
        """P10: op10 reopened at 8; op20 (IN_PROGRESS) already holds 10. Closing op10 2 short
        would leave op20 over its new ceiling of 8 -- refused, nothing written."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        _, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[OPEN, OPEN])
        add_entry(db_session, ann, op10, produced=8)
        set_op(db_session, op10, quantity_complete=8)
        add_entry(db_session, ann, op20, produced=10)
        set_op(db_session, op20, quantity_complete=10)
        before = _op_tuple(db_session, op10.id)
        resp = client.post(
            COMPLETE_URL.format(op=op10.id),
            json={"quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=user_headers(ann),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 20 already records 10 good, more than the 8")
        _clean(db_session)
        assert _op_tuple(db_session, op10.id) == before
        assert [r for r in op_audit_rows(db_session, op10.id) if r.action == "COMPLETE_OPERATION"] == []


# --------------------------------------------------------------------------- #
# Soft feed is soft
# --------------------------------------------------------------------------- #
class TestSoftFeed:
    def test_a_clock_out_at_the_feed_reduced_count_does_not_auto_complete(self, client, db_session):
        """P2: brackets lost 2 (1 assembly). The weld received 9 but its ceiling is 10:
        a clock-out of 9 leaves it open (replenished brackets may still come); an explicit
        Complete closes it at 9 with nothing more to account for."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        brackets = make_part(db_session, part_type="purchased")
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc1, wc2],
            statuses=[DONE, OPEN],
            component_parts=[brackets, None],
            component_quantities=[20, None],
        )
        set_op(db_session, op10, quantity_complete=18, quantity_scrapped=2, quantity_short_declared=2)
        entry = add_entry(db_session, ann, op20, open_entry=True)
        out = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 9}, headers=user_headers(ann)
        )
        assert out.status_code == 200, out.text
        assert out.json()["operation_status"] == "in_progress"
        assert fresh(db_session, WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE

        closed = client.post(COMPLETE_URL.format(op=op20.id), json={}, headers=user_headers(ann))
        assert closed.status_code == 200, closed.text
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert (wo_row.status, float(wo_row.quantity_complete)) == (WorkOrderStatus.COMPLETE, 9)

    def test_a_replenished_stage_stops_carrying_the_feed_shortfall(self):
        wo = _wo()
        brackets = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        weld = _op(20, good=10, status=DONE)  # replenished: all 10 built
        paint = _op(30)
        acct = operation_accounting(wo, paint, [brackets, weld, paint])
        assert (acct.ceiling, acct.received) == (10, 10)

    def test_a_reconcile_waits_for_the_hard_ceiling_too(self, db_session):
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        brackets = make_part(db_session, part_type="purchased")
        wo, (op10, op20) = make_routing(
            db_session,
            work_centers=[wc1, wc2],
            statuses=[DONE, OPEN],
            component_parts=[brackets, None],
            component_quantities=[20, None],
        )
        set_op(db_session, op10, quantity_complete=18, quantity_scrapped=2, quantity_short_declared=2)
        add_entry(db_session, ann, op20, produced=9)
        set_op(db_session, op20, quantity_complete=9)
        reconcile_work_orders_from_completion_evidence(db_session, [wo], [])
        assert op20.status == OPEN


# --------------------------------------------------------------------------- #
# Changing a declaration is judged by its effect on the other operations
# --------------------------------------------------------------------------- #
def _reduce(client, op, user, *, delta, reopen=False):
    return client.post(
        OFFICE_REDUCE_URL.format(op=op.id),
        json={"quantity_delta": delta, "reason": "over-count", "reopen_operation": reopen},
        headers=user_headers(user),
    )


class TestReopenAcrossChains:
    def test_reopening_a_component_stage_refuses_while_the_fed_assembly_is_complete(
        self, client, db_session, supervisor
    ):
        """Compliance #2: C10's declaration fed M20 (closed at 8 of the 8 that reached it).
        Reopening C10 would clear that feed and leave M20 a completed operation 2 short."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        brackets = make_part(db_session, part_type="purchased")
        _, (c10, m20) = make_routing(
            db_session,
            work_centers=[wc1, wc2],
            statuses=[DONE, DONE],
            component_parts=[brackets, None],
            component_quantities=[20, None],
        )
        add_entry(db_session, ann, c10, produced=16)
        set_op(db_session, c10, quantity_complete=16, quantity_scrapped=4, quantity_short_declared=4)
        set_op(db_session, m20, quantity_complete=8)
        before = (_op_tuple(db_session, c10.id), _op_tuple(db_session, m20.id))

        resp = _reduce(client, c10, supervisor, delta=2, reopen=True)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "Op 20 is already complete with these units and would no longer add up; correct Op 20 first."
        )
        _clean(db_session)
        assert (_op_tuple(db_session, c10.id), _op_tuple(db_session, m20.id)) == before


class TestCorrectionReStampsTheDeclaration:
    @pytest.fixture
    def covered_by_scrap(self, db_session: Session):
        """Sequential 10: op10 COMPLETE at 10 good + 2 scrap (replacement runs, nothing
        declared); op20 open."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, READY])
        add_entry(db_session, ann, op10, produced=10, scrapped=2)
        set_op(db_session, op10, quantity_complete=10, quantity_scrapped=2)
        return ann, wo, op10, op20

    def test_the_lowered_count_is_declared_and_credits_the_next_stage(
        self, client, db_session, supervisor, covered_by_scrap
    ):
        ann, wo, op10, op20 = covered_by_scrap
        resp = _reduce(client, op10, supervisor, delta=2)
        assert resp.status_code == 200, resp.text
        assert _op_tuple(db_session, op10.id)[:4] == (DONE, 8, 2, 2)
        (row,) = [
            r
            for r in op_audit_rows(db_session, op10.id)
            if (r.extra_data or {}).get("transition") == "short_redeclared_by_correction"
        ]
        assert (row.old_values, row.new_values) == ({"quantity_short_declared": None}, {"quantity_short_declared": 2})

        # op20 now receives 8 and closes at the 8 that physically arrived -- no phantom scrap.
        add_entry(db_session, ann, op20, produced=8)
        set_op(db_session, op20, quantity_complete=8, status=OPEN)
        closed = client.post(COMPLETE_URL.format(op=op20.id), json={}, headers=user_headers(ann))
        assert closed.status_code == 200, closed.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_complete) == 8

    def test_refused_when_the_next_stage_already_holds_more(self, client, db_session, supervisor, covered_by_scrap):
        ann, _, op10, op20 = covered_by_scrap
        set_op(db_session, op20, quantity_complete=10, status=DONE)
        before = _op_tuple(db_session, op10.id)
        resp = _reduce(client, op10, supervisor, delta=2)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 20 already records 10 good, more than the 8")
        _clean(db_session)
        assert _op_tuple(db_session, op10.id) == before

    def test_not_saved_without_its_audit_row(self, client, db_session, supervisor, covered_by_scrap, monkeypatch):
        _, _, op10, _ = covered_by_scrap
        before = _op_tuple(db_session, op10.id)
        real_log = AuditService.log

        def _fail_redeclare(self, *args, **kwargs):
            if (kwargs.get("extra_data") or {}).get("transition") == "short_redeclared_by_correction":
                return None
            return real_log(self, *args, **kwargs)

        monkeypatch.setattr(AuditService, "log", _fail_redeclare)
        resp = _reduce(client, op10, supervisor, delta=2)
        assert resp.status_code == 503, resp.text
        assert _op_tuple(db_session, op10.id) == before


class TestSequencingFlip:
    def _put(self, client, wo, supervisor, sequential):
        return client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "sequential_operations": sequential},
            headers=user_headers(supervisor),
        )

    def test_turning_it_off_after_a_declared_short_is_refused(self, client, db_session, supervisor):
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, _) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = self._put(client, wo, supervisor, False)
        assert resp.status_code == 409, resp.text
        assert "cannot switch this work order off sequential operations" in resp.json()["detail"]
        assert fresh(db_session, WorkOrder, wo.id).sequential_operations is True

    def test_turning_it_on_credits_a_stage_that_has_not_closed(self, client, db_session, supervisor):
        """The pooled remedy: op10 closed 2 short, op20 (another work center) was refused for
        2 it never received; a supervisor turns sequencing on and op20 is credited."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], sequential=False, statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        add_entry(db_session, ann, op20, produced=8)
        set_op(db_session, op20, quantity_complete=8)

        refused = client.post(COMPLETE_URL.format(op=op20.id), json={}, headers=user_headers(ann))
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "Do not report pieces you never received as scrap" in detail
        assert "turn on Sequential operations" in detail

        wo = fresh(db_session, WorkOrder, wo.id)
        flipped = self._put(client, wo, supervisor, True)
        assert flipped.status_code == 200, flipped.text
        closed = client.post(COMPLETE_URL.format(op=op20.id), json={}, headers=user_headers(ann))
        assert closed.status_code == 200, closed.text
        assert float(fresh(db_session, WorkOrderOperation, op20.id).quantity_scrapped or 0) == 0

    def test_turning_it_on_after_a_later_stage_closed_is_refused(self, client, db_session, supervisor):
        wc1, wc2, wc3 = make_work_center(db_session), make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20, _) = make_routing(
            db_session, work_centers=[wc1, wc2, wc3], sequential=False, statuses=[DONE, DONE, OPEN]
        )
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        set_op(db_session, op20, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = self._put(client, wo, supervisor, True)
        assert resp.status_code == 409, resp.text
        assert "Op 20 has already completed under the current setting" in resp.json()["detail"]
        assert fresh(db_session, WorkOrder, wo.id).sequential_operations is False

    def test_a_flip_with_nothing_declared_is_unchanged(self, client, db_session, supervisor):
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, OPEN])
        wo = fresh(db_session, WorkOrder, wo.id)
        assert self._put(client, wo, supervisor, False).status_code == 200


class TestPooledFloorIsToldNotToScrapTwice:
    def test_the_read_models_carry_the_uncredited_figure(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], sequential=False, statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        add_entry(db_session, ann, op20, produced=8)
        body = client.get(WORK_ORDER_URL.format(wo=wo.id), headers=user_headers(supervisor)).json()
        ops = {op["id"]: op for op in body["operations"]}
        assert ops[op20.id]["upstream_short_uncredited"] == 2
        assert ops[op20.id]["upstream_quantity_short"] == 0, "still no credit -- a warning, not accounting"
        assert ops[op20.id]["has_labor_evidence"] is True
        assert ops[op10.id]["upstream_short_uncredited"] == 0

        floor = client.get(
            "/api/v1/shop-floor/operations", params={"work_center_id": wc2.id}, headers=user_headers(supervisor)
        )
        assert floor.status_code == 200, floor.text
        rows = floor.json()
        rows = rows.get("operations", rows) if isinstance(rows, dict) else rows
        (row,) = [r for r in rows if r["id"] == op20.id]
        assert row["upstream_short_uncredited"] == 2


# --------------------------------------------------------------------------- #
# quantity_ordered
# --------------------------------------------------------------------------- #
class TestQuantityOrdered:
    def test_a_raise_under_a_completed_stage_is_refused(self, client, db_session, supervisor):
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, _) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, READY])
        set_op(db_session, op10, quantity_complete=10)
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_ordered": 12},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 10 is already complete at the order of 10")
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_ordered) == 10

    def test_a_raise_before_any_stage_completed_is_allowed(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_ordered": 12},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text

    def test_restated_whole_order_targets_move_with_the_order(self, client, db_session, supervisor):
        """P26: the wizard persists the assembly's own operations with component_quantity =
        the order. Lowering the order for never-started pieces must move them too, or they
        keep demanding the old count and can never close."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(
            db_session, work_centers=[wc1, wc2], statuses=[OPEN, PENDING], component_quantities=[10, 10]
        )
        add_entry(db_session, ann, op10, produced=8)
        set_op(db_session, op10, quantity_complete=8)
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_ordered": 8},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert _op_tuple(db_session, op10.id)[4] == 8
        assert _op_tuple(db_session, op20.id)[4] == 8
        moved = [
            r
            for r in op_audit_rows(db_session, op10.id)
            if (r.extra_data or {}).get("transition") == "work_order_quantity_changed"
        ]
        assert len(moved) == 1

        closed = client.post(COMPLETE_URL.format(op=op10.id), json={}, headers=user_headers(ann))
        assert closed.status_code == 200, closed.text

    def test_an_api_token_may_not_lower_the_order_once_work_is_recorded(
        self, client, db_session, supervisor, admin_headers
    ):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        wo = fresh(db_session, WorkOrder, wo.id)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_ordered": 8},
            headers=_token_headers(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_COUNT_JUDGMENT


# --------------------------------------------------------------------------- #
# Force-complete
# --------------------------------------------------------------------------- #
class TestForceComplete:
    def test_the_token_refusal_comes_before_the_scrap_refusal(self, client, db_session, supervisor, admin_headers):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8},
            headers=_token_headers(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE

    def test_a_token_may_not_book_above_what_the_floor_recorded(self, client, db_session, supervisor, admin_headers):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_override_reason": "agent says so"},
            headers=_token_headers(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_COUNT_JUDGMENT
        assert fresh(db_session, WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE

    def test_quantity_plus_the_floor_loss_may_not_exceed_the_order(self, client, db_session, supervisor):
        """P7: op10 declared 2; op20 open at 6 good + 2 scrap. Even with an override reason,
        8 cannot be finished -- 4 of the 10 were already reported lost."""
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        add_entry(db_session, ann, op20, produced=6, scrapped=2)
        set_op(db_session, op20, quantity_complete=6, quantity_scrapped=2)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_override_reason": "finished off the books"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == (
            "quantity_complete (8) exceeds the 6 that can still be delivered; 4 were reported scrapped short"
        )
        _clean(db_session)
        assert fresh(db_session, WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE

        ok = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id), params={"quantity_complete": 6}, headers=user_headers(supervisor)
        )
        assert ok.status_code == 200, ok.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_complete) == 6

    def test_a_laser_pool_override_cannot_book_the_lost_runs(self):
        wo = _wo(ordered=20, laser=True)
        nest1 = _op(10, good=8, scrap=2, declared=2, status=DONE, component_quantity=10)
        nest2 = _op(20, component_quantity=10)
        bounds = work_order_completion_bounds(wo, [nest1, nest2], {nest1.id: 8}, {nest1.id: True})
        assert bounds.hard_loss == 2
        assert 20 + bounds.hard_loss > 20, "the force-complete refuses 20 with or without a reason"


# --------------------------------------------------------------------------- #
# Office close
# --------------------------------------------------------------------------- #
class TestOfficeClose:
    def test_a_scrap_rise_against_a_stale_good_count_is_refused(self, client, db_session, supervisor):
        """P4: the desk entered 8 good + 2 scrap; Bob has since reported 10. Booking the scrap
        would close 10 good + 2 scrap on a 10-piece operation."""
        bob = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, bob, op, produced=10)
        set_op(db_session, op, quantity_complete=10)
        before = _op_tuple(db_session, op.id)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 10 now records 10 good, more than the 8 entered")
        _clean(db_session)
        assert _op_tuple(db_session, op.id) == before

    def test_an_approved_open_entry_blocks_an_acknowledged_short_close(self, client, db_session, supervisor):
        alice = make_user(db_session, first_name="Alice", last_name="Ames")
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, alice, op, produced=8)
        open_entry = add_entry(db_session, alice, op, open_entry=True, approved=True)
        set_op(db_session, op, quantity_complete=8)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={
                "quantity_complete": 8,
                "quantity_scrapped": 2,
                "scrap_reason": "crack",
                "acknowledge_open_entries": True,
            },
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Alice Ames has an APPROVED time entry still open on Op 10")
        assert fresh(db_session, TimeEntry, open_entry.id).clock_out is None

    def test_a_token_may_not_assert_above_the_floor_count(self, client, db_session, supervisor, admin_headers):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 10, "quantity_override_reason": "agent"},
            headers=_token_headers(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_COUNT_JUDGMENT

    def test_the_read_model_names_the_floor_record_the_override_is_judged_against(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[OPEN, PENDING])
        add_entry(db_session, ann, op10, produced=6)
        body = client.get(WORK_ORDER_URL.format(wo=wo.id), headers=user_headers(supervisor)).json()
        ops = {op["id"]: op for op in body["operations"]}
        assert (ops[op10.id]["has_labor_evidence"], ops[op10.id]["quantity_recorded_good"]) == (True, 6)
        assert (ops[op20.id]["has_labor_evidence"], ops[op20.id]["quantity_recorded_good"]) == (False, 0)


# --------------------------------------------------------------------------- #
# Tenant scope of the evidence the floor close books
# --------------------------------------------------------------------------- #
def test_the_produced_evidence_sum_ignores_a_foreign_company_entry(db_session):
    ann = make_user(db_session)
    foreign = make_user(db_session, company_id=COMPANY_B)
    wc = make_work_center(db_session)
    _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
    add_entry(db_session, ann, op, produced=3)
    add_entry(db_session, foreign, op, produced=5)  # mis-tenanted: company B on company A's op id
    assert _operation_produced_evidence(db_session, op) == 3


# --------------------------------------------------------------------------- #
# The frontend's follow-up buttons key on this wording -- pin it
# --------------------------------------------------------------------------- #
def test_the_refusal_wording_the_office_modal_detects_is_stable():
    """``frontend/src/utils/officeCompletion.ts`` offers its follow-up buttons ("Clock them out
    and close", "Reopen Op N and correct", the override-reason field) by matching these
    substrings of the server's refusals. Rewording one here without updating that file would
    make the button silently disappear -- this pin is the tripwire."""
    op = WorkOrderOperation(id=1, sequence=10, operation_number="10")
    assert "Resubmit with acknowledge_open_entries" in others_clocked_in_message(op, ["Bob"], office=True)
    assert "Resubmit with reopen_operation" in reduce_would_leave_short_message(op, 2, 2)
    assert "quantity_override_reason" in override_reason_required_message(op, 8)


def test_what_if_accounting_never_dirties_the_session(db_session):
    from app.services.work_order_state_service import accounting_change_strands

    wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
    wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[OPEN, OPEN])
    set_op(db_session, op20, quantity_complete=10)
    strand = accounting_change_strands(
        wo,
        wo.operations,
        changed=op10,
        status=DONE,
        quantity_short_declared=2,
        quantity_complete=8,
        quantity_scrapped=2,
    )
    assert strand is not None and strand.operation.id == op20.id and strand.reason == "over_ceiling"
    assert (strand.before.ceiling, strand.after.ceiling) == (10, 8)
    _clean(db_session)
    assert op10.status == OPEN and op10.quantity_short_declared is None
    assert operation_accounting(wo, op20).ceiling == 10, "the real rows were never touched"
