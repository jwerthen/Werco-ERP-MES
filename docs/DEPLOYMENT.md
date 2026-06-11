# Deployment Guide

This guide covers deploying Werco ERP to production environments.

## Prerequisites

- Production server with:
  - Python 3.11+
  - Node.js 18+
  - Supabase project with Postgres database access
  - Redis (recommended for caching)
  - Nginx or similar reverse proxy
- Domain name with SSL certificate
- S3-compatible storage for document files (`STORAGE_BACKEND=s3`; alternatively a mounted, backed-up volume at `UPLOAD_DIR` for `STORAGE_BACKEND=local`)

## Environment Configuration

### Production .env File

Create a secure `.env` file in the backend directory:

```env
# Database
DATABASE_URL=postgresql://postgres.meatfdvteugbeksckgqg:<SUPABASE_DB_PASSWORD>@aws-1-us-west-2.pooler.supabase.com:5432/postgres
DATABASE_PROVIDER=supabase

# Security - MUST be random and strong
SECRET_KEY=generate-with-openssl-rand-base64-32
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=480

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_TIMES=100
RATE_LIMIT_SECONDS=60

# Application
APP_NAME=Werco ERP Production
DEBUG=false
ENVIRONMENT=production
API_V1_PREFIX=/api/v1

# CORS - Update with your frontend domain
CORS_ORIGINS=https://werco-erp.yourdomain.com
CORS_ALLOW_CREDENTIALS=true
CORS_ALLOW_METHODS=GET,POST,PUT,DELETE,PATCH,OPTIONS
CORS_ALLOW_HEADERS=*

# File Storage — durable bytes for document uploads, shipping labels/BOLs, RFQ
# files, and PO source documents (app/services/storage_service.py). The default
# STORAGE_BACKEND=local keeps files on the container filesystem (only durable if
# /app/uploads is a mounted volume); "s3" uses AWS S3 or any S3-compatible store
# and fails fast at boot if bucket/credentials are missing.
# See docs/ENVIRONMENT_VARIABLES.md -> File Storage.
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
S3_BUCKET_NAME=werco-erp-documents-prod
# Required for S3-compatible stores (Railway buckets, Cloudflare R2); omit for AWS S3
# S3_ENDPOINT_URL=https://your-s3-compatible-endpoint

# Monitoring
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
LOG_LEVEL=WARN

# Redis Cache
REDIS_URL=redis://localhost:6379/0

# LLM Integration (optional)
ANTHROPIC_API_KEY=your_anthropic_key

# Audit Log Retention / Archival (CMMC AU-3.3.8) — runs in the ARQ worker.
# Audit logs are immutable and never deleted; aged rows are archived monthly to
# this directory. Point it at a persistent, backed-up volume on the worker.
# See docs/AUDIT_LOG_RETENTION_RUNBOOK.md.
AUDIT_ARCHIVE_DIR=/var/lib/werco/audit-archive
```

> **Background jobs run in the ARQ worker** (`arq app.worker.WorkerSettings`), separate from the API
> process — make sure it is running. Its monthly `archive_aged_audit_logs_job` (1st of month, 03:00)
> exports aged audit rows to `AUDIT_ARCHIVE_DIR`; provision that path as durable, backed-up storage so
> archives survive restarts/rebuilds. The weekly `cleanup_old_logs_job` purges only ephemeral
> job/notification logs — **not** audit logs. See `docs/AUDIT_LOG_RETENTION_RUNBOOK.md` and the
> Audit Log Retention / Archival section of `docs/ENVIRONMENT_VARIABLES.md`.

### Generate Secure Secret Key

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

or

```bash
openssl rand -base64 32
```

## Deployment Methods

### Option 1: Docker Compose (Simpler)

1. **Use the production compose file with Supabase configured:**
   ```yaml
   services:
     redis:
       image: redis:7-alpine
       restart: always

     backend:
       build: ./backend
       ports:
         - "8000:8000"
       environment:
         - DATABASE_URL=${DATABASE_URL}
         - DATABASE_PROVIDER=supabase
         - REDIS_URL=redis://redis:6379/0
       depends_on:
         - redis
       restart: always

     frontend:
       build: ./frontend
       ports:
         - "3000:80"
       restart: always

   ```

