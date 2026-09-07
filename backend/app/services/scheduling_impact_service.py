"""Read-only schedule proposals and exact, tenant-bound reviewed-plan application.

No preview rows are persisted. A short-lived signed token carries the reviewed
operation changes and both state fingerprints; it cannot be used as an auth token.
"""

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.cache import invalidate_work_centers_cache
from app.core.config import settings
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.scheduling import SchedulingImpactRequest
from app.services.audit_service import AuditService
from app.services.scheduling_projection import (
    _build_daily_load_for_work_center,
    _project_work_order_schedule,
    _projection_daily_load,
)
from app.services.scheduling_service import SchedulingService
from app.services.working_calendar_service import load_working_calendars, working_hours

TERMINAL = (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED)
PLAN_TTL_SECONDS = 600
MAX_PLAN_OPERATIONS = 1000


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return datetime.combine(value, datetime.min.time()).isoformat()


def _day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def _value(value):
    return value.value if hasattr(value, "value") else value


def _fingerprint(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _signing_key() -> str:
    return hashlib.sha256((settings.SECRET_KEY + ":scheduling-impact-v1").encode()).hexdigest()


def _clone_operation(op: WorkOrderOperation):
    return SimpleNamespace(
        id=op.id,
        work_order_id=op.work_order_id,
        work_center_id=op.work_center_id,
        sequence=op.sequence,
        status=op.status,
        scheduled_start=op.scheduled_start,
        scheduled_end=op.scheduled_end,
        setup_time_hours=op.setup_time_hours,
        run_time_hours=op.run_time_hours,
    )


@dataclass
class ScheduleState:
    selected_ids: List[int]
    centers: Dict[int, WorkCenter]
    orders: Dict[int, WorkOrder]
    operations: Dict[int, WorkOrderOperation]
    calendars: dict

    def snapshot(self) -> dict:
        return {
            "calendars": [self.calendars[key] for key in sorted(self.calendars)],
            "orders": [
                {
                    "id": wo.id,
                    "version": wo.version,
                    "status": _value(wo.status),
                    "deleted": wo.is_deleted,
                    "priority": wo.priority,
                    "quantity": wo.quantity_ordered,
                    "due_date": wo.due_date.isoformat() if wo.due_date else None,
                    "number": wo.work_order_number,
                }
                for wo in sorted(self.orders.values(), key=lambda row: row.id)
            ],
            "centers": [
                {
                    "id": wc.id,
                    "active": wc.is_active,
                    "code": wc.code,
                    "capacity": float(wc.capacity_hours_per_day or 8.0),
                }
                for wc in sorted(self.centers.values(), key=lambda row: row.id)
            ],
            "operations": [
                {
                    "id": op.id,
                    "version": op.version,
                    "work_order_id": op.work_order_id,
                    "work_center_id": op.work_center_id,
                    "sequence": op.sequence,
                    "name": op.name,
                    "number": op.operation_number,
                    "status": _value(op.status),
                    "start": _iso(op.scheduled_start),
                    "end": _iso(op.scheduled_end),
                    "setup": float(op.setup_time_hours or 0),
                    "run": float(op.run_time_hours or 0),
                }
                for op in sorted(self.operations.values(), key=lambda row: row.id)
            ],
        }


class SchedulingImpactService:
    def __init__(self, db: Session, company_id: int, actor_id: int):
        self.db = db
        self.company_id = company_id
        self.actor_id = actor_id

    def _load_state(self, ids: List[int], center_ids: Optional[List[int]] = None) -> ScheduleState:
        selected = (
            self.db.query(WorkOrder)
            .filter(WorkOrder.company_id == self.company_id, WorkOrder.id.in_(ids))
            .populate_existing()
            .all()
        )
        if len(selected) != len(ids):
            raise HTTPException(404, "One or more work orders were not found")
        selected_ops = (
            self.db.query(WorkOrderOperation)
            .filter(WorkOrderOperation.company_id == self.company_id, WorkOrderOperation.work_order_id.in_(ids))
            .populate_existing()
            .all()
        )
        if center_ids is None:
            center_ids = sorted({op.work_center_id for op in selected_ops if op.status != OperationStatus.COMPLETE})
        centers = (
            self.db.query(WorkCenter)
            .filter(WorkCenter.company_id == self.company_id, WorkCenter.id.in_(center_ids))
            .populate_existing()
            .all()
        )
        # Include currently inactive/finished rows too: reopening an existing job or
        # operation can consume capacity without changing its center FK. Apply must
        # lock those dependencies as well as the currently visible schedule.
        relevant_ops = (
            self.db.query(WorkOrderOperation)
            .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
            .filter(
                WorkOrderOperation.company_id == self.company_id,
                WorkOrder.company_id == self.company_id,
                WorkOrderOperation.work_center_id.in_(center_ids),
            )
            .populate_existing()
            .all()
        )
        operations = {op.id: op for op in [*relevant_ops, *selected_ops]}
        order_ids = {op.work_order_id for op in operations.values()} | set(ids)
        orders = (
            self.db.query(WorkOrder)
            .filter(WorkOrder.company_id == self.company_id, WorkOrder.id.in_(order_ids))
            .populate_existing()
            .all()
        )
        return ScheduleState(
            ids,
            {wc.id: wc for wc in centers},
            {wo.id: wo for wo in orders},
            operations,
            load_working_calendars(self.db, self.company_id, center_ids),
        )

    @staticmethod
    def _loads(state: ScheduleState, operations: dict) -> dict:
        loads = {}
        for center_id in state.centers:
            eligible = [
                op
                for op in operations.values()
                if op.work_center_id == center_id
                and op.status != OperationStatus.COMPLETE
                and not state.orders[op.work_order_id].is_deleted
                and state.orders[op.work_order_id].status not in TERMINAL
            ]
            loads[center_id] = _build_daily_load_for_work_center(eligible, state.calendars)
        return loads

    def preview(self, request: SchedulingImpactRequest) -> dict:
        state = self._load_state(request.work_order_ids)
        before = state.snapshot()
        simulated = {op.id: _clone_operation(op) for op in state.operations.values()}
        before_loads = self._loads(state, simulated)
        jobs = []
        changes = []
        today = datetime.now(ZoneInfo("America/Chicago")).date()
        for order_id in request.work_order_ids:
            wo = state.orders[order_id]
            ops = sorted(
                [op for op in simulated.values() if op.work_order_id == order_id], key=lambda op: (op.sequence, op.id)
            )
            remaining = [op for op in ops if op.status != OperationStatus.COMPLETE]
            existing_finish = max(
                (_day(op.scheduled_end or op.scheduled_start) for op in remaining if op.scheduled_start), default=None
            )
            # An unscheduled remaining step means the overall completion date is unknown.
            if any(not op.scheduled_start for op in remaining):
                existing_finish = None
            item = {
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "due_date": wo.due_date.isoformat() if wo.due_date else None,
                "before_finish": existing_finish.isoformat() if existing_finish else None,
                "after_finish": existing_finish.isoformat() if existing_finish else None,
                "before_late_days": (
                    max(0, (existing_finish - wo.due_date).days) if existing_finish and wo.due_date else None
                ),
                "late_days": None,
                "outcome": "changed",
                "reason": None,
                "operations": [],
            }
            jobs.append(item)
            if wo.is_deleted or wo.status in TERMINAL or not remaining:
                item.update(
                    outcome="blocked", reason="Work order is deleted, finished, or has no remaining operations."
                )
                continue
            if any(
                op.work_center_id not in state.centers or not state.centers[op.work_center_id].is_active
                for op in remaining
            ):
                item.update(
                    outcome="blocked", reason="A remaining operation has no active work center in this company."
                )
                continue
            current = remaining[0]
            if request.action == "earliest" and current.scheduled_start:
                item.update(
                    outcome="skipped",
                    reason="Current operation is already scheduled; use Shift Dates to move its plan.",
                )
                continue
            if request.action == "shift":
                projections = []
                try:
                    previous_end = None
                    for op in remaining:
                        if not op.scheduled_start:
                            continue
                        target = _day(op.scheduled_start) + timedelta(days=request.shift_days)
                        if previous_end is not None:
                            target = max(target, previous_end + timedelta(days=1))
                        if state.calendars.get(op.work_center_id, {}).get("version", 0) == 0:
                            projection = {
                                "operation": op,
                                "work_center_id": op.work_center_id,
                                "scheduled_start": target,
                                "scheduled_end": target
                                + (_day(op.scheduled_end or op.scheduled_start) - _day(op.scheduled_start)),
                                "hours": float(op.setup_time_hours or 0) + float(op.run_time_hours or 0),
                            }
                        else:
                            projection = _project_work_order_schedule(
                                [op], op, target, op.work_center_id, False, state.calendars
                            )[0]
                        previous_end = projection["scheduled_end"]
                        projections.append(projection)
                except ValueError as error:
                    item.update(outcome="blocked", reason=str(error))
                    continue
                if not projections:
                    item.update(outcome="skipped", reason="No scheduled remaining operations to shift.")
                    continue
            else:
                others = {key: op for key, op in simulated.items() if op.work_order_id != wo.id}
                base_loads = self._loads(state, others)
                projections = []
                calendar_error = None
                for offset in range(request.horizon_days):
                    try:
                        candidate = _project_work_order_schedule(
                            ops, current, today + timedelta(days=offset), current.work_center_id, True, state.calendars
                        )
                    except ValueError as error:
                        calendar_error = str(error)
                        break
                    if candidate[-1]["scheduled_end"] >= today + timedelta(days=request.horizon_days):
                        break
                    candidate_loads = {key: dict(value) for key, value in base_loads.items()}
                    fits = True
                    for projection in candidate:
                        center_id = projection["work_center_id"]
                        for day, hours in _projection_daily_load(projection, state.calendars).items():
                            capacity = working_hours(state.calendars, center_id, day)
                            new_load = candidate_loads.setdefault(center_id, {}).get(day, 0) + hours
                            candidate_loads[center_id][day] = new_load
                            if new_load > capacity + 0.000001:
                                fits = False
                    if fits:
                        projections = candidate
                        break
                if not projections:
                    item.update(
                        outcome="blocked",
                        reason=calendar_error
                        or f"No available working capacity within {request.horizon_days} days. Adjust the calendar/capacity or review a manual date.",
                    )
                    continue
            for projection in projections:
                op = projection["operation"]
                original = state.operations[op.id]
                after_start = datetime.combine(projection["scheduled_start"], datetime.min.time())
                after_end = datetime.combine(projection["scheduled_end"], datetime.min.time())
                after_status = original.status
                if (
                    op.id == current.id
                    and wo.status in (WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS)
                    and original.status == OperationStatus.PENDING
                ):
                    after_status = OperationStatus.READY
                if (_iso(original.scheduled_start), _iso(original.scheduled_end), original.status) == (
                    _iso(after_start),
                    _iso(after_end),
                    after_status,
                ):
                    continue
                change = {
                    "operation_id": op.id,
                    "operation_number": original.operation_number,
                    "operation_name": original.name,
                    "work_center_id": op.work_center_id,
                    "work_center_code": state.centers[op.work_center_id].code,
                    "before_start": _iso(original.scheduled_start),
                    "before_end": _iso(original.scheduled_end),
                    "after_start": _iso(after_start),
                    "after_end": _iso(after_end),
                    "before_status": _value(original.status),
                    "after_status": _value(after_status),
                }
                changes.append(change)
                item["operations"].append(change)
                op.scheduled_start, op.scheduled_end, op.status = after_start, after_end, after_status
            if not item["operations"]:
                item.update(outcome="skipped", reason="The reviewed dates already match the current schedule.")
            finish = max(
                (_day(op.scheduled_end or op.scheduled_start) for op in remaining if op.scheduled_start), default=None
            )
            if any(not op.scheduled_start for op in remaining):
                finish = None
            item["after_finish"] = finish.isoformat() if finish else None
            item["late_days"] = max(0, (finish - wo.due_date).days) if finish and wo.due_date else None
        if len(changes) > MAX_PLAN_OPERATIONS:
            raise HTTPException(422, "This plan contains too many operations. Review a smaller selection.")
        after_loads = self._loads(state, simulated)
        capacity_rows = []
        changed_centers = {row["work_center_id"] for row in changes}
        for center_id in sorted(changed_centers):
            wc = state.centers[center_id]
            touched_days = set()
            for change in changes:
                if change["work_center_id"] != center_id:
                    continue
                for prefix in ("before", "after"):
                    start = _day(change[f"{prefix}_start"])
                    end = _day(change[f"{prefix}_end"]) or start
                    if start:
                        touched_days.update(
                            start + timedelta(days=offset) for offset in range(max(0, (end - start).days) + 1)
                        )
            for day in sorted(touched_days):
                capacity = working_hours(state.calendars, center_id, day)
                old_hours = before_loads[center_id].get(day, 0)
                new_hours = after_loads[center_id].get(day, 0)
                if max(old_hours, new_hours) <= capacity + 0.000001:
                    continue
                affected = {
                    op.work_order_id
                    for op in simulated.values()
                    if op.work_center_id == center_id
                    and op.status != OperationStatus.COMPLETE
                    and op.scheduled_start
                    and _day(op.scheduled_start) <= day <= _day(op.scheduled_end or op.scheduled_start)
                    and state.orders[op.work_order_id].status not in TERMINAL
                    and not state.orders[op.work_order_id].is_deleted
                }
                capacity_rows.append(
                    {
                        "work_center_id": center_id,
                        "work_center_code": wc.code,
                        "date": day.isoformat(),
                        "capacity_hours": round(capacity, 2),
                        "before_hours": round(old_hours, 2),
                        "after_hours": round(new_hours, 2),
                        "overload_hours": round(max(0, new_hours - capacity), 2),
                        "affected_jobs": [
                            {"work_order_id": key, "work_order_number": state.orders[key].work_order_number}
                            for key in sorted(affected)
                        ],
                    }
                )
        expected_after = copy.deepcopy(before)
        by_id = {row["id"]: row for row in expected_after["operations"]}
        for change in changes:
            row = by_id[change["operation_id"]]
            row.update(
                start=change["after_start"],
                end=change["after_end"],
                status=change["after_status"],
                version=row["version"] + 1,
            )
        expires = datetime.now(timezone.utc) + timedelta(seconds=PLAN_TTL_SECONDS)
        claims = {
            "purpose": "scheduling-impact-v1",
            "company_id": self.company_id,
            "actor_id": self.actor_id,
            "exp": expires,
            "selected_ids": request.work_order_ids,
            "center_ids": sorted(state.centers),
            "before": _fingerprint(before),
            "after": _fingerprint(expected_after),
            "changes": [
                {key: change[key] for key in ("operation_id", "after_start", "after_end", "after_status")}
                for change in changes
            ],
        }
        return {
            "action": request.action,
            "shift_days": request.shift_days,
            "plan_token": jwt.encode(claims, _signing_key(), algorithm="HS256") if changes else None,
            "expires_at": expires.isoformat(),
            "jobs": jobs,
            "capacity": capacity_rows,
            "summary": {
                "selected_jobs": len(jobs),
                "changed_jobs": sum(item["outcome"] == "changed" for item in jobs),
                "changed_operations": len(changes),
                "skipped_jobs": sum(item["outcome"] == "skipped" for item in jobs),
                "blocked_jobs": sum(item["outcome"] == "blocked" for item in jobs),
                "late_jobs": sum(bool(item["late_days"]) for item in jobs),
                "overloaded_days": sum(row["overload_hours"] > 0 for row in capacity_rows),
            },
        }

    def apply(self, token: str, audit: AuditService) -> dict:
        try:
            claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
        except JWTError:
            raise HTTPException(409, "This scheduling plan expired or is invalid. Generate and review a new preview.")
        if (
            claims.get("purpose") != "scheduling-impact-v1"
            or claims.get("company_id") != self.company_id
            or claims.get("actor_id") != self.actor_id
        ):
            raise HTTPException(403, "This scheduling plan belongs to another user or company.")
        ids, center_ids = claims["selected_ids"], claims["center_ids"]
        state = self._load_state(ids, center_ids)
        # Short apply-only locks, consistently ordered parent -> center -> operation.
        # Center FOR UPDATE locks also hold FK inserts/moves until validation+commit.
        self.db.query(WorkOrder).filter(
            WorkOrder.company_id == self.company_id, WorkOrder.id.in_(state.orders)
        ).order_by(WorkOrder.id).with_for_update().all()
        self.db.query(WorkCenter).filter(
            WorkCenter.company_id == self.company_id, WorkCenter.id.in_(center_ids)
        ).order_by(WorkCenter.id).with_for_update().all()
        self.db.query(WorkOrderOperation).filter(
            WorkOrderOperation.company_id == self.company_id, WorkOrderOperation.id.in_(state.operations)
        ).order_by(WorkOrderOperation.id).with_for_update().all()
        state = self._load_state(ids, center_ids)
        fingerprint = _fingerprint(state.snapshot())
        applied_ids = sorted(
            {
                state.operations[row["operation_id"]].work_order_id
                for row in claims["changes"]
                if row["operation_id"] in state.operations
            }
        )
        if fingerprint == claims["after"]:
            return {
                "message": "This reviewed plan is already applied.",
                "already_applied": True,
                "applied_work_order_ids": applied_ids,
                "changed_operations": len(claims["changes"]),
            }
        if fingerprint != claims["before"]:
            raise HTTPException(
                409,
                "The schedule, work orders, or capacity changed after this preview. Nothing was applied. Generate and review a new preview.",
            )
        for change in claims["changes"]:
            op = state.operations[change["operation_id"]]
            old = {
                "scheduled_start": _iso(op.scheduled_start),
                "scheduled_end": _iso(op.scheduled_end),
                "status": _value(op.status),
            }
            op.scheduled_start = datetime.fromisoformat(change["after_start"])
            op.scheduled_end = datetime.fromisoformat(change["after_end"])
            op.status = OperationStatus(change["after_status"])
            audit.log_update(
                resource_type="work_order_operation",
                resource_id=op.id,
                resource_identifier=op.operation_number,
                old_values=old,
                new_values={
                    "scheduled_start": change["after_start"],
                    "scheduled_end": change["after_end"],
                    "status": change["after_status"],
                },
                description="Applied reviewed scheduling plan",
                extra_data={"work_order_id": op.work_order_id, "via": "impact_plan"},
            )
        self.db.flush()
        SchedulingService(self.db, self.company_id).update_availability_rates(
            work_center_ids=center_ids, horizon_days=90, commit=False
        )
        self.db.commit()
        invalidate_work_centers_cache()
        return {
            "message": f"Applied reviewed schedule to {len(applied_ids)} work orders.",
            "already_applied": False,
            "applied_work_order_ids": applied_ids,
            "changed_operations": len(claims["changes"]),
        }
