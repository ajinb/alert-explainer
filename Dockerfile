FROM python:3.12-slim AS build
# cloudandsre is a direct git+https dependency (not on PyPI), so pip needs a git
# client to resolve it. It stays in this stage — a production container has no
# business shipping a VCS client.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Resolve everything into a venv here, then copy the venv to the runtime stage.
# Installing from wheels in the runtime stage does not work: the wheel metadata
# records the direct URL, so pip re-invokes git even with --no-index.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

FROM python:3.12-slim
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN useradd --no-create-home --uid 1000 app
USER 1000
EXPOSE 8080
ENV ALERT_EXPLAINER_HOST=0.0.0.0 ALERT_EXPLAINER_PORT=8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8080/healthz').status_code==200 else 1)"
CMD ["python", "-m", "alert_explainer"]
