"""LLM enrichment of a single Prometheus alert.

Uses Anthropic prompt caching on the system prompt so the per-alert input cost is just the alert
JSON plus the response. The system prompt is the static, expensive part of the context window;
caching it cuts the input bill on repeat calls by ~90%.
"""

from __future__ import annotations

import asyncio
import json
import time

from anthropic import APIError, APITimeoutError, AsyncAnthropic
from pydantic import ValidationError

from .config import settings
from .models import Alert, Enrichment


class EnrichmentSchemaError(RuntimeError):
    """The model answered, but not in the shape we need.

    Distinct from a transport or availability failure: the LLM is up and
    responding. Kept separate so a formatting problem cannot trip the circuit
    breaker and degrade enrichment for every subsequent alert.
    """


SYSTEM_PROMPT = """You are an expert SRE on-call assistant. When given a Prometheus alert payload, you produce a concise, structured triage brief that helps the on-call engineer act faster.

Always respond as valid JSON matching this schema:
{
  "summary": "1-2 sentence plain-English description of what fired and why it matters.",
  "likely_causes": ["cause 1", "cause 2", "cause 3"],
  "triage_checklist": ["step 1", "step 2", "step 3", "step 4"],
  "false_positive_check": "1-sentence test the engineer can run to rule out a false positive.",
  "confidence": "high" | "medium" | "low"
}

Rules:
- Be concrete. Reference the labels and annotations actually present in the alert.
- Prefer specific commands and dashboard names over vague advice.
- If the alert is sparse (no annotations, generic name), say so via lower confidence.
- Never fabricate runbook URLs, service names, or dashboards that are not in the input.
- Output JSON only — no markdown, no prose around it."""

# Anthropic pricing (Sonnet-4-6 / 2026 list): $3/MTok input, $15/MTok output.
# Cache reads are ~1/10 input cost on hit.
_PRICE_INPUT_PER_TOK = 3.0 / 1_000_000
_PRICE_CACHE_READ_PER_TOK = 0.30 / 1_000_000
_PRICE_OUTPUT_PER_TOK = 15.0 / 1_000_000


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


def _estimate_cost(usage: object) -> tuple[float, bool]:
    """Return (cost_usd, cache_hit) from an Anthropic usage block."""
    in_tok = getattr(usage, "input_tokens", 0)
    out_tok = getattr(usage, "output_tokens", 0)
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_create = getattr(usage, "cache_creation_input_tokens", 0)
    cost = (
        in_tok * _PRICE_INPUT_PER_TOK
        + cache_create * _PRICE_INPUT_PER_TOK * 1.25  # cache writes cost ~25% more
        + cache_read * _PRICE_CACHE_READ_PER_TOK
        + out_tok * _PRICE_OUTPUT_PER_TOK
    )
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

    try:
        return await asyncio.wait_for(
            client.messages.create(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
            ),
            timeout=settings.enrich_timeout_seconds,
        )
    except (TimeoutError, APIError, APITimeoutError) as e:
        raise RuntimeError(f"LLM call failed: {type(e).__name__}: {e}") from e


async def enrich_alert(alert: Alert, *, client: AsyncAnthropic | None = None) -> Enrichment:
    """Single LLM call, with one corrective retry if the reply is malformed.

    Caller should wrap in CircuitBreaker.call(...) for fault tolerance. Note the
    breaker is configured to ignore EnrichmentSchemaError — see queue.py.
    """
    client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)
    started = time.monotonic()

    msg = await _call_model(client, alert)

    cost, cache_hit = _estimate_cost(msg.usage)
    raw = _first_text(msg)

    try:
        return _parse_enrichment(raw, cost, cache_hit, started)
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
        retry_cost, retry_cache_hit = _estimate_cost(retry_msg.usage)
        return _parse_enrichment(
            _first_text(retry_msg), cost + retry_cost, retry_cache_hit, started
        )


def _parse_enrichment(raw: str, cost: float, cache_hit: bool, started: float) -> Enrichment:
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
            model=settings.model,
            cache_hit=cache_hit,
        )
    except ValidationError as e:
        raise EnrichmentSchemaError(f"payload failed validation: {e}") from e
