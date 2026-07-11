FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for PostgreSQL and PDF processing
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    poppler-utils \
    tesseract-ocr \
    antiword \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Copy application code (bust cache with ARG)
ARG CACHEBUST=1
COPY --chown=appuser:appgroup . .

# Writable dirs for the non-root user. COPY --chown does not change /app
# itself (root-owned from WORKDIR), and documents.py makedirs /app/uploads at
# import time - without this the app crashes with PermissionError on startup
# (matches the mkdir/chown done in Dockerfile.prod).
RUN mkdir -p /app/uploads /app/logs && chown appuser:appgroup /app /app/uploads /app/logs

# Trust the reverse proxy's X-Forwarded-Proto regardless of how the container is
# launched. The CMD below also passes --forwarded-allow-ips=*, but Railway can
# override the container start command, in which case that CLI flag never applies
# (this is why the trailing-slash 307 kept building an http:// Location in prod).
# uvicorn reads FORWARDED_ALLOW_IPS from the environment independently of the
# start command, so setting it here restores the proxy-header trust as
# defense-in-depth. Mirrors Dockerfile.prod. See test_deploy_proxy_headers.py.
ENV FORWARDED_ALLOW_IPS=*

# Expose port (Railway sets PORT env var)
EXPOSE 8000

# Run as non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8000}/health')" || exit 1

# Start command - Railway provides PORT environment variable
# Run Alembic migrations, then start the server.
# --forwarded-allow-ips="*" is required with --proxy-headers: behind Railway's TLS
# proxy the peer is not 127.0.0.1, so without it uvicorn ignores X-Forwarded-Proto,
# request.url.scheme stays "http", and FastAPI's trailing-slash 307 redirects build
# an http:// Location the browser refuses to follow from an https page (status 0).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips=*"]
