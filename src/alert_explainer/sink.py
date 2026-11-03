"""Where the enriched alert goes after enrichment.

Backends (``ALERT_EXPLAINER_SINKS``, comma-separated; fan-out runs concurrently):

* ``slack``     — Slack incoming webhook, formatted message
* ``teams``     — Microsoft Teams Workflows webhook, Adaptive Card
* ``pagerduty`` — PagerDuty Events API v2 (trigger / resolve, deduplicated on the alert fingerprint)
* ``opsgenie``  — Opsgenie Alert API (create / close by alias)
* ``webhook``   — the raw ``EnrichedAlert`` JSON to any URL you own
* ``stdout``    — print it (local dev, tests)

Delivery is at-least-once within the process: connect errors, 429 and 5xx
retry with exponential backoff (honouring ``Retry-After``); 4xx other than 429
does not. Anything still undelivered is counted and, if ``dead_letter_path`` is
set, appended there as JSONL so it can be replayed.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from . import metrics
from .config import Settings, settings
from .models import EnrichedAlert

log = structlog.get_logger()

_PD_SEVERITY = {"critical": "critical", "page": "critical", "high": "error", "warning": "warning"}
_OG_PRIORITY = {"critical": "P1", "page": "P1", "high": "P2", "warning": "P3", "info": "P4"}


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _headline(enriched: EnrichedAlert) -> str:
    a = enriched.alert
    if enriched.enrichment is not None:
        return f"[{a.severity.upper()}] {a.alertname}: {enriched.enrichment.summary}"
    return f"[{a.severity.upper()}] {a.alertname}"


def _details(enriched: EnrichedAlert) -> dict[str, Any]:
    e = enriched.enrichment
    out: dict[str, Any] = {"labels": enriched.alert.labels}
    if e is None:
        out["enrichment_error"] = enriched.enrichment_error or "unknown"
    else:
        out.update(
            likely_causes=e.likely_causes,
            triage_checklist=e.triage_checklist,
            false_positive_check=e.false_positive_check,
            confidence=e.confidence,
            model=e.model,
        )
    return out


def _format_for_slack(enriched: EnrichedAlert) -> dict:
    a = enriched.alert
    e = enriched.enrichment
    sev = a.severity.upper()
    header = f":rotating_light: *{sev}* — {a.alertname}"

    if e is None:
        body = (
            f"_AI enrichment unavailable: {enriched.enrichment_error or 'unknown'}_\n"
            f"```{json.dumps(a.labels, indent=2)}```"
        )
    else:
        causes = "\n".join(f"• {c}" for c in e.likely_causes) or "_(none)_"
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(e.triage_checklist))
        body = (
            f"*Summary*\n{e.summary}\n\n"
            f"*Likely causes*\n{causes}\n\n"
            f"*Triage*\n{steps}\n\n"
            f"*False-positive check*\n{e.false_positive_check}\n\n"
            f"_Confidence: {e.confidence} · {e.latency_ms} ms · ${e.cost_usd:.4f} · "
            f"{'cache hit' if e.cache_hit else 'cache miss'}_"
        )
    return {"text": f"{header}\n{body}"}


def _format_for_teams(enriched: EnrichedAlert) -> dict:
    e = enriched.enrichment
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "text": f"{enriched.alert.severity.upper()} — {enriched.alert.alertname}",
        },
    ]
    if e is None:
        body.append(
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "text": f"AI enrichment unavailable: {enriched.enrichment_error}",
            }
        )
    else:
        body += [
            {"type": "TextBlock", "wrap": True, "text": e.summary},
            {"type": "TextBlock", "weight": "Bolder", "text": "Likely causes"},
            {
                "type": "TextBlock",
                "wrap": True,
                "text": "\n".join(f"- {c}" for c in e.likely_causes) or "(none)",
            },
            {"type": "TextBlock", "weight": "Bolder", "text": "Triage"},
            {
                "type": "TextBlock",
                "wrap": True,
                "text": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(e.triage_checklist)),
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "text": f"False-positive check: {e.false_positive_check} · confidence {e.confidence}",
            },
        ]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if enriched.alert.generatorURL:
        card["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": "Open in Prometheus",
                "url": enriched.alert.generatorURL,
            }
        ]
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


@dataclass
class Sink:
    name: str

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        raise NotImplementedError

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        retry_after = response.headers.get("retry-after")
        raise DeliveryError(
            f"HTTP {response.status_code}: {response.text[:200]}",
            retryable=response.status_code == 429 or response.status_code >= 500,
            retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )


@dataclass
class StdoutSink(Sink):
    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        print(json.dumps(enriched.model_dump(), indent=2))


@dataclass
class SlackSink(Sink):
    url: str = ""

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        self._check(await client.post(self.url, json=_format_for_slack(enriched)))


@dataclass
class TeamsSink(Sink):
    url: str = ""

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        self._check(await client.post(self.url, json=_format_for_teams(enriched)))


@dataclass
class WebhookSink(Sink):
    url: str = ""

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        self._check(await client.post(self.url, json=enriched.model_dump()))


@dataclass
class PagerDutySink(Sink):
    routing_key: str = ""
    url: str = "https://events.pagerduty.com/v2/enqueue"

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        a = enriched.alert
        event: dict[str, Any] = {
            "routing_key": self.routing_key,
            "event_action": "resolve" if a.status == "resolved" else "trigger",
            "dedup_key": a.dedup_key,
        }
        if event["event_action"] == "trigger":
            event["payload"] = {
                "summary": _headline(enriched)[:1024],
                "source": a.labels.get("instance") or a.labels.get("job") or "alertmanager",
                "severity": _PD_SEVERITY.get(a.severity, "info"),
                "component": a.labels.get("service") or a.labels.get("job"),
                "custom_details": _details(enriched),
            }
            if a.generatorURL:
                event["links"] = [{"href": a.generatorURL, "text": "Prometheus"}]
        self._check(await client.post(self.url, json=event))


@dataclass
class OpsgenieSink(Sink):
    api_key: str = ""
    url: str = "https://api.opsgenie.com/v2/alerts"

    async def deliver(self, enriched: EnrichedAlert, client: httpx.AsyncClient) -> None:
        a = enriched.alert
        headers = {"Authorization": f"GenieKey {self.api_key}"}
        if a.status == "resolved":
            r = await client.post(
                f"{self.url}/{a.dedup_key}/close",
                params={"identifierType": "alias"},
                headers=headers,
                json={"source": "alert-explainer"},
            )
            if r.status_code == 404:  # never created (or already closed): nothing to do
                return
            self._check(r)
            return
        e = enriched.enrichment
        body = {
            "message": _headline(enriched)[:130],
            "alias": a.dedup_key,
            "description": (e.summary if e else enriched.enrichment_error or "")[:15000],
            "priority": _OG_PRIORITY.get(a.severity, "P3"),
            "source": "alert-explainer",
            "tags": [f"{k}:{v}"[:50] for k, v in sorted(a.labels.items())][:20],
            "details": {k: str(v)[:8000] for k, v in _details(enriched).items()},
        }
        self._check(await client.post(self.url, headers=headers, json=body))


def build_sinks(cfg: Settings) -> list[Sink]:
    names = [n.strip().lower() for n in cfg.sinks.split(",") if n.strip()]
    if not names:
        names = ["slack"] if cfg.downstream_webhook_url else ["stdout"]
    sinks: list[Sink] = []
    for name in names:
        if name == "stdout":
            sinks.append(StdoutSink(name))
        elif name == "slack":
            sinks.append(SlackSink(name, url=cfg.slack_webhook_url or cfg.downstream_webhook_url))
        elif name == "teams":
            sinks.append(TeamsSink(name, url=cfg.teams_webhook_url))
        elif name == "webhook":
            sinks.append(WebhookSink(name, url=cfg.downstream_webhook_url))
        elif name == "pagerduty":
            sinks.append(PagerDutySink(name, cfg.pagerduty_routing_key, cfg.pagerduty_events_url))
        elif name == "opsgenie":
            sinks.append(OpsgenieSink(name, cfg.opsgenie_api_key, cfg.opsgenie_api_url))
        else:
            raise ValueError(f"unknown sink {name!r}")
    missing = [
        s.name
        for s in sinks
        if (isinstance(s, SlackSink | TeamsSink | WebhookSink) and not s.url)
        or (isinstance(s, PagerDutySink) and not s.routing_key)
        or (isinstance(s, OpsgenieSink) and not s.api_key)
    ]
    if missing:
        raise ValueError(f"sink(s) {missing} are enabled but not configured")
    return sinks


def _dead_letter(enriched: EnrichedAlert, sink: str, error: str, path: str) -> None:
    if not path:
        return
    record = {
        "at": datetime.now(UTC).isoformat(),
        "sink": sink,
        "error": error,
        "enriched": enriched.model_dump(),
    }
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


async def deliver_with_retry(
    sink: Sink,
    enriched: EnrichedAlert,
    client: httpx.AsyncClient,
    *,
    max_attempts: int,
    base_delay: float = 0.5,
    sleep=asyncio.sleep,
) -> bool:
    """Deliver to one sink. Returns True on success; never raises."""
    error = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            await sink.deliver(enriched, client)
            metrics.DELIVERIES.labels(sink.name, "ok").inc()
            return True
        except DeliveryError as e:
            error, retryable, wait = str(e), e.retryable, e.retry_after
        except httpx.TransportError as e:
            error, retryable, wait = f"{type(e).__name__}: {e}", True, None
        if not retryable or attempt == max_attempts:
            break
        metrics.DELIVERY_RETRIES.labels(sink.name).inc()
        backoff = base_delay * 2 ** (attempt - 1)
        await sleep(min(wait if wait is not None else backoff * (1 + random.random()), 30.0))

    metrics.DELIVERIES.labels(sink.name, "failed").inc()
    log.error(
        "downstream_delivery_failed",
        sink=sink.name,
        error=error,
        alertname=enriched.alert.alertname,
    )
    _dead_letter(enriched, sink.name, error, settings.dead_letter_path)
    return False


_sinks: list[Sink] | None = None


async def send(enriched: EnrichedAlert) -> None:
    """Fan the enriched alert out to every configured sink."""
    global _sinks
    if _sinks is None:
        _sinks = build_sinks(settings)
    async with httpx.AsyncClient(timeout=settings.delivery_timeout_seconds) as client:
        await asyncio.gather(
            *(
                deliver_with_retry(s, enriched, client, max_attempts=settings.delivery_max_attempts)
                for s in _sinks
            )
        )
