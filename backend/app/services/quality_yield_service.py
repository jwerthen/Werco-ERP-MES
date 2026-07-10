"""First-pass yield / rolled throughput yield + scrap Pareto (Lean Phase 1, 1d).

FPY per operation = (quantity_complete - quantity_reworked - quantity_scrapped)
/ (quantity_complete + quantity_scrapped), numerator clamped at 0 (an op whose
rework+scrap exceeds its good count has 0% first-pass, not negative). Group
FPYs are quantity-weighted (sum of first-pass units / sum of attempted units),
never averages of ratios. RTY per WO = product of its operations' FPYs.

Scrap Pareto reconciles the three places scrap is recorded so every unit is
counted once per period, attributed to its reason code (uncoded = the
'unspecified' bucket):

  tier 1: TimeEntry.quantity_scrapped (clock-out / production reports), window
          by clock_in. Provenance rule: backfill/import-sourced entries are
          excluded from the buckets and tallied separately.
  tier 2: operation-level scrap NOT backed by time entries (office/admin op
          writes) = op.quantity_scrapped - lifetime entry scrap for that op,
          clamped >= 0; window by the op's actual_end (fallback updated_at).
  tier 3: WO-level scrap NOT backed by operations (the /work-orders/{id}/complete
          override writes WO scrap only) = wo.quantity_scrapped - lifetime op
          scrap, clamped >= 0; window by the WO's actual_end (fallback
          updated_at). Skipped when filtering by work center (WO-level scrap has
          no work-center attribution).

Cost = quantity x part.standard_cost where available. All queries tenant-scoped.
"""

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.scrap_reason import ScrapReasonCode
from app.models.time_entry import BASELINE_EXCLUDED_SOURCES, TimeEntry
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.analytics import FPYGroup, FPYResponse, ScrapParetoBucket, ScrapParetoResponse

logger = logging.getLogger(__name__)


def _op_first_pass_units(op_complete: float, op_reworked: float, op_scrapped: float) -> float:
    return max(0.0, op_complete - op_reworked - op_scrapped)


def _fpy_pct(first_pass: float, attempted: float) -> Optional[float]:
    if attempted <= 0:
        return None
    return round(min(1.0, first_pass / attempted) * 100.0, 1)


