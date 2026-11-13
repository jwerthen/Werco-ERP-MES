from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

CENTRAL_TIME_ZONE = ZoneInfo("America/Chicago")


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_utc_iso(value: Optional[datetime]) -> Optional[str]:
    dt = ensure_utc(value)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def to_central_iso(value: Optional[datetime]) -> Optional[str]:
    dt = ensure_utc(value)
    if dt is None:
        return None
    return dt.astimezone(CENTRAL_TIME_ZONE).isoformat()


def central_today() -> date:
    """Today's date on the shop's (Central) calendar, never the server host's local date."""
    return datetime.now(CENTRAL_TIME_ZONE).date()
