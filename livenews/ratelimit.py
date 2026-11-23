from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    """Sliding one-minute window. Callers await acquire() before each request.

    Alpaca's free data plan allows 200 requests a minute across all data
    endpoints, so every Alpaca data poller shares one of these, set a bit
    under the hard cap.
    """

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = max(1, per_minute)
        self._clock = clock
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _trim(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= 60.0:
            self._hits.popleft()

    def used(self) -> int:
        self._trim(self._clock())
        return len(self._hits)

    def try_acquire(self) -> bool:
        now = self._clock()
        self._trim(now)
        if len(self._hits) < self.per_minute:
            self._hits.append(now)
            return True
        return False

    async def acquire(self) -> None:
        async with self._lock:
            while not self.try_acquire():
                wait = 60.0 - (self._clock() - self._hits[0])
                await asyncio.sleep(max(0.05, wait))