def get_fpy_rty(
    db: Session,
    company_id: int,
    start: date,
    end: date,
    work_center_id: Optional[int] = None,
    part_id: Optional[int] = None,
) -> FPYResponse:
    """FPY/RTY aggregated per part and per work center over the window.

    Operations are anchored to the window by ``actual_end`` (a completed op is a
    finished yield sample). RTY is computed per WO over ALL its window ops in
    route order and is only meaningful for the full route, so it is omitted
    (None) when a ``work_center_id`` filter narrows the route.
    """
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    query = (
        db.query(
            WorkOrderOperation.id,
            WorkOrderOperation.work_order_id,
            WorkOrderOperation.work_center_id,
            WorkOrderOperation.quantity_complete,
            WorkOrderOperation.quantity_scrapped,
            WorkOrderOperation.quantity_reworked,
            WorkOrder.part_id,
            Part.part_number,
            Part.name.label("part_name"),
        )
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .join(Part, WorkOrder.part_id == Part.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrderOperation.actual_end.isnot(None),
            WorkOrderOperation.actual_end >= start_dt,
            WorkOrderOperation.actual_end <= end_dt,
        )
    )
    if work_center_id:
        query = query.filter(WorkOrderOperation.work_center_id == work_center_id)
    if part_id:
        query = query.filter(WorkOrder.part_id == part_id)
    ops = query.all()

    overall_first_pass = 0.0
    overall_attempted = 0.0
    by_part: Dict[int, Dict] = {}
    by_wc: Dict[int, Dict] = {}
    wo_op_fpys: Dict[int, List[float]] = {}
    wo_part: Dict[int, int] = {}

    for op in ops:
        complete = float(op.quantity_complete or 0)
        scrapped = float(op.quantity_scrapped or 0)
        reworked = float(op.quantity_reworked or 0)
        attempted = complete + scrapped
        first_pass = _op_first_pass_units(complete, reworked, scrapped)

        overall_first_pass += first_pass
        overall_attempted += attempted

        part_bucket = by_part.setdefault(
            op.part_id,
            {"key": op.part_number, "name": op.part_name, "ops": 0, "attempted": 0.0, "first_pass": 0.0, "wos": set()},
        )
        part_bucket["ops"] += 1
        part_bucket["attempted"] += attempted
        part_bucket["first_pass"] += first_pass
        part_bucket["wos"].add(op.work_order_id)

        if op.work_center_id:
            wc_bucket = by_wc.setdefault(
                op.work_center_id, {"ops": 0, "attempted": 0.0, "first_pass": 0.0, "wos": set()}
            )
            wc_bucket["ops"] += 1
            wc_bucket["attempted"] += attempted
            wc_bucket["first_pass"] += first_pass
            wc_bucket["wos"].add(op.work_order_id)

        if attempted > 0:
            wo_op_fpys.setdefault(op.work_order_id, []).append(min(1.0, first_pass / attempted))
            wo_part[op.work_order_id] = op.part_id

    # RTY per WO = product of its op FPYs; only when the full route is in scope.
    rty_by_wo: Dict[int, float] = {}
    if not work_center_id:
        for wo_id, fpys in wo_op_fpys.items():
            rty = 1.0
            for value in fpys:
                rty *= value
            rty_by_wo[wo_id] = rty

    rty_by_part: Dict[int, List[float]] = {}
    for wo_id, rty in rty_by_wo.items():
        rty_by_part.setdefault(wo_part[wo_id], []).append(rty)

    part_rows = []
    for pid, bucket in by_part.items():
        part_rtys = rty_by_part.get(pid)
        part_rows.append(
            FPYGroup(
                key=bucket["key"],
                name=bucket["name"],
                operations=bucket["ops"],
                units_attempted=round(bucket["attempted"], 2),
                first_pass_units=round(bucket["first_pass"], 2),
                fpy_pct=_fpy_pct(bucket["first_pass"], bucket["attempted"]),
                rty_pct=round(sum(part_rtys) / len(part_rtys) * 100.0, 1) if part_rtys else None,
                work_orders=len(bucket["wos"]),
            )
        )
    part_rows.sort(key=lambda row: row.fpy_pct if row.fpy_pct is not None else 101.0)

    wc_rows = []
    if by_wc:
        from app.models.work_center import WorkCenter

        wc_info = {
            wc.id: wc
            for wc in db.query(WorkCenter)
            .filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(list(by_wc.keys())))
            .all()
        }
        for wc_id, bucket in by_wc.items():
            wc = wc_info.get(wc_id)
            wc_rows.append(
                FPYGroup(
                    key=wc.code if wc else str(wc_id),
                    name=wc.name if wc else None,
                    operations=bucket["ops"],
                    units_attempted=round(bucket["attempted"], 2),
                    first_pass_units=round(bucket["first_pass"], 2),
                    fpy_pct=_fpy_pct(bucket["first_pass"], bucket["attempted"]),
                    rty_pct=None,  # RTY is a route-level metric, not per-WC
                    work_orders=len(bucket["wos"]),
                )
            )
        wc_rows.sort(key=lambda row: row.fpy_pct if row.fpy_pct is not None else 101.0)

    overall_rty = None
    if rty_by_wo:
        overall_rty = round(sum(rty_by_wo.values()) / len(rty_by_wo) * 100.0, 1)

    return FPYResponse(
        period_start=start,
        period_end=end,
        overall_fpy_pct=_fpy_pct(overall_first_pass, overall_attempted),
        overall_rty_pct=overall_rty,
        by_part=part_rows,
        by_work_center=wc_rows,
        generated_at=datetime.utcnow(),
    )


