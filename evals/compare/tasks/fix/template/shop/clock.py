"""Time source that tests can pin."""

import datetime


class Clock:
    """Wall clock in UTC."""

    def now(self):
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    def today(self):
        return self.now().date()


class FixedClock(Clock):
    """A clock that only moves when told to."""

    def __init__(self, start):
        self._now = start

    def now(self):
        return self._now

    def advance(self, **kwargs):
        self._now = self._now + datetime.timedelta(**kwargs)
        return self._now

    def set(self, value):
        self._now = value
