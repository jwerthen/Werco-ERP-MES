"""``app.services.maintenance_interval`` -- when a PM schedule comes due next.

Owner decision 2026-11-20: monthly, quarterly, semi-annual and annual schedules add CALENDAR months
to the completion (company) day, clamped to the end of a shorter month; daily, weekly, biweekly and
custom add days. The old fixed day counts (30 / 90 / 182 / 365) made an on-time PM drift earlier
every cycle. Pure functions, so pure tests; every expected date is a literal, and the cases are
chosen so the old day counts give a DIFFERENT answer wherever one is noted.
"""

from datetime import date, timedelta

import pytest

from app.models.maintenance import FREQUENCY_DAYS_MAP, FREQUENCY_MONTHS_MAP, MaintenanceFrequency
from app.services.maintenance_interval import DEFAULT_INTERVAL_DAYS, add_months, next_due_date

pytestmark = pytest.mark.unit

F = MaintenanceFrequency


class TestAddMonths:
    @pytest.mark.parametrize(
        "start,months,expected",
        [
            (date(2026, 3, 15), 1, date(2026, 4, 15)),
            (date(2026, 12, 15), 1, date(2027, 1, 15)),  # across the year end
            (date(2027, 1, 31), 1, date(2027, 2, 28)),  # clamped: 2027 is not a leap year
            (date(2028, 1, 31), 1, date(2028, 2, 29)),  # clamped: 2028 is
            (date(2026, 3, 31), 1, date(2026, 4, 30)),
            (date(2026, 11, 30), 3, date(2027, 2, 28)),
            (date(2026, 8, 31), 6, date(2027, 2, 28)),
            (date(2028, 2, 29), 12, date(2029, 2, 28)),
            (date(2028, 2, 29), 48, date(2032, 2, 29)),  # four years on, a leap day again
            (date(2026, 1, 1), 0, date(2026, 1, 1)),
            (date(2026, 1, 31), 13, date(2027, 2, 28)),
            (date(2026, 12, 31), 24, date(2028, 12, 31)),
        ],
    )
    def test_calendar_months_clamped_to_a_shorter_month(self, start, months, expected):
        assert add_months(start, months) == expected


class TestNextDueDate:
    @pytest.mark.parametrize(
        "frequency,completed_on,expected,old_day_count_answer",
        [
            (F.MONTHLY, date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 2)),
            (F.MONTHLY, date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 1)),
            (F.MONTHLY, date(2026, 3, 15), date(2026, 4, 15), date(2026, 4, 14)),
            (F.MONTHLY, date(2026, 12, 15), date(2027, 1, 15), date(2027, 1, 14)),
            (F.QUARTERLY, date(2026, 11, 30), date(2027, 2, 28), None),  # 90 days happens to agree
            (F.QUARTERLY, date(2027, 1, 31), date(2027, 4, 30), date(2027, 5, 1)),
            (F.SEMI_ANNUAL, date(2026, 8, 31), date(2027, 2, 28), date(2027, 3, 1)),
            (F.ANNUAL, date(2028, 2, 29), date(2029, 2, 28), None),  # 365 days happens to agree
            (F.ANNUAL, date(2027, 3, 1), date(2028, 3, 1), date(2028, 2, 29)),
        ],
    )
    def test_calendar_cadences_add_calendar_months(self, frequency, completed_on, expected, old_day_count_answer):
        assert next_due_date(frequency, None, completed_on) == expected
        if old_day_count_answer is not None:
            assert old_day_count_answer != expected

    def test_the_clamp_sticks_across_completions(self):
        """Anchoring to the original day of month would need an anchor column; the owner accepted the
        clamp: Jan 31 -> Feb 28 -> Mar 28, not back to Mar 31."""
        first = next_due_date(F.MONTHLY, None, date(2027, 1, 31))
        second = next_due_date(F.MONTHLY, None, first)
        assert (first, second) == (date(2027, 2, 28), date(2027, 3, 28))

    def test_frequency_days_is_ignored_on_a_calendar_cadence(self):
        assert next_due_date(F.MONTHLY, 45, date(2027, 1, 31)) == date(2027, 2, 28)

    @pytest.mark.parametrize(
        "frequency,expected",
        [
            (F.DAILY, date(2027, 2, 1)),
            (F.WEEKLY, date(2027, 2, 7)),
            (F.BIWEEKLY, date(2027, 2, 14)),
        ],
    )
    def test_day_cadences_add_days(self, frequency, expected):
        assert next_due_date(frequency, None, date(2027, 1, 31)) == expected
        # A stray frequency_days does not change a day cadence either.
        assert next_due_date(frequency, 45, date(2027, 1, 31)) == expected

    def test_custom_adds_its_frequency_days(self):
        assert next_due_date(F.CUSTOM, 45, date(2027, 1, 31)) == date(2027, 3, 17)
        assert next_due_date(F.CUSTOM, 1, date(2028, 2, 28)) == date(2028, 2, 29)

    @pytest.mark.parametrize("frequency_days", [None, 0])
    def test_custom_without_days_falls_back_to_30(self, frequency_days):
        assert DEFAULT_INTERVAL_DAYS == 30
        assert next_due_date(F.CUSTOM, frequency_days, date(2027, 1, 31)) == date(2027, 3, 2)

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("monthly", date(2027, 2, 28)),
            ("quarterly", date(2027, 4, 30)),
            ("annual", date(2028, 1, 31)),
            ("weekly", date(2027, 2, 7)),
        ],
    )
    def test_a_string_value_works_as_the_enum(self, value, expected):
        """``MaintenanceFrequency`` is a ``str`` enum, so a raw value read from a row or a request
        looks up the same map entry as the member."""
        assert next_due_date(value, None, date(2027, 1, 31)) == expected
        assert next_due_date(MaintenanceFrequency(value), None, date(2027, 1, 31)) == expected

    def test_every_frequency_has_exactly_one_rule(self):
        """A new frequency must be classified: a day count, a month count, or custom's own days."""
        assert not set(FREQUENCY_DAYS_MAP) & set(FREQUENCY_MONTHS_MAP)
        assert set(FREQUENCY_DAYS_MAP) | set(FREQUENCY_MONTHS_MAP) | {F.CUSTOM} == set(F)
        assert FREQUENCY_DAYS_MAP == {F.DAILY: 1, F.WEEKLY: 7, F.BIWEEKLY: 14}
        assert FREQUENCY_MONTHS_MAP == {F.MONTHLY: 1, F.QUARTERLY: 3, F.SEMI_ANNUAL: 6, F.ANNUAL: 12}

    def test_the_result_is_never_before_the_completion_day(self):
        start = date(2027, 1, 1)
        for offset in range(0, 366 * 2):
            day = start + timedelta(days=offset)
            for frequency in F:
                assert next_due_date(frequency, 45, day) > day, (frequency, day)
