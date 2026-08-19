"""Enrichment parsing: malformed replies, the corrective retry, and breaker isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cloudandsre import AsyncCircuitBreaker

from alert_explainer import enrich as enrich_module
from alert_explainer.enrich import EnrichmentSchemaError, enrich_alert
from alert_explainer.models import Alert

VALID = {
    "summary": "5xx spike on checkout",
    "likely_causes": ["bad deploy"],
    "triage_checklist": ["check the rollout"],
    "false_positive_check": "confirm the scrape target is up",
    "confidence": "high",
}


def _msg(text: str, *, lead_with_thinking: bool = False):
    blocks = []
    if lead_with_thinking:
        blocks.append(SimpleNamespace(type="thinking", thinking="pondering"))
    blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def alert() -> Alert:
    return Alert(labels={"alertname": "HighErrorRate", "severity": "critical"})


def _install(monkeypatch, replies: list):
    """Serve `replies` in order; record how many calls were made."""
    calls = {"n": 0}

    async def fake_call(client, alert, *, correction=None):
        idx = calls["n"]
        calls["n"] += 1
        return replies[min(idx, len(replies) - 1)]

    monkeypatch.setattr(enrich_module, "_call_model", fake_call)
    return calls


async def test_valid_response_parses(alert, monkeypatch):
    _install(monkeypatch, [_msg(json.dumps(VALID))])
    result = await enrich_alert(alert, client=object())
    assert result.summary == "5xx spike on checkout"
    assert result.confidence == "high"


async def test_thinking_block_first_does_not_break_extraction(alert, monkeypatch):
    # content[0] is not a text block here — the old indexing raised AttributeError.
    _install(monkeypatch, [_msg(json.dumps(VALID), lead_with_thinking=True)])
    result = await enrich_alert(alert, client=object())
    assert result.summary == "5xx spike on checkout"


async def test_missing_summary_retries_and_recovers(alert, monkeypatch):
    incomplete = {k: v for k, v in VALID.items() if k != "summary"}
    calls = _install(monkeypatch, [_msg(json.dumps(incomplete)), _msg(json.dumps(VALID))])
    result = await enrich_alert(alert, client=object())
    assert calls["n"] == 2
    assert result.summary == "5xx spike on checkout"


async def test_non_json_retries_and_recovers(alert, monkeypatch):
    calls = _install(monkeypatch, [_msg("I think the service is down."), _msg(json.dumps(VALID))])
    result = await enrich_alert(alert, client=object())
    assert calls["n"] == 2
    assert result.summary == "5xx spike on checkout"


async def test_retry_happens_at_most_once(alert, monkeypatch):
    calls = _install(monkeypatch, [_msg("still not json")])
    with pytest.raises(EnrichmentSchemaError):
        await enrich_alert(alert, client=object())
    assert calls["n"] == 2  # original + one retry, never a third


async def test_json_array_is_a_schema_error(alert, monkeypatch):
    _install(monkeypatch, [_msg("[1, 2, 3]")])
    with pytest.raises(EnrichmentSchemaError):
        await enrich_alert(alert, client=object())


# ---------------------------------------------------------------------------
# The important one: schema faults must not open the breaker
# ---------------------------------------------------------------------------


async def test_schema_errors_never_open_the_breaker():
    breaker: AsyncCircuitBreaker[None] = AsyncCircuitBreaker(
        failure_threshold=2,
        reset_after_seconds=30,
        ignore_exceptions=(EnrichmentSchemaError,),
    )

    async def malformed():
        raise EnrichmentSchemaError("bad shape")

    for _ in range(10):
        with pytest.raises(EnrichmentSchemaError):
            await breaker.call(malformed)

    assert breaker.is_open is False


async def test_real_failures_still_open_the_breaker():
    breaker: AsyncCircuitBreaker[None] = AsyncCircuitBreaker(
        failure_threshold=2,
        reset_after_seconds=30,
        ignore_exceptions=(EnrichmentSchemaError,),
    )

    async def unreachable():
        raise RuntimeError("LLM call failed: APITimeoutError")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(unreachable)

    assert breaker.is_open is True
