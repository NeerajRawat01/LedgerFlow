# --- LedgerFlow backend image ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=development \
    SERVICE_NAME=ledgerflow-backend

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# Filesystem persistence roots (mounted from the host in docker-compose).
ENV DATA_DIR=/app/data \
    STORAGE_DIR=/app/storage \
    OUTPUT_DIR=/app/output

EXPOSE 8000

# Backend health check (no curl in slim image; use Python stdlib).
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Intentionally NO database connection configuration.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
