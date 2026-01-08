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

# Copy application code
COPY . .

# Expose port (Railway sets PORT env var)
EXPOSE 8000

# Start command - Railway provides PORT environment variable
# Note: Migrations are run via Base.metadata.create_all in main.py for now
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
