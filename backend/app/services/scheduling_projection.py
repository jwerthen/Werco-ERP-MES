"""Pure schedule projection/load helpers shared by committed and reviewed plans."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.models.work_order import OperationStatus, WorkOrderOperation
from app.services.working_calendar_service import working_hours


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
    calendars: Optional[dict] = None,
    minimum_hours: float = 0,
) -> List[Dict[str, Any]]:
    if calendars is not None:
        return _calendar_projection(
            operations, current_op, scheduled_start, work_center_id, forward_schedule, calendars, minimum_hours
        )
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
    calendars: Optional[dict] = None,
    zero_estimate_hours: float = 0,
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

        if calendars is not None and calendars.get(op.work_center_id, {}).get("version", 0) > 0:
            remaining = _operation_total_hours(op) or zero_estimate_hours
            current = start_date
            last_working = start_date
            while current <= end_date:
                capacity = working_hours(calendars, op.work_center_id, current)
                if capacity > 0:
                    hours = min(remaining, capacity)
                    load_map[current] = load_map.get(current, 0.0) + hours
                    remaining -= hours
                    last_working = current
                current += timedelta(days=1)
            # Existing work whose fixed dates cannot fit must remain visible as
            # overload, even when its entire span is now a shutdown.
            if remaining > 0:
                load_map[last_working] = load_map.get(last_working, 0.0) + remaining
            continue

        span_days = (end_date - start_date).days + 1
        total_hours = _operation_total_hours(op)
        per_day_hours = total_hours / span_days if span_days > 0 else total_hours

        current = start_date
        while current <= end_date:
            load_map[current] = load_map.get(current, 0.0) + per_day_hours
            current += timedelta(days=1)
    return load_map


def _calendar_projection(
    operations, current_op, scheduled_start, work_center_id, forward_schedule, calendars, minimum_hours=0
):
    rows = [current_op]
    if forward_schedule:
        rows += [op for op in operations if op.sequence > current_op.sequence and op.status != OperationStatus.COMPLETE]
    projected = []
    cursor = scheduled_start.date() if isinstance(scheduled_start, datetime) else scheduled_start
    for op in rows:
        center_id = (work_center_id or op.work_center_id) if op.id == current_op.id else op.work_center_id
        remaining = max(minimum_hours, _operation_total_hours(op))
        calendar = calendars.get(center_id, {})
        if calendar.get("version", 0) > 0 and not any(calendar["weekly_hours"]):
            openings = [
                date.fromisoformat(row["date"])
                for row in calendar["overrides"]
                if row["hours"] > 0 and row["date"] >= cursor.isoformat()
            ]
            if not openings or (min(openings) - cursor).days >= 3660:
                raise ValueError("No working time is configured within ten years for a required work center")
            cursor = min(openings)
        first = None
        for _ in range(3660):
            capacity = (
                working_hours(calendars, center_id, cursor)
                if calendars.get(center_id, {}).get("version", 0) > 0
                else 8.0
            )
            if capacity > 0:
                first = first or cursor
                remaining -= capacity
                if remaining <= 0:
                    break
            cursor += timedelta(days=1)
        else:
            raise ValueError("No working time is configured within ten years for a required work center")
        projected.append(
            {
                "operation": op,
                "work_center_id": center_id,
                "scheduled_start": first,
                "scheduled_end": cursor,
                "hours": max(minimum_hours, _operation_total_hours(op)),
            }
        )
        cursor += timedelta(days=1)
    return projected


def _projection_daily_load(projection, calendars):
    return _build_daily_load_for_work_center(
        [
            SimpleNamespace(
                work_center_id=projection["work_center_id"],
                scheduled_start=projection["scheduled_start"],
                scheduled_end=projection["scheduled_end"],
                setup_time_hours=projection["hours"],
                run_time_hours=0,
            )
        ],
        calendars,
    )
