"""Reusable JobCost recompute from TimeEntry evidence (Batch 7 / rank 10, COST-2).

Before Batch 7 the only place that turned TimeEntry labor into JobCost actuals was the
``POST /job-costs/{id}/calculate`` endpoint body (``calculate_costs`` in
``api/endpoints/job_costing.py``). No completion path ever called it, so a completed WO
with a linked JobCost kept stale actuals and stayed ``IN_PROGRESS`` until someone hit
``/calculate`` by hand (COST-2).

This module extracts that recompute into ``recompute_from_time_entries`` so BOTH the
endpoint and the completion rollup share ONE implementation, and adds
``sync_job_cost_on_completion`` which the completion cost rollup calls to bring a linked
JobCost up to date and flip it to ``COMPLETED``.

Rules:

* **Tenant-scoped.** Every JobCost / CostEntry lookup filters ``company_id``; the labor
  rate comes from the shared ``labor_cost_service`` resolver (per work center, COST-5),
  not the old hardcoded ``$45``.
* **Audited.** A recompute on the completion path writes a tamper-evident audit row via
  ``AuditService`` (the JobCost actuals feed compliance-facing cost reports). The
  endpoint keeps its existing behavior.
* **No commit.** Joins the caller's unit of work (the completion handler / endpoint owns
  the commit), so the JobCost sync is atomic with the completion when the flag is ON.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models.job_costing import CostEntry, CostEntrySource, CostEntryType, JobCost, JobCostStatus
from app.models.time_entry import TimeEntry
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.labor_cost_service import is_approved_labor_required, resolve_labor_rates

logger = logging.getLogger(__name__)


def recalculate_totals(job_cost: JobCost) -> None:
    """Recompute totals, variances, and margin from entries + estimates.

    Identical math to the endpoint's ``recalculate_job_cost`` -- centralized so the
    completion sync and the endpoint stay in lock-step. Mutates ``job_cost`` in place.
    """
    material_total = 0.0
    labor_total = 0.0
    overhead_total = 0.0
    other_total = 0.0

    for entry in job_cost.entries:
        if entry.entry_type == CostEntryType.MATERIAL or entry.entry_type == "material":
            material_total += entry.total_cost
        elif entry.entry_type == CostEntryType.LABOR or entry.entry_type == "labor":
            labor_total += entry.total_cost
        elif entry.entry_type == CostEntryType.OVERHEAD or entry.entry_type == "overhead":
            overhead_total += entry.total_cost
        else:
            other_total += entry.total_cost

    job_cost.actual_material_cost = material_total
    job_cost.actual_labor_cost = labor_total
    job_cost.actual_overhead_cost = overhead_total + other_total
    job_cost.actual_total_cost = material_total + labor_total + overhead_total + other_total

    job_cost.estimated_total_cost = (
        job_cost.estimated_material_cost + job_cost.estimated_labor_cost + job_cost.estimated_overhead_cost
    )

    job_cost.material_variance = job_cost.actual_material_cost - job_cost.estimated_material_cost
    job_cost.labor_variance = job_cost.actual_labor_cost - job_cost.estimated_labor_cost
    job_cost.overhead_variance = job_cost.actual_overhead_cost - job_cost.estimated_overhead_cost
    job_cost.total_variance = job_cost.actual_total_cost - job_cost.estimated_total_cost

    if job_cost.revenue and job_cost.revenue > 0:
        job_cost.margin_amount = job_cost.revenue - job_cost.actual_total_cost
        job_cost.margin_percent = (job_cost.margin_amount / job_cost.revenue) * 100
    else:
        job_cost.margin_amount = 0.0
        job_cost.margin_percent = 0.0


def recompute_from_time_entries(
    db: Session,
    *,
    job_cost: JobCost,
    company_id: int,
    user_id: Optional[int] = None,
) -> JobCost:
    """Regenerate TIME_ENTRY-sourced labor entries from closed TimeEntries + recompute.

    The shared recompute the endpoint and the completion sync both call. Deletes the
    existing auto-generated (``CostEntrySource.TIME_ENTRY``) labor entries and rebuilds
    one per closed TimeEntry at the work-center labor rate (COST-5, via the shared
    resolver -- no more hardcoded $45), then recomputes totals/variances/margin. Every
    query is tenant-scoped on ``company_id``. Joins the caller's unit of work (no commit).
    """
    # Closed time entries for this work order, tenant-scoped.
    te_query = db.query(TimeEntry).filter(
        TimeEntry.work_order_id == job_cost.work_order_id,
        TimeEntry.company_id == company_id,
        TimeEntry.clock_out.isnot(None),
    )
    # G5-A opt-in: when REQUIRE_APPROVED_LABOR_FOR_COST is ON, exclude un-approved
    # labor from cost. Default OFF -> no extra predicate -> byte-identical to before.
    if is_approved_labor_required(company_id):
        te_query = te_query.filter(TimeEntry.approved.isnot(None))
    time_entries = te_query.all()

    # Resolve a labor rate per work center the entries touched (per WC -- COST-5). An
    # entry's work_center is its own column; fall back to its operation's work center
    # so labor cost still reflects where the work happened when the entry omits it.
    op_ids = [te.operation_id for te in time_entries if te.operation_id is not None]
    op_wc: dict[int, Optional[int]] = {}
    if op_ids:
        # Tenant-scoped (invariant #1): WorkOrderOperation is a TenantMixin table, so the
        # operation->work_center map MUST be company-scoped. Without the company filter a
        # foreign-company operation row sharing an id could leak its work center (and thus
        # its rate) into this company's labor cost.
        for op in (
            db.query(WorkOrderOperation)
            .filter(WorkOrderOperation.id.in_(op_ids), WorkOrderOperation.company_id == company_id)
            .all()
        ):
            op_wc[op.id] = op.work_center_id

    def _wc_for(te: TimeEntry) -> Optional[int]:
        if te.work_center_id is not None:
            return te.work_center_id
        if te.operation_id is not None:
            return op_wc.get(te.operation_id)
        return None

    rates = resolve_labor_rates(db, company_id, [_wc_for(te) for te in time_entries])

    # Remove existing auto-generated labor entries (tenant-scoped) before rebuilding.
    existing_auto = (
        db.query(CostEntry)
        .filter(
            CostEntry.job_cost_id == job_cost.id,
            CostEntry.company_id == company_id,
            CostEntry.source == CostEntrySource.TIME_ENTRY,
        )
        .all()
    )
    for entry in existing_auto:
        db.delete(entry)
    db.flush()

    for te in time_entries:
        duration = float(te.duration_hours or 0)
        if duration <= 0:
            continue
        rate = rates.get(_wc_for(te), rates[None])
        entry = CostEntry(
            job_cost_id=job_cost.id,
            entry_type=CostEntryType.LABOR,
            description=f"Labor - {te.entry_type.value if hasattr(te.entry_type, 'value') else te.entry_type}",
            quantity=duration,
            unit_cost=rate,
            total_cost=duration * rate,
            work_order_operation_id=te.operation_id,
            source=CostEntrySource.TIME_ENTRY,
            reference=f"TE-{te.id}",
            entry_date=te.clock_in.date() if te.clock_in else date.today(),
            created_by=user_id,
        )
        entry.company_id = company_id
        db.add(entry)
    db.flush()

    db.refresh(job_cost)
    recalculate_totals(job_cost)
    return job_cost


def sync_job_cost_on_completion(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    user_id: Optional[int],
    audit: AuditService,
) -> Optional[JobCost]:
    """On WO completion, sync the linked JobCost + flip it to COMPLETED (COST-2).

    Loads the WO's JobCost (tenant-scoped); if none exists this is a no-op (Batch 7 does
    NOT auto-create a JobCost -- only an existing one is synced). Regenerates its
    TIME_ENTRY labor at the shared rate, recomputes variances, sets
    ``status = COMPLETED``, and writes ONE tamper-evident audit row. Best-effort: a
    JobCost-sync failure must never fail the completion, so the body is wrapped. Joins
    the caller's unit of work (no commit). Returns the synced JobCost, or ``None``.
    """
    try:
        job_cost = (
            db.query(JobCost)
            .options(joinedload(JobCost.entries))
            .filter(JobCost.work_order_id == work_order.id, JobCost.company_id == company_id)
            .first()
        )
        if job_cost is None:
            return None

        old_status = job_cost.status.value if hasattr(job_cost.status, "value") else job_cost.status
        old_total = float(job_cost.actual_total_cost or 0)

        recompute_from_time_entries(db, job_cost=job_cost, company_id=company_id, user_id=user_id)
        job_cost.status = JobCostStatus.COMPLETED
        db.flush()

        audit.log_update(
            resource_type="job_cost",
            resource_id=job_cost.id,
            resource_identifier=str(job_cost.id),
            old_values={"status": old_status, "actual_total_cost": old_total},
            new_values={
                "status": JobCostStatus.COMPLETED.value,
                "actual_total_cost": float(job_cost.actual_total_cost or 0),
            },
            description=(
                f"Synced job cost from time entries on completion of WO {work_order.work_order_number}: "
                f"actual ${float(job_cost.actual_total_cost or 0):.2f}, status COMPLETED"
            ),
            action="job_cost_sync",
            extra_data={"work_order_id": work_order.id, "source": "completion_cost_rollup"},
        )
        return job_cost
    except Exception:  # pragma: no cover - JobCost sync must never fail a completion
        logger.exception(
            "JobCost sync failed on completion of WO %s (company %s); completion is unaffected",
            work_order.id,
            company_id,
        )
        return None
