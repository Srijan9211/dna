"""A small thread-safe cache whose entries expire.

Used for lookups that repeat on every request but change rarely (a version's
reviewable list, a user's display name).  Entries live a few seconds to a minute, so a
change shows up quickly, and the cache is bounded so it cannot grow without limit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable


class TTLCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> tuple[bool, Any]:
        """``(True, value)`` for a live entry, else ``(False, None)``."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            if entry[0] <= self._clock():
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, entry[1]

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
