"""
Prediction Service - Delivery dates, capacity forecasting, inventory demand
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import case, func
from sqlalchemy.orm import Session, contains_eager

from app.models.bom import BOM, BOMItem
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.analytics import (
    CapacityForecast,
    CapacityForecastResponse,
    DeliveryPrediction,
    InventoryDemandResponse,
    OperationPrediction,
    StockoutPrediction,
    WorkCenterForecast,
)

logger = logging.getLogger(__name__)

# Default hours per week per work center
DEFAULT_HOURS_PER_WEEK = 40


class PredictionService:
    """Forecasts scoped to ONE company.

    ``company_id`` is a constructor argument, not a per-call one, for the same reason it is
    on ``AnalyticsService`` (which the very same endpoints construct as
    ``AnalyticsService(db, company_id)``): every public method here is a tenant-scoped read,
    every private helper feeds one, and there is no install-wide computation in the module.
    Putting it on the constructor makes the scope impossible to forget on a new helper and
    makes an unscoped construction a TypeError rather than a silent platform-wide read --
    which is exactly how all three endpoints were leaking before.

    Pass the ACTIVE company from ``get_current_company_id``, never ``current_user
    .company_id``: only the former honours platform-admin context switching.
    """

    def __init__(self, db: Session, company_id: int):
        self.db = db
        self.company_id = company_id

    # ============ DELIVERY PREDICTION ============

    def predict_delivery(self, work_order_id: int) -> DeliveryPrediction:
        """
        Predict completion date for a work order based on:
        - Historical cycle times per work center
        - Current queue depth at each work center
        - Operation sequence
        """
        # Invariant 1: ``work_order_id`` is a caller-supplied sequential PK and nothing
        # else on this path checked ownership, so enumerating integers walked every
        # tenant's work orders -- header, part number, AND the sequenced routing with
        # per-step machine and hours, which for a job shop is the process plan itself.
        # Invariant 3: ``WorkOrder`` carries ``SoftDeleteMixin``; a deleted job is not a
        # job to forecast.
        wo = (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.id == work_order_id,
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted.is_(False),
            )
            .first()
        )
        if not wo:
            # Flat, identifier-free message, per the #189 convention: a foreign id must be
            # indistinguishable from an absent one so the status code is not an existence
            # oracle. The "no operations" refusal below can now only be reached for a work
            # order this caller OWNS, so keeping it distinct discloses nothing.
            raise ValueError("Work order not found")

        operations = (
            self.db.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.work_order_id == work_order_id,
                WorkOrderOperation.company_id == self.company_id,
            )
            .order_by(WorkOrderOperation.sequence)
            .all()
        )

        if not operations:
            raise ValueError("Work order has no routing operations")

        # Work-center names are rendered into the response (and into
        # ``bottleneck_work_center``), so resolve them through a scoped read rather than
        # the ``op.work_center`` relationship: ``work_center_id`` is a plain FK that was
        # cross-tenant-writable until #194, so a historically mis-parented row would lazy-
        # load -- and render -- another tenant's machine name. Same reasoning #200 applied
        # to the BOM component renders.
        wc_names = self._work_center_names({op.work_center_id for op in operations})

        # Get historical cycle times per work center
        cycle_times = self._get_historical_cycle_times()

        # Get current queue depth per work center
        queue_depths = self._get_queue_depths()

        # Predict each operation
        predicted_ops = []
        current_time = datetime.utcnow()
        bottleneck = None
        max_queue_wait = 0

        for op in operations:
            if op.status == OperationStatus.COMPLETE:
                # Already done
                predicted_ops.append(
                    OperationPrediction(
                        operation_id=op.id,
                        operation_name=op.name,
                        work_center_name=wc_names.get(op.work_center_id, "Unknown"),
                        predicted_start=op.actual_start or current_time,
                        predicted_end=op.actual_end or current_time,
                        queue_position=0,
                        estimated_hours=op.actual_run_hours + op.actual_setup_hours,
                    )
                )
                if op.actual_end:
                    current_time = op.actual_end
                continue

            wc_id = op.work_center_id

            # Estimated hours for this operation
            est_hours = op.setup_time_hours + (op.run_time_per_piece * wo.quantity_ordered)

            # Apply historical efficiency factor
            if wc_id in cycle_times and cycle_times[wc_id]["count"] > 0:
                efficiency = cycle_times[wc_id]["avg_ratio"]
                est_hours *= efficiency

            # Queue wait time
            queue_depth = queue_depths.get(wc_id, 0)
            avg_job_hours = cycle_times.get(wc_id, {}).get("avg_hours", 4)
            queue_wait_hours = queue_depth * avg_job_hours

            # Track bottleneck
            if queue_wait_hours > max_queue_wait:
                max_queue_wait = queue_wait_hours
                bottleneck = wc_names.get(op.work_center_id)

            # Calculate start and end times (8-hour work days)
            queue_wait_days = queue_wait_hours / 8
            op_days = est_hours / 8

            predicted_start = current_time + timedelta(days=queue_wait_days)
            predicted_end = predicted_start + timedelta(days=op_days)

            predicted_ops.append(
                OperationPrediction(
                    operation_id=op.id,
                    operation_name=op.name,
                    work_center_name=wc_names.get(op.work_center_id, "Unknown"),
                    predicted_start=predicted_start,
                    predicted_end=predicted_end,
                    queue_position=queue_depth,
                    estimated_hours=est_hours,
                )
            )

            current_time = predicted_end

        # Final prediction
        predicted_completion = predicted_ops[-1].predicted_end if predicted_ops else datetime.utcnow()

        # Calculate confidence based on queue variability
        confidence = self._calculate_confidence(operations, cycle_times)

        # On-time probability
        on_time_prob = 1.0
        if wo.due_date:
            days_margin = (wo.due_date - predicted_completion.date()).days
            if days_margin < 0:
                on_time_prob = 0.1  # Very unlikely
            elif days_margin < 2:
                on_time_prob = 0.5
            elif days_margin < 5:
                on_time_prob = 0.75
            else:
                on_time_prob = 0.95

        # Same treatment as the work-center names above: ``WorkOrder.part_id`` is a plain
        # FK, so the part number goes through a scoped read rather than ``wo.part``. Note
        # the deliberate absence of an ``is_deleted`` predicate here -- this is the
        # identity of the part this job is building, and blanking it to "Unknown" because
        # the part was later retired would corrupt the record, not protect it. The tenant
        # predicate is the one that matters.
        part_number = "Unknown"
        if wo.part_id is not None:
            part_number = (
                self.db.query(Part.part_number)
                .filter(Part.id == wo.part_id, Part.company_id == self.company_id)
                .scalar()
                or "Unknown"
            )

        return DeliveryPrediction(
            work_order_id=wo.id,
            work_order_number=wo.work_order_number,
            part_number=part_number,
            quantity=wo.quantity_ordered,
            due_date=wo.due_date,
            predicted_completion=predicted_completion,
            confidence=round(confidence, 2),
            on_time_probability=round(on_time_prob, 2),
            operations=predicted_ops,
            bottleneck_work_center=bottleneck,
        )

    def _work_center_names(self, work_center_ids: Set[Optional[int]]) -> Dict[int, str]:
        """Resolve work-center ids to names, tenant-scoped, in one query.

        The single place this module turns a ``work_center_id`` into a string a caller
        sees. Ids belonging to another company simply do not resolve, so the render falls
        back to "Unknown" instead of disclosing a foreign machine name.
        """
        wanted = {wc_id for wc_id in work_center_ids if wc_id is not None}
        if not wanted:
            return {}

        rows = (
            self.db.query(WorkCenter.id, WorkCenter.name)
            .filter(WorkCenter.id.in_(wanted), WorkCenter.company_id == self.company_id)
            .all()
        )
        return {row.id: row.name for row in rows}

    def _get_historical_cycle_times(self) -> Dict[int, Dict[str, float]]:
        """Get average cycle times per work center from historical data."""
        # Last 90 days of completed operations
        cutoff = datetime.utcnow() - timedelta(days=90)

        results = (
            self.db.query(
                WorkOrderOperation.work_center_id,
                func.avg(WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours).label(
                    'avg_hours'
                ),
                func.avg(
                    (WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours)
                    / func.nullif(WorkOrderOperation.setup_time_hours + WorkOrderOperation.run_time_hours, 0)
                ).label('avg_ratio'),
                func.count(WorkOrderOperation.id).label('count'),
            )
            .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
            .filter(
                # Invariant 1. Grouping by ``work_center_id`` does NOT make this
                # transitively safe: the column is a plain FK that was cross-tenant-
                # writable until #194, so historically mis-parented rows mix two shops'
                # cycle times into one bucket -- and even where the FKs are clean, another
                # tenant's efficiency ratio was steering this caller's estimates through
                # ``avg_ratio``. Same reasoning #191 used to reject transitive scoping.
                WorkOrderOperation.company_id == self.company_id,
                # Invariant 3, via the job the operation belongs to. ``WorkOrderOperation``
                # has no soft-delete of its own, so its work order is the only deletion
                # dimension -- and this read did not reach it, which is why the join is
                # here rather than a bare predicate. Both sibling reads in this module
                # (``predict_delivery``'s header lookup, ``forecast_capacity``'s open-job
                # set) already filter it, so without the join the module disagreed with
                # itself about whether a deleted job exists. ``company_id`` on the joined
                # header too: ``work_order_id`` is a plain FK, so it is exactly the
                # transitive-scoping argument rejected above.
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted.is_(False),
                WorkOrderOperation.status == OperationStatus.COMPLETE,
                WorkOrderOperation.actual_end >= cutoff,
            )
            .group_by(WorkOrderOperation.work_center_id)
            .all()
        )

        return {
            r.work_center_id: {
                "avg_hours": float(r.avg_hours or 4),
                "avg_ratio": float(r.avg_ratio or 1.0),
                "count": r.count,
            }
            for r in results
        }

    def _get_queue_depths(self) -> Dict[int, int]:
        """Get number of jobs waiting at each work center.

        Counts operations on LIVE work orders only. The join is load-bearing twice over --
        see ``_get_historical_cycle_times``, which reads the same table the same way.
        """
        results = (
            self.db.query(WorkOrderOperation.work_center_id, func.count(WorkOrderOperation.id).label('queue'))
            .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
            .filter(
                # Surfaced verbatim as ``OperationPrediction.queue_position``: unscoped,
                # this disclosed how loaded every other tenant's machines are.
                WorkOrderOperation.company_id == self.company_id,
                # Invariant 3: a soft-deleted job is not work waiting at a machine. Left
                # uncounted, it inflated the caller's OWN queue depth -- and queue depth is
                # not just a displayed number, it multiplies into ``queue_wait_hours`` and
                # so into every downstream predicted date.
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted.is_(False),
                WorkOrderOperation.status.in_([OperationStatus.PENDING, OperationStatus.READY]),
            )
            .group_by(WorkOrderOperation.work_center_id)
            .all()
        )

        return {r.work_center_id: r.queue for r in results}

    def _calculate_confidence(self, operations: List[WorkOrderOperation], cycle_times: Dict) -> float:
        """Calculate confidence level based on data quality."""
        if not operations:
            return 0.5

        # More historical data = higher confidence
        total_data_points = sum(cycle_times.get(op.work_center_id, {}).get("count", 0) for op in operations)

        if total_data_points > 100:
            return 0.9
        elif total_data_points > 50:
            return 0.8
        elif total_data_points > 20:
            return 0.7
        elif total_data_points > 5:
            return 0.6
        else:
            return 0.5

    # ============ CAPACITY FORECASTING ============

    def forecast_capacity(self, weeks_ahead: int = 4) -> CapacityForecastResponse:
        """
        Forecast capacity utilization by work center for upcoming weeks.
        """
        # Invariant 1: this returned EVERY tenant's machine list, by name, to any
        # Admin/Manager/Supervisor -- and it is the default ``/analytics`` landing panel.
        work_centers = (
            self.db.query(WorkCenter)
            .filter(WorkCenter.company_id == self.company_id, WorkCenter.is_active == True)
            .all()
        )

        # Get all open/in-progress work orders. Invariant 1 + invariant 3: every tenant's
        # open jobs were feeding ``committed_hours``, and soft-deleted ones with them.
        open_wos = (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted.is_(False),
                WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]),
            )
            .all()
        )

        # Build operation hours by work center. The operations are read in ONE scoped
        # query rather than through ``wo.operations``: the relationship lazy-loaded per
        # work order (an N+1 over every open job on the shop) and, being driven off a
        # plain FK, would have pulled in a mis-parented foreign row. Ordered so the
        # float accumulation is deterministic across runs.
        ordered_qty_by_wo = {wo.id: wo.quantity_ordered for wo in open_wos}
        op_hours_by_wc: Dict[Optional[int], float] = defaultdict(float)
        if ordered_qty_by_wo:
            open_ops = (
                self.db.query(WorkOrderOperation)
                .filter(
                    WorkOrderOperation.work_order_id.in_(ordered_qty_by_wo.keys()),
                    WorkOrderOperation.company_id == self.company_id,
                    WorkOrderOperation.status != OperationStatus.COMPLETE,
                )
                .order_by(WorkOrderOperation.work_order_id, WorkOrderOperation.sequence, WorkOrderOperation.id)
                .all()
            )
            for op in open_ops:
                quantity_ordered = ordered_qty_by_wo[op.work_order_id]
                hours = op.setup_time_hours + (op.run_time_per_piece * quantity_ordered)
                # Subtract already completed portion
                if op.quantity_complete > 0:
                    hours *= 1 - op.quantity_complete / quantity_ordered
                op_hours_by_wc[op.work_center_id] += hours

        # Build weekly forecasts
        weeks = []
        alerts = []
        today = date.today()

        for week_num in range(weeks_ahead):
            week_start = today + timedelta(weeks=week_num)
            week_end = week_start + timedelta(days=6)

            wc_forecasts = []
            for wc in work_centers:
                # Available hours
                available = wc.capacity_hours_per_day * 5 * wc.efficiency_factor  # 5-day week

                # Committed hours (spread evenly across weeks for simplicity)
                committed = op_hours_by_wc.get(wc.id, 0) / weeks_ahead

                utilization = (committed / available * 100) if available > 0 else 0
                is_overloaded = utilization > 90

                wc_forecasts.append(
                    WorkCenterForecast(
                        work_center_id=wc.id,
                        work_center_name=wc.name,
                        committed_hours=round(committed, 1),
                        available_hours=round(available, 1),
                        utilization_pct=round(utilization, 1),
                        is_overloaded=is_overloaded,
                    )
                )

                if is_overloaded and week_num == 0:
                    alerts.append(
                        {
                            "type": "overload",
                            "severity": "high" if utilization > 110 else "medium",
                            "work_center": wc.name,
                            "utilization": round(utilization, 1),
                            "message": f"{wc.name} is at {round(utilization, 1)}% capacity this week",
                        }
                    )

            total_committed = sum(wc.committed_hours for wc in wc_forecasts)
            total_available = sum(wc.available_hours for wc in wc_forecasts)

            weeks.append(
                CapacityForecast(
                    week_start=week_start,
                    week_end=week_end,
                    work_centers=wc_forecasts,
                    total_committed=round(total_committed, 1),
                    total_available=round(total_available, 1),
                    overall_utilization=round(total_committed / total_available * 100, 1) if total_available > 0 else 0,
                )
            )

        return CapacityForecastResponse(weeks=weeks, alerts=alerts)

    # ============ INVENTORY DEMAND PREDICTION ============

    def predict_inventory_demand(self) -> InventoryDemandResponse:
        """
        Predict stockout dates based on:
        - Open work order demand (BOM explosion)
        - Historical usage trends
        - Open PO quantities and due dates
        """
        predictions = []

        # Get all active parts with inventory.
        # Invariant 1: this selected the part set with NO scope, which is what made the
        # whole endpoint cross-tenant end to end -- the three per-part reads below were
        # then executed for every other tenant's parts, and foreign part numbers/names
        # were rendered into ``predictions``. Invariant 3: ``Part`` carries
        # ``SoftDeleteMixin``, so retired parts were being forecast and reordered.
        parts = (
            self.db.query(Part)
            .filter(
                Part.company_id == self.company_id,
                Part.is_deleted.is_(False),
                Part.is_active == True,
                Part.part_type.in_(["purchased", "raw_material"]),
            )
            .all()
        )

        # KNOWN LIMITATION (unbounded fan-out, deliberately NOT fixed here). This loop issues
        # four to six queries PER PART -- the on-hand sum below, one or two inside
        # ``_calculate_wo_demand``, one in ``_calculate_daily_usage``, one in
        # ``_get_open_po_info`` -- and nothing bounds ``parts``. Tenant scoping shrinks that set
        # (it was previously every tenant's parts) but does NOT bound it: a single company with
        # a large purchased/raw-material catalogue still fans out linearly in the part count.
        # Note the ``predictions[:50]`` below caps the RESPONSE, not the work -- every part is
        # fully computed before the slice. This endpoint was therefore left out of the #193
        # bounding pass, which capped list/export/pagination parameters; the fix here is not a
        # request cap but batching the four per-part reads into grouped queries, which is a
        # behaviour-preserving refactor rather than a scoping change and is out of scope for a
        # tenancy fix. Revisit before this endpoint is put in front of a large catalogue.
        for part in parts:
            # Current stock. ``InventoryItem`` carries ``TenantMixin`` (no soft delete;
            # ``is_active`` is the existing liveness filter and is unchanged).
            current_stock = (
                self.db.query(func.sum(InventoryItem.quantity_on_hand))
                .filter(
                    InventoryItem.part_id == part.id,
                    InventoryItem.company_id == self.company_id,
                    InventoryItem.is_active == True,
                )
                .scalar()
                or 0
            )

            # Calculate demand from open work orders (BOM explosion). Scoped to the
            # part's OWN company -- see ``_calculate_wo_demand`` for why that argument is
            # required. The return value is not consumed by the stockout model today.
            # With the part set above now scoped, ``part.company_id`` is necessarily
            # ``self.company_id``; the argument is kept rather than dropped so the helper
            # stays independently unit-testable (see its docstring).
            self._calculate_wo_demand(part.id, part.company_id)

            # Historical daily usage (last 90 days)
            daily_usage = self._calculate_daily_usage(part.id)

            # Open PO quantities
            open_po = self._get_open_po_info(part.id)

            # Predict stockout
            if daily_usage > 0:
                days_until_stockout = int(current_stock / daily_usage)
                stockout_date = date.today() + timedelta(days=days_until_stockout)
            else:
                days_until_stockout = None
                stockout_date = None

            # Determine urgency
            if days_until_stockout is None:
                urgency = "ok"
            elif days_until_stockout <= 7:
                urgency = "critical"
            elif days_until_stockout <= 14:
                urgency = "warning"
            else:
                urgency = "ok"

            # Adjust urgency if PO is coming
            if urgency in ["critical", "warning"] and open_po["next_due"]:
                if open_po["next_due"] <= stockout_date:
                    urgency = "ok" if urgency == "warning" else "warning"

            predictions.append(
                StockoutPrediction(
                    part_id=part.id,
                    part_number=part.part_number,
                    part_name=part.name,
                    current_stock=current_stock,
                    daily_usage_rate=round(daily_usage, 2),
                    predicted_stockout_date=stockout_date,
                    days_until_stockout=days_until_stockout,
                    open_po_quantity=open_po["quantity"],
                    next_po_due=open_po["next_due"],
                    urgency=urgency,
                )
            )

        # Sort by urgency and days until stockout
        urgency_order = {"critical": 0, "warning": 1, "ok": 2}
        predictions.sort(
            key=lambda x: (urgency_order[x.urgency], x.days_until_stockout if x.days_until_stockout else 999)
        )

        critical_count = sum(1 for p in predictions if p.urgency == "critical")
        warning_count = sum(1 for p in predictions if p.urgency == "warning")

        return InventoryDemandResponse(
            predictions=predictions[:50],  # Top 50 most urgent
            critical_count=critical_count,
            warning_count=warning_count,
        )

    def _calculate_wo_demand(self, part_id: int, company_id: int) -> float:
        """Total demand for ``part_id`` from open work orders, via BOM explosion.

        ``company_id`` is REQUIRED, not optional: this was the one place in the backend that
        read ``bom_items`` WITHOUT reaching them through their ``BOM`` header -- no join, no
        ``company_id``, no ``is_deleted``. Four defects came out of those two queries.

        * **Invariant 1.** ``BOMItem`` carries ``TenantMixin``; an unfiltered read summed
          every tenant's lines (and then every tenant's open work orders) into one number.
        * **Invariant 3.** ``delete_bom`` is a SOFT delete that deliberately RETAINS the
          lines, on the documented premise that every ``BOMItem`` read reaches them through
          a header filtered on ``is_deleted == False``. This reader did not, so a deleted
          BOM's lines would have gone on generating demand forever -- where the old hard
          delete removed them. The join below is what makes that premise true; see
          ``delete_bom``'s docstring, which now enumerates the readers.
        * **``item.quantity_per`` does not exist.** The column is ``BOMItem.quantity``
          (``quantity_per`` is the BOM *response schema*'s name for it, and
          ``quantity_per_assembly`` is an RFQ-quote column). Reaching this line raised
          ``AttributeError`` -> 500 on ``GET /analytics/predict/inventory-demand`` for any
          tenant with a purchased or raw-material part on any BOM.
        * **Invariant 3 again, on the other side.** ``WorkOrder`` carries
          ``SoftDeleteMixin`` and the per-line work-order query did not filter it either, so
          a deleted job kept its material on order.

        The value this returns is CURRENTLY DISCARDED by ``predict_inventory_demand`` -- the
        stockout model runs off historical daily usage alone. Feeding open-WO demand into
        that model is a forecasting change, not a bug fix, so it is left alone here; the
        computation is kept correct and scoped so that wiring it up is a one-line change
        rather than a one-line change plus three latent defects.

        The parameter SURVIVES the service gaining a ``self.company_id``: every production
        caller now passes a company it is entitled to (the part set that reaches here is
        itself scoped, so the two are necessarily equal), but keeping it explicit leaves
        this helper unit-testable on its own and keeps the queries below reading as the
        self-contained, scoped unit #200 made them. The bodies of the two queries are
        unchanged by that PR's successor.
        """
        demand = 0.0

        # Lines that use this part, reached THROUGH their BOM header -- tenant-scoped on
        # both sides and excluding soft-deleted (and inactive) BOMs.
        # ``contains_eager`` reuses the joined header rather than lazy-loading ``item.bom``
        # once per line -- and, more importantly, makes the loaded ``item.bom`` the FILTERED
        # one, so the parent part id below can never come from a header this filter excluded.
        bom_items = (
            self.db.query(BOMItem)
            .join(BOM, BOM.id == BOMItem.bom_id)
            .options(contains_eager(BOMItem.bom))
            .filter(
                BOMItem.component_part_id == part_id,
                BOMItem.company_id == company_id,
                BOM.company_id == company_id,
                # ``is_active`` matches every other component lookup in the backend
                # (parts.py / routing.py / setup.py / the backflush ancestor walk); a BOM
                # that is not the active one for its part is not building anything.
                BOM.is_active.is_(True),
                BOM.is_deleted.is_(False),
            )
            .all()
        )

        parent_part_ids = {item.bom.part_id for item in bom_items}
        if not parent_part_ids:
            return demand

        open_wos_by_part: Dict[int, List[WorkOrder]] = defaultdict(list)
        for wo in (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.part_id.in_(parent_part_ids),
                WorkOrder.company_id == company_id,
                # ``WorkOrder`` carries ``SoftDeleteMixin`` and this read did not filter it:
                # a deleted job is not open demand.
                WorkOrder.is_deleted.is_(False),
                WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]),
            )
            .all()
        ):
            open_wos_by_part[wo.part_id].append(wo)

        for item in bom_items:
            for wo in open_wos_by_part.get(item.bom.part_id, ()):
                remaining_qty = (wo.quantity_ordered or 0) - (wo.quantity_complete or 0)
                demand += remaining_qty * float(item.quantity or 0)

        return demand

    def _calculate_daily_usage(self, part_id: int) -> float:
        """Calculate average daily NET usage from last 90 days (issues less returns).

        Drives reorder points and the MRP suggestion, so the sign is load-bearing: material
        that was issued to a job and then returned to stock with a reason (``TransactionType
        .RETURN``, PR 3) was never used, and counting it as usage makes the shop re-buy
        material that is sitting on the rack. Note that simply adding RETURN to the type
        filter would have been WORSE than leaving it out -- the ``abs()`` below turns the
        credit into MORE usage -- hence the explicit sign switch on ``transaction_type``.
        """
        cutoff = datetime.utcnow() - timedelta(days=90)

        # ISSUE -> +|quantity|, RETURN -> -|quantity|. Keyed on the type, not the stored
        # sign, so the ISSUE leg is exactly the previous expression.
        signed_usage = case(
            (InventoryTransaction.transaction_type == TransactionType.RETURN, -func.abs(InventoryTransaction.quantity)),
            else_=func.abs(InventoryTransaction.quantity),
        )
        # The gap #200 documented here is closed: the active company is now a constructor
        # argument, so this read carries its own predicate rather than relying on
        # ``part_id`` being tenant-owned. ``InventoryTransaction.part_id`` is a plain FK,
        # which is precisely the condition that makes transitive scoping unsafe (#191).
        total_issued = (
            self.db.query(func.sum(signed_usage))
            .filter(
                InventoryTransaction.part_id == part_id,
                InventoryTransaction.company_id == self.company_id,
                InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
                InventoryTransaction.created_at >= cutoff,
            )
            .scalar()
            or 0
        )

        # Clamp: the window is rolling, so material issued before the cutoff and returned
        # inside it nets negative as a pure boundary artifact. A negative daily usage would
        # push reorder points below zero. With no RETURN rows this is a sum of magnitudes
        # and the clamp is a no-op, so existing reorder points are unchanged.
        return max(0.0, float(total_issued)) / 90

    def _get_open_po_info(self, part_id: int) -> Dict[str, Any]:
        """Get open PO quantity and next due date for a part.

        Both sides carry the tenant predicate: the line (``TenantMixin``) and its header.
        ``PurchaseOrderLine`` has no ``SoftDeleteMixin`` -- its parent PO is the only
        deletion dimension, the same shape #191 settled on vendor-performance -- and a
        soft-deleted PO was previously still counted as inbound supply, which suppresses a
        real stockout warning.

        ``contains_eager`` makes the ``line.purchase_order`` read below reuse the FILTERED
        header rather than lazy-loading an unscoped one, following #200's treatment of the
        BOM join in this same module.
        """
        lines = (
            self.db.query(PurchaseOrderLine)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .options(contains_eager(PurchaseOrderLine.purchase_order))
            .filter(
                PurchaseOrderLine.part_id == part_id,
                PurchaseOrderLine.company_id == self.company_id,
                PurchaseOrder.company_id == self.company_id,
                PurchaseOrder.is_deleted.is_(False),
                PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
                PurchaseOrderLine.is_closed == False,
            )
            .all()
        )

        total_qty = sum(line.quantity_ordered - line.quantity_received for line in lines)

        # Find earliest due date
        next_due = None
        for line in lines:
            due = line.required_date or line.purchase_order.expected_date
            if due and (next_due is None or due < next_due):
                next_due = due

        return {"quantity": total_qty, "next_due": next_due}
