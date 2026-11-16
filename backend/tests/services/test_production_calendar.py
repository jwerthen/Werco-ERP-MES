"""Pure production-day / shift arithmetic (P1.2) on Werco's confirmed rules, built in memory.

Werco: Mon-Fri, day start 05:00 Central, a shift belongs to the day it started.
Weld (WLD-01..06): W1 05:00-15:30, W2 15:30-02:00, 600 planned min.
Rest of shop (default): S1 07:30-16:00, S2 15:30-00:00, 480 planned min.

2026-11-17 is a Tuesday (CST, UTC-6). 2026-03-08 and 2026-11-01 are the DST days.
"""

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.time_utils import (
    CENTRAL_TIME_ZONE,
    END_OUT_OF_RANGE_DETAIL,
    START_AFTER_END_DETAIL,
    local_date_range_to_utc,
)
from app.services.production_calendar import (
    ATTRIBUTION_REACH_DAYS,
    ShiftDef,
    attribution_read_from,
    build_calendar,
    current_production_day,
    days_to_mask,
    entry_attribution,
    hhmm_to_minute,
    mask_to_days,
    minute_to_hhmm,
    production_day,
    production_day_window_utc,
    production_days_window_utc,
    shift_for,
    shift_for_interval,
    shift_minutes_from_wall_times,
    shifts_at,
    shifts_for_day,
    unconfigured_calendar,
    weekly_hours_for,
)
from tests.production_calendar_helpers import werco_calendar

pytestmark = pytest.mark.unit

WELD = 101  # WLD-01
REST = 900  # any other work center -> the default "Rest of shop" pattern
CAL = werco_calendar()


def central(year, month, day, hour, minute=0):
    """A Central wall time as the naive-UTC instant the schema stores."""
    return (
        datetime(year, month, day, hour, minute, tzinfo=CENTRAL_TIME_ZONE).astimezone(timezone.utc).replace(tzinfo=None)
    )


def codes(occurrences):
    return [occ.shift.code for occ in occurrences]


# --------------------------------------------------------------------------------------
# The brief's acceptance: a 01:30 Wednesday weld entry is Tuesday's 2nd shift
# --------------------------------------------------------------------------------------


def test_0130_wednesday_weld_instant_is_tuesday_production_day_and_second_shift():
    at = central(2026, 11, 18, 1, 30)  # Wednesday 01:30 CST = 07:30Z
    assert at == datetime(2026, 11, 18, 7, 30)
    assert production_day(at, CAL, WELD) == date(2026, 11, 17)
    occ = shift_for(at, CAL, WELD)
    assert occ is not None and occ.shift.code == "W2"
    assert occ.production_day == date(2026, 11, 17)
    assert occ.start_utc == datetime(2026, 11, 17, 21, 30)  # Tue 15:30 CST
    assert occ.end_utc == datetime(2026, 11, 18, 8, 0)  # Wed 02:00 CST


def test_0130_wednesday_weld_time_entry_attributes_to_tuesday_second_shift():
    day, occ = entry_attribution(central(2026, 11, 18, 1, 30), central(2026, 11, 18, 3, 0), CAL, WELD)
    assert day == date(2026, 11, 17)
    assert occ is not None and occ.shift.code == "W2"
    # Even with the clock-out still open at 02:30 it is the 2nd shift of Tuesday.
    day, occ = entry_attribution(central(2026, 11, 18, 1, 30), None, CAL, WELD, now=central(2026, 11, 18, 2, 30))
    assert (day, occ.shift.code) == (date(2026, 11, 17), "W2")


def test_same_instant_at_a_rest_of_shop_center_is_tuesday_with_no_shift():
    at = central(2026, 11, 18, 1, 30)
    assert production_day(at, CAL, REST) == date(2026, 11, 17)
    assert shift_for(at, CAL, REST) is None  # 00:00-07:30 gap
    day, occ = entry_attribution(at, at + timedelta(minutes=30), CAL, REST)
    assert (day, occ) == (date(2026, 11, 17), None)


def test_weekend_nights():
    # Sat 01:30 at weld: still Friday's production day and Friday's 2nd shift.
    sat = central(2026, 11, 21, 1, 30)
    assert production_day(sat, CAL, WELD) == date(2026, 11, 20)
    assert shift_for(sat, CAL, WELD).shift.code == "W2"
    # Mon 01:30 at weld: Sunday's production day, and nobody works Sunday.
    mon = central(2026, 11, 23, 1, 30)
    assert production_day(mon, CAL, WELD) == date(2026, 11, 22)
    assert shift_for(mon, CAL, WELD) is None


# --------------------------------------------------------------------------------------
# The brief's acceptance: the 15:30-16:00 rest-of-shop hand-off overlap
# --------------------------------------------------------------------------------------