2. **Start services:**
   ```bash
   docker-compose -f docker-compose.prod.yml up -d
   ```

3. **Run database migrations:**
   ```bash
   docker-compose -f docker-compose.prod.yml exec backend alembic upgrade head
   ```
   > On a **brand-new, empty** database, `alembic upgrade head` alone will fail —
   > follow the bootstrap order in [Database Setup](#database-setup) (create_all →
   > `alembic stamp <baseline>` → incremental `upgrade head`) first. This step is
   > the standard path only once the DB has been bootstrapped.

### Option 2: Systemd Services (Recommended for Production)

#### Backend Service

Create `/etc/systemd/system/werco-erp-backend.service`:

```ini
[Unit]
Description=Werco ERP Backend
After=network.target postgresql.service

[Service]
Type=notify
User=werco
Group=werco
WorkingDirectory=/opt/werco-erp/backend
Environment="PATH=/opt/werco-erp/backend/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/werco-erp/backend/.env
ExecStart=/opt/werco-erp/backend/venv/bin/gunicorn app.main:app \
    --workers 4 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --access-logfile /var/log/werco-erp/backend-access.log \
    --error-logfile /var/log/werco-erp/backend-error.log \
    --log-level warning

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### Frontend Service

Create `/etc/systemd/system/werco-erp-frontend.service`:

```ini
[Unit]
Description=Werco ERP Frontend
After=network.target

[Service]
Type=simple
User=werco
Group=werco
WorkingDirectory=/opt/werco-erp/frontend
ExecStart=/usr/bin/node /opt/werco-erp/frontend/node_modules/.bin/serve \
    -s build -l 3000

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### Start services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable werco-erp-backend werco-erp-frontend
sudo systemctl start werco-erp-backend werco-erp-frontend
```

## Nginx Configuration

Create `/etc/nginx/sites-available/werco-erp`:

```nginx
# Rate limiting
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;

# Upstream servers
upstream backend {
    server 127.0.0.1:8000;
}

upstream frontend {
    server 127.0.0.1:3000;
}

