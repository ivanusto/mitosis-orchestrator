"""Business-day arithmetic."""

import datetime

ONE_DAY = datetime.timedelta(days=1)


def holidays_for(year):
    """Fixed holidays observed by the warehouse."""
    return {
        datetime.date(year, 1, 1),
        datetime.date(year, 12, 25),
        datetime.date(year, 12, 26),
    }


def is_business_day(day, holidays=frozenset()):
    return day.weekday() < 5 and day not in holidays


def next_business_day(day, holidays=frozenset()):
    """The first business day on or after day."""
    while not is_business_day(day, holidays):
        day += ONE_DAY
    return day


def add_business_days(start, count, holidays=frozenset()):
    """Move count business days forward from start (start itself is not counted)."""
    if count < 0:
        raise ValueError("count must not be negative")
    day = start
    remaining = count
    while remaining > 0:
        day += ONE_DAY
        if is_business_day(day, holidays):
            remaining -= 1
    return day


def business_days_between(start, end, holidays=frozenset()):
    """Number of business days in (start, end]."""
    if end < start:
        return -business_days_between(end, start, holidays)
    total = 0
    day = start
    while day < end:
        day += ONE_DAY
        if is_business_day(day, holidays):
            total += 1
    return total


_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    if not 1 <= month <= 12:
        raise ValueError("month must be 1..12, got %r" % (month,))
    return 29 if month == 2 and is_leap_year(year) else _DAYS[month - 1]


def month_end(day):
    return datetime.date(day.year, day.month, days_in_month(day.year, day.month))


def quarter_of(day):
    return (day.month - 1) // 3 + 1


def add_months(day, months):
    """The same day of the month, `months` later (negative goes back).

    A day that does not exist in the target month is clamped to its last day, so 31 January plus
    one month is the last day of February.
    """
    index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return datetime.date(year, month, min(day.day, days_in_month(year, month)))
