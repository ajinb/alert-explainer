"""v0.2 behaviour: dedup, enrichment cache, request bounds, sinks, and the real SDK request shape."""

from __future__ import annotations

import json

import anthropic
import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from alert_explainer import app as app_module
from alert_explainer import enrich as enrich_module
from alert_explainer import queue as queue_module
from alert_explainer import sink as sink_module
from alert_explainer.app import app
from alert_explainer.config import Settings, settings
from alert_explainer.dedup import TTLCache
from alert_explainer.enrich import EnrichmentRefused, enrich_alert
from alert_explainer.models import Alert, EnrichedAlert, Enrichment
from alert_explainer.queue import AlertWorkQueue

ENRICHMENT = Enrichment(
    summary="5xx spike on checkout",
    likely_causes=["bad deploy"],
    triage_checklist=["check the rollout"],
    false_positive_check="confirm the scrape target is up",
    confidence="high",
    model="claude-sonnet-5-5",
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    async def fake_enrich(alert: Alert) -> Enrichment:
        return ENRICHMENT

    async def fake_sink(_: EnrichedAlert) -> None:
        pass

    monkeypatch.setattr(queue_module, "enrich_alert", fake_enrich)
    app_module.queue._sink = fake_sink  # type: ignore[attr-defined]
    with TestClient(app) as c:
        yield c


def _payload(**alert) -> dict:
    base = {
        "status": "firing",
        "labels": {"alertname": "A", "severity": "critical"},
        "startsAt": "2026-11-01T10:00:00Z",
        "fingerprint": "abc123",
    }
    return {"version": "4", "status": "firing", "alerts": [{**base, **alert}]}


# --- dedup (#4) ---------------------------------------------------------------


def test_alertmanager_retry_is_deduplicated(client: TestClient) -> None:
    first = client.post("/webhook", json=_payload()).json()
    again = client.post("/webhook", json=_payload()).json()
    assert (first["accepted"], first["duplicates"]) == (1, 0)
    assert (again["accepted"], again["duplicates"]) == (0, 1)


def test_resolution_of_same_fingerprint_is_not_a_duplicate(client: TestClient) -> None:
    client.post("/webhook", json=_payload())
    resolved = client.post("/webhook", json=_payload(status="resolved")).json()
    assert resolved["accepted"] == 1


def test_missing_fingerprint_falls_back_to_label_hash() -> None:
    a = Alert(labels={"alertname": "A", "pod": "x"})
    b = Alert(labels={"pod": "x", "alertname": "A"})
    assert a.dedup_key == b.dedup_key
    assert a.dedup_key != Alert(labels={"alertname": "A", "pod": "y"}).dedup_key


def test_ttl_cache_expires_and_evicts() -> None:
    now = [0.0]
    cache: TTLCache[int] = TTLCache(10, maxsize=2, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts "a"
    assert cache.get("a") is None and cache.get("c") == 3
    now[0] = 11
    assert cache.get("b") is None


# --- enrichment cache (#6) ----------------------------------------------------


async def test_refire_reuses_cached_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def counting_enrich(alert: Alert) -> Enrichment:
        calls["n"] += 1
        return ENRICHMENT.model_copy(update={"cost_usd": 0.004})

    monkeypatch.setattr(queue_module, "enrich_alert", counting_enrich)

    async def noop(_):
        pass

    q = AlertWorkQueue(sink=noop)
    alert = Alert(labels={"alertname": "A"}, fingerprint="f1")
    first = await q._enrich_one(alert)
    second = await q._enrich_one(alert)
    assert calls["n"] == 1
    assert first.enrichment.cost_usd == 0.004
    assert second.enrichment.cache_hit is True and second.enrichment.cost_usd == 0.0


# --- request bounds and validation (#3) ---------------------------------------


def test_chunked_body_over_limit_is_rejected_without_content_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 1000)

    def chunks():
        for _ in range(50):
            yield b" " * 100

    r = client.post("/webhook", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b"not json", "application/json", 422),
        (b"[]", "application/json", 422),
        (b'{"alerts": {"labels": {}}}', "application/json", 422),
        (b'{"alerts": [{"labels": {"severity": 3}}]}', "application/json", 422),
        (b'{"alerts": [{"status": "exploded"}]}', "application/json", 422),
        (b'{"status": "maybe", "alerts": []}', "application/json", 422),
        (b"alerts=1", "application/x-www-form-urlencoded", 415),
    ],
)
def test_malformed_webhooks_are_rejected(client, body, content_type, expected) -> None:
    r = client.post("/webhook", content=body, headers={"content-type": content_type})
    assert r.status_code == expected


def test_validation_errors_do_not_echo_the_input(client: TestClient) -> None:
    r = client.post(
        "/webhook",
        content=b'{"alerts": [{"labels": {"k": 1}}]}',
        headers={"content-type": "application/json"},
    )
    assert "input" not in r.json()["detail"][0]


# --- sinks (#1, #10) ----------------------------------------------------------


def _enriched(status: str = "firing") -> EnrichedAlert:
    return EnrichedAlert(
        alert=Alert(
            status=status,
            labels={"alertname": "HighErrorRate", "severity": "critical", "job": "checkout"},
            fingerprint="fp-1",
            generatorURL="http://prom/graph",
        ),
        enrichment=ENRICHMENT,
    )


class Recorder:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(202)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def _no_sleep(_):
    return None


