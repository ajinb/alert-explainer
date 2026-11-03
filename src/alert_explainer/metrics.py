"""Prometheus metrics, on a private registry so tests and embedders never collide.

Scrape ``/metrics``. The JSON snapshot that used to live there moved to ``/status``.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
)

REGISTRY = CollectorRegistry(auto_describe=True)

ALERTS_RECEIVED = Counter(
    "alert_explainer_alerts_received",
    "Alerts accepted from Alertmanager, by alert status.",
    ["status"],
    registry=REGISTRY,
)
ALERTS_DROPPED = Counter(
    "alert_explainer_alerts_dropped",
    "Alerts not enqueued: queue_full or duplicate (inside the dedup window).",
    ["reason"],
    registry=REGISTRY,
)
ENRICHMENTS = Counter(
    "alert_explainer_enrichments",
    "Enrichment outcomes: ok, cached, schema_error, refused, llm_error, circuit_open.",
    ["outcome"],
    registry=REGISTRY,
)
ENRICHMENT_LATENCY = Histogram(
    "alert_explainer_enrichment_latency_seconds",
    "Wall time to enrich one alert (LLM call plus any corrective retry).",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
    registry=REGISTRY,
)
LLM_COST = Counter(
    "alert_explainer_llm_cost_usd",
    "Estimated LLM spend in USD (list prices, see cloudandsre.cost).",
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "alert_explainer_llm_tokens",
    "LLM tokens by type: input (uncached), cache_read, cache_write, output.",
    ["type"],
    registry=REGISTRY,
)
DELIVERIES = Counter(
    "alert_explainer_deliveries",
    "Downstream deliveries by sink and outcome: ok, failed.",
    ["sink", "outcome"],
    registry=REGISTRY,
)
DELIVERY_RETRIES = Counter(
    "alert_explainer_delivery_retries",
    "Delivery attempts beyond the first, by sink.",
    ["sink"],
    registry=REGISTRY,
)
QUEUE_DEPTH = Gauge(
    "alert_explainer_queue_depth", "Alerts waiting for a worker.", registry=REGISTRY
)
BREAKER_OPEN = Gauge(
    "alert_explainer_breaker_open", "1 while the LLM circuit breaker is open.", registry=REGISTRY
)
BUILD_INFO = Info("alert_explainer_build", "Version and model.", registry=REGISTRY)


def record_usage(usage: object, cost_usd: float) -> None:
    """Count tokens and spend from an Anthropic ``usage`` block."""
    for kind, attr in (
        ("input", "input_tokens"),
        ("cache_read", "cache_read_input_tokens"),
        ("cache_write", "cache_creation_input_tokens"),
        ("output", "output_tokens"),
    ):
        value = int(getattr(usage, attr, 0) or 0)
        if value:
            LLM_TOKENS.labels(kind).inc(value)
    if cost_usd:
        LLM_COST.inc(cost_usd)
