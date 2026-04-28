# Changelog

All notable changes to alert-explainer are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
