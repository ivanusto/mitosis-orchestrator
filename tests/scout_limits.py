"""Safe defaults shared by tests/scout_oracle.py and tests/scout_census.py.

A fuzz or census run starts many shells at once. On a machine that also hosts an inference engine that
can push available memory low enough for the OOM killer to take the engine. Two guards:

  child_preexec(gib)   RLIMIT_AS for every spawned child (0 disables)
  MemoryFloor          read MemAvailable before each new run, stop starting runs under a floor

Python 3.8, standard library only.
"""

import threading
from typing import Callable, Optional

try:
    import resource
except ImportError:  # Windows: no RLIMIT_AS, and Popen has no preexec_fn there either
    resource = None  # type: ignore

DEFAULT_CHILD_AS_GIB = 4.0
DEFAULT_MIN_AVAILABLE_GIB = 3.0
MEMINFO = "/proc/meminfo"


def read_available_gib(path: str = MEMINFO) -> Optional[float]:
    """MemAvailable in GiB, or None when it cannot be read (no floor then)."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024.0 * 1024.0)
    except (OSError, ValueError, IndexError):
        return None
    return None


def child_preexec(gib: float) -> Optional[Callable[[], None]]:
    """A preexec_fn that caps the address space of the child, or None when gib is 0 or negative or `resource` is absent (Windows)."""
    if resource is None or not gib or gib <= 0:
        return None
    limit = int(gib * 1024 * 1024 * 1024)

    def apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (OSError, ValueError):
            pass

    return apply


class MemoryFloor:
    """allow() is called before starting each run. Under min_gib it answers False and counts a skip.
    The reader is injectable for tests; a None reading means no floor."""

    def __init__(self, min_gib: float = DEFAULT_MIN_AVAILABLE_GIB,
                 reader: Callable[[], Optional[float]] = read_available_gib) -> None:
        self.min_gib = min_gib
        self.reader = reader
        self.skipped = 0
        self.lowest: Optional[float] = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        if not self.min_gib or self.min_gib <= 0:
            return True
        avail = self.reader()
        if avail is None:
            return True
        with self._lock:
            if self.lowest is None or avail < self.lowest:
                self.lowest = avail
            if avail < self.min_gib:
                self.skipped += 1
                return False
        return True

    def report(self) -> dict:
        return {"min_available_gib": self.min_gib, "skipped_for_memory": self.skipped,
                "lowest_available_gib": None if self.lowest is None else round(self.lowest, 2)}
