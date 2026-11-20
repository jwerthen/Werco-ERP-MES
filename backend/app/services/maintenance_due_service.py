"""Which preventive maintenance is due, and which is overdue: the ONE implementation.

The Maintenance dashboard's four due / overdue tiles (``GET /maintenance/dashboard``) and the daily
``check_maintenance_due_job`` alerts (``maintenance.due`` / ``maintenance.overdue``) both read it, so a
tile and an alert can never disagree about what is due.

Two kinds of item:

* **Work orders** not yet started (scheduled / overdue status), by their own ``due_date``.
* **PM schedules** that are active, carry a ``next_due_date`` and have NO work order covering them --
  one that is scheduled, overdue or in progress. A schedule with such a work order is represented by
  it, so no PM is counted twice. A PM whose work order is ON HOLD is not done, so its schedule still
  counts by the schedule's own date (owner decision 2026-11-17).

"Overdue" is a due date before the company's day; "due" is the company's day through ``window_days``
later, inclusive (7: the dashboard's week and the alerts' T-7). The caller passes the company's day
(``company_today``), never the server's UTC day.

Read-only, tenant-scoped: every query filters ``company_id``.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.maintenance import (
    COVERING_MAINTENANCE_STATUSES,
    DUE_WORK_ORDER_STATUSES,
    MaintenanceSchedule,
    MaintenanceWorkOrder,
)

DUE_WINDOW_DAYS = 7


@dataclass
class MaintenanceDue:
    today: date
    window_end: date
    schedules_overdue: List[MaintenanceSchedule] = field(default_factory=list)
    schedules_due: List[MaintenanceSchedule] = field(default_factory=list)
    work_orders_overdue: List[MaintenanceWorkOrder] = field(default_factory=list)
    work_orders_due: List[MaintenanceWorkOrder] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.schedules_overdue or self.schedules_due or self.work_orders_overdue or self.work_orders_due)


def maintenance_due(db: Session, company_id: int, today: date, *, window_days: int = DUE_WINDOW_DAYS) -> MaintenanceDue:
    """The company's due and overdue PM schedules and maintenance work orders on ``today``, each list
    ordered by due date (then id), targets loaded."""
    window_end = today + timedelta(days=window_days)
    covered_schedule_ids = select(MaintenanceWorkOrder.schedule_id).where(
        MaintenanceWorkOrder.company_id == company_id,
        MaintenanceWorkOrder.schedule_id.isnot(None),
        MaintenanceWorkOrder.status.in_(COVERING_MAINTENANCE_STATUSES),
    )
    schedules = (
        db.query(MaintenanceSchedule)
        .options(joinedload(MaintenanceSchedule.work_center), joinedload(MaintenanceSchedule.asset))
        .filter(
            MaintenanceSchedule.company_id == company_id,
            MaintenanceSchedule.is_active.is_(True),
            MaintenanceSchedule.next_due_date.isnot(None),
            MaintenanceSchedule.next_due_date <= window_end,
            MaintenanceSchedule.id.notin_(covered_schedule_ids),
        )
        .order_by(MaintenanceSchedule.next_due_date, MaintenanceSchedule.id)
        .all()
    )
    work_orders = (
        db.query(MaintenanceWorkOrder)
        .options(joinedload(MaintenanceWorkOrder.work_center), joinedload(MaintenanceWorkOrder.asset))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_(DUE_WORK_ORDER_STATUSES),
            MaintenanceWorkOrder.due_date.isnot(None),
            MaintenanceWorkOrder.due_date <= window_end,
        )
        .order_by(MaintenanceWorkOrder.due_date, MaintenanceWorkOrder.id)
        .all()
    )
    return MaintenanceDue(
        today=today,
        window_end=window_end,
        schedules_overdue=[s for s in schedules if s.next_due_date < today],
        schedules_due=[s for s in schedules if s.next_due_date >= today],
        work_orders_overdue=[wo for wo in work_orders if wo.due_date < today],
        work_orders_due=[wo for wo in work_orders if wo.due_date >= today],
    )
