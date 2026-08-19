from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALERT_EXPLAINER_", env_file=".env", extra="ignore"
    )

    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024

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

    # Downstream where enriched alerts are POSTed (Slack incoming-webhook URL, PagerDuty events API, or your own).
    downstream_webhook_url: str = ""

    # Circuit breaker
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    enrich_timeout_seconds: float = 8.0

    # Queue (in-memory asyncio.PriorityQueue — graduate to Redis when fleet > 1)
    queue_maxsize: int = 1000
    worker_concurrency: int = 4

    # SLO targets (also exposed on /metrics-style endpoint for visibility)
    slo_p50_latency_ms: int = 4000
    slo_cost_per_alert_usd: float = 0.01

    log_level: str = "INFO"


settings = Settings()
