from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALERT_EXPLAINER_", env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024

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
