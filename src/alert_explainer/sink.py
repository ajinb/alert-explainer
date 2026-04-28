"""Where the enriched alert goes after enrichment.

Default sink POSTs JSON to `settings.downstream_webhook_url`. That covers the three common targets:
- Slack incoming webhook (set the URL, format below renders cleanly in Slack)
- A custom relay your team owns
- Stdout (when no URL is configured — useful for local dev and tests)

PagerDuty Events API v2 is a one-line swap; not in v0.1 to keep the dependency footprint small.
"""

from __future__ import annotations

import json

import httpx
import structlog

from .config import settings
from .models import EnrichedAlert

log = structlog.get_logger()


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


async def send(enriched: EnrichedAlert) -> None:
    if not settings.downstream_webhook_url:
        # Local / no-config mode: print to stdout so operators can see it without setting up Slack.
        print(json.dumps(enriched.model_dump(), indent=2))
        return

    payload = _format_for_slack(enriched)
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(settings.downstream_webhook_url, json=payload)
        if r.status_code >= 400:
            log.error(
                "downstream_post_failed",
                status=r.status_code,
                body=r.text[:200],
                alertname=enriched.alert.alertname,
            )
