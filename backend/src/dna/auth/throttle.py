"""Brute-force protection for the password login.

Counts *failed* attempts in a sliding window, per (client address, user name) and per
user name alone, and refuses further attempts until the oldest failure ages out.  A
successful login clears the counters for that pair.

It is held in process memory, which is the right size for one instance.  Behind several
instances each keeps its own count, so the effective limit is the configured one times
the instance count; put a rate limit on the reverse proxy for a hard guarantee.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Optional

# The per-user limit is looser than the per-(address, user) limit: it exists to stop
# many addresses guessing one account, not to lock a real user out after a few typos.
_USER_FACTOR = 3
_MAX_KEYS = 10_000


class LoginThrottle:
    def __init__(
        self,
        max_failures: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._clock = clock
        self._failures: OrderedDict[str, Deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _keys(address: str, username: str) -> list[tuple[str, int]]:
        user = (username or "").strip().lower()
        return [(f"a:{address}|{user}", 1), (f"u:{user}", _USER_FACTOR)]

    def _recent(self, key: str, now: float) -> Deque[float]:
        events = self._failures.get(key)
        if events is None:
            return deque()
        while events and now - events[0] >= self.window:
            events.popleft()
        if not events:
            del self._failures[key]
            return deque()
        return events

    def retry_after(self, address: str, username: str) -> int:
        """Seconds the caller must wait, or 0 when an attempt is allowed."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key, factor in self._keys(address, username):
                events = self._recent(key, now)
                limit = self.max_failures * factor
                if len(events) >= limit:
                    wait = max(wait, events[len(events) - limit] + self.window - now)
        return int(wait) + 1 if wait > 0 else 0

    def failed(self, address: str, username: str) -> None:
        now = self._clock()
        with self._lock:
            for key, _ in self._keys(address, username):
                self._recent(key, now)
                self._failures.setdefault(key, deque()).append(now)
                self._failures.move_to_end(key)  # most recently active last
            while len(self._failures) > _MAX_KEYS:
                self._failures.popitem(last=False)  # forget the stalest

    def succeeded(self, address: str, username: str) -> None:
        with self._lock:
            self._failures.pop(self._keys(address, username)[0][0], None)


_throttle: Optional[LoginThrottle] = None


def get_login_throttle() -> Optional[LoginThrottle]:
    """The shared throttle built from configuration, or None when disabled."""
    global _throttle
    from dna.configsys import get_config

    cfg = get_config().get("auth").login_throttle
    if not cfg.enabled:
        return None
    if (
        _throttle is None
        or _throttle.max_failures != cfg.max_failures
        or _throttle.window != cfg.window_seconds
    ):
        _throttle = LoginThrottle(cfg.max_failures, cfg.window_seconds)
    return _throttle


def reset_login_throttle() -> None:
    global _throttle
    _throttle = None
