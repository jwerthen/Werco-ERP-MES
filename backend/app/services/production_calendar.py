"""Production-day and shift arithmetic -- pure, no database imports.

A **production day** is the shop's working day, not the calendar date. It starts at the
company's ``production_day_start_minute`` on the local wall clock (Werco: 05:00 Central)
and runs to the same wall-clock time the next day, so a weld entry at 01:30 Wednesday
belongs to TUESDAY's production day. A shift that runs past midnight belongs to the day
it started. Every timestamp column in the schema is naive UTC; every bound produced here
for a SQL filter is naive UTC too (``*_utc`` fields), half-open ``[start, end)``.

With no production calendar configured (no settings row) the production day is the
company-local calendar day (day start 00:00) and there are no shifts -- see
:func:`unconfigured_calendar`.

Attribution rules (owner/lead decisions, 2026-11-13):

* An **instant** belongs to ``production_day(ts)``. If it falls inside two overlapping
  shifts (Werco's rest-of-shop 15:30-16:00 hand-off) it belongs to the OUTGOING shift --
  the one that started earlier (:func:`shift_for`).
* An **interval** (a time entry) belongs to the shift it overlaps MOST; a tie goes to the
  outgoing (earlier-start) shift (:func:`shift_for_interval`). An interval that overlaps
  no shift belongs to the production day of its start and to no shift
  (:func:`entry_attribution`). This is what puts an early 04:55 weld clock-in on that
  day's 1st shift instead of the previous production day.

Wall-clock arithmetic: shift and day boundaries are wall-clock times in the company zone,
so a production day is 23 hours long across spring-forward and 25 hours across fall-back.
A boundary that names a wall time skipped by spring-forward resolves with ``fold=0``
(the pre-transition offset); one that occurs twice at fall-back resolves to its first
occurrence.

Shifts are stored as integer minutes from local midnight of the day they start:
``0 <= start_minute <= 1439`` and ``start_minute < end_minute <= start_minute + 1440``;
an ``end_minute`` above 1440 ends the next calendar day. ``days_mask`` bit 0 is Monday
... bit 6 is Sunday (``date.weekday()``, the ``WorkingCalendar.weekly_hours`` order) and
names the day a shift STARTS on.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.core.time_utils import (
    CENTRAL_TIME_ZONE,
    END_OUT_OF_RANGE_DETAIL,
    START_AFTER_END_DETAIL,
    START_OUT_OF_RANGE_DETAIL,
    ensure_utc,
)

MINUTES_PER_DAY = 1440
ALL_DAYS_MASK = 0b1111111
WEEKDAYS_MASK = 0b0011111

# How many production days AFTER the production day of an interval's start the interval can
# be attributed to. :func:`shift_for_interval` scans the shifts of the production day before
# the start's through this many days after it (a malformed weeks-long interval cannot make
# it walk every day in between), so a row attributed to production day ``d`` always STARTS
# on or after the start of production day ``d - ATTRIBUTION_REACH_DAYS``. That is the bound
# the SQL selection reads back to (:func:`attribution_read_from`): the two are one rule, so
# a long interval -- a forgotten clock-out closed the next afternoon, a breakdown over two
# nights -- is always read by the query of the day it is attributed to.
ATTRIBUTION_REACH_DAYS = 2


@dataclass(frozen=True)
class ShiftDef:
    """One defined shift, as attribution sees it (active definitions only)."""

    id: int
    code: str
    name: str
    pattern_id: int
    start_minute: int
    end_minute: int
    days_mask: int
    planned_minutes: int
    sort_order: int = 0

    def runs_on(self, weekday: int) -> bool:
        """True when the shift STARTS on ``weekday`` (Monday=0 .. Sunday=6)."""
        return bool(self.days_mask & (1 << weekday))


@dataclass(frozen=True)
class ShiftOccurrence:
    """One shift on one production day, with its naive-UTC ``[start_utc, end_utc)`` bounds."""

    shift: ShiftDef
    production_day: date
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class ProductionCalendar:
    """A company's production calendar, loaded once per request or cron pass.

    ``shifts_by_wc`` maps every work center that is an explicit member of a pattern to
    that pattern's ACTIVE shifts (possibly empty). A work center in no pattern -- and an
    entry with no work center -- uses ``default_shifts`` (the default pattern's active
    shifts, or none). ``version`` is 0 and ``configured`` False when the company has no
    production calendar.
    """

    company_id: Optional[int]
    tz: ZoneInfo
    day_start_minute: int = 0
    shifts_by_wc: Mapping[int, Tuple[ShiftDef, ...]] = field(default_factory=dict)
    default_shifts: Tuple[ShiftDef, ...] = ()
    version: int = 0
    configured: bool = False

    def shifts_for_work_center(self, work_center_id: Optional[int]) -> Tuple[ShiftDef, ...]:
        if work_center_id is not None and work_center_id in self.shifts_by_wc:
            return self.shifts_by_wc[work_center_id]
        return self.default_shifts

    @property
    def has_shifts(self) -> bool:
        return bool(self.default_shifts) or any(self.shifts_by_wc.values())


def unconfigured_calendar(tz: Optional[ZoneInfo] = None, company_id: Optional[int] = None) -> ProductionCalendar:
    """The calendar of a company with no configuration: local-midnight days, no shifts."""
    return ProductionCalendar(company_id=company_id, tz=tz or CENTRAL_TIME_ZONE)


# --------------------------------------------------------------------------------------
# Minute <-> "HH:MM" helpers (the API's wire format)
# --------------------------------------------------------------------------------------


def minute_to_hhmm(minute: int) -> str:
    """``930`` -> ``"15:30"``; a minute past midnight of the next day wraps (``1560`` -> ``"02:00"``)."""
    minute = minute % MINUTES_PER_DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


def hhmm_to_minute(value: str) -> int:
    """``"05:00"`` -> ``300``. Raises ``ValueError`` on anything but a 00:00-23:59 wall time."""
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() and 1 <= len(part) <= 2 for part in parts):
        raise ValueError(f"'{value}' is not a time of day in HH:MM form")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        raise ValueError(f"'{value}' is not a time of day in HH:MM form")
    return hours * 60 + minutes


def shift_minutes_from_wall_times(start: str, end: str) -> Tuple[int, int]:
    """``("15:30", "02:00")`` -> ``(930, 1560)``: an end at or before the start ends the next day."""
    start_minute = hhmm_to_minute(start)
    end_wall = hhmm_to_minute(end)
    end_minute = end_wall if end_wall > start_minute else end_wall + MINUTES_PER_DAY
    return start_minute, end_minute


def days_to_mask(days: Sequence[int]) -> int:
    mask = 0
    for day in days:
        mask |= 1 << int(day)
    return mask


def mask_to_days(mask: int) -> List[int]:
    return [day for day in range(7) if mask & (1 << day)]


# --------------------------------------------------------------------------------------
# Wall-clock instants
# --------------------------------------------------------------------------------------


def _wall_to_utc(day: date, minute: int, tz: ZoneInfo) -> datetime:
    """Naive UTC for the local wall time ``minute`` minutes after local midnight of ``day``.

    ``minute`` may exceed 1440 (a time on a later day). Wall-clock arithmetic: the
    timedelta is added to the NAIVE wall time, then the zone is attached.
    """
    wall = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
    return wall.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def _local_wall(ts: datetime, tz: ZoneInfo) -> datetime:
    """``ts`` (naive = UTC) as a NAIVE local wall time in ``tz``."""
    aware = ensure_utc(ts)
    assert aware is not None
    return aware.astimezone(tz).replace(tzinfo=None)


def production_day(ts: datetime, cal: ProductionCalendar, work_center_id: Optional[int] = None) -> date:
    """The production day an instant belongs to (naive ``ts`` is read as UTC).

    Defined by the SAME boundaries the SQL windows use: the day ``d`` with
    ``production_day_start_utc(d) <= ts < production_day_start_utc(d + 1)``. The wall-clock
    shortcut (local wall time minus the day start) is the first guess and is exact except
    for the hour a DST transition makes a day start skipped or repeated (a start between
    01:00 and 02:59 in a US zone): there the ``fold=0`` boundary decides, so an instant is
    never attributed to a day whose window does not contain it.

    ``work_center_id`` is accepted for the brief's ``production_day(ts, work_center)``
    signature and reserved: the day start is company-level today.
    """
    del work_center_id  # reserved; the day start is company-wide
    instant = _naive_utc(ts)
    day = (_local_wall(instant, cal.tz) - timedelta(minutes=cal.day_start_minute)).date()
    # At most one step either way (a DST shift is an hour); the loops are bounded anyway.
    for _ in range(2):
        if instant < production_day_start_utc(day, cal):
            day -= timedelta(days=1)
        elif instant >= production_day_start_utc(day + timedelta(days=1), cal):
            day += timedelta(days=1)
        else:
            break
    return day


def production_day_start_utc(day: date, cal: ProductionCalendar) -> datetime:
    """Naive UTC instant at which production day ``day`` begins."""
    return _day_start_utc(day, cal.day_start_minute, cal.tz)


@lru_cache(maxsize=4096)
def _day_start_utc(day: date, minute: int, tz: ZoneInfo) -> datetime:
    return _wall_to_utc(day, minute, tz)


def attribution_read_from(first_day: date, cal: ProductionCalendar) -> datetime:
    """Naive UTC: the earliest START of an interval that can be attributed to ``first_day`` or later.

    The start of production day ``first_day - ATTRIBUTION_REACH_DAYS``, clamped at
    ``datetime.min`` at the start of the date range. An interval attributed to day ``d``
    starts on a production day no earlier than ``d - ATTRIBUTION_REACH_DAYS`` (the scan
    bound of :func:`shift_for_interval`; with no shift overlap it is the start's own day),
    so a selection ``start >= attribution_read_from(first_day)`` never misses one.
    """
    try:
        return production_day_start_utc(first_day - timedelta(days=ATTRIBUTION_REACH_DAYS), cal)
    except OverflowError:
        return datetime.min


def production_day_window_utc(
    day: date, cal: ProductionCalendar, work_center_id: Optional[int] = None
) -> Tuple[datetime, datetime]:
    """Naive-UTC ``[start, end)`` of production day ``day`` (23 h / 25 h across DST).

    Raises ``ValueError`` for ``date.max`` (there is no next day to end on).
    """
    del work_center_id
    if day == date.max:
        raise ValueError(END_OUT_OF_RANGE_DETAIL)
    return production_day_start_utc(day, cal), production_day_start_utc(day + timedelta(days=1), cal)


def production_days_window_utc(
    start: Optional[date], end: Optional[date], cal: ProductionCalendar
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Naive-UTC ``[start_utc, end_utc)`` covering the INCLUSIVE production-day range ``start..end``.

    Either side may be ``None`` (unbounded). Filter with ``>=`` on the start and a STRICT
    ``<`` on the end. Raises ``ValueError`` -- whose message is the 422 detail -- when
    ``start > end``, when ``end`` is ``date.max`` or a bound cannot be represented.
    The production-day twin of ``app.core.time_utils.local_date_range_to_utc``; with no
    calendar configured the two return the same bounds.
    """
    if start is not None and end is not None and start > end:
        raise ValueError(START_AFTER_END_DETAIL)
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    if start is not None:
        try:
            start_utc = production_day_start_utc(start, cal)
        except OverflowError as exc:
            raise ValueError(START_OUT_OF_RANGE_DETAIL) from exc
    if end is not None:
        if end == date.max:
            raise ValueError(END_OUT_OF_RANGE_DETAIL)
        try:
            end_utc = production_day_start_utc(end + timedelta(days=1), cal)
        except OverflowError as exc:
            raise ValueError(END_OUT_OF_RANGE_DETAIL) from exc
    return start_utc, end_utc


def production_days_window_or_empty(start: date, end: date, cal: ProductionCalendar) -> Tuple[datetime, datetime]:
    """Service-side window for an already-validated range; a REVERSED range reads as empty."""
    if start > end:
        empty_at = production_day_start_utc(start, cal)
        return empty_at, empty_at
    start_utc, end_utc = production_days_window_utc(start, end, cal)
    assert start_utc is not None and end_utc is not None
    return start_utc, end_utc


def current_production_day(cal: ProductionCalendar, now: Optional[datetime] = None) -> date:
    """Today's production day (``now`` defaults to the real clock; naive is read as UTC)."""
    instant = datetime.now(timezone.utc) if now is None else now
    return production_day(instant, cal)


# --------------------------------------------------------------------------------------
# Shifts
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _occurrence(shift: ShiftDef, day: date, tz: ZoneInfo) -> ShiftOccurrence:
    return ShiftOccurrence(
        shift=shift,
        production_day=day,
        start_utc=_wall_to_utc(day, shift.start_minute, tz),
        end_utc=_wall_to_utc(day, shift.end_minute, tz),
    )


def occurrence_on(shift: ShiftDef, day: date, cal: ProductionCalendar) -> ShiftOccurrence:
    """``shift`` on production day ``day`` (whether or not it runs that weekday)."""
    return _occurrence(shift, day, cal.tz)


def _sort_key(occurrence: ShiftOccurrence) -> Tuple[datetime, int, int]:
    return (occurrence.start_utc, occurrence.shift.sort_order, occurrence.shift.id)


def shifts_for_day(day: date, cal: ProductionCalendar, work_center_id: Optional[int] = None) -> List[ShiftOccurrence]:
    """The shifts that START on production day ``day`` at the work center, earliest first."""
    weekday = day.weekday()
    occurrences = [
        _occurrence(shift, day, cal.tz)
        for shift in cal.shifts_for_work_center(work_center_id)
        if shift.runs_on(weekday)
    ]
    return sorted(occurrences, key=_sort_key)


def shifts_at(ts: datetime, cal: ProductionCalendar, work_center_id: Optional[int] = None) -> List[ShiftOccurrence]:
    """Every shift running at instant ``ts`` (start inclusive, end exclusive), earliest start first."""
    instant = _naive_utc(ts)
    day = production_day(instant, cal)
    candidates = shifts_for_day(day - timedelta(days=1), cal, work_center_id) + shifts_for_day(day, cal, work_center_id)
    return sorted(
        (occ for occ in candidates if occ.start_utc <= instant < occ.end_utc),
        key=_sort_key,
    )


def shift_for(ts: datetime, cal: ProductionCalendar, work_center_id: Optional[int] = None) -> Optional[ShiftOccurrence]:
    """The shift an instant belongs to; in an overlap the OUTGOING (earlier-start) shift wins."""
    running = shifts_at(ts, cal, work_center_id)
    return running[0] if running else None


def shift_for_interval(
    start: datetime,
    end: datetime,
    cal: ProductionCalendar,
    work_center_id: Optional[int] = None,
) -> Optional[ShiftOccurrence]:
    """The shift ``[start, end)`` overlaps MOST; ties go to the outgoing shift; ``None`` without overlap.

    A zero-length (or reversed) interval is treated as the instant ``start``.
    """
    lo = _naive_utc(start)
    hi = _naive_utc(end)
    if hi <= lo:
        return shift_for(lo, cal, work_center_id)
    start_day = production_day(lo, cal)
    first_day = start_day - timedelta(days=1)
    # Cap the scan so a malformed months-long interval cannot make attribution loop over
    # every day in between. The cap is ATTRIBUTION_REACH_DAYS, which the SQL selection
    # (attribution_read_from) reads back to -- change the two together, never one.
    last_day = production_day(hi, cal)
    if (last_day - start_day).days > ATTRIBUTION_REACH_DAYS:
        last_day = start_day + timedelta(days=ATTRIBUTION_REACH_DAYS)
    best: Optional[ShiftOccurrence] = None
    best_overlap = timedelta(0)
    day = first_day
    while day <= last_day:
        for occ in shifts_for_day(day, cal, work_center_id):
            overlap = min(hi, occ.end_utc) - max(lo, occ.start_utc)
            if overlap <= timedelta(0):
                continue
            if best is None or overlap > best_overlap or (overlap == best_overlap and _sort_key(occ) < _sort_key(best)):
                best, best_overlap = occ, overlap
        day += timedelta(days=1)
    return best


def entry_attribution(
    clock_in: datetime,
    clock_out: Optional[datetime],
    cal: ProductionCalendar,
    work_center_id: Optional[int] = None,
    *,
    now: Optional[datetime] = None,
) -> Tuple[date, Optional[ShiftOccurrence]]:
    """``(production_day, shift)`` for a time entry (an open entry runs to ``now``).

    The entry belongs to the shift it overlaps most (ties to the outgoing shift) and to
    that shift's production day; an entry overlapping no shift belongs to the production
    day of its clock-in and to no shift.
    """
    start = _naive_utc(clock_in)
    if clock_out is not None:
        end = _naive_utc(clock_out)
    elif now is not None:
        end = _naive_utc(now)
    else:
        end = start
    occurrence = shift_for_interval(start, end, cal, work_center_id) if cal.has_shifts else None
    if occurrence is not None:
        return occurrence.production_day, occurrence
    return production_day(start, cal), None


def weekly_hours_for(cal: ProductionCalendar, work_center_id: Optional[int]) -> List[float]:
    """Seven Mon..Sun planned hours: the sum of ``planned_minutes`` of the active shifts starting each day.

    The ``WorkingCalendar.weekly_hours`` shape ("combined hours across all shifts"). A
    shift that runs past midnight counts on the day it started.
    """
    hours: List[float] = []
    shifts = cal.shifts_for_work_center(work_center_id)
    for weekday in range(7):
        minutes = sum(shift.planned_minutes for shift in shifts if shift.runs_on(weekday))
        hours.append(round(minutes / 60.0, 4))
    return hours


def _naive_utc(ts: datetime) -> datetime:
    aware = ensure_utc(ts)
    assert aware is not None
    return aware.replace(tzinfo=None)


def build_calendar(
    *,
    company_id: Optional[int],
    tz: ZoneInfo,
    day_start_minute: int,
    version: int,
    patterns: Sequence[Tuple[int, bool, Sequence[int], Sequence[ShiftDef]]],
) -> ProductionCalendar:
    """Assemble a :class:`ProductionCalendar` from ``(pattern_id, is_default, work_center_ids, active_shifts)``."""
    shifts_by_wc: Dict[int, Tuple[ShiftDef, ...]] = {}
    default_shifts: Tuple[ShiftDef, ...] = ()
    for _pattern_id, is_default, work_center_ids, shifts in patterns:
        ordered = tuple(sorted(shifts, key=lambda s: (s.start_minute, s.sort_order, s.id)))
        if is_default:
            default_shifts = ordered
        for work_center_id in work_center_ids:
            shifts_by_wc[work_center_id] = ordered
    return ProductionCalendar(
        company_id=company_id,
        tz=tz,
        day_start_minute=day_start_minute,
        shifts_by_wc=shifts_by_wc,
        default_shifts=default_shifts,
        version=version,
        configured=True,
    )