def test_rest_of_shop_handoff_overlap_instants():
    assert codes(shifts_at(central(2026, 11, 17, 15, 29), CAL, REST)) == ["S1"]
    assert codes(shifts_at(central(2026, 11, 17, 15, 30), CAL, REST)) == ["S1", "S2"]
    assert codes(shifts_at(central(2026, 11, 17, 15, 45), CAL, REST)) == ["S1", "S2"]
    # An instant in the overlap belongs to the OUTGOING (earlier-start) shift.
    assert shift_for(central(2026, 11, 17, 15, 45), CAL, REST).shift.code == "S1"
    # End is exclusive.
    assert codes(shifts_at(central(2026, 11, 17, 16, 0), CAL, REST)) == ["S2"]
    assert codes(shifts_at(central(2026, 11, 18, 0, 0), CAL, REST)) == []


def test_rest_of_shop_handoff_overlap_intervals_greatest_overlap_ties_to_outgoing():
    assert (
        shift_for_interval(central(2026, 11, 17, 15, 45), central(2026, 11, 17, 23, 30), CAL, REST).shift.code == "S2"
    )
    assert shift_for_interval(central(2026, 11, 17, 7, 30), central(2026, 11, 17, 16, 0), CAL, REST).shift.code == "S1"
    # Exactly the overlap: a tie -> the outgoing shift.
    tie = shift_for_interval(central(2026, 11, 17, 15, 30), central(2026, 11, 17, 16, 0), CAL, REST)
    assert tie.shift.code == "S1"
    # Mostly the incoming shift wins even though it started in the overlap.
    assert shift_for_interval(central(2026, 11, 17, 15, 40), central(2026, 11, 17, 18, 0), CAL, REST).shift.code == "S2"


def test_weld_handoff_has_no_overlap():
    assert codes(shifts_at(central(2026, 11, 17, 15, 30), CAL, WELD)) == ["W2"]
    assert codes(shifts_at(central(2026, 11, 17, 15, 29), CAL, WELD)) == ["W1"]


def test_early_weld_clock_in_attributes_by_overlap_to_that_days_first_shift():
    clock_in = central(2026, 11, 17, 4, 55)
    # By the instant alone 04:55 is still Monday's production day...
    assert production_day(clock_in, CAL, WELD) == date(2026, 11, 16)
    # ...but the entry overlaps Tuesday's W1 by 10.5 h, so it is Tuesday's 1st shift.
    day, occ = entry_attribution(clock_in, central(2026, 11, 17, 15, 30), CAL, WELD)
    assert (day, occ.shift.code) == (date(2026, 11, 17), "W1")


def test_entry_overlapping_no_shift_falls_back_to_the_production_day_of_its_start():
    day, occ = entry_attribution(central(2026, 11, 21, 9, 0), central(2026, 11, 21, 12, 0), CAL, WELD)  # Saturday
    assert (day, occ) == (date(2026, 11, 21), None)


def test_entry_with_no_work_center_uses_the_default_pattern():
    day, occ = entry_attribution(central(2026, 11, 17, 8, 0), central(2026, 11, 17, 10, 0), CAL, None)
    assert (day, occ.shift.code) == (date(2026, 11, 17), "S1")


# --------------------------------------------------------------------------------------
# Windows, DST, the unconfigured default
# --------------------------------------------------------------------------------------


def test_production_day_windows_are_wall_clock_and_23_or_25_hours_across_dst():
    start, end = production_day_window_utc(date(2026, 11, 17), CAL)
    assert (start, end) == (datetime(2026, 11, 17, 11, 0), datetime(2026, 11, 18, 11, 0))
    # Fall back (Sun 2026-11-01): Saturday's production day is 25 hours long.
    start, end = production_day_window_utc(date(2026, 10, 31), CAL)
    assert (start, end) == (datetime(2026, 10, 31, 10, 0), datetime(2026, 11, 1, 11, 0))
    assert end - start == timedelta(hours=25)
    # Spring forward (Sun 2026-03-08): Saturday's production day is 23 hours long.
    start, end = production_day_window_utc(date(2026, 3, 7), CAL)
    assert (start, end) == (datetime(2026, 3, 7, 11, 0), datetime(2026, 3, 8, 10, 0))
    assert end - start == timedelta(hours=23)
    # CDT summer day.
    assert production_day_window_utc(date(2026, 7, 15), CAL)[0] == datetime(2026, 7, 15, 10, 0)


def test_shift_occurrences_keep_wall_times_across_dst():
    # A Friday-into-Saturday weld 2nd shift across the fall-back night keeps 15:30-02:00 wall times.
    occ = [o for o in shifts_for_day(date(2026, 10, 30), CAL, WELD) if o.shift.code == "W2"][0]
    assert occ.start_utc == datetime(2026, 10, 30, 20, 30)  # 15:30 CDT
    assert occ.end_utc == datetime(2026, 10, 31, 7, 0)  # 02:00 CDT


