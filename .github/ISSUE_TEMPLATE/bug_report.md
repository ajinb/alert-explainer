---
name: Bug report
about: Something is broken or behaving unexpectedly
labels: bug
---

**What happened**

<!-- A clear, concrete description. Paste logs and stack traces in code blocks. -->

**Steps to reproduce**

1.
2.
3.

**What you expected**

<!-- What should have happened instead. -->

**Environment**

- alert-explainer version: <!-- run: docker exec ... alert-explainer --version, or check /metrics -->
- Python version:
- Deployment: <!-- docker-compose / Kubernetes / bare metal -->
- Alertmanager version:
- Anthropic model in use: <!-- e.g. claude-sonnet-4-6 -->

**Sample alert that triggered the bug**

<!-- If safe to share, paste a redacted alert payload here. -->

```json
{ ... }
```
