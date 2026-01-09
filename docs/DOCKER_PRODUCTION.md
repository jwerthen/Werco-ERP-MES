# Docker Production Deployment Guide

This guide covers deploying Werco ERP using Docker in a production environment.

## Prerequisites

- Docker Engine 20.10+
- Docker Compose V2
- SSL certificates (Let's Encrypt recommended)
- Domain name configured with DNS

## Quick Start

```bash
# 1. Create production environment file
cp .env.prod.example .env.prod
# Edit .env.prod with your actual values

# 2. Generate secure secrets
python -c "import secrets; print(secrets.token_urlsafe(64))"  # For SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(64))"  # For REFRESH_TOKEN_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"  # For POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(32))"  # For REDIS_PASSWORD

# 3. Set up SSL certificates
mkdir -p nginx/certs
# Copy your SSL certificates:
# - nginx/certs/fullchain.pem (certificate + chain)
# - nginx/certs/privkey.pem (private key)

# 4. Build and start services
docker-compose -f docker-compose.prod.yml build
docker-compose -f docker-compose.prod.yml up -d

# 5. Check status
docker-compose -f docker-compose.prod.yml ps
docker-compose -f docker-compose.prod.yml logs -f
```

## Architecture

```
                    ┌─────────────────┐
                    │    Internet     │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Nginx (443)    │  ← SSL termination, rate limiting
                    └────────┬────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
┌────────▼────────┐ ┌────────▼────────┐ ┌───────▼────────┐
│    Frontend     │ │    Backend      │ │    Worker      │
│  (React/nginx)  │ │   (FastAPI)     │ │  (ARQ Jobs)    │
└─────────────────┘ └────────┬────────┘ └───────┬────────┘
                             │                   │
                    ┌────────┴───────────────────┴────────┐
                    │              Internal Network        │
                    └────────┬───────────────────┬────────┘
                             │                   │
                    ┌────────▼────────┐ ┌───────▼────────┐
                    │   PostgreSQL    │ │     Redis      │
                    │   (Database)    │ │   (Cache/Queue)│
                    └─────────────────┘ └────────────────┘
```

## Services

| Service | Purpose | Port (Internal) | Port (External) |
|---------|---------|-----------------|-----------------|
| nginx | Reverse proxy, SSL, rate limiting | - | 80, 443 |
| frontend | React static files | 3000 | - |
| backend | FastAPI application | 8000 | - |
| worker | Background job processor | - | - |
| db | PostgreSQL database | 5432 | - |
| redis | Cache and job queue | 6379 | - |

## Security Features

### Network Isolation
- Internal services (db, redis) are on an isolated network
- Only nginx is exposed to the external network
- Services communicate via internal Docker network

### SSL/TLS
- Modern TLS 1.2/1.3 configuration
- Strong cipher suites
- HTTP to HTTPS redirect

### Rate Limiting
- API endpoints: 10 requests/second (burst 20)
- Login endpoints: 1 request/second (burst 5)
- Connection limit: 20 per IP

### Security Headers
- X-Frame-Options: SAMEORIGIN
- X-Content-Type-Options: nosniff
- X-XSS-Protection: 1; mode=block
- Referrer-Policy: strict-origin-when-cross-origin

### Non-Root Containers
- Backend and frontend run as non-root users
- Minimal base images (Alpine/slim)

## SSL Certificate Setup

### Let's Encrypt (Recommended)

```bash
# Install certbot
apt-get install certbot

# Get certificates (stop nginx first)
docker-compose -f docker-compose.prod.yml stop nginx
certbot certonly --standalone -d erp.yourcompany.com

# Copy certificates
cp /etc/letsencrypt/live/erp.yourcompany.com/fullchain.pem nginx/certs/
cp /etc/letsencrypt/live/erp.yourcompany.com/privkey.pem nginx/certs/

# Start nginx
docker-compose -f docker-compose.prod.yml up -d nginx

# Set up auto-renewal (add to crontab)
0 0 1 * * certbot renew --pre-hook "docker-compose -f /path/to/docker-compose.prod.yml stop nginx" --post-hook "docker-compose -f /path/to/docker-compose.prod.yml start nginx"
```

### Self-Signed (Development/Testing Only)

```bash
mkdir -p nginx/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/certs/privkey.pem \
  -out nginx/certs/fullchain.pem \
  -subj "/CN=localhost"
```

## Operations

### View Logs

```bash
# All services
docker-compose -f docker-compose.prod.yml logs -f

# Specific service
docker-compose -f docker-compose.prod.yml logs -f backend

# Last 100 lines
docker-compose -f docker-compose.prod.yml logs --tail=100 backend
```

### Database Backup

```bash
# Create backup
docker exec werco-db-prod pg_dump -U $POSTGRES_USER $POSTGRES_DB > backup_$(date +%Y%m%d_%H%M%S).sql

# Restore backup
docker exec -i werco-db-prod psql -U $POSTGRES_USER $POSTGRES_DB < backup.sql
```

### Update Deployment

```bash
# Pull latest code
git pull origin main

# Rebuild and restart
docker-compose -f docker-compose.prod.yml build
docker-compose -f docker-compose.prod.yml up -d

# Or zero-downtime update (one service at a time)
docker-compose -f docker-compose.prod.yml up -d --no-deps --build backend
docker-compose -f docker-compose.prod.yml up -d --no-deps --build frontend
docker-compose -f docker-compose.prod.yml up -d --no-deps --build worker
```

### Scaling

```bash
# Scale worker instances
docker-compose -f docker-compose.prod.yml up -d --scale worker=3
```

### Health Checks

```bash
# Check all services
docker-compose -f docker-compose.prod.yml ps

# Backend health
curl -k https://localhost/health/ready

# Database connectivity
docker exec werco-db-prod pg_isready -U $POSTGRES_USER

# Redis connectivity
docker exec werco-redis-prod redis-cli -a $REDIS_PASSWORD ping
```

## Troubleshooting

### Service Won't Start

```bash
# Check logs
docker-compose -f docker-compose.prod.yml logs backend

# Check container status
docker inspect werco-backend-prod

# Verify environment variables
docker-compose -f docker-compose.prod.yml config
```

### Database Connection Issues

```bash
# Test database connectivity
docker exec werco-backend-prod python -c "from app.db.database import engine; engine.connect()"

# Check database logs
docker-compose -f docker-compose.prod.yml logs db
```

### SSL Certificate Issues

```bash
# Verify certificate
openssl x509 -in nginx/certs/fullchain.pem -text -noout

# Test SSL
openssl s_client -connect localhost:443
```

### Memory Issues

```bash
# Check resource usage
docker stats

# Increase limits in docker-compose.prod.yml
deploy:
  resources:
    limits:
      memory: 4G
```

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_USER` | Yes | Database username |
| `POSTGRES_PASSWORD` | Yes | Database password |
| `POSTGRES_DB` | No | Database name (default: werco_erp) |
| `SECRET_KEY` | Yes | JWT signing key (64+ chars) |
| `REFRESH_TOKEN_SECRET_KEY` | Yes | Refresh token key (64+ chars) |
| `REDIS_PASSWORD` | Yes | Redis password |
| `CORS_ORIGINS` | Yes | Allowed CORS origins |
| `REACT_APP_API_URL` | Yes | Frontend API URL |
| `SMTP_*` | Yes | Email configuration |
| `ANTHROPIC_API_KEY` | No | AI features (optional) |
| `SENTRY_DSN` | No | Error tracking (optional) |
| `GUNICORN_WORKERS` | No | Backend workers (default: 4) |

## Resource Requirements

### Minimum (Small Installation)
- 2 CPU cores
- 4 GB RAM
- 20 GB storage

### Recommended (Production)
- 4+ CPU cores
- 8+ GB RAM
- 100+ GB SSD storage
- Separate database server for high availability

## Next Steps

1. Configure monitoring (Prometheus/Grafana)
2. Set up log aggregation (ELK stack)
3. Configure automated backups
4. Set up alerting for health check failures
5. Configure CDN for static assets
