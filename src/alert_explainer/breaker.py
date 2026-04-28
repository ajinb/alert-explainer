"""Minimal async circuit breaker.

WAF pattern: Circuit Breaker. Wraps an unreliable async call (the LLM) and short-circuits to a
fallback after N consecutive failures, auto-resetting after a cool-down. Keeping it inline rather
than pulling in `pybreaker` or `aiocircuitbreaker` because the simplicity philosophy says: 40 lines
of state in one file beats a dependency you do not need yet. Graduate to a library when you need
half-open probing or per-tenant breakers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class CircuitOpen(Exception):
    """Raised when the breaker is open and the call is short-circuited."""


class CircuitBreaker(Generic[T]):
    def __init__(self, *, failure_threshold: int, reset_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        # Past the reset window the breaker becomes retry-eligible; the caller's next
        # success or failure decides whether the breaker stays closed or re-opens.
        return (time.monotonic() - self._opened_at) < self._reset_seconds

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if self.is_open:
                raise CircuitOpen("circuit breaker is open")

        try:
            result = await fn()
        except Exception:
            async with self._lock:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._opened_at = time.monotonic()
            raise

        async with self._lock:
            self._failures = 0
            self._opened_at = None
        return result
