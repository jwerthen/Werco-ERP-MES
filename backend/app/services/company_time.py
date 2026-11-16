"""The active company's time zone, and the calendar-day windows built on it.

``Company.timezone`` (String(50), nullable, default America/Chicago, no IANA
validation) decides where a company's calendar day starts and ends. Every read that
turns a user-supplied calendar date into a filter on a UTC timestamp column goes
through here, so "January 15" means the same instants on every endpoint: local
midnight to the next local midnight, converted to UTC -- never UTC midnight, which
cuts a Central shop's day off at 6 PM (CST) / 7 PM (CDT).

The pure helpers live in ``app.core.time_utils``; this module only adds the one DB
read (the company's zone). ``db.get`` goes through the identity map, so calling it
per request is cheap.
"""

from datetime import date, datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.time_utils import local_today, local_window, resolve_zone
from app.models.company import Company


def company_zone(db: Session, company_id: Optional[int]) -> ZoneInfo:
    """The company's IANA zone; America/Chicago when unset, unknown or the row is missing."""
    company = db.get(Company, company_id) if company_id is not None else None
    return resolve_zone(getattr(company, "timezone", None))


def company_today(db: Session, company_id: Optional[int]) -> date:
    """Today's calendar date on the company's wall clock."""
    return local_today(company_zone(db, company_id))


def company_window(db: Session, company_id: Optional[int], start: date, end: date) -> Tuple[datetime, datetime]:
    """Naive-UTC ``[start_utc, end_utc)`` for an inclusive company-local date range.

    For services handed an already-validated range (routers refuse a reversed or
    out-of-range one with a 422 first); a reversed range reads as empty. See
    ``app.core.time_utils.local_window``.
    """
    return local_window(start, end, company_zone(db, company_id))
