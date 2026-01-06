# Deployment Guide

This guide covers deploying Werco ERP to production environments.

## Prerequisites

- Production server with:
  - Python 3.11+
  - Node.js 18+
  - PostgreSQL 15+
  - Redis (recommended for caching)
  - Nginx or similar reverse proxy
- Domain name with SSL certificate
- S3-compatible storage (for file uploads)

## Environment Configuration

### Production .env File

Create a secure `.env` file in the backend directory:

```env
# Database
DATABASE_URL=postgresql://user:STRONG_PASSWORD@localhost:5432/werco_erp

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

# File Storage (S3)
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
S3_BUCKET_NAME=werco-erp-documents-prod

# Monitoring
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
LOG_LEVEL=WARN

# Redis Cache
REDIS_URL=redis://localhost:6379/0

# LLM Integration (optional)
ANTHROPIC_API_KEY=your_anthropic_key
```

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

1. **Update docker-compose.yml** for production:
   ```yaml
   version: '3.8'

   services:
     db:
       image: postgres:15-alpine
       environment:
         POSTGRES_USER: werco_prod_user
         POSTGRES_PASSWORD: ${DB_PASSWORD}
         POSTGRES_DB: werco_erp
       volumes:
         - postgres_data:/var/lib/postgresql/data
       restart: always

     redis:
       image: redis:7-alpine
       restart: always

     backend:
       build: ./backend
       ports:
         - "8000:8000"
       environment:
         - DATABASE_URL=postgresql+psycopg2://werco_prod_user:${DB_PASSWORD}@db:5432/werco_erp
         - REDIS_URL=redis://redis:6379/0
       depends_on:
         - db
         - redis
       restart: always

     frontend:
       build: ./frontend
       ports:
         - "3000:80"
       restart: always

   volumes:
     postgres_data:

   networks:
     default:
       name: werco-prod
   ```

2. **Start services:**
   ```bash
   docker-compose up -d
   ```

3. **Run database migrations:**
   ```bash
   docker-compose exec backend alembic upgrade head
   ```

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

2. **Run migrations:**
   ```bash
   cd /opt/werco-erp/backend
   source venv/bin/activate
   alembic upgrade head
   ```

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
