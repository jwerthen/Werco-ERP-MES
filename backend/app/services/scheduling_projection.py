"""Pure schedule projection/load helpers shared by committed and reviewed plans."""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.models.work_order import OperationStatus, WorkOrderOperation


def _operation_total_hours(operation: WorkOrderOperation) -> float:
    return max(0.0, float(operation.setup_time_hours or 0) + float(operation.run_time_hours or 0))


def _days_needed_for_operation(operation: WorkOrderOperation) -> int:
    total_hours = _operation_total_hours(operation)
    return max(1, int(total_hours / 8) + (1 if total_hours % 8 > 0 else 0))


def _project_work_order_schedule(
    operations: List[WorkOrderOperation],
    current_op: WorkOrderOperation,
    scheduled_start: date,
    work_center_id: Optional[int] = None,
    forward_schedule: bool = False,
) -> List[Dict[str, Any]]:
    projected_ops = []
    current_work_center_id = work_center_id or current_op.work_center_id
    current_days = _days_needed_for_operation(current_op)
    current_end = scheduled_start + timedelta(days=current_days - 1)
    projected_ops.append(
        {
            "operation": current_op,
            "work_center_id": current_work_center_id,
            "scheduled_start": scheduled_start,
            "scheduled_end": current_end,
            "hours": _operation_total_hours(current_op),
        }
    )

    if not forward_schedule:
        return projected_ops

    prev_end = current_end
    for op in operations:
        if op.sequence <= current_op.sequence:
            continue
        if op.status == OperationStatus.COMPLETE:
            continue
        op_start = prev_end + timedelta(days=1)
        op_days = _days_needed_for_operation(op)
        op_end = op_start + timedelta(days=op_days - 1)
        projected_ops.append(
            {
                "operation": op,
                "work_center_id": op.work_center_id,
                "scheduled_start": op_start,
                "scheduled_end": op_end,
                "hours": _operation_total_hours(op),
            }
        )
        prev_end = op_end

    return projected_ops


def _build_daily_load_for_work_center(
    operations: List[WorkOrderOperation],
) -> Dict[date, float]:
    load_map: Dict[date, float] = {}
    for op in operations:
        if not op.scheduled_start:
            continue
        start_date = op.scheduled_start.date() if isinstance(op.scheduled_start, datetime) else op.scheduled_start
        end_date = start_date
        if op.scheduled_end:
            end_date = op.scheduled_end.date() if isinstance(op.scheduled_end, datetime) else op.scheduled_end
        if end_date < start_date:
            end_date = start_date

        span_days = (end_date - start_date).days + 1
        total_hours = _operation_total_hours(op)
        per_day_hours = total_hours / span_days if span_days > 0 else total_hours

        current = start_date
        while current <= end_date:
            load_map[current] = load_map.get(current, 0.0) + per_day_hours
            current += timedelta(days=1)
    return load_map
