"""`python -m alert_explainer` and `alert-explainer` entry point."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "alert_explainer.app:app",
        host=os.environ.get("ALERT_EXPLAINER_HOST", "0.0.0.0"),
        port=int(os.environ.get("ALERT_EXPLAINER_PORT", "8080")),
        log_level=os.environ.get("ALERT_EXPLAINER_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
