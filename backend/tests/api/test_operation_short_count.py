"""Short count at operation completion (owner ruling 1, 2026-11-05) -- accounting + floor close.

The ruling: a short count at completion is reported as scrap with a required reason, or the
operation stays open -- never short-closed at the counted quantity.

1. UNIT -- the one accounting implementation in ``work_order_state_service``:
   ``OperationAccounting``, ``operation_chain_key`` / ``chain_credit_enabled``,
   ``stage_accounting`` (declared shorts along chains, sequential-only main-chain credit,
   soft component feed), ``finished_quantity_at_all_complete``,
   ``work_order_completion_bounds`` and ``upstream_figures_by_operation``.
2. FLOOR ``POST /shop-floor/operations/{id}/complete`` -- a pure CLOSE verb: every refusal
   writes nothing (operation, scrap, open entries, audit, ledger); a success closes at the
   recorded good with any declared scrap, persists ``quantity_short_declared``, credits no
   one's entry, and writes the completion row with REQUIRED evidence when it closed short.

Clock-out, /production and the read-time reconcile live in
``test_operation_short_count_clock_out.py``.
"""

import math

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.inventory import InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.process_sheet import ProcessSheet, WOOperationStep
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_SHORT_CLOSE,
    MSG_NO_LABOR_RECORDED,
    MSG_WORK_ORDER_ON_HOLD,
    complete_blockers,
    exceeds_recorded_message,
    operation_label,
    others_clocked_in_message,
    scrap_overdeclare_message,
    short_count_message,
)
from app.services.wallboard_service import operator_display_name
from app.services.work_order_state_service import (
    MAIN_CHAIN_KEY,
    OperationAccounting,
    StageFigures,
    chain_credit_enabled,
    finished_quantity_at_all_complete,
    operation_accounting,
    operation_chain_key,
    record_short_declaration,
    stage_accounting,
    sync_work_order_quantity_complete,
    upstream_figures_by_operation,
    work_order_completion_bounds,
)
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, make_user, make_work_center, user_headers
from tests.api.short_count_helpers import (
    COMPLETE_URL,
    add_entry,
    fresh,
    ledger_rows,
    make_routing,
    make_scrap_code,
    op_audit_rows,
    set_op,
    tie_material,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API_TOKENS_URL = "/api/v1/api-tokens/"


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


SCRAP_REASON_REQUIRED = "scrap_reason or scrap_reason_code_id is required when quantity_scrapped is greater than 0"


# --------------------------------------------------------------------------- #
# Transient (no DB) builders for the pure accounting functions
# --------------------------------------------------------------------------- #
def _wo(ordered: float = 10, *, sequential: bool = True, laser: bool = False) -> WorkOrder:
    return WorkOrder(
        id=1,
        quantity_ordered=ordered,
        sequential_operations=sequential,
        work_order_type=WorkOrderType.LASER_CUTTING.value if laser else "standard",
    )


_op_ids = {"n": 0}


def _op(
    sequence: int,
    *,
    good: float = 0,
    scrap: float = 0,
    declared=None,
    status: OperationStatus = OperationStatus.PENDING,
    component_part_id=None,
    component_quantity=None,
    work_center_id: int = 1,
) -> WorkOrderOperation:
    _op_ids["n"] += 1
    return WorkOrderOperation(
        id=_op_ids["n"],
        sequence=sequence,
        operation_number=str(sequence),
        status=status,
        quantity_complete=good,
        quantity_scrapped=scrap,
        quantity_short_declared=declared,
        component_part_id=component_part_id,
        component_quantity=component_quantity,
        work_center_id=work_center_id,
    )


DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS


def _acct(wo: WorkOrder, ops: list, op) -> OperationAccounting:
    return operation_accounting(wo, op, ops)


# --------------------------------------------------------------------------- #
# 1. UNIT -- OperationAccounting
# --------------------------------------------------------------------------- #
class TestOperationAccountingValueObject:
    @pytest.mark.parametrize(
        "target, good, scrapped, upstream, feed, received, unaccounted, closing_short, full, accounted",
        [
            (10, 10, 0, 0, 0, 10, 0, 0, True, True),
            (10, 8, 0, 0, 0, 10, 2, 2, False, False),
            (10, 8, 2, 0, 0, 10, 0, 2, False, True),
            (10, 10, 2, 0, 0, 10, 0, 0, True, True),  # scrap-then-replace: full count, no short
            (10, 8, 0, 2, 0, 8, 0, 0, True, True),  # upstream declared 2 -> 8 is a full count
            (10, 9, 0, 0, 1, 9, 0, 0, True, True),  # soft feed: 9 reached it
            (10, 10, 0, 0, 1, 9, 0, 0, True, True),  # replenished components: good may exceed received
            (0, 0, 0, 0, 0, 0, 0, 0, False, False),  # no target: never full, never accounted
            (10, 0, 0, 10, 0, 0, 0, 0, False, True),  # nothing reached it
        ],
    )
    def test_figures(
        self, target, good, scrapped, upstream, feed, received, unaccounted, closing_short, full, accounted
    ):
        acct = OperationAccounting(
            target=target, good=good, scrapped=scrapped, chain_upstream=upstream, feed_short=feed
        )
        assert acct.ceiling == max(0, target - upstream)
        assert acct.received == received
        assert acct.upstream_short == target - received
        assert acct.unaccounted == unaccounted
        assert acct.closing_short == closing_short
        assert acct.is_full_count is full
        assert acct.is_accounted is accounted

    def test_record_short_declaration_writes_only_a_real_short(self):
        op = _op(10, good=8, scrap=2)
        record_short_declaration(op, OperationAccounting(target=10, good=8, scrapped=2))
        assert op.quantity_short_declared == 2
        record_short_declaration(op, OperationAccounting(target=10, good=10, scrapped=2))
        assert op.quantity_short_declared is None


# --------------------------------------------------------------------------- #
# 1. UNIT -- chains and stage accounting
# --------------------------------------------------------------------------- #
class TestChainsAndStageAccounting:
    def test_sequential_declared_short_reaches_the_next_stage(self):
        wo = _wo()
        op10 = _op(10, good=8, scrap=2, declared=2, status=DONE)
        op20 = _op(20)
        op30 = _op(30)
        ops = [op10, op20, op30]
        acct20 = _acct(wo, ops, op20)
        assert (acct20.ceiling, acct20.received, acct20.upstream_short) == (8, 8, 2)
        assert _acct(wo, ops, op30).received == 8

    def test_declarations_accumulate_along_the_chain(self):
        wo = _wo()
        ops = [
            _op(10, good=9, scrap=1, declared=1, status=DONE),
            _op(20, good=8, scrap=1, declared=1, status=DONE),
            _op(30),
        ]
        assert _acct(wo, ops, ops[2]).chain_upstream == 2

    def test_replacement_over_start_declares_nothing_so_no_credit(self):
        wo = _wo()
        op10 = _op(10, good=10, scrap=2, status=DONE)
        record_short_declaration(op10, OperationAccounting(target=10, good=10, scrapped=2))
        ops = [op10, _op(20)]
        assert op10.quantity_short_declared is None
        assert _acct(wo, ops, ops[1]).chain_upstream == 0

    def test_a_legacy_short_close_with_no_declaration_gives_no_credit(self):
        wo = _wo()
        ops = [_op(10, good=8, status=DONE), _op(20)]
        assert _acct(wo, ops, ops[1]).received == 10

    def test_an_open_upstream_operation_gives_no_credit(self):
        wo = _wo()
        ops = [_op(10, good=8, scrap=2, declared=2, status=OPEN), _op(20)]
        assert _acct(wo, ops, ops[1]).received == 10

    def test_a_pooled_main_chain_gets_no_credit_even_across_work_centers(self):
        """A -> B -> A on a POOLED work order: same-target ops there are indistinguishable from
        line items, so crediting them would let a line close short with no scrap."""
        wo = _wo(sequential=False)
        ops = [
            _op(10, good=8, scrap=2, declared=2, status=DONE, work_center_id=1),
            _op(20, work_center_id=2),
            _op(30, work_center_id=1),
        ]
        assert chain_credit_enabled(wo, MAIN_CHAIN_KEY) is False
        assert _acct(wo, ops, ops[1]).received == 10
        assert _acct(wo, ops, ops[2]).received == 10
        assert stage_accounting(wo, ops) == {}

    def test_component_chain_is_credited_even_on_a_pooled_work_order(self):
        wo = _wo(sequential=False)
        laser = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        brake = _op(20, component_part_id=7, component_quantity=20)
        ops = [laser, brake]
        assert operation_chain_key(wo, brake) == ("component", 7)
        assert _acct(wo, ops, brake).ceiling == 18

    @pytest.mark.parametrize("lost, expected_received", [(2, 9), (1, 9), (4, 8), (0, 10)])
    def test_component_feed_reaches_the_assembly_softly(self, lost, expected_received):
        wo = _wo()
        bracket = _op(
            10,
            good=20 - lost,
            scrap=lost,
            declared=lost or None,
            status=DONE,
            component_part_id=7,
            component_quantity=20,
        )
        weld = _op(20)
        acct = _acct(wo, [bracket, weld], weld)
        assert acct.received == expected_received
        assert acct.ceiling == 10, "feed credit is soft -- it never caps good"

    def test_component_feed_and_an_earlier_main_short_are_not_counted_twice(self):
        """Frames lost at op 10 and brackets lost at op 20 are different units: 9 frames and
        9 bracket sets still make 9 assemblies, not 8."""
        wo = _wo()
        frames = _op(10, good=9, scrap=1, declared=1, status=DONE)
        brackets = _op(20, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        weld = _op(30)
        acct = _acct(wo, [frames, brackets, weld], weld)
        assert (acct.ceiling, acct.received) == (9, 9)

    def test_feed_persists_past_a_full_count_assembly_stage(self):
        wo = _wo()
        brackets = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        weld = _op(20, good=9, status=DONE)  # full count of the 9 that reached it, nothing declared
        paint = _op(30)
        assert _acct(wo, [brackets, weld, paint], paint).received == 9

    def test_laser_pool_and_line_items_are_singletons(self):
        laser_wo = _wo(laser=True)
        nest = _op(10, good=1, scrap=1, declared=1, status=DONE, component_quantity=2)
        other = _op(20, component_quantity=2)
        assert operation_chain_key(laser_wo, other) is None
        assert _acct(laser_wo, [nest, other], other).received == 2

        line_wo = _wo(ordered=10, sequential=True)
        line_a = _op(10, good=3, scrap=1, declared=1, status=DONE, component_quantity=4)
        line_b = _op(20, component_quantity=6)
        assert operation_chain_key(line_wo, line_b) is None
        assert _acct(line_wo, [line_a, line_b], line_b).received == 6

    def test_no_double_count_on_a_same_work_center_sequence(self):
        """INT2: A, B, B, C on a SEQUENCED work order, op 20 closes 8 + 2."""
        wo = _wo()
        op10 = _op(10, good=10, status=DONE, work_center_id=1)
        op20 = _op(20, good=8, scrap=2, declared=2, status=DONE, work_center_id=2)
        op30 = _op(30, good=8, status=DONE, work_center_id=2)
        op40 = _op(40, good=8, status=DONE, work_center_id=3)
        ops = [op10, op20, op30, op40]
        assert _acct(wo, ops, op30).received == 8
        assert _acct(wo, ops, op40).received == 8
        assert finished_quantity_at_all_complete(wo, ops, 10) == 8

    def test_stage_figures_are_empty_without_a_credited_chain(self):
        wo = _wo()
        ops = [_op(10, good=10, status=DONE), _op(20)]
        assert stage_accounting(wo, ops) == {ops[0].id: StageFigures(), ops[1].id: StageFigures()}


# --------------------------------------------------------------------------- #
# 1. UNIT -- the all-complete header and the force-complete bounds
# --------------------------------------------------------------------------- #
class TestHeaderAndBounds:
    def test_no_declaration_snaps_to_ordered_and_clamps(self):
        wo = _wo()
        ops = [_op(10, good=8, status=DONE), _op(20, good=8, status=DONE)]
        assert finished_quantity_at_all_complete(wo, ops, 4) == 10
        assert finished_quantity_at_all_complete(wo, ops, 12) == 10, "clamped to the order"

    def test_nothing_ordered_keeps_the_header(self):
        assert finished_quantity_at_all_complete(_wo(ordered=0), [_op(10, status=DONE)], 3) == 3

    def test_declaration_with_a_main_chain_books_the_last_stage(self):
        wo = _wo()
        ops = [_op(10, good=10, status=DONE), _op(20, good=8, scrap=2, declared=2, status=DONE)]
        assert finished_quantity_at_all_complete(wo, ops, 10) == 8

    def test_replenished_component_assembly_still_books_its_good(self):
        wo = _wo()
        brackets = _op(10, good=18, scrap=2, declared=2, status=DONE, component_part_id=7, component_quantity=20)
        weld = _op(20, good=10, status=DONE)
        assert finished_quantity_at_all_complete(wo, [brackets, weld], 0) == 10

    def test_line_items_summing_to_the_order_book_per_line(self):
        wo = _wo(ordered=10, sequential=False)
        ops = [
            _op(10, good=3, scrap=1, declared=1, status=DONE, component_quantity=4),
            _op(10, good=6, status=DONE, component_quantity=6),
        ]
        assert finished_quantity_at_all_complete(wo, ops, 0) == 9

    def test_line_items_not_summing_to_the_order_do_not_snap(self):
        wo = _wo(ordered=10, sequential=False)
        ops = [
            _op(10, good=3, scrap=1, declared=1, status=DONE, component_quantity=4),
            _op(10, good=5, status=DONE, component_quantity=5),
        ]
        assert finished_quantity_at_all_complete(wo, ops, 7) == 7

    def test_sync_and_the_reconcile_twin_agree(self, db_session: Session):
        """The finalizer's all-complete branch and the read reconcile take the same helper."""
        from app.services.work_order_state_service import reconcile_work_orders_from_completion_evidence

        wc = make_work_center(db_session)
        wo_a, (a10, a20) = make_routing(db_session, work_centers=[wc, wc], statuses=[DONE, DONE])
        set_op(db_session, a10, quantity_complete=10)
        set_op(db_session, a20, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        sync_work_order_quantity_complete(wo_a, a20, all_operations_complete=True)
        assert wo_a.quantity_complete == 8
        db_session.rollback()

        wo_b, (b10, b20) = make_routing(db_session, work_centers=[wc, wc], statuses=[DONE, DONE])
        set_op(db_session, b10, quantity_complete=10)
        set_op(db_session, b20, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        set_op(db_session, wo_b, quantity_complete=10, status=WorkOrderStatus.IN_PROGRESS)
        reconcile_work_orders_from_completion_evidence(db_session, [wo_b], [])
        assert wo_b.status == WorkOrderStatus.COMPLETE
        assert wo_b.quantity_complete == 8

    def test_bounds_main_chain(self):
        wo = _wo()
        op10 = _op(10, good=10, status=DONE)
        op20 = _op(20, good=8, scrap=2, status=OPEN)
        ops = [op10, op20]
        bounds = work_order_completion_bounds(wo, ops, {op10.id: 10, op20.id: 8}, {op10.id: True, op20.id: True})
        assert bounds.deliverable == 8
        assert bounds.binding_operation_id == op20.id
        assert bounds.physical_max == 10
        assert bounds.floor_loss == 2, "open-op scrap counts, capped at its closing short"

    def test_bounds_with_a_declared_upstream_short(self):
        wo = _wo()
        op10 = _op(10, good=8, scrap=2, declared=2, status=DONE)
        op20 = _op(20)
        bounds = work_order_completion_bounds(wo, [op10, op20], {op10.id: 8, op20.id: 0}, {op10.id: True})
        assert bounds.deliverable == 8, "an unreported stage caps at what reached it"
        assert bounds.physical_max == 8
        assert bounds.floor_loss == 2

    def test_bounds_on_a_pooled_main_chain_take_the_worst_stage_not_the_sum(self):
        wo = _wo(sequential=False)
        op10 = _op(10, good=8, scrap=2, declared=2, status=DONE)
        op20 = _op(20, good=8, scrap=2, declared=2, status=DONE)
        bounds = work_order_completion_bounds(
            wo, [op10, op20], {op10.id: 8, op20.id: 8}, {op10.id: True, op20.id: True}
        )
        assert bounds.floor_loss == 2
        assert bounds.physical_max == 8

    def test_bounds_laser_pool_sums_nests(self):
        wo = _wo(ordered=5, laser=True)
        nest1 = _op(10, good=2, status=DONE, component_quantity=2)
        nest2 = _op(20, good=1, scrap=1, status=OPEN, component_quantity=3)
        bounds = work_order_completion_bounds(
            wo, [nest1, nest2], {nest1.id: 2, nest2.id: 1}, {nest1.id: True, nest2.id: True}
        )
        assert bounds.deliverable == 3
        assert bounds.floor_loss == 1

    def test_bounds_without_evidence_do_not_bind(self):
        wo = _wo()
        ops = [_op(10), _op(20)]
        bounds = work_order_completion_bounds(wo, ops, {}, {})
        assert (bounds.deliverable, bounds.physical_max, bounds.floor_loss) == (10, 10, 0)
        assert bounds.binding_operation_id is None


class TestUpstreamFiguresByOperation:
    def test_one_query_and_tenant_scoped(self, db_session: Session):
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc, wc], statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        # INT11: a FOREIGN-company row sharing this work_order_id must never contribute.
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        db_session.add(
            WorkOrderOperation(
                work_order_id=wo.id,
                work_center_id=wc_b.id,
                sequence=15,
                operation_number="15",
                name="Foreign",
                status=DONE,
                quantity_complete=1,
                quantity_short_declared=5,
                company_id=COMPANY_B,
            )
        )
        db_session.commit()
        db_session.expire_all()
        page = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op20.id).all()
        page[0].work_order  # the read models load it with the row

        statements: list[str] = []

        def _count(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(db_session.get_bind(), "before_cursor_execute", _count)
        try:
            figures = upstream_figures_by_operation(db_session, COMPANY_A, page)
        finally:
            event.remove(db_session.get_bind(), "before_cursor_execute", _count)

        assert len(statements) == 1, statements
        assert figures[op20.id].chain_upstream == 2
        assert figures[op20.id].received == 8


# --------------------------------------------------------------------------- #
# 2. FLOOR /complete -- refusals write nothing
# --------------------------------------------------------------------------- #
def _complete(client: TestClient, op, user_or_headers, **body):
    headers = user_or_headers if isinstance(user_or_headers, dict) else user_headers(user_or_headers)
    return client.post(COMPLETE_URL.format(op=op.id), json=body, headers=headers)


def _snapshot(db: Session, op_id: int):
    # A re-read starts with expire_all(), which DISCARDS an unflushed change a refusal left
    # in the shared session (the test client never rolls back) -- so look first. Session.dirty
    # is optimistic (any SET event); is_modified is the net change.
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified and not db.new, f"pending changes in the session: {modified} {db.new}"
    db.expire_all()
    op = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).one()
    entries = db.query(TimeEntry).filter(TimeEntry.operation_id == op_id).order_by(TimeEntry.id).all()
    return (
        op.status,
        float(op.quantity_complete or 0),
        float(op.quantity_scrapped or 0),
        op.scrap_reason,
        op.quantity_short_declared,
        [
            (e.id, e.clock_out is None, float(e.quantity_produced or 0), float(e.quantity_scrapped or 0))
            for e in entries
        ],
    )


@pytest.fixture
def eight_of_ten(db_session: Session):
    """One RELEASED operation, target 10, 8 good recorded on a closed entry."""
    ann = make_user(db_session, role=UserRole.OPERATOR, first_name="Ann", last_name="Smith")
    wc = make_work_center(db_session)
    wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
    add_entry(db_session, ann, op, produced=8)
    set_op(db_session, op, quantity_complete=8)
    return ann, wo, op


class TestFloorCloseRefusals:
    def _assert_nothing_written(self, db_session, op, before):
        assert not db_session.dirty and not db_session.new
        assert _snapshot(db_session, op.id) == before
        assert op_audit_rows(db_session, op.id) == []
        assert ledger_rows(db_session) == []
        # No completion / ready event either (the fixtures emit none; a refusal must not).
        assert db_session.query(OperationalEvent).filter(OperationalEvent.company_id == COMPANY_A).count() == 0

    def test_short_with_nothing_declared_is_refused_and_left_open(self, client, db_session, eight_of_ten):
        ann, wo, op = eight_of_ten
        before = _snapshot(db_session, op.id)
        resp = _complete(client, op, ann)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "Op 10: 2 of 10 not yet accounted for (8 good, 0 scrapped). Record finished pieces (clock out with "
            "your count, or Report production), report lost pieces as scrap with a reason, or leave the operation "
            "open. Pieces never started: ask a supervisor to lower the quantity."
        )
        self._assert_nothing_written(db_session, op, before)
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED

    def test_a_partial_scrap_that_still_leaves_units_open_is_refused(self, client, db_session, eight_of_ten):
        ann, _, op = eight_of_ten
        before = _snapshot(db_session, op.id)
        resp = _complete(client, op, ann, quantity_scrapped=1, scrap_reason="burr")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Op 10: 1 of 10 not yet accounted for (8 good, 1 scrapped).")
        self._assert_nothing_written(db_session, op, before)

    def test_asserting_more_than_was_recorded_is_refused(self, client, db_session, eight_of_ten):
        ann, _, op = eight_of_ten
        before = _snapshot(db_session, op.id)
        resp = _complete(client, op, ann, quantity_complete=10)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == exceeds_recorded_message(op, 10, 8)
        assert "exceeds the 8 good recorded on Op 10" in resp.json()["detail"]
        self._assert_nothing_written(db_session, op, before)

    def test_a_stale_scrap_declaration_is_refused(self, client, db_session, eight_of_ten):
        """INT5: the crew screen loaded at 8; Bob then reported the last 2 via /production."""
        ann, _, op = eight_of_ten
        bob = make_user(db_session, role=UserRole.OPERATOR, first_name="Bob", last_name="Jones")
        add_entry(db_session, bob, op, open_entry=True)
        report = client.post(
            f"/api/v1/shop-floor/operations/{op.id}/production",
            json={"quantity_complete_delta": 2},
            headers=user_headers(bob),
        )
        assert report.status_code == 200, report.text
        before = _snapshot(db_session, op.id)
        ledger_before = len(ledger_rows(db_session))
        audit_before = len(op_audit_rows(db_session, op.id))

        resp = _complete(client, op, ann, quantity_scrapped=2, scrap_reason="lost")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == scrap_overdeclare_message(op, 0, 2)
        assert _snapshot(db_session, op.id) == before
        assert len(ledger_rows(db_session)) == ledger_before
        assert len(op_audit_rows(db_session, op.id)) == audit_before

    def test_a_short_close_names_who_is_still_clocked_in(self, client, db_session, eight_of_ten):
        ann, _, op = eight_of_ten
        bob = make_user(db_session, role=UserRole.OPERATOR, first_name="Bob", last_name="Jones")
        bob_entry = add_entry(db_session, bob, op, open_entry=True)
        before = _snapshot(db_session, op.id)

        resp = _complete(client, op, ann, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == others_clocked_in_message(op, ["Bob J."])
        assert resp.json()["detail"].startswith("Bob J. is still clocked in to Op 10")
        self._assert_nothing_written(db_session, op, before)

        acked = _complete(client, op, ann, quantity_scrapped=2, scrap_reason="crack", acknowledge_open_entries=True)
        assert acked.status_code == 200, acked.text
        bob_row = fresh(db_session, TimeEntry, bob_entry.id)
        assert bob_row.clock_out is not None, "acknowledged: Bob is clocked out"
        assert float(bob_row.quantity_produced or 0) == 0, "and nothing is credited to him"
        assert [e["user_id"] for e in acked.json()["closed_time_entries"]] == [bob.id]
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        assert row.extra_data["acknowledged_open_entries"] == [bob_entry.id]

    def test_an_api_token_may_not_close_short_but_may_close_a_full_count(
        self, client, db_session, eight_of_ten, admin_headers
    ):
        ann, _, op = eight_of_ten
        bot = make_user(db_session, role=UserRole.OPERATOR)
        issued = client.post(API_TOKENS_URL, json={"user_id": bot.id, "label": "short probe"}, headers=admin_headers)
        assert issued.status_code == 201, issued.text
        token_headers = {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}
        before = _snapshot(db_session, op.id)

        resp = _complete(client, op, token_headers, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE
        self._assert_nothing_written(db_session, op, before)

        add_entry(db_session, ann, op, produced=2)
        full = _complete(client, op, token_headers)
        assert full.status_code == 200, full.text
        assert fresh(db_session, WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    def test_a_serialized_short_close_with_missing_step_records_is_refused_explicitly(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(
            db_session, work_centers=[wc], statuses=[OPEN], quantity_ordered=2, serial_numbers='["SN-1", "SN-2"]'
        )
        add_entry(db_session, ann, op, produced=1)
        set_op(db_session, op, quantity_complete=1)
        sheet = ProcessSheet(sheet_number="PS-SC-1", title="Torque", revision="A", status="released", company_id=1)
        db_session.add(sheet)
        db_session.flush()
        db_session.add(
            WOOperationStep(
                company_id=1,
                work_order_operation_id=op.id,
                source_sheet_id=sheet.id,
                source_sheet_revision="A",
                sequence=1,
                label="Torque verified",
                step_type="checkbox",
                is_required=True,
            )
        )
        db_session.commit()
        before = _snapshot(db_session, op.id)

        resp = _complete(client, op, ann, quantity_scrapped=1, scrap_reason="crack")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str), "not the generic STEPS_INCOMPLETE payload"
        assert detail.startswith("Op 10 is 1 short on a serialized work order")
        assert "SN-1, SN-2" in detail and "Never record steps for a part that was scrapped." in detail
        self._assert_nothing_written(db_session, op, before)

    def test_the_labor_gate_fires_before_the_short_check(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        resp = _complete(client, op, ann)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == MSG_NO_LABOR_RECORDED

    def test_input_validation(self, client, db_session, eight_of_ten):
        ann, _, op = eight_of_ten
        before = _snapshot(db_session, op.id)
        no_reason = _complete(client, op, ann, quantity_scrapped=2)
        assert no_reason.status_code == 422
        assert SCRAP_REASON_REQUIRED in no_reason.text
        assert _complete(client, op, ann, quantity_scrapped=2, scrap_reason="   ").status_code == 422
        assert _complete(client, op, ann, quantity_scrapped=-1).status_code == 400
        assert _complete(client, op, ann, quantity_scrapped=2, scrap_reason_code_id=999999).status_code == 404
        inactive = make_scrap_code(db_session, is_active=False)
        assert _complete(client, op, ann, quantity_scrapped=2, scrap_reason_code_id=inactive.id).status_code == 422
        extra = _complete(client, op, ann, quantity_scrapped_delta=2)
        assert extra.status_code == 422, "unknown keys are refused, not silently ignored"
        nan = client.post(
            COMPLETE_URL.format(op=op.id),
            content='{"quantity_scrapped": NaN, "scrap_reason": "x"}',
            headers={**user_headers(ann), "Content-Type": "application/json"},
        )
        assert nan.status_code in (400, 422), nan.text
        self._assert_nothing_written(db_session, op, before)

    def test_a_held_job_still_gates_a_ready_operation_first(self, client, db_session):
        """Combined pin: the gates' work-order start rung outranks every short-count check."""
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OperationStatus.READY])
        add_entry(db_session, ann, op, produced=0)
        wo_row = fresh(db_session, WorkOrder, op.work_order_id)
        set_op(db_session, wo_row, status=WorkOrderStatus.ON_HOLD)
        resp = _complete(client, op, ann, quantity_complete=99, quantity_scrapped=-5)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD


# --------------------------------------------------------------------------- #
# 2. FLOOR /complete -- successes and audit
# --------------------------------------------------------------------------- #
def _fg_receipts(db: Session, wo_id: int) -> list[InventoryTransaction]:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )


def _issues_for_op(db: Session, op_id: int) -> float:
    return sum(
        abs(float(row.quantity or 0))  # an ISSUE posts a negative quantity
        for row in db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == "work_order_operation",
            InventoryTransaction.reference_id == op_id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )


class TestFloorCloseSuccesses:
    def test_a_short_covered_by_scrap_closes_and_is_declared(self, client, db_session, eight_of_ten):
        ann, wo, op = eight_of_ten
        tie_material(db_session, wo, op)
        code = make_scrap_code(db_session)

        resp = _complete(client, op, ann, quantity_scrapped=2, scrap_reason_code_id=code.id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_fully_complete"] is True
        assert body["operation"]["quantity_complete"] == 8
        assert body["operation"]["quantity_scrapped"] == 2
        assert body["operation"]["quantity_short_declared"] == 2
        assert body["operation"]["upstream_quantity_short"] == 0

        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.status == OperationStatus.COMPLETE
        assert (op_row.quantity_complete, op_row.quantity_scrapped, op_row.quantity_short_declared) == (8, 2, 2)
        assert op_row.scrap_reason_code_id == code.id
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert wo_row.status == WorkOrderStatus.COMPLETE
        assert wo_row.quantity_complete == 8, "the header books what left the last stage, not the order"
        (receipt,) = _fg_receipts(db_session, wo.id)
        assert float(receipt.quantity) == 8
        assert _issues_for_op(db_session, op.id) == 10, "tied consumption follows good + scrap"

        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        assert row.old_values["quantity_scrapped"] == 0
        assert row.new_values["quantity_scrapped"] == 2
        assert row.new_values["quantity_short_declared"] == 2
        assert row.extra_data["closed_short"] is True
        assert row.extra_data["scrap_declared_at_close"] == 2
        assert row.extra_data["received"] == 10
        assert "Qty: 8/10, scrapped 2" in row.description

    def test_a_short_close_whose_audit_row_cannot_be_saved_writes_nothing(self, client, db_session, monkeypatch):
        """INT4 -- the kiosk shape: clock-out 8 good + 2 scrap, then /complete {}."""
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        tie_material(db_session, wo, op)
        add_entry(db_session, ann, op, produced=8, scrapped=2)
        set_op(db_session, op, quantity_complete=8, quantity_scrapped=2)

        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)
        resp = _complete(client, op, ann)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == "The completion audit could not be saved; nothing was changed. Please retry."
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.status == OperationStatus.IN_PROGRESS
        assert op_row.quantity_short_declared is None
        assert ledger_rows(db_session) == []
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.RELEASED

    def test_a_full_count_close_keeps_its_best_effort_audit(self, client, db_session, monkeypatch):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=10)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)
        resp = _complete(client, op, ann)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    def test_crew_entries_close_with_their_own_counts_and_nothing_credited(self, client, db_session):
        lead = make_user(db_session, role=UserRole.OPERATOR)
        alice = make_user(db_session, role=UserRole.OPERATOR)
        bob = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN], quantity_ordered=8)
        a = add_entry(db_session, alice, op, produced=6, open_entry=True)
        b = add_entry(db_session, bob, op, produced=2, open_entry=True)
        set_op(db_session, op, quantity_complete=8)

        resp = _complete(client, op, lead)
        assert resp.status_code == 200, resp.text
        assert sorted(e["user_id"] for e in resp.json()["closed_time_entries"]) == sorted([alice.id, bob.id])
        a_row, b_row = fresh(db_session, TimeEntry, a.id), fresh(db_session, TimeEntry, b.id)
        assert (a_row.quantity_produced, b_row.quantity_produced) == (6, 2)
        assert a_row.clock_out is not None and b_row.clock_out is not None

    @pytest.mark.parametrize("approved", [False, True])
    def test_scrap_lands_on_the_callers_unapproved_entry_only(self, client, db_session, approved):
        lead = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        entry = add_entry(db_session, lead, op, produced=8, open_entry=True, approved=approved)
        set_op(db_session, op, quantity_complete=8)

        resp = _complete(client, op, lead, quantity_scrapped=2, scrap_reason="crack")
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, TimeEntry, entry.id)
        (audit_row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        if approved:
            assert float(row.quantity_scrapped or 0) == 0, "approval is the immutability boundary"
            assert audit_row.extra_data["scrap_attributed_to_time_entry_id"] is None
        else:
            assert float(row.quantity_scrapped or 0) == 2
            assert row.scrap_reason == "crack"
            assert audit_row.extra_data["scrap_attributed_to_time_entry_id"] == entry.id
        assert fresh(db_session, WorkOrderOperation, op.id).quantity_scrapped == 2

    def test_a_downstream_stage_closes_at_what_reached_it(self, client, db_session):
        ann = make_user(db_session, role=UserRole.OPERATOR)
        wc1, wc2 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc1, wc2], statuses=[DONE, OPEN])
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        add_entry(db_session, ann, op20, produced=8)
        set_op(db_session, op20, quantity_complete=8)

        resp = _complete(client, op20, ann)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"]["upstream_quantity_short"] == 2
        op_row = fresh(db_session, WorkOrderOperation, op20.id)
        assert (op_row.status, op_row.quantity_scrapped, op_row.quantity_short_declared) == (DONE, 0, None)
        assert fresh(db_session, WorkOrder, wo.id).quantity_complete == 8

    def test_in_progress_work_on_a_held_job_follows_the_short_rules(self, client, db_session, eight_of_ten):
        """Combined pin (b): gates keeps IN_PROGRESS completable on a held job; ruling 1 still applies.

        The gates' work-order start rung covers a READY operation only (completing it would
        start it), so work already running when the job was held reaches the short-count
        rules unchanged: refused 409 with nothing written while 2 are unaccounted, then
        closed 200 once they are declared as scrap with a reason -- under REQUIRED audit.
        """
        ann, wo, op = eight_of_ten
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=WorkOrderStatus.ON_HOLD)
        before = _snapshot(db_session, op.id)

        refused = _complete(client, op, ann)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == short_count_message(op, OperationAccounting(target=10, good=8, scrapped=0))
        assert refused.json()["detail"] != MSG_WORK_ORDER_ON_HOLD
        assert _snapshot(db_session, op.id) == before
        assert op_audit_rows(db_session, op.id) == []
        assert ledger_rows(db_session) == []

        ok = _complete(client, op, ann, quantity_scrapped=2, scrap_reason="crack")
        assert ok.status_code == 200, ok.text
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_complete, op_row.quantity_scrapped, op_row.scrap_reason) == (
            DONE,
            8,
            2,
            "crack",
        )
        assert op_row.quantity_short_declared == 2
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        assert row.extra_data["closed_short"] is True
        assert row.extra_data["scrap_declared_at_close"] == 2