def get_scrap_rate(db: Session, company_id: int, start: date, end: date) -> Optional[float]:
    """Aggregate scrap share across operations completed in the window.

    scrap_pct = sum(quantity_scrapped) / sum(quantity_complete + quantity_scrapped),
    over ops anchored by ``actual_end`` (same population as FPY). Returns None
    ("n/a") on an empty denominator, never a fake 0. Tenant-scoped.
    """
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())
    row = (
        db.query(
            func.coalesce(func.sum(WorkOrderOperation.quantity_scrapped), 0.0),
            func.coalesce(func.sum(WorkOrderOperation.quantity_complete), 0.0),
        )
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrderOperation.actual_end.isnot(None),
            WorkOrderOperation.actual_end >= start_dt,
            WorkOrderOperation.actual_end <= end_dt,
        )
        .first()
    )
    scrapped = float(row[0] or 0) if row else 0.0
    complete = float(row[1] or 0) if row else 0.0
    denominator = complete + scrapped
    if denominator <= 0:
        return None
    return round(scrapped / denominator * 100.0, 1)


def get_scrap_pareto(
    db: Session,
    company_id: int,
    start: date,
    end: date,
    work_center_id: Optional[int] = None,
    part_id: Optional[int] = None,
) -> ScrapParetoResponse:
    """Scrapped quantity (and standard cost) grouped by scrap reason code."""
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    # bucket key: scrap_reason_code_id (None = 'unspecified') -> {qty, cost}
    buckets: Dict[Optional[int], Dict[str, float]] = {}

    def _add(code_id: Optional[int], quantity: float, cost: float) -> None:
        if quantity <= 0:
            return
        bucket = buckets.setdefault(code_id, {"quantity": 0.0, "cost": 0.0})
        bucket["quantity"] += quantity
        bucket["cost"] += cost

    # ── Tier 1: TimeEntry scrap in the window (provenance-filtered) ────────────
    entry_query = (
        db.query(
            TimeEntry.scrap_reason_code_id,
            func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0.0).label("qty"),
            func.coalesce(func.sum(TimeEntry.quantity_scrapped * func.coalesce(Part.standard_cost, 0.0)), 0.0).label(
                "cost"
            ),
        )
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .outerjoin(Part, WorkOrder.part_id == Part.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.quantity_scrapped > 0,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            or_(TimeEntry.source.is_(None), TimeEntry.source.notin_(BASELINE_EXCLUDED_SOURCES)),
            # Soft-deleted WOs' scrap is off the Pareto (tiers 2/3 already filter
            # is_deleted); the outer join means WO-less entries must stay counted.
            or_(WorkOrder.id.is_(None), WorkOrder.is_deleted == False),  # noqa: E712
        )
    )
    if work_center_id:
        entry_query = entry_query.filter(TimeEntry.work_center_id == work_center_id)
    if part_id:
        entry_query = entry_query.filter(WorkOrder.part_id == part_id)
    for code_id, qty, cost in entry_query.group_by(TimeEntry.scrap_reason_code_id).all():
        _add(code_id, float(qty or 0), float(cost or 0))

    # Provenance rule: scrap booked on backfill/import entries, reported separately.
    # Same join/filter shape as the tier-1 bucket query above so the excluded
    # figure honors BOTH optional filters -- a part-filtered Pareto must not
    # report other parts' backfill scrap in its excluded tally (the part is
    # resolved through the entry's WorkOrder, exactly like tier 1).
    excluded_query = (
        db.query(func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0.0))
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.quantity_scrapped > 0,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.source.in_(BASELINE_EXCLUDED_SOURCES),
            # Same WO-liveness rule as tier 1: soft-deleted WOs' scrap is out.
            or_(WorkOrder.id.is_(None), WorkOrder.is_deleted == False),  # noqa: E712
        )
    )
    if work_center_id:
        excluded_query = excluded_query.filter(TimeEntry.work_center_id == work_center_id)
    if part_id:
        excluded_query = excluded_query.filter(WorkOrder.part_id == part_id)
    excluded_quantity = float(excluded_query.scalar() or 0.0)

    # ── Tier 2: operation scrap not backed by time entries ─────────────────────
    op_anchor = func.coalesce(WorkOrderOperation.actual_end, WorkOrderOperation.updated_at)
    op_query = (
        db.query(
            WorkOrderOperation.id,
            WorkOrderOperation.scrap_reason_code_id,
            WorkOrderOperation.quantity_scrapped,
            func.coalesce(Part.standard_cost, 0.0).label("unit_cost"),
        )
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .outerjoin(Part, WorkOrder.part_id == Part.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrderOperation.quantity_scrapped > 0,
            op_anchor >= start_dt,
            op_anchor <= end_dt,
        )
    )
    if work_center_id:
        op_query = op_query.filter(WorkOrderOperation.work_center_id == work_center_id)
    if part_id:
        op_query = op_query.filter(WorkOrder.part_id == part_id)
    op_rows = op_query.all()

    op_entry_scrap: Dict[int, float] = {}
    if op_rows:
        op_ids = [row.id for row in op_rows]
        op_entry_scrap = {
            op_id: float(qty or 0)
            for op_id, qty in db.query(
                TimeEntry.operation_id, func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0.0)
            )
            .filter(TimeEntry.company_id == company_id, TimeEntry.operation_id.in_(op_ids))
            .group_by(TimeEntry.operation_id)
            .all()
        }
    for row in op_rows:
        delta = max(0.0, float(row.quantity_scrapped or 0) - op_entry_scrap.get(row.id, 0.0))
        _add(row.scrap_reason_code_id, delta, delta * float(row.unit_cost or 0))

    # ── Tier 3: WO scrap not backed by operations (skipped under a WC filter) ──
    if not work_center_id:
        wo_anchor = func.coalesce(WorkOrder.actual_end, WorkOrder.updated_at)
        wo_query = (
            db.query(
                WorkOrder.id,
                WorkOrder.scrap_reason_code_id,
                WorkOrder.quantity_scrapped,
                func.coalesce(Part.standard_cost, 0.0).label("unit_cost"),
            )
            .outerjoin(Part, WorkOrder.part_id == Part.id)
            .filter(
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.quantity_scrapped > 0,
                wo_anchor >= start_dt,
                wo_anchor <= end_dt,
            )
        )
        if part_id:
            wo_query = wo_query.filter(WorkOrder.part_id == part_id)
        wo_rows = wo_query.all()
        wo_op_scrap: Dict[int, float] = {}
        if wo_rows:
            wo_ids = [row.id for row in wo_rows]
            wo_op_scrap = {
                wo_id: float(qty or 0)
                for wo_id, qty in db.query(
                    WorkOrderOperation.work_order_id, func.coalesce(func.sum(WorkOrderOperation.quantity_scrapped), 0.0)
                )
                .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id.in_(wo_ids))
                .group_by(WorkOrderOperation.work_order_id)
                .all()
            }
        for row in wo_rows:
            delta = max(0.0, float(row.quantity_scrapped or 0) - wo_op_scrap.get(row.id, 0.0))
            _add(row.scrap_reason_code_id, delta, delta * float(row.unit_cost or 0))

    # ── Resolve codes + build the ranked Pareto ─────────────────────────────────
    code_ids = [code_id for code_id in buckets.keys() if code_id is not None]
    code_info: Dict[int, ScrapReasonCode] = {}
    if code_ids:
        code_info = {
            code.id: code
            for code in tenant_query(db, ScrapReasonCode, company_id).filter(ScrapReasonCode.id.in_(code_ids)).all()
        }

    total_quantity = sum(bucket["quantity"] for bucket in buckets.values())
    total_cost = sum(bucket["cost"] for bucket in buckets.values())

    ranked = sorted(buckets.items(), key=lambda kv: kv[1]["quantity"], reverse=True)
    rows: List[ScrapParetoBucket] = []
    cumulative = 0.0
    for code_id, bucket in ranked:
        share = (bucket["quantity"] / total_quantity * 100.0) if total_quantity > 0 else 0.0
        cumulative += share
        info = code_info.get(code_id) if code_id is not None else None
        rows.append(
            ScrapParetoBucket(
                scrap_reason_code_id=code_id,
                code=info.code if info else "unspecified",
                name=info.name if info else None,
                category=info.category if info else None,
                quantity=round(bucket["quantity"], 2),
                cost=round(bucket["cost"], 2),
                percentage=round(share, 1),
                cumulative_pct=round(min(cumulative, 100.0), 1),
            )
        )

    return ScrapParetoResponse(
        period_start=start,
        period_end=end,
        total_quantity=round(total_quantity, 2),
        total_cost=round(total_cost, 2),
        buckets=rows,
        excluded_backfill_import_quantity=round(excluded_quantity, 2),
        generated_at=datetime.utcnow(),
    )
