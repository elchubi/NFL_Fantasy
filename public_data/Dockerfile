FROM python:3.12-slim AS builder
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEFAULT_PORT=8000 \
    APP_MODULE=main:app \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY main.py entrypoint.py ./
COPY app ./app

# Non-root user. Stays root at container start on purpose: entrypoint.py
# fixes ownership of whatever gets mounted at /data (a Railway Volume mounts
# as a separate, typically root-owned filesystem regardless of this chown)
# and drops to appuser itself before serving anything - see entrypoint.py.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

# No VOLUME instruction: Railway rejects it at build time ("use Railway
# Volumes" instead) and Docker Compose doesn't need it either - the named
# volume in docker-compose.yml and the Railway Volume mounted from its UI
# both just bind-mount over /data without the image declaring it.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status == 200 else 1)"

CMD ["python", "entrypoint.py"]
