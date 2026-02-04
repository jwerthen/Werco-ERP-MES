# Multi-target Dockerfile (build backend or frontend)
# Usage:
#   docker build -t werco-erp-frontend --target frontend --build-arg REACT_APP_API_URL=... .
#   docker build -t werco-erp-backend --target backend .

# =========================
# Frontend image
# =========================
FROM node:18-alpine AS frontend-build

WORKDIR /app

# Copy package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci --legacy-peer-deps

# Copy source code (cache bust)
ARG CACHEBUST=1
COPY frontend/ ./

# Build argument for API URL (set during Railway build)
ARG REACT_APP_API_URL
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN if [ -z "$REACT_APP_API_URL" ]; then echo "REACT_APP_API_URL is required at build time"; exit 1; fi

# Build the app
RUN npm run build

FROM node:18-alpine AS frontend

WORKDIR /app

# Install serve for static file serving
RUN npm install -g serve

# Copy built files from build stage
COPY --from=frontend-build /app/build ./build

# Expose port
EXPOSE 3000

# Start server - Railway provides PORT env var
CMD serve -s build -l ${PORT:-3000}


# =========================
# Backend image (default stage)
# =========================
FROM python:3.11-slim AS backend

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
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend application code (bust cache with ARG)
ARG CACHEBUST=1
COPY backend/ ./

# Expose port (Railway sets PORT env var)
EXPOSE 8000

# Start command - Railway provides PORT environment variable
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers
