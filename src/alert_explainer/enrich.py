"""LLM enrichment of a single Prometheus alert.

* **Structured outputs** — the reply is constrained to ``TRIAGE_BRIEF_SCHEMA`` by
  the API, so malformed JSON is now the exception the corrective retry handles,
  not the common case.
* **Prompt caching** — the system prompt (plus any ``system_context_file``) is
  marked cacheable. Caching only engages once that prefix is longer than the
  model's minimum cacheable length; the stock prompt alone is shorter, so the
  saving arrives when you add team context, which is the part worth caching.
* **Refusal fallback** — on models that support it, ``fallbacks: "default"``
  re-runs a declined request on Anthropic's recommended fallback model. A
  refusal that survives is an :class:`EnrichmentRefused`, which neither retries
  nor trips the breaker.
* **Tracing** — when ``opentelemetry-api`` is installed, each call is a CLIENT
  span carrying the GenAI semantic-convention attributes.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from anthropic import APIError, APITimeoutError, AsyncAnthropic
from cloudandsre.cost import cost_for_message
from cloudandsre.genai import genai_attributes, genai_span_name
from pydantic import ValidationError

from . import metrics
from .config import settings
from .models import TRIAGE_BRIEF_SCHEMA, Alert, Enrichment

try:  # optional: tracing lights up when the OpenTelemetry API is installed
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer("alert_explainer")
except ImportError:  # pragma: no cover - exercised only without the extra
    _tracer = None

# Models that accept ``fallbacks: "default"``; anything else would 400 on it.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class EnrichmentSchemaError(RuntimeError):
    """The model answered, but not in the shape we need.

    Distinct from a transport or availability failure: the LLM is up and
    responding. Kept separate so a formatting problem cannot trip the circuit
    breaker and degrade enrichment for every subsequent alert.
    """


class EnrichmentRefused(EnrichmentSchemaError):
    """The model (and any fallback) declined. The service is healthy; do not retry."""


SYSTEM_PROMPT = """You are an expert SRE on-call assistant. When given a Prometheus alert payload, you produce a concise, structured triage brief that helps the on-call engineer act faster.

Fields:
- summary: 1-2 plain-English sentences on what fired and why it matters.
- likely_causes: the 2-4 most plausible causes, most likely first.
- triage_checklist: 3-6 concrete steps, in the order the engineer should run them.
- false_positive_check: one test the engineer can run to rule out a false positive.
- confidence: high, medium or low.

