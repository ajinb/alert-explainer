"""FastAPI app — Alertmanager webhook receiver + health endpoints."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Response, status

from . import __version__, sink
from .config import settings
from .models import AlertmanagerPayload
from .queue import AlertWorkQueue

logging.basicConfig(level=settings.log_level)
log = structlog.get_logger()

queue = AlertWorkQueue(sink=sink.send)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    await queue.start()
    yield
    await queue.stop()


app = FastAPI(title="alert-explainer", version=__version__, lifespan=lifespan)


@app.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_alertmanager(payload: AlertmanagerPayload) -> dict[str, Any]:
    """Alertmanager webhook entry point. Buffers alerts onto the priority queue and returns
    immediately so Alertmanager's own retries do not stack up behind LLM latency."""
    accepted = 0
    dropped = 0
    for alert in payload.alerts:
        if await queue.enqueue(alert):
            accepted += 1
        else:
            dropped += 1
    log.info(
        "webhook_received",
        accepted=accepted,
        dropped=dropped,
        receiver=payload.receiver,
        depth=queue.depth,
    )
    return {"accepted": accepted, "dropped": dropped, "depth": queue.depth}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — process is up. Always 200 unless the event loop is wedged."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness — true when we are willing to accept traffic. Goes 503 when the queue is
    saturated or the LLM circuit breaker is open, so Alertmanager / load balancers can shed."""
    queue_full = queue.depth >= int(settings.queue_maxsize * 0.95)
    breaker_open = queue.breaker_open
    ready = not (queue_full or breaker_open)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": ready,
        "queue_depth": queue.depth,
        "queue_maxsize": settings.queue_maxsize,
        "breaker_open": breaker_open,
    }


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Coarse internal counters in JSON. Swap to prometheus_client + a /metrics text endpoint when
    you wire this into Prometheus for real."""
    return {
        "queue_depth": queue.depth,
        "queue_maxsize": settings.queue_maxsize,
        "breaker_open": queue.breaker_open,
        "version": __version__,
        "model": settings.model,
        "slo_p50_latency_ms": settings.slo_p50_latency_ms,
        "slo_cost_per_alert_usd": settings.slo_cost_per_alert_usd,
    }
