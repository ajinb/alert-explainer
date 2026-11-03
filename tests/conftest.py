"""Module-level caches outlive a single test; reset them so tests stay order-independent."""

import pytest

from alert_explainer import app as app_module
from alert_explainer import sink as sink_module


@pytest.fixture(autouse=True)
def _reset_process_state():
    app_module.recent_alerts._data.clear()
    app_module.queue._cache._data.clear()
    sink_module._sinks = None
    yield
    sink_module._sinks = None
