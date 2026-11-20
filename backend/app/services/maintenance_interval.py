"""When a PM schedule comes due next: the ONE implementation of a schedule's interval.

Day cadences (daily, weekly, biweekly, and ``custom``'s ``frequency_days``) add days. Calendar
cadences (monthly, quarterly, semi-annual, annual) add CALENDAR MONTHS to the day the PM was
completed (owner decision 2026-11-20): monthly used to be a fixed 30 days, quarterly 90, semi-annual
182 and annual 365, so a PM completed on time drifted earlier every cycle (Dec 1 -> Dec 31 -> Jan 30
...) and an annual one landed a day early across a Feb 29.

The base stays the COMPLETION day (the floating rule the loop has always used: a late PM pushes the
next one out by its lateness). A day past the end of the target month clamps to that month's last
day -- Jan 31 + 1 month is Feb 28 (Feb 29 in a leap year), Feb 29 + 1 year is Feb 28 -- and the
clamp sticks: Jan 31 -> Feb 28 -> Mar 28. Anchoring to the original day of month would need an anchor
column; the owner accepted the clamp.

Pure functions: no database, no clock.
"""

import calendar
from datetime import date, timedelta
from typing import Optional

from app.models.maintenance import FREQUENCY_DAYS_MAP, FREQUENCY_MONTHS_MAP, MaintenanceFrequency

# A custom schedule always carries frequency_days (the schemas refuse one without); this is the
# historical fallback for a legacy row that does not.
DEFAULT_INTERVAL_DAYS = 30


def add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months, the day clamped to the end of a shorter month."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def next_due_date(frequency: MaintenanceFrequency, frequency_days: Optional[int], completed_on: date) -> date:
    """The schedule's next due date after a PM completed on ``completed_on`` (the company's day)."""
    months = FREQUENCY_MONTHS_MAP.get(frequency)
    if months is not None:
        return add_months(completed_on, months)
    days = FREQUENCY_DAYS_MAP.get(frequency) or frequency_days or DEFAULT_INTERVAL_DAYS
    return completed_on + timedelta(days=days)
