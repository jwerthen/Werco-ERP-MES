"""
Constraint-Based Finite Capacity Scheduling Service

Schedules work order operations considering:
- Finite work center capacity
- Operation sequence dependencies
- Due dates and priorities
- Setup time optimization
"""
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from dataclasses import dataclass
import logging

from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.work_center import WorkCenter
from app.models.part import Part
from app.core.cache import invalidate_work_centers_cache

logger = logging.getLogger(__name__)


@dataclass
class ScheduledSlot:
    """A scheduled time slot for an operation"""
    operation_id: int
    work_center_id: int
    start_date: date
    end_date: date
    hours_required: float
    priority: int
    due_date: date


@dataclass
class WorkCenterCapacity:
    """Work center capacity tracking"""
    work_center_id: int
    hours_per_day: float
    # Dict of date -> hours used
    daily_load: Dict[date, float]


class SchedulingService:
    """Constraint-based scheduling service"""

    def __init__(self, db: Session):
        self.db = db
        self.capacity_map: Dict[int, WorkCenterCapacity] = {}

    def run_scheduling(
        self,
        work_center_ids: List[int] = None,
        horizon_days: int = 90,
        optimize_setup: bool = False,
        work_order_ids: List[int] = None
    ) -> Dict[str, any]:
        """
        Run constraint-based scheduling algorithm

        Args:
            work_center_ids: List of work centers to schedule (None = all)
            horizon_days: Scheduling horizon in days
            optimize_setup: Group similar parts to minimize setup changes
            work_order_ids: List of work orders to schedule (None = all)

        Returns:
            Dict with scheduling results
        """
        logger.info(f"Starting scheduling run: horizon={horizon_days} days, "
                   f"optimize_setup={optimize_setup}")

        # Get all work centers
        work_centers = self._get_work_centers(work_center_ids)
        self._initialize_capacity(work_centers, horizon_days)

        # Get unscheduled operations
        operations = self._get_operations_to_schedule(work_center_ids, work_order_ids)

        if not operations:
            self.update_availability_rates(
                work_center_ids=[wc.id for wc in work_centers] or None,
                horizon_days=horizon_days
            )
            return {"scheduled_count": 0, "message": "No operations to schedule"}

        # Sort operations by priority and due date
        sorted_ops = self._prioritize_operations(operations, optimize_setup)

        # Schedule operations
        scheduled = []
        conflicts = []

        for op in sorted_ops:
            result = self._schedule_operation(op, horizon_days)

            if result["success"]:
                scheduled.append(result)
            else:
                conflicts.append({
                    "operation_id": op.id,
                    "work_order": op.work_order.work_order_number,
                    "reason": result.get("reason", "Unknown")
                })

        # Commit changes
        self.update_availability_rates(
            work_center_ids=[wc.id for wc in work_centers] or None,
            horizon_days=horizon_days,
            commit=False
        )
        self.db.commit()
        invalidate_work_centers_cache()

        logger.info(f"Scheduling complete: {len(scheduled)} scheduled, "
                   f"{len(conflicts)} conflicts")

        return {
            "scheduled_count": len(scheduled),
            "conflict_count": len(conflicts),
            "scheduled_operations": scheduled,
            "conflicts": conflicts
        }

    def _get_work_centers(self, work_center_ids: List[int] = None) -> List[WorkCenter]:
        """Get work centers for scheduling"""
        query = self.db.query(WorkCenter).filter(WorkCenter.is_active == True)

        if work_center_ids:
            query = query.filter(WorkCenter.id.in_(work_center_ids))

        return query.all()

    def _initialize_capacity(self, work_centers: List[WorkCenter], horizon_days: int):
        """Initialize capacity tracking for work centers"""
        for wc in work_centers:
            self.capacity_map[wc.id] = WorkCenterCapacity(
                work_center_id=wc.id,
                hours_per_day=wc.capacity_hours_per_day or 8.0,  # Default 8 hours
                daily_load={}
            )

            # Load existing scheduled operations
            start_date = date.today()
            end_date = start_date + timedelta(days=horizon_days)

            scheduled_ops = self.db.query(WorkOrderOperation).filter(
                WorkOrderOperation.work_center_id == wc.id,
                WorkOrderOperation.scheduled_start != None,
                WorkOrderOperation.scheduled_start >= start_date,
                WorkOrderOperation.scheduled_start <= end_date,
                WorkOrderOperation.status != OperationStatus.COMPLETE
            ).all()

            # Populate daily load from existing schedule
            for op in scheduled_ops:
                self._add_to_capacity(
                    wc.id,
                    op.scheduled_start,
                    (op.setup_time_hours or 0) + (op.run_time_hours or 0)
                )

    def _get_operations_to_schedule(
        self,
        work_center_ids: List[int] = None,
        work_order_ids: List[int] = None
    ) -> List[WorkOrderOperation]:
        """Get operations that need scheduling"""
        from sqlalchemy.orm import joinedload

        query = self.db.query(WorkOrderOperation).options(
            joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
        ).join(WorkOrder).filter(
            WorkOrder.status.in_([
                WorkOrderStatus.RELEASED,
                WorkOrderStatus.IN_PROGRESS
            ]),
            WorkOrderOperation.status.in_([
                OperationStatus.PENDING,
                OperationStatus.READY
            ]),
            WorkOrderOperation.scheduled_start == None  # Unscheduled
        )

        if work_center_ids:
            query = query.filter(WorkOrderOperation.work_center_id.in_(work_center_ids))

        if work_order_ids:
            query = query.filter(WorkOrderOperation.work_order_id.in_(work_order_ids))

        return query.all()

    def _prioritize_operations(
        self,
        operations: List[WorkOrderOperation],
        optimize_setup: bool
    ) -> List[WorkOrderOperation]:
        """Sort operations by priority, due date, and optionally part grouping"""

        if optimize_setup:
            # Group by part for setup optimization
            operations.sort(key=lambda op: (
                -op.work_order.priority,  # Higher priority first (descending)
                op.work_order.due_date or date.max,  # Earlier due date first
                op.work_order.part_id,  # Group same parts together
                op.sequence  # Operation sequence
            ))
        else:
            # Standard priority + due date sorting
            operations.sort(key=lambda op: (
                -op.work_order.priority,
                op.work_order.due_date or date.max,
                op.sequence
            ))

        return operations

    def _schedule_operation(
        self,
        operation: WorkOrderOperation,
        horizon_days: int
    ) -> Dict[str, any]:
        """
        Schedule a single operation

        Returns:
            Dict with success status and details
        """
        work_center_id = operation.work_center_id
        if not work_center_id or work_center_id not in self.capacity_map:
            return {
                "success": False,
                "reason": "No work center assigned or work center not active"
            }

        # Calculate hours needed
        hours_needed = (operation.setup_time_hours or 0) + (operation.run_time_hours or 0)
        if hours_needed <= 0:
            hours_needed = 1  # Minimum 1 hour

        # Check for predecessor operations (sequence dependencies)
        earliest_start = self._get_earliest_start_date(operation)

        # Find available capacity
        scheduled_date = self._find_available_capacity(
            work_center_id,
            hours_needed,
            earliest_start,
            horizon_days
        )

        if not scheduled_date:
            return {
                "success": False,
                "reason": f"No available capacity in {horizon_days}-day horizon"
            }

        # Calculate end date
        capacity = self.capacity_map[work_center_id]
        days_needed = int(hours_needed / capacity.hours_per_day)
        if hours_needed % capacity.hours_per_day > 0:
            days_needed += 1

        end_date = scheduled_date + timedelta(days=max(0, days_needed - 1))

        # Update operation schedule
        operation.scheduled_start = scheduled_date
        operation.scheduled_end = end_date

        # Update capacity tracking
        self._add_to_capacity(work_center_id, scheduled_date, hours_needed)

        return {
            "success": True,
            "operation_id": operation.id,
            "work_order": operation.work_order.work_order_number,
            "operation": operation.operation_number,
            "scheduled_start": scheduled_date.isoformat(),
            "scheduled_end": end_date.isoformat(),
            "hours": hours_needed
        }

    def _get_earliest_start_date(self, operation: WorkOrderOperation) -> date:
        """Get earliest possible start date considering dependencies"""

        # Check if there are predecessor operations in same work order
        if operation.sequence > 10:
            # Find previous operation
            prev_op = self.db.query(WorkOrderOperation).filter(
                WorkOrderOperation.work_order_id == operation.work_order_id,
                WorkOrderOperation.sequence < operation.sequence
            ).order_by(WorkOrderOperation.sequence.desc()).first()

            if prev_op and prev_op.scheduled_end:
                prev_end = prev_op.scheduled_end.date() if isinstance(prev_op.scheduled_end, datetime) else prev_op.scheduled_end
                return prev_end + timedelta(days=1)

        # Otherwise, earliest is today
        return date.today()

    def _find_available_capacity(
        self,
        work_center_id: int,
        hours_needed: float,
        earliest_start: date,
        horizon_days: int
    ) -> Optional[date]:
        """Find first available date with sufficient capacity"""

        capacity = self.capacity_map[work_center_id]
        end_date = date.today() + timedelta(days=horizon_days)

        current_date = max(earliest_start, date.today())

        while current_date <= end_date:
            # Skip weekends (optional - could be configurable)
            if current_date.weekday() >= 5:  # Saturday or Sunday
                current_date += timedelta(days=1)
                continue

            # Check available capacity for this date
            used_hours = capacity.daily_load.get(current_date, 0)
            available_hours = capacity.hours_per_day - used_hours

            if available_hours >= hours_needed:
                return current_date

            current_date += timedelta(days=1)

        return None

    def _add_to_capacity(self, work_center_id: int, scheduled_date: date, hours: float):
        """Add hours to work center capacity tracking"""
        capacity = self.capacity_map[work_center_id]

        if isinstance(scheduled_date, datetime):
            scheduled_date = scheduled_date.date()

        if scheduled_date not in capacity.daily_load:
            capacity.daily_load[scheduled_date] = 0

        capacity.daily_load[scheduled_date] += hours

    def _count_business_days(self, start_date: date, end_date: date) -> int:
        """Count business days (Mon-Fri) between two dates inclusive"""
        count = 0
        current = start_date

        while current <= end_date:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)

        return count

    def _get_scheduled_hours_by_work_center(
        self,
        start_date: date,
        end_date: date,
        work_center_ids: Optional[List[int]] = None
    ) -> Dict[int, float]:
        """Get total scheduled hours by work center for a date range"""
        hours_expr = func.coalesce(WorkOrderOperation.setup_time_hours, 0) + func.coalesce(
            WorkOrderOperation.run_time_hours, 0
        )

        query = self.db.query(
            WorkOrderOperation.work_center_id,
            func.coalesce(func.sum(hours_expr), 0).label("scheduled_hours")
        ).filter(
            WorkOrderOperation.scheduled_start != None,
            WorkOrderOperation.scheduled_start >= start_date,
            WorkOrderOperation.scheduled_start <= end_date,
            WorkOrderOperation.status != OperationStatus.COMPLETE
        )

        if work_center_ids:
            query = query.filter(WorkOrderOperation.work_center_id.in_(work_center_ids))

        results = query.group_by(WorkOrderOperation.work_center_id).all()

        return {wc_id: float(hours or 0) for wc_id, hours in results}

    def update_availability_rates(
        self,
        work_center_ids: Optional[List[int]] = None,
        horizon_days: int = 90,
        commit: bool = True
    ) -> Dict[int, float]:
        """Update work center availability rates based on scheduled load"""
        work_centers = self._get_work_centers(work_center_ids)
        if not work_centers:
            return {}

        start_date = date.today()
        end_date = start_date + timedelta(days=horizon_days)
        business_days = self._count_business_days(start_date, end_date)
        scheduled_hours = self._get_scheduled_hours_by_work_center(
            start_date,
            end_date,
            [wc.id for wc in work_centers]
        )

        availability_rates = {}
        for wc in work_centers:
            available_hours = (wc.capacity_hours_per_day or 8.0) * business_days
            used_hours = scheduled_hours.get(wc.id, 0)

            if available_hours <= 0:
                availability = 0.0
            else:
                availability = max(0.0, min(100.0, (1 - (used_hours / available_hours)) * 100))

            wc.availability_rate = round(availability, 1)
            availability_rates[wc.id] = wc.availability_rate

        if commit:
            self.db.commit()
            invalidate_work_centers_cache()

        return availability_rates

    def get_load_chart(
        self,
        work_center_id: int,
        start_date: date,
        end_date: date
    ) -> List[Dict]:
        """
        Get work center load chart data

        Returns:
            List of daily load data points
        """
        capacity = self.capacity_map.get(work_center_id)
        if not capacity:
            # Initialize if not already done
            wc = self.db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
            if not wc:
                return []

            self._initialize_capacity([wc], (end_date - start_date).days)
            capacity = self.capacity_map[work_center_id]

        result = []
        current = start_date

        while current <= end_date:
            used_hours = capacity.daily_load.get(current, 0)
            utilization_pct = (used_hours / capacity.hours_per_day * 100) if capacity.hours_per_day > 0 else 0

            result.append({
                "date": current.isoformat(),
                "used_hours": used_hours,
                "available_hours": capacity.hours_per_day,
                "utilization_pct": round(utilization_pct, 1)
            })

            current += timedelta(days=1)

        return result

    def detect_conflicts(self, work_center_id: int = None) -> List[Dict]:
        """
        Detect scheduling conflicts (over-capacity situations)

        Returns:
            List of conflict details
        """
        conflicts = []

        wc_ids = [work_center_id] if work_center_id else list(self.capacity_map.keys())

        for wc_id in wc_ids:
            capacity = self.capacity_map.get(wc_id)
            if not capacity:
                continue

            for date_key, used_hours in capacity.daily_load.items():
                if used_hours > capacity.hours_per_day:
                    conflicts.append({
                        "work_center_id": wc_id,
                        "date": date_key.isoformat(),
                        "used_hours": used_hours,
                        "capacity_hours": capacity.hours_per_day,
                        "overload_hours": used_hours - capacity.hours_per_day,
                        "utilization_pct": round(used_hours / capacity.hours_per_day * 100, 1)
                    })

        return conflicts
