"""
Prediction Service - Delivery dates, capacity forecasting, inventory demand
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from collections import defaultdict

from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.work_center import WorkCenter
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, POStatus
from app.models.part import Part
from app.models.bom import BOM, BOMItem
from app.schemas.analytics import (
    DeliveryPrediction, OperationPrediction,
    CapacityForecast, CapacityForecastResponse, WorkCenterForecast,
    StockoutPrediction, InventoryDemandResponse
)

logger = logging.getLogger(__name__)

# Default hours per week per work center
DEFAULT_HOURS_PER_WEEK = 40


class PredictionService:
    def __init__(self, db: Session):
        self.db = db
    
    # ============ DELIVERY PREDICTION ============
    
    def predict_delivery(self, work_order_id: int) -> DeliveryPrediction:
        """
        Predict completion date for a work order based on:
        - Historical cycle times per work center
        - Current queue depth at each work center
        - Operation sequence
        """
        wo = self.db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
        if not wo:
            raise ValueError(f"Work order {work_order_id} not found")
        
        operations = self.db.query(WorkOrderOperation).filter(
            WorkOrderOperation.work_order_id == work_order_id
        ).order_by(WorkOrderOperation.sequence).all()
        
        if not operations:
            raise ValueError(f"No operations found for work order {work_order_id}")
        
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
                predicted_ops.append(OperationPrediction(
                    operation_id=op.id,
                    operation_name=op.name,
                    work_center_name=op.work_center.name if op.work_center else "Unknown",
                    predicted_start=op.actual_start or current_time,
                    predicted_end=op.actual_end or current_time,
                    queue_position=0,
                    estimated_hours=op.actual_run_hours + op.actual_setup_hours
                ))
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
                bottleneck = op.work_center.name if op.work_center else None
            
            # Calculate start and end times (8-hour work days)
            queue_wait_days = queue_wait_hours / 8
            op_days = est_hours / 8
            
            predicted_start = current_time + timedelta(days=queue_wait_days)
            predicted_end = predicted_start + timedelta(days=op_days)
            
            predicted_ops.append(OperationPrediction(
                operation_id=op.id,
                operation_name=op.name,
                work_center_name=op.work_center.name if op.work_center else "Unknown",
                predicted_start=predicted_start,
                predicted_end=predicted_end,
                queue_position=queue_depth,
                estimated_hours=est_hours
            ))
            
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
        
        return DeliveryPrediction(
            work_order_id=wo.id,
            work_order_number=wo.work_order_number,
            part_number=wo.part.part_number if wo.part else "Unknown",
            quantity=wo.quantity_ordered,
            due_date=wo.due_date,
            predicted_completion=predicted_completion,
            confidence=round(confidence, 2),
            on_time_probability=round(on_time_prob, 2),
            operations=predicted_ops,
            bottleneck_work_center=bottleneck
        )
    
    def _get_historical_cycle_times(self) -> Dict[int, Dict[str, float]]:
        """Get average cycle times per work center from historical data."""
        # Last 90 days of completed operations
        cutoff = datetime.utcnow() - timedelta(days=90)
        
        results = self.db.query(
            WorkOrderOperation.work_center_id,
            func.avg(WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours).label('avg_hours'),
            func.avg(
                (WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours) /
                func.nullif(WorkOrderOperation.setup_time_hours + WorkOrderOperation.run_time_hours, 0)
            ).label('avg_ratio'),
            func.count(WorkOrderOperation.id).label('count')
        ).filter(
            WorkOrderOperation.status == OperationStatus.COMPLETE,
            WorkOrderOperation.actual_end >= cutoff
        ).group_by(WorkOrderOperation.work_center_id).all()
        
        return {
            r.work_center_id: {
                "avg_hours": float(r.avg_hours or 4),
                "avg_ratio": float(r.avg_ratio or 1.0),
                "count": r.count
            }
            for r in results
        }
    
    def _get_queue_depths(self) -> Dict[int, int]:
        """Get number of jobs waiting at each work center."""
        results = self.db.query(
            WorkOrderOperation.work_center_id,
            func.count(WorkOrderOperation.id).label('queue')
        ).filter(
            WorkOrderOperation.status.in_([OperationStatus.PENDING, OperationStatus.READY])
        ).group_by(WorkOrderOperation.work_center_id).all()
        
        return {r.work_center_id: r.queue for r in results}
    
    def _calculate_confidence(self, operations: List[WorkOrderOperation], cycle_times: Dict) -> float:
        """Calculate confidence level based on data quality."""
        if not operations:
            return 0.5
        
        # More historical data = higher confidence
        total_data_points = sum(
            cycle_times.get(op.work_center_id, {}).get("count", 0)
            for op in operations
        )
        
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
        work_centers = self.db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
        
        # Get all open/in-progress work orders
        open_wos = self.db.query(WorkOrder).filter(
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
        ).all()
        
        # Build operation hours by work center
        op_hours_by_wc = defaultdict(float)
        for wo in open_wos:
            for op in wo.operations:
                if op.status != OperationStatus.COMPLETE:
                    hours = op.setup_time_hours + (op.run_time_per_piece * wo.quantity_ordered)
                    # Subtract already completed portion
                    if op.quantity_complete > 0:
                        hours *= (1 - op.quantity_complete / wo.quantity_ordered)
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
                
                wc_forecasts.append(WorkCenterForecast(
                    work_center_id=wc.id,
                    work_center_name=wc.name,
                    committed_hours=round(committed, 1),
                    available_hours=round(available, 1),
                    utilization_pct=round(utilization, 1),
                    is_overloaded=is_overloaded
                ))
                
                if is_overloaded and week_num == 0:
                    alerts.append({
                        "type": "overload",
                        "severity": "high" if utilization > 110 else "medium",
                        "work_center": wc.name,
                        "utilization": round(utilization, 1),
                        "message": f"{wc.name} is at {round(utilization, 1)}% capacity this week"
                    })
            
            total_committed = sum(wc.committed_hours for wc in wc_forecasts)
            total_available = sum(wc.available_hours for wc in wc_forecasts)
            
            weeks.append(CapacityForecast(
                week_start=week_start,
                week_end=week_end,
                work_centers=wc_forecasts,
                total_committed=round(total_committed, 1),
                total_available=round(total_available, 1),
                overall_utilization=round(total_committed / total_available * 100, 1) if total_available > 0 else 0
            ))
        
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
        
        # Get all active parts with inventory
        parts = self.db.query(Part).filter(
            Part.is_active == True,
            Part.part_type.in_(["purchased", "raw_material"])
        ).all()
        
        for part in parts:
            # Current stock
            current_stock = self.db.query(
                func.sum(InventoryItem.quantity_on_hand)
            ).filter(
                InventoryItem.part_id == part.id,
                InventoryItem.is_active == True
            ).scalar() or 0
            
            # Calculate demand from open work orders (BOM explosion)
            wo_demand = self._calculate_wo_demand(part.id)
            
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
            
            predictions.append(StockoutPrediction(
                part_id=part.id,
                part_number=part.part_number,
                part_name=part.name,
                current_stock=current_stock,
                daily_usage_rate=round(daily_usage, 2),
                predicted_stockout_date=stockout_date,
                days_until_stockout=days_until_stockout,
                open_po_quantity=open_po["quantity"],
                next_po_due=open_po["next_due"],
                urgency=urgency
            ))
        
        # Sort by urgency and days until stockout
        urgency_order = {"critical": 0, "warning": 1, "ok": 2}
        predictions.sort(key=lambda x: (
            urgency_order[x.urgency],
            x.days_until_stockout if x.days_until_stockout else 999
        ))
        
        critical_count = sum(1 for p in predictions if p.urgency == "critical")
        warning_count = sum(1 for p in predictions if p.urgency == "warning")
        
        return InventoryDemandResponse(
            predictions=predictions[:50],  # Top 50 most urgent
            critical_count=critical_count,
            warning_count=warning_count
        )
    
    def _calculate_wo_demand(self, part_id: int) -> float:
        """Calculate total demand from open work orders via BOM explosion."""
        demand = 0.0
        
        # Get BOMs that use this part
        bom_items = self.db.query(BOMItem).filter(
            BOMItem.component_part_id == part_id
        ).all()
        
        for item in bom_items:
            # Find open work orders for the parent part
            wos = self.db.query(WorkOrder).filter(
                WorkOrder.part_id == item.bom.part_id,
                WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
            ).all()
            
            for wo in wos:
                remaining_qty = wo.quantity_ordered - wo.quantity_complete
                demand += remaining_qty * item.quantity_per
        
        return demand
    
    def _calculate_daily_usage(self, part_id: int) -> float:
        """Calculate average daily usage from last 90 days."""
        cutoff = datetime.utcnow() - timedelta(days=90)
        
        total_issued = self.db.query(
            func.sum(func.abs(InventoryTransaction.quantity))
        ).filter(
            InventoryTransaction.part_id == part_id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.created_at >= cutoff
        ).scalar() or 0
        
        return total_issued / 90
    
    def _get_open_po_info(self, part_id: int) -> Dict[str, Any]:
        """Get open PO quantity and next due date for a part."""
        lines = self.db.query(PurchaseOrderLine).join(PurchaseOrder).filter(
            PurchaseOrderLine.part_id == part_id,
            PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
            PurchaseOrderLine.is_closed == False
        ).all()
        
        total_qty = sum(line.quantity_ordered - line.quantity_received for line in lines)
        
        # Find earliest due date
        next_due = None
        for line in lines:
            due = line.required_date or line.purchase_order.expected_date
            if due and (next_due is None or due < next_due):
                next_due = due
        
        return {
            "quantity": total_qty,
            "next_due": next_due
        }
