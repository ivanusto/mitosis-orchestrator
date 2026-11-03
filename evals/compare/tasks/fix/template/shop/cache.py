"""A small time-to-live cache driven by an injectable clock."""


class TTLCache:
    def __init__(self, clock, ttl_seconds):
        self.clock = clock
        self.ttl = ttl_seconds
        self._data = {}
        self.hits = 0
        self.misses = 0

    def _expired(self, stamp):
        return (self.clock.now() - stamp).total_seconds() >= self.ttl

    def set(self, key, value):
        self._data[key] = (self.clock.now(), value)

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None or self._expired(entry[0]):
            self._data.pop(key, None)
            self.misses += 1
            return default
        self.hits += 1
        return entry[1]

    def get_or_compute(self, key, compute):
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            value = compute()
            self.set(key, value)
        return value

    def invalidate(self, key):
        return self._data.pop(key, None) is not None

    def purge(self):
        """Drop every expired entry and return how many were dropped."""
        stale = [k for k, (stamp, _) in self._data.items() if self._expired(stamp)]
        for key in stale:
            del self._data[key]
        return len(stale)

    def stats(self):
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses, "size": len(self._data), "ratio": (self.hits / total) if total else 0.0}
