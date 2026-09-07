# ---- Stage 1: build the dependency wheels -----------------------------------
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
    DEFAULT_PORT=8000 \
    APP_MODULE=main:app \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYERS_CACHE_PATH=/data/players_cache.json

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY main.py entrypoint.py ./
COPY app ./app

# Non-root user, with a writable volume for the player cache.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status == 200 else 1)"

CMD ["python", "entrypoint.py"]
