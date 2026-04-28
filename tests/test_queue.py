"""Direct unit tests on AlertWorkQueue (no FastAPI). The TestClient + lifespan threading model
makes asserting on async worker side effects racy, so we exercise the queue here instead."""

from __future__ import annotations

import asyncio

import pytest

from alert_explainer import queue as queue_module
from alert_explainer.models import Alert, EnrichedAlert, Enrichment
from alert_explainer.queue import AlertWorkQueue


def _alert(name: str, severity: str) -> Alert:
    return Alert(labels={"alertname": name, "severity": severity})


async def _fake_enrich(alert: Alert) -> Enrichment:
    return Enrichment(
        summary=f"summary for {alert.alertname}",
        likely_causes=["c"],
        triage_checklist=["s"],
        false_positive_check="check",
        confidence="medium",
    )


async def test_priority_drain_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_module, "enrich_alert", _fake_enrich)

    captured: list[EnrichedAlert] = []

    async def sink(e: EnrichedAlert) -> None:
        captured.append(e)

    q = AlertWorkQueue(sink=sink)
    # Enqueue out of order: info first, then critical, then warning.
    await q.enqueue(_alert("Info1", "info"))
    await q.enqueue(_alert("Crit1", "critical"))
    await q.enqueue(_alert("Warn1", "warning"))

    # Single worker so drain order is deterministic.
    await q.start(n_workers=1)
    for _ in range(50):
        if len(captured) >= 3:
            break
        await asyncio.sleep(0.02)
    await q.stop()

    assert [e.alert.alertname for e in captured] == ["Crit1", "Warn1", "Info1"]


async def test_passthrough_on_enrichment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(alert: Alert) -> Enrichment:
        raise RuntimeError("LLM is sad")

    monkeypatch.setattr(queue_module, "enrich_alert", boom)

    captured: list[EnrichedAlert] = []

    async def sink(e: EnrichedAlert) -> None:
        captured.append(e)

    q = AlertWorkQueue(sink=sink)
    await q.enqueue(_alert("Whatever", "warning"))
    await q.start(n_workers=1)
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.02)
    await q.stop()

    assert len(captured) == 1
    assert captured[0].enrichment is None
    assert "LLM is sad" in (captured[0].enrichment_error or "")


async def test_full_queue_returns_false() -> None:
    async def sink(_: EnrichedAlert) -> None:
        pass

    q = AlertWorkQueue(sink=sink)
    # Force tiny capacity by reaching into the underlying queue.
    q._q = asyncio.PriorityQueue(maxsize=2)  # type: ignore[assignment]
    assert await q.enqueue(_alert("A", "warning")) is True
    assert await q.enqueue(_alert("B", "warning")) is True
    assert await q.enqueue(_alert("C", "warning")) is False
