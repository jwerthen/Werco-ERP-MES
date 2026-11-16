from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The zone a company with no (or an unusable) ``Company.timezone`` is treated as being
# in. Never UTC: the shop's calendar day is a local day, and a UTC fallback would put
# the day boundary at 6/7 PM on the shop floor -- the exact bug the local-day helpers
# below exist to remove.
DEFAULT_COMPANY_TIMEZONE = "America/Chicago"

CENTRAL_TIME_ZONE = ZoneInfo(DEFAULT_COMPANY_TIMEZONE)

# Refusal texts for an unusable calendar-date range. The first two match the consumables
# history filter (``consumable_service.history_query``) word for word, so every
# date-range 422 in the API reads the same.
START_AFTER_END_DETAIL = "Start date must be on or before end date"
END_OUT_OF_RANGE_DETAIL = "End date is outside the supported range"
START_OUT_OF_RANGE_DETAIL = "Start date is outside the supported range"


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


def resolve_zone(name: Optional[str]) -> ZoneInfo:
    """The IANA zone ``name`` names, or America/Chicago when it is blank or unknown.

    ``Company.timezone`` is unvalidated free text (nullable, no IANA check), so a bad
    value must not 500 a read. The fallback is the shop's zone, deliberately never UTC.
    """
    candidate = (name or "").strip()
    if not candidate:
        return CENTRAL_TIME_ZONE
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        # ZoneInfo raises ValueError (not ZoneInfoNotFoundError) for a key that is not a
        # valid relative path, e.g. "../etc/passwd" or an absolute path.
        return CENTRAL_TIME_ZONE


def local_date_range_to_utc(
    start: Optional[date],
    end: Optional[date],
    zone: ZoneInfo,
    *,
    naive: bool = True,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Turn an INCLUSIVE calendar-date range in ``zone`` into a half-open UTC window.

    Returns ``(start_utc, end_utc)`` for a filter of the form
    ``column >= start_utc AND column < end_utc``:

    * ``start_utc`` is local midnight at the start of ``start``;
    * ``end_utc`` is local midnight at the start of the day AFTER ``end`` -- EXCLUSIVE.
      Use a strict ``<``: an inclusive ``<=`` against this bound would also take rows
      stamped at exactly the next local midnight.

    Either side may be ``None`` (that side is unbounded). DST is handled by the zone:
    a spring-forward day is 23 hours long and a fall-back day 25.

    ``naive=True`` (the default) strips the tzinfo after converting, for the naive-UTC
    ``DateTime`` columns most of the schema uses: an aware bound against a Postgres
    ``timestamp without time zone`` is cast through the session TimeZone, which is right
    only by accident. Pass ``naive=False`` for ``DateTime(timezone=True)`` columns
    (``OperationalEvent.occurred_at``, ``ConsumableCheckout.issued_at``).

    Raises ``ValueError`` -- whose message is the 422 detail to return -- when
    ``start > end``, when ``end`` is ``date.max`` (there is no next day), or when a
    bound cannot be represented as a UTC datetime.
    """
    if start is not None and end is not None and start > end:
        raise ValueError(START_AFTER_END_DETAIL)

    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    if start is not None:
        try:
            start_utc = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
        except OverflowError as exc:
            raise ValueError(START_OUT_OF_RANGE_DETAIL) from exc
    if end is not None:
        if end == date.max:
            raise ValueError(END_OUT_OF_RANGE_DETAIL)
        try:
            end_utc = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        except OverflowError as exc:
            raise ValueError(END_OUT_OF_RANGE_DETAIL) from exc

    if naive:
        start_utc = start_utc.replace(tzinfo=None) if start_utc is not None else None
        end_utc = end_utc.replace(tzinfo=None) if end_utc is not None else None
    return start_utc, end_utc


def local_window(start: date, end: date, zone: ZoneInfo) -> Tuple[datetime, datetime]:
    """Naive-UTC ``[start_utc, end_utc)`` for an inclusive local-date range, for services.

    Same bounds as ``local_date_range_to_utc``, for a service handed a range a router
    has already validated: a REVERSED range reads as an empty window (both bounds at
    ``start``'s local midnight) -- what the old UTC-midnight filter returned for it --
    instead of raising from deep inside a read. ``date.max`` as ``end`` still raises.
    """
    if start > end:
        empty_at, _ = local_date_range_to_utc(start, None, zone)
        return cast(Tuple[datetime, datetime], (empty_at, empty_at))
    # Both dates are given, so both bounds are set.
    return cast(Tuple[datetime, datetime], local_date_range_to_utc(start, end, zone))


def local_today(zone: ZoneInfo, now: Optional[datetime] = None) -> date:
    """Today's calendar date on ``zone``'s wall clock (``now`` defaults to the real clock).

    Use this, never naive ``date.today()``: a UTC server's ``date.today()`` flips to the
    next day at 6/7 PM Central.
    """
    instant = datetime.now(timezone.utc) if now is None else (ensure_utc(now) or now)
    return instant.astimezone(zone).date()


def to_local_date(value: Optional[datetime], zone: ZoneInfo) -> Optional[date]:
    """The calendar date in ``zone`` of an instant (a naive value is read as UTC)."""
    dt = ensure_utc(value)
    if dt is None:
        return None
    return dt.astimezone(zone).date()


def to_local_iso(value: Optional[datetime], zone: ZoneInfo) -> Optional[str]:
    """ISO-8601 in ``zone``'s wall time WITH its UTC offset, to the second.

    A naive value is read as UTC (the schema stores naive UTC). Example:
    ``to_local_iso(datetime(2026, 1, 15, 23, 30), CENTRAL_TIME_ZONE)`` is
    ``'2026-01-15T17:30:00-06:00'``; in July the offset is ``-05:00``.
    """
    dt = ensure_utc(value)
    if dt is None:
        return None
    return dt.astimezone(zone).isoformat(timespec="seconds")


def to_central_iso(value: Optional[datetime]) -> Optional[str]:
    dt = ensure_utc(value)
    if dt is None:
        return None
    return dt.astimezone(CENTRAL_TIME_ZONE).isoformat()


def central_today() -> date:
    """Today's date on the shop's (Central) calendar, never the server host's local date."""
    return datetime.now(CENTRAL_TIME_ZONE).date()
