"""FastAPI app — Alertmanager webhook receiver + health endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from . import __version__, metrics, sink
from .config import settings
from .dedup import TTLCache
from .models import AlertmanagerPayload
from .queue import AlertWorkQueue

logging.basicConfig(level=settings.log_level)
log = structlog.get_logger()

queue = AlertWorkQueue(sink=sink.send)
# (fingerprint, status, startsAt) seen inside the window → Alertmanager retry or repeat.
recent_alerts: TTLCache[bool] = TTLCache(settings.dedup_window_seconds, settings.cache_maxsize)
metrics.BUILD_INFO.info({"version": __version__, "model": settings.model})

SIGNATURE_HEADER = "X-Alert-Explainer-Signature"


def _expected_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _bearer_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented, token)


def _signature_ok(request: Request, secret: str, body: bytes) -> bool:
    provided = request.headers.get(SIGNATURE_HEADER, "")
    return hmac.compare_digest(provided, _expected_signature(secret, body))


def _authenticate(request: Request, body: bytes) -> None:
    """Reject requests that present neither a valid bearer token nor a valid signature.

    Every accepted alert costs a paid LLM call, so an open endpoint is a
    cost-amplification DoS, not just an integrity problem. Either mechanism is
    sufficient; see config for why both exist.
    """
    token = settings.webhook_bearer_token
    secret = settings.webhook_hmac_secret
    if not token and not secret:
        return  # Verification disabled; startup logs a warning.

    if token and _bearer_ok(request, token):
        return
    if secret and _signature_ok(request, secret, body):
        return

    log.warning(
        "webhook_auth_rejected",
        client=request.client.host if request.client else None,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid webhook credentials.",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    if not settings.webhook_bearer_token and not settings.webhook_hmac_secret:
        log.warning(
            "webhook_authentication_disabled",
            detail=(
                "Neither ALERT_EXPLAINER_WEBHOOK_BEARER_TOKEN nor "
                "ALERT_EXPLAINER_WEBHOOK_HMAC_SECRET is set — /webhook accepts "
                "unauthenticated requests and each one bills an LLM call. "
                "Set one before exposing this service."
            ),
        )
    sink.build_sinks(settings)  # fail fast on a misconfigured sink, not on the first alert
    await queue.start()
    yield
    await queue.stop()


app = FastAPI(title="alert-explainer", version=__version__, lifespan=lifespan)


@app.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_alertmanager(request: Request) -> dict[str, Any]:
    """Alertmanager webhook entry point. Buffers alerts onto the priority queue and returns
    immediately so Alertmanager's own retries do not stack up behind LLM latency.

    Signature-verified and size-bounded before anything is parsed or enqueued.
    """
    content_type = request.headers.get("content-type", "application/json").split(";")[0].strip()
    if content_type not in ("application/json", ""):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Expected application/json (the Alertmanager webhook format).",
        )

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > settings.max_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Body exceeds {settings.max_body_bytes} bytes.",
        )

    # Read incrementally: a chunked request has no Content-Length, and
    # `await request.body()` would buffer all of it before any size check.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Body exceeds {settings.max_body_bytes} bytes.",
            )
    body = bytes(body)

    _authenticate(request, body)

    try:
        payload = AlertmanagerPayload.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=e.errors(include_url=False, include_input=False),
        ) from e

    if len(payload.alerts) > settings.max_alerts_per_request:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{len(payload.alerts)} alerts in one request exceeds the "
                f"{settings.max_alerts_per_request} limit."
            ),
        )

    accepted = dropped = duplicates = 0
    for alert in payload.alerts:
        if not recent_alerts.add_if_absent(
            f"{alert.dedup_key}:{alert.status}:{alert.startsAt}", True
        ):
            duplicates += 1
            metrics.ALERTS_DROPPED.labels("duplicate").inc()
            continue
        metrics.ALERTS_RECEIVED.labels(alert.status).inc()
        if await queue.enqueue(alert):
            accepted += 1
        else:
            dropped += 1
    log.info(
        "webhook_received",
        accepted=accepted,
        dropped=dropped,
        duplicates=duplicates,
        receiver=payload.receiver,
        depth=queue.depth,
    )
    return {
        "accepted": accepted,
        "dropped": dropped,
        "duplicates": duplicates,
        "depth": queue.depth,
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — process is up. Always 200 unless the event loop is wedged."""
    return {"status": "ok"}


def _readiness() -> dict[str, Any]:
    queue_full = queue.depth >= int(settings.queue_maxsize * 0.95)
    breaker_open = queue.breaker_open
    return {
        "ready": not (queue_full or breaker_open),
        "queue_depth": queue.depth,
        "queue_maxsize": settings.queue_maxsize,
        "breaker_open": breaker_open,
    }


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness — true when we are willing to accept traffic. Goes 503 when the queue is
    saturated or the LLM circuit breaker is open, so Alertmanager / load balancers can shed."""
    state = _readiness()
    if not state["ready"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return state


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus text exposition: queue, breaker, enrichment outcomes, latency, tokens,
    spend, deliveries and dedup drops. See metrics.py for the full list."""
    return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/status")
async def status_snapshot() -> dict[str, Any]:
    """Human-readable JSON snapshot (the pre-0.2 /metrics payload)."""
    return {
        **_readiness(),
        "version": __version__,
        "model": settings.model,
        "effort": settings.effort,
        "sinks": [s.name for s in sink.build_sinks(settings)],
        "dedup_entries": len(recent_alerts),
        "slo_p50_latency_ms": settings.slo_p50_latency_ms,
        "slo_cost_per_alert_usd": settings.slo_cost_per_alert_usd,
    }
