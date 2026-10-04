"""Token-bucket rate limiter with separate read/write budgets."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """A simple token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: int) -> None:
        """
        Args:
            rate: Tokens added per second.
            capacity: Maximum tokens in the bucket.
        """
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, tokens: int = 1) -> None:
        """Wait until the requested number of tokens are available."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # Calculate wait time for enough tokens
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate
                # Release lock while sleeping
                self._lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await self._lock.acquire()


class DualRateLimiter:
    """Separate rate limiters for read and write operations, matching Kalshi tiers."""

    def __init__(
        self,
        read_rate: float = 20.0,
        read_capacity: int = 20,
        write_rate: float = 10.0,
        write_capacity: int = 10,
    ) -> None:
        self.read_limiter = TokenBucket(rate=read_rate, capacity=read_capacity)
        self.write_limiter = TokenBucket(rate=write_rate, capacity=write_capacity)

    async def acquire_read(self) -> None:
        await self.read_limiter.acquire()

    async def acquire_write(self) -> None:
        await self.write_limiter.acquire()