# HTTP to HTTPS redirect
server {
    listen 80;
    server_name werco-erp.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

# HTTPS server
server {
    listen 443 ssl http2;
    server_name werco-erp.yourdomain.com;

    # SSL certificates
    ssl_certificate /etc/letsencrypt/live/werco-erp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/werco-erp.yourdomain.com/privkey.pem;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Frontend
    location / {
        proxy_pass http://frontend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Backend API
    location /api/ {
        limit_req zone=api burst=20 nodelay;

        proxy_pass http://backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Health check endpoint
    location /health {
        proxy_pass http://backend;
        # Forward the real client Host (not the "backend" upstream name) so the
        # backend's TrustedHostMiddleware allowlist (ALLOWED_HOSTS) sees a real
        # hostname. See docs/ENVIRONMENT_VARIABLES.md#trusted-hosts-http-host-header
        proxy_set_header Host $host;
        access_log off;
    }
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/werco-erp /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## SSL Certificates (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d werco-erp.yourdomain.com
```

## Database Setup

1. **Create database user:**
   ```sql
   CREATE USER werco_prod_user WITH PASSWORD 'strong_password';
   CREATE DATABASE werco_erp OWNER werco_prod_user;
   ```

2. **Bootstrap the schema (first time, empty database):**

   A bare `alembic upgrade head` against an **empty** database is **not** the
   supported path and will fail — the core tables are created by
   `Base.metadata.create_all()` on first app boot (`001` only adds indexes), and
   `002_add_laser_press_brake_types.py` runs `ALTER TYPE workcentertype ...`,
   which errors if the enum type doesn't exist yet. Instead:
   ```bash
   cd /opt/werco-erp/backend
   source venv/bin/activate

   # 1. Create the schema (first app boot creates it via create_all; or do it explicitly)
   python -m scripts.seed_data            # calls create_all (+ seeds demo data)

   # 2. Mark the DB as already at the migration baseline create_all matches
   alembic stamp <baseline-revision>

   # 3. Apply migrations newer than the baseline
   alembic upgrade head
   ```
   See `docs/DEVELOPMENT.md` → "Database Migrations" → "Bootstrap order" for
   details. After bootstrap, normal incremental `alembic upgrade head` is the
   standard path.

3. **Set up automatic backups:**
   ```bash
   # Add to crontab
   0 2 * * * /usr/bin/python3 /opt/werco-erp/scripts/backup_database.py >> /var/log/werco-erp/backup.log 2>&1
   ```

## Monitoring & Logging

### Log Rotation

Create `/etc/logrotate.d/werco-erp`:
```
/var/log/werco-erp/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0640 werco werco
    sharedscripts
}
```

### Monitoring Setup

- **Sentry** for error tracking (configured via SENTRY_DSN)
- **Prometheus + Grafana** for metrics monitoring
- **Health checks**: `GET /health`
- **Application logs**: Check `/var/log/werco-erp/`

## Backup Strategy

### Automatic Backups

```bash
# Daily database backup at 2 AM
0 2 * * * /usr/bin/python3 /opt/werco-erp/scripts/backup_database.py

# Weekly full backup on Sundays at 3 AM
0 3 * * 0 /usr/bin/rsync -avz /opt/werco-erp /backups/werco-erp-weekly

# Monthly cleanup of old backups
0 4 1 * * /usr/bin/find /backups -name "*.sql.gz" -mtime +30 -delete
```

### Backup Locations

- **Local**: `/backups/`
- **S3**: Configured via AWS credentials
- **Retention**: 30 days

## Scaling Considerations

### Horizontal Scaling

1. **Load balancer**: Use Nginx or HAProxy
2. **Multiple backend instances**: Use `gunicorn --workers N`
3. **Session storage**: Use Redis for shared sessions
4. **Database**: Use connection pooling, consider read replicas

### Vertical Scaling

- Increase server resources (CPU, RAM)
- Optimize database queries
- Add indexes
- Enable caching

## Security Checklist

- [ ] Change default passwords
- [ ] Use strong, random SECRET_KEY
- [ ] Enable firewall (UFW)
- [ ] Configure SSL/TLS
- [ ] Set up rate limiting
- [ ] Enable error monitoring (Sentry)
- [ ] Regular security updates
- [ ] Restrict database access
- [ ] Use fail2ban for brute force protection
- [ ] Regular backup verification
- [ ] Enable audit logging

## Troubleshooting

### Backend not starting

```bash
# Check logs
sudo journalctl -u werco-erp-backend -f

# Check database connection
psql -h localhost -U werco_prod_user -d werco_erp

# Check port availability
sudo netstat -tulpn | grep 8000
```

### Frontend not loading

```bash
# Check build artifacts
ls -la /opt/werco-erp/frontend/build/

# Check Nginx
sudo nginx -t
sudo systemctl status nginx
```

### Performance issues

```bash
# Check system resources
htop

# Check database performance
psql -c "SELECT * FROM pg_stat_activity;"

# Check Redis
redis-cli INFO stats
```

## Updates & Maintenance

### Update Application

```bash
# Pull latest code
cd /opt/werco-erp
git pull origin main

# Backend
cd backend
source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart werco-erp-backend

# Frontend
cd /opt/werco-erp/frontend
npm install
npm run build
sudo systemctl restart werco-erp-frontend
```

### Rolling Updates (Zero Downtime)

1. Deploy new version alongside running instance
2. Switch Nginx upstream to new instance
3. Test new instance
4. Remove old instance

## Disaster Recovery

1. **Have backups**: Database and application code
2. **Document recovery procedures**
3. **Regular restore tests**
4. **Monitoring alerts for failures**
5. **Failover procedures documented**

## Support

For production issues:
- Check logs: `/var/log/werco-erp/`
- Monitor: Sentry dashboard
- Health check: `https://werco-erp.yourdomain.com/health`
- Documentation: `docs/DEVELOPMENT.md`
