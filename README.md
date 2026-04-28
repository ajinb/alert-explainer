# alert-explainer

> Drop-in service that sits between Alertmanager and your on-call routing. Every alert that hits it
> gets back a plain-English summary, ranked likely causes, a triage checklist, and a false-positive
> check — generated in 1–4 seconds for under a cent per alert. The on-call engineer reads a brief,
> not a metric label.

[![CI](https://github.com/ajinb/alert-explainer/actions/workflows/ci.yml/badge.svg)](https://github.com/ajinb/alert-explainer/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

## What it does

Alertmanager fires an alert. alert-explainer accepts the v4 webhook payload, ranks alerts by
severity, calls Claude to produce a structured triage brief, and POSTs the enriched alert
downstream (Slack, PagerDuty, your relay). When the LLM is unavailable or rate-limited, it
short-circuits and forwards the original alert un-enriched — the on-call experience degrades, it
does not break.

```
            ┌─────────────────┐
 alerts ───▶│  /webhook       │  Alertmanager v4 payload
            └─────────┬───────┘
                      ▼
            ┌──────────────────┐  Priority Queue (critical → warning → info)
            │ asyncio queue    │  Queue-Based Load Leveling (bounded, sheds when full)
            └─────────┬────────┘
                      ▼
            ┌──────────────────┐  Circuit Breaker around the LLM call
            │ enrich (Claude)  │  Prompt caching on the system prompt
            └─────────┬────────┘
                      ▼
            ┌──────────────────┐
            │  downstream      │  Slack webhook | custom relay | stdout (dev)
            └──────────────────┘
```

## Reliability patterns

This service is the production reference for four [Azure Well-Architected reliability
patterns](https://learn.microsoft.com/en-us/azure/well-architected/reliability/), all enforced in
~500 lines of Python:

| Pattern | Where |
|---|---|
| **Circuit Breaker** | `breaker.py` — wraps the LLM call; opens after N consecutive failures, auto-resets after a cool-down |
| **Priority Queue** | `queue.py` — `critical` alerts dequeue before `warning` before `info` |
| **Queue-Based Load Leveling** | `queue.py` — bounded `asyncio.PriorityQueue`; webhook returns 202 immediately so Alertmanager retries do not stack |
| **Health Endpoint Monitoring** | `app.py` — `/readyz` returns 503 when the queue is saturated or the breaker is open |

The simplicity stance: this is the simplest implementation that holds at single-instance scale.
Graduate to Redis Streams when you need cross-replica fairness or persistence. Graduate to
`prometheus_client` when you wire `/metrics` into Prometheus for real. Both are two-way doors —
swap them in when the load demands it, not before.

## Quickstart (5 minutes)

```bash
git clone https://github.com/ajinb/alert-explainer.git
cd alert-explainer
python -m venv .venv && source .venv/bin/activate
pip install -e .

export ALERT_EXPLAINER_ANTHROPIC_API_KEY=sk-ant-...
python -m alert_explainer &                      # starts on :8080

curl -X POST http://localhost:8080/webhook \
  -H 'content-type: application/json' \
  -d @examples/sample_alert.json
```

The enriched alert prints to stdout (set `ALERT_EXPLAINER_DOWNSTREAM_WEBHOOK_URL` to forward to
Slack instead).

### Docker

```bash
docker compose up --build
```

### Wiring into Alertmanager

Add `examples/alertmanager-snippet.yml` to your `alertmanager.yml`. The receiver block points at
`http://alert-explainer.<namespace>.svc.cluster.local:8080/webhook`.

## Configuration

All settings are environment variables under the `ALERT_EXPLAINER_` prefix.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | Required. Your Anthropic API key. |
| `MODEL` | `claude-sonnet-4-6` | Anthropic model. Use `claude-haiku-4-5` for cheaper, `claude-opus-4-7` for higher quality. |
| `MAX_TOKENS` | `1024` | Cap on enrichment response length. |
| `DOWNSTREAM_WEBHOOK_URL` | *(empty)* | Where enriched alerts go. Slack incoming webhook URL works out of the box. Empty = print to stdout. |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive LLM failures before the breaker opens. |
| `BREAKER_RESET_SECONDS` | `30` | How long the breaker stays open before retry-eligible. |
| `ENRICH_TIMEOUT_SECONDS` | `8` | Per-call timeout on the LLM. |
| `QUEUE_MAXSIZE` | `1000` | Bounded queue. When full, alerts are dropped (see logs) and `/readyz` flips to 503. |
| `WORKER_CONCURRENCY` | `4` | How many parallel LLM calls in flight. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Endpoints

- `POST /webhook` — Alertmanager v4 payload. Returns `202 {"accepted": N, "dropped": N, "depth": N}`.
- `GET /healthz` — liveness. Always 200 unless the event loop is wedged.
- `GET /readyz` — readiness. 200 when accepting traffic, 503 when queue ≥ 95% full or breaker is open.
- `GET /metrics` — JSON snapshot of queue depth, breaker state, version, model, SLO targets.

## SLO targets (defaults)

| Metric | Target |
|---|---|
| P50 enrichment latency | < 4 s |
| Cost per enriched alert | < $0.01 (Sonnet, with prompt caching) |
| Service availability (passthrough on LLM outage) | ≥ 99.5% |

These are sane defaults for a small/mid-size deployment. Override in `config.py` if your
alert volume or budget shape demands it.

## Companion content

- **Blog post**: *Alert fatigue? Let AI triage* — on cloudandsre.com
- **Book chapter**: Ch. 5 of *Self-Healing Infrastructure* — covers the SLO derivation, the
  circuit-breaker design choices, and the cost analysis behind Sonnet vs. Opus for this surface.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Negative tests welcome.

## License

[Apache-2.0](LICENSE).
