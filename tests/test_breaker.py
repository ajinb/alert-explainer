import asyncio

import pytest

from alert_explainer.breaker import CircuitBreaker, CircuitOpen


async def _ok() -> str:
    return "ok"


async def _boom() -> str:
    raise RuntimeError("boom")


async def test_breaker_opens_after_threshold() -> None:
    cb: CircuitBreaker[str] = CircuitBreaker(failure_threshold=3, reset_seconds=10.0)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    with pytest.raises(CircuitOpen):
        await cb.call(_ok)


async def test_breaker_resets_after_window() -> None:
    cb: CircuitBreaker[str] = CircuitBreaker(failure_threshold=1, reset_seconds=0.05)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    with pytest.raises(CircuitOpen):
        await cb.call(_ok)
    await asyncio.sleep(0.1)
    assert await cb.call(_ok) == "ok"


async def test_breaker_success_resets_failure_count() -> None:
    cb: CircuitBreaker[str] = CircuitBreaker(failure_threshold=3, reset_seconds=10.0)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert await cb.call(_ok) == "ok"  # success
    # Two more failures — should NOT open the breaker because the count was reset.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert not cb.is_open
