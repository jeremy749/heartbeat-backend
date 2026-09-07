"""Failure throttling for the login endpoint.

Deliberately small: a sliding window of recent failures per key, held in memory.
It resets when the process restarts and is not shared between workers, so it
slows guessing down rather than guaranteeing a limit - which is the right
trade-off for a single-instance deployment. Put the counts in Redis or a table
if this ever runs behind more than one worker.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict

# Stop one client's misses from evicting everyone else's, but don't let the map
# grow without bound either: an attacker rotating keys would otherwise be able
# to pin memory.
MAX_TRACKED_KEYS = 10_000


class FailureLimiter:
    """Counts recent failures per key and says how long to wait after too many."""

    def __init__(self, max_failures: int, window_seconds: float) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> Deque[float]:
        """This key's failures inside the window; drops the ones that aged out."""
        q = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def retry_after(self, key: str) -> float:
        """Seconds the caller must wait, or 0.0 when it may proceed."""
        now = time.monotonic()
        with self._lock:
            q = self._recent(key, now)
            if len(q) < self.max_failures:
                return 0.0
            # Free again once the oldest failure in the window ages out.
            return max(0.0, self.window_seconds - (now - q[0]))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._recent(key, now).append(now)
            if len(self._hits) > MAX_TRACKED_KEYS:
                self._sweep(now)

    def reset(self, key: str) -> None:
        """Forget a key's failures - called after a successful sign-in."""
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def _sweep(self, now: float) -> None:
        """Drop keys whose failures have all aged out. Caller holds the lock."""
        cutoff = now - self.window_seconds
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[key]
