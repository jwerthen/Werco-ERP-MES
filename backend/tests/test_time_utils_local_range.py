"""Unit tests for the company-local calendar-day helpers in ``app.core.time_utils``.

A calendar date the user names ("January 15") is a LOCAL day. Against the naive-UTC
timestamp columns the schema uses, it must run from local midnight to the next local
midnight converted to UTC -- not from UTC midnight, which cut a Central shop's day off
at 6 PM (CST) / 7 PM (CDT) and moved every second-shift row into the next day.

Pinned here: the CST and CDT boundaries, both 2026 DST transition days (23 h and
25 h), the exclusive end bound, the 422 messages, the zone fallback (Central, never
UTC), and the local-with-offset export cell format.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.time_utils import (
    CENTRAL_TIME_ZONE,
    DEFAULT_COMPANY_TIMEZONE,
    END_OUT_OF_RANGE_DETAIL,
    START_AFTER_END_DETAIL,
    local_date_range_to_utc,
    local_today,
    local_window,
    resolve_zone,
    to_local_date,
    to_local_iso,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "day,expected_start,expected_end",
    [
        # CST (UTC-6): a 24 h day from 06:00Z to 06:00Z.
        (date(2026, 1, 15), datetime(2026, 1, 15, 6), datetime(2026, 1, 16, 6)),
        # CDT (UTC-5): a 24 h day from 05:00Z to 05:00Z.
        (date(2026, 7, 15), datetime(2026, 7, 15, 5), datetime(2026, 7, 16, 5)),
        # Spring forward: 23 h day (CST start, CDT end).
        (date(2026, 3, 8), datetime(2026, 3, 8, 6), datetime(2026, 3, 9, 5)),
        # Fall back: 25 h day (CDT start, CST end).
        (date(2026, 11, 1), datetime(2026, 11, 1, 5), datetime(2026, 11, 2, 6)),
    ],
)
def test_single_central_day_is_local_midnight_to_next_local_midnight(day, expected_start, expected_end):
    start_utc, end_utc = local_date_range_to_utc(day, day, CENTRAL_TIME_ZONE)

    assert (start_utc, end_utc) == (expected_start, expected_end)
    # naive=True is the default: the bounds compare against naive-UTC DateTime columns.
    assert start_utc.tzinfo is None and end_utc.tzinfo is None


def test_dst_day_lengths_are_23_and_25_hours():
    spring = local_date_range_to_utc(date(2026, 3, 8), date(2026, 3, 8), CENTRAL_TIME_ZONE)
    fall = local_date_range_to_utc(date(2026, 11, 1), date(2026, 11, 1), CENTRAL_TIME_ZONE)

    assert (spring[1] - spring[0]).total_seconds() == 23 * 3600
    assert (fall[1] - fall[0]).total_seconds() == 25 * 3600


def test_multi_day_range_spans_a_dst_change():
    start_utc, end_utc = local_date_range_to_utc(date(2026, 3, 1), date(2026, 3, 31), CENTRAL_TIME_ZONE)

    assert start_utc == datetime(2026, 3, 1, 6)  # CST
    assert end_utc == datetime(2026, 4, 1, 5)  # CDT


def test_naive_false_returns_aware_utc_for_timezone_aware_columns():
    start_utc, end_utc = local_date_range_to_utc(date(2026, 1, 15), date(2026, 1, 15), CENTRAL_TIME_ZONE, naive=False)

    assert start_utc == datetime(2026, 1, 15, 6, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 1, 16, 6, tzinfo=timezone.utc)
    assert start_utc.utcoffset().total_seconds() == 0


def test_open_ended_sides_stay_none():
    assert local_date_range_to_utc(None, None, CENTRAL_TIME_ZONE) == (None, None)
    assert local_date_range_to_utc(date(2026, 1, 15), None, CENTRAL_TIME_ZONE) == (datetime(2026, 1, 15, 6), None)
    assert local_date_range_to_utc(None, date(2026, 1, 15), CENTRAL_TIME_ZONE) == (None, datetime(2026, 1, 16, 6))


def test_reversed_range_raises_the_422_text():
    with pytest.raises(ValueError, match=START_AFTER_END_DETAIL):
        local_date_range_to_utc(date(2026, 1, 16), date(2026, 1, 15), CENTRAL_TIME_ZONE)


def test_end_date_max_raises_instead_of_overflowing():
    with pytest.raises(ValueError, match=END_OUT_OF_RANGE_DETAIL):
        local_date_range_to_utc(None, date.max, CENTRAL_TIME_ZONE)


def test_unrepresentable_start_raises_value_error_not_overflow():
    # date.min at local midnight east of UTC is before datetime.min in UTC.
    with pytest.raises(ValueError):
        local_date_range_to_utc(date.min, None, ZoneInfo("Etc/GMT-14"))


def test_the_old_combine_window_and_the_new_one_disagree_by_the_offset():
    """The regression: 7 PM CST on Jan 15 is 01:00Z on Jan 16 -- inside Jan 15 now."""
    start_utc, end_utc = local_date_range_to_utc(date(2026, 1, 15), date(2026, 1, 15), CENTRAL_TIME_ZONE)
    second_shift = datetime(2026, 1, 16, 1, 0)  # 19:00 CST Jan 15
    previous_evening = datetime(2026, 1, 15, 3, 0)  # 21:00 CST Jan 14

    assert start_utc <= second_shift < end_utc
    assert not (start_utc <= previous_evening < end_utc)


@pytest.mark.parametrize("name", [None, "", "   ", "Not/AZone", "../etc/passwd", "/etc/localtime"])
def test_resolve_zone_falls_back_to_central_never_utc(name):
    assert resolve_zone(name) == ZoneInfo(DEFAULT_COMPANY_TIMEZONE)


def test_resolve_zone_honors_a_real_zone():
    assert resolve_zone("America/New_York") == ZoneInfo("America/New_York")
    assert resolve_zone(" America/Denver ") == ZoneInfo("America/Denver")


def test_another_company_zone_moves_the_day():
    start_utc, end_utc = local_date_range_to_utc(date(2026, 1, 15), date(2026, 1, 15), ZoneInfo("America/New_York"))

    assert (start_utc, end_utc) == (datetime(2026, 1, 15, 5), datetime(2026, 1, 16, 5))


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 1, 15, 23, 30), "2026-01-15T17:30:00-06:00"),
        (datetime(2026, 7, 15, 23, 30), "2026-07-15T18:30:00-05:00"),
        # An aware value is converted, not re-labelled.
        (datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc), "2026-01-15T17:30:00-06:00"),
        # Sub-second precision is dropped: export cells are to the second.
        (datetime(2026, 1, 15, 23, 30, 5, 999999), "2026-01-15T17:30:05-06:00"),
        # 01:30 happens twice on fall-back day: 06:30Z is the first (CDT), 07:30Z the second (CST).
        (datetime(2026, 11, 1, 6, 30), "2026-11-01T01:30:00-05:00"),
        (datetime(2026, 11, 1, 7, 30), "2026-11-01T01:30:00-06:00"),
    ],
)
def test_to_local_iso_carries_the_offset(instant, expected):
    assert to_local_iso(instant, CENTRAL_TIME_ZONE) == expected


def test_to_local_iso_none_passthrough():
    assert to_local_iso(None, CENTRAL_TIME_ZONE) is None


def test_to_local_date_reads_naive_as_utc():
    assert to_local_date(datetime(2026, 1, 16, 1, 0), CENTRAL_TIME_ZONE) == date(2026, 1, 15)
    assert to_local_date(datetime(2026, 1, 16, 6, 0), CENTRAL_TIME_ZONE) == date(2026, 1, 16)
    assert to_local_date(None, CENTRAL_TIME_ZONE) is None


def test_local_today_is_the_wall_clock_date_not_utc():
    # 02:00Z on Jan 16 is still Jan 15 in Chicago (8 PM CST).
    assert local_today(CENTRAL_TIME_ZONE, now=datetime(2026, 1, 16, 2, 0)) == date(2026, 1, 15)
    assert local_today(CENTRAL_TIME_ZONE, now=datetime(2026, 1, 16, 6, 0)) == date(2026, 1, 16)
    assert local_today(CENTRAL_TIME_ZONE, now=datetime(2026, 7, 16, 4, 59, tzinfo=timezone.utc)) == date(2026, 7, 15)


def test_local_window_matches_the_range_and_reads_a_reversed_range_as_empty():
    assert local_window(date(2026, 1, 15), date(2026, 1, 15), CENTRAL_TIME_ZONE) == (
        datetime(2026, 1, 15, 6),
        datetime(2026, 1, 16, 6),
    )
    # Services keep the old "reversed = no rows" behavior instead of raising mid-read.
    assert local_window(date(2026, 1, 16), date(2026, 1, 15), CENTRAL_TIME_ZONE) == (
        datetime(2026, 1, 16, 6),
        datetime(2026, 1, 16, 6),
    )
