# Contributing to alert-explainer

Thanks for considering a contribution. A few principles before you start:

- **Working code beats clever code.** This project ships with the assumption that someone will paste it in front of their on-call rotation tomorrow. Bias toward simple, observable, explicit.
- **Negative tests are the load-bearing tests.** A green positive-path suite tells you the happy path works. The negative suite tells you the breaker, the queue limits, the timeouts, and the schema validation are doing their jobs. Add negative tests for any behavior change.
- **Stay simple.** Default to "the simplest thing that holds." If you reach for a new dependency, name the graduation trigger that justifies it (e.g., "Redis when fleet > 1," "OPA when YAML can't express it"). See [the simplicity sidebar in the companion book chapter on policy](https://cloudandsre.com/blog).

## Local setup

```bash
git clone https://github.com/ajinb/alert-explainer.git
cd alert-explainer
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Run the service against a sample alert:

```bash
export ALERT_EXPLAINER_ANTHROPIC_API_KEY=...        # your key
python -m alert_explainer &                          # starts FastAPI on :8080
curl -X POST http://localhost:8080/webhook \
  -H 'content-type: application/json' \
  -d @examples/sample_alert.json
```

## Pull requests

- One change per PR. Smaller is better.
- Run `ruff check src tests && pytest` locally before pushing.
- Update the README if you change configuration, endpoints, or deployment.
- The `CHANGELOG.md` gets a one-line entry under `Unreleased` for any user-visible change.

## What this project is *not*

- Not a remediation tool. alert-explainer never executes actions on your infrastructure. It enriches alerts. The human acts.
- Not a replacement for runbooks. It surfaces what is in the alert and what the model can reason about. Real runbooks still belong in your knowledge base.
- Not a monitoring system. It plugs into the one you already run.
