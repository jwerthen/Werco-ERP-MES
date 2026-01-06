#!/bin/bash
# Werco ERP Production Deployment Script

set -e  # Exit on any error

echo "======================================"
echo "Werco ERP Production Deployment"
echo "======================================"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="/opt/werco-erp"
BACKUP_DIR="$PROJECT_DIR/backups"
LOG_FILE="/var/log/werco-erp/deploy.log"

# Create log directory
mkdir -p "$(dirname "$LOG_FILE")"

# Log function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Check function
check() {
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $1"
        log "✓ $1"
    else
        echo -e "${RED}✗${NC} $1"
        log "✗ $1 - FAILED"
        exit 1
    fi
}

echo -e "${YELLOW}Pre-deployment checks...${NC}"

# 1. Verify environment variables
echo "Checking environment variables..."
if [ -z "$SECRET_KEY" ]; then
    echo -e "${RED}ERROR: SECRET_KEY not set!${NC}"
    exit 1
fi
check "SECRET_KEY configured"

if [ "$DEBUG" = "true" ]; then
    echo -e "${RED}ERROR: DEBUG is true! Must be false in production.${NC}"
    exit 1
fi
check "DEBUG is disabled"

if [ "$ENVIRONMENT" != "production" ]; then
    echo -e "${YELLOW}WARNING: ENVIRONMENT is not 'production'${NC}"
fi

# 2. Backup current deployment
echo ""
echo "Creating backup of current deployment..."
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/pre_deploy_$(date +%Y%m%d_%H%M%S).tar.gz"

if [ -d "$PROJECT_DIR/backend" ]; then
    tar -czf "$BACKUP_FILE" -C "$PROJECT_DIR" backend frontend
    check "Backup created: $BACKUP_FILE"
else
    echo "No existing deployment found, skipping backup"
fi

# 3. Git pull (if applicable)
echo ""
if [ -d "$PROJECT_DIR/.git" ]; then
    echo "Pulling latest code..."
    cd "$PROJECT_DIR"
    git fetch origin main
    git checkout main
    git pull origin main
    check "Code updated from git"
else
    echo "Not a git repository, skipping git pull"
fi

# 4. Install dependencies
echo ""
echo "Installing dependencies..."

# Backend
cd "$PROJECT_DIR/backend"
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt > /dev/null 2>&1
    check "Backend dependencies installed"
fi

# Frontend
cd "$PROJECT_DIR/frontend"
if [ -f "package.json" ]; then
    npm ci --production
    check "Frontend dependencies installed"
    npm run build
    check "Frontend build completed"
fi

# 5. Database migrations
echo ""
echo "Running database migrations..."
cd "$PROJECT_DIR/backend"
alembic upgrade head
check "Database migrations applied"

# 6. Create database backup
echo ""
python scripts/backup_database.py
check "Database backup completed"

# 7. Restart services
echo ""
echo "Restarting services..."
if command -v systemctl &> /dev/null; then
    systemctl restart werco-erp-backend
    systemctl restart werco-erp-frontend
    check "Services restarted (systemd)"
elif command -v docker-compose &> /dev/null; then
    docker-compose down
    docker-compose up -d
    check "Services restarted (docker-compose)"
else
    echo -e "${YELLOW}WARNING: No service manager found, manual restart required${NC}"
fi

# 8. Health checks
echo ""
echo "Performing health checks..."
sleep 5  # Wait for services to start

# Backend health check
if curl -sf http://localhost:8000/health > /dev/null; then
    check "Backend health check passed"
else
    echo -e "${RED}ERROR: Backend health check failed!${NC}"
    exit 1
fi

# Frontend health check
if curl -sf http://localhost:3000 > /dev/null; then
    check "Frontend health check passed"
else
    echo -e "${YELLOW}WARNING: Frontend health check failed (may be loading)${NC}"
fi

# 9. Cleanup old backups (keep last 30 days)
echo ""
echo "Cleaning up old backups..."
find "$BACKUP_DIR" -name "pre_deploy_*.tar.gz" -mtime +30 -delete
check "Old backups cleaned up"

# 10. Deployment complete
echo ""
echo "======================================"
echo -e "${GREEN}Deployment completed successfully!${NC}"
echo "======================================"
log "Deployment completed successfully"

echo ""
echo "Post-deployment checks:"
echo "  - Application: http://localhost:3000"
echo "  - Documentation: http://localhost:8000/api/docs"
echo "  - Health check: http://localhost:8000/health"
echo ""
echo "Logs: $LOG_FILE"
echo "Backup: $BACKUP_FILE"
echo ""

exit 0
