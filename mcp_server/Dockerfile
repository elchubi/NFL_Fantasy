# ---- Stage 1: dependencies --------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEFAULT_PORT=8080 \
    APP_MODULE=server:app \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY server.py backend.py entrypoint.py ./

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# The MCP server's own liveness, separate from the backend's /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "entrypoint.py"]