class TestMessages:
    def test_labels_never_double_prefix(self):
        assert operation_label(_op(10)) == "Op 10"
        legacy = _op(10)
        legacy.operation_number = "Op 10"
        assert operation_label(legacy) == "Op 10"
        nest = _op(10)
        nest.operation_number = "Nest 3"
        assert operation_label(nest) == "Nest 3"
        optical = _op(10)
        optical.operation_number = "OPTICAL"
        assert operation_label(optical) == "Op OPTICAL"

    def test_short_message_names_upstream_loss(self):
        acct = OperationAccounting(target=10, good=6, scrapped=0, chain_upstream=2)
        message = short_count_message(_op(20), acct)
        assert message.startswith("Op 20: 2 of 8 not yet accounted for (6 good, 0 scrapped).")
        assert message.endswith(" (2 never reached it — closed short upstream.)")

    def test_others_message_grammar(self):
        assert others_clocked_in_message(_op(10), ["Bob J.", "Cy D."]).startswith("Bob J. and Cy D. are still")
        assert others_clocked_in_message(_op(10), ["Bob J."], office=True).endswith(
            "pieces they have not reported will not be counted."
        )

    def test_quantities_print_without_float_noise(self):
        acct = OperationAccounting(target=10, good=8.000000001, scrapped=0)
        assert "(8 good, 0 scrapped)" in short_count_message(_op(10), acct)
        assert not math.isnan(acct.received)


def test_complete_blockers_stay_state_only_for_a_mid_run_operation(db_session: Session):
    """SF5: the scanner never advertises an in-progress operation as 'short'."""
    ann = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
    add_entry(db_session, ann, op, produced=3)
    assert complete_blockers(db_session, op, wo, COMPANY_A) == []


def test_operator_display_name_is_what_the_refusal_names():
    assert operator_display_name("Bob", "Jones") == "Bob J."
