FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata \
    # Dashboard param changes survive restarts on the `backendstate` volume.
    STRATEGY_OVERRIDES_FILE=/app/state/strategies.local.yaml

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY alembic.ini strategies.yaml ./
COPY app ./app

RUN useradd --create-home --uid 10001 tradeflow \
    && mkdir -p /app/state \
    && chown tradeflow:tradeflow /app/state
USER tradeflow

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
# Migrations run on every start (idempotent). The process exits after the daily Kite
# session expiry; Docker's restart policy starts a fresh one (KiteTicker can't restart).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-server-header"]