Rules:
- Be concrete. Reference the labels and annotations actually present in the alert.
- Prefer specific commands and dashboard names over vague advice.
- If the alert is sparse (no annotations, generic name), say so via lower confidence.
- Never fabricate runbook URLs, service names, or dashboards that are not in the input
  or the team context below."""


@lru_cache(maxsize=1)
def _system_text(context_file: str) -> str:
    if not context_file:
        return SYSTEM_PROMPT
    context = Path(context_file).read_text(encoding="utf-8").strip()
    return f"{SYSTEM_PROMPT}\n\n<team_context>\n{context}\n</team_context>"


def _format_alert(alert: Alert) -> str:
    payload = {
        "alertname": alert.alertname,
        "severity": alert.severity,
        "status": alert.status,
        "labels": alert.labels,
        "annotations": alert.annotations,
        "startsAt": alert.startsAt,
        "generatorURL": alert.generatorURL,
    }
    return f"```json\n{json.dumps(payload, indent=2)}\n```"


def _estimate_cost(msg: object) -> tuple[float, bool]:
    """Return (cost_usd, cache_hit) for an Anthropic response, and record it.

    Priced on the model that *served* the reply, which differs from the
    configured one when a refusal fallback ran. Pricing lives in
    cloudandsre.cost so the table is maintained in one place.
    """
    usage = getattr(msg, "usage", None)
    model = getattr(msg, "model", None) or settings.model
    breakdown = cost_for_message(SimpleNamespace(model=model, usage=usage))
    cost = float(breakdown["usd"])
    metrics.record_usage(usage, cost)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return cost, cache_read > 0


def _first_text(msg: object) -> str:
    """Return the first text block's content.

    Not content[0] — a thinking or other non-text block can lead the list, and
    indexing blindly raises AttributeError on a response that is otherwise fine.
    """
    for block in getattr(msg, "content", None) or []:
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            return block.text
    return ""


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


async def _call_model(client: AsyncAnthropic, alert: Alert, *, correction: str | None = None):
    """One request to the model. Transport and timeout faults surface as RuntimeError.

    ``correction`` appends a follow-up turn restating the output contract, used
    when the first reply came back in the wrong shape.
    """
    content = f"Explain this Prometheus alert and give me triage steps:\n\n{_format_alert(alert)}"
    messages: list[dict] = [{"role": "user", "content": content}]
    if correction:
        messages.append({"role": "user", "content": correction})

    extra: dict = {}
    if settings.model in _FALLBACK_MODELS:
        extra = {"betas": [_FALLBACK_BETA], "fallbacks": "default"}

    span = (
        _tracer.start_as_current_span(
            genai_span_name("chat", settings.model), kind=_otel_trace.SpanKind.CLIENT
        )
        if _tracer is not None
        else nullcontext()
    )
    with span as active:
        try:
            msg = await asyncio.wait_for(
                client.beta.messages.create(
                    model=settings.model,
                    max_tokens=settings.max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": _system_text(settings.system_context_file),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages,
                    output_config={
                        "effort": settings.effort,
                        "format": {"type": "json_schema", "schema": TRIAGE_BRIEF_SCHEMA},
                    },
                    **extra,
                ),
                timeout=settings.enrich_timeout_seconds,
            )
        except (TimeoutError, APIError, APITimeoutError) as e:
            if active is not None:
                active.set_attribute("error.type", type(e).__name__)
            raise RuntimeError(f"LLM call failed: {type(e).__name__}: {e}") from e
        if active is not None:
            active.set_attributes(
                genai_attributes(
                    msg,
                    request_model=settings.model,
                    reasoning_level=settings.effort,
                    output_type="json",
                    max_tokens=settings.max_tokens,
                    include_cost=True,
                )
            )
    return msg


async def enrich_alert(alert: Alert, *, client: AsyncAnthropic | None = None) -> Enrichment:
    """Single LLM call, with one corrective retry if the reply is malformed.

    Caller should wrap in CircuitBreaker.call(...) for fault tolerance. Note the
    breaker is configured to ignore EnrichmentSchemaError — see queue.py.
    """
    client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)
    started = time.monotonic()

    msg = await _call_model(client, alert)

    cost, cache_hit = _estimate_cost(msg)
    _raise_if_refused(msg)
    raw = _first_text(msg)
    served_by = getattr(msg, "model", None) or settings.model

    try:
        return _parse_enrichment(raw, cost, cache_hit, started, served_by)
    except EnrichmentSchemaError as first_error:
        # One corrective retry. The model is clearly reachable — it just answered
        # in the wrong shape — so it is worth restating the contract once before
        # falling back to a pass-through alert.
        retry_msg = await _call_model(
            client,
            alert,
            correction=(
                f"Your previous reply could not be used: {first_error}. "
                "Reply again with the JSON object described in the system prompt "
                "and nothing else — no prose, no code fence."
            ),
        )
        retry_cost, retry_cache_hit = _estimate_cost(retry_msg)
        _raise_if_refused(retry_msg)
        return _parse_enrichment(
            _first_text(retry_msg),
            cost + retry_cost,
            retry_cache_hit,
            started,
            getattr(retry_msg, "model", None) or served_by,
        )


def _raise_if_refused(msg: object) -> None:
    if getattr(msg, "stop_reason", None) == "refusal":
        category = getattr(getattr(msg, "stop_details", None), "category", None)
        raise EnrichmentRefused(f"model declined (category={category})")


def _parse_enrichment(
    raw: str, cost: float, cache_hit: bool, started: float, served_by: str | None = None
) -> Enrichment:
    """Turn raw model text into an Enrichment, or raise EnrichmentSchemaError."""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise EnrichmentSchemaError(f"non-JSON output: {raw[:200]!r}") from e

    if not isinstance(data, dict):
        raise EnrichmentSchemaError(f"expected a JSON object, got {type(data).__name__}")

    # summary is the one field the whole brief hangs on. Missing or blank means
    # the reply is unusable, so treat it as a schema error and retry rather than
    # emitting an enrichment with an empty headline.
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise EnrichmentSchemaError("missing or empty 'summary'")

    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        return Enrichment(
            summary=summary,
            likely_causes=list(data.get("likely_causes", []) or []),
            triage_checklist=list(data.get("triage_checklist", []) or []),
            false_positive_check=data.get("false_positive_check", ""),
            confidence=data.get("confidence", "medium"),
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            model=served_by or settings.model,
            cache_hit=cache_hit,
        )
    except ValidationError as e:
        raise EnrichmentSchemaError(f"payload failed validation: {e}") from e
