"""FastAPI app — Alertmanager webhook receiver + health endpoints."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import ValidationError

from . import __version__, sink
from .config import settings
from .models import AlertmanagerPayload
from .queue import AlertWorkQueue

logging.basicConfig(level=settings.log_level)
log = structlog.get_logger()

queue = AlertWorkQueue(sink=sink.send)

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
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > settings.max_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Body exceeds {settings.max_body_bytes} bytes.",
        )

    body = await request.body()
    if len(body) > settings.max_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Body exceeds {settings.max_body_bytes} bytes.",
        )

    _authenticate(request, body)

    try:
        payload = AlertmanagerPayload.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()
        ) from e

    if len(payload.alerts) > settings.max_alerts_per_request:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{len(payload.alerts)} alerts in one request exceeds the "
                f"{settings.max_alerts_per_request} limit."
            ),
        )

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
