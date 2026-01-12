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

# Copy application code (bust cache with ARG)
ARG CACHEBUST=1
COPY . .

# Expose port (Railway sets PORT env var)
EXPOSE 8000

# Start command - Railway provides PORT environment variable
# Run Alembic migrations, then start the server
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
