"""Token-bucket rate limiting for the public API."""


class TokenBucket:
    def __init__(self, clock, capacity, refill_per_second):
        self.clock = clock
        self.capacity = capacity
        self.refill = refill_per_second
        self._tokens = float(capacity)
        self._stamp = clock.now()

    def _top_up(self):
        now = self.clock.now()
        elapsed = (now - self._stamp).total_seconds()
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill)
        self._stamp = now

    def allow(self, cost=1):
        self._top_up()
        if cost > self._tokens:
            return False
        self._tokens -= cost
        return True

    def available(self):
        self._top_up()
        return int(self._tokens)

    def seconds_until(self, cost=1):
        self._top_up()
        if cost <= self._tokens:
            return 0.0
        if self.refill <= 0:
            return float("inf")
        return (cost - self._tokens) / self.refill


class KeyedLimiter:
    """One bucket per key (for example per customer id)."""

    def __init__(self, clock, capacity, refill_per_second):
        self._args = (clock, capacity, refill_per_second)
        self._buckets = {}

    def allow(self, key, cost=1):
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(*self._args)
        return self._buckets[key].allow(cost)
