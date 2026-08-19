FROM python:3.12-slim AS build
# cloudandsre is a direct git+https dependency (it is not on PyPI), so pip needs
# a git client to resolve it. Installing it here keeps it out of the runtime
# image — a production container has no business shipping a VCS client.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
# Build the app wheel, then build wheels for it and every dependency, so the
# runtime stage can install completely offline.
RUN pip install --no-cache-dir --upgrade pip build && \
    python -m build --wheel --outdir /dist && \
    pip wheel --no-cache-dir --wheel-dir /wheels /dist/*.whl

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels alert-explainer && rm -rf /wheels
RUN useradd --no-create-home --uid 1000 app
USER 1000
EXPOSE 8080
ENV ALERT_EXPLAINER_HOST=0.0.0.0 ALERT_EXPLAINER_PORT=8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8080/healthz').status_code==200 else 1)"
CMD ["python", "-m", "alert_explainer"]
