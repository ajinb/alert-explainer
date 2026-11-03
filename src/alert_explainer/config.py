from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALERT_EXPLAINER_", env_file=".env", extra="ignore"
    )

    anthropic_api_key: str = ""
    model: str = "claude-sonnet-5-5"
    # Output cap per call. Thinking counts against it on current models, so it
    # is set well above the ~400 tokens a brief actually needs.
    max_tokens: int = 4096
    # Thinking depth. Alert triage is latency-bound (see slo_p50_latency_ms), so
    # the default is the cheapest level; raise it if briefs come back shallow.
    effort: str = "low"
    # Optional file of team context — service catalogue, dashboard names,
    # runbook conventions — appended to the system prompt. It sits inside the
    # cached prefix, so it is paid for once per cache window, not per alert.
    system_context_file: str = ""

    # Authentication for the Alertmanager webhook. Every enqueued alert costs an
    # LLM call, so an unauthenticated endpoint is a cost-amplification DoS.
    #
    # Two mechanisms; configure at least one. Alertmanager cannot compute request
    # signatures, so a bearer token is what you want when it posts here directly
    # (http_config.authorization). Use the HMAC secret when a relay or gateway
    # signs on its behalf, or for any sender you control. If both are set, either
    # one validates. Both empty disables verification — dev only; the app warns
    # loudly at startup.
    webhook_bearer_token: str = ""
    webhook_hmac_secret: str = ""

    # Inbound request bounds. A single POST should not be able to exhaust memory
    # or enqueue unbounded paid work.
    max_body_bytes: int = 1_048_576  # 1 MiB
    max_alerts_per_request: int = 200

    # Alertmanager re-sends the same firing alert on every repeat_interval and
    # retries any POST that did not return 2xx. Within this window an identical
    # (fingerprint, status, startsAt) is suppressed before it reaches the queue.
    dedup_window_seconds: float = 300.0
    # A re-fire of an alert we already explained reuses that brief for this long.
    enrichment_cache_ttl_seconds: float = 1800.0
    cache_maxsize: int = 5000

    # Where enriched alerts go. Comma-separated: slack, teams, pagerduty,
    # opsgenie, webhook, stdout. Empty keeps the v0.1 behaviour: Slack-formatted
    # POST to downstream_webhook_url when it is set, stdout otherwise.
    sinks: str = ""
    downstream_webhook_url: str = ""  # used by the slack and webhook sinks
    slack_webhook_url: str = ""  # overrides downstream_webhook_url for slack
    teams_webhook_url: str = ""  # Teams Workflows webhook (Adaptive Card)
    pagerduty_routing_key: str = ""
    pagerduty_events_url: str = "https://events.pagerduty.com/v2/enqueue"
    opsgenie_api_key: str = ""
    opsgenie_api_url: str = "https://api.opsgenie.com/v2/alerts"  # api.eu.opsgenie.com for EU

    # Delivery: transient failures (connect errors, 429, 5xx) retry with
    # exponential backoff; anything still undelivered is appended to the
    # dead-letter file (JSONL) when one is configured, and always counted.
    delivery_max_attempts: int = 3
    delivery_timeout_seconds: float = 10.0
    dead_letter_path: str = ""

    # Circuit breaker
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    enrich_timeout_seconds: float = 20.0

    # Queue (in-memory asyncio.PriorityQueue — graduate to Redis when fleet > 1)
    queue_maxsize: int = 1000
    worker_concurrency: int = 4

    # SLO targets (also exposed on /status for visibility)
    slo_p50_latency_ms: int = 4000
    slo_cost_per_alert_usd: float = 0.01

    log_level: str = "INFO"


settings = Settings()
