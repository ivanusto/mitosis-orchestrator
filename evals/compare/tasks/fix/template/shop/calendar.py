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