async def test_pagerduty_trigger_and_resolve_share_a_dedup_key() -> None:
    rec = Recorder([httpx.Response(202), httpx.Response(202)])
    pd = sink_module.PagerDutySink("pagerduty", routing_key="R123")
    async with rec.client() as c:
        await pd.deliver(_enriched("firing"), c)
        await pd.deliver(_enriched("resolved"), c)
    trigger, resolve = (json.loads(r.content) for r in rec.requests)
    assert trigger["event_action"] == "trigger" and resolve["event_action"] == "resolve"
    assert trigger["dedup_key"] == resolve["dedup_key"] == "fp-1"
    assert trigger["payload"]["severity"] == "critical"
    assert trigger["payload"]["source"] == "checkout"
    assert "payload" not in resolve


async def test_opsgenie_create_and_close_by_alias() -> None:
    rec = Recorder([httpx.Response(202), httpx.Response(202)])
    og = sink_module.OpsgenieSink("opsgenie", api_key="K")
    async with rec.client() as c:
        await og.deliver(_enriched("firing"), c)
        await og.deliver(_enriched("resolved"), c)
    create, close = rec.requests
    assert create.headers["authorization"] == "GenieKey K"
    body = json.loads(create.content)
    assert body["alias"] == "fp-1" and body["priority"] == "P1" and len(body["message"]) <= 130
    assert close.url.path.endswith("/fp-1/close")
    assert close.url.params["identifierType"] == "alias"


async def test_teams_payload_is_an_adaptive_card() -> None:
    rec = Recorder([httpx.Response(200)])
    async with rec.client() as c:
        await sink_module.TeamsSink("teams", url="https://example/teams").deliver(_enriched(), c)
    card = json.loads(rec.requests[0].content)["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"


async def test_transient_failures_retry_then_succeed() -> None:
    rec = Recorder([httpx.Response(503), httpx.Response(429), httpx.Response(200)])
    async with rec.client() as c:
        ok = await sink_module.deliver_with_retry(
            sink_module.SlackSink("slack", url="https://example/slack"),
            _enriched(),
            c,
            max_attempts=3,
            sleep=_no_sleep,
        )
    assert ok and len(rec.requests) == 3


async def test_permanent_failure_is_not_retried_and_goes_to_dead_letter(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dlq = tmp_path / "dlq.jsonl"
    monkeypatch.setattr(settings, "dead_letter_path", str(dlq))
    rec = Recorder([httpx.Response(400, text="bad routing key")])
    async with rec.client() as c:
        ok = await sink_module.deliver_with_retry(
            sink_module.PagerDutySink("pagerduty", routing_key="bad"),
            _enriched(),
            c,
            max_attempts=3,
            sleep=_no_sleep,
        )
    assert not ok and len(rec.requests) == 1
    record = json.loads(dlq.read_text().splitlines()[0])
    assert record["sink"] == "pagerduty" and "400" in record["error"]


def test_build_sinks_keeps_v01_default_and_rejects_half_configured() -> None:
    assert [s.name for s in sink_module.build_sinks(Settings(sinks=""))] == ["stdout"]
    legacy = sink_module.build_sinks(Settings(downstream_webhook_url="https://hooks/x"))
    assert [s.name for s in legacy] == ["slack"]
    with pytest.raises(ValueError, match="pagerduty"):
        sink_module.build_sinks(Settings(sinks="pagerduty"))
    with pytest.raises(ValueError, match="unknown sink"):
        sink_module.build_sinks(Settings(sinks="carrier-pigeon"))


# --- the real SDK request shape -----------------------------------------------


def _api_reply(payload: dict, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": json.dumps(payload)}] if payload else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 300, "output_tokens": 120, "cache_read_input_tokens": 0},
    }


def _sdk_client(replies: list[dict], seen: list[httpx2.Request]) -> anthropic.AsyncAnthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=replies.pop(0))

    return anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def test_request_uses_structured_outputs_effort_and_fallback() -> None:
    seen: list[httpx2.Request] = []
    brief = {
        k: getattr(ENRICHMENT, k)
        for k in (
            "summary",
            "likely_causes",
            "triage_checklist",
            "false_positive_check",
            "confidence",
        )
    }
    result = await enrich_alert(
        Alert(labels={"alertname": "A", "severity": "critical"}),
        client=_sdk_client([_api_reply(brief)], seen),
    )
    assert result.summary == ENRICHMENT.summary and result.model == "claude-sonnet-5-5"
    body = json.loads(seen[0].content)
    assert "temperature" not in body
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]


async def test_refusal_raises_without_a_corrective_retry() -> None:
    seen: list[httpx2.Request] = []
    with pytest.raises(EnrichmentRefused):
        await enrich_alert(
            Alert(labels={"alertname": "A"}),
            client=_sdk_client([_api_reply({}, stop_reason="refusal")], seen),
        )
    assert len(seen) == 1


async def test_team_context_file_is_appended_inside_the_cached_block(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = tmp_path / "team.md"
    ctx.write_text("Checkout runs in namespace shop; dashboard 'Checkout RED'.")
    monkeypatch.setattr(settings, "system_context_file", str(ctx))
    enrich_module._system_text.cache_clear()
    text = enrich_module._system_text(settings.system_context_file)
    assert "<team_context>" in text and "Checkout RED" in text
    enrich_module._system_text.cache_clear()
