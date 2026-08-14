"""Where a resolved blocker leaves its operation.

``WorkOrderBlockerService._resume_operation_if_no_open_blockers`` is the last
un-migrated twin of ``shop_floor.resume_operation``: it lifts an ON_HOLD operation
when the last open blocker on it clears. It used to write READY unconditionally,
which put cards on the dispatch board and the kiosk queue -- both surface READY
work only -- that every start verb then refused, with nothing to heal them (the
read-path promotion reads PENDING rows only).

These pin the floor-at-PENDING rule and, in particular, the DRAFT carve-out:
``dispatch_service.queued_operations_query`` filters TERMINAL work orders, and
DRAFT is NOT terminal, so this branch is the only thing standing between a
resolved blocker and unreleased work appearing on the floor.
"""

import pytest

from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.work_order_blocker_service import WorkOrderBlockerService


@pytest.fixture
def shop(db_session, test_company):
    part = Part(
        company_id=test_company.id,
        part_number="BLK-RESUME-1",
        name="Weld Assembly",
        part_type="manufactured",
        unit_of_measure="EA",
    )
    cell = WorkCenter(company_id=test_company.id, code="BLK-W1", name="Weld Cell", work_center_type="welding")
    other = WorkCenter(company_id=test_company.id, code="BLK-W2", name="Finish Bay", work_center_type="welding")
    db_session.add_all([part, cell, other])
    db_session.flush()
    return part, cell, other


def _wo(db, company_id, part, *, status, sequential, number):
    wo = WorkOrder(
        company_id=company_id,
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=status,
        sequential_operations=sequential,
    )
    db.add(wo)
    db.flush()
    return wo


def _op(db, company_id, wo, *, sequence, work_center, status, actual_start=None):
    op = WorkOrderOperation(
        company_id=company_id,
        work_order_id=wo.id,
        work_center_id=work_center.id,
        sequence=sequence,
        name=f"OP{sequence}",
        status=status,
        actual_start=actual_start,
    )
    db.add(op)
    db.flush()
    return op


def _resolve_only_blocker(db, company_id, wo, op) -> OperationStatus:
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=wo.id,
        operation_id=op.id,
        category="material",
        title="waiting on stock",
        status=WorkOrderBlockerStatus.OPEN.value,
    )
    db.add(blocker)
    db.flush()
    service = WorkOrderBlockerService(db)
    service._resume_operation_if_no_open_blockers(blocker)
    db.flush()
    db.refresh(op)
    return op.status


class TestAResolvedBlockerNeverPutsRefusableWorkOnTheBoard:
    def test_a_draft_work_order_floors_at_pending(self, db_session, test_company, shop):
        """DRAFT is not terminal, so the queue query would happily show this op."""
        part, cell, _ = shop
        wo = _wo(db_session, test_company.id, part, status=WorkOrderStatus.DRAFT, sequential=True, number="BLK-DRAFT")
        op = _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, op) == OperationStatus.PENDING

    def test_a_terminal_work_order_floors_at_pending(self, db_session, test_company, shop):
        part, cell, _ = shop
        wo = _wo(db_session, test_company.id, part, status=WorkOrderStatus.CANCELLED, sequential=True, number="BLK-CXL")
        op = _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, op) == OperationStatus.PENDING

    def test_a_sequenced_op_behind_an_open_predecessor_floors_at_pending(self, db_session, test_company, shop):
        part, cell, _ = shop
        wo = _wo(db_session, test_company.id, part, status=WorkOrderStatus.RELEASED, sequential=True, number="BLK-SEQ")
        _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.READY)
        held = _op(db_session, test_company.id, wo, sequence=20, work_center=cell, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, held) == OperationStatus.PENDING

    def test_a_pooled_op_behind_a_cross_work_center_predecessor_floors_at_pending(self, db_session, test_company, shop):
        """Deliberate: this tightens POOLED work orders too, and that is the point.

        The pooled gate waives a SAME-work-center predecessor, not a cross-work-center
        one, so READY here was always a card the start verbs refused on arrival.
        """
        part, cell, other = shop
        wo = _wo(
            db_session, test_company.id, part, status=WorkOrderStatus.RELEASED, sequential=False, number="BLK-POOL"
        )
        _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.READY)
        held = _op(db_session, test_company.id, wo, sequence=20, work_center=other, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, held) == OperationStatus.PENDING


class TestItStillLiftsWorkThatIsGenuinelyStartable:
    def test_an_unblocked_released_op_returns_to_ready(self, db_session, test_company, shop):
        part, cell, _ = shop
        wo = _wo(db_session, test_company.id, part, status=WorkOrderStatus.RELEASED, sequential=True, number="BLK-OK")
        op = _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, op) == OperationStatus.READY

    def test_a_pooled_same_work_center_sibling_returns_to_ready(self, db_session, test_company, shop):
        """The pooled waiver still applies -- this is the batch-WO shape."""
        part, cell, _ = shop
        wo = _wo(
            db_session, test_company.id, part, status=WorkOrderStatus.RELEASED, sequential=False, number="BLK-POOL2"
        )
        _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.READY)
        held = _op(db_session, test_company.id, wo, sequence=20, work_center=cell, status=OperationStatus.ON_HOLD)
        assert _resolve_only_blocker(db_session, test_company.id, wo, held) == OperationStatus.READY

    def test_worked_labor_is_never_demoted(self, db_session, test_company, shop):
        """An op with an actual_start resumes to IN_PROGRESS even behind a predecessor."""
        from datetime import datetime

        part, cell, _ = shop
        wo = _wo(
            db_session, test_company.id, part, status=WorkOrderStatus.RELEASED, sequential=True, number="BLK-WORKED"
        )
        _op(db_session, test_company.id, wo, sequence=10, work_center=cell, status=OperationStatus.READY)
        held = _op(
            db_session,
            test_company.id,
            wo,
            sequence=20,
            work_center=cell,
            status=OperationStatus.ON_HOLD,
            actual_start=datetime(2026, 8, 14, 9, 0),
        )
        assert _resolve_only_blocker(db_session, test_company.id, wo, held) == OperationStatus.IN_PROGRESS
