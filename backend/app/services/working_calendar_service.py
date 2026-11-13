"""One date-level capacity model for projections, previews and committed plans."""

from dataclasses import dataclass
from datetime import date
from typing import List

from fastapi import HTTPException

from app.models.work_center import WorkCenter
from app.models.working_calendar import WorkingCalendar


def load_working_calendars(db, company_id, center_ids=None):
    centers_query = db.query(WorkCenter).filter(WorkCenter.company_id == company_id)
    rows_query = db.query(WorkingCalendar).filter(WorkingCalendar.company_id == company_id)
    if center_ids is not None:
        centers_query = centers_query.filter(WorkCenter.id.in_(center_ids))
        rows_query = rows_query.filter(WorkingCalendar.work_center_id.in_(center_ids))
    rows = {row.work_center_id: row for row in rows_query.populate_existing().all()}
    result = {}
    for center in centers_query.populate_existing().all():
        row = rows.get(center.id)
        result[center.id] = {
            "work_center_id": center.id,
            "version": row.version if row else 0,
            # Unconfigured centers keep their existing every-day capacity.
            "weekly_hours": row.weekly_hours if row else [float(center.capacity_hours_per_day or 8)] * 7,
            "overrides": row.overrides if row else [],
        }
    return result


def working_hours(calendars, center_id, day: date, default=8.0):
    calendar = (calendars or {}).get(center_id)
    if calendar is None:
        return float(default)
    stamp = day.isoformat()
    for override in calendar["overrides"]:
        if override["date"] == stamp:
            return float(override["hours"])
    return float(calendar["weekly_hours"][day.weekday()])


WORKING_CALENDAR_CONFLICT_DETAIL = "This working calendar changed. Reload it before saving your edits."


@dataclass
class WorkingCalendarSave:
    """One flushed working-calendar write, ready for its audit row."""

    row: WorkingCalendar
    center: WorkCenter
    before: dict
    after: dict

    @property
    def created(self) -> bool:
        return self.before["version"] == 0


def save_working_calendar(
    db,
    company_id: int,
    center: WorkCenter,
    *,
    weekly_hours: List[float],
    overrides: List[dict],
    expected_version: int,
    user_id: int,
) -> WorkingCalendarSave:
    """Write one work center's weekly hours + dated overrides after a version check; flushes, never commits.

    THE one writer of ``working_calendars`` rows, shared by ``PUT
    /scheduling/work-centers/{id}/calendar`` and the production-calendar sync. The
    caller resolves ``center`` tenant-scoped (and row-locks it), writes the audit row
    for the returned save (:func:`audit_working_calendar_save`) and commits. A stale
    ``expected_version`` is HTTP 409 before anything is written. ``overrides`` are
    stored sorted by date; ``version`` is bumped and ``updated_by`` stamped.
    """
    before = load_working_calendars(db, company_id, [center.id])[center.id]
    if before["version"] != expected_version:
        raise HTTPException(409, WORKING_CALENDAR_CONFLICT_DETAIL)
    row = (
        db.query(WorkingCalendar)
        .filter(
            WorkingCalendar.company_id == company_id,
            WorkingCalendar.work_center_id == center.id,
        )
        .populate_existing()
        .first()
    )
    if row is None:
        row = WorkingCalendar(company_id=company_id, work_center_id=center.id, version=0)
        db.add(row)
    row.weekly_hours = list(weekly_hours)
    row.overrides = sorted(overrides, key=lambda entry: entry["date"])
    row.version += 1
    row.updated_by = user_id
    db.flush()
    after = load_working_calendars(db, company_id, [center.id])[center.id]
    return WorkingCalendarSave(row=row, center=center, before=before, after=after)


def audit_working_calendar_save(audit, saved: WorkingCalendarSave, *, required: bool = False, extra_data=None):
    """The audit row for one :func:`save_working_calendar`: CREATE on a first save, UPDATE after.

    ``required=False`` keeps the scheduling editor's long-standing best-effort
    ``log_create`` / ``log_update``; ``required=True`` writes through ``log_required``
    (the caller wraps it in ``app.api.audit_guard.required_audit``).
    """
    if not required:
        if saved.created:
            return audit.log_create(
                "working_calendar", saved.row.id, saved.center.code, new_values=saved.after, extra_data=extra_data
            )
        return audit.log_update(
            "working_calendar",
            saved.row.id,
            saved.center.code,
            old_values=saved.before,
            new_values=saved.after,
            extra_data=extra_data,
        )
    return audit.log_required(
        "CREATE" if saved.created else "UPDATE",
        "working_calendar",
        resource_id=saved.row.id,
        resource_identifier=saved.center.code,
        description=f"{'Created' if saved.created else 'Updated'} working_calendar: {saved.center.code}",
        old_values=None if saved.created else saved.before,
        new_values=saved.after,
        extra_data=extra_data,
    )
