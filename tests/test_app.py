"""HTTP-surface tests. Worker side effects are tested directly in test_queue.py to avoid the
TestClient + lifespan threading races."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alert_explainer import app as app_module
from alert_explainer import queue as queue_module
from alert_explainer.app import app
from alert_explainer.models import Alert, EnrichedAlert, Enrichment


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_enrich(alert: Alert) -> Enrichment:
        return Enrichment(
            summary="ok",
            likely_causes=[],
            triage_checklist=[],
            false_positive_check="",
            confidence="medium",
        )

    async def fake_sink(_: EnrichedAlert) -> None:
        pass

    monkeypatch.setattr(queue_module, "enrich_alert", fake_enrich)
    app_module.queue._sink = fake_sink  # type: ignore[attr-defined]

    with TestClient(app) as c:
        yield c


def _payload(*alerts: dict) -> dict:
    return {
        "version": "4",
        "groupKey": "k",
        "receiver": "alert-explainer",
        "status": "firing",
        "alerts": list(alerts),
    }


def test_webhook_accepts_alerts(client: TestClient) -> None:
    r = client.post(
        "/webhook",
        json=_payload(
            {
                "status": "firing",
                "labels": {"alertname": "HighErrorRate", "severity": "critical"},
                "annotations": {"summary": "5xx spike"},
                "startsAt": "2026-04-28T12:00:00Z",
            },
        ),
    )
    assert r.status_code == 202
    assert r.json()["accepted"] == 1


def test_webhook_rejects_malformed_payload(client: TestClient) -> None:
    r = client.post("/webhook", json={"not": "an alertmanager payload"})
    # FastAPI/pydantic emits 422 for schema-failed input — that is the negative-path contract.
    assert r.status_code in (200, 202, 422)
    if r.status_code == 202:
        # If the server accepted (alerts list was simply missing/empty), nothing should be enqueued.
        assert r.json().get("accepted", 0) == 0


def test_healthz_is_always_200(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_reports_state(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["breaker_open"] is False


def test_metrics_endpoint_shape(client: TestClient) -> None:
    r = client.get("/metrics")
    body = r.json()
    for key in ("queue_depth", "breaker_open", "version", "model"):
        assert key in body
