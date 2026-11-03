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
 alerts ───▶│  /webhook       │  Alertmanager v4 payload · auth · streamed size cap
            └─────────┬───────┘
                      ▼
            ┌──────────────────┐  Dedup on (fingerprint, status, startsAt):
            │ retry-storm gate │  Alertmanager retries and repeats never bill twice
            └─────────┬────────┘
                      ▼
            ┌──────────────────┐  Priority Queue (critical → warning → info)
            │ asyncio queue    │  Queue-Based Load Leveling (bounded, sheds when full)
            └─────────┬────────┘
                      ▼
            ┌──────────────────┐  Enrichment cache by fingerprint (TTL)
            │ enrich (Claude)  │  Circuit Breaker · structured outputs · refusal fallback
            └─────────┬────────┘  Prompt caching on the system prompt + team context
                      ▼
            ┌──────────────────┐  Fan-out with retry + backoff, dead-letter file
            │  sinks           │  Slack | Teams | PagerDuty | Opsgenie | webhook | stdout
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
| **Idempotent Receiver** | `app.py` + `dedup.py` — identical `(fingerprint, status, startsAt)` inside the window is dropped before it is queued |
| **Cache-Aside** | `queue.py` — a re-fire of an alert explained in the last 30 minutes reuses that brief at $0 |
| **Retry with Backoff + Dead Letter** | `sink.py` — transient delivery failures retry (honouring `Retry-After`); the rest land in a JSONL dead-letter file |

The simplicity stance: this is the simplest implementation that holds at single-instance scale.
Graduate to Redis Streams when you need cross-replica fairness or persistence. Graduate to
`prometheus_client` when you wire `/metrics` into Prometheus for real. Both are two-way doors —
swap them in when the load demands it, not before.

## Quickstart (5 minutes)

```bash
git clone https://github.com/ajinb/alert-explainer.git
cd alert-explainer
python -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[otel]" for OpenTelemetry GenAI spans

export ALERT_EXPLAINER_ANTHROPIC_API_KEY=sk-ant-...
python -m alert_explainer &                      # starts on :8080

curl -X POST http://localhost:8080/webhook \
  -H 'content-type: application/json' \
  -d @examples/sample_alert.json
```

The enriched alert prints to stdout. Set `ALERT_EXPLAINER_SINKS` (and the matching credentials)
to forward to Slack, Teams, PagerDuty or Opsgenie instead — several at once if you like.

### Docker

```bash
docker compose up --build
```

### Kubernetes (Helm)

```bash
kubectl create secret generic alert-explainer \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=WEBHOOK_BEARER_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=PAGERDUTY_ROUTING_KEY=...
helm install alert-explainer charts/alert-explainer \
  --set secret.existingSecret=alert-explainer \
  --set config.SINKS=pagerduty \
  --set serviceMonitor.enabled=true
```

The chart runs as non-root with a read-only root filesystem, no service-account token, and a
config checksum that rolls pods when settings change. One replica: the queue, dedup window and
cache are in-process (see *Scaling* below).

### Wiring into Alertmanager

Add `examples/alertmanager-snippet.yml` to your `alertmanager.yml`. The receiver block points at
`http://alert-explainer.<namespace>.svc.cluster.local:8080/webhook`.

## Configuration

