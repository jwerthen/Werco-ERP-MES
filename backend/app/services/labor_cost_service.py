"""Shared labor-rate / labor-cost resolution (Batch 7 / rank 10, COST-5).

Before Batch 7 the "actual" labor cost for a job was computed from TWO different
hardcoded constants that disagreed for the same work order:

* ``app/api/endpoints/job_costing.py`` charged ``$45/hr``;
* ``app/services/analytics_service.py`` charged ``$50/hr`` (``actual_hours * 50``).

Neither read a configured rate, even though ``WorkCenter.hourly_rate`` exists as the
proper per-work-center source of truth. This module is the SINGLE place that resolves
a labor (and overhead/burden) rate, so the completion cost rollup, the JobCost
recompute, and the analytics cost report all agree.

Resolution policy (COST-5):

* **Labor rate** -> ``WorkCenter.hourly_rate`` when the work center has a positive
  rate (labor cost should reflect WHERE the work happened), else the single
  configurable ``settings.DEFAULT_LABOR_RATE`` fallback.
* **Overhead/burden rate** -> ``settings.DEFAULT_OVERHEAD_RATE`` (a per-work-center
  overhead column can be threaded in later; the routing's ``overhead_rate`` lives on
  the routing operation, not the work center, and is an estimate input).

Everything here is read-only and tenant-scoped: every work-center lookup filters
``company_id`` so a foreign work center can never leak a rate across tenants.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.work_center import WorkCenter


def is_labor_cost_rollup_enabled(company_id: Optional[int] = None) -> bool:
    """Whether completion should auto-populate actual hours/cost + sync JobCost.

    OPT-IN, default OFF (Batch 7 product decision). Currently a GLOBAL setting
    (``settings.LABOR_COST_ROLLUP_ENABLED``) because the Company model has no
    per-company settings/feature-flags column yet. ``company_id`` is accepted now so
    callers don't have to change when this becomes a per-company flag -- this is the
    single chokepoint to repoint at a Company settings field later (see the TODO on
    ``LABOR_COST_ROLLUP_ENABLED`` in ``app/core/config.py``).
    """
    return bool(settings.LABOR_COST_ROLLUP_ENABLED)


def resolve_labor_rate(db: Session, company_id: int, work_center_id: Optional[int]) -> float:
    """Labor rate ($/hr) for a work center: ``WorkCenter.hourly_rate`` else the default.

    Tenant-scoped (the work-center lookup filters ``company_id``). A missing work
    center, an unscoped/foreign id, or a non-positive ``hourly_rate`` all fall back to
    ``settings.DEFAULT_LABOR_RATE`` so a rate is ALWAYS resolved (cost is never silently
    zeroed by a misconfigured work center).
    """
    if work_center_id is not None:
        work_center = (
            db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
        )
        if work_center is not None and work_center.hourly_rate and float(work_center.hourly_rate) > 0:
            return float(work_center.hourly_rate)
    return float(settings.DEFAULT_LABOR_RATE)


def resolve_overhead_rate(db: Session, company_id: int, work_center_id: Optional[int]) -> float:
    """Overhead/burden rate ($/hr) charged on labor hours.

    A single configurable default for now (``settings.DEFAULT_OVERHEAD_RATE``). The
    ``work_center_id`` is accepted so a per-work-center overhead column can be wired in
    here later without touching the rollup callers.
    """
    return float(settings.DEFAULT_OVERHEAD_RATE)


def resolve_labor_rates(
    db: Session, company_id: int, work_center_ids: list[Optional[int]]
) -> dict[Optional[int], float]:
    """Batch-resolve labor rates for several work centers in one query.

    Used by the analytics cost report and the WO actual-cost rollup so they don't
    issue one SELECT per work center. Returns ``{work_center_id: rate}``; ``None`` keys
    (operations with no work center) map to the default rate.
    """
    wanted = {wc_id for wc_id in work_center_ids if wc_id is not None}
    rates: dict[Optional[int], float] = {None: float(settings.DEFAULT_LABOR_RATE)}
    if wanted:
        rows = (
            db.query(WorkCenter.id, WorkCenter.hourly_rate)
            .filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(wanted))
            .all()
        )
        by_id = {row.id: row.hourly_rate for row in rows}
        for wc_id in wanted:
            rate = by_id.get(wc_id)
            rates[wc_id] = float(rate) if rate and float(rate) > 0 else float(settings.DEFAULT_LABOR_RATE)
    return rates
