"""One date-level capacity model for projections, previews and committed plans."""

from datetime import date

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
