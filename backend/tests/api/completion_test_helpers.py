"""Shared labor-evidence seeding for tests that close an operation from the floor.

Since owner ruling 1 (2026-11-05) ``POST /shop-floor/operations/{id}/complete`` is a CLOSE
verb: it records no good pieces itself and books the operation COMPLETE at the good the
floor already recorded -- ``max(stored quantity_complete, SUM(TimeEntry.quantity_produced))``,
capped at the target. A short count is refused 409 and the operation stays open. A test
that wants a floor close therefore has to put the pieces on the floor first, the way a real
kiosk clock-out does: on a closed labor entry.

One copy of that fixture lives here so the churn files call it instead of hand-seeding
``TimeEntry`` rows (four near-identical copies already existed before this module).

* ``make_closed_time_entry`` -- one closed RUN entry carrying the given quantities.
* ``seed_recorded_good`` -- top the operation's labor evidence UP TO ``quantity`` (adds the
  missing difference on one closed entry), so the floor close finds exactly that much
  recorded good. Seeding the target makes a ``{}`` close a full-count close.

``commit=False`` flushes instead, for tests that count queries or hold an open unit of work.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation


def make_closed_time_entry(
    db: Session,
    *,
    user: User,
    op: WorkOrderOperation,
    quantity_produced: float,
    quantity_scrapped: float = 0.0,
    wo: Optional[WorkOrder] = None,
    wc: Optional[WorkCenter] = None,
    company_id: Optional[int] = None,
    commit: bool = True,
) -> TimeEntry:
    """A closed one-hour RUN entry on ``op`` holding the given good / scrap.

    ``wo`` / ``wc`` / ``company_id`` default to the operation's own; pass them only when a
    test deliberately builds an entry that disagrees with its operation.
    """
    now = datetime.utcnow()
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id if wo is not None else op.work_order_id,
        operation_id=op.id,
        work_center_id=wc.id if wc is not None else op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=now - timedelta(hours=2),
        clock_out=now - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=quantity_produced,
        quantity_scrapped=quantity_scrapped,
        company_id=company_id if company_id is not None else op.company_id,
    )
    db.add(entry)
    if commit:
        db.commit()
        db.refresh(entry)
    else:
        db.flush()
    return entry


def seed_recorded_good(
    db: Session,
    op: WorkOrderOperation,
    quantity: float,
    user: User,
    *,
    commit: bool = True,
) -> Optional[TimeEntry]:
    """Make the floor's recorded good on ``op`` reach ``quantity`` through labor evidence.

    Adds ONE closed entry carrying ``quantity - SUM(existing quantity_produced)`` when that is
    positive (so an operation that already has 6 on the floor gets a 4-piece entry for 10),
    and returns it; returns ``None`` when the evidence already covers ``quantity``. The
    operation's stored ``quantity_complete`` is left alone -- the close verb writes it.
    """
    db.flush()
    existing = (
        db.query(func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0))
        .filter(TimeEntry.operation_id == op.id)
        .scalar()
    )
    missing = float(quantity) - float(existing or 0.0)
    if missing <= 1e-9:
        return None
    return make_closed_time_entry(db, user=user, op=op, quantity_produced=missing, commit=commit)
