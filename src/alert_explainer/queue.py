"""Priority queue + worker pool.

WAF patterns:
- Priority Queue — `critical` alerts are dequeued before `warning` before `info`.
- Queue-Based Load Leveling — incoming bursts are buffered; the worker pool drains at a steady rate.

Starts in-memory (asyncio.PriorityQueue). Graduate to Redis Streams or NATS when you need
multi-replica fairness, cross-instance backpressure, or persistence across restarts.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable

import structlog
from cloudandsre import AsyncCircuitBreaker
from cloudandsre.circuit_breaker import CircuitOpenError

from .config import settings
from .enrich import EnrichmentSchemaError, enrich_alert
from .models import Alert, EnrichedAlert

log = structlog.get_logger()

_seq = itertools.count()  # tiebreaker so PriorityQueue never tries to compare Alert objects

EnrichmentSink = Callable[[EnrichedAlert], Awaitable[None]]


class AlertWorkQueue:
    def __init__(self, *, sink: EnrichmentSink) -> None:
        self._q: asyncio.PriorityQueue[tuple[int, int, float, Alert]] = asyncio.PriorityQueue(
            maxsize=settings.queue_maxsize
        )
        self._sink = sink
        self._breaker: AsyncCircuitBreaker[None] = AsyncCircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            reset_after_seconds=settings.breaker_reset_seconds,
            # A malformed reply means the LLM is up and answering, just not in
            # the shape we asked for. Counting that as a breaker failure let bad
            # model output degrade enrichment for every alert behind it.
            ignore_exceptions=(EnrichmentSchemaError,),
        )
        self._workers: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def depth(self) -> int:
        return self._q.qsize()

    @property
    def breaker_open(self) -> bool:
        return self._breaker.is_open

    async def enqueue(self, alert: Alert) -> bool:
        """Returns True if accepted, False if the queue is full (caller decides what to do)."""
        try:
            self._q.put_nowait((alert.priority_rank, next(_seq), time.monotonic(), alert))
            return True
        except asyncio.QueueFull:
            log.warning(
                "queue_full_dropping_alert",
                alertname=alert.alertname,
                severity=alert.severity,
            )
            return False

    async def start(self, n_workers: int | None = None) -> None:
        if self._running:
            return
        self._running = True
        n = n_workers or settings.worker_concurrency
        for i in range(n):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        log.info("workers_started", count=n)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        log.info("workers_stopped")

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                _, _, _, alert = await self._q.get()
            except asyncio.CancelledError:
                return

            try:
                enriched = await self._enrich_one(alert)
                await self._sink(enriched)
            except Exception as e:  # last-resort guard so one alert can never kill the worker
                log.exception("worker_unexpected_error", worker_id=worker_id, error=str(e))
            finally:
                self._q.task_done()

    async def _enrich_one(self, alert: Alert) -> EnrichedAlert:
        try:
            enrichment = await self._breaker.call(lambda: enrich_alert(alert))
            return EnrichedAlert(alert=alert, enrichment=enrichment)
        except CircuitOpenError:
            log.warning(
                "circuit_open_passthrough",
                alertname=alert.alertname,
                severity=alert.severity,
            )
            return EnrichedAlert(alert=alert, enrichment_error="llm_circuit_open")
        except Exception as e:
            log.warning(
                "enrichment_failed_passthrough",
                alertname=alert.alertname,
                severity=alert.severity,
                error=str(e),
            )
            return EnrichedAlert(alert=alert, enrichment_error=str(e))
