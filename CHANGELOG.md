# Changelog

All notable changes to alert-explainer are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-11

### Security
- `/webhook` buffered the whole request body before checking its size, so a chunked request
  with no `Content-Length` could exhaust memory. The body is now read incrementally and cut
  off at `MAX_BODY_BYTES`.
- 422 responses no longer echo the offending input back to the caller.
- The default bind address is `127.0.0.1`; the container image opts into `0.0.0.0`.
- `cloudandsre` is pinned to the `v0.2.0` tag instead of `@main`.

### Added
- **Sinks:** Microsoft Teams (Adaptive Card), PagerDuty Events API v2 (trigger/resolve on one
  `dedup_key`), Opsgenie (create/close by alias), raw JSON webhook; fan-out to several at once (#10).
- **Delivery retries** with exponential backoff and `Retry-After` support, plus a JSONL
  dead-letter file for anything still undelivered (#1).
- **Prometheus metrics** at `/metrics` (text format): received, dropped (by reason),
  enrichment outcomes, latency histogram, tokens, spend, deliveries, queue depth, breaker (#2).
- **Retry-storm dedup** on `(fingerprint, status, startsAt)` (#4) and an **enrichment cache**
  keyed by fingerprint so a re-fire costs nothing (#6).
- Stricter webhook validation (`status` enum, content type, non-object bodies) with negative-path
  tests (#3).
- **Helm chart** (`charts/alert-explainer`): non-root, read-only root FS, existing-Secret
  support, optional ServiceMonitor and PDB (#7).
- **Structured outputs**: the triage brief is schema-constrained by the API.
- **Refusal fallback** (`fallbacks: "default"`) on supported models; a surviving refusal is
  reported without a pointless retry and never trips the breaker.
- Optional **OpenTelemetry GenAI spans** (`pip install ".[otel]"`).
- `SYSTEM_CONTEXT_FILE`: team context inside the cached prompt prefix.
- Tests that drive the real Anthropic SDK through a mock transport.

### Changed
- Default model `claude-sonnet-4-6` → `claude-sonnet-5-5`, effort `low`, `MAX_TOKENS` 4096,
  `ENRICH_TIMEOUT_SECONDS` 20. `anthropic` pinned to `>=2.4,<3`.
- `/metrics` is Prometheus text; the old JSON snapshot moved to `/status`.
- Cost is attributed to the model that served the reply (relevant when a fallback ran).

### Fixed
- The prompt-caching claim: caching only engages above the model's minimum cacheable prefix,
  which the stock prompt does not reach on its own. The README and docstring now say so.

## [0.1.0] — 2026-04-28

Initial release.

- FastAPI app exposing `POST /webhook` for the standard Alertmanager v4 payload, plus `/healthz`, `/readyz`, `/metrics`.
- AI enrichment via Anthropic Claude with prompt caching on the system prompt (~90% input-cost cut on cache hits).
- WAF reliability patterns implemented out of the box:
  - **Circuit Breaker** wrapping every LLM call (in-process, lock-protected).
  - **Priority Queue** so `critical` drains before `warning` drains before `info`.
  - **Queue-Based Load Leveling** via `asyncio.PriorityQueue` with bounded `maxsize` so spikes shed gracefully (queue-full alerts fall through to the on-call engineer un-enriched rather than dropping).
  - **Health Endpoint Monitoring** via `/healthz` and `/readyz` (the latter goes 503 when saturated or breaker-open).
- Default sink: stdout in dev, Slack incoming-webhook formatting in prod (`ALERT_EXPLAINER_DOWNSTREAM_WEBHOOK_URL`).
- Dockerfile + docker-compose for local runs; CI matrix on Python 3.11 and 3.12.