def test_production_day_boundary_at_day_start():
    assert production_day(central(2026, 11, 17, 4, 59), CAL) == date(2026, 11, 16)
    assert production_day(central(2026, 11, 17, 5, 0), CAL) == date(2026, 11, 17)
    assert current_production_day(CAL, central(2026, 11, 18, 4, 0)) == date(2026, 11, 17)


def test_no_configuration_is_the_company_local_calendar_day():
    cal = unconfigured_calendar(CENTRAL_TIME_ZONE)
    for at in (central(2026, 1, 15, 23, 59), central(2026, 7, 15, 0, 0), central(2026, 3, 8, 3, 0)):
        assert production_day(at, cal) == at.replace(tzinfo=timezone.utc).astimezone(CENTRAL_TIME_ZONE).date()
    for day in (date(2026, 1, 15), date(2026, 7, 15), date(2026, 3, 8), date(2026, 11, 1)):
        assert production_days_window_utc(day, day, cal) == local_date_range_to_utc(day, day, CENTRAL_TIME_ZONE)
    assert shift_for(central(2026, 11, 17, 9, 0), cal, WELD) is None
    assert entry_attribution(central(2026, 11, 17, 23, 0), central(2026, 11, 18, 1, 0), cal, WELD) == (
        date(2026, 11, 17),
        None,
    )
    assert weekly_hours_for(cal, WELD) == [0.0] * 7


def test_production_days_window_refusals_match_the_calendar_day_texts():
    with pytest.raises(ValueError, match=START_AFTER_END_DETAIL):
        production_days_window_utc(date(2026, 1, 2), date(2026, 1, 1), CAL)
    with pytest.raises(ValueError, match=END_OUT_OF_RANGE_DETAIL):
        production_days_window_utc(None, date.max, CAL)
    assert production_days_window_utc(None, None, CAL) == (None, None)


def test_weekly_hours_for_werco():
    assert weekly_hours_for(CAL, WELD) == [20.0, 20.0, 20.0, 20.0, 20.0, 0.0, 0.0]
    assert weekly_hours_for(CAL, REST) == [16.0, 16.0, 16.0, 16.0, 16.0, 0.0, 0.0]


# --------------------------------------------------------------------------------------
# Wire-format helpers
# --------------------------------------------------------------------------------------


def test_wall_time_helpers():
    assert hhmm_to_minute("05:00") == 300
    assert minute_to_hhmm(1560) == "02:00"
    assert shift_minutes_from_wall_times("15:30", "02:00") == (930, 1560)
    assert shift_minutes_from_wall_times("15:30", "00:00") == (930, 1440)
    assert shift_minutes_from_wall_times("05:00", "15:30") == (300, 930)
    assert shift_minutes_from_wall_times("07:00", "07:00") == (420, 1860)  # a 24-hour shift
    assert mask_to_days(days_to_mask([4, 0, 2])) == [0, 2, 4]
    for bad in ("24:00", "5", "05:60", "", "aa:bb"):
        with pytest.raises(ValueError):
            hhmm_to_minute(bad)


# --------------------------------------------------------------------------------------
# production_day() agrees with the SQL windows, even for a day start inside a DST shift
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day_start,instant,expected",
    [
        # Spring forward, day start 02:30 (a wall time 2026-03-08 skips): the 3/8 window
        # starts at 08:30Z (fold=0, the CST offset), so 08:10Z (03:10 CDT) is still 3/7.
        pytest.param("02:30", datetime(2026, 3, 8, 8, 10), date(2026, 3, 7), id="spring-forward-gap"),
        pytest.param("02:30", datetime(2026, 3, 8, 8, 30), date(2026, 3, 8), id="spring-forward-boundary"),
        # Fall back, day start 01:30 (a wall time 2026-11-01 repeats): the 11/1 window
        # starts at its FIRST occurrence (06:30Z), so 07:15Z (01:15 CST, second pass) is 11/1.
        pytest.param("01:30", datetime(2026, 11, 1, 7, 15), date(2026, 11, 1), id="fall-back-repeat"),
        pytest.param("01:30", datetime(2026, 11, 1, 6, 29), date(2026, 10, 31), id="fall-back-before"),
    ],
)
def test_production_day_is_the_day_whose_window_contains_the_instant(day_start, instant, expected):
    cal = dataclasses.replace(unconfigured_calendar(), day_start_minute=hhmm_to_minute(day_start))

    assert production_day(instant, cal) == expected
    start_utc, end_utc = production_day_window_utc(expected, cal)
    assert start_utc <= instant < end_utc


