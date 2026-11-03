"""Bounded TTL caches for retry-storm suppression and enrichment reuse.

In-process, like the queue. A multi-replica deployment needs a shared store
(Redis ``SET NX EX``) for both — the interface here is deliberately that small.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """LRU-bounded mapping whose entries expire ``ttl_seconds`` after insertion."""

    def __init__(
        self,
        ttl_seconds: float,
        maxsize: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if self._clock() >= expires:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: V) -> None:
        if self._ttl <= 0:
            return
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def add_if_absent(self, key: str, value: V) -> bool:
        """Insert and return True, or return False if a live entry already exists."""
        if self.get(key) is not None:
            return False
        self.set(key, value)
        return True
