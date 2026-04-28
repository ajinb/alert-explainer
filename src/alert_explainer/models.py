from typing import Literal

from pydantic import BaseModel, Field

# ---------- Inbound: Prometheus Alertmanager webhook shape ----------
# Reference: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config


class Alert(BaseModel):
    status: Literal["firing", "resolved"] = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "unknown").lower()

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "Unnamed")

    @property
    def priority_rank(self) -> int:
        """Lower rank = higher priority for the asyncio PriorityQueue."""
        return {
            "critical": 0,
            "page": 0,
            "high": 1,
            "warning": 2,
            "info": 3,
            "low": 3,
            "unknown": 4,
        }.get(self.severity, 4)


class AlertmanagerPayload(BaseModel):
    version: str = "4"
    groupKey: str = ""
    receiver: str = ""
    status: str = "firing"
    alerts: list[Alert] = Field(default_factory=list)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)


# ---------- Outbound: enriched alert ----------


class Enrichment(BaseModel):
    summary: str
    likely_causes: list[str]
    triage_checklist: list[str]
    false_positive_check: str
    confidence: Literal["high", "medium", "low"] = "medium"
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    cache_hit: bool = False


class EnrichedAlert(BaseModel):
    alert: Alert
    enrichment: Enrichment | None = None
    enrichment_error: str | None = None  # Set when circuit-breaker tripped or LLM call failed.
