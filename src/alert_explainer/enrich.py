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

from .config import settings
from .models import Alert, Enrichment

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


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


async def enrich_alert(alert: Alert, *, client: AsyncAnthropic | None = None) -> Enrichment:
    """Single LLM call. Caller should wrap in CircuitBreaker.call(...) for fault tolerance."""
    client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)
    started = time.monotonic()

    try:
        msg = await asyncio.wait_for(
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
                messages=[
                    {
                        "role": "user",
                        "content": f"Explain this Prometheus alert and give me triage steps:\n\n{_format_alert(alert)}",
                    }
                ],
            ),
            timeout=settings.enrich_timeout_seconds,
        )
    except (TimeoutError, APIError, APITimeoutError) as e:
        raise RuntimeError(f"LLM call failed: {type(e).__name__}: {e}") from e

    raw = msg.content[0].text if msg.content else ""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM returned non-JSON output: {raw[:200]!r}") from e

    cost, cache_hit = _estimate_cost(msg.usage)
    latency_ms = int((time.monotonic() - started) * 1000)

    return Enrichment(
        summary=data["summary"],
        likely_causes=list(data.get("likely_causes", [])),
        triage_checklist=list(data.get("triage_checklist", [])),
        false_positive_check=data.get("false_positive_check", ""),
        confidence=data.get("confidence", "medium"),
        cost_usd=round(cost, 6),
        latency_ms=latency_ms,
        model=settings.model,
        cache_hit=cache_hit,
    )