All settings are environment variables under the `ALERT_EXPLAINER_` prefix.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | Required. Your Anthropic API key. |
| `MODEL` | `claude-sonnet-5-5` | Anthropic model. `claude-haiku-5-5` is cheaper; `claude-opus-5-5` reasons harder. |
| `EFFORT` | `low` | Thinking depth (`low` … `max`). Triage is latency-bound; raise it if briefs come back shallow. |
| `MAX_TOKENS` | `4096` | Output cap per call, thinking included. |
| `SYSTEM_CONTEXT_FILE` | *(empty)* | Optional team context (service catalogue, dashboard names, runbook conventions) appended to the cached system prompt. |
| `WEBHOOK_BEARER_TOKEN` | *(empty)* | Shared bearer token for `/webhook`. **Set this** — see Authentication below. |
| `WEBHOOK_HMAC_SECRET` | *(empty)* | Alternative to the bearer token: HMAC-SHA256 over the raw body. |
| `MAX_BODY_BYTES` | `1048576` | Reject request bodies larger than this (413), including chunked bodies with no `Content-Length`. |
| `MAX_ALERTS_PER_REQUEST` | `200` | Reject a single POST carrying more alerts than this (413). |
| `DEDUP_WINDOW_SECONDS` | `300` | Identical `(fingerprint, status, startsAt)` inside this window is dropped as a retry or repeat. |
| `ENRICHMENT_CACHE_TTL_SECONDS` | `1800` | How long a brief is reused for a re-fire of the same alert. `0` disables. |
| `SINKS` | *(empty)* | Comma list of `slack`, `teams`, `pagerduty`, `opsgenie`, `webhook`, `stdout`. Empty = Slack format to `DOWNSTREAM_WEBHOOK_URL` if set, else stdout (v0.1 behaviour). |
| `DOWNSTREAM_WEBHOOK_URL` | *(empty)* | Target for the `slack` (if `SLACK_WEBHOOK_URL` is unset) and `webhook` sinks. |
| `SLACK_WEBHOOK_URL` / `TEAMS_WEBHOOK_URL` | *(empty)* | Slack incoming webhook; Teams Workflows webhook (Adaptive Card). |
| `PAGERDUTY_ROUTING_KEY` | *(empty)* | Events API v2 integration key. Resolved alerts resolve the incident (same `dedup_key`). |
| `OPSGENIE_API_KEY` / `OPSGENIE_API_URL` | *(empty)* / US endpoint | Alert API key; use `https://api.eu.opsgenie.com/v2/alerts` for EU. Resolved alerts close by alias. |
| `DELIVERY_MAX_ATTEMPTS` | `3` | Attempts per sink for connect errors, 429 and 5xx. |
| `DEAD_LETTER_PATH` | *(empty)* | JSONL file for alerts that could not be delivered. |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive LLM failures before the breaker opens. |
| `BREAKER_RESET_SECONDS` | `30` | How long the breaker stays open before retry-eligible. |
| `ENRICH_TIMEOUT_SECONDS` | `20` | Per-call timeout on the LLM. |
| `QUEUE_MAXSIZE` | `1000` | Bounded queue. When full, alerts are dropped (see logs) and `/readyz` flips to 503. |
| `WORKER_CONCURRENCY` | `4` | How many parallel LLM calls in flight. |
| `HOST` | `127.0.0.1` | Bind address. The container image sets `0.0.0.0`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Authentication

`/webhook` triggers a paid LLM call for every alert it accepts, so an open endpoint is a
cost-amplification DoS, not just an integrity problem. Set one of the two credentials
below before exposing the service — with neither set, the endpoint stays open and the
app logs a warning on every startup.

**Bearer token — use this with Alertmanager.** Alertmanager cannot compute request
signatures, so this is the mechanism that works when it posts here directly:

```bash
export ALERT_EXPLAINER_WEBHOOK_BEARER_TOKEN=$(openssl rand -hex 32)
```

```yaml
# alertmanager.yml
receivers:
  - name: alert-explainer
    webhook_configs:
      - url: http://alert-explainer:8080/webhook
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/alert-explainer-token
```

**HMAC signature — use this for senders you control.** Send
`X-Alert-Explainer-Signature: sha256=<hex>`, computed over the exact raw request body:

```bash
export ALERT_EXPLAINER_WEBHOOK_HMAC_SECRET=$(openssl rand -hex 32)
```

```python
signature = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
```

If both are configured, either one validates.

## Endpoints

- `POST /webhook` — Alertmanager v4 payload. Returns `202 {"accepted": N, "dropped": N, "duplicates": N, "depth": N}`.
  `401` if credentials are configured and missing/invalid; `413` if the body or alert count is over the cap;
  `415` for a non-JSON content type; `422` for a payload that is not a valid Alertmanager webhook.
- `GET /healthz` — liveness. Always 200 unless the event loop is wedged.
- `GET /readyz` — readiness. 200 when accepting traffic, 503 when queue ≥ 95% full or breaker is open.
- `GET /metrics` — Prometheus text format: `alert_explainer_alerts_received_total`, `_alerts_dropped_total{reason}`,
  `_enrichments_total{outcome}`, `_enrichment_latency_seconds`, `_llm_tokens_total{type}`, `_llm_cost_usd_total`,
  `_deliveries_total{sink,outcome}`, `_queue_depth`, `_breaker_open`. *(Changed in 0.2; the JSON moved to `/status`.)*
- `GET /status` — JSON snapshot of queue depth, breaker state, version, model, effort, sinks, SLO targets.

## Tracing

Install the `otel` extra and configure any OpenTelemetry SDK/exporter. Each LLM call becomes a
`CLIENT` span named `chat <model>` with the [GenAI semantic-convention](https://github.com/open-telemetry/semantic-conventions-genai)
attributes — request vs. response model (they differ when a refusal fallback ran), total input
tokens including cache reads and writes, output tokens, effort, and `gen_ai.cost.usd` (a toolkit
extension, not a convention).

## Scaling

The queue, dedup window and enrichment cache are in-process, so run one replica and scale
vertically (`WORKER_CONCURRENCY`). Going horizontal means moving all three to a shared store —
Redis Streams for the queue, `SET NX EX` for dedup and the cache — which is the next roadmap item.

## SLO targets (defaults)

| Metric | Target |
|---|---|
| P50 enrichment latency | < 4 s |
| Cost per enriched alert | < $0.01 (Sonnet 5.5 at `low` effort; cached re-fires cost $0) |
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
