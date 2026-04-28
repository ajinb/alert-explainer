FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip build && \
    python -m build --wheel --outdir /wheels

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
RUN useradd --no-create-home --uid 1000 app
USER 1000
EXPOSE 8080
ENV ALERT_EXPLAINER_HOST=0.0.0.0 ALERT_EXPLAINER_PORT=8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8080/healthz').status_code==200 else 1)"
CMD ["python", "-m", "alert_explainer"]
