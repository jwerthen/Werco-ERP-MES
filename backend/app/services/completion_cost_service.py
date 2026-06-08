"""Labor-hour + actual/estimated cost rollup on work-order completion (Batch 7 / rank 10).

Findings closed here: COST-1 (actual_cost/estimated_cost never computed),
COST-3 (shop-floor complete_operation auto-closes TimeEntries but never rolls their
hours into op/WO actuals), COST-4 (reconcile drives ops/WO to COMPLETE without any
hour rollup), and the rollup half of COST-5 (one shared labor-rate source). The
JobCost-sync half of COST-2 lives in ``services/job_costing_service.py``; this module
calls it.

Two posture rules govern everything here:

* **OPT-IN, default OFF.** The auto-population of ``actual_hours`` / ``actual_cost`` and
  the JobCost sync is GATED behind ``settings.LABOR_COST_ROLLUP_ENABLED`` (resolved via
  ``labor_cost_service.is_labor_cost_rollup_enabled``). When OFF, completion preserves
  the pre-Batch-7 behavior -- no auto cost/hours, JobCost untouched, on-demand
  ``/job-costs/{id}/calculate`` still works. When ON, completion auto-rolls everything.
  The ONE entry point ``apply_completion_cost_rollup`` is a no-op when the flag is OFF.

* **Best-effort, additive, never fails a completion.** Cost/hour writes join the
  caller's unit of work (NO commit here) so they are atomic with the completion when
  the flag is ON, but a cost-side failure must never abort an otherwise-valid
  completion -- the entry point wraps the rollup defensively. Hours are MONOTONIC-UP on
  reconcile (the evidence-sourced hour rollup never lowers a value), mirroring the
  Batch-3 quantity-repair pattern.

Hours are the SUM of ``duration_hours`` across ALL operators' TimeEntries on an
operation -- multiple welders each have their own entry and they are summed, never
deduped by operation (the multi-operator invariant the audit calls out in COST-3/COST-4).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.inventory import InventoryTransaction, TransactionType
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.job_costing_service import sync_job_cost_on_completion
from app.services.labor_cost_service import (
    is_labor_cost_rollup_enabled,
    resolve_labor_rates,
    resolve_overhead_rate,
)

logger = logging.getLogger(__name__)

# entry_types that count as SETUP labor; everything else (RUN / REWORK / INSPECTION /
# DOWNTIME / BREAK) rolls into run hours, mirroring the clock_out split which credits
# SETUP -> actual_setup_hours and the rest -> actual_run_hours.
_SETUP_ENTRY_TYPES = (TimeEntryType.SETUP,)


def _is_setup_entry(entry: TimeEntry) -> bool:
    return entry.entry_type in _SETUP_ENTRY_TYPES


def rollup_labor_hours_for_closed_entries(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    closed_entries: Iterable[TimeEntry],
) -> float:
    """Accumulate just-closed entries' ``duration_hours`` into op + WO actuals (COST-3).

    The ADDITIVE rollup used when an endpoint has JUST written ``clock_out`` +
    ``duration_hours`` on a set of previously-open entries (the shop-floor
    ``complete_operation`` auto-close). Mirrors the clock_out hour logic: SETUP entries
    credit ``operation.actual_setup_hours``, all others credit ``actual_run_hours``, and
    every entry credits ``work_order.actual_hours``. Returns the total hours added.

    Sums across ALL passed entries (multiple operators on one operation each contribute
    their own entry). Joins the caller's unit of work; does not commit.
    """
    added = 0.0
    for entry in closed_entries:
        duration = float(entry.duration_hours or 0)
        if duration <= 0:
            continue
        if _is_setup_entry(entry):
            operation.actual_setup_hours = float(operation.actual_setup_hours or 0) + duration
        else:
            operation.actual_run_hours = float(operation.actual_run_hours or 0) + duration
        work_order.actual_hours = float(work_order.actual_hours or 0) + duration
        added += duration
    return added


def _operation_duration_by_type(db: Session, operation_ids: list[int]) -> dict[int, tuple[float, float]]:
    """SUM(duration_hours) per operation, split into (setup_hours, run_hours).

    The durable-evidence aggregation that backs the monotonic-up reconcile rollup
    (COST-4). Sums CLOSED TimeEntries (``clock_out`` is not NULL -> ``duration_hours``
    is populated) across ALL operators on each operation. SETUP entries go to the setup
    bucket, everything else to run.
    """
    result: dict[int, tuple[float, float]] = {}
    if not operation_ids:
        return result
    rows = (
        db.query(
            TimeEntry.operation_id,
            TimeEntry.entry_type,
            func.coalesce(func.sum(TimeEntry.duration_hours), 0.0).label("hours"),
        )
        .filter(
            TimeEntry.operation_id.in_(operation_ids),
            TimeEntry.clock_out.isnot(None),
        )
        .group_by(TimeEntry.operation_id, TimeEntry.entry_type)
        .all()
    )
    for row in rows:
        if row.operation_id is None:
            continue
        setup, run = result.get(row.operation_id, (0.0, 0.0))
        hours = float(row.hours or 0)
        if row.entry_type in _SETUP_ENTRY_TYPES:
            setup += hours
        else:
            run += hours
        result[row.operation_id] = (setup, run)
    return result


def rollup_labor_hours_from_evidence(db: Session, work_order: WorkOrder) -> bool:
    """Set op/WO actual hours MONOTONIC-UP from durable TimeEntry evidence (COST-4).

    Idempotent and never-regressing: for each operation, the setup/run hours are raised
    to the SUM of closed-entry ``duration_hours`` (split by entry_type) but never
    lowered; ``work_order.actual_hours`` is raised to the sum across all the WO's
    operations. Safe to re-run on every reconcile / WO completion sweep -- it converges
    on the durable evidence without double-counting (it SETS to the sum, it does not add
    a delta). Returns True if anything changed.

    Sums across ALL operators (no dedup by operation). Joins the caller's unit of work.
    """
    operations = [op for op in (work_order.operations or []) if op.id is not None]
    if not operations:
        return False
    by_op = _operation_duration_by_type(db, [op.id for op in operations])

    changed = False
    wo_total = 0.0
    for op in operations:
        setup_hours, run_hours = by_op.get(op.id, (0.0, 0.0))
        if setup_hours > float(op.actual_setup_hours or 0):
            op.actual_setup_hours = setup_hours
            changed = True
        if run_hours > float(op.actual_run_hours or 0):
            op.actual_run_hours = run_hours
            changed = True
        wo_total += float(op.actual_setup_hours or 0) + float(op.actual_run_hours or 0)

    if wo_total > float(work_order.actual_hours or 0):
        work_order.actual_hours = wo_total
        changed = True
    return changed


def _labor_and_overhead_cost(db: Session, work_order: WorkOrder, company_id: int) -> tuple[float, float]:
    """(labor_cost, overhead_cost) from each operation's actual hours at its WC rate.

    Labor cost is summed PER OPERATION at the operation's work-center rate (COST-5:
    labor cost reflects WHERE the work happened), using the shared
    ``labor_cost_service`` resolver so analytics and the rollup agree. Overhead is the
    actual labor hours at the resolved overhead/burden rate.
    """
    operations = [op for op in (work_order.operations or []) if op.id is not None]
    rates = resolve_labor_rates(db, company_id, [op.work_center_id for op in operations])
    overhead_rate = resolve_overhead_rate(db, company_id, None)

    labor_cost = 0.0
    overhead_cost = 0.0
    for op in operations:
        op_hours = float(op.actual_setup_hours or 0) + float(op.actual_run_hours or 0)
        if op_hours <= 0:
            continue
        rate = rates.get(op.work_center_id, rates[None])
        labor_cost += op_hours * rate
        overhead_cost += op_hours * overhead_rate
    return labor_cost, overhead_cost


def _issued_material_cost(db: Session, work_order: WorkOrder, company_id: int) -> float:
    """Total cost of material ISSUEd to the WO (the Batch-6 backflush/issue txns).

    Sums ``abs(total_cost)`` over every ISSUE ``InventoryTransaction`` referencing this
    work order (ISSUE quantities are stored negative, so ``total_cost`` may be negative;
    we take the magnitude). Tenant-scoped.
    """
    total = (
        db.query(func.coalesce(func.sum(func.abs(InventoryTransaction.total_cost)), 0.0))
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == work_order.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .scalar()
    )
    return float(total or 0.0)


def compute_and_store_actual_cost(db: Session, work_order: WorkOrder, company_id: int) -> dict[str, float]:
    """Populate ``WorkOrder.actual_cost`` = labor + issued material + overhead (COST-1).

    Computes from the (already rolled-up) actual hours at the shared WC rate plus the
    cost of material ISSUEd to the WO (Batch-6 ISSUE txns) plus overhead. Stores the
    total on ``work_order.actual_cost`` and returns the breakdown so the JobCost sync /
    caller can reuse it without recomputing. Monotonic-up is NOT applied to cost (it is
    a deterministic function of hours+material that are themselves monotonic-up).
    Joins the caller's unit of work.
    """
    labor_cost, overhead_cost = _labor_and_overhead_cost(db, work_order, company_id)
    material_cost = _issued_material_cost(db, work_order, company_id)
    total = labor_cost + material_cost + overhead_cost
    work_order.actual_cost = total
    return {
        "labor_cost": labor_cost,
        "material_cost": material_cost,
        "overhead_cost": overhead_cost,
        "total": total,
    }


def compute_and_store_estimated_cost(db: Session, work_order: WorkOrder, company_id: int) -> float:
    """Best-effort ``WorkOrder.estimated_cost`` from routing standard hours + BOM (COST-5).

    estimated_cost = Σ(operation standard setup+run hours × WC rate) + estimated BOM
    material (best-effort). The WO operations already carry ``setup_time_hours`` /
    ``run_time_hours`` copied from the routing at WO creation, so estimate from those at
    the same shared WC rate the actuals use (so estimate vs actual is an apples-to-apples
    variance). BOM material is estimated from the finished part's active BOM exploded at
    the ordered quantity; if routing/BOM data is thin the corresponding leg is simply 0.
    Stores on ``work_order.estimated_cost`` and returns it. Joins the caller's unit of work.
    """
    operations = [op for op in (work_order.operations or []) if op.id is not None]
    rates = resolve_labor_rates(db, company_id, [op.work_center_id for op in operations])

    ordered_qty = float(work_order.quantity_ordered or 0)
    labor_estimate = 0.0
    for op in operations:
        setup = float(op.setup_time_hours or 0)
        run_per_piece = float(op.run_time_per_piece or 0)
        run = float(op.run_time_hours or 0) or (run_per_piece * ordered_qty)
        std_hours = setup + run
        if std_hours <= 0:
            continue
        rate = rates.get(op.work_center_id, rates[None])
        labor_estimate += std_hours * rate

    material_estimate = _estimated_bom_material_cost(db, work_order, company_id, ordered_qty)
    estimated = labor_estimate + material_estimate
    work_order.estimated_cost = estimated
    return estimated


def _estimated_bom_material_cost(db: Session, work_order: WorkOrder, company_id: int, ordered_qty: float) -> float:
    """Best-effort estimated BOM material cost (Σ component standard_cost × extended qty).

    Reuses the tenant-scoped BOM explosion helpers from the work-orders endpoint (which
    already apply ``scrap_factor``). Returns 0.0 when the part has no active BOM (thin
    data is acceptable per COST-1's "best-effort" note).
    """
    if ordered_qty <= 0:
        return 0.0
    # Lazy import to avoid an import cycle with the endpoints module (same pattern the
    # completion_inventory_service uses for backflush BOM resolution).
    from app.api.endpoints.work_orders import _collect_bom_components, _get_active_bom

    bom = _get_active_bom(db, work_order.part_id, company_id)
    if not bom:
        return 0.0
    total = 0.0
    for _item, component, extended_qty in _collect_bom_components(db, bom, company_id, parent_qty=ordered_qty):
        total += float(getattr(component, "standard_cost", 0) or 0) * float(extended_qty or 0)
    return total


def apply_completion_cost_rollup(
    db: Session,
    work_order: WorkOrder,
    *,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[dict[str, float]]:
    """Flag-gated entry point: roll up hours + cost + JobCost on WO completion.

    NO-OP when ``LABOR_COST_ROLLUP_ENABLED`` is OFF (the default) -- completion then
    preserves the pre-Batch-7 behavior. When ON, in the caller's unit of work (atomic
    with the completion, no commit here):

    1. raise op/WO actual hours monotonic-up from durable TimeEntry evidence (COST-4);
    2. compute + store ``WorkOrder.actual_cost`` = labor + issued material + overhead
       (COST-1) using the shared WC rate (COST-5);
    3. sync the linked ``JobCost`` (TIME_ENTRY labor regenerated, variances recomputed,
       status -> COMPLETED) via ``job_costing_service`` (COST-2); and
    4. write ONE tamper-evident audit row recording the rolled-up actuals.

    Best-effort: the whole body is wrapped so a cost-side error can NEVER fail an
    otherwise-valid completion (the hour/cost figures are additive, not a correctness
    gate on the status change). Returns the cost breakdown when it ran, else ``None``.
    """
    if not is_labor_cost_rollup_enabled(company_id):
        return None
    if work_order is None or work_order.id is None:
        return None
    try:
        old_hours = float(work_order.actual_hours or 0)
        old_cost = float(work_order.actual_cost or 0)

        rollup_labor_hours_from_evidence(db, work_order)
        breakdown = compute_and_store_actual_cost(db, work_order, company_id)
        db.flush()

        # COST-2: sync the linked JobCost (if any) -- regenerate TIME_ENTRY labor at the
        # shared rate, recompute variances, set status COMPLETED. Tenant-scoped + audited
        # inside the service. Best-effort (its own failure can't fail the completion).
        sync_job_cost_on_completion(
            db,
            work_order=work_order,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )

        # Tamper-evident record of the auto-rolled actuals (the cost/hours figures now
        # surface in compliance-facing reports, so the rollup itself is auditable).
        audit.log_update(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_values={"actual_hours": old_hours, "actual_cost": old_cost},
            new_values={
                "actual_hours": float(work_order.actual_hours or 0),
                "actual_cost": float(work_order.actual_cost or 0),
            },
            description=(
                f"Rolled up labor actuals on completion of WO {work_order.work_order_number}: "
                f"{float(work_order.actual_hours or 0):.2f} hr, ${breakdown['total']:.2f} "
                f"(labor ${breakdown['labor_cost']:.2f} + material ${breakdown['material_cost']:.2f} "
                f"+ overhead ${breakdown['overhead_cost']:.2f})"
            ),
            action="cost_rollup",
            extra_data={"cost_breakdown": breakdown, "source": "completion_cost_rollup"},
        )
        return breakdown
    except Exception:  # pragma: no cover - a cost rollup must never fail a completion
        logger.exception(
            "completion cost rollup failed for WO %s (company %s); completion is unaffected",
            work_order.id,
            company_id,
        )
        return None
