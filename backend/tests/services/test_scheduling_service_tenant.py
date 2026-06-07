"""Tenant-isolation tests for SchedulingService (MS-3).

A scheduling run scoped to one company must never schedule, read, or overwrite
another company's work centers / operations / availability rates. The service
also stays backward-compatible when constructed without a company_id (the
completion-path callers in shop_floor.py / work_orders.py rely on that and pass
explicit, already-tenant-scoped work_center_ids).
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.scheduling_service import SchedulingService


def _seed_company(db: Session, company_id: int, slug: str) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=slug, is_active=True))
        db.commit()


def _seed_unscheduled_op(db: Session, company_id: int, suffix: str) -> WorkOrderOperation:
    """One tenant: a part, an active work center, a RELEASED work order, and a
    single unscheduled READY operation that run_scheduling should pick up."""
    part = Part(
        part_number=f"SVC-PART-{suffix}",
        name="Part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    wc = WorkCenter(
        code=f"SVC-WC-{suffix}",
        name="WC",
        work_center_type="machining",
        is_active=True,
        capacity_hours_per_day=8.0,
        company_id=company_id,
    )
    db.add_all([part, wc])
    db.flush()

    wo = WorkOrder(
        work_order_number=f"SVC-WO-{suffix}",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=5,
        due_date=date.today() + timedelta(days=14),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()

    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Mill",
        status=OperationStatus.READY,
        setup_time_hours=1.0,
        run_time_hours=1.0,
        scheduled_start=None,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


@pytest.mark.requires_db
def test_run_scheduling_only_touches_own_company(db_session: Session):
    _seed_company(db_session, 1, "co-1")
    _seed_company(db_session, 2, "co-2")

    op_a = _seed_unscheduled_op(db_session, company_id=1, suffix="A")
    op_b = _seed_unscheduled_op(db_session, company_id=2, suffix="B")

    # A manager of company 1 runs scheduling with NO work_center_ids (the cross-
    # tenant exposure in MS-3). It must only schedule company 1's operation.
    result = SchedulingService(db_session, company_id=1).run_scheduling(work_center_ids=None, horizon_days=30)

    assert result["scheduled_count"] == 1

    db_session.refresh(op_a)
    db_session.refresh(op_b)
    assert op_a.scheduled_start is not None, "company 1's op should be scheduled"
    assert op_b.scheduled_start is None, "company 2's op must remain untouched"


@pytest.mark.requires_db
def test_availability_rate_not_overwritten_across_tenants(db_session: Session):
    _seed_company(db_session, 1, "co-1")
    _seed_company(db_session, 2, "co-2")

    _seed_unscheduled_op(db_session, company_id=1, suffix="A")
    op_b = _seed_unscheduled_op(db_session, company_id=2, suffix="B")

    wc_b = db_session.query(WorkCenter).filter(WorkCenter.id == op_b.work_center_id).first()
    wc_b.availability_rate = 42.0
    db_session.commit()

    SchedulingService(db_session, company_id=1).run_scheduling(work_center_ids=None, horizon_days=30)

    db_session.refresh(wc_b)
    assert wc_b.availability_rate == 42.0, "another tenant's availability_rate must not be recomputed"


@pytest.mark.requires_db
def test_unscoped_service_still_works_for_completion_path(db_session: Session):
    """Backward compatibility: SchedulingService(db) with no company_id keeps
    today's behavior for the completion-path callers that pass scoped ids."""
    _seed_company(db_session, 1, "co-1")
    op = _seed_unscheduled_op(db_session, company_id=1, suffix="A")

    # No company_id, explicit work_center_ids — exactly how shop_floor/work_orders
    # invoke update_availability_rates.
    rates = SchedulingService(db_session).update_availability_rates(
        work_center_ids=[op.work_center_id], horizon_days=90
    )
    assert op.work_center_id in rates
