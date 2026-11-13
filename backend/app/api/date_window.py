"""Calendar-date query parameters -> a UTC filter window, with 422s at the HTTP boundary.

A ``date`` query parameter (``start_date`` / ``end_date``, ``date_from`` / ``date_to``)
names a calendar day in the COMPANY's time zone. A filter on a naive-UTC timestamp
column must therefore run from local midnight to the next local midnight, converted to
UTC -- see ``app.core.time_utils.local_date_range_to_utc``. The old pattern,
``datetime.combine(d, datetime.min/max.time())``, compared a UTC column against UTC
midnight, so a Central "day" ran 6/7 PM to 6/7 PM and second-shift rows landed in the
next day; ``tests/test_date_filter_combine_guard.py`` keeps that pattern out of ``app/``.

Usage (always a strict ``<`` on the end bound -- it is the EXCLUSIVE next midnight)::

    start_utc, end_utc = company_date_window(db, company_id, start_date, end_date)
    if start_utc is not None:
        query = query.filter(Model.created_at >= start_utc)
    if end_utc is not None:
        query = query.filter(Model.created_at < end_utc)
"""

from datetime import date, datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.time_utils import local_date_range_to_utc
from app.services.company_time import company_zone

# The text every ``date`` filter parameter description in a query-string contract uses,
# so the MCP tool schemas generated from them all say the same thing.
LOCAL_DATE_FROM_DESCRIPTION = (
    "Inclusive start: a calendar date in the company time zone (Company.timezone, default "
    "America/Chicago), from local midnight"
)
LOCAL_DATE_TO_DESCRIPTION = (
    "Inclusive end: a calendar date in the company time zone (Company.timezone, default "
    "America/Chicago), through the end of that local day"
)


def date_window(
    start: Optional[date], end: Optional[date], zone: ZoneInfo, *, naive: bool = True
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``local_date_range_to_utc`` with its ``ValueError`` turned into an HTTP 422."""
    try:
        return local_date_range_to_utc(start, end, zone, naive=naive)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def company_date_window(
    db: Session,
    company_id: int,
    start: Optional[date],
    end: Optional[date],
    *,
    naive: bool = True,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``[start_utc, end_utc)`` for an inclusive local-date range on the company's zone.

    422 when ``start > end`` ("Start date must be on or before end date") or a bound is
    out of range ("End date is outside the supported range" for ``9999-12-31``).
    """
    return date_window(start, end, company_zone(db, company_id), naive=naive)


def company_period(
    db: Session,
    company_id: int,
    period: str,
    start_date: Optional[date],
    end_date: Optional[date],
) -> Tuple[date, date, ZoneInfo]:
    """Resolve an analytics ``period`` (today / 7d / 30d / 90d / ytd / custom) on the company clock.

    Returns ``(start, end, zone)``: an inclusive local-date range whose "today" is the
    company's wall-clock date, plus the zone the service should read it in. A custom
    range that is reversed or out of range is a 422.
    """
    from app.services.analytics_service import get_date_range

    zone = company_zone(db, company_id)
    start, end = get_date_range(period, start_date, end_date, zone=zone)
    date_window(start, end, zone)
    return start, end, zone