def test_production_day_matches_its_window_every_quarter_hour_across_both_dst_weekends():
    """Exhaustive: every day start in the DST hours, every 15 minutes over both DST weekends."""
    for day_start in range(0, 4 * 60, 15):
        cal = dataclasses.replace(unconfigured_calendar(), day_start_minute=day_start)
        for first in (datetime(2026, 3, 7, 0), datetime(2026, 10, 31, 0)):
            instant = first
            while instant < first + timedelta(days=3):
                day = production_day(instant, cal)
                start_utc, end_utc = production_day_window_utc(day, cal)
                assert start_utc <= instant < end_utc, (day_start, instant, day)
                instant += timedelta(minutes=15)


# --------------------------------------------------------------------------------------
# The SQL selection bound (round-2 review, 2026-11-15): a long interval is attributed to a
# later production day than the one it started on, and the selection must still read it.
# --------------------------------------------------------------------------------------


def test_attribution_read_from_is_the_start_of_the_production_day_reach_days_earlier():
    # Tuesday 2026-11-17 -> Sunday 2026-11-15 05:00 CST = 11:00 UTC.
    assert ATTRIBUTION_REACH_DAYS == 2
    assert attribution_read_from(date(2026, 11, 17), CAL) == datetime(2026, 11, 15, 11, 0)
    # Clamped instead of overflowing at the start of the date range.
    assert attribution_read_from(date(1, 1, 2), CAL) == datetime.min


def test_a_24_hour_weld_entry_is_attributed_to_the_next_production_day():
    """Mon 16:00 -> Tue 16:00: W1 Tuesday overlaps 10.5 h, W2 Monday 10 h -> Tuesday's W1."""
    clock_in = central(2026, 11, 16, 16, 0)
    day, occ = entry_attribution(clock_in, clock_in + timedelta(hours=24), CAL, WELD)
    assert (day, occ.shift.code) == (date(2026, 11, 17), "W1")
    # More than the old fixed 12 h look-back before Tuesday's 05:00 start -- the reason the
    # selection now reads back to attribution_read_from instead.
    assert clock_in < production_day_window_utc(day, CAL)[0] - timedelta(hours=12)
    assert clock_in >= attribution_read_from(day, CAL)


def _one_shift_calendar(days):
    """A default pattern with one 07:00-15:00 shift on ``days``; day start 05:00 Central."""
    shift = ShiftDef(
        id=31,
        code="X1",
        name="Only",
        pattern_id=3,
        start_minute=420,
        end_minute=900,
        days_mask=days_to_mask(days),
        planned_minutes=480,
    )
    return build_calendar(
        company_id=1, tz=CENTRAL_TIME_ZONE, day_start_minute=300, version=1, patterns=[(3, True, [], [shift])]
    )


@pytest.mark.parametrize(
    "cal, work_center_id",
    [
        (CAL, WELD),
        (CAL, REST),
        (_one_shift_calendar([0, 1, 2, 3, 4]), None),
        (_one_shift_calendar([2]), None),  # Wednesday only: long intervals reach two days ahead
        (_one_shift_calendar([3]), None),  # Thursday only: beyond the reach from Monday
    ],
)
def test_every_attribution_lies_inside_the_sql_selection(cal, work_center_id):
    """The invariant ``_attribution_candidates`` rests on, over starts every 75 min and long lengths.

    An interval attributed to production day ``d`` starts at or after
    ``attribution_read_from(d)``, starts before ``d``'s window ends, and either starts inside
    the window or ends after it begins. If the attribution scan ever reached further than
    ``ATTRIBUTION_REACH_DAYS``, the selection would silently drop rows again.
    """
    first = central(2026, 11, 15, 0)
    for step in range(0, 6 * 24 * 60, 75):
        clock_in = first + timedelta(minutes=step)
        for hours in (0, 1, 8, 13, 24, 30, 42, 58, 80, 200):
            clock_out = clock_in + timedelta(hours=hours)
            day, _occ = entry_attribution(clock_in, clock_out, cal, work_center_id)
            window_start, window_end = production_day_window_utc(day, cal)
            assert clock_in >= attribution_read_from(day, cal), (clock_in, hours, day)
            assert clock_in < window_end, (clock_in, hours, day)
            assert clock_in >= window_start or clock_out > window_start, (clock_in, hours, day)


def test_a_wednesday_only_shift_takes_a_58_hour_entry_from_monday():
    cal = _one_shift_calendar([2])
    day, occ = entry_attribution(central(2026, 11, 16, 6, 0), central(2026, 11, 18, 16, 0), cal, None)
    assert (day, occ.shift.code) == (date(2026, 11, 18), "X1")
