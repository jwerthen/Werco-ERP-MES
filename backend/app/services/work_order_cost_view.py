"""The cost figures the single-work-order read reports (P1.3). PURE READS: no writes, no audit.

Two figures on two different bases, both tenant-scoped:

* ``job_cost_actuals`` -- the work order's ``JobCost`` actuals by element, or ``None`` when
  it has no JobCost (nothing auto-creates one; the completion sync only updates an existing
  row). Read with its own ``company_id`` predicate, never through ``WorkOrder.job_cost``:
  that backref is an unscoped lazy LIST, and the global ``job_costs.work_order_id`` unique
  key can be held by a legacy foreign-tenant row, which must never be disclosed.
* ``issued_material_cost`` -- net ISSUE minus RETURN cost from the inventory ledger
  (``completion_cost_service.issued_material_cost``, the material leg of
  ``WorkOrder.actual_cost``). Independent of ``LABOR_COST_ROLLUP_ENABLED`` and of JobCost.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.job_costing import JobCost
from app.models.work_order import WorkOrder
from app.schemas.work_order import WorkOrderJobCostActuals
from app.services.completion_cost_service import issued_material_cost


def job_cost_actuals_for(db: Session, *, company_id: int, work_order_id: int) -> Optional[WorkOrderJobCostActuals]:
    """The work order's JobCost actuals by element, or ``None`` when it has no JobCost."""
    job_cost = tenant_query(db, JobCost, company_id).filter(JobCost.work_order_id == work_order_id).first()
    if job_cost is None:
        return None
    status = job_cost.status.value if hasattr(job_cost.status, "value") else job_cost.status
    return WorkOrderJobCostActuals(
        job_cost_id=job_cost.id,
        status=str(status),
        actual_material_cost=float(job_cost.actual_material_cost or 0.0),
        actual_labor_cost=float(job_cost.actual_labor_cost or 0.0),
        actual_burden_cost=float(job_cost.actual_overhead_cost or 0.0),
        actual_subcontract_cost=float(job_cost.actual_subcontract_cost or 0.0),
        actual_total_cost=float(job_cost.actual_total_cost or 0.0),
        updated_at=job_cost.updated_at,
    )


def inject_cost_figures(db: Session, *, company_id: int, work_order: WorkOrder) -> None:
    """Set ``job_cost_actuals`` and ``issued_material_cost`` as in-memory attributes.

    Neither is an ORM column (like ``operation_count``), so assigning them dirties nothing.
    Both queries run under ``no_autoflush``: the caller has already normalized mapped columns
    in memory for rendering, and a read must never flush them.
    """
    with db.no_autoflush:
        work_order.job_cost_actuals = job_cost_actuals_for(db, company_id=company_id, work_order_id=work_order.id)
        work_order.issued_material_cost = issued_material_cost(db, work_order, company_id)
