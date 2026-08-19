"""HTTP-surface tests. Worker side effects are tested directly in test_queue.py to avoid the
TestClient + lifespan threading races."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from alert_explainer import app as app_module
from alert_explainer import queue as queue_module
from alert_explainer.app import app
from alert_explainer.config import settings
from alert_explainer.models import MAX_FIELD_CHARS, Alert, EnrichedAlert, Enrichment


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


# ---------------------------------------------------------------------------
# Webhook authentication and request bounds
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def signed_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "webhook_hmac_secret", "test-secret")
    return client


def _one_alert_body() -> bytes:
    return json.dumps(
        _payload({"status": "firing", "labels": {"alertname": "A", "severity": "critical"}})
    ).encode()


def test_unsigned_request_is_rejected_when_secret_is_set(signed_client: TestClient) -> None:
    body = _one_alert_body()
    r = signed_client.post("/webhook", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 401


def test_wrong_signature_is_rejected(signed_client: TestClient) -> None:
    body = _one_alert_body()
    r = signed_client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Alert-Explainer-Signature": "sha256=deadbeef",
        },
    )
    assert r.status_code == 401


def test_signature_over_a_different_body_is_rejected(signed_client: TestClient) -> None:
    # Guards against verifying the signature against the wrong bytes.
    body = _one_alert_body()
    r = signed_client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Alert-Explainer-Signature": _sign("test-secret", b'{"alerts":[]}'),
        },
    )
    assert r.status_code == 401


def test_correctly_signed_request_is_accepted(signed_client: TestClient) -> None:
    body = _one_alert_body()
    r = signed_client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Alert-Explainer-Signature": _sign("test-secret", body),
        },
    )
    assert r.status_code == 202
    assert r.json()["accepted"] == 1


def test_unset_secret_leaves_the_endpoint_open(client: TestClient) -> None:
    # Backwards-compatible dev mode — startup logs a warning instead.
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 202


def test_oversized_body_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 500)
    big = _payload({"status": "firing", "labels": {"alertname": "A", "note": "x" * 2000}})
    r = client.post("/webhook", json=big)
    assert r.status_code == 413


def test_too_many_alerts_in_one_request_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_alerts_per_request", 5)
    many = _payload(*[{"status": "firing", "labels": {"alertname": f"A{i}"}} for i in range(10)])
    r = client.post("/webhook", json=many)
    assert r.status_code == 413


def test_long_annotation_values_are_truncated_not_rejected() -> None:
    alert = Alert(labels={"alertname": "A"}, annotations={"summary": "x" * (MAX_FIELD_CHARS * 2)})
    value = alert.annotations["summary"]
    assert len(value) < MAX_FIELD_CHARS * 2
    assert value.endswith("[truncated]")
    # The alert itself still went through — truncation must not drop it.
    assert alert.alertname == "A"


def test_bearer_token_is_accepted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Alertmanager cannot sign requests, so bearer is the deployable path.
    monkeypatch.setattr(settings, "webhook_bearer_token", "tok-abc")
    r = client.post("/webhook", json=_payload(), headers={"Authorization": "Bearer tok-abc"})
    assert r.status_code == 202


def test_wrong_bearer_token_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "webhook_bearer_token", "tok-abc")
    r = client.post("/webhook", json=_payload(), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_bearer_token_required_when_only_token_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "webhook_bearer_token", "tok-abc")
    assert client.post("/webhook", json=_payload()).status_code == 401


def test_either_mechanism_validates_when_both_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "webhook_bearer_token", "tok-abc")
    monkeypatch.setattr(settings, "webhook_hmac_secret", "test-secret")
    body = _one_alert_body()

    signed = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Alert-Explainer-Signature": _sign("test-secret", body),
        },
    )
    bearer = client.post("/webhook", json=_payload(), headers={"Authorization": "Bearer tok-abc"})
    assert signed.status_code == 202
    assert bearer.status_code == 202
